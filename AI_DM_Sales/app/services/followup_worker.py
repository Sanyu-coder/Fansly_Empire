import logging
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.core.database import (
    _parse_iso_datetime,
    add_dm_message,
    get_fan_ppv_transactions,
    get_fan_record,
    get_pending_fomo_reminders,
    get_thread_detail,
    get_unlocked_ppv_ids,
    get_unlocked_ppv_transactions,
    mark_fan_fomo_reminders_sent,
    mark_fomo_reminder_sent,
    record_ppv_offer,
    upsert_fan_sales_state,
)
from app.core.llm_client import llm_client, execute_with_resilient_retry, LLMResilientRetryExhaustedError
from app.services.character_loader import (
    is_vault_item_sendable_to_customer,
    load_dm_persona,
    load_plans,
    load_settings,
    load_vault_items,
)
from app.services.fansly_dm_bot import bot_manager, check_char_sleep_state
from app.services.sales_engine import sales_engine

logger = logging.getLogger("AI_DM_Sales.FollowupWorker")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)


class FollowupWorker:
    """
    バックグラウンド定期実行ワーカー：
    1. 未開封PPVのFOMOリマインド (送信後6〜12時間経過 & is_unlocked == 0 & reminder_sent == 0)
       - 顧客の緊急停止・ミュート・省エネフラグを確認
       - 希少性 (FOMO) を煽る軽めの追撃メッセージ（英語＋日本語訳）を1回だけ送信キューへ投入し、reminder_sent = 1 に更新
    2. Step 1購入直後のStep 2追撃提案 (Step 1 is_unlocked == 1 から30分〜数時間経過した熱量の高い顧客)
       - 未購入の Step 2 動画/画像を自動選定・動的価格算出して送信キューへ投入し、ppv_transactions に記録
    """

    def __init__(
        self,
        interval_sec: float = 60.0,
        fomo_min_hours: float = 6.0,
        fomo_max_hours: Optional[float] = 12.0,
        step2_min_minutes: float = 30.0,
        step2_max_hours: float = 6.0,
        llm=None,
        bot_mgr=None,
    ):
        self.interval_sec = interval_sec
        self.fomo_min_hours = fomo_min_hours
        self.fomo_max_hours = fomo_max_hours
        self.step2_min_minutes = step2_min_minutes
        self.step2_max_hours = step2_max_hours
        self.llm = llm or llm_client
        self.bot_mgr = bot_mgr or bot_manager

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.running: bool = False
        self.last_run_at: Optional[str] = None
        self.total_fomo_sent: int = 0
        self.total_step2_sent: int = 0

    def start(self) -> bool:
        with self._lock:
            if self.running and self._thread and self._thread.is_alive():
                return True
            self._stop_event.clear()
            self.running = True
            self._thread = threading.Thread(
                target=self._worker_loop,
                name="FollowupWorker",
                daemon=True,
            )
            self._thread.start()
            logger.info("FollowupWorker background thread started.")
            return True

    def stop(self, timeout: float = 3.0) -> bool:
        with self._lock:
            self._stop_event.set()
            self.running = False
            thread = self._thread
            self._thread = None
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        logger.info("FollowupWorker background thread stopped.")
        return True

    def _is_fan_busy_in_session_or_unanswered(
        self,
        char_id: str,
        fan_id: str,
        db_path: Optional[str] = None,
    ) -> bool:
        """
        対象ファンに対して:
        1) BotManager / CharacterBotWorker 側で自動返信カウントダウン中または送信待ちキューがある、または
        2) DB上の最新メッセージがファンからの未返信メッセージ (sender == 'fan') である
        場合は、通常返信とのセッション競合・追い越し送信を防ぐため追撃を見送る。
        """
        if self.bot_mgr is not None and hasattr(self.bot_mgr, "has_active_session_activity"):
            try:
                if self.bot_mgr.has_active_session_activity(char_id, fan_id):
                    return True
            except Exception:
                pass
        elif self.bot_mgr is not None and hasattr(self.bot_mgr, "get_pending_replies"):
            try:
                pend = self.bot_mgr.get_pending_replies(char_id=char_id, fan_id=fan_id)
                if pend:
                    return True
            except Exception:
                pass

        try:
            detail = get_thread_detail(char_id, fan_id, db_path=db_path) or {}
            msgs = detail.get("messages") or []
            if msgs and str(msgs[-1].get("sender") or "") == "fan":
                return True
        except Exception:
            pass
        return False

    def generate_fomo_reminder_messages(
        self,
        char_id: str,
        username: str,
        offered_price: float,
        ppv_id: Optional[str] = None,
        pending_count: int = 1,
    ) -> Dict[str, str]:
        """
        キャラペルソナに基づき、希少性（FOMO）を煽る軽めの追撃メッセージ（英語＋日本語訳）を生成する。
        複数件の未開封PPVがある場合でも1通の自然なリマインドに集約する。
        """
        persona = load_dm_persona(char_id)
        display_name = persona.get("display_name") or char_id
        tone = persona.get("tone_and_style") or "Playful, teasing, affectionate."
        nickname = persona.get("user_nickname_default") or "babe"

        if pending_count > 1:
            fallback_en = (
                f"Hey {nickname}... I'm getting a little shy leaving those secret clips in our chat🙈 "
                f"I might delete them tonight, so peek before I take them down 🖤💋"
            )
            fallback_ja = (
                "ねえ…さっき送った秘密のクリップたち、なんだか恥ずかしくなってきたから今日中に消しちゃうかも🙈 "
                "消す前にこっそり覗いてみてね🖤💋"
            )
            clip_desc = (
                f"the {pending_count} secret PPV clips you sent earlier "
                f"(latest one is ${offered_price:.2f})"
            )
            user_prompt = (
                f"Fan username: {username}, Unopened PPV count: {pending_count}, "
                f"Latest PPV ID: {ppv_id or 'secret_clip'}, Latest Price: ${offered_price:.2f}"
            )
        else:
            fallback_en = (
                f"Hey {nickname}... I'm getting a little shy leaving that secret clip in our chat🙈 "
                f"I might delete it tonight, so peek at it for ${offered_price:.2f} before I take it down 🖤💋"
            )
            fallback_ja = (
                f"ねえ…さっき送った秘密の動画、なんだか恥ずかしくなってきたから今日中に消しちゃうかも🙈 "
                f"消す前に ${offered_price:.2f} でこっそり覗いてみてね🖤💋"
            )
            clip_desc = f"the secret PPV clip you sent a few hours ago (${offered_price:.2f})"
            user_prompt = f"Fan username: {username}, PPV ID: {ppv_id or 'secret_clip'}, Price: ${offered_price:.2f}"

        system_prompt = (
            f"You are {display_name}, a Fansly creator.\n"
            f"Tone & Style: {tone}\n"
            f"Write ONE short, playful, non-pushy FOMO reminder DM (1-2 sentences) to a fan who hasn't unlocked {clip_desc}.\n"
            "Hint that you're feeling shy and might delete it tonight if they don't peek soon.\n"
            "Output ONLY the raw English DM text."
        )

        try:
            msg_en = execute_with_resilient_retry(
                lambda: self.llm.generate_text(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    temperature=0.7,
                    max_tokens=120,
                    fallback_text=fallback_en,
                ),
                task_name="fomo_reminder_generate",
            )
        except LLMResilientRetryExhaustedError:
            msg_en = fallback_en
        msg_ja = self.llm.translate_to_japanese(msg_en) if msg_en != fallback_en else fallback_ja
        return {"en": msg_en, "ja": msg_ja}

    def process_unopened_ppv_reminders(
        self,
        char_id: Optional[str] = None,
        now_dt: Optional[datetime] = None,
        ignore_sleep: bool = False,
        db_path: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        ppv_transactions から「送信後6〜12時間経過」「is_unlocked == 0」「reminder_sent == 0」を抽出し、
        (char_id, fan_id) 単位で集約する。
        同一ファンに未追撃PPVが複数件存在する場合でも、追撃の生成・送信は1通のみに集約し、
        該当ファンの未追撃レコードをすべて一括で reminder_sent = 1 に更新する。
        """
        candidates = get_pending_fomo_reminders(
            min_hours=self.fomo_min_hours,
            max_hours=self.fomo_max_hours,
            char_id=char_id,
            now_dt=now_dt,
            db_path=db_path,
        )
        grouped_by_fan: Dict[tuple, List[Dict[str, Any]]] = {}
        for item in candidates:
            cid = str(item["char_id"])
            fid = str(item["fan_id"])
            grouped_by_fan.setdefault((cid, fid), []).append(item)

        sent_list: List[Dict[str, Any]] = []

        for (cid, fid), fan_items in grouped_by_fan.items():
            # 代表レコードは最新のオファー（offered_at ASC, tx_id ASC の末尾）を採用
            rep_item = fan_items[-1]
            tx_ids = [int(it["tx_id"]) for it in fan_items]
            tx_id = int(rep_item["tx_id"])
            uname = str(rep_item.get("username") or fid)
            price = float(rep_item.get("offered_price") or 9.99)
            ppv_id = rep_item.get("ppv_id")

            if any(int(it.get("is_escalated") or 0) == 1 for it in fan_items):
                continue
            if any(int(it.get("is_muted") or 0) == 1 for it in fan_items):
                continue
            if any(int(it.get("is_low_priority") or 0) == 1 for it in fan_items):
                continue

            if not ignore_sleep:
                if not bool(load_settings(cid).get("bot_enabled", False)):
                    continue
                sleeping, _ = check_char_sleep_state(cid, now_dt=now_dt)
                if sleeping:
                    continue

            fan_rec = get_fan_record(cid, fid, db_path=db_path) or {}
            if int(fan_rec.get("manual_hold") or 0) == 1:
                continue

            if self._is_fan_busy_in_session_or_unanswered(cid, fid, db_path=db_path):
                logger.info(
                    f"[{cid}] Deferring FOMO reminder for @{fid}: active reply countdown/outbox or unanswered fan DM"
                )
                continue

            msgs = self.generate_fomo_reminder_messages(
                char_id=cid,
                username=uname,
                offered_price=price,
                ppv_id=ppv_id,
                pending_count=len(fan_items),
            )
            msg_en = msgs["en"]
            msg_ja = msgs["ja"]

            # LLM生成中にファンから新着DMが届いていないか再確認
            if self._is_fan_busy_in_session_or_unanswered(cid, fid, db_path=db_path):
                logger.info(
                    f"[{cid}] Deferring FOMO reminder post-generation for @{fid}: active reply countdown/outbox or unanswered fan DM"
                )
                continue

            queued = self.bot_mgr.enqueue_outbound_message(
                char_id=cid,
                fan_id=fid,
                username=uname,
                message_en=msg_en,
                message_ja=msg_ja,
                offered_price=price,
                source="fomo_reminder",
            )
            if isinstance(queued, dict) and queued.get("skipped_active_pending_reply"):
                continue

            marked_count = mark_fan_fomo_reminders_sent(
                char_id=cid,
                fan_id=fid,
                tx_ids=tx_ids,
                db_path=db_path,
            )
            if marked_count <= 0:
                continue

            add_dm_message(
                char_id=cid,
                fan_id=fid,
                sender="ai",
                content=msg_en,
                sales_phase=str(rep_item.get("sales_phase") or "4_Lock_Close"),
                content_ja=msg_ja,
                db_path=db_path,
            )

            with self._lock:
                self.total_fomo_sent += 1

            sent_list.append(
                {
                    "tx_id": tx_id,
                    "tx_ids": tx_ids,
                    "marked_tx_count": marked_count,
                    "char_id": cid,
                    "fan_id": fid,
                    "ppv_id": ppv_id,
                    "ppv_ids": [it.get("ppv_id") for it in fan_items],
                    "offered_price": price,
                    "elapsed_hours": rep_item.get("elapsed_hours"),
                    "message_en": msg_en,
                    "message_ja": msg_ja,
                    "reminder_sent": 1,
                    "queued": queued,
                }
            )
        return sent_list

    def generate_step2_pitch_messages(
        self,
        char_id: str,
        username: str,
        step2_ppv: Dict[str, Any],
        offered_price: float,
    ) -> Dict[str, str]:
        """Step 1購入後の熱量が高い顧客向けに、Step 2（本命続編）を提案する日英メッセージを生成する"""
        persona = load_dm_persona(char_id)
        display_name = persona.get("display_name") or char_id
        tone = persona.get("tone_and_style") or "Playful, teasing, affectionate."
        nickname = persona.get("user_nickname_default") or "babe"
        pitch_hint = str(step2_ppv.get("pitch_hint") or "It's the hotter continuation of what you just saw.").strip()

        fallback_en = (
            f"Thank you so much for unlocking my clip earlier, {nickname} 🖤 "
            f"Since you loved it, I kept the much hotter Step 2 continuation secret... {pitch_hint} "
            f"Want to unlock it right now for ${offered_price:.2f}? 😈💋"
        )
        fallback_ja = (
            f"さっきは秘密の動画を開けてくれて本当にありがとう♡ "
            f"気に入ってくれたあなただけに、もっと過激な続編（Step 2）を用意したの… "
            f"今すぐ ${offered_price:.2f} で覗いてみる？😈💋"
        )

        system_prompt = (
            f"You are {display_name}, a Fansly creator.\n"
            f"Tone & Style: {tone}\n"
            "The fan just unlocked your Step 1 intro PPV clip recently and is high-intent.\n"
            f"Pitch your Step 2 main secret clip ({pitch_hint}) priced at ${offered_price:.2f} in 2 sentences.\n"
            "Output ONLY the raw English DM text."
        )
        user_prompt = f"Fan: {username}, Step 2 PPV ID: {step2_ppv.get('ppv_id')}, Price: ${offered_price:.2f}"

        try:
            msg_en = execute_with_resilient_retry(
                lambda: self.llm.generate_text(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    temperature=0.7,
                    max_tokens=150,
                    fallback_text=fallback_en,
                ),
                task_name="step2_followup_generate",
            )
        except LLMResilientRetryExhaustedError:
            msg_en = fallback_en
        msg_ja = self.llm.translate_to_japanese(msg_en) if msg_en != fallback_en else fallback_ja
        return {"en": msg_en, "ja": msg_ja}

    def process_step2_followups(
        self,
        char_id: Optional[str] = None,
        now_dt: Optional[datetime] = None,
        ignore_sleep: bool = False,
        db_path: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        is_unlocked == 1 となった Step 1 購入から30分〜数時間経過した熱量の高い顧客に対し、
        未購入・未提案の Step 2 動画/画像を自動提案し、ppv_transactions に記録＆送信キューへ投入する。
        """
        ref_now = now_dt or datetime.now()
        unlocked_rows = get_unlocked_ppv_transactions(char_id=char_id, db_path=db_path)
        vault_cache: Dict[str, List[Dict[str, Any]]] = {}
        plans_cache: Dict[str, Dict[str, Any]] = {}
        processed_fans: set = set()
        results: List[Dict[str, Any]] = []

        for row in unlocked_rows:
            cid = str(row["char_id"])
            fid = str(row["fan_id"])
            fan_key = (cid, fid)
            if fan_key in processed_fans:
                continue

            if int(row.get("is_escalated") or 0) == 1:
                continue
            if int(row.get("is_muted") or 0) == 1:
                continue
            if int(row.get("is_low_priority") or 0) == 1:
                continue

            if not ignore_sleep:
                if not bool(load_settings(cid).get("bot_enabled", False)):
                    continue
                sleeping, _ = check_char_sleep_state(cid, now_dt=now_dt)
                if sleeping:
                    continue

            fan_rec = get_fan_record(cid, fid, db_path=db_path) or {}
            if int(fan_rec.get("manual_hold") or 0) == 1:
                continue

            if self._is_fan_busy_in_session_or_unanswered(cid, fid, db_path=db_path):
                logger.info(
                    f"[{cid}] Deferring Step 2 followup for @{fid}: active reply countdown/outbox or unanswered fan DM"
                )
                continue

            if cid not in vault_cache:
                vault_cache[cid] = load_vault_items(cid)
            if cid not in plans_cache:
                plans_cache[cid] = load_plans(cid)

            vault_items = vault_cache[cid]
            step_by_id = {
                str(v.get("ppv_id")): int(v.get("ladder_step") or 1)
                for v in vault_items
                if v.get("ppv_id")
            }

            unlocked_ppv_id = str(row.get("ppv_id") or "")
            if step_by_id.get(unlocked_ppv_id, 1) != 1:
                continue

            unlocked_dt = _parse_iso_datetime(row.get("unlocked_at") or row.get("offered_at"))
            if not unlocked_dt:
                continue
            elapsed_minutes = (ref_now - unlocked_dt).total_seconds() / 60.0
            if elapsed_minutes < float(self.step2_min_minutes):
                continue
            if elapsed_minutes > float(self.step2_max_hours) * 60.0:
                continue

            # 既に Step 2 以上のオファーまたは購入がある場合は重複提案しない
            all_fan_txs = get_fan_ppv_transactions(cid, fid, db_path=db_path)
            has_step2_or_higher = any(
                step_by_id.get(str(tx.get("ppv_id") or ""), 1) >= 2 for tx in all_fan_txs
            )
            if has_step2_or_higher:
                processed_fans.add(fan_key)
                continue

            unlocked_ids = set(get_unlocked_ppv_ids(cid, fid, db_path=db_path))
            step2_candidates = [
                v
                for v in vault_items
                if bool(v.get("is_active", True))
                and is_vault_item_sendable_to_customer(cid, v)
                and int(v.get("ladder_step") or 1) == 2
                and str(v.get("ppv_id") or "") not in unlocked_ids
            ]
            if not step2_candidates:
                processed_fans.add(fan_key)
                continue

            chosen_step2 = dict(step2_candidates[0])
            total_spent = float(row.get("total_spent_usd") or 0.0)
            current_tier = str(row.get("current_tier") or "free_follower")
            pricing_mode = sales_engine.determine_pricing_mode(
                total_spent_usd=max(total_spent, float(row.get("offered_price") or 5.99)),
                current_tier=current_tier,
                plans=plans_cache[cid],
            )
            if pricing_mode == "whale":
                offered_price = float(chosen_step2.get("whale_price_usd") or 34.99)
            elif pricing_mode == "tripwire":
                offered_price = float(chosen_step2.get("tripwire_price_usd") or 14.99)
            else:
                offered_price = float(chosen_step2.get("base_price_usd") or 19.99)

            chosen_step2["offered_price_usd"] = round(offered_price, 2)
            chosen_step2["pricing_mode"] = pricing_mode
            self._record_and_enqueue_step2(
                cid=cid,
                fid=fid,
                row=row,
                unlocked_ppv_id=unlocked_ppv_id,
                chosen_step2=chosen_step2,
                offered_price=offered_price,
                pricing_mode=pricing_mode,
                elapsed_minutes=elapsed_minutes,
                ref_now=ref_now,
                results=results,
                db_path=db_path,
            )
            processed_fans.add(fan_key)
        return results

    def _record_and_enqueue_step2(
        self,
        cid: str,
        fid: str,
        row: Dict[str, Any],
        unlocked_ppv_id: str,
        chosen_step2: Dict[str, Any],
        offered_price: float,
        pricing_mode: str,
        elapsed_minutes: float,
        ref_now: datetime,
        results: List[Dict[str, Any]],
        db_path: Optional[str] = None,
    ) -> None:
        uname = str(row.get("username") or fid)
        msgs = self.generate_step2_pitch_messages(
            char_id=cid,
            username=uname,
            step2_ppv=chosen_step2,
            offered_price=offered_price,
        )
        msg_en = msgs["en"]
        msg_ja = msgs["ja"]

        # LLM生成中にファンから新着DMが届いていないか再確認
        if self._is_fan_busy_in_session_or_unanswered(cid, fid, db_path=db_path):
            logger.info(
                f"[{cid}] Deferring Step 2 followup post-generation for @{fid}: active reply countdown/outbox or unanswered fan DM"
            )
            return

        queued = self.bot_mgr.enqueue_outbound_message(
            char_id=cid,
            fan_id=fid,
            username=uname,
            message_en=msg_en,
            message_ja=msg_ja,
            ppv_item=chosen_step2,
            offered_price=offered_price,
            source="step2_followup",
        )
        if isinstance(queued, dict) and queued.get("skipped_active_pending_reply"):
            return

        tx_id = record_ppv_offer(
            char_id=cid,
            fan_id=fid,
            ppv_id=str(chosen_step2.get("ppv_id") or ""),
            offered_price=offered_price,
            tx_type="ppv",
            offered_at=ref_now.strftime("%Y-%m-%dT%H:%M:%S"),
            db_path=db_path,
        )
        add_dm_message(
            char_id=cid,
            fan_id=fid,
            sender="ai",
            content=msg_en,
            sales_phase="4_Lock_Close",
            content_ja=msg_ja,
            ppv_id=str(chosen_step2.get("ppv_id") or ""),
            ppv_price_usd=offered_price,
            tx_id=tx_id,
            db_path=db_path,
        )
        upsert_fan_sales_state(
            char_id=cid,
            fan_id=fid,
            sales_phase="4_Lock_Close",
            db_path=db_path,
        )

        with self._lock:
            self.total_step2_sent += 1
        results.append(
            {
                "tx_id": tx_id,
                "char_id": cid,
                "fan_id": fid,
                "trigger_step1_ppv_id": unlocked_ppv_id,
                "offered_step2_ppv_id": chosen_step2.get("ppv_id"),
                "offered_price": offered_price,
                "pricing_mode": pricing_mode,
                "elapsed_minutes": round(elapsed_minutes, 1),
                "message_en": msg_en,
                "message_ja": msg_ja,
                "queued": queued,
            }
        )

    def run_once(
        self,
        char_id: Optional[str] = None,
        now_dt: Optional[datetime] = None,
        ignore_sleep: bool = False,
        db_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """FOMOリマインドチェックとStep 2追撃提案チェックを1サイクル実行する"""
        self.last_run_at = (now_dt or datetime.now()).strftime("%Y-%m-%dT%H:%M:%S")
        fomo_sent = self.process_unopened_ppv_reminders(
            char_id=char_id,
            now_dt=now_dt,
            ignore_sleep=ignore_sleep,
            db_path=db_path,
        )
        step2_sent = self.process_step2_followups(
            char_id=char_id,
            now_dt=now_dt,
            ignore_sleep=ignore_sleep,
            db_path=db_path,
        )
        return {
            "ok": True,
            "ran_at": self.last_run_at,
            "fomo_reminders_sent": fomo_sent,
            "step2_followups_sent": step2_sent,
        }

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "running": bool(self.running),
                "last_run_at": self.last_run_at,
                "total_fomo_sent": self.total_fomo_sent,
                "total_step2_sent": self.total_step2_sent,
                "interval_sec": self.interval_sec,
            }

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set() and self.running:
            try:
                self.run_once()
            except Exception as e:
                logger.error(f"FollowupWorker loop error: {e}")

            end_time = time.time() + self.interval_sec
            while time.time() < end_time:
                if self._stop_event.is_set() or not self.running:
                    break
                time.sleep(min(0.5, max(0.05, end_time - time.time())))


followup_worker = FollowupWorker()



