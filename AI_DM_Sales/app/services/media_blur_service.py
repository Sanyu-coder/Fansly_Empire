import argparse
import base64
import io
import json
import os
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Optional

from PIL import Image, ImageFilter, ImageOps

VIDEO_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".webm",
    ".avi",
    ".mkv",
    ".m4v",
    ".mpeg",
    ".mpg",
    ".ts",
    ".flv",
    ".wmv",
}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}

BLUR_SUBDIR_NAME = "previews"
BLUR_FILENAME_SUFFIX = "_blur.jpg"


def is_video_media(file_path_or_name: str, media_type: Optional[str] = None) -> bool:
    mtype = str(media_type or "").strip().lower()
    if mtype == "image":
        ext = os.path.splitext(str(file_path_or_name or ""))[1].lower()
        return ext in VIDEO_EXTENSIONS
    if mtype == "video":
        ext = os.path.splitext(str(file_path_or_name or ""))[1].lower()
        if ext in IMAGE_EXTENSIONS:
            return False
        return True
    ext = os.path.splitext(str(file_path_or_name or ""))[1].lower()
    return ext in VIDEO_EXTENSIONS


def get_blur_preview_relpath(filename: str) -> str:
    """ppv_vault ディレクトリからの相対パス (例: 'previews/vault_001_clip_blur.jpg') を返す"""
    clean_name = os.path.basename(str(filename or "").replace("\\", "/").strip())
    stem = os.path.splitext(clean_name)[0] or "media"
    if stem.endswith("_blur"):
        return f"{BLUR_SUBDIR_NAME}/{stem}.jpg"
    return f"{BLUR_SUBDIR_NAME}/{stem}{BLUR_FILENAME_SUFFIX}"


def get_blur_preview_path(media_path: str) -> str:
    """元メディアファイルパスに対応するぼかしプレビュー画像の絶対保存先パスを返す"""
    abs_media = os.path.abspath(media_path)
    parent_dir = os.path.dirname(abs_media)
    rel = get_blur_preview_relpath(os.path.basename(abs_media))
    return os.path.join(parent_dir, *rel.split("/"))


def resolve_existing_blur_preview_path(media_path: str) -> Optional[str]:
    """既に生成済みのぼかしプレビュー画像が存在すればその絶対パスを返す（無ければ None）"""
    if not media_path:
        return None
    primary = get_blur_preview_path(media_path)
    if os.path.isfile(primary) and os.path.getsize(primary) > 0:
        return primary

    abs_media = os.path.abspath(media_path)
    parent_dir = os.path.dirname(abs_media)
    stem = os.path.splitext(os.path.basename(abs_media))[0]
    fallback = os.path.join(parent_dir, f"{stem}{BLUR_FILENAME_SUFFIX}")
    if os.path.isfile(fallback) and os.path.getsize(fallback) > 0:
        return fallback
    return None


def _normalize_pil_rgb(img: Image.Image, max_side: int = 1280) -> Image.Image:
    """EXIF回転補正・透過背景合成・RGB変換・長辺リサイズを一括実行する"""
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass

    if getattr(img, "is_animated", False):
        try:
            img.seek(0)
        except Exception:
            pass

    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, (15, 11, 26, 255))
        rgb = Image.alpha_composite(bg, rgba).convert("RGB")
    else:
        rgb = img.convert("RGB")

    w, h = rgb.size
    if max_side > 0 and max(w, h) > max_side:
        scale = max_side / float(max(w, h))
        new_size = (max(1, int(round(w * scale))), max(1, int(round(h * scale))))
        rgb = rgb.resize(new_size, Image.Resampling.LANCZOS)
    return rgb


