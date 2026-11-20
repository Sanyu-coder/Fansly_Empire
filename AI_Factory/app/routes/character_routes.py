import os
import re
import json
from flask import Blueprint, request, jsonify
from app.config import CHARACTERS_ROOT, WORKFLOWS_DIR, DEFAULT_FIXED_TAGS
from app.services.character_service import (
    get_char_paths,
    list_all_characters,
    resolve_default_s2v_workflow,
    DEFAULT_WF_VIDEO_I2V,
)

character_bp = Blueprint("character", __name__)


@character_bp.route("/api/characters")
def api_characters():
    wfs = sorted([f for f in os.listdir(WORKFLOWS_DIR) if f.lower().endswith(".json")])
    return jsonify({"characters": list_all_characters(), "workflows": wfs})


@character_bp.route("/api/characters/create", methods=["POST"])
def api_characters_create():
    data = request.json or {}
    raw_id = data.get("id", "").strip()
    if not re.match(r"^[a-zA-Z0-9_-]+$", raw_id):
        return jsonify({"ok": False, "error": "キャラIDは半角英数字・アンダースコア・ハイフンのみ使用できます"})

    char_dir = os.path.join(CHARACTERS_ROOT, raw_id)
    if os.path.exists(char_dir):
        return jsonify({"ok": False, "error": "そのキャラIDはすでに存在します"})

    paths = get_char_paths(raw_id)
    display_name = data.get("display_name", raw_id).strip()
    handle = data.get("handle", f"@{raw_id}").strip()
    wf_gen = (data.get("wf_gen") or "").strip() or "CF T2I (LLM) リリス  API.json"
    wf_video = (data.get("wf_video") or "").strip() or DEFAULT_WF_VIDEO_I2V
    wf_video_s2v = (data.get("wf_video_s2v") or "").strip() or resolve_default_s2v_workflow(raw_id)
    persona = (
        data.get("persona_prompt")
        or data.get("character_persona")
        or ""
    ).strip() or (
        f'Character: "{display_name}", a virtual creator on Fansly.\n'
        '- Address the viewer directly ("you", "your", "dear").\n'
        '- Style 1 (`commanding`): Confident, playful, teasingly superior.\n'
        '- Style 2 (`affectionate`): Warm, sweet, inviting the viewer closer.\n'
        '- Style 3 (`bashful_gap`): Flustered, blushing, caught in an intimate moment.'
    )

    scenario_sys = (
        f'You are a creative director for "{display_name}", a virtual character on Fansly.\n'
        'Create ONE vivid, alluring, and specific visual scenario in Japanese for image generation.\n'
        'Make sure the pose/action, location, outfit, and facial expression have clear, concrete visual details that a viewer can immediately notice.\n\n'
        '### OUTPUT FORMAT (STRICTLY VALID JSON ONLY):\n'
        '{\n'
        f'  "jp_scenario": "{display_name}の画像生成プロンプトを作成してください。\\n\\n- 距離感/ショット: <shot>\\n- カメラアングル: <angle>\\n- 場所 (location): <location>\\n- ポーズ/アクション (pose-action): <specific pose and action>\\n- 服装: <outfit>\\n- 表情: <specific expression>"\n'
        '}'
    )

    fixed_tags = data.get("fixed_tags") or data.get("fixed_hashtags") or list(DEFAULT_FIXED_TAGS)
    schedule_slots = data.get("schedule_slots") if isinstance(data.get("schedule_slots"), list) else []

    profile = {
        "id": raw_id,
        "display_name": display_name,
        "handle": handle,
        "wf_gen": wf_gen,
        "wf_upscale": "アップスケーラー API.json",
        "wf_mosaic": "画像 モザイク処理 .json",
        "wf_video": wf_video,
        "wf_video_s2v": wf_video_s2v,
        "scenario_system_prompt": scenario_sys,
        "persona_prompt": persona,
        "character_persona": persona,
        "fixed_tags": fixed_tags,
        "fixed_hashtags": fixed_tags,
        "schedule_slots": schedule_slots[:10],
    }
    with open(paths["profile"], "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=2)

    return jsonify({"ok": True, "char_id": raw_id})


@character_bp.route("/api/characters/update", methods=["POST"])
def api_characters_update():
    data = request.json or {}
    char_id = data.get("id", "queen_lilith")
    paths = get_char_paths(char_id)
    if os.path.exists(paths["profile"]):
        with open(paths["profile"], "r", encoding="utf-8") as f:
            profile = json.load(f)
    else:
        profile = {"id": char_id}

    profile["display_name"] = data.get("display_name", profile.get("display_name", char_id))
    profile["handle"] = data.get("handle", profile.get("handle", f"@{char_id}"))
    profile["wf_gen"] = data.get("wf_gen", profile.get("wf_gen", ""))
    profile["wf_video"] = (
        str(data.get("wf_video") or "").strip()
        or profile.get("wf_video")
        or DEFAULT_WF_VIDEO_I2V
    )
    profile["wf_video_s2v"] = (
        str(data.get("wf_video_s2v") or "").strip()
        or profile.get("wf_video_s2v")
        or resolve_default_s2v_workflow(char_id)
    )

    fixed_tags = (
        data.get("fixed_tags")
        or data.get("fixed_hashtags")
        or profile.get("fixed_tags")
        or profile.get("fixed_hashtags")
        or list(DEFAULT_FIXED_TAGS)
    )
    profile["fixed_tags"] = fixed_tags
    profile["fixed_hashtags"] = fixed_tags

    persona = (
        data.get("persona_prompt")
        if "persona_prompt" in data
        else data.get("character_persona", profile.get("persona_prompt") or profile.get("character_persona", ""))
    )
    profile["persona_prompt"] = persona
    profile["character_persona"] = persona

    if "scenario_system_prompt" in data:
        profile["scenario_system_prompt"] = data.get("scenario_system_prompt", "")

    if "schedule_slots" in data and isinstance(data["schedule_slots"], list):
        profile["schedule_slots"] = data["schedule_slots"][:10]
    elif "schedule_slots" not in profile:
        profile["schedule_slots"] = []

    with open(paths["profile"], "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=2)

    return jsonify({"ok": True})
