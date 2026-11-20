import os
import re
import json
import random
import shutil
import requests
from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify, send_from_directory

from app.config import DEFAULT_FIXED_TAGS, OLLAMA_URL, OLLAMA_MODEL_TEXT
from app.services.character_service import (
    get_char_paths,
    list_all_characters,
    get_default_lilith_profile,
)
from app.services.file_service import (
    get_images_in_dir,
    get_videos_in_dir,
    extract_slot_id,
    group_candidate_slots,
    resolve_candidate_stage,
    resolve_adopted_images,
    resolve_step2_videos,
    build_candidate_summary,
    list_candidates_by_stage,
    resolve_queue_folder_path,
    collect_queue_item,
    normalize_video_model,
)


def check_video_lock_status(folder_path: str, final_meta: dict, post_meta: dict) -> tuple[bool, str]:
    """互換用：動画未完成ロック状態チェック"""
    if final_meta.get("video_lock") is True or post_meta.get("video_lock") is True:
        return True, "video_lock=True（動画未完成またはモザイク確認待ちロック中）"
    if final_meta.get("ready_for_publish") is False:
        return True, "ready_for_publish=False（動画完了待ち待機中）"

    video_task = post_meta.get("video_task") or final_meta.get("video_task")
    if isinstance(video_task, dict) and video_task:
        status = video_task.get("status", "queued")
        out_vid = (
            video_task.get("main_video")
            or video_task.get("output_video")
            or post_meta.get("generated_video")
            or final_meta.get("main_video")
            or final_meta.get("video")
        )
        if status != "done":
            return True, f"video_task.status='{status}'（動画生成未完了）"
        if not out_vid or not os.path.exists(os.path.join(folder_path, out_vid)):
            return True, "生成済み動画ファイルがフォルダ内に存在しません"

    return False, ""


def get_folder_lock_status(char_id: str, folder_name: str) -> dict:
    """フォルダのロック状態を取得"""
    paths = get_char_paths(char_id)
    folder_path = resolve_queue_folder_path(char_id, folder_name) or os.path.join(paths["ready"], folder_name)

    post_data_path = os.path.join(folder_path, "post_data.json")
    final_post_path = os.path.join(folder_path, "final_post.json")

    post_meta = {}
    final_meta = {}

    if os.path.exists(post_data_path):
        try:
            with open(post_data_path, "r", encoding="utf-8") as f:
                post_meta = json.load(f)
        except Exception:
            pass

    if os.path.exists(final_post_path):
        try:
            with open(final_post_path, "r", encoding="utf-8") as f:
                final_meta = json.load(f)
        except Exception:
            pass

    is_locked, lock_reason = check_video_lock_status(folder_path, final_meta, post_meta)

    return {
        "folder": folder_name,
        "is_locked": is_locked,
        "lock_reason": lock_reason,
        "video_task": post_meta.get("video_task") or final_meta.get("video_task"),
        "ready_for_publish": final_meta.get("ready_for_publish", True),
    }


queue_bp = Blueprint("queue_bp", __name__)

MAX_SCHEDULE_SLOTS = 10


