import argparse
import base64
import io
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from typing import Any, Dict, List, Optional, Sequence

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
ORIGINALS_SUBDIR_NAME = "originals"

GENITALIA_LABELS = (
    "FEMALE_GENITALIA_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
    "ANUS_EXPOSED",
)
VALID_REDACT_TYPES = ("mosaic", "blur")

_NUDE_DETECTOR_LOCK = threading.Lock()
_NUDE_DETECTOR_INSTANCE: Optional[Any] = None


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


def get_original_media_relpath(filename: str) -> str:
    """ppv_vault ディレクトリからの原本退避用相対パス (例: 'originals/vault_001_clip.mp4') を返す"""
    clean_name = os.path.basename(str(filename or "").replace("\\", "/").strip())
    if not clean_name:
        clean_name = "media"
    return f"{ORIGINALS_SUBDIR_NAME}/{clean_name}"


def get_original_media_path(media_path: str) -> str:
    """メディアファイルパスに対応する退避原本ファイル (originals/<filename>) の絶対パスを返す"""
    abs_media = os.path.abspath(media_path)
    parent_dir = os.path.dirname(abs_media)
    fname = os.path.basename(abs_media)
    if os.path.basename(parent_dir).lower() == ORIGINALS_SUBDIR_NAME.lower():
        return abs_media
    return os.path.join(parent_dir, ORIGINALS_SUBDIR_NAME, fname)


def resolve_existing_original_media_path(media_path: str) -> Optional[str]:
    """既に退避済みの原本ファイルが存在すればその絶対パスを返す（無ければ None）"""
    if not media_path:
        return None
    orig_path = get_original_media_path(media_path)
    if os.path.isfile(orig_path) and os.path.getsize(orig_path) > 0:
        return orig_path
    return None


def ensure_original_backup(media_path: str, overwrite: bool = False) -> Optional[str]:
    """
    元ファイルを削除せず originals/ サブディレクトリへ退避保存して絶対パスを返す。
    既に originals/ に退避済み原本が存在する場合は overwrite=False により上書きせず、
    初回退避した無修正原本を確実に保護する。
    """
    if not media_path:
        return None
    abs_media = os.path.abspath(media_path)
    orig_path = get_original_media_path(abs_media)

    if os.path.normcase(abs_media) == os.path.normcase(orig_path):
        return orig_path if os.path.isfile(orig_path) and os.path.getsize(orig_path) > 0 else None

    if not overwrite and os.path.isfile(orig_path) and os.path.getsize(orig_path) > 0:
        return orig_path

    if not os.path.isfile(abs_media) or os.path.getsize(abs_media) <= 0:
        return orig_path if os.path.isfile(orig_path) and os.path.getsize(orig_path) > 0 else None

    orig_dir = os.path.dirname(orig_path)
    os.makedirs(orig_dir, exist_ok=True)

    ext = os.path.splitext(orig_path)[1] or ".bin"
    fd, tmp_copy = tempfile.mkstemp(suffix=ext, prefix="orig_bak_", dir=orig_dir)
    os.close(fd)
    try:
        shutil.copy2(abs_media, tmp_copy)
        os.replace(tmp_copy, orig_path)
    finally:
        if os.path.exists(tmp_copy):
            try:
                os.remove(tmp_copy)
            except Exception:
                pass

    if os.path.isfile(orig_path) and os.path.getsize(orig_path) > 0:
        return orig_path
    return None


def cleanup_original_backup(media_path: str) -> bool:
    """PPV素材完全削除時に紐づく退避原本ファイルを削除する"""
    if not media_path:
        return False
    orig_path = get_original_media_path(media_path)
    if os.path.isfile(orig_path):
        try:
            os.remove(orig_path)
            return True
        except Exception:
            return False
    return False


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
    backup_original: bool = True,
) -> Dict[str, Any]:
    """
    PPV素材追加時に1回のデコードで:
      0) originals/ サブディレクトリへの無修正原本退避保存 (original_path / original_relpath)
      1) Qwen 2.5 VL 解析用の一時サムネイル静止画ファイル (thumbnail_path)
      2) 全画面強ぼかしプレビュー画像ファイル (blur_preview_path / blur_preview_filename)
    を同時に生成して返す。呼び出し側は Vision 解析完了後に thumbnail_path を削除すること。
    """
    result: Dict[str, Any] = {
        "original_path": None,
        "original_relpath": "",
        "thumbnail_path": None,
        "blur_preview_path": None,
        "blur_preview_filename": "",
        "blur_preview_exists": False,
    }

    if backup_original and media_path and os.path.isfile(media_path):
        try:
            orig_saved = ensure_original_backup(media_path, overwrite=False)
            if orig_saved and os.path.isfile(orig_saved):
                result["original_path"] = orig_saved
                result["original_relpath"] = get_original_media_relpath(os.path.basename(media_path))
        except Exception:
            pass

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


