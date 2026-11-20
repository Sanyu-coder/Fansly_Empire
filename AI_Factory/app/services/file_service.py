import os
import re
import json
from app.services.character_service import get_char_paths
from app.services.mosaic_service import resolve_raw_video_filename

VIDEO_MODEL_I2V = "WAN 2.2 I2V"
VIDEO_MODEL_S2V = "WAN 2.2 S2V"


def normalize_video_model(raw_val) -> str:
    """動画生成モデル指定値を正規化する ('WAN 2.2 I2V' | 'WAN 2.2 S2V')"""
    s = str(raw_val or "").strip().lower()
    if "s2v" in s:
        return VIDEO_MODEL_S2V
    return VIDEO_MODEL_I2V


def get_images_in_dir(dir_path):
    if not dir_path or not os.path.isdir(dir_path):
        return []
    exts = (".png", ".jpg", ".jpeg", ".webp")
    return sorted([f for f in os.listdir(dir_path) if f.lower().endswith(exts)])


def get_videos_in_dir(dir_path):
    if not dir_path or not os.path.isdir(dir_path):
        return []
    exts = (".mp4", ".webm", ".mov")
    return sorted([
        f for f in os.listdir(dir_path)
        if f.lower().endswith(exts) and not f.lower().endswith(".tmp.mp4")
    ])


def extract_slot_id(filename_or_slot: str) -> str:
    """ファイル名またはスロット文字列から slot_id（例: "001"）を抽出"""
    raw = str(filename_or_slot or "").strip()
    if not raw:
        return ""
    base = os.path.basename(raw)
    m = re.match(r"^(.+?)_(SD|HQ|MOSAIC|FYP_PREVIEW)(\.[^.]+)?$", base, re.IGNORECASE)
    if m:
        return m.group(1)
    stem, _ = os.path.splitext(base)
    return stem


def group_candidate_slots(dir_path):
    """候補フォルダ内の画像をスロット単位（SD / HQ / MOSAIC / FYP_PREVIEW）にグループ化"""
    files = get_images_in_dir(dir_path)
    slots_map = {}
    for fname in files:
        m = re.match(r"^(.+?)_(SD|HQ|MOSAIC|FYP_PREVIEW)(\.[^.]+)$", fname, re.IGNORECASE)
        if m:
            slot_id = m.group(1)
            variant = m.group(2).upper()
        else:
            slot_id = os.path.splitext(fname)[0]
            variant = "HQ"

        if slot_id not in slots_map:
            slots_map[slot_id] = {
                "slot_id": slot_id,
                "sd": None,
                "hq": None,
                "mosaic": None,
                "fyp_preview": None,
            }
        slots_map[slot_id][variant.lower()] = fname

    return [slots_map[k] for k in sorted(slots_map.keys())]


def resolve_candidate_stage(post_data: dict) -> str:
    """
    post_data.json から候補フォルダのステージ ("step1" | "step2") を一意に判定する。
    """
    if not isinstance(post_data, dict):
        return "step1"

    stage = str(post_data.get("stage") or "").strip().lower()
    if stage in ("step1", "step2"):
        return stage

    vtask = post_data.get("video_task")
    if isinstance(vtask, dict):
        status = str(vtask.get("status") or "none").strip().lower()
        src_img = str(vtask.get("source_image") or "").strip()
        if src_img and status in ("queued", "running", "done", "error"):
            return "step2"

    if (post_data.get("step1_saved") or post_data.get("step1_approved")) and post_data.get("video_image"):
        return "step2"

    return "step1"


