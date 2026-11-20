import json
import logging
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.core.database import (
    add_dm_message,
    get_active_playbook_for_group,
    get_dormant_fans,
    get_thread_detail,
    upsert_fan_sales_state,
)
from app.core.llm_client import llm_client
from app.services.character_loader import list_character_ids, load_dm_persona, load_settings
from app.services.fansly_dm_bot import bot_manager, check_char_sleep_state
from app.services.profiler_service import format_psych_profile_for_prompt

logger = logging.getLogger("AI_DM_Sales.DormantRevivalWorker")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)


def _build_personalized_fallback_revival_en(
    persona: Dict[str, Any],
    username: str,
    psych_profile: Dict[str, Any],
) -> str:
    """Ollamaオフライン時でも顧客カルテ (好み・話題) を反映した個別再会メッセージを生成する"""
    nickname = (persona.get("user_nickname_default") or "babe").split("/")[0].strip()
    prefs = psych_profile.get("preferences") or psych_profile.get("fetishes") or []
    hooks = psych_profile.get("hooks") or psych_profile.get("hot_topics") or []
    signals = " ".join(
        [str(x) for x in (prefs if isinstance(prefs, list) else [prefs])]
        + [str(x) for x in (hooks if isinstance(hooks, list) else [hooks])]
    )
    if any(k in signals for k in ["寝室", "ベッド", "夜", "bedroom"]):
        hook_line = "I was lying in bed tonight remembering how much you loved my late-night bedroom vibes"
    elif any(k in signals for k in ["コスプレ", "ランジェリー", "衣装", "cosplay", "lingerie"]):
        hook_line = "I just tried on a teasing new lingerie outfit tonight and immediately thought of your taste"
    elif any(k in signals for k in ["無修正", "フル", "秘密", "uncensored", "secret"]):
        hook_line = "I filmed something extra naughty in private earlier and remembered how much you enjoy our little secrets"
    else:
        hook_line = "I was thinking about our cozy private chats earlier and realized how much I missed your energy"

    clean_name = (username or nickname).lstrip("@")
    return (
        f"Hey {clean_name}~ it's been a little while and I really missed talking to you {nickname} 🖤 "
        f"{hook_line}... how have you been lately? 💋✨"
    )


def generate_personalized_revival_dm(
    char_id: str,
    fan_record: Dict[str, Any],
    recent_messages: Optional[List[Dict[str, Any]]] = None,
    llm=None,
    db_path: Optional[str] = None,
) -> Dict[str, str]:
    """
    顧客カルテ (psych_profile_json) とキャラ設定 (dm_persona.json) を参照し、
    ローカルOllama (/api/generate + think=False) で個別再会DM（英語 + 日本語訳）を生成する。
    """
    client = llm or llm_client
    persona = load_dm_persona(char_id)
    display_name = persona.get("display_name") or char_id
    tone_and_style = persona.get("tone_and_style") or "Alluring, affectionate, playful."
    first_person = persona.get("first_person") or "I"
    nickname = (persona.get("user_nickname_default") or "babe").split("/")[0].strip()
    forbidden = ", ".join(persona.get("forbidden_topics") or ["minors", "refunds", "AI/bot"])

    username = str(fan_record.get("username") or fan_record.get("fan_id") or nickname)
    total_spent = float(fan_record.get("total_spent_usd") or 0.0)
    inactive_days_actual = float(fan_record.get("inactive_days_actual") or 7.0)
    ab_group = str(fan_record.get("ab_group") or "A")

    raw_prof = fan_record.get("psych_profile") or fan_record.get("psych_profile_json") or "{}"
    if isinstance(raw_prof, str):
        try:
            psych_profile = json.loads(raw_prof)
        except Exception:
            psych_profile = {}
    elif isinstance(raw_prof, dict):
        psych_profile = raw_prof
    else:
        psych_profile = {}

    profile_block = format_psych_profile_for_prompt(psych_profile)
    playbook = get_active_playbook_for_group(char_id, ab_group=ab_group, db_path=db_path)
    playbook_rules = str(playbook.get("rules_text") or "").strip()

    recent_lines = [
        f"{'Fan' if m.get('sender') == 'fan' else display_name}: {m.get('content', '')}"
        for m in (recent_messages or [])[-4:]
    ]
    history_snippet = "\n".join(recent_lines) if recent_lines else "(No recent chat snippet)"

    system_prompt = (
        f"You are {display_name}, chatting via private DM on Fansly.\n"
        f"- First Person: {first_person} | Pet Name: {nickname} | Style: {tone_and_style}\n"
        f"- Forbidden: {forbidden}\n\n"
        f"{profile_block}\n\n"
        f"[Active Playbook Rules]\n{playbook_rules}\n\n"
        "[Task: Personalized Dormant VIP Fan Revival DM]\n"
        f"Paying supporter @{username} (spent ${total_spent:.2f}) has been quiet for ~{int(round(inactive_days_actual))} days.\n"
        "Write a warm, seductive, non-templated check-in & reunion hook DM in English (2-3 short sentences max).\n"
        "Weave in their specific preferences/fetishes/hooks from the profile so they feel remembered, "
        "ask how they've been, and softly tease something new they'd love without hard-selling a price yet.\n"
        "Output ONLY the English DM text."
    )
    user_prompt = f"Past Context:\n{history_snippet}\n\nWrite personalized English reunion DM to @{username}:"

    fallback_en = _build_personalized_fallback_revival_en(persona, username, psych_profile)
    msg_en = client.generate_text(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        temperature=0.75,
        max_tokens=220,
        fallback_text=fallback_en,
    ).strip() or fallback_en

    msg_ja = client.translate_to_japanese(msg_en)
    return {
        "content": msg_en,
        "content_ja": msg_ja,
        "prompt_version": f"v{playbook.get('version_id', 1)}-{ab_group}-revival",
    }


