import json
import mimetypes
import os
import re
import tempfile
from datetime import datetime
from typing import Any, Dict, List, Tuple

from flask import Blueprint, Response, jsonify, request, send_file
from werkzeug.utils import secure_filename

from app.core.database import (
    get_ppv_stats_by_char,
    get_system_reply_delay_settings,
    get_system_stage_thresholds,
    normalize_char_threshold_bounds,
    normalize_reply_delay_bounds,
    normalize_stage_thresholds,
    save_system_reply_delay_settings,
    save_system_stage_thresholds,
    validate_stage_thresholds_strict,
)
from app.core.llm_client import llm_client
from app.services.character_loader import (
    detect_media_type,
    ensure_character_files,
    get_char_paths,
    get_next_vault_sequence,
    list_character_ids,
    load_dm_persona,
    load_settings,
    load_vault_items,
    save_settings,
    save_vault_items,
)
from app.services.media_blur_service import (
    BLUR_SUBDIR_NAME,
    cleanup_blur_preview,
    extract_first_frame_image,
    generate_blur_previews_for_all_characters,
    generate_blur_previews_for_character,
    generate_blurred_preview,
    get_blur_preview_relpath,
    is_video_media,
    prepare_vault_media_assets,
    resolve_existing_blur_preview_path,
)

vault_bp = Blueprint("vault_bp", __name__)


def _parse_tags(raw_tags: Any) -> List[str]:
    if isinstance(raw_tags, list):
        return [str(t).strip().lstrip("#") for t in raw_tags if str(t).strip()]
    if isinstance(raw_tags, str):
        return [t.strip().lstrip("#") for t in re.split(r"[,、\s]+", raw_tags) if t.strip()]
    return []


def _needs_vision_enrichment(tags: List[str], description_memo: str) -> bool:
    """タグや売り込みメモが未入力、またはファイル名由来の英数字トークンのみの場合は Vision 解析を実行する"""
    if not tags or not description_memo:
        return True
    ja_re = re.compile(r"[\u3040-\u30ff\u4e00-\u9faf]")
    generic_ascii = {"fyp", "uncensored", "video", "image", "mp4", "step1", "step2", "step3", "secret", "clip"}
    if all(t.lower() in generic_ascii for t in tags):
        return True
    if not any(ja_re.search(t) for t in tags) and not ja_re.search(description_memo):
        return True
    return False


MIME_OVERRIDES = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".mkv": "video/x-matroska",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
}


def resolve_vault_media_file(char_id: str, filename: str) -> Tuple[str, str, bool]:
    """Windowsバックスラッシュ等を正規化し、ppv_vault（および previews サブフォルダ）内の実在ファイルパスと存在有無を返す"""
    paths = get_char_paths(char_id)
    vault_dir = os.path.abspath(paths["vault_dir"])
    normalized_raw = str(filename or "").replace("\\", "/").strip().lstrip("/")
    if not normalized_raw:
        return "", "", False

    parts = [p for p in normalized_raw.split("/") if p and p not in (".", "..")]
    if not parts:
        return "", "", False

    if len(parts) == 2 and parts[0].lower() == BLUR_SUBDIR_NAME:
        rel_clean = f"{BLUR_SUBDIR_NAME}/{os.path.basename(parts[1])}"
        target_dir = os.path.abspath(os.path.join(vault_dir, BLUR_SUBDIR_NAME))
        full_path = os.path.abspath(os.path.join(target_dir, os.path.basename(parts[1])))
        try:
            if os.path.commonpath([vault_dir, full_path]) != vault_dir:
                return rel_clean, full_path, False
        except ValueError:
            return rel_clean, full_path, False
        if os.path.isfile(full_path) and os.path.getsize(full_path) > 0:
            return rel_clean, full_path, True
        return rel_clean, full_path, False

    safe_name = os.path.basename(parts[-1])
    if not safe_name:
        return "", "", False
    full_path = os.path.abspath(os.path.join(vault_dir, safe_name))
    try:
        if os.path.commonpath([vault_dir, full_path]) != vault_dir:
            return safe_name, full_path, False
    except ValueError:
        return safe_name, full_path, False
    if os.path.isfile(full_path) and os.path.getsize(full_path) > 0:
        return safe_name, full_path, True
    if os.path.isdir(vault_dir):
        lower_target = safe_name.lower()
        for entry in os.listdir(vault_dir):
            if entry.lower() == lower_target:
                cand = os.path.abspath(os.path.join(vault_dir, entry))
                if os.path.isfile(cand) and os.path.getsize(cand) > 0:
                    return entry, cand, True
    return safe_name, full_path, False