def resolve_adopted_images(cand_dir: str, selected_slots: list, mosaic_slots: list) -> tuple[list[str], list[str]]:
    """
    selected_slots と mosaic_slots から最終コピー対象の画像ファイル名リストを解決する。
      - slot_id in mosaic_slots     -> <slot_id>_MOSAIC.png
      - slot_id not in mosaic_slots -> <slot_id>_HQ.png
    """
    slots_info = {s["slot_id"]: s for s in group_candidate_slots(cand_dir)}
    normalized_selected = []
    for s in (selected_slots or []):
        sid = extract_slot_id(s)
        if sid and sid not in normalized_selected:
            normalized_selected.append(sid)

    mosaic_set = {extract_slot_id(s) for s in (mosaic_slots or []) if extract_slot_id(s)}

    resolved_files = []
    errors = []

    for sid in normalized_selected:
        slot_entry = slots_info.get(sid, {})
        if sid in mosaic_set:
            cand_name = slot_entry.get("mosaic") or f"{sid}_MOSAIC.png"
            if os.path.exists(os.path.join(cand_dir, cand_name)):
                resolved_files.append(cand_name)
            else:
                errors.append(f"スロット {sid} のモザイク画像 ({cand_name}) が存在しません")
        else:
            cand_name = slot_entry.get("hq") or f"{sid}_HQ.png"
            if os.path.exists(os.path.join(cand_dir, cand_name)):
                resolved_files.append(cand_name)
            elif slot_entry.get("sd") and os.path.exists(os.path.join(cand_dir, slot_entry["sd"])):
                resolved_files.append(slot_entry["sd"])
            else:
                errors.append(f"スロット {sid} の高画質画像 ({cand_name}) が存在しません")

    return resolved_files, errors


def resolve_step2_videos(cand_dir: str, video_task: dict) -> tuple[str, str, str]:
    """
    Step 2 完了確定時に video_task から (main_video, fyp_preview_video, error_msg) を解決・検証する。
    """
    if not isinstance(video_task, dict):
        return "", "", "video_task が設定されていません"

    status = str(video_task.get("status") or "none").strip().lower()
    if status != "done":
        return "", "", f"動画生成が完了していません (status='{status}')"

    raw_vid = resolve_raw_video_filename(cand_dir, video_task.get("output_video"), video_task)
    raw_vid_exists = bool(raw_vid and os.path.exists(os.path.join(cand_dir, raw_vid)))

    main_video = str(video_task.get("main_video") or "").strip()
    fyp_preview_video = str(video_task.get("fyp_preview_video") or "").strip()

    if not main_video and raw_vid_exists:
        main_video = raw_vid
    if not fyp_preview_video and raw_vid_exists:
        fyp_preview_video = main_video or raw_vid

    if not main_video or not os.path.exists(os.path.join(cand_dir, main_video)):
        if raw_vid_exists:
            main_video = raw_vid
        else:
            return "", "", "本編用動画ファイル (main_video) がフォルダ内に存在しません"

    if not fyp_preview_video or not os.path.exists(os.path.join(cand_dir, fyp_preview_video)):
        if os.path.exists(os.path.join(cand_dir, main_video)):
            fyp_preview_video = main_video
        else:
            return "", "", "FYPプレビュー用動画ファイル (fyp_preview_video) がフォルダ内に存在しません"

    return main_video, fyp_preview_video, ""


