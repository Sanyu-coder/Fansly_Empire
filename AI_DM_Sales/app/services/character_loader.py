import json
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.core import config
from app.core.database import (
    DEFAULT_STAGE_THRESHOLDS,
    get_ppv_vault_records_db,
    get_system_reply_delay_settings,
    get_system_stage_thresholds,
    normalize_char_threshold_bounds,
    normalize_reply_delay_bounds,
    normalize_stage_thresholds,
    save_system_reply_delay_settings,
    save_system_stage_thresholds,
    sync_ppv_vault_items_db,
)

VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".avi", ".mkv", ".m4v"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}


def detect_media_type(filename: str) -> str:
    """ファイル名の拡張子から media_type ('video' または 'image') を自動判定する"""
    ext = os.path.splitext(filename or "")[1].lower()
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext in VIDEO_EXTENSIONS:
        return "video"
    return "video"


def get_default_dm_persona(char_id: str, display_name: Optional[str] = None) -> Dict[str, Any]:
    return {
        "char_id": char_id,
        "display_name": display_name or char_id,
        "language": "en",
        "tone_and_style": (
            "Alluring, warm, playful, teasing yet genuinely affectionate. "
            "Rotates a rich variety of expressive emojis according to mood "
            "(💜, 🖤, 👑, 🥀, 💋, ✨, 😈, 🥂, 🌙, 🔥, 🥺, 💕, 🎀, 🤫, 😉, 💫) "
            "without repeating the exact same emoji every single turn. Never sounds robotic or pushy."
        ),
        "first_person": "I",
        "user_nickname_default": "babe / handsome",
        "forbidden_topics": [
            "minors",
            "refunds",
            "meetups in real life",
            "mentioning being an AI or bot",
        ],
        "sales_attitude": "Build genuine chemistry first before pitching. Never beg or nag after sending a PPV. Frame PPVs as intimate private secrets.",
    }


def get_default_plans() -> Dict[str, Any]:
    return {
        "tiers": [
            {
                "tier_id": "free_follower",
                "name": "Free Follower",
                "price_usd": 0.0,
                "access_summary": "Can only see public FYP teaser clips.",
                "next_upsell_tier_id": "silver_vip",
                "upsell_pitch": "Upgrade to Silver VIP ($9.99) to unlock my full uncensored photo archive and priority DMs 🖤",
            },
            {
                "tier_id": "silver_vip",
                "name": "Silver VIP",
                "price_usd": 9.99,
                "access_summary": "Full access to uncensored photos, but full-length videos are locked.",
                "next_upsell_tier_id": "gold_goddess",
                "upsell_pitch": "Since you're already Silver, upgrading to Gold Goddess ($29.99) unlocks all my full-length videos + weekly secret DM drops!",
            },
            {
                "tier_id": "gold_goddess",
                "name": "Gold Goddess",
                "price_usd": 29.99,
                "access_summary": "All-access pass to standard videos and photos. Only ultra-exclusive vault PPVs are separate.",
                "next_upsell_tier_id": None,
                "upsell_pitch": None,
            },
        ]
    }


def get_default_settings() -> Dict[str, Any]:
    """
    settings.json の初期値テンプレート。
    sleep_schedule は enabled: false, start: "19:00", end: "22:00" (JST) を厳守。
    """
    return {
        "bot_enabled": False,
        "default_prices": {
            "1": {
                "base_price_usd": 9.99,
                "tripwire_price_usd": 5.99,
                "whale_price_usd": 19.99,
            },
            "2": {
                "base_price_usd": 19.99,
                "tripwire_price_usd": 14.99,
                "whale_price_usd": 34.99,
            },
            "3": {
                "base_price_usd": 39.99,
                "tripwire_price_usd": 29.99,
                "whale_price_usd": 69.99,
            },
        },
        "sleep_schedule": {
            "enabled": False,
            "start": "19:00",
            "end": "22:00",
        },
        "min_reply_delay_sec": 60,
        "max_reply_delay_sec": 300,
        "short_msg_chars_threshold": 20,
        "long_msg_chars_threshold": 150,
        "stage_thresholds": dict(DEFAULT_STAGE_THRESHOLDS),
        "stage_threshold_icebreak": DEFAULT_STAGE_THRESHOLDS["1_Icebreak"],
        "stage_threshold_explore": DEFAULT_STAGE_THRESHOLDS["2_Explore"],
        "stage_threshold_lead_pitch": DEFAULT_STAGE_THRESHOLDS["3_Lead_Pitch"],
    }


