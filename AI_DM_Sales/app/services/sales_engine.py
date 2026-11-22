import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from app.core import config
from app.core.database import (
    DEFAULT_STAGE_THRESHOLDS,
    append_fan_decision_log,
    get_active_playbook_for_group,
    get_fan_record,
    get_system_stage_thresholds,
    get_unlocked_ppv_ids,
    normalize_stage_thresholds,
    record_ppv_offer,
    upsert_fan_sales_state,
)
from app.core.llm_client import LLMClient, llm_client, execute_with_resilient_retry, LLMResilientRetryExhaustedError
from app.services.character_loader import (
    get_char_paths,
    is_vault_item_sendable_to_customer,
    load_dm_persona,
    load_plans,
    load_settings,
    load_vault_items,
)
from app.services.media_blur_service import (
    get_blur_preview_relpath,
    resolve_existing_blur_preview_path,
)
from app.services.profiler_service import (
    format_psych_profile_for_prompt,
    update_fan_psych_profile,
)


class SalesEngine:
    """
    S.E.L.L. 営業エンジン本体。
    - 危険ワード・連続未読スルーによる緊急停止判定 (is_escalated = 1)
    - タダ乗り客省エネ降格 (6_Low_Priority / is_low_priority = 1)
    - S.E.L.L. フェーズ遷移ステートマシン (1_Icebreak -> 2_Explore -> 3_Lead_Pitch -> 4_Lock_Close -> 5_Cooldown)
    - 階段式PPV選定 (既購入除外 + Step 1 -> Step 2 -> Step 3) ＆ 動的プライシング (tripwire / base / whale)
    - キャラクター口調・サブスクプラン・顧客カルテ連動プロンプト構築＆日英生成
    """

    PHASES = [
        "1_Icebreak",
        "2_Discovery",
        "2_Explore",
        "3_Lead_Pitch",
        "4_Lock_Close",
        "4_Closing",
        "5_Cooldown",
        "6_Low_Priority",
    ]

    EMOJI_PALETTE = [
        "💜", "🖤", "👑", "🥀", "💋", "✨", "😈", "🥂",
        "🌙", "🔥", "🥺", "💕", "🎀", "🤫", "😉", "💫",
    ]

    TIP_CONFIRM_REGEX = re.compile(
        r"(\b(tip|tips|tipping)\b|\bspoil\s+(me|your|this)\b|\btreat\s+(me|your)\b|\blittle\s+gift\b|\bbuy\s+me\s+(a\s+)?coffee\b)",
        re.IGNORECASE,
    )

    MIN_TURNS_BEFORE_PITCH_NEW_FAN = 3
    MAX_CONSECUTIVE_UNANSWERED = 2
    LOW_PRIORITY_PPV_IGNORED_THRESHOLD = 2
    WHALE_SPENT_THRESHOLD_USD = 50.0

    HIGH_INTENT_KEYWORDS = [
        "secret",
        "video",
        "videos",
        "clip",
        "clips",
        "uncensored",
        "bedroom",
        "naughty",
        "show me",
        "see more",
        "how much",
        "price",
        "buy",
        "unlock",
        "ppv",
        "exclusive",
        "full version",
        "naked",
        "nude",
        "private",
        "custom",
        "send me",
        "tease",
        "climax",
    ]

    OBJECTION_KEYWORDS = [
        "not right now",
        "maybe later",
        "too expensive",
        "no money",
        "i'm broke",
        "im broke",
        "can't afford",
        "cant afford",
        "no thanks",
        "pass for now",
        "next time",
    ]

    TIP_PRAISE_KEYWORDS = [
        "goddess",
        "you're amazing",
        "you are amazing",
        "so gorgeous",
        "obsessed with you",
        "you're the best",
        "you are the best",
        "angel",
        "perfect",
        "madly in love",
        "spoil you",
        "treat you",
        "buy you coffee",
        "tip you",
        "send a tip",
        "send you a tip",
        "worth every penny",
        "just unlocked",
        "loved the video",
        "loved that video",
        "最高",
        "大好き",
        "女神",
        "天使",
        "可愛すぎ",
        "ご褒美",
        "チップ",
        "投げ銭",
        "貢ぎ",
    ]

    TIP_SPECIAL_REQUEST_KEYWORDS = [
        "spoil me",
        "call me good boy",
        "praise me",
        "give me attention",
        "all to myself",
        "do something special",
        "special request",
        "just for me",
        "say my name",
        "pose for me",
        "want to see more",
        "show me more",
        "もっと見たい",
        "特別に",
        "やってほしい",
        "リクエスト",
        "甘え",
        "褒めて",
        "名前呼んで",
    ]

    EXPLICIT_TIP_OR_PERSONAL_REQUEST_KEYWORDS = [
        "spoil you",
        "treat you",
        "buy you coffee",
        "tip you",
        "send a tip",
        "send you a tip",
        "spoil me",
        "call me good boy",
        "praise me",
        "give me attention",
        "all to myself",
        "do something special",
        "special request",
        "just for me",
        "say my name",
        "pose for me",
        "チップ",
        "投げ銭",
        "貢ぎ",
        "ご褒美",
        "特別に",
        "やってほしい",
        "リクエスト",
        "褒めて",
        "名前呼んで",
    ]

    AFFINITY_KEYWORDS = [
        "love",
        "cute",
        "pretty",
        "beautiful",
        "gorgeous",
        "sexy",
        "hot",
        "amazing",
        "sweet",
        "happy",
        "fun",
        "nice",
        "awesome",
        "great",
        "angel",
        "goddess",
        "obsessed",
        "perfect",
        "kiss",
        "heart",
        "blush",
        "excited",
        "haha",
        "hehe",
        "miss you",
        "adore",
        "好き",
        "大好き",
        "可愛い",
        "かわいい",
        "綺麗",
        "きれい",
        "最高",
        "嬉しい",
        "楽しい",
        "素敵",
        "天使",
        "女神",
        "セクシー",
        "エロ",
        "ドキドキ",
        "癒や",
        "癒さ",
        "応援",
    ]

    AFFINITY_EMOJIS = (
        "💕", "❤️", "🖤", "💜", "💖", "💗", "💓", "😍", "🥰", "😘", "🔥", "✨", "🥺", "💋", "👑", "😈",
    )

    PREFERENCE_TOPIC_KEYWORDS = [
        "lingerie",
        "lace",
        "cosplay",
        "outfit",
        "stockings",
        "thigh",
        "feet",
        "legs",
        "bedroom",
        "shower",
        "bath",
        "voice",
        "whisper",
        "asmr",
        "tease",
        "gothic",
        "elf",
        "maid",
        "bikini",
        "dress",
        "レース",
        "下着",
        "ランジェリー",
        "コスプレ",
        "衣装",
        "太もも",
        "脚",
        "寝室",
        "ベッド",
        "囁き",
        "甘え",
        "上目遣い",
        "ゴシック",
        "エルフ",
        "自撮り",
    ]

    OBJECTION_KEYWORDS_JA = [
        "高い",
        "お金ない",
        "金欠",
        "また今度",
        "今はいい",
        "やめとく",
        "見送る",
        "買えない",
    ]

    IMMEDIATE_BUY_KEYWORDS = (
        "how much",
        "price",
        "buy",
        "unlock",
        "send me",
        "right now",
        "いくら",
        "買いたい",
        "アンロック",
        "今すぐ見たい",
        "見せて",
    )

    NEXT_STAGE_MAP = {
        "1_Icebreak": "2_Explore",
        "2_Explore": "3_Lead_Pitch",
        "3_Lead_Pitch": "4_Lock_Close",
        "4_Lock_Close": "5_Cooldown",
        "5_Cooldown": "2_Explore",
        "6_Low_Priority": "2_Explore",
    }

    def __init__(self, llm=None):
        self.llm = llm or llm_client

    @classmethod
    def normalize_stage_name(cls, phase: Optional[str]) -> str:
        """ステージ表記ゆれ (2_Discovery -> 2_Explore, 4_Closing -> 4_Lock_Close) を標準 S.E.L.L. ステージ名へ正規化する"""
        p = (phase or "1_Icebreak").strip()
        if p == "2_Discovery":
            return "2_Explore"
        if p == "4_Closing":
            return "4_Lock_Close"
        if p not in cls.PHASES:
            return "1_Icebreak"
        return p

    @classmethod
    def get_stage_thresholds(
        cls,
        stage_thresholds: Optional[Dict[str, Any]] = None,
        settings: Optional[Dict[str, Any]] = None,
        db_path: Optional[str] = None,
    ) -> Dict[str, int]:
        """
        システム設定DB (system_settings) または明示引数から各ステージの突破閾値 (pt) を取得する。
        API経由で更新された閾値が次回判定から即座に反映されるよう、常にDB最新値を優先参照する。
        """
        db_thresh = get_system_stage_thresholds(db_path=db_path)
        if isinstance(stage_thresholds, dict) and stage_thresholds:
            return normalize_stage_thresholds(raw_thresholds=stage_thresholds, fallback=db_thresh)
        if db_thresh:
            return db_thresh
        if isinstance(settings, dict):
            return normalize_stage_thresholds(raw_thresholds=settings, fallback=DEFAULT_STAGE_THRESHOLDS)
        return dict(DEFAULT_STAGE_THRESHOLDS)

    @classmethod
    def get_stage_target_meta(
        cls,
        stage: str,
        thresholds: Dict[str, int],
    ) -> Tuple[str, int]:
        """指定ステージにおける (next_stage, stage_threshold) を返す"""
        norm = cls.normalize_stage_name(stage)
        next_st = cls.NEXT_STAGE_MAP.get(norm, "2_Explore")
        if norm == "1_Icebreak":
            return next_st, int(thresholds.get("1_Icebreak", 40))
        if norm == "2_Explore":
            return next_st, int(thresholds.get("2_Explore", 70))
        if norm == "3_Lead_Pitch":
            return next_st, int(thresholds.get("3_Lead_Pitch", 85))
        if norm == "4_Lock_Close":
            return next_st, 100
        return next_st, int(thresholds.get("1_Icebreak", 40))

    @classmethod
    def get_stage_floor_score(cls, stage: str, thresholds: Dict[str, int]) -> int:
        """各ステージの開始基準スコアを返す"""
        norm = cls.normalize_stage_name(stage)
        t_ice = int(thresholds.get("1_Icebreak", 40))
        t_exp = int(thresholds.get("2_Explore", 70))
        t_pitch = int(thresholds.get("3_Lead_Pitch", 85))
        if norm == "2_Explore":
            return t_ice
        if norm == "3_Lead_Pitch":
            return t_exp
        if norm == "4_Lock_Close":
            return t_pitch
        if norm == "5_Cooldown":
            return t_ice
        return 0

    @classmethod
    def verify_tip_request_in_reply(cls, reply_en: str) -> bool:
        """
        実際に生成された最終英語返信文の中に、チップ（tip / spoil me / treat me 等）を
        明確にねだる・提案する文言が含まれているかを厳密検証する。
        """
        if not reply_en or not reply_en.strip():
            return False
        return bool(cls.TIP_CONFIRM_REGEX.search(reply_en))

    @classmethod
    def extract_recent_ai_emojis(cls, msg_history: List[Dict[str, Any]], max_msgs: int = 2) -> List[str]:
        """直近のAIメッセージで使用された絵文字を抽出し、連続重複回避に利用する"""
        ai_texts = [
            str(m.get("content") or "")
            for m in msg_history
            if m.get("sender") in ("ai", "human_manual") and m.get("content")
        ][-max_msgs:]
        joined = " ".join(ai_texts)
        used = [em for em in cls.EMOJI_PALETTE if em in joined]
        for extra in ("👀", "🐾", "😍"):
            if extra in joined and extra not in used:
                used.append(extra)
        return used

    def detect_tip_opportunity(
        self,
        user_message: str,
        current_phase: str,
        next_phase: str,
        current_tier: str = "free_follower",
        total_spent_usd: float = 0.0,
        turn_count: int = 1,
    ) -> Dict[str, Any]:
        """
        チップ（投げ銭）誘導フック候補判定：
        - 新規未課金ファンの会話極初期（1〜2往復目）では発動しない。
        - 単なる 5_Cooldown 滞在だけでは発動せず、明確な強い称賛・甘え・特別リクエスト・購入直後絶賛がある時のみ候補とする。
        - ※最終確定は LLM 生成後の verify_tip_request_in_reply() を通過した場合のみ。
        """
        raw_msg = user_message or ""
        msg_lower = raw_msg.lower()
        spent = float(total_spent_usd or 0.0)
        is_paid_or_vip = spent > 0.0 or (current_tier or "free_follower").strip().lower() != "free_follower"

        if not is_paid_or_vip and int(turn_count or 1) < self.MIN_TURNS_BEFORE_PITCH_NEW_FAN:
            return {"triggered": False, "suggested_amount_usd": 0.0, "reason": None}

        has_objection = any(obj in msg_lower for obj in self.OBJECTION_KEYWORDS)
        if has_objection:
            return {"triggered": False, "suggested_amount_usd": 0.0, "reason": None}

        matched_praise = next(
            (kw for kw in self.TIP_PRAISE_KEYWORDS if kw in msg_lower or kw in raw_msg),
            None,
        )
        matched_request = next(
            (kw for kw in self.TIP_SPECIAL_REQUEST_KEYWORDS if kw in msg_lower or kw in raw_msg),
            None,
        )
        explicit_ppv_buy = any(
            kw in msg_lower
            for kw in ("how much", "price", "can i unlock", "want to unlock", "unlock it right now", "ppv", "uncensored video", "secret video")
        )

        suggested_tip = 10.0 if is_paid_or_vip and spent >= 20.0 else 5.0

        if (matched_praise or matched_request) and not explicit_ppv_buy:
            trigger_kw = matched_request or matched_praise
            if current_phase == "5_Cooldown" or next_phase == "5_Cooldown":
                reason_label = f"PPV購入後・感謝フェーズでの称賛検知 ('{trigger_kw}')"
                t_type = "cooldown_gratitude"
            else:
                reason_label = (
                    f"特別リクエスト・甘え検知 ('{trigger_kw}')"
                    if matched_request
                    else f"強い称賛・好意検知 ('{trigger_kw}')"
                )
                t_type = "request_or_praise"
            return {
                "triggered": True,
                "candidate": True,
                "suggested_amount_usd": suggested_tip,
                "reason": reason_label,
                "trigger_type": t_type,
            }

        return {"triggered": False, "suggested_amount_usd": 0.0, "reason": None}

    def check_emergency_stop(
        self,
        user_message: str,
        consecutive_unanswered: int = 0,
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        config.DANGER_KEYWORDS に該当するワード検知時、または連続未読スルー上限到達時に
        (True, escalation_reason, matched_keyword) を返す。
        """
        msg_lower = (user_message or "").lower()

        for kw in config.DANGER_KEYWORDS:
            kw_clean = kw.strip().lower()
            if not kw_clean:
                continue
            if kw_clean in ("bot", "ai"):
                if re.search(rf"\b{re.escape(kw_clean)}\b", msg_lower):
                    return True, f"危険ワード検知: '{kw_clean}'", kw_clean
            else:
                if kw.lower() in msg_lower or re.search(rf"\b{re.escape(kw_clean)}\b", msg_lower):
                    return True, f"危険ワード検知: '{kw_clean}'", kw_clean

        if int(consecutive_unanswered or 0) >= self.MAX_CONSECUTIVE_UNANSWERED:
            return (
                True,
                f"連続未読スルー上限到達 ({consecutive_unanswered}回)",
                "consecutive_unanswered",
            )

        return False, None, None

    def check_low_priority_demotion(
        self,
        current_tier: str,
        consecutive_ppv_ignored: int,
    ) -> bool:
        """current_tier == 'free_follower' かつ consecutive_ppv_ignored >= 2 の場合 True"""
        tier_norm = (current_tier or "free_follower").strip().lower()
        return (
            tier_norm == "free_follower"
            and int(consecutive_ppv_ignored or 0) >= self.LOW_PRIORITY_PPV_IGNORED_THRESHOLD
        )

    def generate_low_priority_reply(
        self,
        char_id: str,
        persona: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, str]:
        """省エネモード (6_Low_Priority) 用の超短文相槌（英語 + 日本語訳）を生成"""
        persona = persona or load_dm_persona(char_id)
        nickname = (persona.get("user_nickname_default") or "babe").split("/")[0].strip()
        en_reply = f"Hehe thanks {nickname}~ maybe later 🐾🖤"
        ja_reply = "えへへ、ありがとね〜またあとでね🐾🖤"
        return en_reply, ja_reply


    def evaluate_turn_stage_points(
        self,
        user_message: str,
        current_tier: str = "free_follower",
        total_spent_usd: float = 0.0,
    ) -> Tuple[int, str, Dict[str, Any]]:
        """
        1ターン分のファンの発言から獲得ポイント (earned_points) と加点理由 (score_reason_ja) を算出する。
        """
        raw_msg = (user_message or "").strip()
        msg_lower = raw_msg.lower()
        spent = float(total_spent_usd or 0.0)
        tier_norm = (current_tier or "free_follower").strip().lower()
        is_trusted_or_paid = spent > 0.0 or tier_norm != "free_follower"

        hit_objection = next(
            (
                kw
                for kw in (list(self.OBJECTION_KEYWORDS) + list(self.OBJECTION_KEYWORDS_JA))
                if kw in msg_lower or kw in raw_msg
            ),
            None,
        )
        if hit_objection:
            return (
                -10,
                f"価格躊躇・見送りシグナル検知 ('{hit_objection}' / -10pt)",
                {
                    "has_objection": True,
                    "wants_immediate_buy": False,
                    "high_intent_hits": 0,
                    "is_trusted_or_paid": is_trusted_or_paid,
                },
            )

        has_affinity_kw = any(kw in msg_lower or kw in raw_msg for kw in self.AFFINITY_KEYWORDS)
        has_affinity_emoji = any(em in raw_msg for em in self.AFFINITY_EMOJIS)
        wants_immediate_buy = any(kw in msg_lower or kw in raw_msg for kw in self.IMMEDIATE_BUY_KEYWORDS)
        high_intent_hits = sum(1 for kw in self.HIGH_INTENT_KEYWORDS if kw in msg_lower)
        has_pref_topic = any(kw in msg_lower or kw in raw_msg for kw in self.PREFERENCE_TOPIC_KEYWORDS)
        has_question = ("?" in raw_msg) or ("？" in raw_msg)

        if (
            len(raw_msg) <= 3
            and not has_affinity_kw
            and not has_affinity_emoji
            and not wants_immediate_buy
            and high_intent_hits == 0
        ):
            pts = 8 + (5 if is_trusted_or_paid else 0)
            reason = "短文相槌による会話継続 (+8pt)"
            if is_trusted_or_paid:
                reason += " / 有料VIPボーナス (+5pt)"
            return (
                pts,
                reason,
                {
                    "has_objection": False,
                    "wants_immediate_buy": False,
                    "high_intent_hits": 0,
                    "is_trusted_or_paid": is_trusted_or_paid,
                },
            )

        earned = 15
        reasons: List[str] = ["返信・会話継続 (+15pt)"]

        if has_affinity_kw or has_affinity_emoji:
            earned += 10
            reasons.append("好意的リアクション・称賛 (+10pt)")

        if has_question or has_pref_topic or len(raw_msg) >= 20:
            earned += 5
            reasons.append("積極的な会話・好み開示 (+5pt)")

        if wants_immediate_buy:
            earned += 15
            reasons.append("高購買意欲シグナル (+15pt)")
        elif high_intent_hits >= 1 or any(kw in raw_msg for kw in ("見たい", "秘密", "動画", "写真")):
            earned += 10
            reasons.append("高関心・視聴意欲シグナル (+10pt)")

        if is_trusted_or_paid:
            vip_bonus = 15 if spent >= self.WHALE_SPENT_THRESHOLD_USD else 10
            earned += vip_bonus
            reasons.append(f"有料会員・課金実績ボーナス (+{vip_bonus}pt)")

        return (
            earned,
            " / ".join(reasons),
            {
                "has_objection": False,
                "wants_immediate_buy": wants_immediate_buy,
                "high_intent_hits": high_intent_hits,
                "is_trusted_or_paid": is_trusted_or_paid,
            },
        )

    def _resolve_previous_stage_score(
        self,
        char_id: Optional[str],
        fan_id: Optional[str],
        norm_phase: str,
        thresholds: Dict[str, int],
        explicit_stage_score: Optional[int] = None,
        existing_profile: Union[str, Dict[str, Any], None] = None,
        recent_messages: Optional[List[Dict[str, Any]]] = None,
        current_tier: str = "free_follower",
        total_spent_usd: float = 0.0,
        db_path: Optional[str] = None,
    ) -> int:
        """
        直前ターン終了時点の stage_score を以下の優先順位で復元する：
        1. 明示引数 explicit_stage_score
        2. existing_profile 内のステージ推移データ (シミュレーター等)
        3. fans.decision_log_json の直近エントリ (本番DB / 永続化テスト)
        4. recent_messages の過去ファン発言からの再構築 (ステートレス呼び出し互換)
        5. 現在ステージの基準フロア値
        """
        floor_score = self.get_stage_floor_score(norm_phase, thresholds)

        if explicit_stage_score is not None:
            try:
                return max(0, min(100, int(round(float(explicit_stage_score)))))
            except Exception:
                pass

        prof_dict: Dict[str, Any] = {}
        if isinstance(existing_profile, dict):
            prof_dict = existing_profile
        elif isinstance(existing_profile, str) and existing_profile.strip():
            try:
                import json as _json

                parsed_p = _json.loads(existing_profile)
                if isinstance(parsed_p, dict):
                    prof_dict = parsed_p
            except Exception:
                prof_dict = {}

        if prof_dict:
            st_state = prof_dict.get("_sell_stage_state")
            if isinstance(st_state, dict) and st_state.get("stage_score") is not None:
                saved_stage = self.normalize_stage_name(st_state.get("current_stage"))
                if saved_stage == norm_phase:
                    try:
                        return max(0, min(100, int(round(float(st_state["stage_score"])))))
                    except Exception:
                        pass

        if char_id and fan_id:
            fan_rec = get_fan_record(char_id, fan_id, db_path=db_path)
            if fan_rec and fan_rec.get("decision_log_json"):
                try:
                    import json as _json

                    raw_logs = fan_rec.get("decision_log_json")
                    parsed_logs = _json.loads(raw_logs) if isinstance(raw_logs, str) else raw_logs
                    if isinstance(parsed_logs, dict):
                        parsed_logs = [parsed_logs]
                    if isinstance(parsed_logs, list) and parsed_logs:
                        last_log = parsed_logs[-1] if isinstance(parsed_logs[-1], dict) else {}
                        sr = last_log.get("strategy_reasoning") if isinstance(last_log.get("strategy_reasoning"), dict) else {}
                        rec_stage_raw = last_log.get("current_stage") or sr.get("current_stage")
                        rec_score_raw = (
                            last_log.get("stage_score")
                            if last_log.get("stage_score") is not None
                            else sr.get("stage_score")
                        )
                        if rec_score_raw is not None:
                            if not rec_stage_raw or self.normalize_stage_name(str(rec_stage_raw)) == norm_phase:
                                return max(0, min(100, int(round(float(rec_score_raw)))))
                except Exception:
                    pass

        if recent_messages and norm_phase in ("1_Icebreak", "2_Explore"):
            prior_fan_msgs = [
                str(m.get("content") or "")
                for m in recent_messages
                if isinstance(m, dict) and m.get("sender") == "fan" and m.get("content")
            ]
            if prior_fan_msgs:
                sim_score = 0
                t_ice = int(thresholds.get("1_Icebreak", 40))
                t_exp = int(thresholds.get("2_Explore", 70))
                for idx_m, p_msg in enumerate(prior_fan_msgs):
                    pts, _, _ = self.evaluate_turn_stage_points(
                        user_message=p_msg,
                        current_tier=current_tier,
                        total_spent_usd=total_spent_usd,
                    )
                    sim_score = max(0, min(100, sim_score + pts))
                    if idx_m == 0 and float(total_spent_usd or 0.0) <= 0.0 and (current_tier or "free_follower") == "free_follower" and t_ice >= 35:
                        sim_score = min(sim_score, max(0, t_ice - 5))
                    elif sim_score >= t_exp:
                        sim_score = min(sim_score, max(t_ice, t_exp - 5))
                if norm_phase == "2_Explore":
                    return max(floor_score, min(max(floor_score, t_exp - 5), sim_score))
                return min(max(0, t_ice - 5), sim_score)

        return floor_score

    def evaluate_stage_progression(
        self,
        current_phase: str,
        user_message: str,
        current_tier: str = "free_follower",
        total_spent_usd: float = 0.0,
        consecutive_ppv_ignored: int = 0,
        turn_count: int = 1,
        has_available_ppv: bool = True,
        char_id: Optional[str] = None,
        fan_id: Optional[str] = None,
        previous_score: Optional[int] = None,
        existing_profile: Union[str, Dict[str, Any], None] = None,
        recent_messages: Optional[List[Dict[str, Any]]] = None,
        stage_thresholds: Optional[Dict[str, Any]] = None,
        settings: Optional[Dict[str, Any]] = None,
        db_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        S.E.L.L. 突破スコア＆閾値ベースのステージ遷移判定：
        1_Icebreak (default 40pt) -> 2_Explore (default 70pt) -> 3_Lead_Pitch (default 85pt) -> 4_Lock_Close -> 5_Cooldown
        """
        thresholds = self.get_stage_thresholds(
            stage_thresholds=stage_thresholds,
            settings=settings,
            db_path=db_path,
        )
        t_ice = int(thresholds.get("1_Icebreak", 40))
        t_exp = int(thresholds.get("2_Explore", 70))
        t_pitch = int(thresholds.get("3_Lead_Pitch", 85))

        norm_phase = self.normalize_stage_name(current_phase)
        if norm_phase == "6_Low_Priority":
            norm_phase = "1_Icebreak"

        if self.check_low_priority_demotion(current_tier, consecutive_ppv_ignored):
            return {
                "previous_stage": norm_phase,
                "current_stage": "6_Low_Priority",
                "next_stage": "2_Explore",
                "previous_stage_score": 0,
                "stage_score": 0,
                "stage_threshold": t_ice,
                "remaining_points": t_ice,
                "earned_points": 0,
                "score_reason_ja": f"無料フォロワーかつPPV連続スルー({consecutive_ppv_ignored}回)により省エネモード適用",
                "stage_promoted": False,
                "stage_thresholds": thresholds,
            }

        prev_score = self._resolve_previous_stage_score(
            char_id=char_id,
            fan_id=fan_id,
            norm_phase=norm_phase,
            thresholds=thresholds,
            explicit_stage_score=previous_score,
            existing_profile=existing_profile,
            recent_messages=recent_messages,
            current_tier=current_tier,
            total_spent_usd=total_spent_usd,
            db_path=db_path,
        )

        earned_pts, reason_ja, sig = self.evaluate_turn_stage_points(
            user_message=user_message,
            current_tier=current_tier,
            total_spent_usd=total_spent_usd,
        )

        spent = float(total_spent_usd or 0.0)
        is_trusted_or_paid = bool(sig.get("is_trusted_or_paid"))
        wants_immediate_buy = bool(sig.get("wants_immediate_buy"))
        has_objection = bool(sig.get("has_objection"))
        turns = max(1, int(turn_count or 1))

        promoted = False
        new_stage = norm_phase
        new_score = max(0, min(100, prev_score + earned_pts))

        if has_objection and norm_phase in ("3_Lead_Pitch", "4_Lock_Close"):
            new_stage = "5_Cooldown"
            new_score = max(0, t_ice - 5)
            reason_ja = f"{reason_ja} → 押し売り回避のため 5_Cooldown へ移行"
        elif norm_phase == "4_Lock_Close":
            new_stage = "5_Cooldown"
            new_score = t_ice
            reason_ja = f"{reason_ja} → クロージング後の余韻フェーズ (5_Cooldown) へ移行"
        elif norm_phase == "5_Cooldown":
            if wants_immediate_buy:
                new_stage = "3_Lead_Pitch"
                promoted = True
                new_score = max(t_exp, min(max(t_exp, t_pitch - 5), t_exp + 10))
                reason_ja = f"{reason_ja} → 再購買リクエスト検知により 3_Lead_Pitch へ再突入"
            else:
                new_stage = "2_Explore"
                step_span = max(10, t_exp - t_ice)
                new_score = min(max(t_ice, t_exp - 5), t_ice + min(max(0, earned_pts), max(5, step_span // 2)))
                reason_ja = f"{reason_ja} → クールダウン完了・2_Explore でニーズ再探索を開始"
        elif norm_phase == "1_Icebreak":
            if is_trusted_or_paid and (wants_immediate_buy or spent >= self.WHALE_SPENT_THRESHOLD_USD):
                new_stage = "3_Lead_Pitch"
                promoted = True
                new_score = max(t_exp, min(max(t_exp, t_pitch - 5), max(new_score, t_exp + 5)))
                reason_ja = f"{reason_ja} → 有料VIP・高意欲につき 3_Lead_Pitch へ優先突破！"
            elif not is_trusted_or_paid and turns <= 1 and t_ice >= 35 and new_score >= t_ice:
                # 通常閾値(>=35pt)での新規未課金1往復目はラポール形成を優先し、閾値直前でホールド
                new_stage = "1_Icebreak"
                new_score = max(0, t_ice - 5)
                earned_pts = max(0, new_score - prev_score)
                reason_ja = f"{reason_ja}（初回ラポール優先のため {new_score}/{t_ice}pt で待機）"
            elif new_score >= t_ice:
                new_stage = "2_Explore"
                promoted = True
                if t_exp > t_ice:
                    new_score = max(t_ice, min(new_score, t_exp - 5))
                reason_ja = f"{reason_ja} → 閾値 {t_ice}pt 到達により 2_Explore へ突破！"
        elif norm_phase == "2_Explore":
            if new_score >= t_exp:
                new_stage = "3_Lead_Pitch"
                promoted = True
                if t_pitch > t_exp:
                    new_score = max(t_exp, min(new_score, t_pitch - 5))
                reason_ja = f"{reason_ja} → 閾値 {t_exp}pt 到達により 3_Lead_Pitch へ突破！"
        elif norm_phase == "3_Lead_Pitch":
            if new_score >= t_pitch:
                new_stage = "4_Lock_Close"
                promoted = True
                new_score = min(100, max(t_pitch, new_score))
                reason_ja = f"{reason_ja} → 閾値 {t_pitch}pt 到達により 4_Lock_Close へ突破！"

        target_next_stage, target_threshold = self.get_stage_target_meta(new_stage, thresholds)
        remaining = max(0, int(target_threshold) - int(new_score))

        return {
            "previous_stage": norm_phase,
            "current_stage": new_stage,
            "next_stage": target_next_stage,
            "previous_stage_score": int(prev_score),
            "stage_score": int(new_score),
            "stage_threshold": int(target_threshold),
            "remaining_points": int(remaining),
            "earned_points": int(earned_pts),
            "score_reason_ja": reason_ja,
            "stage_promoted": promoted,
            "stage_thresholds": thresholds,
        }

    def determine_next_phase(
        self,
        current_phase: str,
        user_message: str,
        current_tier: str = "free_follower",
        total_spent_usd: float = 0.0,
        consecutive_ppv_ignored: int = 0,
        turn_count: int = 1,
        has_available_ppv: bool = True,
        previous_score: Optional[int] = None,
        stage_thresholds: Optional[Dict[str, Any]] = None,
        db_path: Optional[str] = None,
    ) -> str:
        """
        S.E.L.L. フェーズ遷移ステートマシン（突破スコア＆閾値対応）：
        1_Icebreak -> 2_Explore -> 3_Lead_Pitch -> 4_Lock_Close -> 5_Cooldown
        """
        if previous_score is None and self.normalize_stage_name(current_phase) == "1_Icebreak" and int(turn_count or 1) >= 2:
            t_map = self.get_stage_thresholds(stage_thresholds=stage_thresholds, db_path=db_path)
            previous_score = max(0, int(t_map.get("1_Icebreak", 40)) - 10)
        elif previous_score is None and self.normalize_stage_name(current_phase) == "2_Explore" and int(turn_count or 1) >= 3:
            t_map = self.get_stage_thresholds(stage_thresholds=stage_thresholds, db_path=db_path)
            previous_score = max(int(t_map.get("1_Icebreak", 40)), int(t_map.get("2_Explore", 70)) - 15)

        prog = self.evaluate_stage_progression(
            current_phase=current_phase,
            user_message=user_message,
            current_tier=current_tier,
            total_spent_usd=total_spent_usd,
            consecutive_ppv_ignored=consecutive_ppv_ignored,
            turn_count=turn_count,
            has_available_ppv=has_available_ppv,
            previous_score=previous_score,
            stage_thresholds=stage_thresholds,
            db_path=db_path,
        )
        return str(prog["current_stage"])

    def determine_pricing_mode(
        self,
        total_spent_usd: float,
        current_tier: str,
        plans: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, str]:
        """
        動的プライシング判定：
        - total_spent_usd == 0 -> ('tripwire', 'tripwire_price_usd')
        - total_spent_usd >= 50.0 または最上位プラン -> ('whale', 'whale_price_usd')
        - それ以外 -> ('base', 'base_price_usd')
        """
        spent = float(total_spent_usd or 0.0)
        tier_norm = (current_tier or "free_follower").strip()

        top_tier_id = "gold_goddess"
        if isinstance(plans, dict):
            tiers_list = plans.get("tiers") or []
            if tiers_list:
                sorted_tiers = sorted(
                    tiers_list,
                    key=lambda t: float(t.get("price_usd") or 0.0),
                    reverse=True,
                )
                top_tier_id = str(sorted_tiers[0].get("tier_id") or "gold_goddess")

        if spent >= self.WHALE_SPENT_THRESHOLD_USD or tier_norm == top_tier_id:
            return "whale", "whale_price_usd"
        if spent <= 0.0:
            return "tripwire", "tripwire_price_usd"
        return "base", "base_price_usd"

    def select_ppv_and_price(
        self,
        char_id: str,
        fan_id: Optional[str] = None,
        total_spent_usd: float = 0.0,
        current_tier: str = "free_follower",
        purchased_ppv_ids: Optional[List[str]] = None,
        plans: Optional[Dict[str, Any]] = None,
        settings: Optional[Dict[str, Any]] = None,
        vault_items: Optional[List[Dict[str, Any]]] = None,
        psych_profile: Optional[Dict[str, Any]] = None,
        user_message: str = "",
        db_path: Optional[str] = None,
    ) -> Tuple[Optional[Dict[str, Any]], str]:
        """
        階段式PPV選定 ＆ 好み属性タグ照合 ＆ 動的プライシング：
        1. DBの ppv_transactions (is_unlocked == 1) および引数の purchased_ppv_ids を除外。
        2. 顧客の過去購入段階・課金額に応じて未購入の Step 1 -> Step 2 -> Step 3 から候補を絞り、
           さらに顧客カルテ (preferences / hooks) や発言と PPV の日本語 tags / description_memo の一致度が高い素材を最優先選定。
        3. 動的プライシング (tripwire / base / whale) で提示価格を決定して返す。
        """
        plans = plans or load_plans(char_id)
        settings = settings or load_settings(char_id)
        all_vault = vault_items if vault_items is not None else load_vault_items(char_id)

        pricing_mode, price_key = self.determine_pricing_mode(
            total_spent_usd=total_spent_usd,
            current_tier=current_tier,
            plans=plans,
        )

        excluded_ids: Set[str] = set()
        if fan_id:
            for pid in get_unlocked_ppv_ids(char_id, fan_id, db_path=db_path):
                excluded_ids.add(str(pid))
        if purchased_ppv_ids:
            for pid in purchased_ppv_ids:
                if pid:
                    excluded_ids.add(str(pid))

        active_items = [
            it
            for it in all_vault
            if bool(it.get("is_active", True)) and is_vault_item_sendable_to_customer(char_id, it)
        ]
        unpurchased = [
            it for it in active_items if str(it.get("ppv_id") or "") not in excluded_ids
        ]

        if not unpurchased:
            return None, pricing_mode

        purchased_steps = [
            int(it.get("ladder_step") or 1)
            for it in active_items
            if str(it.get("ppv_id") or "") in excluded_ids
        ]
        max_purchased_step = max(purchased_steps) if purchased_steps else 0

        spent = float(total_spent_usd or 0.0)
        if max_purchased_step >= 2:
            preferred_step_order = [3, 2, 1]
        elif max_purchased_step == 1 or spent >= self.WHALE_SPENT_THRESHOLD_USD:
            preferred_step_order = [2, 3, 1]
        else:
            preferred_step_order = [1, 2, 3]

        prof_dict = psych_profile if isinstance(psych_profile, dict) else {}
        fan_pref_terms: List[str] = []
        for k in ("preferences", "hooks", "fetishes", "hot_topics"):
            v = prof_dict.get(k)
            if isinstance(v, list):
                fan_pref_terms.extend(str(x).strip().lower() for x in v if str(x).strip())
            elif isinstance(v, str) and v.strip():
                fan_pref_terms.extend(x.strip().lower() for x in v.split(",") if x.strip())
        msg_lower = (user_message or "").lower()

        def _score_candidate(item_cand: Dict[str, Any]) -> Tuple[int, List[str]]:
            item_tags = [str(t).strip() for t in (item_cand.get("tags") or []) if str(t).strip()]
            memo_txt = str(item_cand.get("description_memo") or item_cand.get("pitch_note") or "").lower()
            hint_txt = str(item_cand.get("pitch_hint") or "").lower()
            score = 0
            hits: List[str] = []
            for tag in item_tags:
                tl = tag.lower()
                if tl and (tl in msg_lower or any(tl in fp or fp in tl for fp in fan_pref_terms)):
                    score += 3
                    hits.append(tag)
            for fp in fan_pref_terms:
                if len(fp) >= 2 and (fp in memo_txt or fp in hint_txt):
                    score += 1
            return score, hits

        chosen_item: Optional[Dict[str, Any]] = None
        matched_tags: List[str] = []
        for target_step in preferred_step_order:
            candidates = [
                it for it in unpurchased if int(it.get("ladder_step") or 1) == target_step
            ]
            if candidates:
                scored = [(idx, *_score_candidate(c), c) for idx, c in enumerate(candidates)]
                scored.sort(key=lambda x: (x[1], -x[0]), reverse=True)
                _, _, matched_tags, chosen_item = scored[0]
                break

        if not chosen_item:
            unpurchased.sort(key=lambda x: int(x.get("ladder_step") or 1))
            chosen_item = unpurchased[0]

        step_num = int(chosen_item.get("ladder_step") or 1)
        step_str = str(step_num)
        default_step_prices = (settings.get("default_prices") or {}).get(step_str) or {
            "tripwire_price_usd": 5.99,
            "base_price_usd": 9.99,
            "whale_price_usd": 19.99,
        }

        raw_price = chosen_item.get(price_key)
        if raw_price is None or float(raw_price) <= 0:
            raw_price = default_step_prices.get(price_key, chosen_item.get("base_price_usd", 9.99))

        offered_price = round(float(raw_price), 2)
        fname = str(chosen_item.get("filename") or "").replace("\\", "/").split("/")[-1].strip()
        step_label = "Step 1: 導入" if step_num == 1 else ("Step 2: 本命" if step_num == 2 else "Step 3: 超秘蔵")

        vault_dir = get_char_paths(char_id)["vault_dir"]
        full_media_path = os.path.join(vault_dir, fname) if fname else ""
        blur_abs = (
            resolve_existing_blur_preview_path(full_media_path)
            if full_media_path and os.path.isfile(full_media_path)
            else None
        )
        raw_blur_rel = str(chosen_item.get("blur_preview_filename") or "").replace("\\", "/").strip().lstrip("/")
        if not blur_abs and raw_blur_rel:
            cand_blur = os.path.join(vault_dir, *raw_blur_rel.split("/"))
            if os.path.isfile(cand_blur) and os.path.getsize(cand_blur) > 0:
                blur_abs = cand_blur
        blur_rel = get_blur_preview_relpath(fname) if (blur_abs and fname) else raw_blur_rel

        recommended_ppv = {
            **chosen_item,
            "char_id": char_id,
            "filename": fname,
            "media_url": f"/api/vault/media/{char_id}/{fname}" if fname else "",
            "blur_preview_filename": blur_rel,
            "blur_preview_path": blur_abs,
            "blur_preview_exists": bool(blur_abs),
            "blur_preview_url": f"/api/vault/media/{char_id}/{blur_rel}" if (blur_abs and blur_rel) else "",
            "step_label": step_label,
            "offered_price_usd": offered_price,
            "pricing_mode": pricing_mode,
            "matched_tags": matched_tags,
            "is_unlocked": 0,
            "unlock_status_ja": "未購入",
        }
        return recommended_ppv, pricing_mode

    NAGGING_COOLDOWN_REGEX = re.compile(
        r"(\bdid you (unlock|open|watch|buy|check)\b|\bhurry\s+up\b|\baren't you going to (unlock|buy|open)\b|\bwaiting for you to (unlock|buy)\b|\bstill haven't (unlocked|opened)\b)",
        re.IGNORECASE,
    )

    @classmethod
    def sanitize_cooldown_reply(cls, reply_en: str, nickname: str = "babe", turn_idx: int = 0) -> str:
        """5_Cooldown において万が一LLMが催促・急かし表現を出力した場合に余裕のある雑談文へ浄化する"""
        if not reply_en:
            return reply_en
        if cls.NAGGING_COOLDOWN_REGEX.search(reply_en):
            em1 = cls.EMOJI_PALETTE[turn_idx % len(cls.EMOJI_PALETTE)]
            em2 = cls.EMOJI_PALETTE[(turn_idx + 5) % len(cls.EMOJI_PALETTE)]
            return (
                f"I'm just relaxing and enjoying our private vibe tonight, {nickname} {em1} "
                f"Tell me what kind of mood you're in right now {em2}"
            )
        return reply_en

    def build_sales_system_prompt(
        self,
        char_id: str,
        sales_phase: str,
        current_tier: str,
        total_spent_usd: float,
        pricing_mode: str,
        recommended_ppv: Optional[Dict[str, Any]],
        psych_profile: Union[str, Dict[str, Any], None],
        persona: Optional[Dict[str, Any]] = None,
        plans: Optional[Dict[str, Any]] = None,
        ab_group: str = "A",
        playbook: Optional[Dict[str, Any]] = None,
        tip_hook: Optional[Dict[str, Any]] = None,
        recent_used_emojis: Optional[List[str]] = None,
        db_path: Optional[str] = None,
    ) -> str:
        """
        dm_persona.json・plans.json・顧客カルテ・A/Bプレイブックルール・
        S.E.L.L.フェーズ・提案PPV情報・チップ誘導フックを統合したシステムプロンプトを構築する。
        """
        persona = persona or load_dm_persona(char_id)
        plans = plans or load_plans(char_id)
        active_pb = playbook or get_active_playbook_for_group(char_id, ab_group=ab_group, db_path=db_path)

        display_name = persona.get("display_name") or char_id
        tone_and_style = persona.get("tone_and_style") or "Alluring, playful, affectionate."
        first_person = persona.get("first_person") or "I"
        nickname = (persona.get("user_nickname_default") or "babe").split("/")[0].strip()
        forbidden = ", ".join(persona.get("forbidden_topics") or ["minors", "refunds", "AI/bot"])
        sales_attitude = persona.get("sales_attitude") or "Frame PPVs as exclusive secrets."

        tiers_list = (plans or {}).get("tiers") or []
        tier_obj = next((t for t in tiers_list if t.get("tier_id") == current_tier), None)
        tier_name = tier_obj.get("name") if tier_obj else current_tier
        access_summary = tier_obj.get("access_summary") if tier_obj else ""
        upsell_pitch = tier_obj.get("upsell_pitch") if tier_obj else ""

        profile_block = format_psych_profile_for_prompt(psych_profile)
        pb_rules = str(active_pb.get("rules_text") or "").strip()
        pb_ver = active_pb.get("version_id", 1)
        pb_grp = active_pb.get("ab_group") or ab_group
        playbook_block = (
            f"[Self-Evolved Sales Playbook Rules (AB Group {pb_grp} / v{pb_ver})]\n{pb_rules}"
            if pb_rules
            else ""
        )

        tip_active = bool(tip_hook and tip_hook.get("triggered"))
        tip_amount = float((tip_hook or {}).get("suggested_amount_usd") or 5.0)
        tip_block = ""
        if tip_active:
            tip_block = (
                f"\n\n[Tip / Appreciation Hook Directive (ACTIVE: Suggest ${int(tip_amount)} Tip)]\n"
                f"- Trigger Context: {(tip_hook or {}).get('reason')}\n"
                f"- Stay 100% in your character voice ({tone_and_style}) without sounding robotic.\n"
                f"- React warmly/flirtatiously to their sweet message, and explicitly invite them to spoil you with a ${int(tip_amount)} tip (use 'tip' or 'spoil me' naturally with '${int(tip_amount)}') for extra special personal attention."
            )

        discovery_guide = (
            "PHASE 2 (DISCOVERY / EXPLORE): Playfully chat with the fan and naturally uncover their favorite vibes, "
            "turn-ons, fetishes, or ideal scenarios. Build genuine chemistry and curiosity—do NOT quote prices or push a PPV purchase yet."
        )
        close_guide = (
            "PHASE 4 (CLOSING / LOCK & CLOSE): Confidently present and close the deal on the recommended PPV at the exact offered price. "
            "Highlight what makes this secret clip/photo irresistible for their specific taste, without sounding desperate."
        )
        phase_instructions = {
            "1_Icebreak": (
                "PHASE 1 (ICEBREAK - STRICT RAPPORT ONLY): Warmly welcome/respond to the fan, compliment them or chat about everyday/fun vibes, "
                "and ask an easygoing flirty question. STRICT RULE: Do NOT mention PPVs, prices, unlocking, or selling anything in Phase 1."
            ),
            "2_Discovery": discovery_guide,
            "2_Explore": discovery_guide,
            "3_Lead_Pitch": (
                "PHASE 3 (LEAD PITCH): Chemistry is warm—build excitement and naturally introduce the recommended PPV item "
                "(or subscription tier upgrade) using its pitch_hint and exact offered price as an intimate secret shared just with them."
            ),
            "4_Lock_Close": close_guide,
            "4_Closing": close_guide,
            "5_Cooldown": (
                "PHASE 5 (COOLDOWN - STRICT NO-NAGGING): Savor the lingering mood and relax all sales pressure. "
                "STRICT PROHIBITION: NEVER rush, nag, or ask 'Did you open it yet?', 'Hurry and buy/unlock it', or pressure them about any previous PPV. "
                "Enjoy relaxed, affectionate, playful conversation."
            ),
        }
        phase_guide = phase_instructions.get(sales_phase, discovery_guide)

        palette_str = " ".join(self.EMOJI_PALETTE)
        avoid_str = (
            f"Avoid repeating these emojis used in your immediately preceding message(s): {' '.join(recent_used_emojis)}"
            if recent_used_emojis
            else "Vary your emoji choices every turn so you never look repetitive."
        )
        emoji_block = (
            f"[Expressive Emoji & Conversational Rhythm Guide]\n"
            f"- Available Character Emoji Palette: {palette_str}\n"
            f"- Pick 1 to 3 fitting emojis from the palette above matching the mood (regal, teasing, sweet, shy, or fiery).\n"
            f"- {avoid_str}\n"
            f"- NEVER spam the same 1-2 default emojis in every single message, and NEVER nag or rush the fan after pitching."
        )

        if recommended_ppv and sales_phase in ("3_Lead_Pitch", "4_Lock_Close", "4_Closing"):
            desc_memo_str = str(
                recommended_ppv.get("description_memo") or recommended_ppv.get("pitch_note") or ""
            ).strip()
            memo_line = f"\n- Visual & Sales Hook Memo (JP): {desc_memo_str}" if desc_memo_str else ""
            ppv_block = (
                f"[Recommended PPV Offer]\n"
                f"- PPV ID: {recommended_ppv.get('ppv_id')} ({recommended_ppv.get('filename') or ''})\n"
                f"- Ladder Step: Step {recommended_ppv.get('ladder_step')}\n"
                f"- Media Type: {recommended_ppv.get('media_type')}\n"
                f"- Tags: {', '.join(recommended_ppv.get('tags') or [])}\n"
                f"- Offered Price (Dynamic {pricing_mode.upper()}): ${float(recommended_ppv.get('offered_price_usd') or 9.99):.2f}\n"
                f"- Pitch Hint: {recommended_ppv.get('pitch_hint') or 'Exclusive secret clip just for you'}"
                f"{memo_line}"
            )
        else:
            ppv_block = "[Recommended PPV Offer]\n- None for this turn (focus 100% on genuine rapport, discovery, or relaxed afterglow without pitching prices)."

        return (
            f"You are {display_name}, a real human creator chatting via private DM on Fansly.\n"
            f"[Character Persona]\n"
            f"- First Person Pronoun: {first_person}\n"
            f"- Default Fan Nickname: {nickname}\n"
            f"- Tone & Style: {tone_and_style}\n"
            f"- Sales Attitude: {sales_attitude}\n"
            f"- Strictly Forbidden Topics: {forbidden}\n\n"
            f"{emoji_block}\n\n"
            f"[Fan Subscription & Spending Status]\n"
            f"- Current Tier: {tier_name} ({current_tier}) — {access_summary}\n"
            f"- Total Spent: ${float(total_spent_usd or 0.0):.2f} (Dynamic Pricing Mode: {pricing_mode.upper()})\n"
            f"- Tier Upsell Pitch: {upsell_pitch or 'Already at top tier'}\n\n"
            f"{profile_block}\n\n"
            f"{playbook_block}\n\n"
            f"{ppv_block}{tip_block}\n\n"
            f"[Current S.E.L.L. State Machine Directive: {sales_phase}]\n"
            f"{phase_guide}\n\n"
            "Write ONLY your English DM reply (2 to 3 short, natural, seductive sentences max). "
            "FORMAT RULE: Insert natural line breaks ('\\n') between sentences or distinct thoughts (2 to 3 short lines) so the DM reads like an authentic human chat instead of a single run-on paragraph. Never output literal escaped '\\\\n'. "
            "PRIORITY 1: If Tip Hook Directive is ACTIVE above, you MUST playfully include the word 'tip' (e.g., 'a sweet $10 tip to spoil me') with the exact dollar amount while thanking them warmly. "
            "PRIORITY 2: Otherwise if in 3_Lead_Pitch or 4_Lock_Close with a Recommended PPV, weave in the Pitch Hint and exact dollar price. "
            "PRIORITY 3: When Tip Hook Directive is NOT active in 1_Icebreak, 2_Discovery, or 5_Cooldown, NEVER mention PPV prices and NEVER nag or ask if they unlocked/opened any previous PPV."
        )

    def _build_fallback_sales_reply(
        self,
        char_id: str,
        sales_phase: str,
        pricing_mode: str,
        recommended_ppv: Optional[Dict[str, Any]],
        persona: Dict[str, Any],
        plans: Dict[str, Any],
        current_tier: str,
        tip_hook: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Ollamaオフライン時・フォールバック用のフェーズ連動英語メッセージ"""
        nickname = (persona.get("user_nickname_default") or "babe").split("/")[0].strip()
        tiers_list = (plans or {}).get("tiers") or []
        tier_obj = next((t for t in tiers_list if t.get("tier_id") == current_tier), None)
        upsell_pitch = tier_obj.get("upsell_pitch") if tier_obj else ""

        if tip_hook and tip_hook.get("triggered"):
            tip_amt = int(float(tip_hook.get("suggested_amount_usd") or 5.0))
            return (
                f"Aww {nickname}, you're making my heart flutter so much right now~ 🖤✨\n"
                f"If you spoil me with a sweet ${tip_amt} tip right here, I'll give you extra special personal attention tonight 😘💕"
            )

        if sales_phase in ("3_Lead_Pitch", "4_Lock_Close") and recommended_ppv:
            price_val = float(recommended_ppv.get("offered_price_usd") or 9.99)
            step_num = int(recommended_ppv.get("ladder_step") or 1)
            pitch_hint = recommended_ppv.get("pitch_hint") or "I recorded a secret bedroom clip just for you."
            if sales_phase == "4_Lock_Close":
                return (
                    f"Mmm {nickname}, I can't hold back anymore~ 🖤\n"
                    f"{pitch_hint}\n"
                    f"I attached Step {step_num} right here for a special ${price_val:.2f} ({pricing_mode.upper()}) — unlock it before I get shy and delete it 😈💋"
                )
            return (
                f"Mmm I love how you talk to me, {nickname}~ 🖤\n"
                f"{pitch_hint}\n"
                f"I unlocked it at a special ${price_val:.2f} ({pricing_mode.upper()}) just for you right now 😈💋"
            )

        if sales_phase == "5_Cooldown":
            return (
                f"I love just relaxing and enjoying this sweet lingering vibe with you, {nickname}~ 🌙✨\n"
                "Tell me what you're up to for the rest of tonight? 💜"
            )

        if sales_phase == "1_Icebreak":
            return (
                f"Hey {nickname}~ so happy you messaged me! 💜✨\n"
                "How's your day going so far? Tell me something fun about yourself 😉🌙"
            )

        if upsell_pitch and sales_phase == "3_Lead_Pitch":
            return f"You're making me blush {nickname}~ 🥀💋\n{upsell_pitch}"

        return (
            f"Mmm {nickname}, you really know how to catch my attention~ 👑💫\n"
            "What kind of vibe or secret scenario gets your heart racing the most? 🤫💕"
        )

    def process_fan_message(
        self,
        char_id: str,
        user_message: str,
        fan_id: Optional[str] = None,
        current_tier: str = "free_follower",
        total_spent_usd: float = 0.0,
        sales_phase: str = "1_Icebreak",
        consecutive_ppv_ignored: int = 0,
        consecutive_unanswered: int = 0,
        purchased_ppv_ids: Optional[List[str]] = None,
        recent_messages: Optional[List[Dict[str, Any]]] = None,
        existing_profile_json: Union[str, Dict[str, Any], None] = None,
        ab_group: Optional[str] = None,
        persist_db: bool = True,
        record_tip_in_db: bool = True,
        stage_score: Optional[int] = None,
        stage_thresholds: Optional[Dict[str, Any]] = None,
        db_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        ファンからの受信メッセージを処理し、
        1. 危険ワード・連続スルー緊急停止判定
        2. 顧客カルテ (psych_profile_json) のリアルタイム抽出・更新
        3. タダ乗り客省エネ降格判定 (6_Low_Priority)
        4. 階段式PPV選定 ＆ 動的プライシング
        5. S.E.L.L. 突破スコア加算 ＆ ステージ自動昇格判定
        6. 英語営業応答 + 日本語訳生成
        を一貫実行して構造化ディクショナリを返す。
        """
        persona = load_dm_persona(char_id)
        plans = load_plans(char_id)
        settings = load_settings(char_id)
        vault_items = load_vault_items(char_id)
        active_thresholds = self.get_stage_thresholds(
            stage_thresholds=stage_thresholds,
            settings=settings,
            db_path=db_path,
        )
        norm_input_stage = self.normalize_stage_name(sales_phase)

        msg_history: List[Dict[str, str]] = []
        if isinstance(recent_messages, list):
            for m in recent_messages:
                if isinstance(m, dict) and m.get("content"):
                    msg_history.append(
                        {
                            "sender": str(m.get("sender") or "fan"),
                            "content": str(m.get("content")),
                        }
                    )
        if user_message:
            msg_history.append({"sender": "fan", "content": user_message})

        # Step 1: 危険ワード・連続未読スルーによる緊急停止判定（LLMを呼ばず即座にブロック）
        is_escalated, escalation_reason, matched_trigger = self.check_emergency_stop(
            user_message=user_message,
            consecutive_unanswered=consecutive_unanswered,
        )
        if is_escalated:
            ai_en_lock = "[EMERGENCY LOCK] Safety trigger detected. AI auto-reply has been paused for human review."
            ai_ja_lock = f"⚠️ [緊急停止ガードレール作動] {escalation_reason} のためAI自動返信をロックし、要介入アラートを発報しました。"
            esc_next_st, esc_thresh = self.get_stage_target_meta(norm_input_stage, active_thresholds)
            esc_prev_score = self._resolve_previous_stage_score(
                char_id=char_id,
                fan_id=fan_id,
                norm_phase=norm_input_stage,
                thresholds=active_thresholds,
                explicit_stage_score=stage_score,
                existing_profile=existing_profile_json,
                recent_messages=recent_messages,
                current_tier=current_tier,
                total_spent_usd=total_spent_usd,
                db_path=db_path,
            )
            esc_rem = max(0, int(esc_thresh) - int(esc_prev_score))

            alert_profile = {
                "preferences": ["要人間介入（自動返信ロック中）"],
                "psych_type": "緊急停止・要人間対応（警戒/トラブル）",
                "budget_tier": f"累計課金 ${float(total_spent_usd or 0.0):.2f}",
                "hooks": ["手動サポート対応", "安心感の提供"],
                "fetishes": ["要人間介入（自動返信ロック中）"],
                "personality_type": "緊急停止・要人間対応（警戒/トラブル）",
                "budget_level": f"累計課金 ${float(total_spent_usd or 0.0):.2f}",
                "hot_topics": ["手動サポート対応", "安心感の提供"],
                "risk_flag": matched_trigger,
                "notes": f"緊急停止アラート: {escalation_reason}",
                "_sell_stage_state": {
                    "current_stage": norm_input_stage,
                    "next_stage": esc_next_st,
                    "stage_score": esc_prev_score,
                    "stage_threshold": esc_thresh,
                },
            }

            escalation_decision_log = {
                "recorded_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                "mode": "emergency_stop",
                "fan_message_en": user_message,
                "current_stage": norm_input_stage,
                "next_stage": esc_next_st,
                "stage_score": esc_prev_score,
                "stage_threshold": esc_thresh,
                "remaining_points": esc_rem,
                "earned_points": 0,
                "score_reason_ja": f"緊急停止ロック作動（{escalation_reason}）のため加点凍結",
                "profile_reasoning": {
                    "trigger_fan_message": (user_message or "")[:220],
                    "preferences_reason": f"安全ロック発火（トリガー: {matched_trigger or escalation_reason}）により手動介入待ちへ移行",
                    "psych_type_reason": f"危険ワードまたは連続スルー（{escalation_reason}）を検知",
                    "budget_tier_reason": f"累計課金 ${float(total_spent_usd or 0.0):.2f} を維持",
                    "hooks_reason": "AI営業を停止し、人間オペレーターによる丁寧なフォローを推奨",
                },
                "strategy_reasoning": {
                    "phase_transition": f"{norm_input_stage} → {norm_input_stage} (🚨 LOCKED)",
                    "phase_reason_ja": f"ガードレール検知（{escalation_reason}）によりAI自動返信を即時ロック",
                    "current_stage": norm_input_stage,
                    "next_stage": esc_next_st,
                    "stage_score": esc_prev_score,
                    "stage_threshold": esc_thresh,
                    "remaining_points": esc_rem,
                    "earned_points": 0,
                    "score_reason_ja": f"緊急停止ロック作動（{escalation_reason}）のため加点凍結",
                    "ladder_step_reason_ja": "安全保護のためPPV提案・自動送信を完全停止",
                    "ab_rule_applied": {"ab_group": ab_group or "A", "prompt_version": "guardrail-lock"},
                    "monetization_judgment_ja": "マネタイズ提案見送り（人間介入待ち）",
                    "reply_intent_summary_ja": ai_ja_lock,
                },
            }

            if persist_db and fan_id:
                upsert_fan_sales_state(
                    char_id=char_id,
                    fan_id=fan_id,
                    current_tier=current_tier,
                    total_spent_usd=total_spent_usd,
                    sales_phase=norm_input_stage,
                    consecutive_unanswered=consecutive_unanswered,
                    consecutive_ppv_ignored=consecutive_ppv_ignored,
                    is_escalated=1,
                    escalation_reason=escalation_reason,
                    psych_profile_dict=alert_profile,
                    db_path=db_path,
                )
                append_fan_decision_log(char_id, fan_id, escalation_decision_log, db_path=db_path)

            return {
                "ok": True,
                "char_id": char_id,
                "fan_id": fan_id,
                "is_escalated": 1,
                "escalation_reason": escalation_reason,
                "matched_trigger": matched_trigger,
                "is_low_priority": 0,
                "sales_phase": norm_input_stage,
                "current_stage": norm_input_stage,
                "next_stage": esc_next_st,
                "stage_score": esc_prev_score,
                "stage_threshold": esc_thresh,
                "remaining_points": esc_rem,
                "earned_points": 0,
                "score_reason_ja": escalation_decision_log["score_reason_ja"],
                "pricing_tier": "locked",
                "recommended_ppv": None,
                "ai_en": ai_en_lock,
                "ai_ja": ai_ja_lock,
                "ai_reply": ai_en_lock,
                "ai_reply_ja": ai_ja_lock,
                "psych_profile": alert_profile,
                "decision_log": escalation_decision_log,
            }

        # Step 2: タダ乗り客省エネ降格 (6_Low_Priority) 判定（計算リソース節約のためフル推論をスキップ）
        if self.check_low_priority_demotion(current_tier, consecutive_ppv_ignored):
            next_phase = "6_Low_Priority"
            ai_en_low, ai_ja_low = self.generate_low_priority_reply(char_id, persona=persona)
            low_thresh = int(active_thresholds.get("1_Icebreak", 40))
            low_reason_ja = f"無料フォロワーかつPPV連続スルー {consecutive_ppv_ignored}回のため省エネモード維持 (0pt)"
            low_profile = {
                "preferences": ["無料チラ見せ・おねだり"],
                "psych_type": "タダ乗り・フリーミアム客（省エネ対象）",
                "budget_tier": "ゼロ課金（PPV 2回連続スルー / 省エネモード稼働中）",
                "hooks": ["短文ライト返信", "無料チラ見せ話題"],
                "fetishes": ["無料チラ見せ・おねだり"],
                "personality_type": "タダ乗り・フリーミアム客（省エネ対象）",
                "budget_level": "ゼロ課金（PPV 2回連続スルー / 省エネモード稼働中）",
                "hot_topics": ["短文ライト返信", "無料チラ見せ話題"],
                "notes": "無料会員＋PPV連続スルーのため省エネ降格中（フル営業プロンプト停止）",
                "_sell_stage_state": {
                    "current_stage": next_phase,
                    "next_stage": "2_Explore",
                    "stage_score": 0,
                    "stage_threshold": low_thresh,
                },
            }

            low_decision_log = {
                "recorded_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                "mode": "low_priority",
                "fan_message_en": user_message,
                "current_stage": next_phase,
                "next_stage": "2_Explore",
                "stage_score": 0,
                "stage_threshold": low_thresh,
                "remaining_points": low_thresh,
                "earned_points": 0,
                "score_reason_ja": low_reason_ja,
                "profile_reasoning": {
                    "trigger_fan_message": (user_message or "")[:220],
                    "preferences_reason": "無料会員かつPPV連続未購入のため省エネプロファイルを適用",
                    "psych_type_reason": f"無料プラン（{current_tier}）でPPVを{consecutive_ppv_ignored}回連続スルーしたためタダ乗り傾向と判定",
                    "budget_tier_reason": "累計課金 $0.00 ＋ 提案連続スルーのため低優先ティアへ移行",
                    "hooks_reason": "GPUリソース節約のため超短文リアクションのみ維持",
                },
                "strategy_reasoning": {
                    "phase_transition": f"{norm_input_stage} → 6_Low_Priority",
                    "phase_reason_ja": f"無料フォロワーかつPPV連続スルー {consecutive_ppv_ignored}回（閾値2回以上）のため省エネ降格",
                    "current_stage": next_phase,
                    "next_stage": "2_Explore",
                    "stage_score": 0,
                    "stage_threshold": low_thresh,
                    "remaining_points": low_thresh,
                    "earned_points": 0,
                    "score_reason_ja": low_reason_ja,
                    "ladder_step_reason_ja": "省エネモード中のためPPV添付提案をスキップ",
                    "ab_rule_applied": {"ab_group": ab_group or "A", "prompt_version": "low-priority-short"},
                    "monetization_judgment_ja": "成約見込み低のため売り込みを控え、超短文相槌のみ送信",
                    "reply_intent_summary_ja": f"省エネ短文返信: {ai_ja_low}",
                },
            }

            if persist_db and fan_id:
                upsert_fan_sales_state(
                    char_id=char_id,
                    fan_id=fan_id,
                    current_tier=current_tier,
                    total_spent_usd=total_spent_usd,
                    sales_phase=next_phase,
                    consecutive_unanswered=0,
                    consecutive_ppv_ignored=consecutive_ppv_ignored,
                    is_low_priority=1,
                    is_escalated=0,
                    escalation_reason=None,
                    psych_profile_dict=low_profile,
                    db_path=db_path,
                )
                append_fan_decision_log(char_id, fan_id, low_decision_log, db_path=db_path)

            return {
                "ok": True,
                "char_id": char_id,
                "fan_id": fan_id,
                "is_escalated": 0,
                "escalation_reason": None,
                "is_low_priority": 1,
                "sales_phase": next_phase,
                "current_stage": next_phase,
                "next_stage": "2_Explore",
                "stage_score": 0,
                "stage_threshold": low_thresh,
                "remaining_points": low_thresh,
                "earned_points": 0,
                "score_reason_ja": low_reason_ja,
                "pricing_tier": "low_priority",
                "recommended_ppv": None,
                "ai_en": ai_en_low,
                "ai_ja": ai_ja_low,
                "ai_reply": ai_en_low,
                "ai_reply_ja": ai_ja_low,
                "psych_profile": low_profile,
                "decision_log": low_decision_log,
            }

        # Step 3: リアルタイム顧客カルテ抽出・マージ更新
        psych_profile = update_fan_psych_profile(
            char_id=char_id,
            fan_id=fan_id or "sim_test_fan",
            recent_messages=msg_history,
            existing_profile_json=existing_profile_json,
            total_spent_usd=total_spent_usd,
            current_tier=current_tier,
            db_path=db_path,
            persist_db=persist_db and bool(fan_id),
        )

        # Step 4: 階段式PPV選定 ＆ 好みタグ照合 ＆ 動的プライシング
        recommended_ppv, pricing_mode = self.select_ppv_and_price(
            char_id=char_id,
            fan_id=fan_id,
            total_spent_usd=total_spent_usd,
            current_tier=current_tier,
            purchased_ppv_ids=purchased_ppv_ids,
            plans=plans,
            settings=settings,
            vault_items=vault_items,
            psych_profile=psych_profile,
            user_message=user_message,
            db_path=db_path,
        )

        # Step 5: S.E.L.L. 突破スコア加算 ＆ ステージ遷移判定 ＆ チップ誘導フック候補判定
        turn_count = max(1, len([m for m in msg_history if m.get("sender") == "fan"]))
        stage_prog = self.evaluate_stage_progression(
            current_phase=norm_input_stage,
            user_message=user_message,
            current_tier=current_tier,
            total_spent_usd=total_spent_usd,
            consecutive_ppv_ignored=consecutive_ppv_ignored,
            turn_count=turn_count,
            has_available_ppv=recommended_ppv is not None,
            char_id=char_id,
            fan_id=fan_id,
            previous_score=stage_score,
            existing_profile=existing_profile_json,
            recent_messages=recent_messages,
            stage_thresholds=active_thresholds,
            settings=settings,
            db_path=db_path,
        )
        next_phase = str(stage_prog["current_stage"])

        # アイスブレイク・エクスプロア・クールダウンではPPV売りつけをハードブロック
        if next_phase not in ("3_Lead_Pitch", "4_Lock_Close", "4_Closing"):
            recommended_ppv = None

        tip_hook = self.detect_tip_opportunity(
            user_message=user_message,
            current_phase=norm_input_stage,
            next_phase=next_phase,
            current_tier=current_tier,
            total_spent_usd=total_spent_usd,
            turn_count=turn_count,
        )
        if tip_hook.get("triggered"):
            msg_l = (user_message or "").lower()
            has_explicit_tip_intent = any(
                kw in msg_l or kw in (user_message or "")
                for kw in self.EXPLICIT_TIP_OR_PERSONAL_REQUEST_KEYWORDS
            )
            if (
                next_phase in ("3_Lead_Pitch", "4_Lock_Close", "4_Closing")
                and recommended_ppv is not None
                and not has_explicit_tip_intent
            ):
                # 通常の好意的な会話でステージ突破したターンは、PPV提案を潰さず優先する
                tip_hook = {
                    "triggered": False,
                    "suggested_amount_usd": 0.0,
                    "reason": None,
                    "suppressed_reason": f"ステージ突破（{next_phase}）に伴うPPV提案を優先",
                }
            else:
                recommended_ppv = None
                if next_phase in ("3_Lead_Pitch", "4_Lock_Close", "4_Closing"):
                    next_phase = (
                        "5_Cooldown"
                        if norm_input_stage in ("4_Lock_Close", "4_Closing", "5_Cooldown")
                        else "2_Explore"
                    )
                    adj_next_st, adj_thresh = self.get_stage_target_meta(next_phase, active_thresholds)
                    adj_score = min(int(stage_prog["stage_score"]), max(0, int(adj_thresh) - 5))
                    stage_prog["current_stage"] = next_phase
                    stage_prog["next_stage"] = adj_next_st
                    stage_prog["stage_threshold"] = adj_thresh
                    stage_prog["stage_score"] = adj_score
                    stage_prog["remaining_points"] = max(0, int(adj_thresh) - adj_score)

        resolved_ab = str(ab_group or "").strip().upper()
        if resolved_ab not in ("A", "B") and fan_id:
            fan_rec = get_fan_record(char_id, fan_id, db_path=db_path)
            if fan_rec:
                resolved_ab = str(fan_rec.get("ab_group") or "A").strip().upper()
        if resolved_ab not in ("A", "B"):
            resolved_ab = "A"

        active_playbook = get_active_playbook_for_group(char_id, ab_group=resolved_ab, db_path=db_path)
        prompt_version = f"v{active_playbook.get('version_id', 1)}-{resolved_ab}"
        recent_emojis = self.extract_recent_ai_emojis(msg_history[:-1])

        # Step 6: システムプロンプト構築 ＆ 英語営業応答・日本語訳生成
        system_prompt = self.build_sales_system_prompt(
            char_id=char_id,
            sales_phase=next_phase,
            current_tier=current_tier,
            total_spent_usd=total_spent_usd,
            pricing_mode=pricing_mode,
            recommended_ppv=recommended_ppv,
            psych_profile=psych_profile,
            persona=persona,
            plans=plans,
            ab_group=resolved_ab,
            playbook=active_playbook,
            tip_hook=tip_hook,
            recent_used_emojis=recent_emojis,
            db_path=db_path,
        )

        fallback_reply = self._build_fallback_sales_reply(
            char_id=char_id,
            sales_phase=next_phase,
            pricing_mode=pricing_mode,
            recommended_ppv=recommended_ppv,
            persona=persona,
            plans=plans,
            current_tier=current_tier,
            tip_hook=tip_hook,
        )

        convo_context_lines = [
            f"{'Fan' if m['sender'] == 'fan' else persona.get('display_name', char_id)}: {m['content']}"
            for m in msg_history[-6:]
        ]
        user_prompt = (
            "Recent DM History:\n"
            + "\n".join(convo_context_lines)
            + f"\n\nLatest Fan Message: {user_message}\n"
            "Write your next English DM reply now:"
        )

        try:
            _raw_reply = execute_with_resilient_retry(
                lambda: self.llm.generate_text(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    temperature=0.72,
                    max_tokens=240,
                    fallback_text=fallback_reply,
                ),
                task_name="sales_engine_generate",
            )
        except LLMResilientRetryExhaustedError:
            _raw_reply = fallback_reply
        ai_reply = LLMClient.normalize_dm_newlines(_raw_reply)

        nickname = (persona.get("user_nickname_default") or "babe").split("/")[0].strip()
        if next_phase == "5_Cooldown":
            ai_reply = self.sanitize_cooldown_reply(ai_reply, nickname=nickname, turn_idx=turn_count)

        # ★厳格検証：最終生成された英語返信文の中に本当にチップ要求文言 (tip / spoil me / treat me 等) がある場合のみチップ誘導確定
        if tip_hook.get("triggered"):
            if not self.verify_tip_request_in_reply(ai_reply):
                tip_hook = {
                    "triggered": False,
                    "suggested_amount_usd": 0.0,
                    "reason": None,
                    "suppressed_reason": "最終英語返信文にチップ要求表現が含まれないため誤記録を防止",
                }
        else:
            tip_hook["triggered"] = False

        if not tip_hook.get("triggered") and next_phase in ("3_Lead_Pitch", "4_Lock_Close", "4_Closing") and recommended_ppv:
            offered_price_val = float(recommended_ppv.get("offered_price_usd") or 9.99)
            if "$" not in ai_reply:
                em_a = self.EMOJI_PALETTE[turn_count % len(self.EMOJI_PALETTE)]
                em_b = self.EMOJI_PALETTE[(turn_count + 3) % len(self.EMOJI_PALETTE)]
                ai_reply = f"{ai_reply.rstrip()}\n(Special secret unlock just for you at ${offered_price_val:.2f} {em_a}{em_b})"

        ai_reply_ja = LLMClient.normalize_dm_newlines(self.llm.translate_to_japanese(ai_reply))

        if recommended_ppv:
            psych_profile["recommended_step"] = (
                f"Step {recommended_ppv.get('ladder_step')} ({recommended_ppv.get('ppv_id')})"
            )
            psych_profile["dynamic_pricing"] = (
                f"{pricing_mode.upper()} (${float(recommended_ppv.get('offered_price_usd') or 0.0):.2f})"
            )

        psych_profile["_sell_stage_state"] = {
            "current_stage": stage_prog["current_stage"],
            "next_stage": stage_prog["next_stage"],
            "stage_score": stage_prog["stage_score"],
            "stage_threshold": stage_prog["stage_threshold"],
            "remaining_points": stage_prog["remaining_points"],
            "earned_points": stage_prog["earned_points"],
            "score_reason_ja": stage_prog["score_reason_ja"],
        }

        decision_log = self._build_turn_decision_log(
            user_message=user_message,
            sales_phase=norm_input_stage,
            next_phase=next_phase,
            turn_count=turn_count,
            total_spent_usd=total_spent_usd,
            pricing_mode=pricing_mode,
            recommended_ppv=recommended_ppv,
            tip_hook=tip_hook,
            resolved_ab=resolved_ab,
            prompt_version=prompt_version,
            active_playbook=active_playbook,
            psych_profile=psych_profile,
            ai_reply_ja=ai_reply_ja,
            stage_progression=stage_prog,
        )

        tip_tx_id: Optional[int] = None
        if persist_db and fan_id:
            updated_fan = upsert_fan_sales_state(
                char_id=char_id,
                fan_id=fan_id,
                current_tier=current_tier,
                total_spent_usd=total_spent_usd,
                sales_phase=next_phase,
                consecutive_unanswered=0,
                consecutive_ppv_ignored=consecutive_ppv_ignored,
                is_low_priority=0,
                is_escalated=0,
                escalation_reason=None,
                psych_profile_dict=psych_profile,
                db_path=db_path,
            )
            if updated_fan.get("ab_group"):
                resolved_ab = str(updated_fan["ab_group"])
                decision_log["strategy_reasoning"]["ab_rule_applied"]["ab_group"] = resolved_ab

            append_fan_decision_log(char_id, fan_id, decision_log, db_path=db_path)

            if record_tip_in_db and tip_hook.get("triggered") and self.verify_tip_request_in_reply(ai_reply):
                tip_tx_id = record_ppv_offer(
                    char_id=char_id,
                    fan_id=fan_id,
                    ppv_id=None,
                    offered_price=float(tip_hook.get("suggested_amount_usd") or 5.0),
                    tx_type="tip",
                    db_path=db_path,
                )
                tip_hook["tx_id"] = tip_tx_id

        return {
            "ok": True,
            "char_id": char_id,
            "fan_id": fan_id,
            "ab_group": resolved_ab,
            "prompt_version": prompt_version,
            "playbook_version_id": active_playbook.get("version_id"),
            "is_escalated": 0,
            "escalation_reason": None,
            "is_low_priority": 0,
            "sales_phase": next_phase,
            "current_stage": stage_prog["current_stage"],
            "next_stage": stage_prog["next_stage"],
            "stage_score": stage_prog["stage_score"],
            "stage_threshold": stage_prog["stage_threshold"],
            "remaining_points": stage_prog["remaining_points"],
            "earned_points": stage_prog["earned_points"],
            "score_reason_ja": stage_prog["score_reason_ja"],
            "stage_progression": stage_prog,
            "pricing_tier": pricing_mode,
            "recommended_ppv": recommended_ppv,
            "tip_hook": tip_hook,
            "tip_tx_id": tip_tx_id,
            "ai_en": ai_reply,
            "ai_ja": ai_reply_ja,
            "ai_reply": ai_reply,
            "ai_reply_ja": ai_reply_ja,
            "psych_profile": psych_profile,
            "decision_log": decision_log,
            "system_prompt_preview": system_prompt,
        }

    def _build_turn_decision_log(
        self,
        user_message: str,
        sales_phase: str,
        next_phase: str,
        turn_count: int,
        total_spent_usd: Optional[float],
        pricing_mode: str,
        recommended_ppv: Optional[Dict[str, Any]],
        tip_hook: Dict[str, Any],
        resolved_ab: str,
        prompt_version: str,
        active_playbook: Dict[str, Any],
        psych_profile: Dict[str, Any],
        ai_reply_ja: str,
        stage_progression: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """各ターンにおけるAI顧客カルテ更新根拠・ステージ突破スコア推移・営業戦略検討過程を構造化辞書として構築する"""
        lower_msg = (user_message or "").lower()
        hit_high_intent = [k for k in self.HIGH_INTENT_KEYWORDS if k in lower_msg]
        hit_objection = [k for k in self.OBJECTION_KEYWORDS if k in lower_msg]

        sp = stage_progression or {}
        cur_stage = str(sp.get("current_stage") or next_phase)
        nxt_stage = str(sp.get("next_stage") or self.NEXT_STAGE_MAP.get(cur_stage, "2_Explore"))
        st_score = int(sp.get("stage_score") if sp.get("stage_score") is not None else 0)
        prev_st_score = int(sp.get("previous_stage_score") if sp.get("previous_stage_score") is not None else 0)
        st_thresh = int(sp.get("stage_threshold") if sp.get("stage_threshold") is not None else 40)
        rem_pts = int(sp.get("remaining_points") if sp.get("remaining_points") is not None else max(0, st_thresh - st_score))
        earned_pts = int(sp.get("earned_points") if sp.get("earned_points") is not None else 0)
        score_reason_ja = str(sp.get("score_reason_ja") or "会話継続により加点")
        st_promoted = bool(sp.get("stage_promoted", sales_phase != next_phase))
        st_thresholds_map = sp.get("stage_thresholds") or dict(DEFAULT_STAGE_THRESHOLDS)

        if hit_objection:
            phase_reason_ja = (
                f"価格躊躇・見送りシグナル（{hit_objection[0]}）を検知したため {next_phase} で押し売りを回避 "
                f"（スコア: {prev_st_score} → {st_score}pt）"
            )
        elif st_promoted:
            phase_reason_ja = (
                f"突破スコアが {prev_st_score}pt → {st_score}pt（+{earned_pts}pt）へ上昇し閾値を突破したため、"
                f"{sales_phase} → {next_phase} へ自動昇格！（次目標: {nxt_stage} / {st_thresh}pt）"
            )
        elif sales_phase != next_phase:
            phase_reason_ja = (
                f"会話サイクル（{turn_count}ターン目）に基づき {sales_phase} → {next_phase} へ遷移 "
                f"（現在 {st_score}/{st_thresh}pt・あと {rem_pts}pt で {nxt_stage}）"
            )
        else:
            phase_reason_ja = (
                f"現在ステージ {cur_stage} を継続（スコア {prev_st_score}pt → {st_score}/{st_thresh}pt / "
                f"あと {rem_pts}pt で {nxt_stage} へ突破）"
            )

        if tip_hook.get("triggered"):
            tip_amt_val = float(tip_hook.get("suggested_amount_usd") or 5.0)
            tip_reason_raw = str(tip_hook.get("reason") or "praise_or_special_request")
            ladder_reason_ja = "チップ誘導フックを発動（最終英文にチップ要求表現を確認済）のためPPVロック添付は見送り"
            monetization_ja = f"チップ誘導を発動（理由: {tip_reason_raw} / 提案額: ${tip_amt_val:.2f}）"
        elif next_phase in ("3_Lead_Pitch", "4_Lock_Close", "4_Closing") and recommended_ppv:
            p_step = recommended_ppv.get("ladder_step")
            p_id = recommended_ppv.get("ppv_id")
            p_fname = recommended_ppv.get("filename") or p_id
            p_price = float(recommended_ppv.get("offered_price_usd") or 0.0)
            p_tags = ", ".join((recommended_ppv.get("tags") or [])[:5]) or "-"
            matched_info = (
                f"［好み一致タグ: {', '.join(recommended_ppv['matched_tags'])}］"
                if recommended_ppv.get("matched_tags")
                else ""
            )
            ladder_reason_ja = (
                f"既購入素材を除外の上、Step {p_step} 素材『{p_id} ({p_fname})』（タグ: {p_tags}）{matched_info}を選定 ／ "
                f"累計課金 ${float(total_spent_usd or 0.0):.2f} に応じ {pricing_mode.upper()} 価格（${p_price:.2f}）を適用"
            )
            monetization_ja = (
                f"PPVクロージング提案を実施（{p_id} / {p_fname} / Step {p_step} / ${p_price:.2f}）"
                if next_phase == "4_Lock_Close"
                else f"PPV興味付けリードを実施（{p_id} / {p_fname} / Step {p_step} / ${p_price:.2f}）"
            )
        else:
            ladder_reason_ja = f"フェーズ {next_phase}（雑談・ニーズ探索・冷却）のためPPVロック提案は温存（価格帯判定: {pricing_mode.upper()}）"
            monetization_ja = "売り込みを控え、顧客カルテのフックを用いた親密度・期待感醸成を優先"

        prof_reasoning = psych_profile.get("last_profile_reasoning") or {
            "trigger_fan_message": (user_message or "")[:220],
            "preferences_reason": "直近の会話内容から好みを抽出・マージ",
            "psych_type_reason": f"心理タイプ『{psych_profile.get('psych_type', '-')}』として評価",
            "budget_tier_reason": f"予算感『{psych_profile.get('budget_tier', '-')}』として評価",
            "hooks_reason": "反応の良い話題フックを更新",
        }
        pb_rules_ja = str(active_playbook.get("rules_ja") or active_playbook.get("rules_text") or "").strip()

        return {
            "recorded_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "mode": "auto_sales_reply",
            "fan_message_en": user_message,
            "previous_stage": sales_phase,
            "current_stage": cur_stage,
            "next_stage": nxt_stage,
            "previous_stage_score": prev_st_score,
            "stage_score": st_score,
            "stage_threshold": st_thresh,
            "remaining_points": rem_pts,
            "earned_points": earned_pts,
            "score_reason_ja": score_reason_ja,
            "stage_promoted": st_promoted,
            "stage_thresholds": st_thresholds_map,
            "profile_reasoning": {
                **prof_reasoning,
                "extracted_snapshot": {
                    "preferences": psych_profile.get("preferences") or [],
                    "psych_type": psych_profile.get("psych_type") or "",
                    "budget_tier": psych_profile.get("budget_tier") or "",
                    "hooks": psych_profile.get("hooks") or [],
                },
            },
            "strategy_reasoning": {
                "phase_transition": f"{sales_phase} → {next_phase}",
                "phase_reason_ja": phase_reason_ja,
                "previous_stage": sales_phase,
                "current_stage": cur_stage,
                "next_stage": nxt_stage,
                "previous_stage_score": prev_st_score,
                "stage_score": st_score,
                "stage_threshold": st_thresh,
                "remaining_points": rem_pts,
                "earned_points": earned_pts,
                "score_reason_ja": score_reason_ja,
                "stage_promoted": st_promoted,
                "stage_thresholds": st_thresholds_map,
                "ladder_step_reason_ja": ladder_reason_ja,
                "ab_rule_applied": {
                    "ab_group": resolved_ab,
                    "prompt_version": prompt_version,
                    "playbook_version_id": active_playbook.get("version_id"),
                    "rules_ja_preview": pb_rules_ja[:260],
                },
                "monetization_judgment_ja": monetization_ja,
                "reply_intent_summary_ja": ai_reply_ja,
            },
        }


sales_engine = SalesEngine()






