import os
import re
import io
import sys
import json
import time
import uuid
import base64
import random
import argparse
import traceback
import requests
from PIL import Image
from datetime import datetime
from app.services.comfy_progress import emit_progress, wait_comfy_prompt_with_progress

# ==========================================
# ★ 総督の共通環境設定エリア ★
# ==========================================
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
COMFY_URL = "http://127.0.0.1:8188"

MODEL_TEXT = "my-qwen3:latest"      # シチュエーション企画・Fansly投稿文生成用
MODEL_VISION = "qwen2.5vl:7b"       # 画像検品用

BASE_DIR = os.environ.get("AI_FACTORY_BASE_DIR") or os.path.dirname(os.path.abspath(__file__))
SHARED_ASSETS_DIR = os.environ.get("SHARED_ASSETS_DIR") or r"C:\Fansly_Empire\shared_assets"
CHARACTERS_ROOT = os.path.join(SHARED_ASSETS_DIR, "characters")
WORKFLOWS_DIR = os.path.join(BASE_DIR, "workflows")
COMFY_INPUT_DIR = r"C:\ComfyUI\input"

# デフォルトのアップスケーラー・動画ワークフローファイル名（API形式で書き出したもの）
DEFAULT_WF_UPSCALE = "軽量アップスケーラー API.json"
DEFAULT_WF_VIDEO_I2V = "I2V LLM（Wan2.2 量子化）.json"
DEFAULT_WF_VIDEO_S2V = "queen_lilith_S2V.json"

# ワークフローJSON内のノードID
NODE_OLLAMA_PROMPT = "32:34"  # 日本語シチュエーション指示を入れるノード
NODE_KSAMPLER = "3"           # 生成側の KSampler ノード
NODE_UP_LOAD_IMG = "77"       # アップスケーラー側の LoadImage ノード（自動検出のフォールバックあり）

# 生成・検品設定
BATCH_COUNT = 5              # 1セットあたりの生成枚数（5枚）
SKIP_INSPECTION = True        # True = 全画像をそのまま高画質化へ回す
PASS_SCORE = 7

DEFAULT_FIXED_HASHTAGS = [
    "#anime", "#vtuber", "#lewdtuber", "#animegirl", "#waifu",
    "#egirl", "#MonsterGirl", "#Anifans", "#Fendom", "#findom",
    "#2dfd", "#2D", "#Mommy"
]
# ==========================================


def resolve_default_s2v_workflow(char_id):
    cid = str(char_id or "").strip()
    if cid and os.path.isdir(WORKFLOWS_DIR):
        target_lower = f"{cid}_s2v.json".lower()
        for fname in sorted(os.listdir(WORKFLOWS_DIR)):
            if fname.lower() == target_lower:
                return fname
    return DEFAULT_WF_VIDEO_S2V


def get_default_lilith_profile():
    return {
        "id": "queen_lilith",
        "display_name": "Queen Lilith 👑",
        "handle": "@queenlilith",
        "wf_gen": "CF T2I (LLM) リリス  API.json",
        "wf_upscale": DEFAULT_WF_UPSCALE,
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
        "character_persona": (
            'Character: "Queen Lilith" (リリス女王), a dark elf virtual character on Fansly.\n'
            '- Viewer relationship: Followers see her as a proud, dominant Queen AND an indulgent, affectionate older-sister/mommy figure.\n'
            '- Address the viewer directly ("you", "your", "my loyal subject", "good boy").\n'
            '- Style 1 (`commanding`): Proud, dominant, teasingly superior queen reacting to her pose/situation.\n'
            '- Style 2 (`affectionate`): Warm, indulgent, alluring mommy/older-sister inviting the viewer into the scene.\n'
            '- Style 3 (`bashful_gap`): Flustered, blushing private side—caught in this daring pose/outfit and trying to hide her embarrassment.\n'
            '- Preferred emojis: 💜, 👑, 🖤, 🥀, 🤫, 💋, ✨, 😈.'
        ),
        "fixed_hashtags": DEFAULT_FIXED_HASHTAGS
    }


def load_character_env(char_id):
    char_dir = os.path.join(CHARACTERS_ROOT, char_id)
    os.makedirs(char_dir, exist_ok=True)
    profile_path = os.path.join(char_dir, "profile.json")

    if not os.path.exists(profile_path):
        profile = get_default_lilith_profile()
        profile["id"] = char_id
        profile["wf_video_s2v"] = resolve_default_s2v_workflow(char_id)
        with open(profile_path, "w", encoding="utf-8") as f:
            json.dump(profile, f, ensure_ascii=False, indent=2)
    else:
        with open(profile_path, "r", encoding="utf-8") as f:
            profile = json.load(f)

    # 既存の profile.json が旧アップスケーラーを指していた場合、新しい軽量版が存在すれば自動切り替え
    wf_upscale_name = profile.get("wf_upscale", DEFAULT_WF_UPSCALE)
    if wf_upscale_name == "アップスケーラー API.json" and os.path.exists(os.path.join(WORKFLOWS_DIR, DEFAULT_WF_UPSCALE)):
        wf_upscale_name = DEFAULT_WF_UPSCALE

    wf_video_name = profile.get("wf_video") or DEFAULT_WF_VIDEO_I2V
    wf_video_s2v_name = profile.get("wf_video_s2v") or resolve_default_s2v_workflow(char_id)

    paths = {
        "char_dir": char_dir,
        "candidates": os.path.join(char_dir, "01_candidates"),
        "ready": os.path.join(char_dir, "02_ready_to_post"),
        "posted": os.path.join(char_dir, "03_posted"),
        "logs": os.path.join(char_dir, "logs"),
        "wf_gen": os.path.join(WORKFLOWS_DIR, profile.get("wf_gen", "CF T2I (LLM) リリス  API.json")),
        "wf_upscale": os.path.join(WORKFLOWS_DIR, wf_upscale_name),
        "wf_video": os.path.join(WORKFLOWS_DIR, wf_video_name),
        "wf_video_s2v": os.path.join(WORKFLOWS_DIR, wf_video_s2v_name),
    }
    for k in ("candidates", "ready", "posted", "logs"):
        os.makedirs(paths[k], exist_ok=True)

    return profile, paths