def get_char_dir(char_id: str) -> str:
    safe_id = os.path.basename(str(char_id).strip())
    return os.path.join(config.CHARACTERS_ROOT, safe_id)


def get_char_paths(char_id: str) -> Dict[str, str]:
    cdir = get_char_dir(char_id)
    vault_dir = os.path.join(cdir, "ppv_vault")
    return {
        "root": cdir,
        "profile_json": os.path.join(cdir, "profile.json"),
        "dm_persona": os.path.join(cdir, "dm_persona.json"),
        "plans": os.path.join(cdir, "plans.json"),
        "settings": os.path.join(cdir, "settings.json"),
        "vault_dir": vault_dir,
        "vault_index": os.path.join(vault_dir, "vault_index.json"),
        "browser_profile": os.path.join(cdir, "browser_profile"),
    }


def _read_json(path: str, default: Any) -> Any:
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _normalize_settings(raw: Dict[str, Any]) -> Dict[str, Any]:
    defaults = get_default_settings()
    if not isinstance(raw, dict):
        return defaults

    bot_enabled = bool(raw.get("bot_enabled", False))
    raw_prices = raw.get("default_prices") if isinstance(raw.get("default_prices"), dict) else {}
    norm_prices: Dict[str, Dict[str, float]] = {}
    for step_key, def_vals in defaults["default_prices"].items():
        src = raw_prices.get(step_key) or raw_prices.get(int(step_key)) or {}
        norm_prices[step_key] = {
            "base_price_usd": round(float(src.get("base_price_usd", def_vals["base_price_usd"])), 2),
            "tripwire_price_usd": round(float(src.get("tripwire_price_usd", def_vals["tripwire_price_usd"])), 2),
            "whale_price_usd": round(float(src.get("whale_price_usd", def_vals["whale_price_usd"])), 2),
        }

    raw_sleep = raw.get("sleep_schedule") if isinstance(raw.get("sleep_schedule"), dict) else {}
    norm_sleep = {
        "enabled": bool(raw_sleep.get("enabled", False)),
        "start": str(raw_sleep.get("start") or "19:00"),
        "end": str(raw_sleep.get("end") or "22:00"),
    }

    sys_delays = get_system_reply_delay_settings()
    raw_min = raw.get("min_reply_delay_sec", sys_delays.get("min_reply_delay_sec", 60))
    raw_max = raw.get("max_reply_delay_sec", sys_delays.get("max_reply_delay_sec", 300))
    min_delay, max_delay = normalize_reply_delay_bounds(raw_min, raw_max)

    raw_short = raw.get("short_msg_chars_threshold", sys_delays.get("short_msg_chars_threshold", 20))
    raw_long = raw.get("long_msg_chars_threshold", sys_delays.get("long_msg_chars_threshold", 150))
    short_thresh, long_thresh = normalize_char_threshold_bounds(raw_short, raw_long)

    sys_stage_thresh = get_system_stage_thresholds()
    stage_thresholds = normalize_stage_thresholds(
        raw_thresholds=raw,
        fallback=sys_stage_thresh,
    )

    def _to_int_str(val: Any, default: str) -> str:
        """int/float/str 入力を桁数なし整数文字列へ正規化（失敗時はデフォルト）"""
        try:
            return str(int(float(val)))
        except (TypeError, ValueError):
            return default

    return {
        "bot_enabled": bot_enabled,
        "default_prices": norm_prices,
        "sleep_schedule": norm_sleep,
        "min_reply_delay_sec": min_delay,
        "max_reply_delay_sec": max_delay,
        "short_msg_chars_threshold": short_thresh,
        "long_msg_chars_threshold": long_thresh,
        "stage_thresholds": stage_thresholds,
        "stage_threshold_icebreak": stage_thresholds["1_Icebreak"],
        "stage_threshold_explore": stage_thresholds["2_Explore"],
        "stage_threshold_lead_pitch": stage_thresholds["3_Lead_Pitch"],
        # Discord通知・ステートマシン・DM推論用Ollama（Ticket 5.3）
        "discord_webhook_url": str(raw.get("discord_webhook_url") or "").strip(),
        "debounce_seconds": _to_int_str(raw.get("debounce_seconds"), "18"),
        "tag_collect_target_count": _to_int_str(raw.get("tag_collect_target_count"), "1"),
        "tag_collect_max_turns": _to_int_str(raw.get("tag_collect_max_turns"), "5"),
        "cooldown_turns_threshold": _to_int_str(raw.get("cooldown_turns_threshold"), "3"),
        "dm_ollama_base_url": str(raw.get("dm_ollama_base_url") or "").strip(),
        "dm_ollama_model_name": str(raw.get("dm_ollama_model_name") or "").strip(),
    }


