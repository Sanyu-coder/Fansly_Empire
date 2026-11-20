import os
import json

# ==========================================
# ★ 総督の環境設定エリア ★
# ==========================================
BASE_DIR = os.environ.get("AI_FACTORY_BASE_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHARED_ASSETS_DIR = os.environ.get("SHARED_ASSETS_DIR") or r"C:\Fansly_Empire\shared_assets"
CHARACTERS_ROOT = os.path.join(SHARED_ASSETS_DIR, "characters")
WORKFLOWS_DIR = os.path.join(BASE_DIR, "workflows")

LINE1_SCRIPT = os.path.join(BASE_DIR, "factory_line1.py")
LINE2_SCRIPT = os.path.join(BASE_DIR, "factory_line2.py")
VIDEO_SCRIPT = os.path.join(BASE_DIR, "factory_video.py")

LINE1_MAX_REPEAT = 30
ALL_CHARS_SENTINEL = "ALL"

APP_SETTINGS_PATH = os.path.join(BASE_DIR, "app_settings.json")
LINE2_HEADLESS_DEFAULT = False

PORT = 5050

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434/api/generate")
OLLAMA_MODEL_TEXT = os.environ.get("OLLAMA_MODEL_TEXT", "my-qwen3:latest")

DEFAULT_FIXED_TAGS = [
    "#anime", "#vtuber", "#lewdtuber", "#animegirl", "#waifu",
    "#egirl", "#MonsterGirl", "#Anifans", "#Fendom", "#findom",
    "#2dfd", "#2D", "#Mommy"
]
# ==========================================


def ensure_directories():
    os.makedirs(CHARACTERS_ROOT, exist_ok=True)
    os.makedirs(WORKFLOWS_DIR, exist_ok=True)


def parse_bool_flag(val, default: bool = False) -> bool:
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    s = str(val).strip().lower()
    if s in ("1", "true", "yes", "on", "headless"):
        return True
    if s in ("0", "false", "no", "off", "headed", "gui"):
        return False
    return default


def load_app_settings() -> dict:
    """アプリ共通設定（Line 2 ヘッドレスモード等）を app_settings.json から読み込む"""
    defaults = {
        "line2_headless": LINE2_HEADLESS_DEFAULT,
    }
    if os.path.exists(APP_SETTINGS_PATH):
        try:
            with open(APP_SETTINGS_PATH, "r", encoding="utf-8") as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                merged = dict(defaults)
                merged.update(saved)
                if "line2_headless" in saved:
                    merged["line2_headless"] = parse_bool_flag(saved.get("line2_headless"), LINE2_HEADLESS_DEFAULT)
                elif "headless" in saved:
                    merged["line2_headless"] = parse_bool_flag(saved.get("headless"), LINE2_HEADLESS_DEFAULT)
                return merged
        except Exception:
            pass
    return defaults


def save_app_settings(updates: dict) -> dict:
    """アプリ共通設定をマージして app_settings.json に永続化する"""
    current = load_app_settings()
    if isinstance(updates, dict):
        for k, v in updates.items():
            if k in ("line2_headless", "headless"):
                current["line2_headless"] = parse_bool_flag(v, current.get("line2_headless", LINE2_HEADLESS_DEFAULT))
            else:
                current[k] = v
    os.makedirs(os.path.dirname(APP_SETTINGS_PATH), exist_ok=True)
    with open(APP_SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(current, f, ensure_ascii=False, indent=2)
    return current


def get_line2_headless() -> bool:
    """永続化された第2ライン（Playwright）のヘッドレス設定を取得する"""
    return bool(load_app_settings().get("line2_headless", LINE2_HEADLESS_DEFAULT))


def set_line2_headless(headless) -> bool:
    """第2ライン（Playwright）のヘッドレス設定を更新・保存する"""
    flag = parse_bool_flag(headless, LINE2_HEADLESS_DEFAULT)
    saved = save_app_settings({"line2_headless": flag})
    return bool(saved.get("line2_headless", flag))