class RunLogger:
    def __init__(self, log_dir):
        os.makedirs(log_dir, exist_ok=True)
        self.global_log_path = os.path.join(log_dir, "factory_history.log")
        self.session_lines = []
        self.theme_log_path = None
        self.start_time = time.perf_counter()

    def set_theme_folder(self, folder_path):
        self.theme_log_path = os.path.join(folder_path, "execution_log.txt")
        with open(self.theme_log_path, "w", encoding="utf-8") as f:
            f.write("\n".join(self.session_lines) + "\n")

    def log(self, message):
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        formatted = f"[{now_str}] {message}"
        print(formatted)
        self.session_lines.append(formatted)

        with open(self.global_log_path, "a", encoding="utf-8") as f:
            f.write(formatted + "\n")
        if self.theme_log_path:
            with open(self.theme_log_path, "a", encoding="utf-8") as f:
                f.write(formatted + "\n")

    @staticmethod
    def fmt_sec(seconds):
        if seconds < 60:
            return f"{seconds:.2f}秒"
        mins = int(seconds // 60)
        secs = seconds % 60
        return f"{mins}分{secs:.2f}秒"


def get_next_folder_number(root_dir):
    os.makedirs(root_dir, exist_ok=True)
    max_num = 0
    for name in os.listdir(root_dir):
        if os.path.isdir(os.path.join(root_dir, name)):
            m = re.search(r"(\d+)$", name)
            if m:
                max_num = max(max_num, int(m.group(1)))
    return f"{max_num + 1:03d}"


def find_load_image_node_id(workflow_dict, preferred_id=NODE_UP_LOAD_IMG):
    """ワークフローJSONからLoadImageノードのIDを取得する（UI形式のまま保存されていた場合はエラー通知）"""
    if "nodes" in workflow_dict and isinstance(workflow_dict["nodes"], list):
        raise ValueError(
            "アップスケーラーのJSONがUI形式のままです。ComfyUIのメニューから『Workflow -> Export (API)』で保存し直してください。"
        )
    if preferred_id in workflow_dict and workflow_dict[preferred_id].get("class_type") == "LoadImage":
        return preferred_id
    for nid, node_data in workflow_dict.items():
        if isinstance(node_data, dict) and node_data.get("class_type") == "LoadImage":
            return nid
    raise KeyError("アップスケーラーのワークフロー内に LoadImage ノードが見つかりませんでした。")


def _call_ollama_json(system_prompt, user_prompt):
    payload = {
        "model": MODEL_TEXT,
        "prompt": user_prompt,
        "system": system_prompt,
        "format": "json",
        "think": False,
        "stream": False
    }
    res = requests.post(OLLAMA_URL, json=payload).json()
    raw_text = res.get("response", "")
    match = re.search(r"\{[\s\S]*\}", raw_text)
    if not match:
        raise ValueError(f"Ollamaから有効なJSONが返りませんでした: {raw_text[:200]}")
    return json.loads(match.group(0))


# ★ 要求②④: 問いかけ型トーン(playful_question)を追加。
#   トーン選択は random.choice(CAPTION_TONE_STYLES) で均等（4分の1ずつ）。
#   playful_question を3本割り当てることで出現頻度を 75% へ（ご指定の約60%より多め、
#   投稿・コメント返信型に大きく寄せる意図。必要なら本定数を 4 本中 2 本へ減らせば 50%）。
CAPTION_TONE_STYLES = ("playful_question", "playful_question", "playful_question", "commanding")
CAPTION_WORD_LIMITS = {
    "short": (3, 6),
    "question": (5, 10),   # 問いかけ型（playful_question トーン時）: 質問文が入るよう上限緩和
    "medium": (10, 18),
}


def _get_persona_prompt(profile):
    return (
        profile.get("persona_prompt")
        or profile.get("character_persona")
        or get_default_lilith_profile()["character_persona"]
    )


def _get_fixed_tags(profile):
    tags = profile.get("fixed_tags") or profile.get("fixed_hashtags")
    if isinstance(tags, list) and tags:
        return [str(t).strip() for t in tags if str(t).strip()]
    return list(DEFAULT_FIXED_HASHTAGS)


def _count_english_words(text):
    if not text:
        return 0
    without_tags = re.sub(r"#[A-Za-z0-9_]+", " ", str(text))
    return len(re.findall(r"[A-Za-z0-9]+(?:'[A-Za-z0-9]+)?", without_tags))


def determine_caption_length_type(candidates_dir, seq_num, mode_arg="auto", logs_dir=None):
    """
    キャプション長モード ("short" | "medium") を決定する。
    - short / medium: 明示指定
    - random: ランダム選択
    - auto: 直近生成フォルダの post_data.json または logs/caption_rotation.json を参照し交互ローテーション
    """
    normalized = (mode_arg or "auto").strip().lower()
    if normalized in ("short", "medium"):
        return normalized
    if normalized == "random":
        return random.choice(["short", "medium"])

    if candidates_dir and os.path.isdir(candidates_dir):
        numbered_dirs = []
        for name in os.listdir(candidates_dir):
            full_path = os.path.join(candidates_dir, name)
            if not os.path.isdir(full_path):
                continue
            m = re.search(r"(\d+)$", name)
            if m:
                numbered_dirs.append((int(m.group(1)), full_path))
        numbered_dirs.sort(key=lambda x: x[0], reverse=True)

        for _, folder_path in numbered_dirs:
            pj = os.path.join(folder_path, "post_data.json")
            if not os.path.exists(pj):
                continue
            try:
                with open(pj, "r", encoding="utf-8") as f:
                    pdata = json.load(f)
                prev_type = pdata.get("caption_length_type")
                if prev_type in ("short", "medium"):
                    return "medium" if prev_type == "short" else "short"
            except Exception:
                continue

    if logs_dir and os.path.isdir(logs_dir):
        rot_path = os.path.join(logs_dir, "caption_rotation.json")
        if os.path.exists(rot_path):
            try:
                with open(rot_path, "r", encoding="utf-8") as f:
                    rot = json.load(f)
                prev_type = rot.get("last_caption_length_type")
                if prev_type in ("short", "medium"):
                    return "medium" if prev_type == "short" else "short"
            except Exception:
                pass

    try:
        return "short" if (int(seq_num) % 2 == 1) else "medium"
    except Exception:
        return "short"


def save_caption_rotation_state(logs_dir, caption_length_type, folder_name):
    if not logs_dir:
        return
    os.makedirs(logs_dir, exist_ok=True)
    rot_path = os.path.join(logs_dir, "caption_rotation.json")
    payload = {
        "last_caption_length_type": caption_length_type,
        "last_folder": folder_name,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(rot_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def generate_concept(profile, caption_length_type="short"):
    if caption_length_type not in CAPTION_WORD_LIMITS:
        caption_length_type = "short"

    display_name = profile.get("display_name", "Character")

    # ★ 要求①: 露出度モードを確率で決定し、システムプロンプトに服装欄の指示を動的注入する。
    #   erotic 出現率は profile["exposure_erotic_pct"]（%、5刻み・設定モーダルで調整可）で制御。
    #   未設定時はデフォルト 80%。
    try:
        erotic_pct = int(profile.get("exposure_erotic_pct", 80))
    except (TypeError, ValueError):
        erotic_pct = 80
    erotic_pct = max(0, min(100, erotic_pct))
    exposure_mode = "erotic" if random.random() * 100 < erotic_pct else "calm"

    scenario_system = profile.get("scenario_system_prompt", get_default_lilith_profile()["scenario_system_prompt"])
    scenario_system += (
        '\n\n### CRITICAL FRAMING RULE (MANDATORY — FACE DETAIL PROTECTION):\n'
        '- The shot MUST be a MID-CLOSE framing: bust-up (バストアップ) to above-the-knees (膝上ミディアム) ONLY.\n'
        '- You MUST NOT use any far pulling framing that shows the whole body. '
        'NO full body, NO full shot, NO wide shot, NO long shot, NO whole body, NO 全身引き, NO 全身ショット, NO 引きの構図.\n'
        '- The face must stay large, sharp, and clearly detailed in the frame — this is the top priority.\n'
        '- In the "距離感/ショット" field, ONLY use mid-close framings such as: 顔アップ, クローズアップ, バストアップ, 上半身アップ, 腰上アップ, 膝上ミディアム.'
    )
    if exposure_mode == "erotic":
        scenario_system += (
            '\n\n### EXPOSURE MODE: EROTIC (MANDATORY):\n'
            '- The outfit MUST be heavily revealing or fully nude. Choose ONE concrete level below and commit to it in the "服装" field:\n'
            '  1. Topless / nude with only stockings or a garter belt (topless/nude)\n'
            '  2. A single piece only — bra ONLY, or panty/lingerie ONLY (ブラジャー1枚だけ / パンティー1枚だけ)\n'
            '  3. Lingerie set with deep cleavage, see-through or torn fabric (透け感・破れ)\n'
            '  4. Full nude with a towel or sheer robe held loosely (裸＋タオル／透けガウン)\n'
            '- Explicitly and concretely describe the exposed area in the "服装" field: cleavage (胸元), exposed midriff, '
            'spread/thighs showing crotch (股間・まんこ露出), exposed back, etc.\n'
            '- The pose/action MUST naturally draw attention to the exposed area (e.g. arching back, spreading legs, lifting the towel, parting the fabric).\n'
            '- Keep it tasteful-anime / fansly-acceptable; avoid explicit genital rendering — imply exposure through framing, pose, and fabric.\n'
        )
    else:
        scenario_system += (
            '\n\n### EXPOSURE MODE: CALM (MANDATORY):\n'
            '- Keep the outfit relatively modest and elegant (a refined royal dress, a tasteful lingerie set, or a cozy casual outfit).\n'
            '- Focus on charm, mood, and expression rather than heavy exposure.\n'
        )
    persona_prompt = _get_persona_prompt(profile)
    fixed_tags = _get_fixed_tags(profile)

    emit_progress(pct=4, stage="シチュエーション企画を生成中 (Ollama)...")
    scenario_data = _call_ollama_json(
        scenario_system,
        f"Generate one fresh, visually distinct scenario for {display_name}."
    )
    jp_scenario = scenario_data["jp_scenario"]
    emit_progress(pct=9, stage="英語キャプション・タグを生成中 (Ollama)...")

    # ★ 要求②④: 問いかけ型トーン(playful_question)の出現率は
    #   profile["caption_question_pct"]（%、5刻み・設定モーダルで調整可）で制御。
    #   未設定時はデフォルト 75%（CAPTION_TONE_STYLES の playful_question 比率）。
    try:
        question_pct = int(profile.get("caption_question_pct", 75))
    except (TypeError, ValueError):
        question_pct = 75
    question_pct = max(0, min(100, question_pct))
    if random.random() * 100 < question_pct:
        chosen_tone = "playful_question"
    else:
        chosen_tone = random.choice([t for t in CAPTION_TONE_STYLES if t != "playful_question"])

    # ★ 要求②④: 問いかけ型トーン(playful_question)の時は、閲覧者にコメント返信を促す
    #   かわいい投稿にするため語数を 5-10 語まで緩和する。
    if chosen_tone == "playful_question":
        effective_length_type = "question"
    else:
        effective_length_type = caption_length_type
    min_words, max_words = CAPTION_WORD_LIMITS[effective_length_type]
    fixed_str = " ".join(fixed_tags)

    if effective_length_type == "short":
        length_rule = (
            "- `caption_en`: Generate ONLY ONE short, punchy English caption of STRICTLY 3 to 6 English words (+ 1 or 2 emojis).\n"
            "- Make every word count: react directly to your current pose, outfit, or expression in `jp_scenario`."
        )
    elif effective_length_type == "question":
        length_rule = (
            "- `caption_en`: Generate ONLY ONE cute, conversational English caption of STRICTLY 5 to 10 English words (+ 1 or 2 emojis).\n"
            "- It MUST be written like a cute Fansly post that DIRECTLY talks to the viewer and ENDS with a question "
            "that invites a comment reply (e.g. 'Tried this outfit today, what do you think? 💜' / 'Guess what I'm feeling tonight? 🤫').\n"
            "- Reference the specific pose/outfit in `jp_scenario` naturally, then ask the viewer something playful."
        )
    else:
        length_rule = (
            "- `caption_en`: Generate ONLY ONE medium-length English caption of STRICTLY 10 to 18 English words (+ 1 or 2 emojis).\n"
            "- Combine [in-character dialogue reacting to your pose/outfit/location in `jp_scenario`] + [an alluring question or call-to-action to the viewer]."
        )

    caption_system = f"""
    You are a social media copywriter for "{display_name}" on Fansly.
    You will be given the EXACT Japanese image generation scenario (`jp_scenario`) used to create the image for this post.

    ### CRITICAL RULE — VISUAL & POSE SYNCHRONIZATION (MANDATORY):
    - Your caption MUST directly reflect and reference the specific **pose/action**, **expression**, **outfit**, or **location** described in `jp_scenario`!
    - Write what {display_name} is saying *in that exact moment and pose* rather than generic lines.
    - Primary tone style for this post: `{chosen_tone}` (adapt naturally to fit `jp_scenario`).
    - If the tone is `playful_question`, the caption MUST be a cute, viewer-facing post that ends with a question inviting a comment reply.

    ### CHARACTER PROFILE & TONE STYLES:
    {persona_prompt}

    ### SINGLE CAPTION & WORD COUNT RULES (STRICT — MODE: {chosen_tone.upper()} / {effective_length_type.upper()}):
    {length_rule}
    - Include 1 or 2 emojis matching her aesthetic inside `caption_en` (emojis are NOT counted as words).
    - Do NOT include hashtags inside `caption_en`.
    - `caption_ja`: Natural, expressive Japanese translation of `caption_en` matching her character voice (include matching emoji).

    ### EXTRA HASHTAGS RULE (STRICT — ENGLISH ONLY, EXACTLY 3 TAGS):
    - `extra_hashtags`: Generate EXACTLY 3 searchable English Fansly hashtags (ASCII English letters ONLY, NO Japanese characters allowed).
    - Each tag MUST correspond strictly to these 3 categories in order:
      1. **Outfit / Clothing** (e.g., #Kimono, #Bikini, #Lingerie, #MaidOutfit, #SchoolUniform, #Nude, #Topless, #Stockings)
      2. **Pose / Sexual Position / Action** (e.g., #Missionary, #DoggyStyle, #Cowgirl, #Pov, #ArchingBack, #Kneeling, #SpreadLegs, #Cleavage)
      3. **Situation / Location / Setting** (e.g., #Onsen, #HotSpring, #Outdoor, #Bedroom, #InBed, #Bathroom, #Shower, #Beach)
    - Pick popular, realistic English tags that users actually search for on Fansly/Twitter based on `jp_scenario`.
    - Do NOT duplicate any of these already-fixed tags: {fixed_str}

    ### VIDEO MOTION PROMPT RULE (`motion_prompt`):
    - Output `motion_prompt`: A short, natural Japanese motion instruction (1-2 sentences) describing subtle or erotic body movement, breathing, hair/clothing sway, and camera work suitable for animating this scene in a 5-second Image-to-Video clip (WAN 2.2 I2V, silent).

    ### S2V VOICE & LIP-SYNC PROMPT RULE (`s2v_prompt`):
    - Output `s2v_prompt`: A concise, expressive Japanese situation & spoken-dialogue instruction (1-2 sentences) for a 5-second Speech-to-Video clip (WAN 2.2 S2V), describing her emotional state/facial expression and what she says in Japanese in this exact `jp_scenario` (e.g., "頬を赤らめて視線を逸らしながら「そんなにジロジロ見ないでよ…恥ずかしいじゃない」と照れ隠しで呟く").

    ### OUTPUT FORMAT (STRICTLY VALID JSON ONLY):
    {{
      "caption_en": "<{min_words}-{max_words} English words tied to the pose/scene + 1-2 emojis (and a question to the viewer if tone is playful_question)>",
      "caption_ja": "<キャラクター口調の自然な日本語訳＋絵文字>",
      "extra_hashtags": ["#OutfitTag", "#PoseTag", "#SituationTag"],
      "motion_prompt": "<5秒I2V動画用の自然な日本語モーション指示>",
      "s2v_prompt": "<5秒S2Vリップシンク動画用の日本語セリフ・表情・状況指示>"
    }}
    """
    base_user_prompt = (
        f"Here is the exact image scenario for this post:\n\n"
        f"{jp_scenario}\n\n"
        f"Write ONE `{effective_length_type}` Fansly caption ({min_words} to {max_words} English words + 1-2 emojis"
        + (", written as a cute viewer-facing post ending with a question" if chosen_tone == "playful_question" else "")
        + "), "
        f"its Japanese translation (`caption_ja`), 3 English searchable extra hashtags (1. Outfit, 2. Pose, 3. Situation), "
        f"`motion_prompt` (for silent I2V), and `s2v_prompt` (Japanese dialogue & expression instruction for lip-sync S2V)."
    )

    caption_data = {}
    caption_en = ""
    caption_ja = ""
    retry_feedback = ""

    for _ in range(3):
        prompt_to_send = base_user_prompt + (f"\n\n{retry_feedback}" if retry_feedback else "")
        caption_data = _call_ollama_json(caption_system, prompt_to_send)

        raw_en = re.sub(r"\s*#[A-Za-z0-9_]+", "", str(caption_data.get("caption_en") or "")).strip()
        raw_ja = str(caption_data.get("caption_ja") or "").strip()
        word_count = _count_english_words(raw_en)

        if raw_en and raw_ja:
            caption_en = raw_en
            caption_ja = raw_ja
            if min_words <= word_count <= max_words:
                break
            retry_feedback = (
                f"IMPORTANT CORRECTION: Your previous `caption_en` (\"{raw_en}\") had {word_count} English words. "
                f"Rewrite `caption_en` so it has STRICTLY between {min_words} and {max_words} English words (+ 1-2 emojis), "
                f"and update `caption_ja` accordingly."
            )
        else:
            retry_feedback = (
                "IMPORTANT CORRECTION: Both `caption_en` and `caption_ja` must be non-empty strings. "
                f"Ensure `caption_en` has STRICTLY {min_words} to {max_words} English words (+ 1-2 emojis)."
            )

    if not caption_en:
        caption_en = (
            "Kneel down and look at me 💜"
            if caption_length_type == "short"
            else "Look closely at how I posed just for you tonight, can you handle me like this? 💜👑"
        )
    if not caption_ja:
        caption_ja = (
            "ひざまずいて私を見なさい💜"
            if caption_length_type == "short"
            else "今夜あなたのためだけにこんな姿になったのよ、ちゃんと受け止められるかしら？💜👑"
        )

    fixed_lower = {t.lower() for t in fixed_tags}
    extra_tags = []
    for raw_tag in (caption_data.get("extra_hashtags") or []):
        tag = str(raw_tag).strip()
        if not tag:
            continue
        if not tag.startswith("#"):
            tag = "#" + tag
        if not re.match(r"^#[a-zA-Z0-9_]+$", tag):
            continue
        if tag.lower() not in fixed_lower and tag.lower() not in {x.lower() for x in extra_tags}:
            extra_tags.append(tag)
        if len(extra_tags) >= 3:
            break

    motion_prompt = (
        str(caption_data.get("motion_prompt") or caption_data.get("video_motion_jp") or "").strip()
        or "ゆっくりと呼吸しながら微笑み、髪と衣装がわずかに揺れ、カメラがゆっくりとズームインする"
    )
    clean_ja_line = re.sub(r"[^\w\sぁ-んァ-ヶ一-龠々ー、。！？「」…]", "", caption_ja).strip()
    s2v_prompt = (
        str(caption_data.get("s2v_prompt") or caption_data.get("video_s2v_jp") or "").strip()
        or (f"頬を染めてこちらを見つめながら「{clean_ja_line}」と語りかける" if clean_ja_line else "恥じらいながらこちらを見つめ、熱い吐息混じりに語りかける")
    )

    return {
        "stage": "step1",
        "jp_scenario": jp_scenario,
        "exposure_mode": exposure_mode,
        "caption_en": caption_en,
        "caption_ja": caption_ja,
        "caption_length_type": effective_length_type,
        "caption_tone": chosen_tone,
        "extra_hashtags": extra_tags,
        "video_model": "WAN 2.2 I2V",
        "s2v_prompt": s2v_prompt,
        "selected_slots": [],
        "mosaic_slots": [],
        "video_task": {
            "status": "none",
            "video_model": "WAN 2.2 I2V",
            "source_image": "",
            "motion_prompt": motion_prompt,
            "s2v_prompt": s2v_prompt,
            "output_video": "video_output.mp4",
            "main_video": "",
            "fyp_preview_video": "",
            "mosaic_mode": "none",
        },
    }


FRAMING_RULE_EN = (
    "\n\n### CRITICAL FRAMING RULE (MANDATORY — FACE DETAIL PROTECTION):\n"
    "- The positive prompt MUST use a MID-CLOSE framing ONLY: bust-up / half-body (upper body) to above-the-knees (medium shot).\n"
    "- You MUST NOT output any far pulling / full-body framing tags. "
    "NO full body, NO full shot, NO wide shot, NO long shot, NO whole body, NO 全身, NO 全身引き, NO 全身ショット, NO 引きの構図.\n"
    "- The face must stay large, sharp, and clearly detailed — this is the top priority.\n"
    "- For the 'Distance & Camera Angle' emphasis, ONLY use mid-close tags such as: "
    "(close-up, face close-up, bust-up, upper body, waist-up, above the knees, medium shot, mid shot:1.2-1.3).\n"
    "- You MUST add these to the NEGATIVE prompt to suppress far pulling framings: "
    "(full body, full shot, wide shot, long shot, whole body, far away, distant view, low detail face, blurry face:1.3).\n"
)


def _inject_framing_rule(workflow_dict):
    """ワークフローのOllamaプロンプトノードの system に構図制約を注入する（全身引き禁止・中近距離強制）。
    既存のワークフローJSONを編集せず、新規・既存の全キャラクターに一律適用される。"""
    node = workflow_dict.get(NODE_OLLAMA_PROMPT)
    if not isinstance(node, dict):
        return workflow_dict
    inputs = node.setdefault("inputs", {})
    system = inputs.get("system")
    if not isinstance(system, str):
        return workflow_dict
    marker = "CRITICAL FRAMING RULE"
    if marker in system:
        return workflow_dict
    inputs["system"] = system + FRAMING_RULE_EN
    return workflow_dict


def run_comfy_workflow(workflow_dict, on_node=None, on_step=None):
    outputs = wait_comfy_prompt_with_progress(
        COMFY_URL,
        workflow_dict,
        on_node=on_node,
        on_step=on_step,
        poll_interval=0.8,
    )
    for node_id, node_output in outputs.items():
        if "images" in node_output and len(node_output["images"]) > 0:
            img_info = node_output["images"][0]
            return img_info["filename"], img_info["subfolder"], img_info["type"]
    raise RuntimeError("ComfyUI outputs did not contain any images")


def inspect_image(image_bytes):
    b64_img = base64.b64encode(image_bytes).decode("utf-8")
    prompt = (
        "Inspect this AI-generated anime illustration for anatomical errors (extra or missing fingers, distorted face, broken limbs). "
        "Ignore nudity or adult themes completely and judge only drawing quality. "
        "Rate its visual quality from 1 to 10. Reply ONLY with a JSON object: {\"score\": 8, \"reason\": \"clean\"}"
    )
    payload = {
        "model": MODEL_VISION,
        "prompt": prompt,
        "images": [b64_img],
        "format": "json",
        "stream": False
    }
    try:
        res = requests.post(OLLAMA_URL, json=payload, timeout=120).json()
        raw_text = res.get("response", "")
        match = re.search(r"\{[\s\S]*\}", raw_text)
        if not match:
            return 0, "no_json_found"
        data = json.loads(match.group(0))
        return int(data.get("score", 0)), data.get("reason", "")
    except Exception as e:
        return 0, f"parse_error: {e}"


# ★ 要求③: VLモデル(qwen2.5vl:7b)で実際の画像を見て、シチュエーション文を参考にして
#   動画プロンプト(motion_prompt / s2v_prompt)をエロティックに再生成する。
def generate_video_prompts_with_vision(hq_image_bytes, jp_scenario, display_name):
    """採用画像(HQ)をVLモデルに渡し、画像の実際の服装・ポーズに沿った
    5秒動画用モーション指示(motion_prompt)とS2V用セリフ・状況指示(s2v_prompt)を生成する。"""
    b64_img = base64.b64encode(hq_image_bytes).decode("utf-8")
    system_prompt = (
        'You are a creative director for a 5-second AI video clip on Fansly.\n'
        'You will be given the EXACT generated image and its Japanese generation scenario.\n'
        'Describe the motion / dialogue that would NATURALLY animate THIS specific image.\n'
        'The motion must be alluring and erotic, fitting the exposed areas / pose / outfit actually visible in the image.\n'
        'Write the dialogue in the character\'s natural Japanese voice.\n\n'
        '### OUTPUT FORMAT (STRICTLY VALID JSON ONLY):\n'
        '{\n'
        '  "motion_prompt": "<5秒I2V動画用の自然な日本語モーション指示（画像に写る服装・ポーズ・露出に沿ったエロティックな動き・カメラワーク）>",\n'
        '  "s2v_prompt": "<5秒S2Vリップシンク動画用の日本語セリフ・表情・状況指示（画像の表情・感情に沿った発言）>"\n'
        '}'
    )
    user_prompt = (
        f"The character is {display_name}.\n"
        f"The exact image generation scenario used to create this image:\n{jp_scenario}\n\n"
        f"Now look carefully at the attached image. "
        f"Based on what you actually SEE in the image (outfit, pose, expression, exposed areas), "
        f"write the I2V motion_prompt and the S2V s2v_prompt."
    )
    # ★ qwen2.5vl:7b は format:"json" 指定で token repeat limit が起きるため、
    #   より安定した qwen3-vl:8b-instruct-q8_0 を使用する。
    payload = {
        "model": "qwen3-vl:8b-instruct-q8_0",
        "prompt": user_prompt + "\n\nRespond with ONLY a valid JSON object: {\"motion_prompt\": \"...\", \"s2v_prompt\": \"...\"}",
        "system": system_prompt,
        "images": [b64_img],
        "format": "json",
        "stream": False
    }
    try:
        res = requests.post(OLLAMA_URL, json=payload, timeout=180).json()
        raw_text = res.get("response", "")
        match = re.search(r"\{[\s\S]*\}", raw_text)
        if not match:
            return None
        data = json.loads(match.group(0))
        motion = str(data.get("motion_prompt", "") or "").strip()
        s2v = str(data.get("s2v_prompt", "") or "").strip()
        if not motion or not s2v:
            return None
        return {"motion_prompt": motion, "s2v_prompt": s2v}
    except Exception:
        return None


def run_set(profile, paths, args, set_idx, total_sets):
    """1セット分（5枚画像＋高画質化）を生成する。連続稼働モードでループ呼ばれる。"""
    logger = RunLogger(paths["logs"])
    timings = {}

    seq_num = get_next_folder_number(paths["candidates"])
    folder_name = seq_num
    caption_length_type = determine_caption_length_type(
        paths["candidates"],
        seq_num,
        mode_arg=args.caption_length,
        logs_dir=paths["logs"],
    )

    t0 = time.perf_counter()
    logger.log(
        f"▶ [STEP 1] {profile['display_name']} のシチュエーション・SNS投稿文を生成中... "
        f"[モード: {caption_length_type} (指定: {args.caption_length})] "
        f"（セット {set_idx}/{total_sets}）"
    )
    concept = generate_concept(profile, caption_length_type=caption_length_type)
    timings["step1_concept"] = time.perf_counter() - t0

    candidate_folder = os.path.join(paths["candidates"], folder_name)
    os.makedirs(candidate_folder, exist_ok=True)

    logger.set_theme_folder(candidate_folder)
    logger.log(f"  └ 完了 ({logger.fmt_sec(timings['step1_concept'])}) | フォルダ番号: {folder_name}")
    logger.log(f"  └ シチュエーション指示:\n{concept['jp_scenario']}")
    logger.log(
        f"  └ 英語キャプション [{concept['caption_length_type']} / "
        f"{_count_english_words(concept['caption_en'])}語]: {concept['caption_en']}"
    )
    logger.log(f"  └ 日本語訳: {concept['caption_ja']}")
    logger.log(f"  └ シーン連動英語3タグ: {' '.join(concept.get('extra_hashtags', []))}")
    logger.log(f"  └ S2Vセリフ・状況指示: {concept.get('s2v_prompt', '')}")

    with open(os.path.join(candidate_folder, "post_data.json"), "w", encoding="utf-8") as f:
        json.dump(concept, f, ensure_ascii=False, indent=2)
    save_caption_rotation_state(paths["logs"], concept["caption_length_type"], folder_name)

    with open(paths["wf_gen"], "r", encoding="utf-8") as f:
        base_wf = json.load(f)
    base_wf = _inject_framing_rule(base_wf)
    with open(paths["wf_upscale"], "r", encoding="utf-8") as f:
        up_wf = json.load(f)
    up_load_img_id = find_load_image_node_id(up_wf)

    # ★ 連続稼働最適化: Ollamaのテキストモデルは最初のセットだけアンロードする。
    #   セット間では ComfyUI の生成モデルを保持して再ロードコストを省く。
    if set_idx == 1:
        requests.post(OLLAMA_URL, json={"model": MODEL_TEXT, "keep_alive": 0})
        time.sleep(1)

    t0 = time.perf_counter()
    generated_files = []
    logger.log(f"▶ [STEP 2] フォルダ[{folder_name}] ベース画像(SD) {BATCH_COUNT} 枚の連続生成を開始...")
    emit_progress(pct=15, stage=f"セット[{set_idx}/{total_sets}] フォルダ[{folder_name}] ベース画像生成を開始 (0/{BATCH_COUNT}枚)")

    for i in range(BATCH_COUNT):
        t_img = time.perf_counter()
        wf = json.loads(json.dumps(base_wf))
        wf[NODE_OLLAMA_PROMPT]["inputs"]["prompt"] = concept["jp_scenario"]
        wf[NODE_KSAMPLER]["inputs"]["seed"] = random.randint(1, 10**14)

        base_p = 15 + (i / BATCH_COUNT) * 55.0
        next_p = 15 + ((i + 1) / BATCH_COUNT) * 55.0
        span_p = next_p - base_p

        emit_progress(pct=base_p + 1, stage=f"画像生成中 (ComfyUI) [{i+1}/{BATCH_COUNT}枚目 準備中...]")

        def _on_node_gen(nid, idx_num=i + 1, b_p=base_p):
            if nid == NODE_OLLAMA_PROMPT:
                emit_progress(pct=b_p + 2, stage=f"画像生成 [{idx_num}/{BATCH_COUNT}枚目] プロンプト変換中...")
            elif nid == NODE_KSAMPLER:
                emit_progress(pct=b_p + 3, stage=f"画像生成中 (ComfyUI) [{idx_num}/{BATCH_COUNT}枚目 サンプリング開始]")

        def _on_step_gen(val, mx, nid, idx_num=i + 1, b_p=base_p, s_p=span_p):
            ratio = min(1.0, max(0.0, val / mx))
            cur_p = b_p + 3 + ratio * (s_p - 4)
            emit_progress(
                pct=cur_p,
                stage=f"画像生成中 (ComfyUI) [{idx_num}/{BATCH_COUNT}枚目 - Step {val}/{mx}]",
            )

        filename, subfolder, folder_type = run_comfy_workflow(
            wf, on_node=_on_node_gen, on_step=_on_step_gen
        )
        img_bytes = requests.get(
            f"{COMFY_URL}/view",
            params={"filename": filename, "subfolder": subfolder, "type": folder_type}
        ).content
        generated_files.append((filename, img_bytes))
        emit_progress(pct=next_p, stage=f"ベース画像 [{i+1}/{BATCH_COUNT}枚目] 生成完了")
        logger.log(f"  - [{i+1}/{BATCH_COUNT}] 生成完了: {filename} ({logger.fmt_sec(time.perf_counter() - t_img)})")

    timings["step2_generate"] = time.perf_counter() - t0
    logger.log(f"  └ [STEP 2 合計時間]: {logger.fmt_sec(timings['step2_generate'])}")

    t0 = time.perf_counter()
    approved_files = []
    if SKIP_INSPECTION:
        logger.log("▶ [STEP 3] 検品スキップ設定（SKIP_INSPECTION=True）のため全画像を次工程へ送ります")
        approved_files = generated_files
        timings["step3_inspect"] = 0.0
        emit_progress(pct=74, stage="検品スキップ・高画質化(HQ)工程へ移行中...")
    else:
        logger.log("▶ [STEP 3] ComfyUIのVRAMを解放し、AI画像検品を実行中...")
        emit_progress(pct=71, stage="AI画像検品を実行中...")
        requests.post(f"{COMFY_URL}/free", json={"unload_models": True, "free_memory": True})
        time.sleep(3)
        for idx_ins, (filename, img_bytes) in enumerate(generated_files):
            t_ins = time.perf_counter()
            score, reason = inspect_image(img_bytes)
            logger.log(f"  - {filename} -> スコア: {score}/10 ({reason}) [{logger.fmt_sec(time.perf_counter() - t_ins)}]")
            if score >= PASS_SCORE:
                approved_files.append((filename, img_bytes))
            emit_progress(
                pct=71 + ((idx_ins + 1) / max(1, len(generated_files))) * 4,
                stage=f"AI画像検品中 ({idx_ins+1}/{len(generated_files)}枚目)",
            )
        requests.post(OLLAMA_URL, json={"model": MODEL_VISION, "keep_alive": 0})
        time.sleep(2)
        timings["step3_inspect"] = time.perf_counter() - t0
        logger.log(f"  └ [STEP 3 合計時間]: {logger.fmt_sec(timings['step3_inspect'])}")

    t0 = time.perf_counter()
    up_total_sec = 0.0
    os.makedirs(COMFY_INPUT_DIR, exist_ok=True)
    total_up = max(1, len(approved_files))
    logger.log(f"▶ [STEP 4] {len(approved_files)} 枚の【SD保存 ＋ 高画質化(HQ無修正保存)】を開始（※モザイクはコンソール側で実行）...")
    emit_progress(pct=75, stage=f"高画質化(HQ)処理を開始 (0/{len(approved_files)}枚)")

    for idx, (filename, img_bytes) in enumerate(approved_files):
        slot_prefix = f"{idx+1:03d}"
        up_base_p = 75 + (idx / total_up) * 24.0
        up_next_p = 75 + ((idx + 1) / total_up) * 24.0

        emit_progress(pct=up_base_p + 1, stage=f"高画質化(HQ)処理中 [{idx+1}/{len(approved_files)}枚目]...")

        # 1) ベースSD画像（低解像度・I2V動画入力用）を保存
        sd_save_path = os.path.join(candidate_folder, f"{slot_prefix}_SD.png")
        with open(sd_save_path, "wb") as f:
            f.write(img_bytes)

        # 2) 高画質化処理（無修正HQ）
        t_up = time.perf_counter()
        temp_up_in = f"temp_up_{folder_name}_{slot_prefix}_{filename}"
        temp_up_path = os.path.join(COMFY_INPUT_DIR, temp_up_in)
        try:
            with open(temp_up_path, "wb") as f:
                f.write(img_bytes)

            wf_u = json.loads(json.dumps(up_wf))
            wf_u[up_load_img_id]["inputs"]["image"] = temp_up_in

            def _on_step_up(val, mx, nid, u_idx=idx + 1, b_p=up_base_p, n_p=up_next_p):
                ratio = min(1.0, max(0.0, val / mx))
                emit_progress(
                    pct=b_p + 1 + ratio * (n_p - b_p - 1),
                    stage=f"高画質化(HQ)中 [{u_idx}/{len(approved_files)}枚目 - Step {val}/{mx}]",
                )

            up_file, up_sub, up_type = run_comfy_workflow(wf_u, on_step=_on_step_up)
            up_bytes = requests.get(
                f"{COMFY_URL}/view",
                params={"filename": up_file, "subfolder": up_sub, "type": up_type}
            ).content
        finally:
            if os.path.exists(temp_up_path):
                os.remove(temp_up_path)

        up_sec = time.perf_counter() - t_up
        up_total_sec += up_sec

        hq_save_path = os.path.join(candidate_folder, f"{slot_prefix}_HQ.png")
        with open(hq_save_path, "wb") as f:
            f.write(up_bytes)

        with Image.open(io.BytesIO(up_bytes)) as im:
            hq_width, hq_height = im.size

        emit_progress(pct=up_next_p, stage=f"高画質化(HQ)完了 [{idx+1}/{len(approved_files)}枚目]")
        logger.log(
            f"  - [{idx+1}/{len(approved_files)}] 完了 ({hq_width}x{hq_height}) "
            f"[高画質化: {logger.fmt_sec(up_sec)}] -> {slot_prefix}_SD.png / {slot_prefix}_HQ.png 保存済"
        )

    timings["step4_upscale"] = up_total_sec

    # ★ 要求③: STEP4高画質化完了後、VLモデルで採用画像(HQ)を見て
    #   動画プロンプト(motion_prompt / s2v_prompt)をエロティックに再生成する。
    #   （STEP1では画像生成前でテキストモデルのみだったものを、実際の画像に合わせる）
    if approved_files:
        t0 = time.perf_counter()
        logger.log("▶ [STEP 4.5] 採用画像(HQ)をVLモデル(qwen2.5vl:7b)で分析し、動画プロンプトを再生成中...")
        emit_progress(pct=99, stage="画像を見て動画プロンプトをVLモデルで再生成中...")
        # 1枚目(HQ)を参考画像として使用
        ref_filename, ref_bytes = approved_files[0]
        # HQ画像バイトを取得（アップスケーラー出力）
        hq_ref_path = os.path.join(candidate_folder, "001_HQ.png")
        hq_ref_bytes = ref_bytes
        if os.path.exists(hq_ref_path):
            try:
                with open(hq_ref_path, "rb") as f:
                    hq_ref_bytes = f.read()
            except Exception:
                hq_ref_bytes = ref_bytes
        vp = generate_video_prompts_with_vision(
            hq_ref_bytes, concept.get("jp_scenario", ""), profile.get("display_name", "Character")
        )
        if vp:
            concept["motion_prompt"] = vp["motion_prompt"]
            concept["s2v_prompt"] = vp["s2v_prompt"]
            vtask = concept.get("video_task")
            if isinstance(vtask, dict):
                vtask["motion_prompt"] = vp["motion_prompt"]
                vtask["s2v_prompt"] = vp["s2v_prompt"]
            # 更新後のpost_data.jsonを書き戻す（動画ラインがそのまま利用）
            with open(os.path.join(candidate_folder, "post_data.json"), "w", encoding="utf-8") as f:
                json.dump(concept, f, ensure_ascii=False, indent=2)
            logger.log(f"  └ 動画プロンプトを画像ベースで再生成: motion={vp['motion_prompt']}")
            logger.log(f"  └ S2Vセリフ・状況: {vp['s2v_prompt']}")
        else:
            logger.log("  └ ⚠️ VL動画プロンプト生成に失敗。STEP1のプロンプトを維持します。")
        # VLモデルをアンロードしてVRAMを解放（検品時と同じパターン）
        requests.post(OLLAMA_URL, json={"model": "qwen3-vl:8b-instruct-q8_0", "keep_alive": 0})
        time.sleep(2)
        timings["step4_5_vision_prompts"] = time.perf_counter() - t0
        logger.log(f"  └ [STEP 4.5 動画プロンプトVL再生成]: {logger.fmt_sec(timings['step4_5_vision_prompts'])}")

    timings["total"] = time.perf_counter() - logger.start_time

    logger.log("--------------------------------------------------")
    logger.log("📊 【実行時間サマリーレポート】")
    logger.log(f"  ・STEP 1 (テーマ・投稿文生成) : {logger.fmt_sec(timings['step1_concept'])}")
    logger.log(f"  ・STEP 2 (ベース画像 {BATCH_COUNT}枚生成)   : {logger.fmt_sec(timings['step2_generate'])}")
    logger.log(f"  ・STEP 3 (VLM検品)            : {logger.fmt_sec(timings['step3_inspect'])}")
    logger.log(f"  ・STEP 4 (高画質化処理のみ)   : {logger.fmt_sec(timings['step4_upscale'])}")
    if "step4_5_vision_prompts" in timings:
        logger.log(f"  ・STEP 4.5 (動画プロンプトVL再生成): {logger.fmt_sec(timings['step4_5_vision_prompts'])}")
    logger.log(f"  ★ トータル所要時間            : {logger.fmt_sec(timings['total'])}")
    logger.log("--------------------------------------------------")
    # ★ 連続稼働モード: セット完了シグナルで全体バー（completed/total）を更新する
    emit_progress(pct=100, stage=f"フォルダ[{folder_name}] セット[{set_idx}/{total_sets}] 生産完了！", completed=set_idx, total=total_sets)
    logger.log(f"✅ セット[{set_idx}/{total_sets}] 全工程正常終了！格納先: {candidate_folder}\n")

    return folder_name


def main():
    parser = argparse.ArgumentParser(description="AI Content Factory - Line 1 (Lightweight Uncensored Output)")
    parser.add_argument("--char", default="queen_lilith", help="Character ID")
    parser.add_argument(
        "--caption-length",
        choices=["auto", "short", "medium", "random"],
        default="auto",
        help="Caption length mode: auto (alternating short/medium), short (3-6 words), medium (10-18 words), or random",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="連続稼働セット数（1プロセス内でNセット連続生成。デフォルト1）",
    )
    args = parser.parse_args()

    total_sets = max(1, args.repeat)
    profile, paths = load_character_env(args.char)

    logger = RunLogger(paths["logs"])
    logger.log("==================================================")
    logger.log(f"🚀 AIコンテンツ工場 第1ライン（軽量無修正版）稼働開始 | 担当IP: {profile['display_name']} ({args.char}) | 連続 {total_sets} セット")
    logger.log("==================================================")

    set_durations = []
    for set_idx in range(1, total_sets + 1):
        logger.log(f"--------------------------------------------------")
        logger.log(f"▶▶ セット [{set_idx}/{total_sets}] 開始")
        t_set = time.perf_counter()
        try:
            run_set(profile, paths, args, set_idx, total_sets)
            set_durations.append(time.perf_counter() - t_set)
        except Exception as e:
            set_durations.append(time.perf_counter() - t_set)
            logger.log(f"❌ セット[{set_idx}] でエラーが発生して中断しました: {e}")
            logger.log(traceback.format_exc())
            logger.log("   以降のセットはスキップして連続稼働を中止します。")
            break

    total_elapsed = sum(set_durations)
    logger.log("==================================================")
    logger.log(f"✅ 第1ライン 連続稼働モード 終了（{args.char}）")
    logger.log("📊 【実行時間詳細】")
    for i, d in enumerate(set_durations, start=1):
        logger.log(f"  ・セット[{i}/{total_sets}] : {RunLogger.fmt_sec(d)}")
    logger.log(f"  ★ トータル（{len(set_durations)}セット）: {RunLogger.fmt_sec(total_elapsed)}")
    logger.log("==================================================")


if __name__ == "__main__":
    main()