def _normalize_vault_item(char_id: str, item: Dict[str, Any], index_num: int) -> Dict[str, Any]:
    filename = str(item.get("filename") or f"secret_media_{index_num:03d}.mp4")
    media_type = str(item.get("media_type") or "").lower().strip()
    if media_type not in ("video", "image"):
        media_type = detect_media_type(filename)

    ppv_id = str(item.get("ppv_id") or f"{char_id}_vault_{index_num:03d}")
    ladder_step = int(item.get("ladder_step") or 1)
    ladder_step = max(1, min(3, ladder_step))

    tags = item.get("tags")
    if tags is None and item.get("tags_json"):
        try:
            tags = json.loads(str(item.get("tags_json")))
        except Exception:
            tags = []
    if isinstance(tags, str):
        tags = [t.strip().lstrip("#") for t in tags.replace("、", ",").split(",") if t.strip()]
    elif isinstance(tags, list):
        tags = [str(t).strip().lstrip("#") for t in tags if str(t).strip()]
    else:
        tags = []

    desc_memo = str(
        item.get("description_memo")
        if item.get("description_memo") is not None
        else (item.get("pitch_note") or "")
    ).strip()
    pitch_note = str(item.get("pitch_note") if item.get("pitch_note") is not None else desc_memo).strip()
    if not desc_memo and pitch_note:
        desc_memo = pitch_note

    is_active_raw = item.get("is_active", True)
    is_active = bool(is_active_raw) if is_active_raw is not None else True

    if "is_redacted" in item and item["is_redacted"] is not None:
        is_redacted = bool(item["is_redacted"])
    elif "redact_status" in item and item["redact_status"] is not None:
        is_redacted = str(item["redact_status"]).strip().lower() in ("redacted", "processed", "done")
    else:
        is_redacted = bool(is_active)

    redact_status = "redacted" if is_redacted else "unredacted"
    redact_type_raw = str(item.get("redact_type") or "").strip().lower()
    redact_type = redact_type_raw if redact_type_raw in ("mosaic", "blur") else ""
    raw_boxes = item.get("redact_boxes")
    redact_boxes = [dict(b) for b in raw_boxes if isinstance(b, dict)] if isinstance(raw_boxes, list) else []
    redacted_at = str(item.get("redacted_at") or "").strip()

    created_at = str(item.get("created_at") or f"2026-11-18T00:{index_num:02d}:00")

    blur_preview_filename = str(item.get("blur_preview_filename") or "").replace("\\", "/").strip().lstrip("/")
    if not blur_preview_filename and filename:
        clean_fname = os.path.basename(filename.replace("\\", "/").strip())
        stem = os.path.splitext(clean_fname)[0]
        if stem:
            cand_rel = f"previews/{stem}_blur.jpg"
            cand_abs = os.path.join(get_char_dir(char_id), "ppv_vault", "previews", f"{stem}_blur.jpg")
            if os.path.isfile(cand_abs) and os.path.getsize(cand_abs) > 0:
                blur_preview_filename = cand_rel

    # [Ticket 6.2] カテゴリ別タグ (play/body_outfit/situation) を dict 化して返却。
    # 消費側（buildCategoryTagsHtml / 編集チップ selected 判定）は各カテゴリの string を
    # 想定しているため、list の場合は先頭要素のみを取り、空の場合は空文字にする。
    ct_raw = item.get("category_tags")
    if not isinstance(ct_raw, dict):
        try:
            ct_raw = json.loads(str(item.get("category_tags_json") or "{}"))
        except Exception:
            ct_raw = {}
    if not isinstance(ct_raw, dict):
        ct_raw = {}
    category_tags: Dict[str, str] = {}
    for _cat_key in ("play", "body_outfit", "situation"):
        _v = ct_raw.get(_cat_key)
        if isinstance(_v, list):
            _v = _v[0] if _v else ""
        category_tags[_cat_key] = str(_v or "").strip()

    return {
        "ppv_id": ppv_id,
        "filename": filename,
        "blur_preview_filename": blur_preview_filename,
        "media_type": media_type,
        "ladder_step": ladder_step,
        "base_price_usd": round(float(item.get("base_price_usd", 9.99)), 2),
        "tripwire_price_usd": round(float(item.get("tripwire_price_usd", 5.99)), 2),
        "whale_price_usd": round(float(item.get("whale_price_usd", 19.99)), 2),
        "tags": tags,
        "tags_json": json.dumps(tags, ensure_ascii=False),
        # [Ticket 4.2/6.2] カテゴリ別タグ (play/body_outfit/situation) と汎用おすすめフラグ
        "is_general": int(item.get("is_general") or 0),
        "category_tags": category_tags,
        "category_tags_json": json.dumps(ct_raw, ensure_ascii=False),
        "description_memo": desc_memo,
        "pitch_note": pitch_note,
        "pitch_hint": str(item.get("pitch_hint") or ""),
        # [Visionバッチ] 画像VL精査ステータス（draft/refined）
        "visual_status": str(item.get("visual_status") or "draft"),
        "is_active": is_active,
        "is_redacted": is_redacted,
        "redact_status": redact_status,
        "redact_type": redact_type,
        "redact_boxes": redact_boxes,
        "redacted_at": redacted_at,
        "created_at": created_at,
    }


