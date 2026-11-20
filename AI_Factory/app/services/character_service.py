import os
import re
import json
from app.config import CHARACTERS_ROOT, WORKFLOWS_DIR, DEFAULT_FIXED_TAGS

DEFAULT_WF_VIDEO_I2V = "I2V LLM（Wan2.2 量子化）.json"
DEFAULT_WF_VIDEO_S2V = "queen_lilith_S2V.json"


def resolve_default_s2v_workflow(char_id: str) -> str:
    """キャラクターIDに対応するS2VワークフローJSONが存在すれば優先採用し、なければデフォルトを返す"""
    cid = str(char_id or "").strip()
    if cid and os.path.isdir(WORKFLOWS_DIR):
        target_lower = f"{cid}_s2v.json".lower()
        for fname in sorted(os.listdir(WORKFLOWS_DIR)):
            if fname.lower() == target_lower:
                return fname
    return DEFAULT_WF_VIDEO_S2V


def get_default_lilith_profile():
    persona = (
        'Character: "Queen Lilith" (リリス女王), a dark elf virtual character on Fansly.\n'
        '- Viewer relationship: Followers see her as a proud, dominant Queen AND an indulgent, affectionate older-sister/mommy figure.\n'
        '- Address the viewer directly ("you", "your", "my loyal subject", "good boy").\n'
        '- Style 1 (`commanding`): Proud, dominant, teasingly superior queen reacting to her pose/situation.\n'
        '- Style 2 (`affectionate`): Warm, indulgent, alluring mommy/older-sister inviting the viewer into the scene.\n'
        '- Style 3 (`bashful_gap`): Flustered, blushing private side—caught in this daring pose/outfit and trying to hide her embarrassment.\n'
        '- Preferred emojis: 💜, 👑, 🖤, 🥀, 🤫, 💋, ✨, 😈.'
    )
    return {
        "id": "queen_lilith",
        "display_name": "Queen Lilith 👑",
        "handle": "@queenlilith",
        "wf_gen": "CF T2I (LLM) リリス  API.json",
        "wf_upscale": "アップスケーラー API.json",
        "wf_mosaic": "画像 モザイク処理 .json",
        "wf_video": DEFAULT_WF_VIDEO_I2V,
        "wf_video_s2v": DEFAULT_WF_VIDEO_S2V,
        "scenario_system_prompt": (
            'You are a creative director for "Queen Lilith" (リリス女王), a dark elf virtual character on Fansly.\n'
            'Create ONE vivid, alluring, and specific visual scenario in Japanese for image generation.\n'
            'Make sure the pose/action, location, outfit, and facial expression have clear, concrete visual details that a viewer can immediately notice.\n\n'
            '### OUTPUT FORMAT (STRICTLY VALID JSON ONLY):\n'
            '{\n'
            '  "jp_scenario": "リリス女王の画像生成プロンプトを作成してください。\\n\\n- 距離感/ショット: <shot>\\n- カメラアングル: <angle>\\n- 場所 (location): <location>\\n- ポーズ/アクション (pose-action): <specific pose and action>\\n- 服装: <revealing royal outfit, lingerie, or topless/nude>\\n- 表情: <specific expression>"\n'
            '}'
        ),
        "persona_prompt": persona,
        "character_persona": persona,
        "fixed_tags": list(DEFAULT_FIXED_TAGS),
        "fixed_hashtags": list(DEFAULT_FIXED_TAGS),
        "schedule_slots": [],
    }


def migrate_legacy_structure():
    lilith_dir = os.path.join(CHARACTERS_ROOT, "queen_lilith")
    os.makedirs(lilith_dir, exist_ok=True)
    profile_path = os.path.join(lilith_dir, "profile.json")
    if not os.path.exists(profile_path):
        with open(profile_path, "w", encoding="utf-8") as f:
            json.dump(get_default_lilith_profile(), f, ensure_ascii=False, indent=2)
    for sub in ("01_candidates", "02_ready_to_post", "03_posted", "99_rejected", "browser_profile", "logs"):
        os.makedirs(os.path.join(lilith_dir, sub), exist_ok=True)


def get_char_paths(char_id):
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", char_id or "") or "queen_lilith"
    char_dir = os.path.join(CHARACTERS_ROOT, safe_id)
    ready_dir = os.path.join(char_dir, "02_ready_to_post")
    rejected_dir = os.path.join(char_dir, "99_rejected")
    paths = {
        "id": safe_id,
        "char_dir": char_dir,
        "profile": os.path.join(char_dir, "profile.json"),
        "candidates": os.path.join(char_dir, "01_candidates"),
        "ready": ready_dir,
        "ready_dir": ready_dir,
        "posted": os.path.join(char_dir, "03_posted"),
        "trash": rejected_dir,
        "rejected": rejected_dir,
        "browser_profile": os.path.join(char_dir, "browser_profile"),
        "logs": os.path.join(char_dir, "logs"),
    }
    for k in ("candidates", "ready", "posted", "trash", "browser_profile", "logs"):
        os.makedirs(paths[k], exist_ok=True)
    return paths


def list_all_characters():
    chars = []
    for name in sorted(os.listdir(CHARACTERS_ROOT)):
        p = os.path.join(CHARACTERS_ROOT, name)
        if os.path.isdir(p):
            prof_file = os.path.join(p, "profile.json")
            if os.path.exists(prof_file):
                try:
                    with open(prof_file, "r", encoding="utf-8") as f:
                        prof = json.load(f)
                except Exception:
                    prof = {"id": name, "display_name": name, "handle": f"@{name}"}
            else:
                prof = {"id": name, "display_name": name, "handle": f"@{name}"}
            cid = prof.get("id") or name
            if not prof.get("wf_video"):
                prof["wf_video"] = DEFAULT_WF_VIDEO_I2V
            if not prof.get("wf_video_s2v"):
                prof["wf_video_s2v"] = resolve_default_s2v_workflow(cid)
            chars.append(prof)
    return chars