def extract_first_frame_pil(
    media_path: str,
    media_type: Optional[str] = None,
    max_side: int = 1280,
) -> Optional[Image.Image]:
    """
    画像または動画から1枚目（先頭フレーム）を PIL.Image (RGB) として抽出する。
    - 動画の場合は 0 フレーム目（1枚目）を最優先で取得し、万一先頭が完全黒画面（フェードイン等）の場合のみ
      直後の非黒フレームへ自動フォールバックする。
    - OpenCV で開けない環境・コーデック・Unicodeパスでは FFmpeg へ自動フォールバックする。
    """
    if not media_path or not os.path.isfile(media_path) or os.path.getsize(media_path) <= 0:
        return None

    treat_as_video = is_video_media(media_path, media_type=media_type)

    if not treat_as_video:
        try:
            with Image.open(media_path) as src_img:
                return _normalize_pil_rgb(src_img, max_side=max_side)
        except Exception:
            treat_as_video = True

    # 1. OpenCV による動画 1フレーム目抽出
    try:
        import cv2

        cap = cv2.VideoCapture(media_path)
        if cap.isOpened():
            try:
                total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
                fps = float(cap.get(cv2.CAP_PROP_FPS) or 30.0)
                if fps <= 0:
                    fps = 30.0

                candidate_positions: List[int] = [0]
                if total_frames > 3:
                    for extra_pos in (
                        min(total_frames - 1, max(1, int(round(fps * 0.25)))),
                        int(total_frames * 0.10),
                        int(total_frames * 0.25),
                    ):
                        if extra_pos not in candidate_positions:
                            candidate_positions.append(extra_pos)

                chosen_frame = None
                for pos in candidate_positions:
                    if pos > 0:
                        cap.set(cv2.CAP_PROP_POS_FRAMES, pos)
                    ok, frame = cap.read()
                    if ok and frame is not None and frame.size > 0:
                        if chosen_frame is None:
                            chosen_frame = frame
                        # 先頭フレームが真っ黒な暗転フレームでなければ即採用（1枚目優先）
                        if float(frame.mean()) >= 8.0:
                            chosen_frame = frame
                            break

                if chosen_frame is not None:
                    rgb_arr = cv2.cvtColor(chosen_frame, cv2.COLOR_BGR2RGB)
                    pil_frame = Image.fromarray(rgb_arr)
                    return _normalize_pil_rgb(pil_frame, max_side=max_side)
            finally:
                cap.release()
    except Exception:
        pass

    # 2. FFmpeg フォールバック（先頭 0秒目 -> 失敗または暗転時は 0.2秒目）
    for seek_ts in ("00:00:00", "00:00:00.200"):
        tmp_jpg = None
        try:
            fd, tmp_jpg = tempfile.mkstemp(suffix=".jpg", prefix="ppv_1st_frame_")
            os.close(fd)
            cmd = [
                "ffmpeg",
                "-y",
                "-ss",
                seek_ts,
                "-i",
                media_path,
                "-frames:v",
                "1",
                "-q:v",
                "2",
                tmp_jpg,
            ]
            proc = subprocess.run(cmd, capture_output=True, timeout=20.0)
            if proc.returncode == 0 and os.path.isfile(tmp_jpg) and os.path.getsize(tmp_jpg) > 0:
                with Image.open(tmp_jpg) as f_img:
                    return _normalize_pil_rgb(f_img, max_side=max_side)
        except Exception:
            pass
        finally:
            if tmp_jpg and os.path.exists(tmp_jpg):
                try:
                    os.remove(tmp_jpg)
                except Exception:
                    pass

    return None


def extract_first_frame_image(
    media_path: str,
    output_path: Optional[str] = None,
    media_type: Optional[str] = None,
    max_side: int = 1024,
    jpeg_quality: int = 90,
) -> Optional[str]:
    """
    動画（1フレーム目）または画像を静止画 JPEG ファイルとして保存し、そのファイルパスを返す。
    Qwen 2.5 VL へ渡すサムネイル画像の生成に使用する。
    """
    pil_img = extract_first_frame_pil(media_path, media_type=media_type, max_side=max_side)
    if pil_img is None:
        return None

    target_out = output_path
    created_temp = False
    if not target_out:
        fd, target_out = tempfile.mkstemp(suffix=".jpg", prefix="ppv_vision_thumb_")
        os.close(fd)
        created_temp = True
    else:
        os.makedirs(os.path.dirname(os.path.abspath(target_out)), exist_ok=True)

    try:
        pil_img.save(target_out, format="JPEG", quality=jpeg_quality, optimize=True)
        if os.path.isfile(target_out) and os.path.getsize(target_out) > 0:
            return target_out
    except Exception:
        if created_temp and target_out and os.path.exists(target_out):
            try:
                os.remove(target_out)
            except Exception:
                pass
    return None


def image_file_to_base64(
    image_path: str,
    max_side: int = 1024,
    jpeg_quality: int = 88,
) -> Optional[str]:
    """静止画ファイルを読み込み、長辺 max_side 以下の JPEG Base64 文字列を返す"""
    if not image_path or not os.path.isfile(image_path) or os.path.getsize(image_path) <= 0:
        return None
    try:
        with Image.open(image_path) as img:
            rgb = _normalize_pil_rgb(img, max_side=max_side)
            buf = io.BytesIO()
            rgb.save(buf, format="JPEG", quality=jpeg_quality)
            return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return None


