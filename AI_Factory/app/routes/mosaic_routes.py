import os
import cv2
import tempfile
from flask import Blueprint, request, jsonify, Response
from app.services.character_service import get_char_paths
from app.services.mosaic_service import (
    detect_boxes_on_image,
    resolve_raw_video_filename,
    apply_image_genitalia_mosaic,
    render_step2_video_mosaic,
    reset_video_mosaic,
    reset_image_mosaic,
)

mosaic_bp = Blueprint("mosaic", __name__)


# ==================================================
# 半自動AIモザイク API エンドポイント群
# ==================================================
@mosaic_bp.route("/api/mosaic/video_info", methods=["POST"])
def api_mosaic_video_info():
    data = request.json or {}
    paths = get_char_paths(data.get("char_id", "queen_lilith"))
    folder = data.get("folder", "")
    filename = data.get("filename", "")
    cand_dir = os.path.join(paths["candidates"], folder)

    raw_vid = resolve_raw_video_filename(cand_dir, filename)
    vid_path = os.path.join(cand_dir, raw_vid)
    if not os.path.exists(vid_path):
        vid_path = os.path.join(cand_dir, filename)
    if not os.path.exists(vid_path):
        return jsonify({"ok": False, "error": "動画ファイルが見つかりません"})

    cap = cv2.VideoCapture(vid_path)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 16.0
    cap.release()
    return jsonify({
        "ok": True,
        "total_frames": total,
        "fps": fps,
        "raw_video": os.path.basename(vid_path),
    })


@mosaic_bp.route("/api/mosaic/video_frame")
def api_mosaic_video_frame():
    char_id = request.args.get("char_id", "queen_lilith")
    folder = request.args.get("folder", "")
    filename = request.args.get("filename", "")
    frame_idx = int(request.args.get("frame", 0))

    paths = get_char_paths(char_id)
    cand_dir = os.path.join(paths["candidates"], folder)
    raw_vid = resolve_raw_video_filename(cand_dir, filename)
    vid_path = os.path.join(cand_dir, raw_vid)
    if not os.path.exists(vid_path):
        vid_path = os.path.join(cand_dir, filename)
    if not os.path.exists(vid_path):
        return "Not Found", 404

    cap = cv2.VideoCapture(vid_path)
    cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
    ok, frame = cap.read()
    cap.release()
    if not ok or frame is None:
        return "Frame Read Error", 500

    _, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
    return Response(buf.tobytes(), mimetype="image/jpeg")


@mosaic_bp.route("/api/mosaic/detect", methods=["POST"])
def api_mosaic_detect():
    """
    NudeNet 自動検出 API:
      - mode == "image" (Step 1 画像用):
          GENITALIA（陰部＝赤枠）のみを検出して返す。
      - mode == "video" (Step 2 動画用):
          GENITALIA（陰部＝赤枠）と BREAST_EXPOSED（乳首＝黄枠）の両方を
          1回の検出で色分け・クラス分けして返す。
    """
    data = request.json or {}
    paths = get_char_paths(data.get("char_id", "queen_lilith"))
    folder = data.get("folder", "")
    mode = data.get("mode", "image")
    filename = data.get("filename", "")
    cand_dir = os.path.join(paths["candidates"], folder)

    if mode == "image":
        target_path = os.path.join(cand_dir, filename)
        if not os.path.exists(target_path):
            return jsonify({"ok": False, "error": "対象ファイルが見つかりません"})

        boxes, warn = detect_boxes_on_image(target_path, preset="genitalia", mode="image")
        return jsonify({
            "ok": True,
            "mode": "image",
            "boxes": boxes,
            "red_count": len(boxes),
            "yellow_count": 0,
            "warning": warn,
        })
    else:
        raw_vid = resolve_raw_video_filename(cand_dir, filename)
        target_path = os.path.join(cand_dir, raw_vid)
        if not os.path.exists(target_path):
            target_path = os.path.join(cand_dir, filename)
        if not os.path.exists(target_path):
            return jsonify({"ok": False, "error": "対象ファイルが見つかりません"})

        frame_idx = int(data.get("frame", 0))
        cap = cv2.VideoCapture(target_path)
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
        ok, frame = cap.read()
        cap.release()
        if not ok or frame is None:
            return jsonify({"ok": False, "error": "動画フレームの取得に失敗しました"})

        tmp_fd, tmp_jpg = tempfile.mkstemp(suffix=".jpg")
        os.close(tmp_fd)
        try:
            cv2.imwrite(tmp_jpg, frame)
            boxes, warn = detect_boxes_on_image(tmp_jpg, preset="fyp_both", mode="video")
        finally:
            if os.path.exists(tmp_jpg):
                os.remove(tmp_jpg)

        red_count = sum(1 for b in boxes if b.get("category") == "genitalia")
        yellow_count = sum(1 for b in boxes if b.get("category") == "nipple")
        return jsonify({
            "ok": True,
            "mode": "video",
            "boxes": boxes,
            "red_count": red_count,
            "yellow_count": yellow_count,
            "warning": warn,
        })