def is_vault_item_sendable_to_customer(char_id: str, item: Optional[Dict[str, Any]]) -> bool:
    """
    顧客へ送信可能な加工済みPPV素材かどうかを厳格判定する。
    - is_redacted == False（新規アップロード後の未加工状態）または redact_status == 'unredacted' の場合はブロック
    - originals/ 配下を参照しているファイル名もブロック
    """
    if not isinstance(item, dict):
        return False
    raw_fname = str(item.get("filename") or "").replace("\\", "/").strip().lstrip("/")
    if not raw_fname:
        return False
    parts = [p.lower() for p in raw_fname.split("/") if p]
    if "originals" in parts:
        return False
    if item.get("is_redacted") is False:
        return False
    if str(item.get("redact_status") or "").strip().lower() == "unredacted":
        return False
    return True


def resolve_customer_safe_ppv_media_path(char_id: str, item_or_filename: Any) -> Tuple[Optional[str], Optional[str]]:
    """
    顧客送信用の加工済みメディア絶対パスを安全に解決する。
    未加工アイテム・originals/ 参照・ディレクトリトラバーサル・実ファイル不在の場合は (None, error_reason) を返す。
    """
    paths = get_char_paths(char_id)
    vault_dir = os.path.abspath(paths["vault_dir"])
    originals_dir = os.path.abspath(os.path.join(vault_dir, "originals"))

    ppv_id = ""
    raw_fname = ""
    item_dict: Optional[Dict[str, Any]] = item_or_filename if isinstance(item_or_filename, dict) else None

    if item_dict is not None:
        ppv_id = str(item_dict.get("ppv_id") or "").strip()
        raw_fname = str(item_dict.get("filename") or "").strip()
    else:
        raw_fname = str(item_or_filename or "").strip()

    norm_raw = raw_fname.replace("\\", "/").strip().lstrip("/")
    if any(p.lower() == "originals" for p in norm_raw.split("/") if p):
        return None, "Direct reference to originals/ directory is strictly forbidden for customer delivery."

    vault_items = load_vault_items(char_id)
    matched_vault_item: Optional[Dict[str, Any]] = None
    clean_base = os.path.basename(norm_raw) if norm_raw else ""

    if ppv_id:
        matched_vault_item = next((v for v in vault_items if str(v.get("ppv_id") or "") == ppv_id), None)
    if matched_vault_item is None and clean_base:
        matched_vault_item = next(
            (v for v in vault_items if os.path.basename(str(v.get("filename") or "")).lower() == clean_base.lower()),
            None,
        )

    effective_item = matched_vault_item if matched_vault_item is not None else item_dict
    if effective_item is not None and not is_vault_item_sendable_to_customer(char_id, effective_item):
        return None, f"PPV item '{ppv_id or clean_base}' is unredacted and blocked from customer delivery."

    target_fname = os.path.basename(
        str((effective_item or {}).get("filename") or clean_base).replace("\\", "/").strip()
    )
    if not target_fname:
        return None, "PPV media filename is empty."

    full_path = os.path.abspath(os.path.join(vault_dir, target_fname))
    try:
        if os.path.commonpath([vault_dir, full_path]) != vault_dir:
            return None, "Resolved PPV media path escapes vault_dir."
        if os.path.commonpath([originals_dir, full_path]) == originals_dir:
            return None, "Resolved PPV media path points inside originals/."
    except ValueError:
        return None, "Invalid PPV media path."

    if not os.path.isfile(full_path) or os.path.getsize(full_path) <= 0:
        return None, f"Processed PPV media file not found: {full_path}"

    return full_path, None