def run_dormant_revival(
    char_id: Optional[str] = None,
    inactive_days: int = 7,
    max_targets: int = 10,
    ignore_sleep: bool = True,
    now_dt: Optional[datetime] = None,
    db_path: Optional[str] = None,
    llm=None,
    bot_mgr=None,
) -> Dict[str, Any]:
    """
    休眠ファン掘り起こし個別DMバッチ：
    1. total_spent_usd > 0 / last_contact_at から inactive_days 日以上経過 /
       is_escalated == 0, is_muted == 0, is_low_priority == 0 の休眠ファンを抽出
    2. 各ファンの日本語顧客カルテ (psych_profile_json) とキャラ設定 (dm_persona.json) から個別DM（日英）を生成
    3. dm_messages に保存し、last_contact_at を更新（二重送信防止）した上で bot_manager.enqueue_outbound_message() へ投入
    """
    ref_now = now_dt or datetime.now()
    ref_now_iso = ref_now.strftime("%Y-%m-%dT%H:%M:%S")
    target_bot_mgr = bot_mgr or bot_manager

    candidates = get_dormant_fans(
        char_id=char_id,
        inactive_days=inactive_days,
        max_targets=max_targets,
        now_dt=ref_now,
        db_path=db_path,
    )

    sent_results: List[Dict[str, Any]] = []
    skipped_sleeping: List[str] = []

    for fan in candidates:
        cid = str(fan.get("char_id") or "")
        fid = str(fan.get("fan_id") or "")
        uname = str(fan.get("username") or fid)
        if not cid or not fid:
            continue

        if not ignore_sleep:
            sleeping, _ = check_char_sleep_state(cid, now_dt=ref_now)
            if sleeping:
                skipped_sleeping.append(f"{cid}:@{uname}")
                continue

        detail = get_thread_detail(cid, fid, db_path=db_path) or {}
        recent_msgs = detail.get("messages") or []

        generated = generate_personalized_revival_dm(
            char_id=cid,
            fan_record=fan,
            recent_messages=recent_msgs,
            llm=llm,
            db_path=db_path,
        )
        msg_en = generated["content"]
        msg_ja = generated["content_ja"]
        prompt_ver = generated["prompt_version"]

        msg_row = add_dm_message(
            char_id=cid,
            fan_id=fid,
            sender="ai",
            content=msg_en,
            sales_phase="2_Explore",
            prompt_version=prompt_ver,
            content_ja=msg_ja,
            db_path=db_path,
        )
        upsert_fan_sales_state(
            char_id=cid,
            fan_id=fid,
            sales_phase="2_Explore",
            last_contact_at=ref_now_iso,
            db_path=db_path,
        )

        queued = target_bot_mgr.enqueue_outbound_message(
            char_id=cid,
            fan_id=fid,
            username=uname,
            message_en=msg_en,
            message_ja=msg_ja,
            ppv_item=None,
            offered_price=None,
            source="dormant_revival",
        )
        logger.info(
            f"[{cid}] Dormant revival DM generated & queued for @{uname} "
            f"(spent=${float(fan.get('total_spent_usd') or 0.0):.2f}, inactive={fan.get('inactive_days_actual')}d)"
        )
        sent_results.append(
            {
                "char_id": cid,
                "fan_id": fid,
                "username": uname,
                "total_spent_usd": float(fan.get("total_spent_usd") or 0.0),
                "inactive_days_actual": fan.get("inactive_days_actual"),
                "msg_id": msg_row.get("msg_id"),
                "content": msg_en,
                "content_ja": msg_ja,
                "prompt_version": prompt_ver,
                "queued": queued,
            }
        )

    return {
        "ok": True,
        "ran_at": ref_now_iso,
        "char_id": char_id or "all",
        "inactive_days": int(inactive_days),
        "candidates_found": len(candidates),
        "revived_count": len(sent_results),
        "skipped_sleeping": skipped_sleeping,
        "results": sent_results,
    }


