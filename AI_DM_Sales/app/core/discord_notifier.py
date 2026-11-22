"""
Discord Webhook 通知モジュール (Ticket 1.3)

system_settings の discord_webhook_url を読み取り、Embed 付き Webhook POST を
非同期スレッド (urllib.request, timeout=5s) で送信する。メインスレッドをブロックしない。
同一 title の通知は直近3分以内に既送の場合、インメモリデバウンスで送信を抑制する。
"""

import json
import threading
import time
import urllib.request
from typing import Any, Dict, Optional

from app.core import database

# 同一 title の再通知を抑制する時間窓 (秒) = 3分
_ALERT_DEBOUNCE_WINDOW_SEC = 180.0

# level -> Discord Embed color (10進)
_EMBED_COLORS: Dict[str, int] = {
    "ERROR": 0xE02424,      # 赤
    "WARNING": 0xE3A008,    # 黄
    "INFO": 0x3F83F8,       # 青
}

# インメモリデバウンスキャッシュ: title -> 直近送信時刻 (epoch秒)
_alert_debounce_cache: Dict[str, float] = {}
_debounce_lock = threading.Lock()


def _get_setting_value(key: str, db_path: Optional[str] = None) -> str:
    """system_settings テーブルから指定キーの値 (文字列) を取得する。無ければ空文字。"""
    try:
        conn = database.get_db_connection(db_path)
    except Exception:
        return ""
    try:
        row = conn.execute(
            "SELECT setting_value FROM system_settings WHERE setting_key = ?;",
            (key,),
        ).fetchone()
        return str(row["setting_value"] or "") if row else ""
    except Exception:
        return ""
    finally:
        conn.close()


def _build_payload(
    title: str,
    message: str,
    level: str,
    extra_fields: Optional[Dict[str, str]],
) -> Dict[str, Any]:
    """Discord Webhook 用の Embed ペイロードを構築する。"""
    color = _EMBED_COLORS.get(str(level).upper(), _EMBED_COLORS["INFO"])
    fields: Dict[str, Any] = []
    if extra_fields:
        for k, v in extra_fields.items():
            fields.append({"name": str(k), "value": str(v), "inline": True})
    embed = {
        "title": str(title or ""),
        "description": str(message or ""),
        "color": color,
        "fields": fields,
    }
    return {"embeds": [embed]}


def _post_webhook(url: str, payload: Dict[str, Any]) -> bool:
    """Webhook へ POST 送信する。タイムアウト5秒。例外は握りつぶして False を返す。"""
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            return getattr(resp, "status", 200) < 400
    except Exception:
        return False


def send_discord_alert(
    title: str,
    message: str,
    level: str = "ERROR",
    extra_fields: Optional[Dict[str, str]] = None,
    db_path: Optional[str] = None,
) -> bool:
    """
    Discord Webhook へアラートを非同期送信する。

    - discord_webhook_url が空 / 無効 (http(s) で始まらない) の場合は送信せず False。
    - 同一 title が直近3分以内に送信済みの場合はデバウンスで送信せず False。
    - それ以外の場合は非同期スレッドで POST を行い True を返す。
    送信失敗・例外はプロセスを落としていない（スレッド内で握りつぶす）。
    """
    url = _get_setting_value("discord_webhook_url", db_path)
    if not url or not str(url).startswith(("http://", "https://")):
        return False

    now = time.time()
    with _debounce_lock:
        last = _alert_debounce_cache.get(title)
        if last is not None and (now - last) < _ALERT_DEBOUNCE_WINDOW_SEC:
            return False
        _alert_debounce_cache[title] = now
        # 古いエントリを破棄してメモリを節約
        for k in [
            k
            for k, v in _alert_debounce_cache.items()
            if now - v >= _ALERT_DEBOUNCE_WINDOW_SEC
        ]:
            _alert_debounce_cache.pop(k, None)

    payload = _build_payload(title, message, level, extra_fields)
    threading.Thread(
        target=_post_webhook,
        args=(url, payload),
        daemon=True,
    ).start()
    return True


def is_webhook_configured(db_path: Optional[str] = None) -> bool:
    """discord_webhook_url が有効な http(s) URL で設定されているかを返す。"""
    url = _get_setting_value("discord_webhook_url", db_path)
    return bool(url) and str(url).startswith(("http://", "https://"))