@vault_bp.route("/api/vault/preview_draft", methods=["POST"])
def api_vault_preview_draft():
    """ファイル名やキャラ設定をもとに推奨タグと英語 pitch_hint・初期価格3種を返却"""
    data = request.get_json(silent=True) or request.form.to_dict() or {}
    char_id = str(data.get("char_id") or "").strip()
    if not char_id or char_id == "all":
        chars = list_character_ids()
        char_id = chars[0] if chars else "Sylphy-Mia"

    filename = str(data.get("filename") or "secret_clip.mp4").strip()
    media_type = str(data.get("media_type") or "").strip().lower()
    if media_type not in ("video", "image"):
        media_type = detect_media_type(filename)

    try:
        ladder_step = int(data.get("ladder_step") or 1)
    except Exception:
        ladder_step = 1
    ladder_step = max(1, min(3, ladder_step))

    persona = load_dm_persona(char_id)
    settings = load_settings(char_id)
    step_prices = (settings.get("default_prices") or {}).get(str(ladder_step)) or {
        "base_price_usd": 9.99,
        "tripwire_price_usd": 5.99,
        "whale_price_usd": 19.99,
    }

    draft = llm_client.generate_ppv_draft(
        char_id=char_id,
        persona=persona,
        filename=filename,
        media_type=media_type,
        ladder_step=ladder_step,
    )

    return jsonify(
        {
            "ok": True,
            "char_id": char_id,
            "filename": filename,
            "media_type": media_type,
            "ladder_step": ladder_step,
            "base_price_usd": step_prices["base_price_usd"],
            "tripwire_price_usd": step_prices["tripwire_price_usd"],
            "whale_price_usd": step_prices["whale_price_usd"],
            "tags": draft["tags"],
            "description_memo": draft.get("description_memo") or "",
            "pitch_note": draft.get("description_memo") or "",
            "pitch_hint": draft["pitch_hint"],
        }
    )