def extract_media_thumbnail_base64(
    media_path: str,
    media_type: Optional[str] = None,
    max_side: int = 1024,
    jpeg_quality: int = 88,
) -> Optional[str]:
    """
    画像または動画（1枚目サムネイル）から Vision モデル入力用の JPEG Base64 文字列を生成する。
    生動画バイナリをそのまま Base64 化することは決して行わない。
    """
    pil_img = extract_first_frame_pil(media_path, media_type=media_type, max_side=max_side)
    if pil_img is None:
        return None
    try:
        buf = io.BytesIO()
        pil_img.save(buf, format="JPEG", quality=jpeg_quality)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return None


def generate_blurred_preview(
    media_path: str,
    output_path: Optional[str] = None,
    media_type: Optional[str] = None,
    blur_radius: Optional[float] = None,
    max_side: int = 1280,
    jpeg_quality: int = 86,
    source_pil: Optional[Image.Image] = None,
) -> Optional[str]:
    """
    画像または動画（1枚目サムネイル）全体に強いガウシアンぼかしを掛けたプレビュー用画像を生成・保存する。
    シルエットや色味の魅力を残しつつ、細部・露出部が判別できない十分なぼかし強度を画像サイズに比例して適用する。
    """
    base_img = (
        _normalize_pil_rgb(source_pil, max_side=max_side)
        if source_pil is not None
        else extract_first_frame_pil(media_path, media_type=media_type, max_side=max_side)
    )
    if base_img is None:
        return None

    w, h = base_img.size
    effective_radius = (
        float(blur_radius)
        if blur_radius is not None and float(blur_radius) > 0
        else max(18.0, round(min(w, h) * 0.075, 1))
    )

    blurred_img = base_img.filter(ImageFilter.GaussianBlur(radius=effective_radius))

    dest_path = os.path.abspath(output_path) if output_path else get_blur_preview_path(media_path)
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    blurred_img.save(dest_path, format="JPEG", quality=jpeg_quality, optimize=True)

    if os.path.isfile(dest_path) and os.path.getsize(dest_path) > 0:
        return dest_path
    return None


def prepare_vault_media_assets(
    media_path: str,
    media_type: Optional[str] = None,
    blur_output_path: Optional[str] = None,
    vision_max_side: int = 1024,
    blur_max_side: int = 1280,
) -> Dict[str, Any]:
    """
    PPV素材追加時に1回のデコードで:
      1) Qwen 2.5 VL 解析用の一時サムネイル静止画ファイル (thumbnail_path)
      2) 全画面強ぼかしプレビュー画像ファイル (blur_preview_path / blur_preview_filename)
    を同時に生成して返す。呼び出し側は Vision 解析完了後に thumbnail_path を削除すること。
    """
    result: Dict[str, Any] = {
        "thumbnail_path": None,
        "blur_preview_path": None,
        "blur_preview_filename": "",
        "blur_preview_exists": False,
    }
    base_pil = extract_first_frame_pil(
        media_path,
        media_type=media_type,
        max_side=max(vision_max_side, blur_max_side),
    )
    if base_pil is None:
        return result

    # 1. Vision 推論用 1枚目静止画 JPEG（一時ファイル）
    try:
        vision_pil = _normalize_pil_rgb(base_pil, max_side=vision_max_side)
        fd, thumb_tmp = tempfile.mkstemp(suffix=".jpg", prefix="ppv_vision_1st_")
        os.close(fd)
        vision_pil.save(thumb_tmp, format="JPEG", quality=90, optimize=True)
        if os.path.isfile(thumb_tmp) and os.path.getsize(thumb_tmp) > 0:
            result["thumbnail_path"] = thumb_tmp
    except Exception:
        pass

    # 2. 全画面ぼかしプレビュー JPEG（ppv_vault/previews/<stem>_blur.jpg）
    try:
        blur_path = generate_blurred_preview(
            media_path=media_path,
            output_path=blur_output_path,
            media_type=media_type,
            max_side=blur_max_side,
            source_pil=base_pil,
        )
        if blur_path and os.path.isfile(blur_path):
            result["blur_preview_path"] = blur_path
            result["blur_preview_filename"] = get_blur_preview_relpath(os.path.basename(media_path))
            result["blur_preview_exists"] = True
    except Exception:
        pass

    return result