def build_candidate_summary(cand_dir: str, folder_name: str, post_data: dict = None) -> dict:
    """01_candidates/<folder> の一覧表示用サマリを構築する"""
    if post_data is None:
        pdata_path = os.path.join(cand_dir, "post_data.json")
        post_data = {}
        if os.path.exists(pdata_path):
            try:
                with open(pdata_path, "r", encoding="utf-8") as f:
                    post_data = json.load(f)
            except Exception:
                post_data = {}

    stage = resolve_candidate_stage(post_data)
    slots = group_candidate_slots(cand_dir)
    vids = get_videos_in_dir(cand_dir)

    raw_selected = post_data.get("selected_slots")
    if raw_selected is None and post_data.get("selected_images"):
        raw_selected = [extract_slot_id(x) for x in post_data.get("selected_images", [])]
    selected_slots = [extract_slot_id(s) for s in (raw_selected or []) if extract_slot_id(s)]

    raw_mosaic = post_data.get("mosaic_slots")
    if raw_mosaic is None:
        raw_mosaic = [s["slot_id"] for s in slots if s.get("mosaic")]
    mosaic_slots = [extract_slot_id(s) for s in (raw_mosaic or []) if extract_slot_id(s)]

    vtask = post_data.get("video_task") if isinstance(post_data.get("video_task"), dict) else {}
    v_status = str(vtask.get("status") or "none").strip().lower()
    out_vid = vtask.get("output_video") or vtask.get("video_file") or post_data.get("generated_video") or ""
    if v_status != "done" and out_vid and os.path.exists(os.path.join(cand_dir, out_vid)):
        v_status = "done"

    source_image = vtask.get("source_image") or post_data.get("video_image") or ""
    main_video = vtask.get("main_video") or ""
    fyp_preview_video = vtask.get("fyp_preview_video") or ""
    if v_status == "done":
        resolved_main, resolved_fyp, _ = resolve_step2_videos(cand_dir, {**vtask, "status": "done"})
        main_video = main_video or resolved_main
        fyp_preview_video = fyp_preview_video or resolved_fyp

    normalized_model = normalize_video_model(
        vtask.get("video_model")
        or vtask.get("model")
        or post_data.get("video_model")
        or post_data.get("model")
    )
    motion_prompt = vtask.get("motion_prompt") or post_data.get("video_motion_jp") or ""
    s2v_prompt = (
        vtask.get("s2v_prompt")
        or post_data.get("s2v_prompt")
        or post_data.get("caption_ja")
        or ""
    )

    normalized_vtask = {
        "status": v_status,
        "video_model": normalized_model,
        "source_image": source_image,
        "motion_prompt": motion_prompt,
        "s2v_prompt": s2v_prompt,
        "output_video": out_vid or (vids[0] if vids else "video_output.mp4"),
        "main_video": main_video,
        "fyp_preview_video": fyp_preview_video,
        "mosaic_mode": vtask.get("mosaic_mode", "none"),
    }

    return {
        "name": folder_name,
        "folder": folder_name,
        "stage": stage,
        "slot_count": len(slots),
        "image_count": len(slots),
        "slots": slots,
        "selected_slots": selected_slots,
        "mosaic_slots": mosaic_slots,
        "has_video": v_status == "done" or len(vids) > 0,
        "videos": vids,
        "video_model": normalized_model,
        "s2v_prompt": s2v_prompt,
        "video_task": normalized_vtask,
        "jp_scenario": post_data.get("jp_scenario", ""),
        "caption_en": post_data.get("caption_en", ""),
        "caption_ja": post_data.get("caption_ja", ""),
        "caption_length_type": post_data.get("caption_length_type", "short"),
        "extra_hashtags": post_data.get("extra_hashtags") or post_data.get("hashtags") or [],
    }


def list_candidates_by_stage(char_id: str) -> dict:
    """
    01_candidates 内の全フォルダを stage ("step1" | "step2") で厳密に振り分けて返す。
    """
    paths = get_char_paths(char_id)
    cand_root = paths["candidates"]
    step1_list = []
    step2_list = []
    all_list = []

    if not os.path.isdir(cand_root):
        return {"step1": [], "step2": [], "all": []}

    for folder_name in sorted(os.listdir(cand_root)):
        f_path = os.path.join(cand_root, folder_name)
        if not os.path.isdir(f_path):
            continue
        slots = group_candidate_slots(f_path)
        vids = get_videos_in_dir(f_path)
        pdata_path = os.path.join(f_path, "post_data.json")
        if not slots and not vids and not os.path.exists(pdata_path):
            continue

        item = build_candidate_summary(f_path, folder_name)
        all_list.append(item)
        if item["stage"] == "step2":
            step2_list.append(item)
        else:
            step1_list.append(item)

    return {
        "step1": step1_list,
        "step2": step2_list,
        "all": all_list,
    }