@vault_bp.route("/api/vault/analyze_vision", methods=["POST"])
def api_vault_analyze_vision():
    """
    ローカルOllamaのVision対応モデルを用いてPPV画像/動画を自動解析し、
    自然な日本語の「タグ (tags)」と「売り込みメモ (description_memo / pitch_note)」を生成する。
    - 新規追加時: multipart/form-data で `file` を受け取り一時保存して解析
    - 既存素材再解析時: JSON または form で `char_id` と `ppv_id` (または `filename`) を受け取り解析し、
      `save_to_vault` が True (デフォルト) なら ppv_vault DB および vault_index.json も即時更新する。
    """
    data = (request.get_json(silent=True) or {}) if request.is_json else (request.form.to_dict() or {})
    char_id = str(data.get("char_id") or "").strip()
    if not char_id or char_id == "all":
        chars = list_character_ids()
        char_id = chars[0] if chars else "Sylphy-Mia"

    ppv_id = str(data.get("ppv_id") or "").strip()
    save_to_vault = str(data.get("save_to_vault", "true" if ppv_id else "false")).lower() in ("1", "true", "yes")
    try:
        ladder_step = max(1, min(3, int(data.get("ladder_step") or 1)))
    except Exception:
        ladder_step = 1

    persona = load_dm_persona(char_id)
    settings = load_settings(char_id)
    step_prices = (settings.get("default_prices") or {}).get(str(ladder_step)) or {
        "base_price_usd": 9.99,
        "tripwire_price_usd": 5.99,
        "whale_price_usd": 19.99,
    }

    uploaded_file = request.files.get("file") or request.files.get("media")
    tmp_path = None
    thumb_tmp_path = None
    target_media_path = None
    filename = str(data.get("filename") or "secret_clip.mp4").strip()
    target_item = None
    items = load_vault_items(char_id) if ppv_id else []

    try:
        if uploaded_file and uploaded_file.filename:
            filename = uploaded_file.filename
            ext = os.path.splitext(filename)[1].lower() or ".mp4"
            fd, tmp_path = tempfile.mkstemp(suffix=ext, prefix="ppv_vision_upload_")
            os.close(fd)
            uploaded_file.save(tmp_path)
            target_media_path = tmp_path
        elif ppv_id:
            for it in items:
                if it.get("ppv_id") == ppv_id:
                    target_item = it
                    filename = str(it.get("filename") or filename)
                    ladder_step = int(it.get("ladder_step") or ladder_step)
                    break
            safe_name, full_path, exists = resolve_vault_media_file(char_id, filename)
            if exists:
                target_media_path = full_path
                filename = safe_name
        elif filename:
            safe_name, full_path, exists = resolve_vault_media_file(char_id, filename)
            if exists:
                target_media_path = full_path
                filename = safe_name

        media_type = detect_media_type(filename)
        if target_media_path and is_video_media(target_media_path, media_type=media_type):
            media_type = "video"
            thumb_tmp_path = extract_first_frame_image(
                media_path=target_media_path,
                media_type="video",
                max_side=1024,
                jpeg_quality=90,
            )

        vision_res = llm_client.analyze_ppv_media_with_vision(
            char_id=char_id,
            persona=persona,
            media_path=target_media_path,
            filename=filename,
            media_type=media_type,
            ladder_step=ladder_step,
            auto_pull_model=True,
            thumbnail_image_path=thumb_tmp_path,
        )

        blur_rel = ""
        blur_exists = False
        if target_media_path and tmp_path is None and os.path.isfile(target_media_path):
            existing_blur = resolve_existing_blur_preview_path(target_media_path)
            if not existing_blur:
                existing_blur = generate_blurred_preview(target_media_path, media_type=media_type)
            if existing_blur and os.path.isfile(existing_blur):
                blur_rel = get_blur_preview_relpath(filename)
                blur_exists = True

        saved_item = None
        if ppv_id and target_item is not None and save_to_vault:
            target_item["tags"] = vision_res["tags"]
            target_item["tags_json"] = json.dumps(vision_res["tags"], ensure_ascii=False)
            target_item["description_memo"] = vision_res["description_memo"]
            target_item["pitch_note"] = vision_res["description_memo"]
            if vision_res.get("pitch_hint"):
                target_item["pitch_hint"] = vision_res["pitch_hint"]
            if blur_rel:
                target_item["blur_preview_filename"] = blur_rel
            saved_list = save_vault_items(char_id, items)
            saved_item = next((x for x in saved_list if x.get("ppv_id") == ppv_id), target_item)

        return jsonify(
            {
                "ok": True,
                "char_id": char_id,
                "ppv_id": ppv_id or None,
                "filename": filename,
                "blur_preview_filename": blur_rel or None,
                "blur_preview_exists": blur_exists,
                "media_type": media_type,
                "ladder_step": ladder_step,
                "base_price_usd": step_prices["base_price_usd"],
                "tripwire_price_usd": step_prices["tripwire_price_usd"],
                "whale_price_usd": step_prices["whale_price_usd"],
                "tags": vision_res["tags"],
                "tags_json": json.dumps(vision_res["tags"], ensure_ascii=False),
                "description_memo": vision_res["description_memo"],
                "pitch_note": vision_res["description_memo"],
                "pitch_hint": vision_res["pitch_hint"],
                "used_vision": vision_res.get("used_vision", False),
                "vision_model": vision_res.get("vision_model"),
                "extracted_thumbnail_used": vision_res.get("extracted_thumbnail_used", bool(thumb_tmp_path)),
                "item": saved_item,
            }
        )
    finally:
        for cleanup_p in (thumb_tmp_path, tmp_path):
            if cleanup_p and os.path.exists(cleanup_p):
                try:
                    os.remove(cleanup_p)
                except Exception:
                    pass