@mosaic_bp.route("/api/mosaic/apply", methods=["POST"])
def api_mosaic_apply():
    """
    モザイク焼き込み API:
      - mode == "image" (Step 1 画像用):
          陰部モザイク画像 (<slot_id>_MOSAIC.png) を生成し、post_data.json の mosaic_slots を更新。
      - mode == "video" (Step 2 動画用):
          赤枠・黄枠の構成からパターン①〜③を自動判定し、
          *_MOSAIC.mp4 / *_FYP_PREVIEW.mp4 の生成および post_data.json の video_task 更新を一括実行。
    """
    data = request.json or {}
    paths = get_char_paths(data.get("char_id", "queen_lilith"))
    folder = data.get("folder", "")
    mode = data.get("mode", "image")
    cand_dir = os.path.join(paths["candidates"], folder)

    if not folder or not os.path.isdir(cand_dir):
        return jsonify({"ok": False, "error": "対象フォルダが見つかりません"})

    if mode == "image":
        slot_id = data.get("slot_id", "")
        src_file = data.get("filename", "")
        boxes = data.get("boxes", [])
        res, err = apply_image_genitalia_mosaic(cand_dir, slot_id, src_file, boxes)
        if err:
            return jsonify({"ok": False, "error": err})
        return jsonify({"ok": True, **res})
    else:
        src_vid = data.get("filename", "")
        raw_kf = data.get("keyframes") or {}
        default_boxes = data.get("boxes") or []
        res, err = render_step2_video_mosaic(
            cand_dir=cand_dir,
            src_vid=src_vid,
            keyframes_map=raw_kf,
            default_boxes=default_boxes,
        )
        if err:
            return jsonify({"ok": False, "error": err})
        return jsonify({"ok": True, **res})


@mosaic_bp.route("/api/mosaic/reset", methods=["POST"])
def api_mosaic_reset():
    """
    モザイクをリセット（無修正に戻す）API:
      - mode == "video" (デフォルト / Step 2 動画用):
          生成済みの *_MOSAIC.mp4 / *_FYP_PREVIEW.mp4 を削除し、
          post_data.json の video_task をパターン③（無修正状態・mosaic_mode: "none"）に戻す。
      - mode == "image" (Step 1 画像用):
          指定 slot_id の *_MOSAIC.png を削除し、post_data.json の mosaic_slots から除外する。
    """
    data = request.json or {}
    paths = get_char_paths(data.get("char_id", "queen_lilith"))
    folder = data.get("folder", "")
    mode = data.get("mode", "video")
    cand_dir = os.path.join(paths["candidates"], folder)

    if not folder or not os.path.isdir(cand_dir):
        return jsonify({"ok": False, "error": "対象フォルダが見つかりません"})

    if mode == "image":
        res, err = reset_image_mosaic(
            cand_dir=cand_dir,
            slot_id=data.get("slot_id"),
            filename=data.get("filename"),
        )
        if err:
            return jsonify({"ok": False, "error": err})
        return jsonify({"ok": True, **res})
    else:
        res, err = reset_video_mosaic(
            cand_dir=cand_dir,
            requested_filename=data.get("filename"),
        )
        if err:
            return jsonify({"ok": False, "error": err})
        return jsonify({"ok": True, **res})