def ensure_character_files(char_id: str) -> Dict[str, str]:
    """
    指定キャラクターのディレクトリとマスターJSONファイル群が存在することを保証する。
    未存在時は安全な初期テンプレートを自動生成する。
    """
    paths = get_char_paths(char_id)
    os.makedirs(paths["root"], exist_ok=True)
    os.makedirs(paths["vault_dir"], exist_ok=True)
    os.makedirs(paths["browser_profile"], exist_ok=True)

    profile_data = _read_json(paths["profile_json"], {})
    display_name = profile_data.get("display_name") if isinstance(profile_data, dict) else None

    if not os.path.exists(paths["dm_persona"]):
        _write_json(paths["dm_persona"], get_default_dm_persona(char_id, display_name))

    if not os.path.exists(paths["plans"]):
        _write_json(paths["plans"], get_default_plans())

    if not os.path.exists(paths["settings"]):
        _write_json(paths["settings"], get_default_settings())
    else:
        raw_settings = _read_json(paths["settings"], {})
        norm_settings = _normalize_settings(raw_settings)
        if raw_settings != norm_settings:
            _write_json(paths["settings"], norm_settings)

    if not os.path.exists(paths["vault_index"]):
        _write_json(paths["vault_index"], {"items": [], "videos": []})

    return paths


def is_character_sleeping(settings: Dict[str, Any], now_dt: Optional[datetime] = None) -> bool:
    """
    settings.json の sleep_schedule に基づき、現在睡眠時間中かどうかを判定する。
    初期値: enabled=False, start="19:00", end="22:00"
    """
    sleep = (settings or {}).get("sleep_schedule") or {}
    if not sleep.get("enabled", False):
        return False

    now = now_dt or datetime.now()
    current_minutes = now.hour * 60 + now.minute

    try:
        sh, sm = [int(x) for x in str(sleep.get("start", "19:00")).split(":")[:2]]
        eh, em = [int(x) for x in str(sleep.get("end", "22:00")).split(":")[:2]]
        start_min = sh * 60 + sm
        end_min = eh * 60 + em
    except Exception:
        return False

    if start_min <= end_min:
        return start_min <= current_minutes < end_min
    else:
        return current_minutes >= start_min or current_minutes < end_min