def cleanup_blur_preview(media_path: str) -> bool:
    """PPV素材削除時に紐づくぼかしプレビュー画像を削除する"""
    if not media_path:
        return False
    removed = False
    for cand in (
        get_blur_preview_path(media_path),
        os.path.join(
            os.path.dirname(os.path.abspath(media_path)),
            f"{os.path.splitext(os.path.basename(media_path))[0]}{BLUR_FILENAME_SUFFIX}",
        ),
    ):
        if os.path.isfile(cand):
            try:
                os.remove(cand)
                removed = True
            except Exception:
                pass
    return removed


def generate_blur_previews_for_character(char_id: str, force: bool = False) -> Dict[str, Any]:
    """指定キャラクターの既存 PPV 素材すべてに対して全画面ぼかしプレビュー画像を一括生成する"""
    from app.services.character_loader import get_char_paths, load_vault_items, save_vault_items

    paths = get_char_paths(char_id)
    vault_dir = os.path.abspath(paths["vault_dir"])
    items = load_vault_items(char_id)

    generated_count = 0
    skipped_count = 0
    missing_source_count = 0
    failed_count = 0
    updated_index = False
    details: List[Dict[str, Any]] = []

    for item in items:
        fname = os.path.basename(str(item.get("filename") or "").replace("\\", "/").strip())
        if not fname:
            missing_source_count += 1
            continue

        src_path = os.path.join(vault_dir, fname)
        if not os.path.isfile(src_path) or os.path.getsize(src_path) <= 0:
            missing_source_count += 1
            details.append({"ppv_id": item.get("ppv_id"), "filename": fname, "status": "missing_source"})
            continue

        expected_rel = get_blur_preview_relpath(fname)
        existing_blur = resolve_existing_blur_preview_path(src_path)

        if existing_blur and not force:
            if item.get("blur_preview_filename") != expected_rel:
                item["blur_preview_filename"] = expected_rel
                updated_index = True
            skipped_count += 1
            details.append(
                {
                    "ppv_id": item.get("ppv_id"),
                    "filename": fname,
                    "blur_preview_filename": expected_rel,
                    "blur_preview_path": existing_blur,
                    "status": "skipped_exists",
                }
            )
            continue

        blur_out = generate_blurred_preview(
            src_path,
            media_type=str(item.get("media_type") or ""),
        )
        if blur_out and os.path.isfile(blur_out):
            item["blur_preview_filename"] = expected_rel
            updated_index = True
            generated_count += 1
            details.append(
                {
                    "ppv_id": item.get("ppv_id"),
                    "filename": fname,
                    "blur_preview_filename": expected_rel,
                    "blur_preview_path": blur_out,
                    "status": "generated",
                }
            )
        else:
            failed_count += 1
            details.append({"ppv_id": item.get("ppv_id"), "filename": fname, "status": "failed"})

    if updated_index and items:
        save_vault_items(char_id, items)

    return {
        "ok": True,
        "char_id": char_id,
        "total_items": len(items),
        "generated_count": generated_count,
        "skipped_count": skipped_count,
        "missing_source_count": missing_source_count,
        "failed_count": failed_count,
        "details": details,
    }


def generate_blur_previews_for_all_characters(force: bool = False) -> Dict[str, Any]:
    """全キャラクターの既存 PPV 素材に対して全画面ぼかしプレビュー画像を一括生成する"""
    from app.services.character_loader import list_character_ids

    char_ids = list_character_ids()
    by_char: Dict[str, Any] = {}
    total_generated = 0
    total_skipped = 0
    total_missing = 0
    total_failed = 0

    for cid in char_ids:
        res = generate_blur_previews_for_character(cid, force=force)
        by_char[cid] = res
        total_generated += int(res["generated_count"])
        total_skipped += int(res["skipped_count"])
        total_missing += int(res["missing_source_count"])
        total_failed += int(res["failed_count"])

    return {
        "ok": True,
        "characters": char_ids,
        "total_generated": total_generated,
        "total_skipped": total_skipped,
        "total_missing_source": total_missing,
        "total_failed": total_failed,
        "results_by_char": by_char,
    }


def _cli_main() -> int:
    parser = argparse.ArgumentParser(
        description="既存PPV素材（画像・動画1枚目サムネ）の全画面ぼかしプレビュー画像を一括生成します。"
    )
    parser.add_argument(
        "--char-id",
        default="all",
        help="対象キャラクターID（省略時または 'all' で全キャラクター対象）",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="既にぼかしプレビューが存在する素材も上書き再生成する",
    )
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    target = str(args.char_id or "all").strip()
    if not target or target.lower() == "all":
        summary = generate_blur_previews_for_all_characters(force=bool(args.force))
    else:
        summary = generate_blur_previews_for_character(target, force=bool(args.force))

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli_main())