@vault_bp.route("/api/vault/upload", methods=["POST"])
def api_vault_upload():
    """
    動画・画像ファイルを ppv_vault\\ へ保存（連番リネーム・media_type 自動判定）し、
    1) 動画の場合は1フレーム目のサムネイル静止画を抽出して Qwen 2.5 VL へ渡して日本語タグ＆売り込みメモを自動生成
    2) 画像・動画ともに全画面強ぼかしプレビュー画像（ppv_vault/previews/<stem>_blur.jpg）を自動生成・保存
    して武器庫および DB へ登録する。
    """
    data = (request.get_json(silent=True) or {}) if request.is_json else (request.form.to_dict() or {})
    char_id = str(data.get("char_id") or "").strip()
    if not char_id or char_id == "all":
        chars = list_character_ids()
        char_id = chars[0] if chars else "Sylphy-Mia"

    paths = ensure_character_files(char_id)
    existing_items = load_vault_items(char_id)
    seq_num = get_next_vault_sequence(char_id, existing_items)
    ppv_id = f"{char_id}_vault_{seq_num:03d}"

    uploaded_file = request.files.get("file") or request.files.get("media")
    if uploaded_file and uploaded_file.filename:
        original_filename = uploaded_file.filename
    else:
        original_filename = str(data.get("filename") or f"media_{seq_num:03d}.mp4")

    ext = os.path.splitext(original_filename)[1].lower() or ".mp4"
    safe_stem = secure_filename(os.path.splitext(original_filename)[0]) or "secret"
    safe_stem = re.sub(r"^vault_\d+_", "", safe_stem)
    saved_filename = f"vault_{seq_num:03d}_{safe_stem}{ext}"
    dest_path = os.path.join(paths["vault_dir"], saved_filename)

    if uploaded_file:
        uploaded_file.save(dest_path)
    elif not os.path.exists(dest_path):
        with open(dest_path, "wb") as f:
            f.write(b"")

    media_type = detect_media_type(saved_filename)

    try:
        ladder_step = int(data.get("ladder_step") or 1)
    except Exception:
        ladder_step = 1
    ladder_step = max(1, min(3, ladder_step))

    settings = load_settings(char_id)
    step_defaults = (settings.get("default_prices") or {}).get(str(ladder_step)) or {
        "base_price_usd": 9.99,
        "tripwire_price_usd": 5.99,
        "whale_price_usd": 19.99,
    }

    def _to_float(val: Any, fallback: float) -> float:
        try:
            return round(float(fallback if val in (None, "") else val), 2)
        except Exception:
            return round(float(fallback), 2)

    base_price = _to_float(data.get("base_price_usd"), step_defaults["base_price_usd"])
    tripwire_price = _to_float(data.get("tripwire_price_usd"), step_defaults["tripwire_price_usd"])
    whale_price = _to_float(data.get("whale_price_usd"), step_defaults["whale_price_usd"])

    tags = _parse_tags(data.get("tags"))
    description_memo = str(
        data.get("description_memo")
        if data.get("description_memo") is not None
        else (data.get("pitch_note") or "")
    ).strip()
    pitch_hint = str(data.get("pitch_hint") or "").strip()
    force_vision = str(data.get("auto_vision") or "").lower() in ("1", "true", "yes")

    used_vision = False
    vision_model_used = None
    extracted_thumbnail_used = False
    blur_preview_filename = ""
    blur_preview_path = None
    blur_preview_exists = False
    has_real_file = os.path.isfile(dest_path) and os.path.getsize(dest_path) > 0

    thumb_tmp_path: Optional[str] = None
    try:
        if has_real_file:
            assets = prepare_vault_media_assets(
                media_path=dest_path,
                media_type=media_type,
            )
            thumb_tmp_path = assets.get("thumbnail_path")
            blur_preview_path = assets.get("blur_preview_path")
            blur_preview_filename = str(assets.get("blur_preview_filename") or "")
            blur_preview_exists = bool(assets.get("blur_preview_exists"))

        need_vision_tags = force_vision or _needs_vision_enrichment(tags, description_memo)
        if need_vision_tags or not pitch_hint:
            persona = load_dm_persona(char_id)
            if has_real_file:
                draft = llm_client.analyze_ppv_media_with_vision(
                    char_id=char_id,
                    persona=persona,
                    media_path=dest_path,
                    filename=original_filename,
                    media_type=media_type,
                    ladder_step=ladder_step,
                    auto_pull_model=True,
                    thumbnail_image_path=thumb_tmp_path,
                )
                used_vision = bool(draft.get("used_vision"))
                vision_model_used = draft.get("vision_model")
                extracted_thumbnail_used = bool(draft.get("extracted_thumbnail_used"))
            else:
                draft = llm_client.generate_ppv_draft(
                    char_id=char_id,
                    persona=persona,
                    filename=original_filename,
                    media_type=media_type,
                    ladder_step=ladder_step,
                )
            tags = draft["tags"] if (force_vision or _needs_vision_enrichment(tags, description_memo) or not tags) else tags
            description_memo = (
                draft.get("description_memo") or ""
                if (force_vision or not description_memo or not re.search(r"[\u3040-\u30ff\u4e00-\u9faf]", description_memo))
                else description_memo
            )
            pitch_hint = draft["pitch_hint"] if (force_vision or not pitch_hint) else pitch_hint
    finally:
        if thumb_tmp_path and os.path.exists(thumb_tmp_path):
            try:
                os.remove(thumb_tmp_path)
            except Exception:
                pass

    new_item = {
        "ppv_id": ppv_id,
        "filename": saved_filename,
        "blur_preview_filename": blur_preview_filename,
        "media_type": media_type,
        "ladder_step": ladder_step,
        "base_price_usd": base_price,
        "tripwire_price_usd": tripwire_price,
        "whale_price_usd": whale_price,
        "tags": tags,
        "tags_json": json.dumps(tags, ensure_ascii=False),
        "description_memo": description_memo,
        "pitch_note": description_memo,
        "pitch_hint": pitch_hint,
        "is_active": True,
        "created_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }

    existing_items.append(new_item)
    save_vault_items(char_id, existing_items)

    blur_url = (
        f"/api/vault/media/{char_id}/{blur_preview_filename}"
        if blur_preview_exists and blur_preview_filename
        else ""
    )

    return jsonify(
        {
            "ok": True,
            "char_id": char_id,
            "used_vision": used_vision,
            "vision_model": vision_model_used,
            "extracted_thumbnail_used": extracted_thumbnail_used,
            "blur_preview_exists": blur_preview_exists,
            "blur_preview_filename": blur_preview_filename,
            "blur_preview_path": blur_preview_path,
            "blur_preview_url": blur_url,
            "item": {
                **new_item,
                "char_id": char_id,
                "blur_preview_exists": blur_preview_exists,
                "blur_preview_url": blur_url,
                "offers_count": 0,
                "purchases_count": 0,
                "revenue_usd": 0.0,
                "cvr_percent": 0.0,
            },
        }
    )


@vault_bp.route("/api/vault/list", methods=["GET"])
def api_vault_list():
    """
    PPV武器庫一覧取得API:
    - 階段レベル (ladder_step / step): all / 1 / 2 / 3
    - メディア種別 (media_type): all / video / image
    - ソート (sort): newest / oldest / offers_desc / purchases_desc / revenue_desc
    - DB (ppv_transactions) と集計した 提案回数・購入回数・売上金額 (USD) を付与して返却
    """
    char_id = str(request.args.get("char_id") or "").strip()
    all_chars = list_character_ids()
    if not char_id:
        char_id = all_chars[0] if all_chars else "Sylphy-Mia"

    target_chars = all_chars if char_id == "all" else [char_id]

    step_filter = str(request.args.get("ladder_step") or request.args.get("step") or "all").strip().lower()
    media_filter = str(request.args.get("media_type") or "all").strip().lower()
    sort_by = str(request.args.get("sort") or "newest").strip().lower()

    enriched_items: List[Dict[str, Any]] = []
    default_prices_by_char: Dict[str, Any] = {}
    for cid in target_chars:
        items = load_vault_items(cid)
        tx_stats = get_ppv_stats_by_char(cid)
        c_settings = load_settings(cid)
        default_prices_by_char[cid] = c_settings.get("default_prices") or {}
        for idx, item in enumerate(items):
            ppv_id = item["ppv_id"]
            safe_name, full_media_path, exists = resolve_vault_media_file(cid, str(item.get("filename") or ""))
            blur_abs = resolve_existing_blur_preview_path(full_media_path) if exists and full_media_path else None
            blur_rel = (
                get_blur_preview_relpath(safe_name or str(item.get("filename") or ""))
                if blur_abs
                else str(item.get("blur_preview_filename") or "")
            )
            st = tx_stats.get(ppv_id) or {
                "offers_count": 0,
                "purchases_count": 0,
                "revenue_usd": 0.0,
                "cvr_percent": 0.0,
            }
            enriched_items.append(
                {
                    **item,
                    "filename": safe_name or item.get("filename"),
                    "file_exists": exists,
                    "blur_preview_filename": blur_rel,
                    "blur_preview_exists": bool(blur_abs),
                    "blur_preview_url": f"/api/vault/media/{cid}/{blur_rel}" if blur_abs and blur_rel else "",
                    "char_id": cid,
                    "_order_idx": idx,
                    "offers_count": int(st["offers_count"]),
                    "purchases_count": int(st["purchases_count"]),
                    "revenue_usd": round(float(st["revenue_usd"]), 2),
                    "cvr_percent": float(st["cvr_percent"]),
                }
            )

    # 階段レベル絞り込み
    if step_filter in ("1", "2", "3"):
        step_int = int(step_filter)
        enriched_items = [it for it in enriched_items if int(it.get("ladder_step") or 1) == step_int]

    # メディア種別絞り込み
    if media_filter in ("video", "image"):
        enriched_items = [it for it in enriched_items if str(it.get("media_type") or "video") == media_filter]

    # 5種ソート
    if sort_by == "oldest":
        enriched_items.sort(key=lambda x: (str(x.get("created_at") or ""), x.get("_order_idx", 0)))
    elif sort_by == "offers_desc":
        enriched_items.sort(
            key=lambda x: (int(x.get("offers_count", 0)), float(x.get("revenue_usd", 0.0)), str(x.get("created_at") or "")),
            reverse=True,
        )
    elif sort_by == "purchases_desc":
        enriched_items.sort(
            key=lambda x: (int(x.get("purchases_count", 0)), float(x.get("revenue_usd", 0.0)), str(x.get("created_at") or "")),
            reverse=True,
        )
    elif sort_by == "revenue_desc":
        enriched_items.sort(
            key=lambda x: (float(x.get("revenue_usd", 0.0)), int(x.get("purchases_count", 0)), str(x.get("created_at") or "")),
            reverse=True,
        )
    else:
        # newest (デフォルト)
        enriched_items.sort(
            key=lambda x: (str(x.get("created_at") or ""), x.get("_order_idx", 0)),
            reverse=True,
        )

    for it in enriched_items:
        it.pop("_order_idx", None)

    return jsonify(
        {
            "ok": True,
            "char_id": char_id,
            "filters": {
                "ladder_step": step_filter,
                "media_type": media_filter,
                "sort": sort_by,
            },
            "default_prices_by_char": default_prices_by_char,
            "total": len(enriched_items),
            "items": enriched_items,
        }
    )


@vault_bp.route("/api/vault/update", methods=["POST"])
def api_vault_update():
    """PPV素材の編集・公開ON/OFFの保存"""
    data = request.get_json(silent=True) or {}
    char_id = str(data.get("char_id") or "").strip()
    ppv_id = str(data.get("ppv_id") or "").strip()

    if not char_id or not ppv_id:
        return jsonify({"ok": False, "error": "char_id and ppv_id are required"}), 400

    items = load_vault_items(char_id)
    updated_item = None
    for item in items:
        if item.get("ppv_id") == ppv_id:
            if "ladder_step" in data and data["ladder_step"] is not None:
                item["ladder_step"] = max(1, min(3, int(data["ladder_step"])))
            if "base_price_usd" in data and data["base_price_usd"] is not None:
                item["base_price_usd"] = round(float(data["base_price_usd"]), 2)
            if "tripwire_price_usd" in data and data["tripwire_price_usd"] is not None:
                item["tripwire_price_usd"] = round(float(data["tripwire_price_usd"]), 2)
            if "whale_price_usd" in data and data["whale_price_usd"] is not None:
                item["whale_price_usd"] = round(float(data["whale_price_usd"]), 2)
            if "tags" in data and data["tags"] is not None:
                parsed_t = _parse_tags(data["tags"])
                item["tags"] = parsed_t
                item["tags_json"] = json.dumps(parsed_t, ensure_ascii=False)
            if "description_memo" in data or "pitch_note" in data:
                memo_val = str(
                    data.get("description_memo")
                    if data.get("description_memo") is not None
                    else (data.get("pitch_note") or "")
                ).strip()
                item["description_memo"] = memo_val
                item["pitch_note"] = memo_val
            if "pitch_hint" in data and data["pitch_hint"] is not None:
                item["pitch_hint"] = str(data["pitch_hint"]).strip()
            if "is_active" in data and data["is_active"] is not None:
                item["is_active"] = bool(data["is_active"])
            updated_item = item
            break

    if not updated_item:
        return jsonify({"ok": False, "error": f"PPV item '{ppv_id}' not found"}), 404

    save_vault_items(char_id, items)
    tx_stats = get_ppv_stats_by_char(char_id).get(ppv_id) or {
        "offers_count": 0,
        "purchases_count": 0,
        "revenue_usd": 0.0,
        "cvr_percent": 0.0,
    }
    return jsonify(
        {
            "ok": True,
            "char_id": char_id,
            "item": {**updated_item, "char_id": char_id, **tx_stats},
        }
    )


@vault_bp.route("/api/vault/delete", methods=["POST"])
def api_vault_delete():
    """PPV素材の削除"""
    data = request.get_json(silent=True) or {}
    char_id = str(data.get("char_id") or "").strip()
    ppv_id = str(data.get("ppv_id") or "").strip()
    delete_file = bool(data.get("delete_file", True))

    if not char_id or not ppv_id:
        return jsonify({"ok": False, "error": "char_id and ppv_id are required"}), 400

    items = load_vault_items(char_id)
    target_item = None
    remaining = []
    for item in items:
        if item.get("ppv_id") == ppv_id:
            target_item = item
        else:
            remaining.append(item)

    if not target_item:
        return jsonify({"ok": False, "error": f"PPV item '{ppv_id}' not found"}), 404

    save_vault_items(char_id, remaining)

    if delete_file and target_item.get("filename"):
        paths = get_char_paths(char_id)
        file_path = os.path.join(paths["vault_dir"], os.path.basename(target_item["filename"]))
        cleanup_blur_preview(file_path)
        if os.path.isfile(file_path):
            try:
                os.remove(file_path)
            except Exception:
                pass

    return jsonify({"ok": True, "char_id": char_id, "deleted_ppv_id": ppv_id, "remaining_count": len(remaining)})


@vault_bp.route("/api/vault/generate_blur_previews", methods=["POST"])
def api_vault_generate_blur_previews():
    """既存PPV素材（特定キャラまたは全キャラ）に対して全画面ぼかしプレビュー画像を一括生成するAPI"""
    data = request.get_json(silent=True) or request.form.to_dict() or {}
    char_id = str(data.get("char_id") or "all").strip()
    force = str(data.get("force") or "").lower() in ("1", "true", "yes")

    if not char_id or char_id.lower() == "all":
        res = generate_blur_previews_for_all_characters(force=force)
    else:
        res = generate_blur_previews_for_character(char_id, force=force)
    return jsonify(res)


@vault_bp.route("/api/settings/get", methods=["GET"])
def api_settings_get():
    char_id = str(request.args.get("char_id") or "").strip()
    if not char_id or char_id == "all":
        chars = list_character_ids()
        char_id = chars[0] if chars else "Sylphy-Mia"
    settings = load_settings(char_id)
    sys_delays = get_system_reply_delay_settings()
    sys_stage_thresholds = get_system_stage_thresholds()
    settings.setdefault("min_reply_delay_sec", sys_delays["min_reply_delay_sec"])
    settings.setdefault("max_reply_delay_sec", sys_delays["max_reply_delay_sec"])
    settings.setdefault("short_msg_chars_threshold", sys_delays["short_msg_chars_threshold"])
    settings.setdefault("long_msg_chars_threshold", sys_delays["long_msg_chars_threshold"])
    settings["stage_thresholds"] = dict(sys_stage_thresholds)
    settings["stage_threshold_icebreak"] = sys_stage_thresholds["1_Icebreak"]
    settings["stage_threshold_explore"] = sys_stage_thresholds["2_Explore"]
    settings["stage_threshold_lead_pitch"] = sys_stage_thresholds["3_Lead_Pitch"]
    merged_system_settings = {
        **sys_delays,
        "stage_thresholds": dict(sys_stage_thresholds),
        "stage_threshold_icebreak": sys_stage_thresholds["1_Icebreak"],
        "stage_threshold_explore": sys_stage_thresholds["2_Explore"],
        "stage_threshold_lead_pitch": sys_stage_thresholds["3_Lead_Pitch"],
    }
    return jsonify(
        {
            "ok": True,
            "char_id": char_id,
            "settings": settings,
            "system_settings": merged_system_settings,
            "stage_thresholds": dict(sys_stage_thresholds),
        }
    )


@vault_bp.route("/api/settings/save", methods=["POST"])
def api_settings_save():
    """
    初期価格マスター (Step 1〜3: base / tripwire / whale)、
    睡眠時間設定 (sleep_schedule: enabled, start, end)、
    AI返信待機時間・文字数基準 (min_reply_delay_sec, max_reply_delay_sec, short_msg_chars_threshold, long_msg_chars_threshold)、および
    S.E.L.L. ステージ突破閾値 (stage_thresholds / stage_threshold_*) の保存。
    """
    data = request.get_json(silent=True) or {}
    char_id = str(data.get("char_id") or "").strip()
    apply_all = bool(data.get("apply_all", False))

    all_chars = list_character_ids()
    if not char_id or char_id == "all":
        char_id = all_chars[0] if all_chars else "Sylphy-Mia"

    payload: Dict[str, Any] = {}
    if isinstance(data.get("default_prices"), dict):
        payload["default_prices"] = data["default_prices"]
    if isinstance(data.get("sleep_schedule"), dict):
        payload["sleep_schedule"] = data["sleep_schedule"]
    if "bot_enabled" in data:
        payload["bot_enabled"] = bool(data["bot_enabled"])

    has_delay_update = any(
        k in data
        for k in (
            "min_reply_delay_sec",
            "max_reply_delay_sec",
            "short_msg_chars_threshold",
            "long_msg_chars_threshold",
        )
    )
    if has_delay_update:
        cur_s = load_settings(char_id)
        mn, mx = normalize_reply_delay_bounds(
            data.get("min_reply_delay_sec", cur_s.get("min_reply_delay_sec", 60)),
            data.get("max_reply_delay_sec", cur_s.get("max_reply_delay_sec", 300)),
        )
        sc, lc = normalize_char_threshold_bounds(
            data.get("short_msg_chars_threshold", cur_s.get("short_msg_chars_threshold", 20)),
            data.get("long_msg_chars_threshold", cur_s.get("long_msg_chars_threshold", 150)),
        )
        payload["min_reply_delay_sec"] = mn
        payload["max_reply_delay_sec"] = mx
        payload["short_msg_chars_threshold"] = sc
        payload["long_msg_chars_threshold"] = lc
        save_system_reply_delay_settings(mn, mx, sc, lc)

    has_stage_threshold_update = any(
        k in data
        for k in (
            "stage_thresholds",
            "stage_threshold_icebreak",
            "stage_threshold_explore",
            "stage_threshold_lead_pitch",
        )
    )
    if has_stage_threshold_update:
        is_valid, err_msg, _ = validate_stage_thresholds_strict(raw_data=data)
        if not is_valid:
            return jsonify({"ok": False, "error": err_msg}), 400
        saved_thresh = save_system_stage_thresholds(thresholds=data)
        payload["stage_thresholds"] = saved_thresh
        payload["stage_threshold_icebreak"] = saved_thresh["1_Icebreak"]
        payload["stage_threshold_explore"] = saved_thresh["2_Explore"]
        payload["stage_threshold_lead_pitch"] = saved_thresh["3_Lead_Pitch"]

    target_chars = all_chars if apply_all else [char_id]
    saved_map: Dict[str, Any] = {}
    for cid in target_chars:
        saved_map[cid] = save_settings(cid, payload)

    if (has_delay_update or has_stage_threshold_update) and not apply_all:
        global_sync_payload: Dict[str, Any] = {}
        if has_delay_update:
            global_sync_payload.update(
                {
                    "min_reply_delay_sec": payload["min_reply_delay_sec"],
                    "max_reply_delay_sec": payload["max_reply_delay_sec"],
                    "short_msg_chars_threshold": payload["short_msg_chars_threshold"],
                    "long_msg_chars_threshold": payload["long_msg_chars_threshold"],
                }
            )
        if has_stage_threshold_update:
            global_sync_payload.update(
                {
                    "stage_thresholds": payload["stage_thresholds"],
                    "stage_threshold_icebreak": payload["stage_threshold_icebreak"],
                    "stage_threshold_explore": payload["stage_threshold_explore"],
                    "stage_threshold_lead_pitch": payload["stage_threshold_lead_pitch"],
                }
            )
        for cid in all_chars:
            if cid != char_id:
                save_settings(cid, global_sync_payload)

    return jsonify(
        {
            "ok": True,
            "char_id": char_id,
            "settings": saved_map.get(char_id),
            "stage_thresholds": get_system_stage_thresholds(),
            "updated_characters": list(saved_map.keys()),
        }
    )


@vault_bp.route("/api/vault/media/<char_id>/<path:filename>", methods=["GET"])
def api_vault_media(char_id: str, filename: str):
    """
    ppv_vault 内の動画・画像ファイルを配信する（HTTP Range リクエスト 206 Partial Content 対応）。
    実ファイル未存在時はプレースホルダーSVGを返却する。
    """
    safe_name, full_path, exists = resolve_vault_media_file(char_id, filename)
    if exists and safe_name:
        ext = os.path.splitext(safe_name)[1].lower()
        mime_type = MIME_OVERRIDES.get(ext) or mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
        file_size = os.path.getsize(full_path)
        range_header = request.headers.get("Range", "").strip()

        if range_header and range_header.startswith("bytes="):
            m = re.match(r"bytes=(\d*)-(\d*)", range_header)
            if m:
                start_str, end_str = m.group(1), m.group(2)
                if start_str == "" and end_str != "":
                    suffix_len = min(file_size, int(end_str))
                    start = max(0, file_size - suffix_len)
                    end = file_size - 1
                else:
                    start = int(start_str) if start_str else 0
                    end = int(end_str) if end_str else file_size - 1
                start = max(0, min(start, file_size - 1))
                end = max(start, min(end, file_size - 1))
                chunk_len = end - start + 1

                with open(full_path, "rb") as fp:
                    fp.seek(start)
                    data = fp.read(chunk_len)

                resp = Response(data, status=206, mimetype=mime_type, direct_passthrough=True)
                resp.headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
                resp.headers["Accept-Ranges"] = "bytes"
                resp.headers["Content-Length"] = str(len(data))
                resp.headers["Cache-Control"] = "public, max-age=60"
                return resp

        resp = send_file(full_path, mimetype=mime_type, conditional=True)
        resp.headers["Accept-Ranges"] = "bytes"
        resp.headers["Cache-Control"] = "public, max-age=60"
        return resp

    media_type = detect_media_type(safe_name)
    icon = "🎬" if media_type == "video" else "🖼️"
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 180">
      <defs>
        <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stop-color="#1e1533"/>
          <stop offset="100%" stop-color="#0f0b1a"/>
        </linearGradient>
      </defs>
      <rect width="320" height="180" fill="url(#g)"/>
      <text x="160" y="82" text-anchor="middle" font-size="38">{icon}</text>
      <text x="160" y="118" text-anchor="middle" fill="#c084fc" font-family="sans-serif" font-size="13" font-weight="bold">{safe_name}</text>
      <text x="160" y="140" text-anchor="middle" fill="#64748b" font-family="sans-serif" font-size="11">{char_id} • Vault Preview</text>
    </svg>"""
    return Response(svg, mimetype="image/svg+xml")