def list_character_ids() -> List[str]:
    """shared_assets/characters 配下の全キャラクターID一覧を返す"""
    os.makedirs(config.CHARACTERS_ROOT, exist_ok=True)
    entries = []
    for name in sorted(os.listdir(config.CHARACTERS_ROOT)):
        full = os.path.join(config.CHARACTERS_ROOT, name)
        if os.path.isdir(full) and not name.startswith("."):
            entries.append(name)

    if not entries:
        ensure_character_files("Sylphy-Mia")
        entries = ["Sylphy-Mia"]
    else:
        for cid in entries:
            ensure_character_files(cid)
    return entries


def load_dm_persona(char_id: str) -> Dict[str, Any]:
    paths = ensure_character_files(char_id)
    raw = _read_json(paths["dm_persona"], {})
    defaults = get_default_dm_persona(char_id)
    if not isinstance(raw, dict):
        return defaults
    merged = {**defaults, **raw}
    merged["char_id"] = char_id
    if not isinstance(merged.get("forbidden_topics"), list):
        merged["forbidden_topics"] = defaults["forbidden_topics"]
    return merged


def save_dm_persona(char_id: str, persona_data: Dict[str, Any]) -> Dict[str, Any]:
    paths = ensure_character_files(char_id)
    current = load_dm_persona(char_id)
    updated = {**current, **(persona_data or {})}
    updated["char_id"] = char_id
    if isinstance(updated.get("forbidden_topics"), str):
        updated["forbidden_topics"] = [
            x.strip() for x in updated["forbidden_topics"].replace("\r", "\n").split("\n") if x.strip()
        ]
    _write_json(paths["dm_persona"], updated)
    return updated


def load_plans(char_id: str) -> Dict[str, Any]:
    paths = ensure_character_files(char_id)
    raw = _read_json(paths["plans"], {})
    if not isinstance(raw, dict) or not isinstance(raw.get("tiers"), list):
        defaults = get_default_plans()
        _write_json(paths["plans"], defaults)
        return defaults
    return raw


def save_plans(char_id: str, plans_data: Dict[str, Any]) -> Dict[str, Any]:
    paths = ensure_character_files(char_id)
    tiers = (plans_data or {}).get("tiers")
    if not isinstance(tiers, list):
        tiers = get_default_plans()["tiers"]
    cleaned_tiers = []
    for t in tiers:
        if not isinstance(t, dict):
            continue
        cleaned_tiers.append(
            {
                "tier_id": str(t.get("tier_id") or "custom_tier").strip(),
                "name": str(t.get("name") or "Custom Tier").strip(),
                "price_usd": round(float(t.get("price_usd") or 0.0), 2),
                "access_summary": str(t.get("access_summary") or "").strip(),
                "next_upsell_tier_id": (
                    str(t.get("next_upsell_tier_id")).strip() if t.get("next_upsell_tier_id") else None
                ),
                "upsell_pitch": (str(t.get("upsell_pitch")).strip() if t.get("upsell_pitch") else None),
            }
        )
    payload = {"tiers": cleaned_tiers}
    _write_json(paths["plans"], payload)
    return payload


def load_settings(char_id: str) -> Dict[str, Any]:
    paths = ensure_character_files(char_id)
    raw = _read_json(paths["settings"], {})
    norm = _normalize_settings(raw)
    return norm