class DormantRevivalWorker:
    """
    休眠課金ファン掘り起こしのバックグラウンド定期実行ワーカー。
    - バックグラウンド定期ループでは睡眠時間制御 (ignore_sleep=False) を尊重
    - 管理画面からの手動即時実行では run_dormant_revival(..., ignore_sleep=True) で即座に生成＆送信キュー投入可能
    """

    def __init__(
        self,
        interval_sec: float = 3600.0,
        inactive_days: int = 7,
        max_targets_per_cycle: int = 10,
        llm=None,
        bot_mgr=None,
    ):
        self.interval_sec = interval_sec
        self.inactive_days = inactive_days
        self.max_targets_per_cycle = max_targets_per_cycle
        self.llm = llm
        self.bot_mgr = bot_mgr

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.running: bool = False
        self.last_run_at: Optional[str] = None
        self.total_revived_sent: int = 0

    def start(self) -> bool:
        with self._lock:
            if self.running and self._thread and self._thread.is_alive():
                return True
            self._stop_event.clear()
            self.running = True
            self._thread = threading.Thread(
                target=self._worker_loop,
                name="DormantRevivalWorker",
                daemon=True,
            )
            self._thread.start()
            logger.info("DormantRevivalWorker background thread started.")
            return True

    def stop(self, timeout: float = 3.0) -> bool:
        with self._lock:
            self._stop_event.set()
            self.running = False
            thread = self._thread
            self._thread = None
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        logger.info("DormantRevivalWorker background thread stopped.")
        return True

    def run_once(
        self,
        char_id: Optional[str] = None,
        inactive_days: Optional[int] = None,
        max_targets: Optional[int] = None,
        ignore_sleep: bool = False,
        now_dt: Optional[datetime] = None,
        db_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        res = run_dormant_revival(
            char_id=char_id,
            inactive_days=self.inactive_days if inactive_days is None else int(inactive_days),
            max_targets=self.max_targets_per_cycle if max_targets is None else int(max_targets),
            ignore_sleep=ignore_sleep,
            now_dt=now_dt,
            db_path=db_path,
            llm=self.llm,
            bot_mgr=self.bot_mgr,
        )
        with self._lock:
            self.last_run_at = res.get("ran_at")
            self.total_revived_sent += int(res.get("revived_count") or 0)
        return res

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "running": bool(self.running),
                "last_run_at": self.last_run_at,
                "total_revived_sent": self.total_revived_sent,
                "interval_sec": self.interval_sec,
                "inactive_days": self.inactive_days,
            }

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set() and self.running:
            end_time = time.time() + self.interval_sec
            while time.time() < end_time:
                if self._stop_event.is_set() or not self.running:
                    return
                time.sleep(min(1.0, max(0.05, end_time - time.time())))
            if self._stop_event.is_set() or not self.running:
                break
            try:
                for cid in list_character_ids():
                    if not bool(load_settings(cid).get("bot_enabled", False)):
                        continue
                    self.run_once(char_id=cid, ignore_sleep=False)
            except Exception as e:
                logger.error(f"DormantRevivalWorker loop error: {e}")


dormant_revival_worker = DormantRevivalWorker()