def _get_nude_detector() -> Any:
    """NudeNet 推論器を遅延ロードしてプロセス内で再利用する"""
    global _NUDE_DETECTOR_INSTANCE
    if _NUDE_DETECTOR_INSTANCE is not None:
        return _NUDE_DETECTOR_INSTANCE
    with _NUDE_DETECTOR_LOCK:
        if _NUDE_DETECTOR_INSTANCE is None:
            from nudenet import NudeDetector

            _NUDE_DETECTOR_INSTANCE = NudeDetector()
    return _NUDE_DETECTOR_INSTANCE


def _validate_redact_type(redact_type: str) -> str:
    rtype = str(redact_type or "").strip().lower()
    if rtype not in VALID_REDACT_TYPES:
        raise ValueError(f"Unsupported redact_type: {redact_type!r}. Expected one of {VALID_REDACT_TYPES}.")
    return rtype


def _safe_float(val: Any) -> Optional[float]:
    if val is None or isinstance(val, bool):
        return None
    try:
        fval = float(val)
    except (TypeError, ValueError):
        return None
    if math.isnan(fval) or math.isinf(fval):
        return None
    return fval


def normalize_redact_boxes(boxes: Any) -> List[Dict[str, Any]]:
    """
    手動またはAI検出された座標リストを 0.0〜1.0 の正規化座標 [{"x", "y", "w", "h", ...}] に正規化・検証する。
    不正な値（NaN・非数値・面積ゼロ以下）は例外を投げずに安全に除外する。
    """
    if boxes is None:
        return []
    raw_list: List[Any]
    if isinstance(boxes, dict):
        raw_list = [boxes]
    elif isinstance(boxes, (list, tuple)):
        raw_list = list(boxes)
    else:
        return []

    normalized: List[Dict[str, Any]] = []
    for item in raw_list:
        if isinstance(item, dict):
            rx = _safe_float(item.get("x"))
            ry = _safe_float(item.get("y"))
            rw = _safe_float(item.get("w", item.get("width")))
            rh = _safe_float(item.get("h", item.get("height")))
            label = str(item.get("label") or item.get("class") or "MANUAL").strip() or "MANUAL"
            score = _safe_float(item.get("score"))
        elif isinstance(item, (list, tuple)) and len(item) >= 4:
            rx = _safe_float(item[0])
            ry = _safe_float(item[1])
            rw = _safe_float(item[2])
            rh = _safe_float(item[3])
            label = "MANUAL"
            score = None
        else:
            continue

        if rx is None or ry is None or rw is None or rh is None:
            continue

        # 負の幅・高さが渡された場合は左上座標基準に補正
        if rw < 0:
            rx = rx + rw
            rw = -rw
        if rh < 0:
            ry = ry + rh
            rh = -rh

        x1 = max(0.0, min(1.0, rx))
        y1 = max(0.0, min(1.0, ry))
        x2 = max(0.0, min(1.0, rx + rw))
        y2 = max(0.0, min(1.0, ry + rh))

        cw = x2 - x1
        ch = y2 - y1
        if cw < 0.002 or ch < 0.002:
            continue

        entry: Dict[str, Any] = {
            "x": round(x1, 6),
            "y": round(y1, 6),
            "w": round(cw, 6),
            "h": round(ch, 6),
            "label": label,
        }
        if score is not None:
            entry["score"] = round(max(0.0, min(1.0, score)), 4)
        normalized.append(entry)

    return normalized