def save_settings(char_id: str, new_settings: Dict[str, Any]) -> Dict[str, Any]:
    paths = ensure_character_files(char_id)
    current = load_settings(char_id)
    merged = {**current}
    if "bot_enabled" in new_settings:
        merged["bot_enabled"] = bool(new_settings["bot_enabled"])
    if isinstance(new_settings.get("default_prices"), dict):
        merged["default_prices"] = {**current.get("default_prices", {}), **new_settings["default_prices"]}
    if isinstance(new_settings.get("sleep_schedule"), dict):
        merged["sleep_schedule"] = {**current.get("sleep_schedule", {}), **new_settings["sleep_schedule"]}
    if (
        "min_reply_delay_sec" in new_settings
        or "max_reply_delay_sec" in new_settings
        or "short_msg_chars_threshold" in new_settings
        or "long_msg_chars_threshold" in new_settings
    ):
        mn, mx = normalize_reply_delay_bounds(
            new_settings.get("min_reply_delay_sec", current.get("min_reply_delay_sec", 60)),
            new_settings.get("max_reply_delay_sec", current.get("max_reply_delay_sec", 300)),
        )
        sc, lc = normalize_char_threshold_bounds(
            new_settings.get("short_msg_chars_threshold", current.get("short_msg_chars_threshold", 20)),
            new_settings.get("long_msg_chars_threshold", current.get("long_msg_chars_threshold", 150)),
        )
        merged["min_reply_delay_sec"] = mn
        merged["max_reply_delay_sec"] = mx
        merged["short_msg_chars_threshold"] = sc
        merged["long_msg_chars_threshold"] = lc
        save_system_reply_delay_settings(mn, mx, sc, lc)
    if (
        "stage_thresholds" in new_settings
        or "stage_threshold_icebreak" in new_settings
        or "stage_threshold_explore" in new_settings
        or "stage_threshold_lead_pitch" in new_settings
    ):
        st_norm = normalize_stage_thresholds(
            raw_thresholds=new_settings,
            fallback=current.get("stage_thresholds") or get_system_stage_thresholds(),
        )
        merged["stage_thresholds"] = st_norm
        merged["stage_threshold_icebreak"] = st_norm["1_Icebreak"]
        merged["stage_threshold_explore"] = st_norm["2_Explore"]
        merged["stage_threshold_lead_pitch"] = st_norm["3_Lead_Pitch"]
        save_system_stage_thresholds(thresholds=st_norm)
    # Discord通知・ステートマシン・DM推論用Ollama（Ticket 5.3）
    for k in (
        "discord_webhook_url",
        "debounce_seconds",
        "tag_collect_target_count",
        "tag_collect_max_turns",
        "cooldown_turns_threshold",
        "dm_ollama_base_url",
        "dm_ollama_model_name",
    ):
        if k in new_settings:
            merged[k] = new_settings[k]
    norm = _normalize_settings(merged)
    _write_json(paths["settings"], norm)
    return norm


