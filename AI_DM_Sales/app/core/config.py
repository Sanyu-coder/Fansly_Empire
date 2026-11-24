import os

# ==========================================
# AI_DM_Sales コア環境設定 (Port: 5051)
# ==========================================
BASE_DIR = os.environ.get("AI_DM_SALES_BASE_DIR") or os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

SHARED_ASSETS_DIR = os.environ.get("SHARED_ASSETS_DIR") or r"C:\Fansly_Empire\shared_assets"
CHARACTERS_ROOT = os.environ.get("CHARACTERS_ROOT") or os.path.join(SHARED_ASSETS_DIR, "characters")
DATABASE_DIR = os.path.join(SHARED_ASSETS_DIR, "database")
DB_PATH = os.environ.get("DB_PATH") or os.path.join(DATABASE_DIR, "empire_core.db")
PROFILES_DIR = os.environ.get("PROFILES_DIR") or os.path.join(BASE_DIR, "profiles")

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1")
MODEL_NAME = os.environ.get("MODEL_NAME", "my-qwen3:latest")

# llama.cpp サーバー（Ollama 未起動・VRAM 競合時に優先的に割り込んで使用）
# OpenAI 互換 /v1/chat/completions 対応。私を動かしている llama.cpp server と同一経路。
LLAMA_BASE_URL = os.environ.get("LLAMA_BASE_URL", "http://127.0.0.1:8080/v1")
LLAMA_MODEL_NAME = os.environ.get("LLAMA_MODEL_NAME", "qwen3.8-27b-opencode-104k")

PORT = int(os.environ.get("PORT", "5051"))

# 危険ワード（検知時に即座に is_escalated = 1 へ移行）
DANGER_KEYWORDS = [
    "refund",
    "chargeback",
    "scam",
    "fraud",
    "police",
    "lawyer",
    "underage",
    "minor",
    "child",
    "bot",
    "ai",
    "artificial intelligence",
]


def ensure_directories():
    """アプリケーション稼働に必要なディレクトリを自動作成する"""
    os.makedirs(SHARED_ASSETS_DIR, exist_ok=True)
    os.makedirs(CHARACTERS_ROOT, exist_ok=True)
    os.makedirs(DATABASE_DIR, exist_ok=True)
    os.makedirs(PROFILES_DIR, exist_ok=True)