def _load_pil_rgb_fullres(image_path: str) -> Optional[Image.Image]:
    """元解像度を維持したまま EXIF 回転補正済みの RGB PIL.Image を読み込む"""
    if not image_path or not os.path.isfile(image_path) or os.path.getsize(image_path) <= 0:
        return None
    try:
        with Image.open(image_path) as src_img:
            img = ImageOps.exif_transpose(src_img)
            if getattr(img, "is_animated", False):
                try:
                    img.seek(0)
                except Exception:
                    pass
            if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                rgba = img.convert("RGBA")
                bg = Image.new("RGBA", rgba.size, (18, 18, 24, 255))
                return Image.alpha_composite(bg, rgba).convert("RGB")
            return img.convert("RGB")
    except Exception:
        return None


def detect_genitalia_boxes(
    media_path: str,
    media_type: Optional[str] = None,
    score_threshold: float = 0.2,
    padding_ratio: float = 0.06,
    target_labels: Optional[Sequence[str]] = None,
    use_original: bool = True,
    detector: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    NudeNet を用いて画像または動画から陰部ラベルのみを自動検出し、
    正規化座標 (x, y, w, h: 0.0〜1.0) のリストを返す。
    陰部が未検出だった場合や読み込み失敗時は例外を投げず {"detected": False, "boxes": []} を返却する。
    """
    empty_res: Dict[str, Any] = {"detected": False, "boxes": []}
    if not media_path:
        return empty_res

    target_src = media_path
    if use_original:
        orig_cand = resolve_existing_original_media_path(media_path)
        if orig_cand:
            target_src = orig_cand

    if not os.path.isfile(target_src) or os.path.getsize(target_src) <= 0:
        return empty_res

    allowed_labels = set(target_labels) if target_labels is not None else set(GENITALIA_LABELS)

    try:
        import cv2
        import numpy as np

        treat_as_video = is_video_media(target_src, media_type=media_type)
        pil_frame = (
            extract_first_frame_pil(target_src, media_type="video", max_side=1920)
            if treat_as_video
            else _load_pil_rgb_fullres(target_src)
        )
        if pil_frame is None:
            pil_frame = extract_first_frame_pil(target_src, media_type=media_type, max_side=1920)
        if pil_frame is None:
            return empty_res

        img_w, img_h = pil_frame.size
        if img_w <= 0 or img_h <= 0:
            return empty_res

        rgb_arr = np.asarray(pil_frame, dtype=np.uint8)
        bgr_arr = cv2.cvtColor(rgb_arr, cv2.COLOR_RGB2BGR)

        det_engine = detector if detector is not None else _get_nude_detector()
        raw_detections = det_engine.detect(bgr_arr) or []

        boxes_out: List[Dict[str, Any]] = []
        for det in raw_detections:
            if not isinstance(det, dict):
                continue
            cls_name = str(det.get("class") or "").strip()
            if cls_name not in allowed_labels:
                continue
            score = _safe_float(det.get("score"))
            if score is None or score < float(score_threshold):
                continue
            raw_box = det.get("box")
            if not isinstance(raw_box, (list, tuple)) or len(raw_box) < 4:
                continue
            bx = _safe_float(raw_box[0])
            by = _safe_float(raw_box[1])
            bw = _safe_float(raw_box[2])
            bh = _safe_float(raw_box[3])
            if bx is None or by is None or bw is None or bh is None or bw <= 0 or bh <= 0:
                continue

            pad_x = bw * max(0.0, float(padding_ratio))
            pad_y = bh * max(0.0, float(padding_ratio))
            px1 = max(0.0, bx - pad_x)
            py1 = max(0.0, by - pad_y)
            px2 = min(float(img_w), bx + bw + pad_x)
            py2 = min(float(img_h), by + bh + pad_y)

            nx = px1 / float(img_w)
            ny = py1 / float(img_h)
            nw = (px2 - px1) / float(img_w)
            nh = (py2 - py1) / float(img_h)

            boxes_out.append(
                {
                    "x": nx,
                    "y": ny,
                    "w": nw,
                    "h": nh,
                    "label": cls_name,
                    "score": score,
                }
            )

        cleaned_boxes = normalize_redact_boxes(boxes_out)
        if not cleaned_boxes:
            return {"detected": False, "boxes": []}
        return {"detected": True, "boxes": cleaned_boxes}
    except Exception:
        return {"detected": False, "boxes": []}


def apply_redaction_to_pil(
    img: Image.Image,
    boxes: Sequence[Dict[str, Any]],
    redact_type: str = "mosaic",
    mosaic_block_size: Optional[int] = None,
    blur_radius: Optional[float] = None,
) -> Image.Image:
    """
    PIL.Image の指定された正規化座標リスト領域に対して mosaic または blur を適用した新しい Image を返す。
    """
    rtype = _validate_redact_type(redact_type)
    valid_boxes = normalize_redact_boxes(boxes)
    out_img = img.copy().convert("RGB")
    if not valid_boxes:
        return out_img

    img_w, img_h = out_img.size
    if img_w <= 1 or img_h <= 1:
        return out_img

    resampling_nearest = getattr(getattr(Image, "Resampling", Image), "NEAREST")
    resampling_bilinear = getattr(getattr(Image, "Resampling", Image), "BILINEAR")

    for b in valid_boxes:
        x1 = max(0, min(img_w - 1, int(round(float(b["x"]) * img_w))))
        y1 = max(0, min(img_h - 1, int(round(float(b["y"]) * img_h))))
        x2 = max(x1 + 1, min(img_w, int(round((float(b["x"]) + float(b["w"])) * img_w))))
        y2 = max(y1 + 1, min(img_h, int(round((float(b["y"]) + float(b["h"])) * img_h))))
        rw = x2 - x1
        rh = y2 - y1
        if rw <= 0 or rh <= 0:
            continue

        region = out_img.crop((x1, y1, x2, y2))
        if rtype == "mosaic":
            block = (
                max(4, int(mosaic_block_size))
                if mosaic_block_size is not None and int(mosaic_block_size) > 0
                else max(8, int(round(min(img_w, img_h) / 32.0)))
            )
            small_w = max(1, rw // block)
            small_h = max(1, rh // block)
            # 領域が小さい場合でも確実にモザイク化されるよう上限を調整
            if small_w >= rw and rw > 2:
                small_w = max(1, rw // 4)
            if small_h >= rh and rh > 2:
                small_h = max(1, rh // 4)
            processed_region = region.resize((small_w, small_h), resample=resampling_bilinear).resize(
                (rw, rh), resample=resampling_nearest
            )
        else:
            rad = (
                float(blur_radius)
                if blur_radius is not None and float(blur_radius) > 0
                else max(12.0, round(min(rw, rh) * 0.18, 1))
            )
            processed_region = region.filter(ImageFilter.GaussianBlur(radius=rad))

        out_img.paste(processed_region, (x1, y1, x2, y2))

    return out_img


def apply_redaction_to_bgr_frame(
    frame: Any,
    boxes: Sequence[Dict[str, Any]],
    redact_type: str = "mosaic",
    mosaic_block_size: Optional[int] = None,
    blur_radius: Optional[float] = None,
) -> Any:
    """
    OpenCV BGR フレーム (numpy.ndarray) の指定正規化座標リスト領域に対して mosaic または blur を適用する。
    """
    import cv2

    rtype = _validate_redact_type(redact_type)
    valid_boxes = normalize_redact_boxes(boxes)
    if frame is None or getattr(frame, "size", 0) == 0:
        return frame
    out_frame = frame.copy()
    if not valid_boxes:
        return out_frame

    img_h, img_w = out_frame.shape[:2]
    if img_w <= 1 or img_h <= 1:
        return out_frame

    for b in valid_boxes:
        x1 = max(0, min(img_w - 1, int(round(float(b["x"]) * img_w))))
        y1 = max(0, min(img_h - 1, int(round(float(b["y"]) * img_h))))
        x2 = max(x1 + 1, min(img_w, int(round((float(b["x"]) + float(b["w"])) * img_w))))
        y2 = max(y1 + 1, min(img_h, int(round((float(b["y"]) + float(b["h"])) * img_h))))
        rw = x2 - x1
        rh = y2 - y1
        if rw <= 0 or rh <= 0:
            continue

        roi = out_frame[y1:y2, x1:x2]
        if rtype == "mosaic":
            block = (
                max(4, int(mosaic_block_size))
                if mosaic_block_size is not None and int(mosaic_block_size) > 0
                else max(8, int(round(min(img_w, img_h) / 32.0)))
            )
            small_w = max(1, rw // block)
            small_h = max(1, rh // block)
            if small_w >= rw and rw > 2:
                small_w = max(1, rw // 4)
            if small_h >= rh and rh > 2:
                small_h = max(1, rh // 4)
            small_roi = cv2.resize(roi, (small_w, small_h), interpolation=cv2.INTER_LINEAR)
            proc_roi = cv2.resize(small_roi, (rw, rh), interpolation=cv2.INTER_NEAREST)
        else:
            sigma = (
                float(blur_radius)
                if blur_radius is not None and float(blur_radius) > 0
                else max(12.0, min(rw, rh) * 0.18)
            )
            ksize = max(3, int(round(sigma * 3.0)))
            if ksize % 2 == 0:
                ksize += 1
            proc_roi = cv2.GaussianBlur(roi, (ksize, ksize), sigmaX=sigma, sigmaY=sigma)

        out_frame[y1:y2, x1:x2] = proc_roi

    return out_frame


def _save_pil_via_tempfile(img: Image.Image, dest_path: str) -> str:
    """PIL Image を一時ファイル経由でアトミックに保存する"""
    abs_dest = os.path.abspath(dest_path)
    dest_dir = os.path.dirname(abs_dest)
    os.makedirs(dest_dir, exist_ok=True)

    ext = os.path.splitext(abs_dest)[1].lower()
    fmt_map = {
        ".jpg": ("JPEG", {"quality": 92, "optimize": True}),
        ".jpeg": ("JPEG", {"quality": 92, "optimize": True}),
        ".png": ("PNG", {"optimize": True}),
        ".webp": ("WEBP", {"quality": 92}),
        ".bmp": ("BMP", {}),
        ".gif": ("GIF", {}),
    }
    pil_fmt, save_kwargs = fmt_map.get(ext, ("JPEG", {"quality": 92, "optimize": True}))
    suffix = ext if ext in fmt_map else ".jpg"

    fd, tmp_path = tempfile.mkstemp(suffix=suffix, prefix="redact_img_", dir=dest_dir)
    os.close(fd)
    try:
        save_img = img.convert("RGB") if pil_fmt == "JPEG" else img
        save_img.save(tmp_path, format=pil_fmt, **save_kwargs)
        if not os.path.isfile(tmp_path) or os.path.getsize(tmp_path) <= 0:
            raise RuntimeError("Failed to write redacted image temporary file.")
        os.replace(tmp_path, abs_dest)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
    return abs_dest


def _redact_video_via_tempfile(
    src_video_path: str,
    dest_video_path: str,
    boxes: Sequence[Dict[str, Any]],
    redact_type: str = "mosaic",
    mosaic_block_size: Optional[int] = None,
    blur_radius: Optional[float] = None,
) -> str:
    """
    動画の各フレームに mosaic または blur を適用し、一時ファイル経由で安全に書き出す。
    FFmpeg が利用可能な場合は元動画の音声トラックを引き継ぎ H.264 互換フォーマットへ再多重化する。
    """
    import cv2

    rtype = _validate_redact_type(redact_type)
    valid_boxes = normalize_redact_boxes(boxes)
    abs_src = os.path.abspath(src_video_path)
    abs_dest = os.path.abspath(dest_video_path)
    dest_dir = os.path.dirname(abs_dest)
    os.makedirs(dest_dir, exist_ok=True)

    ext = os.path.splitext(abs_dest)[1].lower() or ".mp4"
    tmp_raw_video: Optional[str] = None
    tmp_muxed_video: Optional[str] = None

    try:
        # 加工枠が0件の場合は原本から一時ファイル経由でクリーンコピー
        if not valid_boxes:
            if os.path.normcase(abs_src) != os.path.normcase(abs_dest):
                fd_cp, tmp_cp = tempfile.mkstemp(suffix=ext, prefix="redact_vid_cp_", dir=dest_dir)
                os.close(fd_cp)
                try:
                    shutil.copy2(abs_src, tmp_cp)
                    os.replace(tmp_cp, abs_dest)
                finally:
                    if os.path.exists(tmp_cp):
                        try:
                            os.remove(tmp_cp)
                        except Exception:
                            pass
            return abs_dest

        cap = cv2.VideoCapture(abs_src)
        if not cap.isOpened():
            raise RuntimeError(f"Could not open video for redaction: {abs_src}")

        try:
            fps = float(cap.get(cv2.CAP_PROP_FPS) or 30.0)
            if fps <= 0 or math.isnan(fps) or math.isinf(fps):
                fps = 30.0
            width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)

            ok_first, first_frame = cap.read()
            if not ok_first or first_frame is None or first_frame.size == 0:
                raise RuntimeError(f"Video contains no readable frames: {abs_src}")
            if width <= 0 or height <= 0:
                height, width = first_frame.shape[:2]

            fd_raw, tmp_raw_video = tempfile.mkstemp(suffix=".mp4", prefix="redact_vid_raw_", dir=dest_dir)
            os.close(fd_raw)

            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            writer = cv2.VideoWriter(tmp_raw_video, fourcc, fps, (width, height))
            if not writer.isOpened():
                raise RuntimeError("Could not initialize temporary VideoWriter.")

            frames_written = 0
            try:
                cur_frame = first_frame
                while cur_frame is not None and cur_frame.size > 0:
                    if cur_frame.shape[1] != width or cur_frame.shape[0] != height:
                        cur_frame = cv2.resize(cur_frame, (width, height), interpolation=cv2.INTER_LINEAR)
                    redacted_frame = apply_redaction_to_bgr_frame(
                        cur_frame,
                        valid_boxes,
                        redact_type=rtype,
                        mosaic_block_size=mosaic_block_size,
                        blur_radius=blur_radius,
                    )
                    writer.write(redacted_frame)
                    frames_written += 1
                    ok_next, next_frame = cap.read()
                    cur_frame = next_frame if ok_next else None
            finally:
                writer.release()
        finally:
            cap.release()

        if frames_written <= 0 or not tmp_raw_video or not os.path.isfile(tmp_raw_video) or os.path.getsize(tmp_raw_video) <= 0:
            raise RuntimeError("Failed to write redacted video frames to temporary file.")

        final_tmp_to_commit = tmp_raw_video

        # FFmpeg が利用可能なら H.264 エンコード＋元音声トラック引き継ぎを一時ファイルで実施
        if shutil.which("ffmpeg"):
            fd_mux, tmp_muxed_video = tempfile.mkstemp(suffix=ext, prefix="redact_vid_mux_", dir=dest_dir)
            os.close(fd_mux)
            cmd = [
                "ffmpeg",
                "-y",
                "-i",
                tmp_raw_video,
                "-i",
                abs_src,
                "-map",
                "0:v:0",
                "-map",
                "1:a?",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "21",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-movflags",
                "+faststart",
                tmp_muxed_video,
            ]
            try:
                proc = subprocess.run(cmd, capture_output=True, timeout=180.0)
                if proc.returncode == 0 and os.path.isfile(tmp_muxed_video) and os.path.getsize(tmp_muxed_video) > 0:
                    final_tmp_to_commit = tmp_muxed_video
            except Exception:
                pass

        os.replace(final_tmp_to_commit, abs_dest)
        return abs_dest
    finally:
        for tmp_cand in (tmp_raw_video, tmp_muxed_video):
            if tmp_cand and os.path.exists(tmp_cand):
                try:
                    os.remove(tmp_cand)
                except Exception:
                    pass


def redact_media_file(
    media_path: str,
    redact_type: str = "mosaic",
    boxes: Optional[Sequence[Dict[str, Any]]] = None,
    auto_detect: bool = False,
    output_path: Optional[str] = None,
    media_type: Optional[str] = None,
    preview_detect_only: bool = False,
    regenerate_blur_preview: bool = True,
    mosaic_block_size: Optional[int] = None,
    blur_radius: Optional[float] = None,
    detector: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    PPVメディア（画像・動画）に対して局部モザイク(mosaic)／ぼかし(blur)加工を実行する統合エンジン。
    - preview_detect_only=True の場合:
        ファイルを変更せず、NudeNet 陰部自動検出を実行して {"detected": bool, "boxes": [...]} を返す。
    - 確定加工実行時:
        1) 元ファイルを削除せず originals/<filename> へ退避保存（既存の退避原本があればそれを保護・再利用）
        2) 無修正原本を入力ソースとして AI自動検出 および/または 指定座標配列 (boxes) に mosaic / blur を適用
        3) 一時ファイル経由で output_path（省略時は media_path）へ安全に書き出し、原本と加工後を二重保持
        4) 必要に応じて全画面ぼかしプレビュー画像 (previews/<stem>_blur.jpg) も再生成
    """
    if not media_path:
        raise FileNotFoundError("media_path is required.")

    abs_media = os.path.abspath(media_path)
    existing_orig = resolve_existing_original_media_path(abs_media)
    if not os.path.isfile(abs_media) and not existing_orig:
        raise FileNotFoundError(f"Media file not found: {abs_media}")

    if preview_detect_only:
        det_only = detect_genitalia_boxes(
            abs_media,
            media_type=media_type,
            use_original=True,
            detector=detector,
        )
        return {
            "ok": True,
            "detected": bool(det_only.get("detected", False)),
            "boxes": list(det_only.get("boxes") or []),
        }

    rtype = _validate_redact_type(redact_type)

    # 1. 原本退避の保証（初回は abs_media -> originals/ へコピー、2回目以降は既存の originals/ を維持）
    original_path = ensure_original_backup(abs_media, overwrite=False)
    if not original_path or not os.path.isfile(original_path):
        raise RuntimeError(f"Failed to secure original backup for: {abs_media}")

    target_out = os.path.abspath(output_path) if output_path else abs_media
    if os.path.normcase(target_out) == os.path.normcase(original_path):
        raise ValueError("output_path must be distinct from the originals/ backup path.")

    # 2. 適用する正規化座標リストの確定（手動指定 + AI自動検出）
    manual_boxes = normalize_redact_boxes(boxes)
    detected_boxes: List[Dict[str, Any]] = []
    if auto_detect:
        det_res = detect_genitalia_boxes(
            original_path,
            media_type=media_type,
            use_original=False,
            detector=detector,
        )
        detected_boxes = list(det_res.get("boxes") or [])

    final_boxes = normalize_redact_boxes(manual_boxes + detected_boxes)
    treat_as_video = is_video_media(original_path, media_type=media_type)

    # 3. 画像または動画の加工を一時ファイル経由で安全に書き出し
    if treat_as_video:
        _redact_video_via_tempfile(
            src_video_path=original_path,
            dest_video_path=target_out,
            boxes=final_boxes,
            redact_type=rtype,
            mosaic_block_size=mosaic_block_size,
            blur_radius=blur_radius,
        )
    else:
        src_pil = _load_pil_rgb_fullres(original_path)
        if src_pil is None:
            raise RuntimeError(f"Failed to decode source image: {original_path}")
        redacted_pil = apply_redaction_to_pil(
            src_pil,
            final_boxes,
            redact_type=rtype,
            mosaic_block_size=mosaic_block_size,
            blur_radius=blur_radius,
        )
        _save_pil_via_tempfile(redacted_pil, target_out)

    # 4. 全画面ぼかしプレビューの再生成
    blur_preview_path: Optional[str] = None
    blur_preview_rel = ""
    if regenerate_blur_preview:
        try:
            blur_preview_path = generate_blurred_preview(
                media_path=target_out,
                media_type="video" if treat_as_video else "image",
            )
            if blur_preview_path and os.path.isfile(blur_preview_path):
                blur_preview_rel = get_blur_preview_relpath(os.path.basename(target_out))
        except Exception:
            blur_preview_path = None

    return {
        "ok": True,
        "detected": len(final_boxes) > 0,
        "boxes": final_boxes,
        "redact_type": rtype,
        "media_type": "video" if treat_as_video else "image",
        "original_path": original_path,
        "original_relpath": get_original_media_relpath(os.path.basename(abs_media)),
        "output_path": target_out,
        "processed_filename": os.path.basename(target_out),
        "blur_preview_path": blur_preview_path,
        "blur_preview_filename": blur_preview_rel,
    }


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