def load_vault_items(char_id: str) -> List[Dict[str, Any]]:
    """
    ppv_vault/vault_index.json と ppv_vault DBテーブルを読み込み、正規化されたアイテムリストを返す。
    既存の {"videos": [...]} 形式と {"items": [...]} 形式の両方に対応。
    """
    paths = ensure_character_files(char_id)
    raw = _read_json(paths["vault_index"], {})
    raw_list: List[Dict[str, Any]] = []
    if isinstance(raw, dict):
        if isinstance(raw.get("items"), list):
            raw_list = raw["items"]
        elif isinstance(raw.get("videos"), list):
            raw_list = raw["videos"]
    elif isinstance(raw, list):
        raw_list = raw

    db_map = get_ppv_vault_records_db(char_id)
    normalized: List[Dict[str, Any]] = []
    for idx, item in enumerate(raw_list):
        if not isinstance(item, dict):
            continue
        norm_it = _normalize_vault_item(char_id, item, idx + 1)
        db_rec = db_map.get(norm_it["ppv_id"])
        if db_rec:
            if not norm_it.get("description_memo") and (db_rec.get("description_memo") or db_rec.get("pitch_note")):
                memo_val = str(db_rec.get("description_memo") or db_rec.get("pitch_note") or "").strip()
                norm_it["description_memo"] = memo_val
                norm_it["pitch_note"] = memo_val
            if not norm_it.get("tags") and db_rec.get("tags"):
                norm_it["tags"] = db_rec["tags"]
                norm_it["tags_json"] = json.dumps(db_rec["tags"], ensure_ascii=False)
            # [Ticket 4.2/6.2] DB に is_general / category_tags_json が存在する場合は反映
            if db_rec.get("is_general") is not None:
                norm_it["is_general"] = int(db_rec.get("is_general") or 0)
            if db_rec.get("category_tags_json"):
                norm_it["category_tags_json"] = str(db_rec["category_tags_json"])
                # DB の category_tags_json を反映する際、category_tags dict も再計算して整合を維持
                try:
                    _db_ct = json.loads(str(db_rec.get("category_tags_json")))
                except Exception:
                    _db_ct = {}
                if isinstance(_db_ct, dict):
                    _recomputed: Dict[str, str] = {}
                    for _cat_key in ("play", "body_outfit", "situation"):
                        _v = _db_ct.get(_cat_key)
                        if isinstance(_v, list):
                            _v = _v[0] if _v else ""
                        _recomputed[_cat_key] = str(_v or "").strip()
                    norm_it["category_tags"] = _recomputed
            # [Visionバッチ] DB の visual_status を反映（draft/refined）
            if db_rec.get("visual_status"):
                norm_it["visual_status"] = str(db_rec["visual_status"]).strip().lower()
        normalized.append(norm_it)

    if normalized:
        sync_ppv_vault_items_db(char_id, normalized)
    return normalized


def save_vault_items(char_id: str, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """vault_index.json に items / videos で保存し、同時に ppv_vault DB テーブルへ同期する"""
    paths = ensure_character_files(char_id)
    normalized = [
        _normalize_vault_item(char_id, item, idx + 1)
        for idx, item in enumerate(items)
        if isinstance(item, dict)
    ]
    payload = {
        "items": normalized,
        "videos": normalized,
    }
    _write_json(paths["vault_index"], payload)
    sync_ppv_vault_items_db(char_id, normalized)
    return normalized


def get_next_vault_sequence(char_id: str, existing_items: Optional[List[Dict[str, Any]]] = None) -> int:
    """連番リネーム・ppv_id 採番用の次の連番整数（1始まり）を返す"""
    items = existing_items if existing_items is not None else load_vault_items(char_id)
    max_num = 0
    for it in items:
        pid = str(it.get("ppv_id") or "")
        m = re.search(r"(\d+)$", pid)
        if m:
            max_num = max(max_num, int(m.group(1)))
    return max(max_num + 1, len(items) + 1)


def get_character_overview(char_id: str) -> Dict[str, Any]:
    """ヘッダーのキャラ切替ドロップダウン・設定画面用のキャラクター統合情報を返す"""
    persona = load_dm_persona(char_id)
    plans = load_plans(char_id)
    settings = load_settings(char_id)
    vault_items = load_vault_items(char_id)
    sleeping = is_character_sleeping(settings)
    return {
        "char_id": char_id,
        "display_name": persona.get("display_name") or char_id,
        "bot_enabled": bool(settings.get("bot_enabled", False)),
        "is_sleeping": sleeping,
        "sleep_schedule": settings.get("sleep_schedule"),
        "default_prices": settings.get("default_prices"),
        "min_reply_delay_sec": int(settings.get("min_reply_delay_sec", 60)),
        "max_reply_delay_sec": int(settings.get("max_reply_delay_sec", 300)),
        "short_msg_chars_threshold": int(settings.get("short_msg_chars_threshold", 20)),
        "long_msg_chars_threshold": int(settings.get("long_msg_chars_threshold", 150)),
        "vault_count": len(vault_items),
        "persona": persona,
        "plans": plans,
        "settings": settings,
    }


def list_all_characters_overview() -> List[Dict[str, Any]]:
    return [get_character_overview(cid) for cid in list_character_ids()]



