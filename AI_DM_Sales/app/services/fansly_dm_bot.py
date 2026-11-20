import logging
import os
import queue
import random
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.core import config
from app.core.database import (
    add_dm_message,
    append_fan_decision_log,
    get_fan_record,
    get_fan_threads,
    get_system_reply_delay_settings,
    get_thread_detail,
    has_eligible_locked_ppv_for_unlock,
    mark_ppv_unlocked,
    normalize_char_threshold_bounds,
    normalize_reply_delay_bounds,
    record_ppv_offer,
    resolve_fan_escalation,
    set_fan_manual_hold,
    upsert_fan_sales_state,
)
from app.core.llm_client import LLMClient, llm_client
from app.services.character_loader import (
    ensure_character_files,
    is_character_sleeping,
    is_vault_item_sendable_to_customer,
    list_character_ids,
    load_plans,
    load_settings,
    resolve_customer_safe_ppv_media_path,
    save_settings,
)
from logging.handlers import RotatingFileHandler
from app.services.media_blur_service import resolve_existing_blur_preview_path
from app.services.profiler_service import update_fan_psych_profile
from app.services.sales_engine import sales_engine

logger = logging.getLogger("AI_DM_Sales.FanslyDMBot")
if not logger.handlers:
    _fmt = logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s")
    _handler = logging.StreamHandler()
    _handler.setFormatter(_fmt)
    logger.addHandler(_handler)
    try:
        _log_dir = os.path.join(config.BASE_DIR, "logs")
        os.makedirs(_log_dir, exist_ok=True)
        _file_handler = RotatingFileHandler(
            os.path.join(_log_dir, "fansly_dm_bot.log"),
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        _file_handler.setFormatter(_fmt)
        logger.addHandler(_file_handler)
    except Exception:
        pass
    logger.setLevel(logging.INFO)

JST = timezone(timedelta(hours=9))
FANSLY_MESSAGES_URL = "https://fansly.com/messages"

# ==============================================================================
# Fansly DOM セレクタ定義（2026年実機 Angular 19 DOM構造準拠・仕様変更時はここを更新）
# ==============================================================================
FANSLY_SELECTORS: Dict[str, List[str]] = {
    "logged_in_indicators": [
        "app-messages-route",
        "app-nav-bar-top .logged-in",
        "app-messaging-group",
        "app-messages-default-conversation-route",
    ],
    "login_Check_logged_out": [
        "input[type='password']",
        "app-login-route",
        "button:has-text('Log In')",
        "button:has-text('Sign In')",
        "a[href*='/login']",
    ],
    "thread_links": [
        ".message-list a[href^='/messages/']",
    ],
    "thread_items": [
        "app-messaging-group",
        ".message-list a[href^='/messages/'] app-messaging-group",
        "app-group-message-selection",
        ".message-thread-item",
    ],
    "unread_badge": [
        ".badge-container .badge",
        ".badge-container div.badge",
        ".unread-badge",
        "[data-unread='true']",
    ],
    "thread_username": [
        "app-account-username.xs-mobile-hidden .display-name",
        ".message-contact .display-name",
        ".display-name",
        ".username",
    ],
    "thread_username_link": [
        "app-account-username.xs-mobile-hidden a.username-wrapper",
        "app-account-username a.username-wrapper",
        "app-account-avatar a[href^='/']",
    ],
    "thread_preview": [
        ".eclipse",
        ".font-size-xs.dark-blue-1.eclipse",
    ],
    "thread_time": [
        ".message-time",
    ],
    "thread_fan_id_attr": [
        "data-fan-id",
        "data-user-id",
        "data-group-id",
        "id",
    ],
    "conversation_route": [
        "app-messages-conversation-route",
        "app-group-message-container",
    ],
    "conversation_header_user_link": [
        ".message-content-header app-account-username a.username-wrapper",
        ".message-content-header-avatar a[href^='/']",
    ],
    "tier_badge": [
        ".subscription-tier-badge",
        ".tier-badge",
        ".subscriber-badge",
        ".message-content-header .user-badge",
        "[data-testid='fan-tier-badge']",
    ],
    "message_items": [
        "app-group-message-container app-group-message",
        "app-group-message.message",
        "app-group-message",
    ],
    "message_bubbles_fan": [
        "app-group-message:not(.my-message) .message-text",
        "app-group-message.message:not(.my-message) .message-text",
        ".message-row.incoming .message-text",
    ],
    "message_bubbles_own": [
        "app-group-message.my-message .message-text",
    ],
    "ppv_unlocked_indicators": [
        "app-group-message.my-message app-group-message-attachment .purchased-content .purchased-avatar:not(.not-purchased)",
        ".ppv-message.unlocked",
        "[data-ppv-unlocked='true']",
        "div:has-text('Unlocked for $')",
    ],
    "dm_not_allowed": [
        "app-group-message-input .no-dm-allowed",
        "app-dm-permission-flags-display",
        "app-group-message-input .message-composer.hidden",
    ],
    "chat_input": [
        "app-group-message-input .message-composer:not(.hidden) textarea.message-input",
        "app-group-message-input textarea[aria-label='Message']",
        "textarea.message-input",
        "textarea[placeholder*='message' i]",
    ],
    "file_input": [
        "app-group-message-input .collapsable-actions input[type='file']",
        "app-group-message-input input[type='file']",
        "input[type='file']",
    ],
    "upload_modal": [
        "app-account-media-upload-v2",
        ".modal.upload-v2",
    ],
    "upload_media_tiles": [
        "app-account-media-upload-v2 .upload-rail .fansly-media-tile.upload-tile:not(.upload-tile-add)",
        "app-account-media-upload-v2 .fansly-media-tile.upload-tile:not(.upload-tile-add)",
    ],
    "upload_preview_badge": [
        "app-account-media-upload-v2 .upload-stage-media .upload-stage-badge",
        "app-account-media-upload-v2 .upload-stage-badge",
        ".upload-stage-badge",
    ],
    "upload_preview_dropdown_upload": [
        "app-account-media-upload-v2 .upload-stage-badge .dropdown-list .dropdown-item:has(app-icon[name='upload'])",
        "app-account-media-upload-v2 .upload-stage-badge .dropdown-list .dropdown-item:has(.fa-upload)",
        "app-account-media-upload-v2 .upload-stage-badge .dropdown-list .dropdown-item:has-text('Upload')",
        "app-account-media-upload-v2 .upload-stage-badge .dropdown-list .dropdown-item:has-text('アップロード')",
    ],
    "upload_preview_file_input": [
        "app-account-media-upload-v2 input[type='file']:not([multiple])",
    ],
    "media_editor_modal": [
        "app-media-editor",
        ".modal.image-editor",
    ],
    "media_editor_done": [
        "app-media-editor .modal-footer button.solid-blue",
        "app-media-editor button.solid-blue",
        "app-media-editor button:has-text('Done')",
        "app-media-editor button:has-text('完了')",
    ],
    "media_editor_cancel": [
        "app-media-editor .modal-footer button.btn.v2.text",
        "app-media-editor .editor-top button[aria-label='Close']",
    ],
    "upload_perm_remove_chip": [
        "app-permission-rows-editor button.perm-cond-remove",
        "app-permission-rows-editor button[aria-label='Remove requirement']",
    ],
    "upload_perm_add_chip": [
        "app-permission-rows-editor .perm-cond-add",
        "app-permission-rows-editor .fansly-chip.perm-cond-add",
    ],
    "price_modal": [
        "app-balance-input-modal",
        ".creator-dashboard-modal",
    ],
    "ppv_price_button": [
        "app-permission-rows-editor .perm-cond-add",
        "button[aria-label='Add paid text']",
        "button.ppv-price-btn",
    ],
    "ppv_price_input": [
        "app-balance-input-modal input[aria-label='Amount']",
        "app-balance-input input[inputmode='decimal']",
        "input[aria-label='Amount']",
        "input[placeholder*='Price' i]",
    ],
    "ppv_price_confirm": [
        "app-balance-input-modal .modal-footer app-button.solid-blue",
        "app-balance-input-modal app-button[clickonenter='true']",
        "button:has-text('Confirm')",
        "button:has-text('Save')",
    ],
    "upload_attach_confirm": [
        "app-account-media-upload-v2 .modal-footer button.upload-primary",
        "button.upload-primary",
    ],
    "upload_modal_cancel": [
        "app-account-media-upload-v2 .modal-footer button.btn.v2.text",
        "app-account-media-upload-v2 .upload-top button[aria-label='Close']",
    ],
    "send_button": [
        "app-group-message-input button.send-button:not([disabled])",
        "app-group-message-input button.send-button",
        "button[aria-label='Send message']",
        "button.send-message-btn",
    ],
}


def normalize_dm_message_text(text: Any) -> str:
    """DM送信・保存用テキストの改行コード（CRLF / エスケープ '\\n'）を正規化して複数行構造を保持する"""
    return LLMClient.normalize_dm_newlines(str(text or ""))


def _is_recent_thread_time(time_str: str) -> bool:
    """スレッド一覧の時刻（'Just now', '32m', '2h' 等）が直近数時間以内か判定する"""
    t = str(time_str or "").strip().lower()
    if not t:
        return False
    if t in ("now", "just now"):
        return True
    return bool(re.match(r"^\d+\s*(s|sec|secs|m|min|mins|h|hr|hrs)$", t))


def get_jst_now(now_dt: Optional[datetime] = None) -> datetime:
    """現在時刻をJSTとして取得する"""
    if now_dt is not None:
        return now_dt
    return datetime.now(JST).replace(tzinfo=None)


def check_char_sleep_state(char_id: str, now_dt: Optional[datetime] = None) -> Tuple[bool, Dict[str, Any]]:
    """
    settings.json の sleep_schedule を現在時刻 (JST) と照合し、
    (sleeping_bool, sleep_schedule_dict) を返す。
    """
    settings = load_settings(char_id)
    schedule = settings.get("sleep_schedule") if isinstance(settings.get("sleep_schedule"), dict) else {
        "enabled": False,
        "start": "19:00",
        "end": "22:00",
    }
    target_dt = get_jst_now(now_dt)
    sleeping = is_character_sleeping(settings, now_dt=target_dt)
    return sleeping, schedule


def get_configured_reply_delay_settings(
    char_id: Optional[str] = None,
    db_path: Optional[str] = None,
) -> Dict[str, int]:
    """
    system_settings テーブルおよび settings.json から返信待機時間設定4項目を取得する。
    - min_reply_delay_sec (デフォルト 60秒)
    - max_reply_delay_sec (デフォルト 300秒)
    - short_msg_chars_threshold (デフォルト 20文字)
    - long_msg_chars_threshold (デフォルト 150文字)
    """
    sys_cfg = get_system_reply_delay_settings(db_path=db_path)
    mn = sys_cfg.get("min_reply_delay_sec", 60)
    mx = sys_cfg.get("max_reply_delay_sec", 300)
    sc = sys_cfg.get("short_msg_chars_threshold", 20)
    lc = sys_cfg.get("long_msg_chars_threshold", 150)
    if char_id:
        try:
            c_settings = load_settings(char_id)
            if "min_reply_delay_sec" in c_settings and "max_reply_delay_sec" in c_settings:
                mn = c_settings["min_reply_delay_sec"]
                mx = c_settings["max_reply_delay_sec"]
            if "short_msg_chars_threshold" in c_settings and "long_msg_chars_threshold" in c_settings:
                sc = c_settings["short_msg_chars_threshold"]
                lc = c_settings["long_msg_chars_threshold"]
        except Exception:
            pass
    norm_min, norm_max = normalize_reply_delay_bounds(mn, mx)
    norm_sc, norm_lc = normalize_char_threshold_bounds(sc, lc)
    return {
        "min_reply_delay_sec": norm_min,
        "max_reply_delay_sec": norm_max,
        "short_msg_chars_threshold": norm_sc,
        "long_msg_chars_threshold": norm_lc,
    }


def get_configured_reply_delay_range(
    char_id: Optional[str] = None,
    db_path: Optional[str] = None,
) -> Tuple[float, float]:
    cfg = get_configured_reply_delay_settings(char_id=char_id, db_path=db_path)
    return (float(cfg["min_reply_delay_sec"]), float(cfg["max_reply_delay_sec"]))


def _compute_human_reply_delay_sec(
    fan_text: str,
    settings: Optional[Dict[str, Any]] = None,
    ai_reply_text: str = "",
    fast_mode: bool = False,
    jitter_ratio: float = 0.15,
) -> Dict[str, Any]:
    """
    ファンからの未返信メッセージ分量（文字数）に応じた返信待機時間の比例算出アルゴリズム：
    - char_len <= short_msg_chars_threshold -> ratio = 0.0 (最短待機秒数付近)
    - char_len >= long_msg_chars_threshold  -> ratio = 1.0 (最長待機秒数付近)
    - 中間は線形補間 (char_len - short_thresh) / (long_thresh - short_thresh)
    - ベース秒数に ±15% 程度の微小ランダムゆらぎを加え、[min_sec, max_sec] にクランプする。
    """
    cfg = settings or {}
    min_sec, max_sec = normalize_reply_delay_bounds(
        cfg.get("min_reply_delay_sec", 60),
        cfg.get("max_reply_delay_sec", 300),
    )
    short_thresh, long_thresh = normalize_char_threshold_bounds(
        cfg.get("short_msg_chars_threshold", 20),
        cfg.get("long_msg_chars_threshold", 150),
    )

    cleaned_fan = (fan_text or "").strip()
    char_len = len(cleaned_fan)

    if fast_mode:
        return {
            "fan_char_len": char_len,
            "short_msg_chars_threshold": short_thresh,
            "long_msg_chars_threshold": long_thresh,
            "length_ratio": 0.0,
            "base_delay_sec": 0.02,
            "final_delay_sec": 0.02,
            "read_delay_sec": 0.01,
            "typing_delay_sec": 0.01,
        }

    if char_len <= short_thresh:
        ratio = 0.0
    elif char_len >= long_thresh:
        ratio = 1.0
    else:
        ratio = (char_len - short_thresh) / float(max(1, long_thresh - short_thresh))

    base_sec = float(min_sec) + float(max_sec - min_sec) * ratio
    if jitter_ratio > 0.0 and max_sec > min_sec:
        jitter_factor = 1.0 + random.uniform(-abs(jitter_ratio), abs(jitter_ratio))
        jittered = base_sec * jitter_factor
    else:
        jittered = base_sec

    final_total = max(float(min_sec), min(float(max_sec), jittered))
    if final_total <= 0.0:
        read_sec, type_sec = 0.0, 0.0
    else:
        ai_len = max(1, len((ai_reply_text or "").strip()))
        desired_type = 10.0 + 20.0 * min(1.0, ai_len / 180.0)
        type_sec = round(min(desired_type, final_total * 0.25), 2)
        read_sec = round(max(0.0, final_total - type_sec), 2)
        final_total = round(read_sec + type_sec, 2)

    return {
        "fan_char_len": char_len,
        "short_msg_chars_threshold": short_thresh,
        "long_msg_chars_threshold": long_thresh,
        "min_reply_delay_sec": min_sec,
        "max_reply_delay_sec": max_sec,
        "length_ratio": round(ratio, 3),
        "base_delay_sec": round(base_sec, 2),
        "final_delay_sec": round(final_total, 2),
        "read_delay_sec": read_sec,
        "typing_delay_sec": type_sec,
    }


def calculate_human_delays(
    message_text: str,
    fast_mode: bool = False,
    read_delay_range: Tuple[float, float] = (60.0, 300.0),
    typing_delay_range: Tuple[float, float] = (10.0, 30.0),
    short_msg_chars_threshold: int = 20,
    long_msg_chars_threshold: int = 150,
) -> Tuple[float, float]:
    """後方互換ラッパー：_compute_human_reply_delay_sec を用いて (read_delay_sec, typing_delay_sec) を返す"""
    res = _compute_human_reply_delay_sec(
        fan_text=message_text,
        settings={
            "min_reply_delay_sec": read_delay_range[0],
            "max_reply_delay_sec": read_delay_range[1],
            "short_msg_chars_threshold": short_msg_chars_threshold,
            "long_msg_chars_threshold": long_msg_chars_threshold,
        },
        ai_reply_text=message_text,
        fast_mode=fast_mode,
    )
    return (float(res["read_delay_sec"]), float(res["typing_delay_sec"]))


def resolve_fan_tier_from_badge(char_id: str, raw_badge_text: Optional[str]) -> str:
    """
    Fanslyチャット画面のバッジ文字列を plans.json の tier_id にマッピングする。
    未検出または一致しない場合は 'free_follower' を返す。
    """
    if not raw_badge_text or not str(raw_badge_text).strip():
        return "free_follower"

    badge_lower = str(raw_badge_text).strip().lower()
    plans_data = load_plans(char_id)
    tiers = plans_data.get("tiers") if isinstance(plans_data, dict) else []
    if isinstance(tiers, list):
        for t in tiers:
            if not isinstance(t, dict):
                continue
            tid = str(t.get("tier_id") or "").strip()
            tname = str(t.get("name") or "").strip()
            if tid and (tid.lower() == badge_lower or tid.lower() in badge_lower):
                return tid
            if tname and (tname.lower() == badge_lower or tname.lower() in badge_lower):
                return tid

    if "gold" in badge_lower or "goddess" in badge_lower or "vip+" in badge_lower:
        return "gold_goddess"
    if "silver" in badge_lower or "vip" in badge_lower or "sub" in badge_lower:
        return "silver_vip"
    return "free_follower"


def _query_first(root: Any, selector_keys: List[str]) -> Any:
    """複数のセレクタ候補を順に試し、最初に見つかった要素を返す（失敗時はNone）"""
    if root is None:
        return None
    for sel in selector_keys:
        try:
            el = root.query_selector(sel)
            if el is not None:
                return el
        except Exception:
            continue
    return None


def _query_all(root: Any, selector_keys: List[str]) -> List[Any]:
    """複数のセレクタ候補を順に試し、要素リストが見つかった最初の結果を返す"""
    if root is None:
        return []
    for sel in selector_keys:
        try:
            els = root.query_selector_all(sel)
            if els:
                return list(els)
        except Exception:
            continue
    return []


class CharacterBotWorker:
    """
    キャラクター単位で独立稼働するPlaywright DM監視・送信ワーカー。
    - 永続ブラウザプロファイル: config.PROFILES_DIR / <char_id>
    - 睡眠時間帯は新規返信・追撃送信を保留し sleeping=True を報告
    - 受信DM検知 -> 顧客プラン判定 -> PPV開封検知 -> S.E.L.L. 営業エンジン -> 既読遅延＆タイピング演出 -> PPVロック添付送信
    """

    def __init__(
        self,
        char_id: str,
        poll_interval_sec: float = 12.0,
        fast_mode: bool = False,
        headless: bool = True,
    ):
        self.char_id = char_id
        self.poll_interval_sec = poll_interval_sec
        self.fast_mode = fast_mode
        self.headless = headless

        paths = ensure_character_files(char_id)
        self.profile_dir = os.path.join(config.PROFILES_DIR, char_id)
        os.makedirs(self.profile_dir, exist_ok=True)
        self.vault_dir = paths.get("vault_dir") or os.path.join(config.CHARACTERS_ROOT, char_id, "ppv_vault")

        self._lock = threading.RLock()
        self._page_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._outbox_queue: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self._active_context: Any = None
        self._active_pw: Any = None
        self._active_page: Any = None
        self._in_busy_op: bool = False
        self._max_send_retries: int = 3
        self._retry_backoff_base_sec: float = 8.0

        self.running: bool = False
        self.manual_login_active: bool = False
        self.processed_messages_count: int = 0
        self.sent_ppv_count: int = 0
        self.last_poll_at: Optional[str] = None
        self.last_heartbeat_ts: float = 0.0
        self.last_heartbeat_at: Optional[str] = None
        self.consecutive_poll_failures: int = 0
        self.restart_count: int = 0
        self.last_restart_at: Optional[str] = None
        self.last_restart_reason: Optional[str] = None
        self.last_error: Optional[str] = None
        self.sent_history: List[Dict[str, Any]] = []
        self._fan_group_hrefs: Dict[str, str] = {}
        self._checked_read_previews: Dict[str, str] = {}
        self._last_replied_fingerprints: Dict[str, str] = {}
        self._monitored_only_fingerprints: Dict[str, str] = {}
        self._unanswered_recheck_after_ts: Dict[str, float] = {}
        self._pending_replies: Dict[str, Dict[str, Any]] = {}

    def touch_heartbeat(self) -> None:
        now_ts = time.time()
        now_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        with self._lock:
            self.last_heartbeat_ts = now_ts
            self.last_heartbeat_at = now_iso

    def _force_close_browser_resources(self) -> None:
        with self._lock:
            ctx = self._active_context
            pw = self._active_pw
            self._active_context = None
            self._active_pw = None
            self._active_page = None
        if ctx is not None:
            try:
                ctx.close()
            except Exception:
                pass
        if pw is not None:
            try:
                pw.stop()
            except Exception:
                pass

    def force_restart(self, reason: str = "watchdog_recovery") -> bool:
        """ハング・停止検知時にブラウザリソースを破棄し、新しい監視スレッドで自動再スタートする"""
        if self.manual_login_active:
            logger.info(f"[{self.char_id}] Skipping force_restart during manual_login_active (reason={reason})")
            return False

        logger.warning(f"[{self.char_id}] Watchdog triggering worker restart: reason={reason}")
        with self._lock:
            old_stop_event = self._stop_event
            old_thread = self._thread
            old_stop_event.set()
            self._wake_event.set()
            self.running = False
            self._thread = None

        self._force_close_browser_resources()

        if old_thread and old_thread.is_alive() and old_thread is not threading.current_thread():
            old_thread.join(timeout=2.5)

        with self._lock:
            self._stop_event = threading.Event()
            self._wake_event.clear()
            self._in_busy_op = False
            self.consecutive_poll_failures = 0
            self.restart_count += 1
            self.last_restart_at = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            self.last_restart_reason = reason
            # 再起動前に送信途中 (sending_typing) で中断した未完了エントリがあれば待機状態へ安全に戻す
            for pend in self._pending_replies.values():
                if not pend["cancel_event"].is_set() and pend.get("stage") == "sending_typing":
                    pend["stage"] = "retrying_after_restart"
                    pend["typing_done"] = True
                    pend["typing_delay_sec"] = 0.0

        return self.start()

    def get_profile_dir(self) -> str:
        os.makedirs(self.profile_dir, exist_ok=True)
        return self.profile_dir

    def is_sleeping(self, now_dt: Optional[datetime] = None) -> Tuple[bool, Dict[str, Any]]:
        return check_char_sleep_state(self.char_id, now_dt=now_dt)

    def start(self) -> bool:
        with self._lock:
            if self.running and self._thread and self._thread.is_alive():
                return True
            self._stop_event.clear()
            self._wake_event.clear()
            self.running = True
            self.touch_heartbeat()
            self._thread = threading.Thread(
                target=self._worker_loop,
                name=f"FanslyDMBot-{self.char_id}",
                daemon=True,
            )
            self._thread.start()
            logger.info(f"[{self.char_id}] CharacterBotWorker started (profile={self.profile_dir})")
            return True

    def stop(self, timeout: float = 3.0) -> bool:
        self.cancel_all_pending_replies()
        with self._lock:
            self._stop_event.set()
            self._wake_event.set()
            self.running = False
            thread = self._thread
            self._thread = None
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        if thread and thread.is_alive() and thread is not threading.current_thread():
            self._force_close_browser_resources()
        logger.info(f"[{self.char_id}] CharacterBotWorker stopped")
        return True

    def has_active_session_activity(self, fan_id: str) -> bool:
        """指定ファンに対して自動返信カウントダウン中または未送信キューが存在するか判定する（追撃競合防止用）"""
        target_fid = str(fan_id or "").strip()
        if not target_fid:
            return False
        with self._lock:
            pend = self._pending_replies.get(target_fid)
            if pend is not None and not pend["cancel_event"].is_set():
                return True
            with self._outbox_queue.mutex:
                for q_item in self._outbox_queue.queue:
                    if str(q_item.get("fan_id") or "").strip() == target_fid:
                        return True
        return False

    def get_pending_replies_snapshot(self, fan_id: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
        """現在待機カウントダウン中の自動返信ステータスを取得する"""
        now_ts = time.time()
        with self._lock:
            items = (
                [(fan_id, self._pending_replies[fan_id])]
                if fan_id and fan_id in self._pending_replies
                else list(self._pending_replies.items())
            )
            out: Dict[str, Dict[str, Any]] = {}
            for fid, entry in items:
                if entry["cancel_event"].is_set():
                    continue
                rem = max(0, int(round(float(entry["scheduled_ts"]) - now_ts)))
                out[fid] = {
                    "char_id": self.char_id,
                    "fan_id": fid,
                    "is_waiting": True,
                    "stage": entry.get("stage", "waiting_read_delay"),
                    "remaining_seconds": rem,
                    "scheduled_time_hms": entry.get("scheduled_time_hms", ""),
                    "scheduled_at_iso": entry.get("scheduled_at_iso", ""),
                    "total_delay_sec": entry.get("total_delay_sec", 0),
                    "retry_count": int(entry.get("retry_count") or 0),
                    "ai_preview_en": str(entry.get("ai_en") or "")[:160],
                    "ai_preview_ja": str(entry.get("ai_ja") or "")[:160],
                }
            return out

    def cancel_pending_reply(self, fan_id: str) -> bool:
        """指定ファンの自動返信カウントダウンを即時キャンセルする（手動介入送信キューは維持）"""
        cancelled = False
        with self._lock:
            entry = self._pending_replies.pop(fan_id, None)
            if entry is not None:
                entry["cancel_event"].set()
                cancelled = True
            kept: List[Dict[str, Any]] = []
            while True:
                try:
                    q_item = self._outbox_queue.get_nowait()
                except queue.Empty:
                    break
                if str(q_item.get("fan_id") or "") == fan_id and str(q_item.get("source") or "") != "human_manual":
                    cancelled = True
                else:
                    kept.append(q_item)
                self._outbox_queue.task_done()
            for q_item in kept:
                self._outbox_queue.put(q_item)
        if cancelled:
            logger.info(f"[{self.char_id}] Cancelled pending auto-reply for @{fan_id}")
        return cancelled

    def cancel_all_pending_replies(self) -> int:
        with self._lock:
            fids = list(self._pending_replies.keys())
        count = 0
        for fid in fids:
            if self.cancel_pending_reply(fid):
                count += 1
        return count

    def enqueue_outbound(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """追撃ワーカー (FollowupWorker) や手動介入から送信キューへ投入する"""
        source = str(payload.get("source") or "followup")
        target_fid = str(payload.get("fan_id") or "").strip()
        if source != "human_manual" and target_fid:
            with self._lock:
                pend = self._pending_replies.get(target_fid)
                if pend is not None and not pend["cancel_event"].is_set():
                    logger.info(
                        f"[{self.char_id}] Deferred/skipped outbox enqueue ({source}) for @{target_fid}: "
                        "auto-reply countdown currently active"
                    )
                    return {
                        "char_id": self.char_id,
                        "fan_id": target_fid,
                        "source": source,
                        "skipped_active_pending_reply": True,
                        "queued_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                    }
        item = {
            "char_id": self.char_id,
            "fan_id": target_fid,
            "username": str(payload.get("username") or payload.get("fan_id") or "").strip(),
            "message_en": normalize_dm_message_text(payload.get("message_en")),
            "message_ja": normalize_dm_message_text(payload.get("message_ja")),
            "ppv_item": payload.get("ppv_item"),
            "offered_price": payload.get("offered_price"),
            "source": source,
            "retry_count": int(payload.get("retry_count") or 0),
            "next_attempt_ts": float(payload.get("next_attempt_ts") or 0.0),
            "queued_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        }
        self._outbox_queue.put(item)
        self._wake_event.set()
        return item

    def get_status(self, now_dt: Optional[datetime] = None) -> Dict[str, Any]:
        sleeping, schedule = self.is_sleeping(now_dt=now_dt)
        settings = load_settings(self.char_id)
        bot_enabled = bool(settings.get("bot_enabled", False))
        pending_snap = self.get_pending_replies_snapshot()
        with self._lock:
            is_alive = bool(self.running and self._thread and self._thread.is_alive())
            return {
                "char_id": self.char_id,
                "running": is_alive,
                "monitoring_active": is_alive,
                "bot_enabled": bot_enabled,
                "sleeping": bool(sleeping),
                "is_sleeping": bool(sleeping),
                "sleep_schedule": schedule,
                "profile_dir": self.profile_dir,
                "manual_login_active": bool(self.manual_login_active),
                "queued_count": self._outbox_queue.qsize(),
                "pending_replies_count": len(pending_snap),
                "pending_replies": pending_snap,
                "processed_count": self.processed_messages_count,
                "sent_ppv_count": self.sent_ppv_count,
                "last_poll_at": self.last_poll_at,
                "last_heartbeat_at": self.last_heartbeat_at,
                "last_heartbeat_ts": self.last_heartbeat_ts,
                "consecutive_poll_failures": self.consecutive_poll_failures,
                "restart_count": self.restart_count,
                "last_restart_at": self.last_restart_at,
                "last_restart_reason": self.last_restart_reason,
                "last_error": self.last_error,
            }

    def _wait_interruptible(
        self,
        seconds: float,
        cancel_event: Optional[threading.Event] = None,
        wake_on_activity: bool = False,
    ) -> bool:
        """停止イベントまたは個別キャンセルイベントを監視しながら指定秒数待機する"""
        if seconds <= 0:
            if self._stop_event.is_set() or (cancel_event is not None and cancel_event.is_set()):
                return False
            return True
        end_time = time.time() + seconds
        last_hb = time.time()
        while time.time() < end_time:
            if self._stop_event.is_set() or (cancel_event is not None and cancel_event.is_set()):
                return False
            if wake_on_activity and self._wake_event.is_set():
                self._wake_event.clear()
                return True
            now_t = time.time()
            if now_t - last_hb >= 5.0:
                self.touch_heartbeat()
                last_hb = now_t
            remaining = end_time - now_t
            time.sleep(min(0.2, max(0.01, remaining)))
        return not (self._stop_event.is_set() or (cancel_event is not None and cancel_event.is_set()))

    def handle_incoming_fan_message(
        self,
        fan_id: str,
        username: str,
        fan_message_en: str,
        raw_tier_badge: Optional[str] = None,
        detected_ppv_unlocked: bool = False,
        unlocked_ppv_id: Optional[str] = None,
        page: Any = None,
        now_dt: Optional[datetime] = None,
        db_path: Optional[str] = None,
        respect_bot_enabled: bool = True,
        force_auto_reply: bool = False,
        async_schedule: bool = False,
    ) -> Dict[str, Any]:
        """
        1件のファン受信メッセージを処理する。
        - 稼働スイッチ (bot_enabled) が OFF の状態でも、PPV開封反映・新着DMのDB保存・日本語翻訳・AI顧客カルテ更新は常時継続する。
        - AI自動返信・PPV自動送信は bot_enabled == True かつ非保留 (manual_hold == 0) かつ非睡眠時のみ実行する。
        """
        if detected_ppv_unlocked and (
            unlocked_ppv_id is not None
            or has_eligible_locked_ppv_for_unlock(self.char_id, fan_id, min_elapsed_sec=15.0, now_dt=now_dt, db_path=db_path)
        ):
            mark_ppv_unlocked(
                char_id=self.char_id,
                fan_id=fan_id,
                ppv_id=unlocked_ppv_id,
                db_path=db_path,
            )

        existing_fan = get_fan_record(self.char_id, fan_id, db_path=db_path) or {}
        if int(existing_fan.get("is_muted") or 0) == 1:
            return {"ok": True, "action": "skipped_muted", "char_id": self.char_id, "fan_id": fan_id}

        detected_tier = (
            resolve_fan_tier_from_badge(self.char_id, raw_tier_badge)
            if raw_tier_badge
            else str(existing_fan.get("current_tier") or "free_follower")
        )
        total_spent = float(existing_fan.get("total_spent_usd") or 0.0)
        sales_phase = str(existing_fan.get("sales_phase") or "1_Icebreak")
        ignored_count = int(existing_fan.get("consecutive_ppv_ignored") or 0)
        unanswered_count = int(existing_fan.get("consecutive_unanswered") or 0)
        existing_profile = existing_fan.get("psych_profile_json") or "{}"

        thread_detail = get_thread_detail(self.char_id, fan_id, db_path=db_path) or {}
        recent_messages = list(thread_detail.get("messages") or [])

        already_recorded_fan_msg = False
        if recent_messages:
            last_db_msg = recent_messages[-1]
            if (
                str(last_db_msg.get("sender") or "") == "fan"
                and str(last_db_msg.get("content") or "").strip() == fan_message_en.strip()
            ):
                already_recorded_fan_msg = True

        if not already_recorded_fan_msg:
            fan_msg_ja = llm_client.translate_to_japanese(fan_message_en)
            saved_fan_msg = add_dm_message(
                char_id=self.char_id,
                fan_id=fan_id,
                sender="fan",
                content=fan_message_en,
                sales_phase=sales_phase,
                content_ja=fan_msg_ja,
                db_path=db_path,
            )
            recent_messages.append(saved_fan_msg)
            logger.info(
                f"[{self.char_id}] Saved incoming DM from @{fan_id} ({username}) to DB: "
                f"en='{fan_message_en[:80]}' / ja='{fan_msg_ja[:60]}'"
            )
        else:
            logger.info(f"[{self.char_id}] Checking pending message for @{fan_id} (already in DB)")

        skip_res = self._maybe_handle_monitor_only_turn(
            fan_id=fan_id,
            username=username,
            fan_message_en=fan_message_en,
            existing_fan=existing_fan,
            detected_tier=detected_tier,
            total_spent=total_spent,
            sales_phase=sales_phase,
            existing_profile=existing_profile,
            recent_messages=recent_messages,
            now_dt=now_dt,
            db_path=db_path,
            respect_bot_enabled=respect_bot_enabled,
            force_auto_reply=force_auto_reply,
        )
        if skip_res is not None:
            return skip_res

        # 非同期カウントダウン予約時 (async_schedule=True) は、実ブラウザでの送信が成功するまで
        # fans.sales_phase や psych_profile を先行コミットしない（送信失敗・再起動時のステージ暴走防止）
        defer_state_commit = bool(async_schedule and page is not None)
        engine_res = sales_engine.process_fan_message(
            char_id=self.char_id,
            user_message=fan_message_en,
            fan_id=fan_id,
            current_tier=detected_tier,
            total_spent_usd=total_spent,
            sales_phase=sales_phase,
            consecutive_ppv_ignored=ignored_count,
            consecutive_unanswered=unanswered_count,
            recent_messages=recent_messages[:-1] if recent_messages else [],
            existing_profile_json=existing_profile,
            persist_db=not defer_state_commit,
            record_tip_in_db=False,
            db_path=db_path,
        )
        upsert_fan_sales_state(
            char_id=self.char_id,
            fan_id=fan_id,
            username=username or fan_id,
            current_tier=detected_tier,
            db_path=db_path,
        )
        res = self._dispatch_sales_engine_reply(
            fan_id=fan_id,
            username=username or fan_id,
            fan_message_en=fan_message_en,
            sales_phase=sales_phase,
            engine_res=engine_res,
            page=page,
            db_path=db_path,
            async_schedule=async_schedule,
            allow_when_bot_off=force_auto_reply,
            detected_tier=detected_tier,
            total_spent_usd=total_spent,
            consecutive_ppv_ignored=ignored_count,
            defer_state_commit=defer_state_commit,
        )
        if res.get("ok") and res.get("action") in ("replied", "escalated_emergency_stop"):
            self._last_replied_fingerprints[fan_id] = fan_message_en.strip()
        return res

    def _maybe_handle_monitor_only_turn(
        self,
        fan_id: str,
        username: str,
        fan_message_en: str,
        existing_fan: Dict[str, Any],
        detected_tier: str,
        total_spent: float,
        sales_phase: str,
        existing_profile: Any,
        recent_messages: List[Dict[str, Any]],
        now_dt: Optional[datetime],
        db_path: Optional[str],
        respect_bot_enabled: bool,
        force_auto_reply: bool,
    ) -> Optional[Dict[str, Any]]:
        """稼働OFF / 手動保留 / 要介入 / 睡眠中の場合はAI顧客カルテ＆根拠のみ更新して自動返信をスキップする"""
        is_escalated = int(existing_fan.get("is_escalated") or 0) == 1
        manual_hold = int(existing_fan.get("manual_hold") or 0) == 1
        bot_enabled = bool(load_settings(self.char_id).get("bot_enabled", False))
        sleeping, schedule = self.is_sleeping(now_dt=now_dt)

        skip_reason: Optional[str] = None
        if is_escalated and not force_auto_reply:
            skip_reason = "skipped_escalated"
        elif manual_hold and not force_auto_reply:
            skip_reason = "skipped_manual_hold"
        elif respect_bot_enabled and not bot_enabled and not force_auto_reply:
            skip_reason = "monitored_only_bot_off"
        elif sleeping and not force_auto_reply:
            skip_reason = "deferred_sleeping"

        if skip_reason is None:
            return None

        updated_prof = None
        if self._monitored_only_fingerprints.get(fan_id) != fan_message_en.strip():
            updated_prof = update_fan_psych_profile(
                char_id=self.char_id,
                fan_id=fan_id,
                recent_messages=recent_messages,
                existing_profile_json=existing_profile,
                total_spent_usd=total_spent,
                current_tier=detected_tier,
                db_path=db_path,
                persist_db=True,
            )
            upsert_fan_sales_state(
                char_id=self.char_id,
                fan_id=fan_id,
                username=username or fan_id,
                current_tier=detected_tier,
                db_path=db_path,
            )
            reason_label_map = {
                "monitored_only_bot_off": "稼働スイッチOFF（監視継続モード）のため新着保存・翻訳・カルテ更新のみ実施し、AI自動返信・PPV送信をスキップ",
                "skipped_manual_hold": "手動対応保留中（manual_hold）のためカルテ更新のみ実施し、AI自動返信を停止",
                "skipped_escalated": "要介入ロック中のため新着保存・カルテ更新のみ実施し自動返信を停止",
                "deferred_sleeping": "睡眠時間帯のため新着保存・カルテ更新のみ実施し起床まで返信を保留",
            }
            prof_reason = (updated_prof or {}).get("last_profile_reasoning") or {}
            append_fan_decision_log(
                self.char_id,
                fan_id,
                {
                    "recorded_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                    "mode": skip_reason,
                    "fan_message_en": fan_message_en,
                    "profile_reasoning": {
                        **prof_reason,
                        "extracted_snapshot": {
                            "preferences": (updated_prof or {}).get("preferences") or [],
                            "psych_type": (updated_prof or {}).get("psych_type") or "",
                            "budget_tier": (updated_prof or {}).get("budget_tier") or "",
                            "hooks": (updated_prof or {}).get("hooks") or [],
                        },
                    },
                    "strategy_reasoning": {
                        "phase_transition": f"{sales_phase} → {sales_phase} (自動返信保留)",
                        "phase_reason_ja": reason_label_map.get(skip_reason, skip_reason),
                        "ladder_step_reason_ja": "自動営業スキップ中のためPPV提案なし",
                        "ab_rule_applied": {"ab_group": existing_fan.get("ab_group") or "A", "prompt_version": "monitor-only"},
                        "monetization_judgment_ja": "手動返信待機または稼働ON待機",
                        "reply_intent_summary_ja": "（自動返信なし・人間オペレーターによる手動返信のみ受付中）",
                    },
                },
                db_path=db_path,
            )
            self._monitored_only_fingerprints[fan_id] = fan_message_en.strip()

        return {
            "ok": True,
            "action": skip_reason,
            "bot_enabled": bot_enabled,
            "manual_hold": manual_hold,
            "sleeping": bool(sleeping),
            "sleep_schedule": schedule,
            "char_id": self.char_id,
            "fan_id": fan_id,
            "psych_profile": updated_prof,
        }

    def _schedule_pending_reply(
        self,
        fan_id: str,
        username: str,
        fan_message_en: str,
        sales_phase: str,
        engine_res: Dict[str, Any],
        custom_delay_sec: Optional[float] = None,
        allow_when_bot_off: bool = False,
        db_path: Optional[str] = None,
        detected_tier: Optional[str] = None,
        total_spent_usd: Optional[float] = None,
        consecutive_ppv_ignored: Optional[int] = None,
        defer_state_commit: bool = False,
    ) -> Dict[str, Any]:
        """AI応答を待機カウントダウン辞書へ登録する"""
        ai_en = normalize_dm_message_text(engine_res.get("ai_en") or engine_res.get("ai_reply"))
        ai_ja = normalize_dm_message_text(engine_res.get("ai_ja") or engine_res.get("ai_reply_ja"))
        next_phase = str(engine_res.get("sales_phase") or sales_phase)
        selected_ppv = (
            engine_res.get("selected_ppv") or engine_res.get("recommended_ppv")
            if next_phase in ("3_Lead_Pitch", "4_Lock_Close", "4_Closing")
            else None
        )
        if selected_ppv and isinstance(selected_ppv, dict):
            safe_sel_path, sel_guard_err = resolve_customer_safe_ppv_media_path(self.char_id, selected_ppv)
            if safe_sel_path is None:
                logger.warning(
                    f"[{self.char_id}] Blocked unredacted/unsafe PPV attachment during reply scheduling: {sel_guard_err}"
                )
                selected_ppv = None
            else:
                selected_ppv = {**selected_ppv, "filename": os.path.basename(safe_sel_path)}
        delay_cfg = get_configured_reply_delay_settings(self.char_id, db_path=db_path)
        delay_calc = _compute_human_reply_delay_sec(
            fan_text=fan_message_en,
            settings=delay_cfg,
            ai_reply_text=ai_en,
            fast_mode=self.fast_mode,
        )
        read_delay = float(delay_calc["read_delay_sec"])
        typing_delay = float(delay_calc["typing_delay_sec"])
        if custom_delay_sec is not None:
            total = max(0.0, float(custom_delay_sec))
            if self.fast_mode:
                read_delay, typing_delay = total, 0.0
            else:
                typing_delay = min(typing_delay, total * 0.25)
                read_delay = max(0.0, total - typing_delay)
            delay_calc["final_delay_sec"] = round(read_delay + typing_delay, 2)

        dlog = engine_res.get("decision_log")
        if isinstance(dlog, dict):
            sr = dlog.setdefault("strategy_reasoning", {})
            sr["reply_delay_reasoning"] = {
                "fan_char_len": delay_calc["fan_char_len"],
                "short_msg_chars_threshold": delay_calc["short_msg_chars_threshold"],
                "long_msg_chars_threshold": delay_calc["long_msg_chars_threshold"],
                "length_ratio": delay_calc["length_ratio"],
                "base_delay_sec": delay_calc["base_delay_sec"],
                "final_delay_sec": delay_calc["final_delay_sec"],
                "summary_ja": (
                    f"受信文字数 {delay_calc['fan_char_len']}文字（基準 {delay_calc['short_msg_chars_threshold']}〜"
                    f"{delay_calc['long_msg_chars_threshold']}文字 / 分量比率 {int(delay_calc['length_ratio'] * 100)}%）"
                    f" → ベース {delay_calc['base_delay_sec']}秒 → 決定待機 {delay_calc['final_delay_sec']}秒"
                ),
            }
            if fan_id and not defer_state_commit:
                append_fan_decision_log(self.char_id, fan_id, dlog, db_path=db_path)

        logger.info(
            f"[{self.char_id}] Delay calc for @{fan_id}: fan_chars={delay_calc['fan_char_len']}, "
            f"ratio={delay_calc['length_ratio']}, base={delay_calc['base_delay_sec']}s -> final={delay_calc['final_delay_sec']}s"
        )

        now_ts = time.time()
        scheduled_ts = now_ts + read_delay + typing_delay
        sched_dt = datetime.fromtimestamp(scheduled_ts)
        entry: Dict[str, Any] = {
            "char_id": self.char_id,
            "fan_id": fan_id,
            "username": username,
            "fan_message_en": fan_message_en,
            "ai_en": ai_en,
            "ai_ja": ai_ja,
            "sales_phase": next_phase,
            "prompt_version": str(engine_res.get("prompt_version") or "v1.0-auto"),
            "selected_ppv": selected_ppv,
            "tip_hook": engine_res.get("tip_hook") if isinstance(engine_res.get("tip_hook"), dict) else {},
            "tip_tx_id": engine_res.get("tip_tx_id"),
            "decision_log": dlog,
            "psych_profile": engine_res.get("psych_profile") if isinstance(engine_res.get("psych_profile"), dict) else None,
            "is_low_priority": int(engine_res.get("is_low_priority") or 0),
            "detected_tier": detected_tier,
            "total_spent_usd": total_spent_usd,
            "consecutive_ppv_ignored": consecutive_ppv_ignored,
            "defer_state_commit": bool(defer_state_commit),
            "read_delay_sec": round(read_delay, 2),
            "typing_delay_sec": round(typing_delay, 2),
            "total_delay_sec": round(read_delay + typing_delay, 1),
            "created_ts": now_ts,
            "send_start_ts": now_ts + read_delay,
            "scheduled_ts": scheduled_ts,
            "scheduled_time_hms": sched_dt.strftime("%H:%M:%S"),
            "scheduled_at_iso": sched_dt.strftime("%Y-%m-%dT%H:%M:%S"),
            "stage": "waiting_read_delay",
            "retry_count": 0,
            "typing_done": False,
            "committed": False,
            "allow_when_bot_off": allow_when_bot_off,
            "cancel_event": threading.Event(),
            "db_path": db_path,
        }
        with self._lock:
            old_entry = self._pending_replies.get(fan_id)
            if old_entry is not None:
                old_entry["cancel_event"].set()
            self._pending_replies[fan_id] = entry
        self._wake_event.set()
        return entry

    def _dispatch_sales_engine_reply(
        self,
        fan_id: str,
        sales_phase: str,
        engine_res: Dict[str, Any],
        username: str = "",
        fan_message_en: str = "",
        page: Any = None,
        db_path: Optional[str] = None,
        async_schedule: bool = False,
        allow_when_bot_off: bool = False,
        detected_tier: Optional[str] = None,
        total_spent_usd: Optional[float] = None,
        consecutive_ppv_ignored: Optional[int] = None,
        defer_state_commit: bool = False,
    ) -> Dict[str, Any]:
        if int(engine_res.get("is_escalated") or 0) == 1:
            if defer_state_commit:
                upsert_fan_sales_state(
                    char_id=self.char_id,
                    fan_id=fan_id,
                    username=username or fan_id,
                    current_tier=detected_tier,
                    total_spent_usd=total_spent_usd,
                    sales_phase="0_Escalated",
                    is_escalated=1,
                    escalation_reason=str(engine_res.get("escalation_reason") or "Emergency stop"),
                    psych_profile_dict=engine_res.get("psych_profile") if isinstance(engine_res.get("psych_profile"), dict) else None,
                    db_path=db_path,
                )
                if isinstance(engine_res.get("decision_log"), dict):
                    append_fan_decision_log(self.char_id, fan_id, engine_res["decision_log"], db_path=db_path)
            logger.warning(
                f"[{self.char_id}] Emergency escalation triggered for @{fan_id}: "
                f"{engine_res.get('escalation_reason')}"
            )
            return {
                "ok": True,
                "action": "escalated_emergency_stop",
                "char_id": self.char_id,
                "fan_id": fan_id,
                "escalation_reason": engine_res.get("escalation_reason"),
            }

        entry = self._schedule_pending_reply(
            fan_id=fan_id,
            username=username or fan_id,
            fan_message_en=fan_message_en,
            sales_phase=sales_phase,
            engine_res=engine_res,
            allow_when_bot_off=allow_when_bot_off,
            db_path=db_path,
            detected_tier=detected_tier,
            total_spent_usd=total_spent_usd,
            consecutive_ppv_ignored=consecutive_ppv_ignored,
            defer_state_commit=defer_state_commit,
        )
        snap = self.get_pending_replies_snapshot(fan_id).get(fan_id)
        logger.info(
            f"[{self.char_id}] AI reply scheduled for @{fan_id} | phase={entry['sales_phase']} | "
            f"read_delay={entry['read_delay_sec']}s, typing={entry['typing_delay_sec']}s, target={entry['scheduled_time_hms']}"
        )
        if async_schedule:
            return {
                "ok": True,
                "action": "scheduled_countdown",
                "char_id": self.char_id,
                "fan_id": fan_id,
                "sales_phase": entry["sales_phase"],
                "pending_reply": snap,
            }

        if not self._wait_interruptible(float(entry["read_delay_sec"]), cancel_event=entry["cancel_event"]):
            with self._lock:
                if self._pending_replies.get(fan_id) is entry:
                    self._pending_replies.pop(fan_id, None)
            logger.info(f"[{self.char_id}] Aborted/cancelled during read_delay for @{fan_id}")
            return {"ok": False, "action": "aborted_during_read_delay", "char_id": self.char_id, "fan_id": fan_id}

        return self._execute_pending_reply_entry(entry, page=page)

    def _verify_message_already_sent_in_dom(self, page: Any, fan_id: str, message_en: str) -> bool:
        """
        送信タイムアウトや例外発生後のリトライ前に、直近の自分側バブル (my-message) に
        同一本文が既に反映済みか検証し、二重送信を防止する。
        """
        if page is None or not hasattr(page, "evaluate"):
            return False
        target_norm = normalize_dm_message_text(message_en).strip()
        if not target_norm:
            return False
        try:
            if not self._ensure_fan_conversation_open(page, fan_id):
                return False
            matched = page.evaluate(
                r"""(expectedText) => {
                    const norm = (s) => String(s || '').replace(/\r\n/g, '\n').replace(/\r/g, '\n').trim();
                    const expected = norm(expectedText);
                    if (!expected) return false;
                    const myMsgs = Array.from(document.querySelectorAll('app-group-message.my-message'));
                    const recentMine = myMsgs.slice(-3);
                    for (const el of recentMine) {
                        const txtEl = el.querySelector('.message-text');
                        const t = txtEl ? norm(txtEl.innerText || txtEl.textContent || '') : '';
                        if (t && t === expected) {
                            return true;
                        }
                    }
                    return false;
                }""",
                target_norm,
            )
            return bool(matched)
        except Exception:
            return False

    def _reschedule_pending_reply_retry(
        self,
        entry: Dict[str, Any],
        reason: str,
        delay_sec: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """ブラウザ未準備や一時的な送信失敗時に安全にバックオフ再スケジュールする（上限超過時はNone）"""
        fan_id = str(entry["fan_id"])
        cancel_ev: threading.Event = entry["cancel_event"]
        if cancel_ev.is_set() or self._stop_event.is_set():
            return None

        err_text = str(reason or self.last_error or "").lower()
        # DM権限拒否などリトライしても解決しない恒久エラーは即時終了
        if "dm permission restricted" in err_text:
            return None

        with self._lock:
            current_entry = self._pending_replies.get(fan_id)
            if current_entry is not None and current_entry is not entry:
                return None

            retries = int(entry.get("retry_count") or 0) + 1
            max_retries = max(1, int(self._max_send_retries))
            if retries > max_retries:
                if self._pending_replies.get(fan_id) is entry:
                    self._pending_replies.pop(fan_id, None)
                return None

            base_wait = (
                float(delay_sec)
                if delay_sec is not None
                else (0.2 if self.fast_mode else float(self._retry_backoff_base_sec) * retries)
            )
            next_ts = time.time() + max(0.05, base_wait)
            sched_dt = datetime.fromtimestamp(next_ts)
            entry["retry_count"] = retries
            entry["typing_done"] = True
            entry["typing_delay_sec"] = 0.0
            entry["send_start_ts"] = next_ts
            entry["scheduled_ts"] = next_ts
            entry["scheduled_time_hms"] = sched_dt.strftime("%H:%M:%S")
            entry["scheduled_at_iso"] = sched_dt.strftime("%Y-%m-%dT%H:%M:%S")
            entry["stage"] = f"retrying_{retries}"
            self._pending_replies[fan_id] = entry

        self._wake_event.set()
        logger.warning(
            f"[{self.char_id}] Scheduled send retry {retries}/{max_retries} for @{fan_id} "
            f"in {base_wait:.1f}s (reason: {reason})"
        )
        return {
            "ok": False,
            "action": "retry_scheduled",
            "retry_count": retries,
            "next_send_ts": next_ts,
            "char_id": self.char_id,
            "fan_id": fan_id,
            "error": reason,
        }

    def _execute_pending_reply_entry(self, entry: Dict[str, Any], page: Any = None) -> Dict[str, Any]:
        """カウントダウン満了後の自動返信送信・DBコミットを実行する"""
        fan_id = str(entry["fan_id"])
        cancel_ev: threading.Event = entry["cancel_event"]
        db_path = entry.get("db_path")

        if cancel_ev.is_set() or self._stop_event.is_set():
            with self._lock:
                if self._pending_replies.get(fan_id) is entry:
                    self._pending_replies.pop(fan_id, None)
            return {"ok": False, "action": "cancelled_to_manual", "char_id": self.char_id, "fan_id": fan_id}

        with self._lock:
            if bool(entry.get("committed")):
                if self._pending_replies.get(fan_id) is entry:
                    self._pending_replies.pop(fan_id, None)
                return {"ok": True, "action": "already_committed", "char_id": self.char_id, "fan_id": fan_id}
            active_cur = self._pending_replies.get(fan_id)
            if active_cur is not None and active_cur is not entry:
                return {"ok": False, "action": "superseded", "char_id": self.char_id, "fan_id": fan_id}

        fan_now = get_fan_record(self.char_id, fan_id, db_path=db_path) or {}
        if (
            int(fan_now.get("manual_hold") or 0) == 1
            or int(fan_now.get("is_muted") or 0) == 1
            or int(fan_now.get("is_escalated") or 0) == 1
        ):
            with self._lock:
                if self._pending_replies.get(fan_id) is entry:
                    self._pending_replies.pop(fan_id, None)
            return {"ok": False, "action": "skipped_manual_or_locked", "char_id": self.char_id, "fan_id": fan_id}

        ai_en = entry["ai_en"]
        ai_ja = entry["ai_ja"]
        next_phase = entry["sales_phase"]
        selected_ppv = entry.get("selected_ppv")
        if selected_ppv and isinstance(selected_ppv, dict):
            safe_exec_path, exec_guard_err = resolve_customer_safe_ppv_media_path(self.char_id, selected_ppv)
            if safe_exec_path is None:
                logger.error(
                    f"[{self.char_id}] Guard blocked PPV delivery to @{fan_id} at send time: {exec_guard_err}"
                )
                selected_ppv = None
                entry["selected_ppv"] = None
            else:
                selected_ppv = {**selected_ppv, "filename": os.path.basename(safe_exec_path)}
                entry["selected_ppv"] = selected_ppv
        read_delay = float(entry.get("read_delay_sec") or 0.0)
        typing_delay = 0.0 if entry.get("typing_done") else float(entry.get("typing_delay_sec") or 0.0)

        # 実運用モード (fast_mode == False) でブラウザページが未初期化／再起動中／閉鎖中の場合は
        # サイレントに成功扱いして破棄せず、ブラウザ復旧まで待機リトライとして保持する
        effective_page = page if page is not None else self._active_page
        if not self.fast_mode:
            page_unavailable = effective_page is None
            if not page_unavailable and hasattr(effective_page, "is_closed") and callable(effective_page.is_closed):
                try:
                    page_unavailable = bool(effective_page.is_closed())
                except Exception:
                    page_unavailable = True
            if page_unavailable:
                retry_res = self._reschedule_pending_reply_retry(
                    entry,
                    reason="Playwright browser page is not ready/recovering",
                )
                if retry_res is not None:
                    return retry_res
                return {
                    "ok": False,
                    "action": "send_failed_browser_unavailable",
                    "char_id": self.char_id,
                    "fan_id": fan_id,
                    "error": self.last_error or "Browser page unavailable",
                }

        with self._lock:
            entry["stage"] = "sending_typing"

        already_sent_in_dom = False
        with self._page_lock:
            self._in_busy_op = True
            try:
                self.touch_heartbeat()
                if int(entry.get("retry_count") or 0) > 0 and effective_page is not None and not selected_ppv:
                    if self._verify_message_already_sent_in_dom(effective_page, fan_id, ai_en):
                        logger.info(
                            f"[{self.char_id}] Verified previous attempt already delivered DM to @{fan_id}; "
                            "skipping duplicate click and committing DB"
                        )
                        already_sent_in_dom = True

                if already_sent_in_dom:
                    sent_ok = True
                else:
                    sent_ok = self._send_via_playwright_page(
                        page=effective_page,
                        fan_id=fan_id,
                        message_en=ai_en,
                        typing_delay_sec=typing_delay,
                        selected_ppv=selected_ppv,
                        cancel_event=cancel_ev,
                        retry_count=int(entry.get("retry_count") or 0),
                    )
                    if not sent_ok and effective_page is not None and not cancel_ev.is_set() and not selected_ppv:
                        if self._verify_message_already_sent_in_dom(effective_page, fan_id, ai_en):
                            logger.info(
                                f"[{self.char_id}] Post-error check confirmed DM was delivered in DOM for @{fan_id}"
                            )
                            sent_ok = True
            finally:
                self._in_busy_op = False
                self.touch_heartbeat()

        if cancel_ev.is_set() or self._stop_event.is_set():
            with self._lock:
                if self._pending_replies.get(fan_id) is entry:
                    self._pending_replies.pop(fan_id, None)
            return {"ok": False, "action": "cancelled_during_typing", "char_id": self.char_id, "fan_id": fan_id}

        if effective_page is not None and not sent_ok:
            retry_res = self._reschedule_pending_reply_retry(
                entry,
                reason=self.last_error or "Playwright send returned False",
            )
            if retry_res is not None:
                return retry_res
            with self._lock:
                if self._pending_replies.get(fan_id) is entry:
                    self._pending_replies.pop(fan_id, None)
                err_low = str(self.last_error or "").lower()
                cooldown_sec = 1800.0 if "dm permission restricted" in err_low else (1.0 if self.fast_mode else 90.0)
                self._unanswered_recheck_after_ts[fan_id] = time.time() + cooldown_sec
                # 次回クールダウン満了時にスレッド再検査されるよう既読プレビューキャッシュを外す
                self._checked_read_previews.pop(fan_id, None)
            logger.warning(
                f"[{self.char_id}] Browser send failed after retries for @{fan_id}; "
                f"skipping DB ai message commit (recheck cooldown={cooldown_sec:.0f}s)"
            )
            return {
                "ok": False,
                "action": "send_failed",
                "char_id": self.char_id,
                "fan_id": fan_id,
                "error": self.last_error,
            }

        with self._lock:
            if bool(entry.get("committed")):
                if self._pending_replies.get(fan_id) is entry:
                    self._pending_replies.pop(fan_id, None)
                return {"ok": True, "action": "already_committed", "char_id": self.char_id, "fan_id": fan_id}
            entry["committed"] = True
            if self._pending_replies.get(fan_id) is entry:
                self._pending_replies.pop(fan_id, None)
            self._unanswered_recheck_after_ts.pop(fan_id, None)

        if bool(entry.get("defer_state_commit")):
            dlog_to_save = entry.get("decision_log")
            updated_fan = upsert_fan_sales_state(
                char_id=self.char_id,
                fan_id=fan_id,
                username=str(entry.get("username") or fan_id),
                current_tier=entry.get("detected_tier"),
                total_spent_usd=entry.get("total_spent_usd"),
                sales_phase=next_phase,
                consecutive_unanswered=0,
                consecutive_ppv_ignored=entry.get("consecutive_ppv_ignored"),
                is_low_priority=int(entry.get("is_low_priority") or 0),
                is_escalated=0,
                escalation_reason=None,
                psych_profile_dict=entry.get("psych_profile") if isinstance(entry.get("psych_profile"), dict) else None,
                db_path=db_path,
            )
            if isinstance(dlog_to_save, dict):
                if updated_fan.get("ab_group"):
                    sr = dlog_to_save.setdefault("strategy_reasoning", {})
                    ab_rule = sr.setdefault("ab_rule_applied", {})
                    if isinstance(ab_rule, dict):
                        ab_rule["ab_group"] = str(updated_fan["ab_group"])
                append_fan_decision_log(self.char_id, fan_id, dlog_to_save, db_path=db_path)

        tip_hook = entry.get("tip_hook") if isinstance(entry.get("tip_hook"), dict) else {}
        tx_id = entry.get("tip_tx_id") or tip_hook.get("tx_id")
        sent_ppv_id: Optional[str] = None
        sent_ppv_price: Optional[float] = None

        if selected_ppv and isinstance(selected_ppv, dict):
            sent_ppv_id = str(selected_ppv.get("ppv_id") or "").strip() or None
            offered_price = float(selected_ppv.get("offered_price_usd") or selected_ppv.get("base_price_usd") or 9.99)
            sent_ppv_price = offered_price
            tx_id = record_ppv_offer(
                char_id=self.char_id,
                fan_id=fan_id,
                ppv_id=sent_ppv_id,
                offered_price=offered_price,
                tx_type="ppv",
                db_path=db_path,
            )
            with self._lock:
                self.sent_ppv_count += 1
        elif tip_hook.get("triggered") and not tx_id and sales_engine.verify_tip_request_in_reply(ai_en):
            tx_id = record_ppv_offer(
                char_id=self.char_id,
                fan_id=fan_id,
                ppv_id=None,
                offered_price=float(tip_hook.get("suggested_amount_usd") or 5.0),
                tx_type="tip",
                db_path=db_path,
            )

        prompt_ver = str(entry.get("prompt_version") or "v1.0-auto")
        add_dm_message(
            char_id=self.char_id,
            fan_id=fan_id,
            sender="ai",
            content=ai_en,
            sales_phase=next_phase,
            prompt_version=prompt_ver,
            content_ja=ai_ja,
            decision_log=entry.get("decision_log"),
            ppv_id=sent_ppv_id,
            ppv_price_usd=sent_ppv_price,
            tx_id=int(tx_id) if (sent_ppv_id and tx_id) else None,
            db_path=db_path,
        )

        fan_msg_fp = str(entry.get("fan_message_en") or "").strip()
        if fan_msg_fp:
            self._last_replied_fingerprints[fan_id] = fan_msg_fp

        with self._lock:
            self.processed_messages_count += 1
            self.sent_history.append(
                {
                    "fan_id": fan_id,
                    "ai_en": ai_en,
                    "ai_ja": ai_ja,
                    "sales_phase": next_phase,
                    "selected_ppv": selected_ppv,
                    "tx_id": tx_id,
                    "read_delay_sec": read_delay,
                    "typing_delay_sec": typing_delay,
                    "sent_ok": sent_ok,
                }
            )

        return {
            "ok": True,
            "action": "replied",
            "char_id": self.char_id,
            "fan_id": fan_id,
            "sales_phase": next_phase,
            "ai_en": ai_en,
            "ai_ja": ai_ja,
            "selected_ppv": selected_ppv,
            "tx_id": tx_id,
            "read_delay_sec": read_delay,
            "typing_delay_sec": typing_delay,
        }

    def _process_due_pending_replies(self, page: Any = None, now_dt: Optional[datetime] = None) -> int:
        """送信開始時刻 (send_start_ts) に達した待機中AI返信を順次送信する"""
        sleeping, _ = self.is_sleeping(now_dt=now_dt)
        if sleeping:
            return 0
        bot_enabled = bool(load_settings(self.char_id).get("bot_enabled", False))
        now_ts = time.time()
        with self._lock:
            due_entries = [
                e
                for e in self._pending_replies.values()
                if not e["cancel_event"].is_set()
                and now_ts >= float(e["send_start_ts"])
                and (bot_enabled or bool(e.get("allow_when_bot_off")))
            ]
        sent_cnt = 0
        for entry in due_entries:
            if self._stop_event.is_set():
                break
            res = self._execute_pending_reply_entry(entry, page=page)
            if res.get("ok"):
                sent_cnt += 1
        return sent_cnt

    def _ensure_fan_conversation_open(self, page: Any, fan_id: str) -> bool:
        """対象 fan_id (@handle) の会話スレッドが開かれていなければ開く"""
        if page is None or not fan_id or not hasattr(page, "evaluate"):
            return True
        try:
            current_handle = page.evaluate(
                """() => {
                    const a = document.querySelector('.message-content-header app-account-username a.username-wrapper, .message-content-header-avatar a[href^="/"]');
                    return a ? (a.getAttribute('href') || '').replace(/^\\/+/, '').split(/[/?#]/)[0].trim() : '';
                }"""
            )
            if current_handle and current_handle.lower() == fan_id.lower():
                return True

            group_href = self._fan_group_hrefs.get(fan_id)
            if not group_href:
                for k, v in self._fan_group_hrefs.items():
                    if k.lower() == fan_id.lower():
                        group_href = v
                        break

            if group_href:
                target_url = f"https://fansly.com{group_href}" if group_href.startswith("/") else group_href
                expected_gid = group_href.replace("/messages/", "").split("?")[0].split("#")[0].strip("/")
                cur_url = str(getattr(page, "url", "") or "")
                if expected_gid and f"/messages/{expected_gid}" in cur_url:
                    return True
                logger.info(f"[{self.char_id}] Navigating to thread for @{fan_id}: {target_url}")
                page.goto(target_url, wait_until="domcontentloaded", timeout=20000)
                if hasattr(page, "wait_for_selector"):
                    page.wait_for_selector("app-messages-conversation-route, app-group-message-input", timeout=15000)
                if hasattr(page, "wait_for_timeout"):
                    page.wait_for_timeout(1000)
                return True

            if "fansly.com/messages" not in str(getattr(page, "url", "") or ""):
                page.goto(FANSLY_MESSAGES_URL, wait_until="domcontentloaded", timeout=20000)
                self._wait_for_messages_spa_ready(page)
            elif hasattr(page, "wait_for_selector"):
                try:
                    page.wait_for_selector("app-messaging-group", timeout=6000)
                except Exception:
                    pass

            clicked_href = page.evaluate(
                """(targetFanId) => {
                    const targetLower = String(targetFanId || '').toLowerCase();
                    const anchors = Array.from(document.querySelectorAll('.message-list a[href^="/messages/"]'));
                    for (const a of anchors) {
                        const href = a.getAttribute('href') || '';
                        const gid = href.replace(/^\\/messages\\//, '').split(/[?#]/)[0].trim().toLowerCase();
                        const u = a.querySelector('app-account-username a.username-wrapper, app-account-avatar a[href^="/"]');
                        const handle = u ? (u.getAttribute('href') || '').replace(/^\\/+/, '').split(/[/?#]/)[0].trim().toLowerCase() : '';
                        if ((handle && handle === targetLower) || (gid && gid === targetLower)) {
                            const clickEl = a.querySelector('.eclipse') || a.querySelector('.message-time') || a;
                            clickEl.click();
                            return href;
                        }
                    }
                    return '';
                }""",
                fan_id,
            )
            if clicked_href:
                self._fan_group_hrefs[fan_id] = clicked_href
                if hasattr(page, "wait_for_selector"):
                    page.wait_for_selector("app-messages-conversation-route, app-group-message-input", timeout=15000)
                if hasattr(page, "wait_for_timeout"):
                    page.wait_for_timeout(1000)
                return True

            logger.warning(f"[{self.char_id}] Could not find thread in message list for @{fan_id}")
            return False
        except Exception as e:
            logger.warning(f"[{self.char_id}] _ensure_fan_conversation_open failed for @{fan_id}: {e}")
            return False

    def resolve_ppv_blur_preview_path(
        self,
        media_path: str,
        selected_ppv: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """
        送信対象PPV（画像・動画ともに対応）に紐づく全画面ぼかしプレビュー画像の絶対パスを解決する。
        ぼかし画像が存在しない場合は None を返し、従来フローへ安全にフォールバックさせる。
        """
        if isinstance(selected_ppv, dict):
            explicit_abs = str(selected_ppv.get("blur_preview_path") or "").strip()
            if explicit_abs and os.path.isfile(explicit_abs) and os.path.getsize(explicit_abs) > 0:
                return os.path.abspath(explicit_abs)

            blur_rel = str(selected_ppv.get("blur_preview_filename") or "").replace("\\", "/").strip().lstrip("/")
            if blur_rel:
                for base_dir in (
                    self.vault_dir,
                    os.path.dirname(os.path.abspath(media_path)) if media_path else "",
                ):
                    if not base_dir:
                        continue
                    cand = os.path.join(base_dir, *blur_rel.split("/"))
                    if os.path.isfile(cand) and os.path.getsize(cand) > 0:
                        return os.path.abspath(cand)

        if media_path:
            resolved = resolve_existing_blur_preview_path(media_path)
            if resolved and os.path.isfile(resolved) and os.path.getsize(resolved) > 0:
                return os.path.abspath(resolved)
        return None

    def _cleanup_lingering_modals(self, page: Any, clear_composer_attachments: bool = False) -> None:
        """
        前回試行のタイムアウトや中断で残存しているサブモーダル・メインアップロードモーダル
        (app-media-editor / app-balance-input-modal / app-account-media-upload-v2 / .xdModal.back-drop)
        を内側から順に安全に閉じ、次回リトライや巡回が遮蔽されないクリーンな状態に戻す。
        """
        if page is None or not hasattr(page, "evaluate"):
            return
        try:
            for _ in range(4):
                modal_state = page.evaluate(
                    """() => {
                        const clickFirst = (selectors) => {
                            for (const sel of selectors) {
                                const el = document.querySelector(sel);
                                if (el && (el.offsetParent !== null || getComputedStyle(el).display !== 'none')) {
                                    el.click();
                                    return sel;
                                }
                            }
                            return '';
                        };
                        if (document.querySelector('app-media-editor')) {
                            return clickFirst([
                                'app-media-editor .modal-footer button.btn.v2.text',
                                'app-media-editor .editor-top button[aria-label="Close"]',
                                'app-media-editor button[aria-label="Close"]'
                            ]) || 'escape_editor';
                        }
                        if (document.querySelector('app-balance-input-modal')) {
                            return clickFirst([
                                'app-balance-input-modal .modal-footer button.btn.v2.text',
                                'app-balance-input-modal button[aria-label="Close"]',
                                'app-balance-input-modal .close'
                            ]) || 'escape_balance';
                        }
                        if (document.querySelector('app-account-media-upload-v2')) {
                            return clickFirst([
                                'app-account-media-upload-v2 .modal-footer button.btn.v2.text',
                                'app-account-media-upload-v2 .upload-top button[aria-label="Close"]',
                                'app-account-media-upload-v2 button[aria-label="Close"]'
                            ]) || 'escape_upload';
                        }
                        return '';
                    }"""
                )
                if not modal_state:
                    break
                if modal_state.startswith("escape_") and hasattr(page, "keyboard"):
                    try:
                        page.keyboard.press("Escape")
                    except Exception:
                        pass
                if hasattr(page, "wait_for_timeout"):
                    page.wait_for_timeout(450)

            if clear_composer_attachments:
                page.evaluate(
                    """() => {
                        const rmBtns = Array.from(
                            document.querySelectorAll(
                                'app-group-message-input button[aria-label*="Remove" i], ' +
                                'app-group-message-input .attachment-remove, ' +
                                'app-group-message-input .remove-attachment'
                            )
                        );
                        for (const b of rmBtns) {
                            try { b.click(); } catch (e) {}
                        }
                    }"""
                )
                if hasattr(page, "wait_for_timeout"):
                    page.wait_for_timeout(250)
        except Exception as clean_err:
            logger.debug(f"[{self.char_id}] _cleanup_lingering_modals info: {clean_err}")

    def _confirm_or_close_media_editor_modal(self, page: Any, media_basename: str = "") -> bool:
        """
        Fansly にてプレビュー用静止画をアップロードした直後に自動で開く画像エディタモーダル
        (app-media-editor: 'Edit image') を検知し、キャンバス初期化完了後に 'Done' ボタンを
        クリックして確定クローズする。
        これが開いたままだと背後の app-account-media-upload-v2 が .inactive-modal となり、
        .xdModal.back-drop / .spinner が Attach ボタンへのクリックを遮蔽するため必須。
        """
        if page is None or not hasattr(page, "wait_for_selector"):
            return False

        editor_appeared = False
        try:
            page.wait_for_selector("app-media-editor, .modal.image-editor", timeout=3500)
            editor_appeared = True
        except Exception:
            editor_appeared = _query_first(page, FANSLY_SELECTORS["media_editor_modal"]) is not None

        if not editor_appeared:
            return False

        logger.info(f"[{self.char_id}] Media editor modal detected for {media_basename}; waiting for canvas & clicking Done...")
        # Fabric キャンバス初期化スピナーが消えるか Done ボタンが有効化されるまで待機（最大6秒）
        if hasattr(page, "evaluate"):
            for _ in range(20):
                try:
                    ready = page.evaluate(
                        """() => {
                            const ed = document.querySelector('app-media-editor');
                            if (!ed) return true;
                            const doneBtn = ed.querySelector('.modal-footer button.solid-blue, button.solid-blue');
                            if (!doneBtn || doneBtn.hasAttribute('disabled')) return false;
                            const sp = ed.querySelector('.canvas-wrapper .spinner');
                            const spVisible = sp && sp.offsetParent !== null && getComputedStyle(sp).display !== 'none' && getComputedStyle(sp).visibility !== 'hidden' && parseFloat(getComputedStyle(sp).opacity || '1') > 0.05;
                            const hasCanvas = !!ed.querySelector('canvas.upper-canvas, canvas.lower-canvas, canvas');
                            return hasCanvas || !spVisible;
                        }"""
                    )
                    if ready:
                        break
                except Exception:
                    break
                page.wait_for_timeout(300)

        page.wait_for_timeout(350)
        clicked_done = False
        if hasattr(page, "locator"):
            done_loc = page.locator(", ".join(FANSLY_SELECTORS["media_editor_done"])).locator("visible=true").first
            if done_loc.count() > 0:
                try:
                    done_loc.click(timeout=6000)
                    clicked_done = True
                except Exception:
                    clicked_done = False

        if not clicked_done and hasattr(page, "evaluate"):
            clicked_done = bool(
                page.evaluate(
                    """() => {
                        const ed = document.querySelector('app-media-editor');
                        if (!ed) return true;
                        const btns = Array.from(ed.querySelectorAll('.modal-footer button.solid-blue, button.solid-blue, .modal-footer button'));
                        const target = btns.find(b => /done|save|apply|完了|保存/i.test((b.innerText || b.textContent || '').trim())) || btns[0];
                        if (target && !target.hasAttribute('disabled')) {
                            target.click();
                            return true;
                        }
                        return false;
                    }"""
                )
            )

        try:
            page.wait_for_selector("app-media-editor", state="detached", timeout=10000)
        except Exception:
            logger.warning(f"[{self.char_id}] app-media-editor did not detach after Done; cancelling editor overlay to unblock Attach")
            if hasattr(page, "evaluate"):
                page.evaluate(
                    """() => {
                        const cancelBtn = document.querySelector(
                            'app-media-editor .modal-footer button.btn.v2.text, app-media-editor .editor-top button[aria-label="Close"]'
                        );
                        if (cancelBtn) cancelBtn.click();
                    }"""
                )
            try:
                page.wait_for_selector("app-media-editor", state="detached", timeout=5000)
            except Exception:
                pass
            return False

        page.wait_for_timeout(450)
        return True

    def _attach_blur_preview_in_upload_modal(
        self,
        page: Any,
        media_path: str,
        blur_preview_path: str,
    ) -> bool:
        """
        Fansly Media Upload v2 モーダル (app-account-media-upload-v2) 上で、
        画像PPV・動画PPVのいずれに対しても対象メディアタイルを選択し、
        プレビュー枠 (+ Add free preview / .upload-stage-badge) へぼかしプレビュー画像を自動アップロード・紐付けする。
        """
        if not blur_preview_path or not os.path.isfile(blur_preview_path) or os.path.getsize(blur_preview_path) <= 0:
            return False

        blur_basename = os.path.basename(blur_preview_path)
        media_basename = os.path.basename(media_path)

        # 単体テスト等のモック Page 対応
        if not hasattr(page, "wait_for_selector"):
            badge_el = _query_first(page, FANSLY_SELECTORS["upload_preview_badge"])
            if badge_el is not None and hasattr(badge_el, "click"):
                badge_el.click()
            prev_inp = _query_first(page, FANSLY_SELECTORS["upload_preview_file_input"])
            if prev_inp is not None and hasattr(prev_inp, "set_input_files"):
                prev_inp.set_input_files(blur_preview_path)
                editor_done_mock = _query_first(page, FANSLY_SELECTORS["media_editor_done"])
                if editor_done_mock is not None and hasattr(editor_done_mock, "click"):
                    editor_done_mock.click()
                logger.info(
                    f"[{self.char_id}] Attached blurred preview (mock) for {media_basename}: {blur_basename}"
                )
                return True
            return False

        try:
            # 1. モーダル内の対象メディアタイル（画像・動画共通、追加ボタン .upload-tile-add 除外）を確実に選択状態にする
            if hasattr(page, "evaluate"):
                page.evaluate(
                    """() => {
                        const modal = document.querySelector('app-account-media-upload-v2, .modal.upload-v2');
                        if (!modal) return false;
                        const tiles = Array.from(
                            modal.querySelectorAll(
                                '.upload-rail .fansly-media-tile.upload-tile:not(.upload-tile-add), ' +
                                '.fansly-media-tile.upload-tile:not(.upload-tile-add)'
                            )
                        );
                        if (tiles.length === 0) return false;
                        const hasActive = tiles.some(
                            t => t.classList.contains('selected')
                                || t.classList.contains('active')
                                || t.getAttribute('aria-selected') === 'true'
                        );
                        if (!hasActive) {
                            tiles[0].click();
                        }
                        return true;
                    }"""
                )
                page.wait_for_timeout(350)

            # 2. ステージ上の「+ Add free preview / + 無料プレビューを追加」(.upload-stage-badge) を開く
            opened_dropdown = False
            if hasattr(page, "locator"):
                add_preview_btn = page.locator(
                    "app-account-media-upload-v2 .upload-stage-media .upload-stage-badge, "
                    "app-account-media-upload-v2 .upload-stage-badge"
                ).locator("visible=true").first
                if add_preview_btn.count() == 0:
                    add_preview_btn = page.locator(
                        "app-account-media-upload-v2 >> text=/Add free preview|無料プレビューを追加/i"
                    ).locator("visible=true").first
                try:
                    add_preview_btn.wait_for(state="visible", timeout=5000)
                    badge_cls = add_preview_btn.get_attribute("class") or ""
                    if "dropdown-open" not in badge_cls:
                        add_preview_btn.click()
                        page.wait_for_timeout(500)
                    opened_dropdown = True
                except Exception:
                    opened_dropdown = False
            else:
                badge_el = _query_first(page, FANSLY_SELECTORS["upload_preview_badge"])
                if badge_el is not None:
                    badge_el.click()
                    page.wait_for_timeout(500)
                    opened_dropdown = True

            if not opened_dropdown:
                logger.warning(
                    f"[{self.char_id}] Free preview badge (.upload-stage-badge) not found for {media_basename}; "
                    "continuing with standard locked PPV flow"
                )
                return False

            # 3. ドロップダウン内の Upload 項目から FileChooser 経由（失敗時はプレビュー専用 input）でぼかし画像をセット
            uploaded = False
            if hasattr(page, "locator") and hasattr(page, "expect_file_chooser"):
                upload_opt = page.locator(
                    ", ".join(FANSLY_SELECTORS["upload_preview_dropdown_upload"])
                ).locator("visible=true").first
                if upload_opt.count() > 0:
                    try:
                        with page.expect_file_chooser(timeout=5000) as fc_info:
                            upload_opt.click()
                        fc_info.value.set_files(blur_preview_path)
                        uploaded = True
                    except Exception as fc_err:
                        logger.warning(
                            f"[{self.char_id}] Preview FileChooser fallback triggered for {media_basename}: {fc_err}"
                        )

            if not uploaded and hasattr(page, "locator"):
                preview_input = page.locator(
                    "app-account-media-upload-v2 input[type='file']:not([multiple])"
                ).first
                if preview_input.count() > 0:
                    preview_input.set_input_files(blur_preview_path)
                    uploaded = True
                else:
                    all_inputs = page.locator("app-account-media-upload-v2 input[type='file']")
                    if all_inputs.count() >= 2:
                        all_inputs.last.set_input_files(blur_preview_path)
                        uploaded = True
            elif not uploaded:
                prev_inp = _query_first(page, FANSLY_SELECTORS["upload_preview_file_input"])
                if prev_inp is not None:
                    prev_inp.set_input_files(blur_preview_path)
                    uploaded = True

            if not uploaded:
                logger.warning(
                    f"[{self.char_id}] Could not locate preview upload target for {media_basename}; "
                    "continuing without blurred preview"
                )
                return False

            # 3.5. 静止画プレビュー選択時に自動起動する Edit image サブモーダル (app-media-editor) を確定クローズする
            self._confirm_or_close_media_editor_modal(page, media_basename=media_basename)

            # 4. ぼかしプレビューのアップロード反映・紐付け完了をDOM上で確認（画像PPV・動画PPV共通）
            preview_confirmed = False
            if hasattr(page, "evaluate"):
                for _ in range(24):
                    if _query_first(page, FANSLY_SELECTORS["media_editor_modal"]) is not None:
                        self._confirm_or_close_media_editor_modal(page, media_basename=media_basename)
                    page.wait_for_timeout(500)
                    dom_st = page.evaluate(
                        r"""() => {
                            if (document.querySelector('app-media-editor')) return { ready: false };
                            const modal = document.querySelector('app-account-media-upload-v2, .modal.upload-v2');
                            if (!modal || modal.classList.contains('inactive-modal')) return { ready: false };
                            const hasSpinner = !!modal.querySelector('.fa-spinner, .fa-spin, mat-spinner');
                            const hasOkBadge = !!modal.querySelector(
                                '.fansly-media-badge.ok, app-icon[name="eye"], i.fa-eye:not(.fa-eye-slash)'
                            );
                            const hasSwitch = !!modal.querySelector('.upload-stage-switch, .upload-stage-replace');
                            const actionsEl = modal.querySelector('.upload-stage-actions');
                            const actionsTxt = actionsEl ? (actionsEl.innerText || actionsEl.textContent || '') : '';
                            const hasRemoveTxt = /remove\s+preview|プレビューを削除/i.test(actionsTxt);
                            return {
                                ready: !hasSpinner && (hasOkBadge || hasSwitch || hasRemoveTxt)
                            };
                        }"""
                    )
                    if isinstance(dom_st, dict) and dom_st.get("ready"):
                        preview_confirmed = True
                        break
            else:
                page.wait_for_timeout(800)
                preview_confirmed = True

            logger.info(
                f"[{self.char_id}] Blurred preview attached for {media_basename}: {blur_basename} "
                f"(confirmed={preview_confirmed})"
            )
            return preview_confirmed
        except Exception as prev_err:
            logger.warning(
                f"[{self.char_id}] Blurred preview attach warning for {media_basename} ({prev_err}); "
                "falling back to standard PPV attach"
            )
            if hasattr(page, "wait_for_selector"):
                try:
                    if _query_first(page, FANSLY_SELECTORS["media_editor_modal"]) is not None:
                        self._confirm_or_close_media_editor_modal(page, media_basename=media_basename)
                except Exception:
                    pass
            return False

    def _attach_ppv_with_price_modal(
        self,
        page: Any,
        media_path: str,
        offered_price: float,
        blur_preview_path: Optional[str] = None,
        selected_ppv: Optional[Dict[str, Any]] = None,
        skip_blur_preview: bool = False,
    ) -> bool:
        """
        Fansly Media Upload v2 + Set price モーダルでローカルPPVメディア（画像・動画）の添付、
        価格ロック設定、およびぼかしプレビュー画像の自動アップロード・紐付けを行う。
        ぼかしプレビュー画像が存在しない場合や skip_blur_preview=True 時は従来フローへ安全にフォールバックする。
        """
        # 前回リトライ等で残存しているモーダルがあれば事前に完全消去する
        self._cleanup_lingering_modals(page, clear_composer_attachments=True)

        file_input = _query_first(page, FANSLY_SELECTORS["file_input"])
        if file_input is None and hasattr(page, "wait_for_selector"):
            try:
                page.wait_for_selector(
                    "app-group-message-input input[type='file'], input[type='file']",
                    state="attached",
                    timeout=8000,
                )
                file_input = _query_first(page, FANSLY_SELECTORS["file_input"])
            except Exception:
                file_input = _query_first(page, FANSLY_SELECTORS["file_input"])

        if file_input is None:
            logger.warning(f"[{self.char_id}] PPV file_input DOM not found for {media_path}")
            return False

        effective_blur_path = None
        if not skip_blur_preview:
            effective_blur_path = (
                os.path.abspath(blur_preview_path)
                if (blur_preview_path and os.path.isfile(blur_preview_path) and os.path.getsize(blur_preview_path) > 0)
                else self.resolve_ppv_blur_preview_path(media_path, selected_ppv=selected_ppv)
            )

        logger.info(
            f"[{self.char_id}] Uploading PPV media: {os.path.basename(media_path)} (${offered_price:.2f}, "
            f"blur_preview={os.path.basename(effective_blur_path) if effective_blur_path else 'none/fallback'})"
        )
        file_input.set_input_files(media_path)

        if not hasattr(page, "wait_for_selector"):
            price_btn = _query_first(page, FANSLY_SELECTORS["ppv_price_button"])
            if price_btn is not None and offered_price > 0:
                price_btn.click()
                price_input = _query_first(page, FANSLY_SELECTORS["ppv_price_input"])
                if price_input is not None:
                    price_input.fill(f"{offered_price:.2f}")
                confirm_btn = _query_first(page, FANSLY_SELECTORS["ppv_price_confirm"])
                if confirm_btn is not None:
                    confirm_btn.click()
            blur_ok_mock = False
            if effective_blur_path:
                blur_ok_mock = self._attach_blur_preview_in_upload_modal(page, media_path, effective_blur_path)
            attach_btn_mock = _query_first(page, FANSLY_SELECTORS["upload_attach_confirm"])
            if attach_btn_mock is not None and hasattr(attach_btn_mock, "click"):
                attach_btn_mock.click()
            return True

        try:
            page.wait_for_selector("app-account-media-upload-v2", timeout=15000)
            page.wait_for_timeout(1000)

            if offered_price > 0:
                for rm_btn in page.query_selector_all("app-permission-rows-editor button.perm-cond-remove"):
                    try:
                        rm_btn.click()
                        page.wait_for_timeout(250)
                    except Exception:
                        pass

                add_chip = _query_first(page, FANSLY_SELECTORS["upload_perm_add_chip"])
                if add_chip is not None:
                    add_chip.click()
                    page.wait_for_timeout(400)

                clicked_pay = page.evaluate(
                    """() => {
                        const items = Array.from(document.querySelectorAll('app-permission-rows-editor .dropdown-item'));
                        const payItem = items.find(el => (el.textContent || '').trim().toLowerCase() === 'pay');
                        if (payItem) { payItem.click(); return true; }
                        return false;
                    }"""
                )
                if not clicked_pay:
                    logger.warning(f"[{self.char_id}] 'Pay' option not found in app-permission-rows-editor")
                    self._cleanup_lingering_modals(page, clear_composer_attachments=True)
                    return False

                page.wait_for_selector("app-balance-input-modal", timeout=10000)
                page.wait_for_timeout(400)
                price_input = _query_first(page, FANSLY_SELECTORS["ppv_price_input"])
                if price_input is None:
                    logger.warning(f"[{self.char_id}] Price input not found in app-balance-input-modal")
                    self._cleanup_lingering_modals(page, clear_composer_attachments=True)
                    return False

                price_input.fill(f"{offered_price:.2f}")
                try:
                    price_input.dispatch_event("input")
                    price_input.dispatch_event("change")
                except Exception:
                    pass
                page.wait_for_timeout(300)

                confirm_btn = _query_first(page, FANSLY_SELECTORS["ppv_price_confirm"])
                if confirm_btn is not None:
                    confirm_btn.click()
                else:
                    page.keyboard.press("Enter")
                page.wait_for_selector("app-balance-input-modal", state="detached", timeout=8000)
                page.wait_for_timeout(500)

            blur_attached_ok = False
            if effective_blur_path:
                blur_attached_ok = self._attach_blur_preview_in_upload_modal(page, media_path, effective_blur_path)
                page.wait_for_timeout(400)
            else:
                logger.info(
                    f"[{self.char_id}] No blurred preview used for {os.path.basename(media_path)}; "
                    "proceeding with standard PPV attach flow"
                )

            # Attach クリック前にサブモーダル (app-media-editor / app-balance-input-modal) が残っていないか最終保証
            if _query_first(page, FANSLY_SELECTORS["media_editor_modal"]) is not None:
                self._confirm_or_close_media_editor_modal(page, media_basename=os.path.basename(media_path))

            try:
                page.wait_for_selector(
                    "app-account-media-upload-v2:not(.inactive-modal) .modal-footer button.upload-primary:not([disabled]), "
                    "app-account-media-upload-v2 .modal-footer button.upload-primary:not([disabled])",
                    timeout=20000,
                )
            except Exception:
                pass

            attach_btn = _query_first(page, FANSLY_SELECTORS["upload_attach_confirm"])
            if attach_btn is None:
                logger.warning(f"[{self.char_id}] Attach button (button.upload-primary) not found")
                self._cleanup_lingering_modals(page, clear_composer_attachments=True)
                return False

            attach_btn.click()
            page.wait_for_selector("app-account-media-upload-v2", state="detached", timeout=60000)
            page.wait_for_timeout(800)
            logger.info(
                f"[{self.char_id}] PPV media attached OK (${offered_price:.2f}, "
                f"blur_preview_attached={blur_attached_ok})"
            )
            return True
        except Exception as attach_err:
            logger.warning(
                f"[{self.char_id}] PPV modal attach error for {os.path.basename(media_path)}: {attach_err}"
            )
            self._cleanup_lingering_modals(page, clear_composer_attachments=True)
            raise

    def _fill_chat_input_preserving_newlines(
        self,
        page: Any,
        chat_input: Any,
        message_en: str,
    ) -> bool:
        """
        Fansly の Angular チャット入力欄 (textarea.message-input) に対し、
        改行（'\\n'）が1行に潰れたり途中送信されたりしないよう確実に複数行入力・同期する。
        1) 行ごとに keyboard.insert_text(line) + Shift+Enter で実ブラウザの改行イベントを発火
        2) DOM の textarea.value を検証し、不一致時は HTMLTextAreaElement ネイティブ value セッター + InputEvent('input') で厳密同期
        """
        norm_text = normalize_dm_message_text(message_en)
        if not norm_text:
            return True

        if hasattr(chat_input, "click"):
            chat_input.click()

        keyboard = getattr(page, "keyboard", None)
        has_real_keyboard = (
            keyboard is not None
            and hasattr(keyboard, "insert_text")
            and hasattr(keyboard, "press")
        )

        if has_real_keyboard:
            # 既存入力値のクリア
            if hasattr(chat_input, "evaluate"):
                try:
                    chat_input.evaluate(
                        """(el) => {
                            el.focus();
                            const proto = window.HTMLTextAreaElement
                                ? window.HTMLTextAreaElement.prototype
                                : Object.getPrototypeOf(el);
                            const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                            if (desc && desc.set) {
                                desc.set.call(el, '');
                            } else {
                                el.value = '';
                            }
                            el.dispatchEvent(new InputEvent('input', { bubbles: true, cancelable: true, inputType: 'deleteContentBackward' }));
                        }"""
                    )
                except Exception:
                    pass

            lines = norm_text.split("\n")
            for idx, line in enumerate(lines):
                if line:
                    keyboard.insert_text(line)
                if idx < len(lines) - 1:
                    keyboard.press("Shift+Enter")
                    if hasattr(page, "wait_for_timeout"):
                        page.wait_for_timeout(35)

            # Angular FormControl / ngModel と実DOM value に改行が完全反映されているか検証・保証
            if hasattr(chat_input, "evaluate"):
                try:
                    chat_input.evaluate(
                        """(el, expectedText) => {
                            const current = String(el.value || '').replace(/\\r\\n/g, '\\n').replace(/\\r/g, '\\n');
                            if (current !== expectedText) {
                                const proto = window.HTMLTextAreaElement
                                    ? window.HTMLTextAreaElement.prototype
                                    : Object.getPrototypeOf(el);
                                const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                                if (desc && desc.set) {
                                    desc.set.call(el, expectedText);
                                } else {
                                    el.value = expectedText;
                                }
                                el.dispatchEvent(
                                    new InputEvent('input', {
                                        bubbles: true,
                                        cancelable: true,
                                        inputType: 'insertText',
                                        data: expectedText
                                    })
                                );
                                el.dispatchEvent(new Event('change', { bubbles: true }));
                            }
                        }""",
                        norm_text,
                    )
                except Exception:
                    pass
            return True

        # モック Page / フォールバック入力（生の Enter を含む keyboard.type(norm_text) の一括呼び出しは途中送信防止のため行わない）
        try:
            chat_input.fill(norm_text)
            if hasattr(chat_input, "dispatch_event"):
                chat_input.dispatch_event("input")
                chat_input.dispatch_event("change")
            return True
        except Exception:
            if keyboard is not None and hasattr(keyboard, "type") and hasattr(keyboard, "press"):
                lines = norm_text.split("\n")
                for idx, line in enumerate(lines):
                    if line:
                        keyboard.type(line)
                    if idx < len(lines) - 1:
                        keyboard.press("Shift+Enter")
                return True
            raise

    def _send_via_playwright_page(
        self,
        page: Any,
        fan_id: str,
        message_en: str,
        typing_delay_sec: float = 0.0,
        selected_ppv: Optional[Dict[str, Any]] = None,
        cancel_event: Optional[threading.Event] = None,
        retry_count: int = 0,
    ) -> bool:
        """
        Playwrightのページオブジェクトが存在する場合、
        1. ローカル ppv_vault/<filename> のアップロード・価格ロック設定・ぼかしプレビュー画像自動添付
        2. チャット入力欄へのフォーカス＆タイピング中演出（10〜30秒）
        3. 改行（'\\n'）を保持したテキスト入力＆送信ボタンクリック
        を安全に実行する。DOM未検出時はエラーログを記録し例外で落とさない。
        """
        if page is None:
            return True

        norm_message_en = normalize_dm_message_text(message_en)

        try:
            # 前回失敗時のモーダルオーバーレイが残存していれば先に閉じておく
            self._cleanup_lingering_modals(page, clear_composer_attachments=False)

            if not self._ensure_fan_conversation_open(page, fan_id):
                self.last_error = f"Target thread not found for @{fan_id}"
                return False

            if hasattr(page, "evaluate"):
                dm_blocked = page.evaluate(
                    """() => {
                        const noDm = document.querySelector('app-group-message-input .no-dm-allowed, app-dm-permission-flags-display');
                        const comp = document.querySelector('app-group-message-input .message-composer');
                        return !!(noDm || (comp && comp.classList.contains('hidden')));
                    }"""
                )
                if dm_blocked:
                    msg = f"DM permission restricted by @{fan_id} (follow/subscribe required)"
                    logger.warning(f"[{self.char_id}] {msg}")
                    self.last_error = msg
                    return False

            attached_ppv_ok = False
            if selected_ppv and isinstance(selected_ppv, dict):
                filename = str(selected_ppv.get("filename") or "").strip()
                offered_price = float(
                    selected_ppv.get("offered_price_usd") or selected_ppv.get("base_price_usd") or 9.99
                )
                safe_media_path, guard_err = resolve_customer_safe_ppv_media_path(self.char_id, selected_ppv)
                if safe_media_path is None:
                    msg = f"Unredacted/unsafe PPV blocked from customer send ({filename or selected_ppv.get('ppv_id')}): {guard_err}"
                    logger.error(f"[{self.char_id}] {msg}")
                    self.last_error = msg
                    self._cleanup_lingering_modals(page, clear_composer_attachments=True)
                    return False

                media_path = safe_media_path
                # リトライ後半 (retry_count >= 2) はぼかしプレビュー起因のDOM異常を回避するため標準ロックPPV添付へ安全フォールバック
                skip_blur = int(retry_count or 0) >= 2
                if not self._attach_ppv_with_price_modal(
                    page=page,
                    media_path=media_path,
                    offered_price=offered_price,
                    selected_ppv=selected_ppv,
                    skip_blur_preview=skip_blur,
                ):
                    logger.warning(f"[{self.char_id}] PPV attach failed for {os.path.basename(media_path)}; aborting send")
                    self._cleanup_lingering_modals(page, clear_composer_attachments=True)
                    return False
                attached_ppv_ok = True

            chat_input = _query_first(page, FANSLY_SELECTORS["chat_input"])
            if chat_input is None and hasattr(page, "wait_for_selector"):
                try:
                    page.wait_for_selector(
                        "app-group-message-input .message-composer:not(.hidden) textarea.message-input, "
                        "textarea.message-input",
                        timeout=8000,
                    )
                    chat_input = _query_first(page, FANSLY_SELECTORS["chat_input"])
                except Exception:
                    chat_input = _query_first(page, FANSLY_SELECTORS["chat_input"])
            if chat_input is None:
                msg = f"Chat input selector not found on page (fan=@{fan_id})"
                logger.warning(f"[{self.char_id}] {msg}")
                self.last_error = msg
                return False

            chat_input.click()
            if typing_delay_sec > 0:
                logger.info(f"[{self.char_id}] Simulating typing for @{fan_id} ({typing_delay_sec:.1f}s)...")
                if not self._wait_interruptible(typing_delay_sec, cancel_event=cancel_event):
                    logger.info(f"[{self.char_id}] Typing simulation aborted/cancelled for @{fan_id}")
                    return False

            if cancel_event is not None and cancel_event.is_set():
                return False

            # タイピング待機中のAngular再描画によるデタッチを防ぐため、入力直前に最新要素を再取得
            fresh_chat_input = _query_first(page, FANSLY_SELECTORS["chat_input"]) or chat_input
            if norm_message_en:
                self._fill_chat_input_preserving_newlines(page, fresh_chat_input, norm_message_en)

            if hasattr(page, "wait_for_timeout"):
                page.wait_for_timeout(400)

            if hasattr(page, "wait_for_selector"):
                try:
                    page.wait_for_selector(
                        "app-group-message-input button.send-button:not([disabled])",
                        timeout=20000 if attached_ppv_ok else 5000,
                    )
                except Exception:
                    pass

            enabled_send_btn = _query_first(page, ["app-group-message-input button.send-button:not([disabled])"])
            send_btn = enabled_send_btn or _query_first(page, FANSLY_SELECTORS["send_button"])
            if send_btn is not None:
                send_btn.click()
            else:
                page.keyboard.press("Enter")

            if hasattr(page, "wait_for_timeout"):
                page.wait_for_timeout(1500)

            # 実ブラウザ環境では送信後にチャット入力欄がクリアされたか確認し、残っていれば有効化された送信ボタンを再試行
            if norm_message_en and hasattr(page, "evaluate"):
                try:
                    still_in_box = page.evaluate(
                        """() => {
                            const ta = document.querySelector('app-group-message-input textarea.message-input, textarea.message-input');
                            return ta ? String(ta.value || '').trim().length > 0 : false;
                        }"""
                    )
                    if still_in_box:
                        retry_btn = _query_first(page, ["app-group-message-input button.send-button:not([disabled])"])
                        if retry_btn is not None:
                            retry_btn.click()
                            if hasattr(page, "wait_for_timeout"):
                                page.wait_for_timeout(1200)
                        still_after = page.evaluate(
                            """() => {
                                const ta = document.querySelector('app-group-message-input textarea.message-input, textarea.message-input');
                                return ta ? String(ta.value || '').trim().length > 0 : false;
                            }"""
                        )
                        if still_after and not self._verify_message_already_sent_in_dom(page, fan_id, norm_message_en):
                            msg = f"Message remained unsent in composer for @{fan_id} (send button disabled or network lag)"
                            logger.warning(f"[{self.char_id}] {msg}")
                            self.last_error = msg
                            return False
                except Exception:
                    pass

            logger.info(f"[{self.char_id}] Sent DM to @{fan_id}: '{norm_message_en[:80]}'")
            return True
        except Exception as e:
            err_msg = f"Playwright send failed for fan={fan_id}: {e}"
            logger.error(f"[{self.char_id}] {err_msg}")
            self.last_error = err_msg
            self._cleanup_lingering_modals(page, clear_composer_attachments=bool(selected_ppv))
            return False

    def _drain_outbox_queue(self, page: Any = None, now_dt: Optional[datetime] = None) -> int:
        """
        送信キュー (手動介入送信 human_manual や FollowupWorker 等の自動追撃) を消化する。
        - 手動介入送信 (source == 'human_manual') は稼働OFF時・睡眠時間中でも即座に送信する。
        - 自動追撃 (followup 等) は bot_enabled == True かつ非睡眠時間帯のみ送信する。
        """
        sleeping, _ = self.is_sleeping(now_dt=now_dt)
        bot_enabled = bool(load_settings(self.char_id).get("bot_enabled", False))
        effective_page = page if page is not None else self._active_page

        drained = 0
        deferred_items: List[Dict[str, Any]] = []
        now_ts = time.time()
        while not self._outbox_queue.empty() and not self._stop_event.is_set():
            try:
                item = self._outbox_queue.get_nowait()
            except queue.Empty:
                break

            source = str(item.get("source") or "followup")
            is_manual = source == "human_manual"
            if not is_manual and (sleeping or not bot_enabled):
                deferred_items.append(item)
                self._outbox_queue.task_done()
                continue

            next_attempt_ts = float(item.get("next_attempt_ts") or 0.0)
            if next_attempt_ts > now_ts:
                deferred_items.append(item)
                self._outbox_queue.task_done()
                continue

            fan_id = str(item.get("fan_id") or "").strip()
            # 同一ファンに対するAI自動返信カウントダウン中／送信中は自動追撃キューをぶつけず後回しにする
            if not is_manual and fan_id:
                with self._lock:
                    pend = self._pending_replies.get(fan_id)
                    has_active_pend = pend is not None and not pend["cancel_event"].is_set()
                if has_active_pend:
                    item["next_attempt_ts"] = time.time() + (0.2 if self.fast_mode else 15.0)
                    deferred_items.append(item)
                    self._outbox_queue.task_done()
                    continue

            if not self.fast_mode:
                page_unavailable = effective_page is None
                if not page_unavailable and hasattr(effective_page, "is_closed") and callable(effective_page.is_closed):
                    try:
                        page_unavailable = bool(effective_page.is_closed())
                    except Exception:
                        page_unavailable = True
                if page_unavailable:
                    retries = int(item.get("retry_count") or 0) + 1
                    if retries <= max(1, int(self._max_send_retries)):
                        item["retry_count"] = retries
                        item["next_attempt_ts"] = time.time() + (0.2 if self.fast_mode else 8.0 * retries)
                        deferred_items.append(item)
                    else:
                        logger.warning(
                            f"[{self.char_id}] Dropping outbox item ({source}) for @{fan_id}: browser unavailable after retries"
                        )
                    self._outbox_queue.task_done()
                    continue

            msg_en = str(item.get("message_en") or "").strip()
            raw_ppv = item.get("ppv_item") if isinstance(item.get("ppv_item"), dict) else None
            ppv_item = None
            if raw_ppv:
                safe_out_path, out_guard_err = resolve_customer_safe_ppv_media_path(self.char_id, raw_ppv)
                if safe_out_path is None:
                    logger.error(
                        f"[{self.char_id}] Dropping outbox PPV ({source}) for @{fan_id}: {out_guard_err}"
                    )
                    self.last_error = str(out_guard_err)
                    self._outbox_queue.task_done()
                    continue
                ppv_item = dict(raw_ppv)
                ppv_item["filename"] = os.path.basename(safe_out_path)
                if item.get("offered_price") is not None:
                    try:
                        ppv_item["offered_price_usd"] = round(float(item["offered_price"]), 2)
                    except Exception:
                        pass
            if fan_id and (msg_en or ppv_item):
                retries_so_far = int(item.get("retry_count") or 0)
                logger.info(
                    f"[{self.char_id}] Draining outbox ({source}, retry={retries_so_far}) -> @{fan_id}: '{msg_en[:60]}'"
                )
                typing_delay = (
                    0.0
                    if (self.fast_mode or retries_so_far > 0)
                    else (1.5 if is_manual else calculate_human_delays(msg_en, fast_mode=self.fast_mode)[1])
                )
                with self._page_lock:
                    self._in_busy_op = True
                    try:
                        self.touch_heartbeat()
                        if retries_so_far > 0 and effective_page is not None and not ppv_item and msg_en:
                            if self._verify_message_already_sent_in_dom(effective_page, fan_id, msg_en):
                                sent_ok = True
                            else:
                                sent_ok = self._send_via_playwright_page(
                                    page=effective_page,
                                    fan_id=fan_id,
                                    message_en=msg_en,
                                    typing_delay_sec=typing_delay,
                                    selected_ppv=ppv_item,
                                    retry_count=retries_so_far,
                                )
                        else:
                            sent_ok = self._send_via_playwright_page(
                                page=effective_page,
                                fan_id=fan_id,
                                message_en=msg_en,
                                typing_delay_sec=typing_delay,
                                selected_ppv=ppv_item,
                                retry_count=retries_so_far,
                            )
                        if not sent_ok and effective_page is not None and not ppv_item and msg_en:
                            if self._verify_message_already_sent_in_dom(effective_page, fan_id, msg_en):
                                sent_ok = True
                    finally:
                        self._in_busy_op = False
                        self.touch_heartbeat()

                with self._lock:
                    self.sent_history.append(
                        {
                            "fan_id": fan_id,
                            "ai_en": msg_en,
                            "ai_ja": item.get("message_ja"),
                            "selected_ppv": ppv_item,
                            "source": source,
                            "retry_count": retries_so_far,
                            "sent_ok": sent_ok,
                        }
                    )
                if sent_ok:
                    drained += 1
                elif effective_page is not None:
                    err_low = str(self.last_error or "").lower()
                    next_retry = retries_so_far + 1
                    is_non_retryable = (
                        "dm permission restricted" in err_low
                        or "unredacted/unsafe ppv blocked" in err_low
                    )
                    if not is_non_retryable and next_retry <= max(1, int(self._max_send_retries)):
                        item["retry_count"] = next_retry
                        item["next_attempt_ts"] = time.time() + (
                            0.2 if self.fast_mode else float(self._retry_backoff_base_sec) * next_retry
                        )
                        deferred_items.append(item)
                        logger.warning(
                            f"[{self.char_id}] Re-queued outbox ({source}) for @{fan_id} "
                            f"(retry {next_retry}/{self._max_send_retries})"
                        )
            self._outbox_queue.task_done()

        for d_item in deferred_items:
            self._outbox_queue.put(d_item)

        if drained > 0 and effective_page is not None and hasattr(effective_page, "goto") and not self._stop_event.is_set():
            try:
                with self._page_lock:
                    if str(getattr(effective_page, "url", "") or "").rstrip("/") != FANSLY_MESSAGES_URL:
                        effective_page.goto(FANSLY_MESSAGES_URL, wait_until="domcontentloaded", timeout=20000)
            except Exception:
                pass
        return drained

    def _wait_for_messages_spa_ready(self, page: Any) -> None:
        """Angular SPA (app-messages-route とスレッド一覧 app-messaging-group) の描画完了を待機する"""
        if not hasattr(page, "wait_for_selector"):
            return
        try:
            page.wait_for_selector(
                "app-messages-route, input[type='password'], app-login-route",
                timeout=12000,
            )
            if _query_first(page, FANSLY_SELECTORS["login_Check_logged_out"]) is not None:
                return
            # 右ペインの app-messages-default-conversation-route はAPI完了前に即座に出るため、
            # 左ペインの app-messaging-group が実際に描画されるまで待機する
            page.wait_for_selector("app-messaging-group", timeout=10000)
            page.wait_for_timeout(600)
        except Exception:
            if hasattr(page, "wait_for_timeout"):
                page.wait_for_timeout(1500)

    def _extract_threads_snapshot(self, page: Any) -> List[Dict[str, Any]]:
        """左ペイン (.message-list a[href^='/messages/'] / app-messaging-group) から全スレッド情報を抽出する"""
        if not hasattr(page, "evaluate"):
            return []
        snap = page.evaluate(
            """() => {
                const results = [];
                const anchors = Array.from(document.querySelectorAll('.message-list a[href^="/messages/"]'));
                const items = anchors.length > 0
                    ? anchors.map(a => ({ anchor: a, group: a.querySelector('app-messaging-group') || a }))
                    : Array.from(document.querySelectorAll('app-messaging-group')).map(g => ({ anchor: g.closest('a[href^="/messages/"]'), group: g }));

                items.forEach((entry, idx) => {
                    const g = entry.group;
                    const a = entry.anchor;
                    const groupHref = a ? (a.getAttribute('href') || '') : '';
                    const groupId = groupHref ? groupHref.replace(/^\\/messages\\//, '').split(/[?#]/)[0] : '';

                    const unameLink = g.querySelector('app-account-username.xs-mobile-hidden a.username-wrapper, app-account-username a.username-wrapper, app-account-avatar a[href^="/"]');
                    const rawProfileHref = unameLink ? (unameLink.getAttribute('href') || '') : '';
                    const handle = rawProfileHref.replace(/^\\/+/, '').split(/[/?#]/)[0].trim();

                    const dispEl = g.querySelector('app-account-username.xs-mobile-hidden .display-name, .message-contact .display-name, .display-name');
                    const displayName = dispEl ? (dispEl.textContent || '').trim() : (handle || `fan_${idx + 1}`);

                    const badgeEl = g.querySelector('.badge-container .badge, .unread-badge, [data-unread="true"]');
                    const badgeText = badgeEl ? (badgeEl.textContent || '').trim() : '';
                    const unreadCount = badgeText ? (parseInt(badgeText, 10) || 1) : (badgeEl ? 1 : 0);

                    const timeEl = g.querySelector('.message-time');
                    const timeText = timeEl ? (timeEl.textContent || '').trim() : '';

                    const prevEl = g.querySelector('.eclipse, .font-size-xs.dark-blue-1.eclipse');
                    const previewText = prevEl ? (prevEl.textContent || '').trim() : '';

                    results.push({
                        idx,
                        group_href: groupHref,
                        group_id: groupId,
                        fan_id: handle || groupId || `fan_${idx + 1}`,
                        username: displayName || handle || `fan_${idx + 1}`,
                        unread_count: unreadCount,
                        has_unread: unreadCount > 0,
                        time_text: timeText,
                        preview_text: previewText
                    });
                });
                return results;
            }"""
        )
        threads = snap if isinstance(snap, list) else []
        for t in threads:
            fid = str(t.get("fan_id") or "").strip()
            ghref = str(t.get("group_href") or "").strip()
            if fid and ghref:
                self._fan_group_hrefs[fid] = ghref
        return threads

    def _extract_conversation_state(self, page: Any) -> Dict[str, Any]:
        """開いている会話画面から最後の my-message 以降のファン送信メッセージ・PPV開封・ティアを取得する"""
        if not hasattr(page, "evaluate"):
            tier_el = _query_first(page, FANSLY_SELECTORS["tier_badge"])
            raw_tier = tier_el.inner_text().strip() if tier_el else None
            unlocked_el = _query_first(page, FANSLY_SELECTORS["ppv_unlocked_indicators"])
            locked_guard_el = _query_first(
                page,
                [
                    ".not-purchased",
                    ".fa-lock",
                    ".icon-lock",
                    "[data-ppv-locked='true']",
                    ".ppv-message.locked",
                    ".locked-price-tag",
                ],
            )
            fan_bubbles = _query_all(page, FANSLY_SELECTORS["message_bubbles_fan"])
            latest_msg = fan_bubbles[-1].inner_text().strip() if fan_bubbles else ""
            return {
                "unanswered_fan_text": latest_msg,
                "last_sender_is_mine": False if latest_msg else True,
                "ppv_unlocked": bool(unlocked_el is not None and locked_guard_el is None),
                "raw_tier": raw_tier,
                "total_messages": len(fan_bubbles),
            }

        try:
            page.wait_for_selector("app-messages-conversation-route", timeout=10000)
            page.wait_for_selector("app-group-message", timeout=8000)
            page.wait_for_timeout(600)
        except Exception:
            page.wait_for_timeout(1200)

        return page.evaluate(
            r"""() => {
                const msgEls = Array.from(document.querySelectorAll('app-group-message-container app-group-message, app-group-message'));
                let lastMyIdx = -1;
                let lastMyAttachmentEl = null;
                const parsed = msgEls.map((el, idx) => {
                    const isMine = el.classList.contains('my-message');
                    if (isMine) lastMyIdx = idx;
                    const txtEl = el.querySelector('.message-text');
                    const text = txtEl ? (txtEl.innerText || txtEl.textContent || '').trim() : '';
                    const attEl = el.querySelector('app-group-message-attachment');
                    if (isMine && attEl) lastMyAttachmentEl = attEl;
                    return { idx, isMine, text, hasAttachment: !!attEl };
                });

                const trailingFanMsgs = parsed.slice(lastMyIdx + 1).filter(m => !m.isMine);
                const dedupTexts = [];
                for (const m of trailingFanMsgs) {
                    const t = m.text || (m.hasAttachment ? '[Media Attachment]' : '');
                    if (t && (dedupTexts.length === 0 || dedupTexts[dedupTexts.length - 1] !== t)) {
                        dedupTexts.push(t);
                    }
                }

                // ★PPV開封厳格判定：最新の自分の添付バブル1件のみを対象とし、
                //   (1) 購入者アバター (.purchased-avatar:not(.not-purchased)) または明示的な Unlocked 完了表記が存在し、かつ
                //   (2) ロックアイコン・未購入クラス・未解除価格タグが完全に消滅している場合のみ true とする
                let ppvUnlocked = false;
                if (lastMyAttachmentEl) {
                    const attText = (lastMyAttachmentEl.innerText || lastMyAttachmentEl.textContent || '').toLowerCase();
                    const hasPurchasedAvatar = !!lastMyAttachmentEl.querySelector('.purchased-content .purchased-avatar:not(.not-purchased)');
                    const hasExplicitUnlockedText = /unlocked\s+for\s+\$|purchased\s+for\s+\$/i.test(attText);
                    const hasNotPurchasedMarker = !!lastMyAttachmentEl.querySelector(
                        '.not-purchased, .fa-lock, .icon-lock, [class*="fa-lock"], .locked-content, .unlock-btn, .pay-to-view'
                    ) || /not\s+purchased|unpurchased|locked/i.test(attText);
                    const hasLockedPriceTag = !!lastMyAttachmentEl.querySelector(
                        '.price-badge, .attachment-price, .media-price, .lock-price'
                    );
                    if ((hasPurchasedAvatar || hasExplicitUnlockedText) && !hasNotPurchasedMarker && !hasLockedPriceTag) {
                        ppvUnlocked = true;
                    }
                }

                const lastMsg = parsed.length > 0 ? parsed[parsed.length - 1] : null;
                const tierEl = document.querySelector('.subscription-tier-badge, .tier-badge, .subscriber-badge, .message-content-header .user-badge');
                const rawTier = tierEl ? (tierEl.textContent || '').trim() : null;

                return {
                    unanswered_fan_text: dedupTexts.join('\\n').trim(),
                    last_sender_is_mine: lastMsg ? !!lastMsg.isMine : false,
                    ppv_unlocked: ppvUnlocked,
                    raw_tier: rawTier,
                    total_messages: parsed.length
                };
            }"""
        )

    def _poll_mock_page(self, page: Any, now_dt: Optional[datetime] = None) -> int:
        """単体テスト用モックPage向けのフォールバックポーリング"""
        threads = _query_all(page, FANSLY_SELECTORS["thread_items"])
        handled_mock = 0
        for idx, thread_el in enumerate(threads[:15]):
            if self._stop_event.is_set() or not self.running:
                break
            if _query_first(thread_el, FANSLY_SELECTORS["unread_badge"]) is None:
                continue
            uname_el = _query_first(thread_el, FANSLY_SELECTORS["thread_username"])
            username = (uname_el.inner_text().strip() if uname_el else "") or f"fan_{idx + 1}"
            fan_id = re.sub(r"[^a-zA-Z0-9_-]", "_", username) or f"fan_{idx + 1}"
            thread_el.click()
            conv = self._extract_conversation_state(page)
            latest_msg = str(conv.get("unanswered_fan_text") or "").strip()
            if not latest_msg:
                continue
            self.handle_incoming_fan_message(
                fan_id=fan_id,
                username=username,
                fan_message_en=latest_msg,
                raw_tier_badge=conv.get("raw_tier"),
                detected_ppv_unlocked=bool(conv.get("ppv_unlocked")),
                page=page,
                now_dt=now_dt,
            )
            handled_mock += 1
        return handled_mock

    def _get_actionable_unanswered_fan_ids(self, max_age_hours: float = 24.0) -> set:
        """
        稼働スイッチON (bot_enabled == True) 時に、DB上で最新メッセージがファン送信 (has_unanswered_fan) のまま
        未返信で残っている対象ファンID集合を返す（クラッシュ再起動後・一時送信失敗後・OFF→ON切替直後の取りこぼし救済用）。
        """
        if not bool(load_settings(self.char_id).get("bot_enabled", False)):
            return set()
        now_ts = time.time()
        now_local = datetime.now()
        result: set = set()
        try:
            threads = get_fan_threads(self.char_id, include_muted=False)
            for th in threads:
                if not th.get("has_unanswered_fan"):
                    continue
                if (
                    int(th.get("is_muted") or 0) == 1
                    or int(th.get("is_escalated") or 0) == 1
                    or int(th.get("manual_hold") or 0) == 1
                ):
                    continue
                fid = str(th.get("fan_id") or "").strip()
                if not fid:
                    continue
                with self._lock:
                    pend = self._pending_replies.get(fid)
                    if pend is not None and not pend["cancel_event"].is_set():
                        continue
                    if now_ts < float(self._unanswered_recheck_after_ts.get(fid) or 0.0):
                        continue
                last_ct = str(th.get("last_contact_at") or "").strip()
                if last_ct and max_age_hours > 0:
                    parsed_dt = None
                    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
                        try:
                            parsed_dt = datetime.strptime(last_ct, fmt)
                            break
                        except ValueError:
                            continue
                    if parsed_dt is not None:
                        age_h = (now_local - parsed_dt).total_seconds() / 3600.0
                        if age_h > max_age_hours:
                            continue
                result.add(fid)
        except Exception as e:
            logger.debug(f"[{self.char_id}] _get_actionable_unanswered_fan_ids check skipped: {e}")
        return result

    def _poll_fansly_page_once(self, page: Any, now_dt: Optional[datetime] = None) -> int:
        """
        https://fansly.com/messages 上のスレッド一覧を巡回し、
        未読メッセージの検知・サブスクプランバッジの取得・PPV購入済みバッジの検知を実行する。
        DOM未検出時は例外を投げず安全に0を返す。
        """
        self.last_poll_at = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        self.touch_heartbeat()
        sleeping, _ = self.is_sleeping(now_dt=now_dt)
        if sleeping or page is None:
            return 0

        try:
            if hasattr(page, "is_closed") and callable(page.is_closed) and page.is_closed():
                raise RuntimeError("Playwright page is closed")

            current_url = str(getattr(page, "url", "") or "")
            if "fansly.com/messages" not in current_url:
                logger.info(f"[{self.char_id}] Opening {FANSLY_MESSAGES_URL} (was: {current_url})")
                page.goto(FANSLY_MESSAGES_URL, wait_until="domcontentloaded", timeout=25000)

            self._wait_for_messages_spa_ready(page)

            if _query_first(page, FANSLY_SELECTORS["login_Check_logged_out"]) is not None:
                self.last_error = "Fansly未ログイン状態: [⚙️ 設定] -> [🔑 ブラウザを開いて手動ログイン] を実行してください"
                logger.warning(f"[{self.char_id}] {self.last_error}")
                with self._lock:
                    self.consecutive_poll_failures = 0
                return 0

            if not hasattr(page, "evaluate"):
                res_mock = self._poll_mock_page(page=page, now_dt=now_dt)
                with self._lock:
                    self.consecutive_poll_failures = 0
                return res_mock

            snapshot = self._extract_threads_snapshot(page)
            with self._lock:
                self.consecutive_poll_failures = 0
            if not snapshot:
                logger.info(f"[{self.char_id}] Poll scan: 0 threads found on {getattr(page, 'url', '')}")
                return 0

            unread_cnt = sum(1 for t in snapshot if t.get("has_unread"))
            top_summary = ", ".join(
                f"@{t['fan_id']}(badge={t['unread_count']}, t='{t['time_text']}', prev='{t['preview_text'][:20]}')"
                for t in snapshot[:5]
            )
            logger.info(
                f"[{self.char_id}] Poll scan: threads={len(snapshot)}, unread={unread_cnt} | top5=[{top_summary}]"
            )
            self.last_error = None

            db_unanswered_fids = self._get_actionable_unanswered_fan_ids(max_age_hours=24.0)
            now_poll_ts = time.time()
            candidates = [
                t
                for t in snapshot[:15]
                if t.get("has_unread")
                or (
                    int(t.get("idx", 99)) < 5
                    and _is_recent_thread_time(str(t.get("time_text") or ""))
                    and self._checked_read_previews.get(str(t.get("fan_id") or "")) != str(t.get("preview_text") or "")
                    and now_poll_ts >= float(self._unanswered_recheck_after_ts.get(str(t.get("fan_id") or "")) or 0.0)
                )
                or (str(t.get("fan_id") or "").strip() in db_unanswered_fids)
            ]
            if not candidates:
                return 0

            handled = 0
            opened_any = False
            for t in candidates:
                if self._stop_event.is_set() or not self.running:
                    break

                fan_id = str(t.get("fan_id") or "").strip()
                username = str(t.get("username") or fan_id).strip()
                preview_text = str(t.get("preview_text") or "").strip()
                if t.get("has_unread"):
                    reason = f"badge={t.get('unread_count')}"
                elif fan_id in db_unanswered_fids:
                    reason = f"db_unanswered_recovery(t={t.get('time_text')})"
                else:
                    reason = f"recent={t.get('time_text')}"

                logger.info(f"[{self.char_id}] Inspecting @{fan_id} ({reason}, href={t.get('group_href')})...")
                if not self._ensure_fan_conversation_open(page, fan_id):
                    continue
                opened_any = True

                conv = self._extract_conversation_state(page)
                ppv_unlocked = bool(conv.get("ppv_unlocked"))
                unanswered_text = str(conv.get("unanswered_fan_text") or "").strip()

                if ppv_unlocked and has_eligible_locked_ppv_for_unlock(
                    char_id=self.char_id,
                    fan_id=fan_id,
                    min_elapsed_sec=15.0,
                    now_dt=now_dt,
                ):
                    logger.info(f"[{self.char_id}] Verified PPV unlock for @{fan_id}; updating DB")
                    mark_ppv_unlocked(char_id=self.char_id, fan_id=fan_id)

                if conv.get("last_sender_is_mine") or not unanswered_text:
                    logger.info(f"[{self.char_id}] @{fan_id}: no pending fan message (last_is_mine={conv.get('last_sender_is_mine')})")
                    self._checked_read_previews[fan_id] = preview_text
                    continue

                if fan_id in self._pending_replies and not self._pending_replies[fan_id]["cancel_event"].is_set():
                    logger.info(f"[{self.char_id}] @{fan_id}: auto-reply countdown already active; skipping duplicate trigger")
                    self._checked_read_previews[fan_id] = preview_text
                    continue

                if self._last_replied_fingerprints.get(fan_id) == unanswered_text:
                    logger.info(f"[{self.char_id}] @{fan_id}: already replied in session; skipping")
                    self._checked_read_previews[fan_id] = preview_text
                    continue

                db_msgs = (get_thread_detail(self.char_id, fan_id) or {}).get("messages") or []
                if (
                    len(db_msgs) >= 2
                    and str(db_msgs[-1].get("sender") or "") in ("ai", "human_manual")
                    and str(db_msgs[-2].get("sender") or "") == "fan"
                    and str(db_msgs[-2].get("content") or "").strip() == unanswered_text
                ):
                    logger.info(f"[{self.char_id}] @{fan_id}: already answered in DB; skipping")
                    self._last_replied_fingerprints[fan_id] = unanswered_text
                    self._checked_read_previews[fan_id] = preview_text
                    continue

                logger.info(f"[{self.char_id}] Incoming DM from @{fan_id} ({username}): '{unanswered_text[:100]}'")
                res = self.handle_incoming_fan_message(
                    fan_id=fan_id,
                    username=username,
                    fan_message_en=unanswered_text,
                    raw_tier_badge=conv.get("raw_tier"),
                    detected_ppv_unlocked=ppv_unlocked,
                    page=page,
                    now_dt=now_dt,
                    async_schedule=True,
                )
                self._checked_read_previews[fan_id] = preview_text
                if res.get("ok"):
                    handled += 1

            if opened_any and not self._stop_event.is_set():
                try:
                    if str(getattr(page, "url", "") or "").rstrip("/") != FANSLY_MESSAGES_URL:
                        page.goto(FANSLY_MESSAGES_URL, wait_until="domcontentloaded", timeout=20000)
                        page.wait_for_timeout(800)
                except Exception:
                    pass

            return handled
        except Exception as e:
            err_msg = f"Poll error: {e}"
            logger.warning(f"[{self.char_id}] {err_msg}")
            self.last_error = err_msg
            with self._lock:
                self.consecutive_poll_failures += 1
            err_lower = str(e).lower()
            fatal_keywords = (
                "target closed",
                "page closed",
                "page is closed",
                "browser has been closed",
                "context or browser has been closed",
                "connection closed",
                "pipe closed",
                "crashed",
            )
            if any(k in err_lower for k in fatal_keywords) or self.consecutive_poll_failures >= 3:
                raise RuntimeError(f"Recoverable browser/poll failure ({self.consecutive_poll_failures}): {e}") from e
            return 0

    def _launch_browser_session(self) -> Tuple[Any, Any, Any]:
        """Playwright persistent_context を起動し、デフォルトタイムアウトを設定して返す"""
        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
        context = pw.chromium.launch_persistent_context(
            user_data_dir=self.profile_dir,
            headless=self.headless,
            viewport={"width": 1360, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            context.set_default_timeout(20000)
            context.set_default_navigation_timeout(30000)
        except Exception:
            pass
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.set_default_timeout(20000)
            page.set_default_navigation_timeout(30000)
        except Exception:
            pass
        with self._lock:
            self._active_pw = pw
            self._active_context = context
            self._active_page = page
        return pw, context, page

    def _close_local_browser_session(self, pw: Any, context: Any) -> None:
        with self._lock:
            if self._active_context is context:
                self._active_context = None
                self._active_page = None
            if self._active_pw is pw:
                self._active_pw = None
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        if pw is not None:
            try:
                pw.stop()
            except Exception:
                pass

    def _worker_loop(self) -> None:
        """
        バックグラウンド監視ループ。
        Playwrightが利用可能な環境では persistent_context を開いて巡回し、
        ページ閉鎖やコンテキスト異常を検知した際は自動でブラウザコンテキストを再生成して復旧する。
        ブラウザ未ログイン・ヘッドレス不可環境でもスレッドがクラッシュせず安全に待機・キュー処理を行う。
        """
        my_stop_event = self._stop_event
        context = None
        pw = None
        page = None
        try:
            os.makedirs(self.profile_dir, exist_ok=True)
            self.touch_heartbeat()
            if not self.fast_mode:
                try:
                    pw, context, page = self._launch_browser_session()
                except Exception as browser_err:
                    self.last_error = f"Playwright browser standby ({browser_err})"
                    logger.info(f"[{self.char_id}] Running in queue/standby mode: {browser_err}")

            last_page_poll_ts = 0.0
            last_browser_retry_ts = time.time()
            while not my_stop_event.is_set() and self.running:
                self.touch_heartbeat()
                if not self.manual_login_active:
                    # ブラウザ未起動・切断状態からの自動再起動チェック
                    if not self.fast_mode:
                        need_reopen = False
                        if page is None:
                            if time.time() - last_browser_retry_ts >= max(15.0, self.poll_interval_sec):
                                need_reopen = True
                        elif hasattr(page, "is_closed") and callable(page.is_closed):
                            try:
                                if page.is_closed():
                                    need_reopen = True
                            except Exception:
                                need_reopen = True

                        if need_reopen:
                            last_browser_retry_ts = time.time()
                            logger.warning(f"[{self.char_id}] Re-initializing Playwright browser context...")
                            self._close_local_browser_session(pw, context)
                            pw, context, page = None, None, None
                            try:
                                pw, context, page = self._launch_browser_session()
                                self.consecutive_poll_failures = 0
                                logger.info(f"[{self.char_id}] Playwright browser context recovered.")
                            except Exception as reopen_err:
                                self.last_error = f"Browser reopen failed ({reopen_err})"
                                logger.warning(f"[{self.char_id}] {self.last_error}")

                    self._drain_outbox_queue(page=page)
                    self.touch_heartbeat()
                    self._process_due_pending_replies(page=page)
                    self.touch_heartbeat()

                    now_loop_ts = time.time()
                    if now_loop_ts - last_page_poll_ts >= self.poll_interval_sec:
                        self.last_poll_at = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
                        last_page_poll_ts = now_loop_ts
                        if page is not None:
                            try:
                                with self._page_lock:
                                    self._in_busy_op = True
                                    try:
                                        self._poll_fansly_page_once(page=page)
                                    finally:
                                        self._in_busy_op = False
                            except Exception as poll_fatal_err:
                                logger.warning(
                                    f"[{self.char_id}] Resetting browser context after poll failure: {poll_fatal_err}"
                                )
                                self._close_local_browser_session(pw, context)
                                pw, context, page = None, None, None
                                last_browser_retry_ts = 0.0
                        self.touch_heartbeat()
                        self._process_due_pending_replies(page=page)

                with self._lock:
                    next_due_list = [
                        max(0.2, float(e["send_start_ts"]) - time.time())
                        for e in self._pending_replies.values()
                        if not e["cancel_event"].is_set()
                    ]
                wait_sec = min([1.0] + next_due_list) if next_due_list else min(2.0, self.poll_interval_sec)
                if not self._wait_interruptible(wait_sec, cancel_event=my_stop_event, wake_on_activity=True):
                    break
        except Exception as loop_err:
            self.last_error = str(loop_err)
            logger.error(f"[{self.char_id}] Worker loop error: {loop_err}")
        finally:
            self._close_local_browser_session(pw, context)
            with self._lock:
                if self._thread is threading.current_thread():
                    self.running = False
                    self._thread = None


class BotManager:
    """
    全キャラクターの Playwright DM監視ワーカーを一括・個別管理するシングルトンマネージャー。
    - キャラ別永続プロファイル: config.PROFILES_DIR / <char_id>
    - 全キャラ一括稼働 (start_all) / 全停止 (stop_all) / 個別トグル (toggle_char)
    - 睡眠時間判定 (JST) と sleeping: True ステータス返却
    - 手動ログインブラウザ起動 (launch_manual_login)
    """

    def __init__(
        self,
        fast_mode: bool = True,
        headless: bool = True,
        watchdog_interval_sec: float = 15.0,
        watchdog_stale_sec: float = 90.0,
    ):
        self._lock = threading.RLock()
        self.fast_mode = fast_mode
        self.headless = headless
        self.watchdog_interval_sec = watchdog_interval_sec
        self.watchdog_stale_sec = watchdog_stale_sec
        self._workers: Dict[str, CharacterBotWorker] = {}
        self._monitoring_active: bool = False
        self._watchdog_stop_event = threading.Event()
        self._watchdog_thread: Optional[threading.Thread] = None
        self.watchdog_restarts_total: int = 0
        self.last_watchdog_check_at: Optional[str] = None

    def _get_or_create_worker(self, char_id: str) -> CharacterBotWorker:
        with self._lock:
            if char_id not in self._workers:
                self._workers[char_id] = CharacterBotWorker(
                    char_id=char_id,
                    fast_mode=self.fast_mode,
                    headless=self.headless,
                )
            return self._workers[char_id]

    def sync_workers_with_characters(self) -> List[str]:
        char_ids = list_character_ids()
        with self._lock:
            for cid in char_ids:
                self._get_or_create_worker(cid)
        return char_ids

    def is_sleeping(self, char_id: str, now_dt: Optional[datetime] = None) -> bool:
        sleeping, _ = check_char_sleep_state(char_id, now_dt=now_dt)
        return sleeping

    def start_watchdog(self) -> bool:
        """常時DM巡回監視の生存確認（ハートビート／スレッド死活監視）ウォッチドッグを起動する"""
        with self._lock:
            self._monitoring_active = True
            if self._watchdog_thread and self._watchdog_thread.is_alive():
                return True
            self._watchdog_stop_event.clear()
            self._watchdog_thread = threading.Thread(
                target=self._watchdog_loop,
                name="FanslyDMBot-Watchdog",
                daemon=True,
            )
            self._watchdog_thread.start()
            logger.info(
                f"Monitoring watchdog started (interval={self.watchdog_interval_sec}s, stale_threshold={self.watchdog_stale_sec}s)"
            )
            return True

    def stop_watchdog(self, timeout: float = 3.0) -> bool:
        with self._lock:
            self._monitoring_active = False
            self._watchdog_stop_event.set()
            w_thread = self._watchdog_thread
            self._watchdog_thread = None
        if w_thread and w_thread.is_alive() and w_thread is not threading.current_thread():
            w_thread.join(timeout=timeout)
        logger.info("Monitoring watchdog stopped")
        return True

    def check_and_recover_workers_once(self, stale_threshold_sec: Optional[float] = None) -> List[Dict[str, Any]]:
        """
        全キャラの監視ワーカーの生存確認を1回実行し、
        - 監視スレッド停止（クラッシュや不意の終了）
        - ハートビート途絶（ブラウザコンテキストやページ操作のハング）
        を検知した場合に手動操作なしで自動再起動する。
        """
        if not self._monitoring_active:
            return []

        threshold = float(stale_threshold_sec if stale_threshold_sec is not None else self.watchdog_stale_sec)
        now_ts = time.time()
        self.last_watchdog_check_at = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        recovered: List[Dict[str, Any]] = []

        char_ids = self.sync_workers_with_characters()
        for cid in char_ids:
            if not self._monitoring_active:
                break
            worker = self._get_or_create_worker(cid)
            if worker.manual_login_active:
                continue

            with worker._lock:
                thread_alive = bool(worker.running and worker._thread and worker._thread.is_alive())
                hb_ts = float(worker.last_heartbeat_ts or 0.0)
                in_busy_op = bool(worker._in_busy_op)

            effective_threshold = max(threshold, 180.0) if in_busy_op else threshold
            reason: Optional[str] = None
            if not thread_alive:
                reason = "worker_thread_stopped"
            elif hb_ts > 0.0 and (now_ts - hb_ts) > effective_threshold:
                reason = f"heartbeat_stale_{int(now_ts - hb_ts)}s"

            if reason is not None:
                ok = worker.force_restart(reason=reason)
                if ok:
                    with self._lock:
                        self.watchdog_restarts_total += 1
                    recovered.append({"char_id": cid, "reason": reason, "restarted": True})
                    logger.warning(f"[{cid}] Watchdog auto-restarted passive monitoring worker ({reason})")

        return recovered

    def _watchdog_loop(self) -> None:
        while not self._watchdog_stop_event.is_set() and self._monitoring_active:
            try:
                self.check_and_recover_workers_once()
            except Exception as wd_err:
                logger.warning(f"Watchdog check error: {wd_err}")

            end_wait = time.time() + max(0.5, float(self.watchdog_interval_sec))
            while time.time() < end_wait:
                if self._watchdog_stop_event.is_set() or not self._monitoring_active:
                    break
                time.sleep(min(0.3, max(0.05, end_wait - time.time())))

    def start_monitoring_all(self) -> Dict[str, Any]:
        """全キャラのDM巡回監視ワーカーとウォッチドッグを起動する（bot_enabled 設定は変更せず、OFFキャラも受信・翻訳・カルテ更新を常時継続）"""
        with self._lock:
            self._monitoring_active = True
            for cid in self.sync_workers_with_characters():
                worker = self._get_or_create_worker(cid)
                worker.start()
            if not self.fast_mode:
                self.start_watchdog()
            return self.get_status()

    def start_char(self, char_id: str) -> Dict[str, Any]:
        with self._lock:
            save_settings(char_id, {"bot_enabled": True})
            worker = self._get_or_create_worker(char_id)
            worker.start()
            if not self.fast_mode:
                self.start_watchdog()
            return worker.get_status()

    def pause_char_auto_sales(self, char_id: str, ensure_monitoring: bool = True) -> Dict[str, Any]:
        """稼働スイッチ OFF 時：AI自動返信・PPV送信・追撃のみ止め、Playwright巡回監視スレッドは維持する"""
        with self._lock:
            save_settings(char_id, {"bot_enabled": False})
            worker = self._get_or_create_worker(char_id)
            worker.cancel_all_pending_replies()
            if ensure_monitoring and not self.fast_mode:
                if not (worker.running and worker._thread and worker._thread.is_alive()):
                    worker.start()
                self.start_watchdog()
            return worker.get_status()

    def stop_char(self, char_id: str, terminate_worker: bool = True) -> Dict[str, Any]:
        with self._lock:
            save_settings(char_id, {"bot_enabled": False})
            worker = self._get_or_create_worker(char_id)
            worker.cancel_all_pending_replies()
            if terminate_worker:
                worker.stop()
            return worker.get_status()

    def toggle_char(
        self,
        char_id: str,
        enabled: Optional[bool] = None,
        ensure_monitoring: bool = True,
    ) -> Dict[str, Any]:
        with self._lock:
            if enabled is None:
                cur_settings = load_settings(char_id)
                target_enabled = not bool(cur_settings.get("bot_enabled", False))
            else:
                target_enabled = bool(enabled)

            if target_enabled:
                self.start_char(char_id)
            else:
                self.pause_char_auto_sales(char_id, ensure_monitoring=ensure_monitoring)
            return self.get_status(char_id=char_id)

    def start_all(self) -> Dict[str, Any]:
        with self._lock:
            for cid in self.sync_workers_with_characters():
                self.start_char(cid)
            if not self.fast_mode:
                self.start_watchdog()
            return self.get_status()

    def pause_all_auto_sales(self, ensure_monitoring: bool = True) -> Dict[str, Any]:
        """[⏹ 全停止] ボタン押下時：全キャラの自動返信のみ停止し、DM巡回監視・翻訳・カルテ更新は常時継続する"""
        with self._lock:
            for cid in self.sync_workers_with_characters():
                self.pause_char_auto_sales(cid, ensure_monitoring=ensure_monitoring)
            return self.get_status()

    def stop_all(self, update_settings: bool = True) -> Dict[str, Any]:
        """ブラウザウィンドウ「×」閉鎖・終了時：ウォッチドッグおよび全ワーカーとPlaywrightプロセスをクリーン終了する"""
        self.stop_watchdog()
        with self._lock:
            for cid in self.sync_workers_with_characters():
                worker = self._get_or_create_worker(cid)
                worker.cancel_all_pending_replies()
                if update_settings:
                    save_settings(cid, {"bot_enabled": False})
                worker.stop()
            return self.get_status()

    def get_pending_replies(
        self,
        char_id: Optional[str] = None,
        fan_id: Optional[str] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """全キャラまたは指定キャラの自動返信カウントダウン一覧 (key='char_id::fan_id') を返す"""
        with self._lock:
            target_cids = (
                [char_id]
                if (char_id and char_id != "all")
                else self.sync_workers_with_characters()
            )
            combined: Dict[str, Dict[str, Any]] = {}
            for cid in target_cids:
                worker = self._get_or_create_worker(cid)
                snaps = worker.get_pending_replies_snapshot(fan_id=fan_id)
                for fid, snap in snaps.items():
                    combined[f"{cid}::{fid}"] = snap
            return combined

    def has_active_session_activity(self, char_id: str, fan_id: str) -> bool:
        """指定キャラ・ファンに対して自動返信カウントダウン中または未送信キューが存在するか判定する"""
        if not char_id or not fan_id:
            return False
        with self._lock:
            worker = self._get_or_create_worker(char_id)
        return worker.has_active_session_activity(fan_id)

    def skip_to_manual(
        self,
        char_id: str,
        fan_id: str,
        hold_at: Optional[str] = None,
        db_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """待機中のAI自動返信タスクをキャンセルし、手動対応保留中 (manual_hold=1) に切り替える"""
        worker = self._get_or_create_worker(char_id)
        cancelled = worker.cancel_pending_reply(fan_id)
        fan_state = set_fan_manual_hold(char_id, fan_id, hold=True, hold_at=hold_at, db_path=db_path)
        return {
            "ok": True,
            "char_id": char_id,
            "fan_id": fan_id,
            "pending_cancelled": cancelled,
            "manual_hold": True,
            "manual_hold_at": fan_state.get("manual_hold_at"),
            "fan": fan_state,
        }

    def resume_auto_reply(
        self,
        char_id: str,
        fan_id: str,
        custom_delay_sec: Optional[float] = None,
        start_worker: bool = False,
        db_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """手動対応保留を解除し、直ちにAI自動返信カウントダウンを再セットして自動営業へ復帰する"""
        set_fan_manual_hold(char_id, fan_id, hold=False, db_path=db_path)
        resolve_fan_escalation(char_id, fan_id, db_path=db_path)

        detail = get_thread_detail(char_id, fan_id, db_path=db_path) or {}
        fan_rec = detail.get("fan") or get_fan_record(char_id, fan_id, db_path=db_path) or {}
        msgs = list(detail.get("messages") or [])

        fan_msgs = [m for m in msgs if str(m.get("sender") or "") == "fan" and m.get("content")]
        latest_fan_text = (
            str(fan_msgs[-1]["content"])
            if fan_msgs
            else "Hey! I've been waiting to hear from you 🖤"
        )
        history_before = msgs[:-1] if (msgs and str(msgs[-1].get("sender") or "") == "fan") else msgs

        engine_res = sales_engine.process_fan_message(
            char_id=char_id,
            user_message=latest_fan_text,
            fan_id=fan_id,
            current_tier=str(fan_rec.get("current_tier") or "free_follower"),
            total_spent_usd=float(fan_rec.get("total_spent_usd") or 0.0),
            sales_phase=str(fan_rec.get("sales_phase") or "1_Icebreak"),
            consecutive_ppv_ignored=int(fan_rec.get("consecutive_ppv_ignored") or 0),
            consecutive_unanswered=0,
            recent_messages=history_before,
            existing_profile_json=fan_rec.get("psych_profile_json") or "{}",
            persist_db=True,
            record_tip_in_db=False,
            db_path=db_path,
        )

        worker = self._get_or_create_worker(char_id)
        worker._schedule_pending_reply(
            fan_id=fan_id,
            username=str(fan_rec.get("username") or fan_id),
            fan_message_en=latest_fan_text,
            sales_phase=str(fan_rec.get("sales_phase") or "1_Icebreak"),
            engine_res=engine_res,
            custom_delay_sec=custom_delay_sec,
            allow_when_bot_off=True,
            db_path=db_path,
        )
        if start_worker and not self.fast_mode and not (worker.running and worker._thread and worker._thread.is_alive()):
            worker.start()
            self.start_watchdog()

        snap = worker.get_pending_replies_snapshot(fan_id).get(fan_id)
        updated_fan = get_fan_record(char_id, fan_id, db_path=db_path) or {}
        return {
            "ok": True,
            "char_id": char_id,
            "fan_id": fan_id,
            "manual_hold": False,
            "pending_reply": snap,
            "fan": updated_fan,
            "decision_log": engine_res.get("decision_log"),
        }

    def ensure_monitoring_healthy(self) -> List[Dict[str, Any]]:
        """
        UIポーリング時などにも呼び出し可能な軽量ヘルスチェック。
        ウォッチドッグ稼働中（アプリ起動中）に停止・応答途絶している監視ワーカーがあれば即座に復旧する。
        """
        if self.fast_mode or not self._monitoring_active:
            return []
        if not (self._watchdog_thread and self._watchdog_thread.is_alive()):
            self.start_watchdog()
        now_ts = time.time()
        # 過剰なロック競合を避けるため、最低5秒以上の間隔をあけて判定
        last_check_ts = getattr(self, "_last_inline_health_ts", 0.0)
        if now_ts - last_check_ts < 5.0:
            return []
        self._last_inline_health_ts = now_ts
        return self.check_and_recover_workers_once()

    def get_status(self, char_id: Optional[str] = None, now_dt: Optional[datetime] = None) -> Dict[str, Any]:
        self.ensure_monitoring_healthy()
        with self._lock:
            char_ids = self.sync_workers_with_characters()
            workers_map: Dict[str, Dict[str, Any]] = {}
            characters_list: List[Dict[str, Any]] = []
            for cid in char_ids:
                worker = self._get_or_create_worker(cid)
                st = worker.get_status(now_dt=now_dt)
                workers_map[cid] = st
                characters_list.append(st)

            any_enabled = any(w.get("bot_enabled") for w in characters_list)
            all_enabled = bool(characters_list) and all(w.get("bot_enabled") for w in characters_list)
            any_running = any(w.get("running") for w in characters_list)
            all_running = bool(characters_list) and all(w.get("running") for w in characters_list)
            any_sleeping = any(w.get("sleeping") for w in characters_list)

            res: Dict[str, Any] = {
                "ok": True,
                "any_bot_enabled": any_enabled,
                "all_bots_enabled": all_enabled,
                "any_running": any_running,
                "all_running": all_running,
                "any_sleeping": any_sleeping,
                "watchdog_active": bool(self._monitoring_active and self._watchdog_thread and self._watchdog_thread.is_alive()),
                "watchdog_restarts_total": self.watchdog_restarts_total,
                "last_watchdog_check_at": self.last_watchdog_check_at,
                "workers": workers_map,
                "characters": characters_list,
            }
            if char_id and char_id != "all" and char_id in workers_map:
                res["char_id"] = char_id
                res["char_status"] = workers_map[char_id]
                res["enabled"] = workers_map[char_id]["bot_enabled"]
                res["running"] = workers_map[char_id]["running"]
                res["sleeping"] = workers_map[char_id]["sleeping"]
            return res

    def enqueue_outbound_message(
        self,
        char_id: str,
        fan_id: str,
        message_en: str,
        message_ja: Optional[str] = None,
        ppv_item: Optional[Dict[str, Any]] = None,
        offered_price: Optional[float] = None,
        username: Optional[str] = None,
        source: str = "followup",
    ) -> Dict[str, Any]:
        with self._lock:
            worker = self._get_or_create_worker(char_id)
            queued_item = worker.enqueue_outbound(
                {
                    "fan_id": fan_id,
                    "username": username or fan_id,
                    "message_en": message_en,
                    "message_ja": message_ja or "",
                    "ppv_item": ppv_item,
                    "offered_price": offered_price,
                    "source": source,
                }
            )
            if (
                not self.fast_mode
                and self._monitoring_active
                and not queued_item.get("skipped_active_pending_reply")
                and not worker.manual_login_active
                and not (worker.running and worker._thread and worker._thread.is_alive())
            ):
                worker.start()
                self.start_watchdog()
            return queued_item

    def handle_incoming_message(
        self,
        char_id: str,
        fan_id: str,
        username: str,
        fan_message_en: str,
        raw_tier_badge: Optional[str] = None,
        detected_ppv_unlocked: bool = False,
        unlocked_ppv_id: Optional[str] = None,
        page: Any = None,
        now_dt: Optional[datetime] = None,
        db_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        worker = self._get_or_create_worker(char_id)
        return worker.handle_incoming_fan_message(
            fan_id=fan_id,
            username=username,
            fan_message_en=fan_message_en,
            raw_tier_badge=raw_tier_badge,
            detected_ppv_unlocked=detected_ppv_unlocked,
            unlocked_ppv_id=unlocked_ppv_id,
            page=page,
            now_dt=now_dt,
            db_path=db_path,
        )

    def launch_manual_login(
        self,
        char_id: str,
        launch_browser: bool = True,
        blocking: bool = False,
        timeout_sec: int = 600,
    ) -> Dict[str, Any]:
        """
        [⚙️ 設定] モーダルの「🔑 ブラウザを開いて手動ログイン / 再認証」から呼ばれ、
        ヘッドフル (headless=False) で config.PROFILES_DIR / <char_id> を使用して
        https://fansly.com/messages を開き、ユーザーがログインしてブラウザを閉じるまで待機・セッション保存する。
        """
        if not char_id or char_id == "all":
            chars = list_character_ids()
            char_id = chars[0] if chars else "Sylphy-Mia"

        worker = self._get_or_create_worker(char_id)
        profile_dir = worker.get_profile_dir()
        was_running = bool(worker.running and worker._thread and worker._thread.is_alive())
        worker.manual_login_active = True
        if was_running:
            worker.stop()

        def _run_manual_session() -> None:
            try:
                from playwright.sync_api import sync_playwright

                with sync_playwright() as p:
                    ctx = p.chromium.launch_persistent_context(
                        user_data_dir=profile_dir,
                        headless=False,
                        viewport={"width": 1360, "height": 900},
                        args=["--disable-blink-features=AutomationControlled"],
                    )
                    page = ctx.pages[0] if ctx.pages else ctx.new_page()
                    page.goto(FANSLY_MESSAGES_URL, wait_until="domcontentloaded", timeout=30000)

                    start_ts = time.time()
                    while time.time() - start_ts < timeout_sec:
                        if not ctx.pages or page.is_closed():
                            break
                        page.wait_for_timeout(1000)
                    try:
                        ctx.close()
                    except Exception:
                        pass
            except Exception as e:
                worker.last_error = f"Manual login session info: {e}"
                logger.warning(f"[{char_id}] Manual login browser error/close: {e}")
            finally:
                worker.manual_login_active = False
                if was_running:
                    worker.start()

        launched = False
        message = f"キャラ '{char_id}' の専用ブラウザプロファイル ({profile_dir}) を準備しました。"
        if launch_browser:
            if blocking:
                _run_manual_session()
                launched = True
                message = f"キャラ '{char_id}' の手動ログインセッションを完了し、プロファイルへ保存しました。"
            else:
                login_thread = threading.Thread(
                    target=_run_manual_session,
                    name=f"ManualLogin-{char_id}",
                    daemon=True,
                )
                login_thread.start()
                launched = True
                message = (
                    f"キャラ '{char_id}' 用の手動ログインブラウザ（ヘッドフル）を起動しました。"
                    "ログイン完了後にブラウザを閉じるとセッションが自動保存されます。"
                )
        else:
            worker.manual_login_active = False

        return {
            "ok": True,
            "char_id": char_id,
            "profile_dir": profile_dir,
            "url": FANSLY_MESSAGES_URL,
            "headless": False,
            "launched": launched,
            "message": message,
        }


bot_manager = BotManager(fast_mode=False, headless=True)