# ==========================================
# 投稿時間設定（1〜10枠・手動管理）の読み書き
# ==========================================
def load_char_profile(char_id: str) -> dict:
    paths = get_char_paths(char_id)
    profile_path = paths["profile"]
    if os.path.exists(profile_path):
        try:
            with open(profile_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return get_default_lilith_profile()


def normalize_time_slot(slot_str: str) -> str | None:
    if not isinstance(slot_str, str):
        return None
    raw = slot_str.strip()
    m = re.match(r"^(\d{1,2}):(\d{2})$", raw)
    if not m:
        return None
    hh = int(m.group(1))
    mm = int(m.group(2))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return f"{hh:02d}:{mm:02d}"


def get_schedule_slots(char_id: str) -> list[str]:
    """profile.json の schedule_slots（最大10枠）を昇順で取得（標準プリセットなし）"""
    prof = load_char_profile(char_id)
    saved = prof.get("schedule_slots")
    if not isinstance(saved, list):
        return []
    cleaned = []
    for s in saved:
        norm = normalize_time_slot(s)
        if norm and norm not in cleaned:
            cleaned.append(norm)
    cleaned.sort()
    return cleaned[:MAX_SCHEDULE_SLOTS]


def save_schedule_slots(char_id: str, slots: list) -> tuple[list[str], str]:
    """schedule_slots を検証・重複除去・昇順ソートして保存（最大10枠）"""
    if not isinstance(slots, list):
        return [], "slots はリスト形式で指定してください"

    cleaned = []
    for s in slots:
        norm = normalize_time_slot(s)
        if norm is None:
            return [], f"不正な時刻フォーマットが含まれています: {s}（HH:MM 形式で指定してください）"
        if norm not in cleaned:
            cleaned.append(norm)

    if len(cleaned) > MAX_SCHEDULE_SLOTS:
        return [], f"スケジュール枠は最大{MAX_SCHEDULE_SLOTS}枠までです（指定数: {len(cleaned)}枠）"

    cleaned.sort()

    paths = get_char_paths(char_id)
    profile_path = paths["profile"]
    prof = load_char_profile(char_id)
    prof["schedule_slots"] = cleaned
    os.makedirs(os.path.dirname(profile_path), exist_ok=True)
    with open(profile_path, "w", encoding="utf-8") as f:
        json.dump(prof, f, ensure_ascii=False, indent=2)
    return cleaned, ""


def get_char_fixed_tags(char_id: str) -> list[str]:
    prof = load_char_profile(char_id)
    tags = prof.get("fixed_tags") or prof.get("fixed_hashtags")
    if isinstance(tags, list) and tags:
        return [str(t).strip() for t in tags if str(t).strip()]
    return list(DEFAULT_FIXED_TAGS)


def get_char_persona_prompt(char_id: str) -> str:
    prof = load_char_profile(char_id)
    return (
        str(prof.get("persona_prompt") or prof.get("character_persona") or "").strip()
        or get_default_lilith_profile()["persona_prompt"]
    )


# ==========================================
# キャプション・タグ構築 ＆ 既存予約日時収集
# ==========================================
def normalize_hashtags(raw_tags) -> list[str]:
    if isinstance(raw_tags, str):
        tokens = re.split(r"[\s,]+", raw_tags.strip())
    elif isinstance(raw_tags, list):
        tokens = []
        for item in raw_tags:
            if isinstance(item, str):
                tokens.extend(re.split(r"[\s,]+", item.strip()))
    else:
        tokens = []

    result = []
    seen = set()
    for t in tokens:
        tag = t.strip()
        if not tag:
            continue
        if not tag.startswith("#"):
            tag = "#" + tag
        low = tag.lower()
        if low not in seen:
            seen.add(low)
            result.append(tag)
    return result


def build_full_text(caption: str, hashtags: list[str]) -> str:
    cap = (caption or "").strip()
    tags_str = " ".join(normalize_hashtags(hashtags))
    if cap and tags_str:
        return f"{cap}\n\n{tags_str}"
    return cap or tags_str


def build_caption_and_tags_from_post_data(char_id: str, post_data: dict) -> tuple[str, list[str], str]:
    caption = str(post_data.get("caption_en") or post_data.get("draft_caption") or post_data.get("caption") or "").strip()
    if not caption:
        pool = []
        for key in ("captions_short", "captions_medium"):
            items = post_data.get(key) or []
            if isinstance(items, list):
                for it in items:
                    if isinstance(it, dict) and it.get("en"):
                        pool.append(str(it["en"]).strip())
                    elif isinstance(it, str) and it.strip():
                        pool.append(it.strip())
        caption = random.choice(pool) if pool else "New exclusive content just for you 💜"

    fixed_tags = get_char_fixed_tags(char_id)
    extra_tags = post_data.get("extra_hashtags") or post_data.get("draft_hashtags") or post_data.get("hashtags") or []
    merged_tags = normalize_hashtags(list(fixed_tags) + list(extra_tags))
    full_text = build_full_text(caption, merged_tags)
    return caption, merged_tags, full_text


def _scan_scheduled_times_in_root(root_dir: str) -> list[datetime]:
    found = []
    if not root_dir or not os.path.isdir(root_dir):
        return found

    sub_roots = [root_dir]
    for sub in ("pending", "scheduled", "posted"):
        sp = os.path.join(root_dir, sub)
        if os.path.isdir(sp):
            sub_roots.append(sp)

    for base in sub_roots:
        for name in os.listdir(base):
            if base == root_dir and name in ("pending", "scheduled", "posted"):
                continue
            p = os.path.join(base, name)
            if not os.path.isdir(p):
                continue
            fjson = os.path.join(p, "final_post.json")
            if not os.path.exists(fjson):
                continue
            try:
                with open(fjson, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                sched_str = str(meta.get("scheduled_at") or "").strip()
                if sched_str:
                    dt = datetime.strptime(sched_str[:16], "%Y-%m-%d %H:%M")
                    found.append(dt)
            except Exception:
                continue
    return found


def collect_existing_scheduled_times(char_id: str) -> list[datetime]:
    """02_ready_to_post と 03_posted の両方に存在する全 final_post.json から予約日時を取得"""
    paths = get_char_paths(char_id)
    booked = []
    booked.extend(_scan_scheduled_times_in_root(paths["ready"]))
    booked.extend(_scan_scheduled_times_in_root(paths["posted"]))
    return booked


def compute_next_available_slot(char_id: str, now: datetime = None) -> tuple[str, str]:
    """
    profile.json の schedule_slots（昇順）を参照し、
    02_ready_to_post と 03_posted の既存予約と重複しない直近の未来空き枠を計算する。
    """
    slots = get_schedule_slots(char_id)
    if not slots:
        return "", "スケジュール枠が未設定です。⚙️スケジュール枠設定から投稿時刻を1つ以上追加してください"

    parsed_slots = []
    for s in slots:
        norm = normalize_time_slot(s)
        if norm:
            hh, mm = map(int, norm.split(":"))
            parsed_slots.append((hh, mm))
    parsed_slots.sort()

    if not parsed_slots:
        return "", "有効なスケジュール枠がありません。⚙️スケジュール枠設定から投稿時刻を追加してください"

    booked = collect_existing_scheduled_times(char_id)
    booked_exact = {b.strftime("%Y-%m-%d %H:%M") for b in booked}

    daily_quota = len(parsed_slots)
    booked_by_date: dict = {}
    for b in booked:
        d = b.date()
        booked_by_date[d] = booked_by_date.get(d, 0) + 1

    base_now = now or datetime.now()
    earliest_allowed = base_now + timedelta(minutes=15)

    for day_offset in range(365):
        target_date = (base_now + timedelta(days=day_offset)).date()
        if booked_by_date.get(target_date, 0) >= daily_quota:
            continue
        for hh, mm in parsed_slots:
            cand_dt = datetime(target_date.year, target_date.month, target_date.day, hh, mm)
            if cand_dt < earliest_allowed:
                continue
            cand_str = cand_dt.strftime("%Y-%m-%d %H:%M")
            if cand_str in booked_exact:
                continue
            if any(abs((cand_dt - b).total_seconds()) < 60 for b in booked):
                continue
            return cand_str, ""

    return "", "直近1年以内に空きスケジュール枠が見つかりませんでした"


def complete_step2_folder_to_ready(char_id: str, folder: str) -> tuple[dict, int]:
    """
    Step 2 完了確定処理:
      1. 動画 status == "done" と main_video / fyp_preview_video の実在を検証
      2. selected_slots / mosaic_slots から採用画像ファイル名を解決（0枚以上）
      3. caption_en + extra_hashtags + fixed_tags から full_text を構築
      4. schedule_slots から直近空き枠 scheduled_at を自動計算（0枠時は 400 エラー）
      5. 02_ready_to_post/<folder>/ へメディアをコピーし final_post.json（4.2スキーマ準拠）を生成
      6. 元フォルダを 99_rejected/processed_<folder> へアーカイブ移動
    """
    paths = get_char_paths(char_id)
    src_dir = os.path.join(paths["candidates"], str(folder or "").strip())
    if not folder or not os.path.isdir(src_dir):
        return {"ok": False, "error": f"候補フォルダが見つかりません: {folder}"}, 404

    post_json_path = os.path.join(src_dir, "post_data.json")
    if not os.path.exists(post_json_path):
        return {"ok": False, "error": "post_data.json が見つかりません"}, 400

    try:
        with open(post_json_path, "r", encoding="utf-8") as f:
            post_data = json.load(f)
    except Exception as e:
        return {"ok": False, "error": f"post_data.json 読み込みエラー: {e}"}, 400

    video_task = post_data.get("video_task") if isinstance(post_data.get("video_task"), dict) else {}
    main_video, fyp_preview_video, vid_err = resolve_step2_videos(src_dir, video_task)
    if vid_err:
        return {"ok": False, "error": vid_err}, 400

    selected_slots = post_data.get("selected_slots")
    if selected_slots is None and post_data.get("selected_images"):
        selected_slots = [extract_slot_id(x) for x in post_data.get("selected_images", [])]
    selected_slots = selected_slots or []
    mosaic_slots = post_data.get("mosaic_slots") or []

    adopted_images, img_errors = resolve_adopted_images(src_dir, selected_slots, mosaic_slots)
    if img_errors:
        return {"ok": False, "error": " / ".join(img_errors)}, 400

    scheduled_at, sched_err = compute_next_available_slot(char_id)
    if sched_err:
        return {"ok": False, "error": sched_err}, 400

    caption, hashtags, full_text = build_caption_and_tags_from_post_data(char_id, post_data)

    dst_dir = os.path.join(paths["ready"], folder)
    if os.path.exists(dst_dir):
        shutil.rmtree(dst_dir)
    os.makedirs(dst_dir, exist_ok=True)

    for img_name in adopted_images:
        shutil.copy2(os.path.join(src_dir, img_name), os.path.join(dst_dir, img_name))

    videos_to_copy = []
    for vf in (main_video, fyp_preview_video):
        if vf and vf not in videos_to_copy:
            videos_to_copy.append(vf)
    for vf in videos_to_copy:
        shutil.copy2(os.path.join(src_dir, vf), os.path.join(dst_dir, vf))

    final_payload = {
        "folder": folder,
        "char_id": char_id,
        "caption": caption,
        "hashtags": hashtags,
        "full_text": full_text,
        "scheduled_at": scheduled_at,
        "main_video": main_video,
        "fyp_preview_video": fyp_preview_video,
        "images": adopted_images,
    }
    with open(os.path.join(dst_dir, "final_post.json"), "w", encoding="utf-8") as f:
        json.dump(final_payload, f, ensure_ascii=False, indent=2)

    archive_dir = os.path.join(paths["trash"], f"processed_{folder}")
    if os.path.exists(archive_dir):
        shutil.rmtree(archive_dir)
    shutil.move(src_dir, archive_dir)

    return {
        "ok": True,
        "folder": folder,
        "char_id": char_id,
        "scheduled_at": scheduled_at,
        "caption": caption,
        "hashtags": hashtags,
        "full_text": full_text,
        "main_video": main_video,
        "fyp_preview_video": fyp_preview_video,
        "images": adopted_images,
        "final_post": final_payload,
    }, 200


def auto_finalize_folder_to_ready(char_id: str, folder: str) -> dict:
    """旧互換ラッパー（complete_step2_folder_to_ready を呼び出す）"""
    res, _ = complete_step2_folder_to_ready(char_id, folder)
    return res


# ==========================================
# 手動スケジュール枠設定 API（最大10枠・標準プリセット廃止）
# ==========================================
@queue_bp.route("/api/schedule_settings/<char_id>", methods=["GET"])
def api_get_schedule_settings(char_id):
    slots = get_schedule_slots(char_id)
    return jsonify({
        "ok": True,
        "char_id": char_id,
        "slots": slots,
        "schedule_slots": slots,
    })


@queue_bp.route("/api/schedule_settings/<char_id>", methods=["POST"])
def api_save_schedule_settings(char_id):
    data = request.json or {}
    raw_slots = data.get("schedule_slots") if "schedule_slots" in data else data.get("slots", [])
    saved, err = save_schedule_slots(char_id, raw_slots)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    return jsonify({
        "ok": True,
        "char_id": char_id,
        "slots": saved,
        "schedule_slots": saved,
    })


# ==========================================
# ステータス・候補取得・タイムライン一覧 API（予約済み表示・ソート対応）
# ==========================================
def _parse_bool_arg(val, default: bool = False) -> bool:
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    s = str(val).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    return default


def _extract_folder_sort_tuple(item: dict) -> tuple[int, int, str]:
    raw = str(item.get("folder") or item.get("name") or "").strip()
    m = re.match(r"^(\d+)", raw)
    if m:
        return (0, int(m.group(1)), raw)
    return (1, 0, raw)


def sort_timeline_items(items: list[dict], sort_by: str = "scheduled_at", order: str = "asc") -> list[dict]:
    """
    タイムラインアイテムを並び替える。
      - sort_by: 'scheduled_at' | 'schedule' | 'folder' | 'number' | 'file_number'
                 （'schedule_desc', 'folder_asc' 等の複合指定にも対応）
      - order:   'asc' | 'desc'
    """
    raw_sort = str(sort_by or "scheduled_at").strip().lower()
    raw_order = str(order or "").strip().lower()

    if raw_sort.endswith("_desc"):
        raw_sort = raw_sort[:-5]
        if not raw_order:
            raw_order = "desc"
    elif raw_sort.endswith("_asc"):
        raw_sort = raw_sort[:-4]
        if not raw_order:
            raw_order = "asc"

    reverse = raw_order == "desc"
    is_folder_sort = raw_sort in ("folder", "number", "file_number", "file_no", "id", "name")

    result = list(items)
    if is_folder_sort:
        result.sort(
            key=lambda x: (
                _extract_folder_sort_tuple(x),
                str(x.get("scheduled_at") or ""),
                str(x.get("archive_folder") or ""),
            ),
            reverse=reverse,
        )
    else:
        if reverse:
            # 降順時は日時設定済み（最新→過去）を先に並べ、日時未設定は末尾へ配置
            with_sched = [x for x in result if str(x.get("scheduled_at") or "").strip()]
            without_sched = [x for x in result if not str(x.get("scheduled_at") or "").strip()]
            with_sched.sort(
                key=lambda x: (str(x.get("scheduled_at")).strip(), _extract_folder_sort_tuple(x)),
                reverse=True,
            )
            without_sched.sort(key=_extract_folder_sort_tuple)
            result = with_sched + without_sched
        else:
            result.sort(
                key=lambda x: (
                    str(x.get("scheduled_at") or "").strip() or "9999-99-99 99:99",
                    _extract_folder_sort_tuple(x),
                )
            )
    return result


def collect_ready_items(char_id: str) -> list[dict]:
    paths = get_char_paths(char_id)
    ready_root = paths["ready"]
    ready = []
    seen_folders = set()

    if os.path.isdir(ready_root):
        # v3.0 フラット構造: 02_ready_to_post/<連番>/
        for name in sorted(os.listdir(ready_root)):
            if name in ("pending", "scheduled", "posted"):
                continue
            p = os.path.join(ready_root, name)
            if os.path.isdir(p):
                item = collect_queue_item(p, name, "ready", name)
                if item:
                    ready.append(item)
                    seen_folders.add(name)

        # 旧サブフォルダ構造互換: 02_ready_to_post/pending/<連番>/ 等
        for status_sub in ("pending", "scheduled"):
            sub_dir = os.path.join(ready_root, status_sub)
            if not os.path.isdir(sub_dir):
                continue
            for name in sorted(os.listdir(sub_dir)):
                if name in seen_folders:
                    continue
                p = os.path.join(sub_dir, name)
                if not os.path.isdir(p):
                    continue
                item_key = f"{status_sub}/{name}"
                item = collect_queue_item(p, name, status_sub, item_key)
                if item:
                    ready.append(item)
                    seen_folders.add(name)

    return ready


def collect_posted_items(char_id: str) -> list[dict]:
    """03_posted/<folder>/（および旧 02_ready_to_post/posted/）から予約完了済みアイテムを取得"""
    paths = get_char_paths(char_id)
    posted_root = paths["posted"]
    ready_root = paths["ready"]
    posted_items = []
    seen_dirs = set()

    if os.path.isdir(posted_root):
        for name in sorted(os.listdir(posted_root)):
            p = os.path.join(posted_root, name)
            if not os.path.isdir(p):
                continue
            item_key = f"03_posted/{name}"
            item = collect_queue_item(p, name, "posted", item_key)
            if item:
                item["status"] = "posted"
                item["posted"] = True
                posted_items.append(item)
                seen_dirs.add(name)

    legacy_posted_dir = os.path.join(ready_root, "posted")
    if os.path.isdir(legacy_posted_dir):
        for name in sorted(os.listdir(legacy_posted_dir)):
            if name in seen_dirs:
                continue
            p = os.path.join(legacy_posted_dir, name)
            if not os.path.isdir(p):
                continue
            item_key = f"posted/{name}"
            item = collect_queue_item(p, name, "posted", item_key)
            if item:
                item["status"] = "posted"
                item["posted"] = True
                posted_items.append(item)
                seen_dirs.add(name)

    return posted_items


@queue_bp.route("/api/status")
def api_status():
    char_id = request.args.get("char") or request.args.get("char_id") or "queen_lilith"
    include_posted = _parse_bool_arg(
        request.args.get("include_posted")
        if "include_posted" in request.args
        else request.args.get("show_posted"),
        default=False,
    )
    sort_by = request.args.get("sort_by") or request.args.get("sort") or "scheduled_at"
    sort_order = request.args.get("order") or request.args.get("sort_order") or ""

    staged = list_candidates_by_stage(char_id)
    step1_candidates = staged["step1"]
    step2_candidates = staged["step2"]
    all_candidates = staged["all"]

    ready_only = collect_ready_items(char_id)
    posted_only = collect_posted_items(char_id)

    sorted_ready_only = sort_timeline_items(ready_only, sort_by=sort_by, order=sort_order)
    sorted_posted_only = sort_timeline_items(posted_only, sort_by=sort_by, order=sort_order)

    if include_posted:
        timeline_items = sort_timeline_items(ready_only + posted_only, sort_by=sort_by, order=sort_order)
    else:
        timeline_items = sorted_ready_only

    schedule_slots = get_schedule_slots(char_id)

    return jsonify({
        "ok": True,
        "char_id": char_id,
        "characters": list_all_characters(),
        "step1_candidates": step1_candidates,
        "step2_candidates": step2_candidates,
        "candidates": all_candidates,
        "ready": timeline_items,
        "ready_only": sorted_ready_only,
        "posted": sorted_posted_only,
        "timeline": timeline_items,
        "ready_count": len(ready_only),
        "posted_count": len(posted_only),
        "include_posted": include_posted,
        "sort_by": sort_by,
        "order": sort_order or ("desc" if str(sort_by).lower().endswith("_desc") else "asc"),
        "schedule_slots": schedule_slots,
    })


@queue_bp.route("/api/timeline")
@queue_bp.route("/api/queue/list")
def api_timeline_list():
    """タイムライン専用の投稿一覧取得API（予約済み 03_posted 表示切替・ソート順指定対応）"""
    char_id = request.args.get("char") or request.args.get("char_id") or "queen_lilith"
    include_posted = _parse_bool_arg(
        request.args.get("include_posted")
        if "include_posted" in request.args
        else request.args.get("show_posted"),
        default=False,
    )
    sort_by = request.args.get("sort_by") or request.args.get("sort") or "scheduled_at"
    sort_order = request.args.get("order") or request.args.get("sort_order") or ""

    ready_only = collect_ready_items(char_id)
    posted_only = collect_posted_items(char_id)

    if include_posted:
        items = sort_timeline_items(ready_only + posted_only, sort_by=sort_by, order=sort_order)
    else:
        items = sort_timeline_items(ready_only, sort_by=sort_by, order=sort_order)

    return jsonify({
        "ok": True,
        "char_id": char_id,
        "include_posted": include_posted,
        "sort_by": sort_by,
        "order": sort_order or ("desc" if str(sort_by).lower().endswith("_desc") else "asc"),
        "ready_count": len(ready_only),
        "posted_count": len(posted_only),
        "items": items,
        "ready": items,
        "posted": sort_timeline_items(posted_only, sort_by=sort_by, order=sort_order),
    })


@queue_bp.route("/api/candidate/<char_id>/<folder>")
def api_candidate_detail(char_id, folder):
    paths = get_char_paths(char_id)
    p = os.path.join(paths["candidates"], folder)
    if not os.path.isdir(p):
        return jsonify({"ok": False, "error": "フォルダが見つかりません"}), 404

    imgs = get_images_in_dir(p)
    vids = get_videos_in_dir(p)
    slots = group_candidate_slots(p)
    post_data = {}
    json_path = os.path.join(p, "post_data.json")
    if os.path.exists(json_path):
        with open(json_path, "r", encoding="utf-8") as f:
            post_data = json.load(f)

    summary = build_candidate_summary(p, folder, post_data)
    post_data["stage"] = summary["stage"]
    post_data["selected_slots"] = summary["selected_slots"]
    post_data["mosaic_slots"] = summary["mosaic_slots"]
    post_data["video_model"] = summary["video_model"]
    post_data["s2v_prompt"] = summary["s2v_prompt"]
    post_data["video_task"] = summary["video_task"]

    return jsonify({
        "ok": True,
        "char_id": char_id,
        "folder": folder,
        "stage": summary["stage"],
        "images": imgs,
        "videos": vids,
        "slots": slots,
        "selected_slots": summary["selected_slots"],
        "mosaic_slots": summary["mosaic_slots"],
        "video_model": summary["video_model"],
        "s2v_prompt": summary["s2v_prompt"],
        "video_task": summary["video_task"],
        "post_data": post_data,
    })


# ==========================================
# Step 1 保存 ＆ Step 2 完了確定 API（直線コンベア）
# ==========================================
@queue_bp.route("/api/candidate/save_step1", methods=["POST"])
def api_candidate_save_step1():
    """
    Step 1 保存API:
      - 動画ベース画像（source_image）1枚必須バリデーション
      - selected_slots（0枚以上可）, mosaic_slots, video_task（status: "queued"）を保存
      - stage を "step2" に更新し、Step 1 一覧から即座に除外する
    """
    data = request.json or {}
    char_id = data.get("char_id", "queen_lilith")
    folder = str(data.get("folder") or "").strip()

    if not folder:
        return jsonify({"ok": False, "error": "folder が指定されていません"}), 400

    paths = get_char_paths(char_id)
    cand_dir = os.path.join(paths["candidates"], folder)
    if not os.path.isdir(cand_dir):
        return jsonify({"ok": False, "error": "対象フォルダが存在しません"}), 404

    vtask_in = data.get("video_task") if isinstance(data.get("video_task"), dict) else {}
    source_image = str(
        data.get("source_image")
        or data.get("video_source_image")
        or vtask_in.get("source_image")
        or ""
    ).strip()

    if not source_image:
        return jsonify({"ok": False, "error": "動画ベース画像を1枚選択してください（全投稿で動画1本が必須です）"}), 400

    if not os.path.exists(os.path.join(cand_dir, source_image)):
        sid = extract_slot_id(source_image)
        fallback_sd = f"{sid}_SD.png"
        fallback_hq = f"{sid}_HQ.png"
        if sid and os.path.exists(os.path.join(cand_dir, fallback_sd)):
            source_image = fallback_sd
        elif sid and os.path.exists(os.path.join(cand_dir, fallback_hq)):
            source_image = fallback_hq
        else:
            return jsonify({"ok": False, "error": f"指定された動画ベース画像が見つかりません: {source_image}"}), 400

    post_json_path = os.path.join(cand_dir, "post_data.json")
    post_data = {}
    if os.path.exists(post_json_path):
        try:
            with open(post_json_path, "r", encoding="utf-8") as f:
                post_data = json.load(f)
        except Exception:
            post_data = {}

    raw_selected = data.get("selected_slots")
    if raw_selected is None and "selected_images" in data:
        raw_selected = data.get("selected_images") or []
    if raw_selected is None:
        raw_selected = post_data.get("selected_slots") or []

    selected_slots = []
    for s in raw_selected:
        sid = extract_slot_id(s)
        if sid and sid not in selected_slots:
            selected_slots.append(sid)

    raw_mosaic = data.get("mosaic_slots")
    if raw_mosaic is None:
        raw_mosaic = post_data.get("mosaic_slots")
    if raw_mosaic is None:
        raw_mosaic = [
            s["slot_id"] for s in group_candidate_slots(cand_dir)
            if s.get("mosaic")
        ]

    mosaic_slots = []
    for s in (raw_mosaic or []):
        sid = extract_slot_id(s)
        if sid and sid not in mosaic_slots:
            mosaic_slots.append(sid)

    _, img_errors = resolve_adopted_images(cand_dir, selected_slots, mosaic_slots)
    if img_errors:
        return jsonify({"ok": False, "error": " / ".join(img_errors)}), 400

    vt = post_data.get("video_task") if isinstance(post_data.get("video_task"), dict) else {}
    prev_source = vt.get("source_image") or ""
    prev_model = normalize_video_model(
        vt.get("video_model")
        or vt.get("model")
        or post_data.get("video_model")
        or post_data.get("model")
    )
    raw_requested_model = (
        data.get("video_model")
        or data.get("model")
        or vtask_in.get("video_model")
        or vtask_in.get("model")
        or vt.get("video_model")
        or vt.get("model")
        or post_data.get("video_model")
        or post_data.get("model")
    )
    video_model = normalize_video_model(raw_requested_model)
    motion_prompt = str(
        data.get("motion_prompt")
        or vtask_in.get("motion_prompt")
        or vt.get("motion_prompt")
        or post_data.get("video_motion_jp")
        or "ゆっくりと呼吸しながら微笑み、髪と衣装がわずかに揺れ、カメラがゆっくりとズームインする"
    ).strip()
    s2v_prompt = str(
        data.get("s2v_prompt")
        or vtask_in.get("s2v_prompt")
        or vt.get("s2v_prompt")
        or post_data.get("s2v_prompt")
        or post_data.get("caption_ja")
        or "恥じらいながらこちらを見つめ、熱い吐息混じりに語りかける"
    ).strip()

    out_vid = vt.get("output_video") or "video_output.mp4"
    keep_done = (
        prev_source == source_image
        and prev_model == video_model
        and vt.get("status") == "done"
        and out_vid
        and os.path.exists(os.path.join(cand_dir, out_vid))
    )

    vt["video_model"] = video_model
    vt["source_image"] = source_image
    vt["motion_prompt"] = motion_prompt
    vt["s2v_prompt"] = s2v_prompt
    vt["output_video"] = out_vid
    if keep_done:
        vt["status"] = "done"
    else:
        vt["status"] = "queued"
        vt["main_video"] = ""
        vt["fyp_preview_video"] = ""
        vt["mosaic_mode"] = "none"

    post_data["stage"] = "step2"
    post_data["selected_slots"] = selected_slots
    post_data["mosaic_slots"] = mosaic_slots
    post_data["video_model"] = video_model
    post_data["s2v_prompt"] = s2v_prompt
    post_data["video_task"] = vt
    post_data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with open(post_json_path, "w", encoding="utf-8") as f:
        json.dump(post_data, f, ensure_ascii=False, indent=2)

    return jsonify({
        "ok": True,
        "char_id": char_id,
        "folder": folder,
        "stage": "step2",
        "selected_slots": selected_slots,
        "mosaic_slots": mosaic_slots,
        "video_model": video_model,
        "s2v_prompt": s2v_prompt,
        "video_task": vt,
        "message": f"🎬 Step 1 を保存し、Step 2（動画生成キュー [{video_model}]）へ移動しました",
    })


@queue_bp.route("/api/candidate/back_to_step1", methods=["POST"])
def api_candidate_back_to_step1():
    """Step 2 から Step 1 へ戻す API"""
    data = request.json or {}
    char_id = data.get("char_id", "queen_lilith")
    folder = str(data.get("folder") or "").strip()
    if not folder:
        return jsonify({"ok": False, "error": "folder が指定されていません"}), 400

    paths = get_char_paths(char_id)
    cand_dir = os.path.join(paths["candidates"], folder)
    post_json_path = os.path.join(cand_dir, "post_data.json")
    if not os.path.isdir(cand_dir) or not os.path.exists(post_json_path):
        return jsonify({"ok": False, "error": "候補フォルダが見つかりません"}), 404

    with open(post_json_path, "r", encoding="utf-8") as f:
        post_data = json.load(f)

    post_data["stage"] = "step1"
    with open(post_json_path, "w", encoding="utf-8") as f:
        json.dump(post_data, f, ensure_ascii=False, indent=2)

    return jsonify({"ok": True, "char_id": char_id, "folder": folder, "stage": "step1"})


@queue_bp.route("/api/candidate/complete_step2", methods=["POST"])
def api_candidate_complete_step2():
    """Step 2 完了確定API"""
    data = request.json or {}
    char_id = data.get("char_id", "queen_lilith")
    folder = str(data.get("folder") or "").strip()
    if not folder:
        return jsonify({"ok": False, "error": "folder が指定されていません"}), 400

    res, status_code = complete_step2_folder_to_ready(char_id, folder)
    return jsonify(res), status_code


@queue_bp.route("/api/approve", methods=["POST"])
def api_approve():
    """旧エンドポイント互換（complete_step2 と同じ処理を実行）"""
    data = request.json or {}
    char_id = data.get("char_id", "queen_lilith")
    folder = str(data.get("folder") or "").strip()
    res, status_code = complete_step2_folder_to_ready(char_id, folder)
    return jsonify(res), status_code


# ==========================================
# タイムライン編集・差し戻し・日英キャラ翻訳・ボツ処理
# ==========================================
def _handle_queue_update(data: dict):
    char_id = data.get("char_id", "queen_lilith")
    item_key = data.get("folder") or data.get("item_key")
    dst_dir = resolve_queue_folder_path(char_id, item_key)
    if not dst_dir or not os.path.isdir(dst_dir):
        return jsonify({"ok": False, "error": "対象キューが見つかりません"}), 404

    folder_name = os.path.basename(dst_dir)
    json_path = os.path.join(dst_dir, "final_post.json")
    if os.path.exists(json_path):
        with open(json_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    else:
        payload = {
            "folder": folder_name,
            "char_id": char_id,
            "images": get_images_in_dir(dst_dir),
        }

    scheduled_at = str(data.get("scheduled_at", payload.get("scheduled_at", ""))).strip()
    if scheduled_at:
        try:
            dt = datetime.strptime(scheduled_at[:16], "%Y-%m-%d %H:%M")
            scheduled_at = dt.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return jsonify({"ok": False, "error": f"予約日時の形式が不正です: {scheduled_at}（YYYY-MM-DD HH:MM）"}), 400

    caption = str(data.get("caption", payload.get("caption", ""))).strip()
    raw_hashtags = data.get("hashtags", payload.get("hashtags", []))
    hashtags = normalize_hashtags(raw_hashtags)
    full_text = build_full_text(caption, hashtags)

    payload["folder"] = payload.get("folder") or payload.get("folder_id") or folder_name
    payload["char_id"] = payload.get("char_id") or char_id
    payload["scheduled_at"] = scheduled_at
    payload["caption"] = caption
    payload["hashtags"] = hashtags
    payload["full_text"] = full_text

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    return jsonify({
        "ok": True,
        "folder": payload["folder"],
        "scheduled_at": scheduled_at,
        "caption": caption,
        "hashtags": hashtags,
        "full_text": full_text,
        "final_post": payload,
    }), 200


@queue_bp.route("/api/queue/update", methods=["POST"])
def api_queue_update():
    return _handle_queue_update(request.json or {})


@queue_bp.route("/api/update_ready", methods=["POST"])
def api_update_ready():
    return _handle_queue_update(request.json or {})


def _handle_queue_revert(data: dict):
    """確定済み投稿（02_ready_to_post）を 01_candidates の stage: 'step2' へ差し戻す"""
    char_id = data.get("char_id", "queen_lilith")
    item_key = data.get("folder") or data.get("item_key")
    ready_dir = resolve_queue_folder_path(char_id, item_key)
    if not ready_dir or not os.path.isdir(ready_dir):
        return jsonify({"ok": False, "error": "対象キューが見つかりません"}), 404

    paths = get_char_paths(char_id)
    posted_abs = os.path.abspath(paths["posted"])
    ready_abs = os.path.abspath(ready_dir)
    if ready_abs == posted_abs or ready_abs.startswith(posted_abs + os.sep):
        return jsonify({"ok": False, "error": "Fansly予約完了済み（03_posted）の投稿は差し戻しできません"}), 400

    folder = os.path.basename(ready_dir)
    paths = get_char_paths(char_id)
    archive_dir = os.path.join(paths["trash"], f"processed_{folder}")
    cand_dir = os.path.join(paths["candidates"], folder)

    final_meta = {}
    fjson_path = os.path.join(ready_dir, "final_post.json")
    if os.path.exists(fjson_path):
        try:
            with open(fjson_path, "r", encoding="utf-8") as f:
                final_meta = json.load(f)
        except Exception:
            final_meta = {}

    if os.path.exists(cand_dir):
        shutil.rmtree(cand_dir)

    if os.path.exists(archive_dir):
        shutil.move(archive_dir, cand_dir)
        shutil.rmtree(ready_dir, ignore_errors=True)
    else:
        shutil.move(ready_dir, cand_dir)

    post_json_path = os.path.join(cand_dir, "post_data.json")
    post_data = {}
    if os.path.exists(post_json_path):
        try:
            with open(post_json_path, "r", encoding="utf-8") as f:
                post_data = json.load(f)
        except Exception:
            post_data = {}

    post_data["stage"] = "step2"
    if final_meta.get("caption"):
        post_data["caption_en"] = final_meta["caption"]

    vtask = post_data.get("video_task") if isinstance(post_data.get("video_task"), dict) else {}
    vtask["status"] = "done"
    if final_meta.get("main_video"):
        vtask["main_video"] = final_meta["main_video"]
    if final_meta.get("fyp_preview_video"):
        vtask["fyp_preview_video"] = final_meta["fyp_preview_video"]
    post_data["video_task"] = vtask

    with open(post_json_path, "w", encoding="utf-8") as f:
        json.dump(post_data, f, ensure_ascii=False, indent=2)

    return jsonify({
        "ok": True,
        "char_id": char_id,
        "folder": folder,
        "stage": "step2",
    }), 200


@queue_bp.route("/api/queue/revert", methods=["POST"])
def api_queue_revert():
    return _handle_queue_revert(request.json or {})


@queue_bp.route("/api/unapprove", methods=["POST"])
def api_unapprove():
    return _handle_queue_revert(request.json or {})


@queue_bp.route("/api/translate_caption", methods=["POST"])
def api_translate_caption():
    """
    日本語 → キャラクター口調の英語キャプション（絵文字付き）への翻訳 API。
    profile.json の persona_prompt / character_persona をシステムプロンプトとして Ollama に渡す。
    """
    data = request.json or {}
    char_id = data.get("char_id", "queen_lilith")
    text_ja = str(
        data.get("text_ja")
        or data.get("japanese_text")
        or data.get("text")
        or data.get("prompt")
        or ""
    ).strip()

    if not text_ja:
        return jsonify({"ok": False, "error": "翻訳する日本語テキストを入力してください"}), 400

    persona_prompt = get_char_persona_prompt(char_id)
    system_prompt = (
        f"{persona_prompt}\n\n"
        "You are writing a short, alluring English social media post caption on Fansly fully in-character.\n"
        "Translate and adapt the user's Japanese draft into natural, captivating English that matches the character's voice, tone, relationship with the viewer, and preferred emojis.\n"
        "- Include 1 to 3 fitting emojis.\n"
        "- Do NOT include any hashtags (#...).\n"
        "- Return STRICTLY valid JSON only in the following format:\n"
        '{"caption_en": "<translated in-character English caption with emojis>"}'
    )
    user_prompt = f"Japanese draft to translate in-character:\n{text_ja}"

    payload = {
        "model": OLLAMA_MODEL_TEXT,
        "prompt": user_prompt,
        "system": system_prompt,
        "format": "json",
        "think": False,
        "stream": False,
    }

    try:
        resp = requests.post(OLLAMA_URL, json=payload, timeout=90)
        resp.raise_for_status()
        res_json = resp.json()
        raw_text = str(res_json.get("response") or "").strip()

        caption_en = ""
        m = re.search(r"\{[\s\S]*\}", raw_text)
        if m:
            try:
                parsed = json.loads(m.group(0))
                caption_en = str(
                    parsed.get("caption_en")
                    or parsed.get("caption")
                    or parsed.get("en")
                    or parsed.get("translation")
                    or ""
                ).strip()
            except Exception:
                caption_en = ""

        if not caption_en:
            caption_en = raw_text.strip().strip('"').strip("'")

        caption_en = re.sub(r"\s*#[A-Za-z0-9_]+", "", caption_en).strip()
        if not caption_en:
            return jsonify({"ok": False, "error": "Ollama から有効な英語キャプションを取得できませんでした"}), 502

        return jsonify({
            "ok": True,
            "char_id": char_id,
            "text_ja": text_ja,
            "caption_en": caption_en,
            "caption": caption_en,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": f"Ollama 翻訳エラー: {e}"}), 502


@queue_bp.route("/api/reject", methods=["POST"])
def api_reject():
    data = request.json or {}
    char_id = data.get("char_id", "queen_lilith")
    folder = str(data.get("folder") or "").strip()
    if not folder:
        return jsonify({"ok": False, "error": "folder が指定されていません"}), 400
    paths = get_char_paths(char_id)
    src_dir = os.path.join(paths["candidates"], folder)
    dst_dir = os.path.join(paths["trash"], f"rejected_{folder}")
    if os.path.exists(dst_dir):
        shutil.rmtree(dst_dir)
    if os.path.exists(src_dir):
        shutil.move(src_dir, dst_dir)
    return jsonify({"ok": True})


@queue_bp.route("/api/folder-status/<char_id>/<folder_name>", methods=["GET"])
def api_folder_status(char_id, folder_name):
    """フォルダのロック状態を取得"""
    try:
        status = get_folder_lock_status(char_id, folder_name)
        return jsonify(status)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@queue_bp.route("/images/<char_id>/queue/<path:item_key>/<filename>")
def serve_queue_media_fallback(char_id, item_key, filename):
    dir_path = resolve_queue_folder_path(char_id, item_key)
    if not dir_path or not os.path.isdir(dir_path):
        return jsonify({"error": "Not Found"}), 404
    return send_from_directory(dir_path, filename)