def resolve_queue_folder_path(char_id, item_key):
    """キューのアイテムキーから実際のフォルダパスを解決する"""
    paths = get_char_paths(char_id)
    if not item_key or ".." in str(item_key):
        return None

    clean_key = str(item_key).strip()
    for prefix in ("ready:", "posted:"):
        if clean_key.startswith(prefix):
            clean_key = clean_key.split(":", 1)[1]
            if prefix == "posted:":
                posted_direct = os.path.join(paths["posted"], clean_key)
                if os.path.isdir(posted_direct):
                    return posted_direct

    base_path = paths["ready"]
    posted_root = paths["posted"]

    if "/" in clean_key:
        parts = clean_key.split("/", 1)
        sub_prefix, sub_name = parts[0], parts[1]
        if sub_prefix in ("03_posted", "posted_archive"):
            cand_posted = os.path.join(posted_root, sub_name)
            if os.path.isdir(cand_posted):
                return cand_posted
        if sub_prefix == "posted":
            cand_posted = os.path.join(posted_root, sub_name)
            if os.path.isdir(cand_posted):
                return cand_posted
            cand_legacy = os.path.join(base_path, "posted", sub_name)
            if os.path.isdir(cand_legacy):
                return cand_legacy
        cand = os.path.join(base_path, sub_prefix, sub_name)
        if os.path.isdir(cand):
            return cand
        clean_key = sub_name

    direct_path = os.path.join(base_path, clean_key)
    if os.path.isdir(direct_path):
        return direct_path

    for sub in ("pending", "scheduled", "posted"):
        sub_path = os.path.join(base_path, sub, clean_key)
        if os.path.isdir(sub_path):
            return sub_path

    posted_root_path = os.path.join(paths["posted"], clean_key)
    if os.path.isdir(posted_root_path):
        return posted_root_path

    return None


def collect_queue_item(folder_path, folder_name, status, item_key):
    imgs = get_images_in_dir(folder_path)
    vids = get_videos_in_dir(folder_path)
    final_json = os.path.join(folder_path, "final_post.json")
    meta = {}
    if os.path.exists(final_json):
        try:
            with open(final_json, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            meta = {}
    if meta.get("posted") or meta.get("status") == "posted" or meta.get("posted_at"):
        status = "posted"

    main_video = meta.get("main_video") or meta.get("video_file") or ""
    fyp_preview_video = meta.get("fyp_preview_video") or ""

    ordered_vids = []
    for vf in (main_video, fyp_preview_video):
        if vf and os.path.exists(os.path.join(folder_path, vf)) and vf not in ordered_vids:
            ordered_vids.append(vf)
    for vf in (meta.get("videos") or []):
        if vf and os.path.exists(os.path.join(folder_path, vf)) and vf not in ordered_vids:
            ordered_vids.append(vf)
    for vf in vids:
        if vf not in ordered_vids:
            ordered_vids.append(vf)

    if not main_video and ordered_vids:
        main_video = ordered_vids[0]
    if not fyp_preview_video and main_video:
        fyp_preview_video = main_video

    resolved_images = [
        img for img in (meta.get("images") or imgs)
        if os.path.exists(os.path.join(folder_path, img))
    ]
    caption = meta.get("caption", "")
    hashtags = meta.get("hashtags", [])
    full_text = meta.get("full_text") or (
        f"{caption}\n\n{' '.join(hashtags)}".strip() if (caption or hashtags) else ""
    )

    resolved_folder = str(meta.get("folder") or meta.get("folder_id") or "").strip()
    if not resolved_folder:
        m_num = re.match(r"^(\d+)(?:_\d{8}_\d{6})?$", str(folder_name or ""))
        resolved_folder = m_num.group(1) if m_num else folder_name

    return {
        "key": item_key,
        "item_key": item_key,
        "folder": resolved_folder,
        "name": resolved_folder if status == "posted" else folder_name,
        "archive_folder": folder_name,
        "char_id": meta.get("char_id", ""),
        "status": status,
        "posted": status == "posted",
        "posted_at": meta.get("posted_at", ""),
        "image_count": len(resolved_images),
        "has_video": bool(main_video or ordered_vids),
        "main_video": main_video,
        "fyp_preview_video": fyp_preview_video,
        "videos": ordered_vids,
        "images": resolved_images,
        "scheduled_at": meta.get("scheduled_at", ""),
        "caption": caption,
        "hashtags": hashtags,
        "full_text": full_text,
    }
