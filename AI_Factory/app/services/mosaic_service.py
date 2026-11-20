import os
import cv2
import json
import shutil
import subprocess

# NudeNet 遅延ロード（初回検出時のみ初期化）
_nude_detector = None
_nude_detector_err = None

# NudeNet 検出対象クラス定義
GENITALIA_CLASSES = {
    "FEMALE_GENITALIA_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
    "ANUS_EXPOSED",
}
NIPPLE_CLASSES = {
    "FEMALE_BREAST_EXPOSED",
}

COLOR_RED = "#ef4444"     # 🔴 赤枠: 陰部（本編＋FYP 両方に適用）
COLOR_YELLOW = "#eab308"  # 🟡 黄枠: 乳首（FYP 無料プレビューのみに適用）


def get_nude_detector():
    global _nude_detector, _nude_detector_err
    if _nude_detector is not None:
        return _nude_detector, None
    if _nude_detector_err is not None:
        return None, _nude_detector_err
    try:
        from nudenet import NudeDetector
        _nude_detector = NudeDetector()
        return _nude_detector, None
    except Exception as e:
        _nude_detector_err = f"NudeNet未導入または初期化エラー ({e})。手動ドラッグ矩形は利用可能です。"
        return None, _nude_detector_err


def classify_box_layer(box):
    """
    ボックス辞書からレイヤー種別 ('genitalia' | 'nipple') を判定する。
    - 'nipple'    : 🟡 黄枠（乳首 / BREAST_EXPOSED / FYPのみモザイク）
    - 'genitalia' : 🔴 赤枠（陰部 / GENITALIA / 本編＋FYP両方モザイク）
    """
    if not isinstance(box, dict):
        return "genitalia"

    for key in ("category", "layer", "type", "class_group", "raw_class", "class"):
        val = str(box.get(key) or "").strip().lower()
        if val in ("nipple", "yellow", "breast", "breast_exposed", "female_breast_exposed"):
            return "nipple"
        if val in (
            "genitalia",
            "red",
            "female_genitalia_exposed",
            "male_genitalia_exposed",
            "anus_exposed",
        ):
            return "genitalia"

    color = str(box.get("color") or "").strip().lower()
    if color in (COLOR_YELLOW, "yellow", "#facc15", "#f59e0b"):
        return "nipple"
    if color in (COLOR_RED, "red", "#f87171", "#dc2626"):
        return "genitalia"

    label = str(box.get("label") or "").strip()
    if "乳首" in label or "BREAST" in label.upper() or "NIPPLE" in label.upper():
        return "nipple"

    return "genitalia"


def normalize_box_metadata(box, force_layer=None):
    """ボックス辞書に category / class_group / color / label を正規化して付与する"""
    layer = force_layer or classify_box_layer(box)
    is_nipple = (layer == "nipple")
    return {
        "x": float(box.get("x", 0)),
        "y": float(box.get("y", 0)),
        "w": float(box.get("w", 0)),
        "h": float(box.get("h", 0)),
        "category": "nipple" if is_nipple else "genitalia",
        "layer": "yellow" if is_nipple else "red",
        "class_group": "BREAST_EXPOSED" if is_nipple else "GENITALIA",
        "raw_class": box.get(
            "raw_class",
            "FEMALE_BREAST_EXPOSED" if is_nipple else "FEMALE_GENITALIA_EXPOSED",
        ),
        "color": COLOR_YELLOW if is_nipple else COLOR_RED,
        "label": "乳首" if is_nipple else "陰部",
        "score": round(float(box.get("score", 1.0)), 2),
    }


def detect_boxes_on_image(img_path, preset="genitalia", mode=None):
    """
    NudeNet を用いて画像上の露出部位を検出する。
    - Step 1 画像用 (mode='image' または preset='genitalia'):
        GENITALIA（陰部）のみを検出し、赤枠属性で返す。
    - Step 2 動画用 (mode='video' または preset='fyp_both'):
        1回の推論で GENITALIA（陰部＝赤枠）と BREAST_EXPOSED（乳首＝黄枠）の両方を
        色分け・クラス分けして返す。
    """
    detector, err = get_nude_detector()
    if detector is None:
        return [], err

    if mode == "image":
        include_nipple = False
    elif mode == "video":
        include_nipple = True
    else:
        include_nipple = (preset == "fyp_both")

    target_classes = set(GENITALIA_CLASSES)
    if include_nipple:
        target_classes |= NIPPLE_CLASSES

    raw_preds = detector.detect(img_path)
    boxes = []
    for p in raw_preds:
        cls_name = p.get("class", "")
        score = float(p.get("score", 0.0))
        if cls_name in target_classes and score >= 0.25:
            x, y, w, h = p["box"]
            pad_x = int(w * 0.12)
            pad_y = int(h * 0.12)
            is_nipple = (cls_name in NIPPLE_CLASSES)
            boxes.append({
                "x": max(0, int(x - pad_x)),
                "y": max(0, int(y - pad_y)),
                "w": int(w + pad_x * 2),
                "h": int(h + pad_y * 2),
                "category": "nipple" if is_nipple else "genitalia",
                "layer": "yellow" if is_nipple else "red",
                "class_group": "BREAST_EXPOSED" if is_nipple else "GENITALIA",
                "raw_class": cls_name,
                "color": COLOR_YELLOW if is_nipple else COLOR_RED,
                "label": "乳首" if is_nipple else "陰部",
                "score": round(score, 2),
            })
    return boxes, None



def apply_pixel_mosaic_bgr(frame, boxes, block_ratio=0.022):
    """OpenCV BGR画像上の指定矩形リストにピクセルモザイクを焼き込む"""
    if frame is None or not boxes:
        return frame.copy() if frame is not None else None

    h_img, w_img = frame.shape[:2]
    block_size = max(12, int(min(w_img, h_img) * block_ratio))
    out = frame.copy()

    for b in boxes:
        x1 = max(0, int(round(float(b.get("x", 0)))))
        y1 = max(0, int(round(float(b.get("y", 0)))))
        x2 = min(w_img, int(round(float(b.get("x", 0)) + float(b.get("w", 0)))))
        y2 = min(h_img, int(round(float(b.get("y", 0)) + float(b.get("h", 0)))))
        if x2 <= x1 + 2 or y2 <= y1 + 2:
            continue

        roi = out[y1:y2, x1:x2]
        rw, rh = x2 - x1, y2 - y1
        small_w = max(1, rw // block_size)
        small_h = max(1, rh // block_size)
        small = cv2.resize(roi, (small_w, small_h), interpolation=cv2.INTER_LINEAR)
        mosaic_roi = cv2.resize(small, (rw, rh), interpolation=cv2.INTER_NEAREST)
        out[y1:y2, x1:x2] = mosaic_roi

    return out


def interpolate_video_boxes(keyframes_map, frame_idx):
    """
    keyframes_map: { frame_idx_int: [ {x,y,w,h,label,category,color,...}, ... ] }
    指定フレームのモザイク矩形リストをキーフレームから補間して返す
    """
    if not keyframes_map:
        return []
    keys = sorted(keyframes_map.keys())
    if frame_idx in keyframes_map:
        return keyframes_map[frame_idx]
    if frame_idx <= keys[0]:
        return keyframes_map[keys[0]]
    if frame_idx >= keys[-1]:
        return keyframes_map[keys[-1]]

    prev_k = keys[0]
    next_k = keys[-1]
    for i in range(len(keys) - 1):
        if keys[i] <= frame_idx <= keys[i + 1]:
            prev_k = keys[i]
            next_k = keys[i + 1]
            break

    b_prev = keyframes_map[prev_k]
    b_next = keyframes_map[next_k]
    if len(b_prev) != len(b_next):
        return b_prev if (frame_idx - prev_k) <= (next_k - frame_idx) else b_next

    t = (frame_idx - prev_k) / float(max(1, next_k - prev_k))
    interp = []
    for a, b in zip(b_prev, b_next):
        item = dict(a)
        item.update({
            "x": a["x"] + (b["x"] - a["x"]) * t,
            "y": a["y"] + (b["y"] - a["y"]) * t,
            "w": a["w"] + (b["w"] - a["w"]) * t,
            "h": a["h"] + (b["h"] - a["h"]) * t,
            "label": a.get("label", ""),
        })
        interp.append(item)
    return interp


def split_keyframes_by_layer(keyframes_map, default_boxes=None):
    """
    キーフレーム辞書（およびフォールバック用の default_boxes）を走査し、
    赤枠用キーフレームマップ (red_kf_map) と黄枠用キーフレームマップ (yellow_kf_map)、
    ならびに赤枠総数・黄枠総数を返す。
    """
    normalized_kf = {}
    if isinstance(keyframes_map, dict):
        for k, b_list in keyframes_map.items():
            try:
                f_idx = int(k)
            except (ValueError, TypeError):
                continue
            if isinstance(b_list, list):
                normalized_kf[f_idx] = b_list

    has_any_kf_box = any(len(v) > 0 for v in normalized_kf.values())
    if (not normalized_kf or not has_any_kf_box) and isinstance(default_boxes, list) and len(default_boxes) > 0:
        normalized_kf = {0: default_boxes}

    red_kf_map = {}
    yellow_kf_map = {}
    red_total = 0
    yellow_total = 0

    for f_idx, b_list in sorted(normalized_kf.items()):
        red_list = []
        yellow_list = []
        for b in (b_list or []):
            if not isinstance(b, dict):
                continue
            w = float(b.get("w", 0))
            h = float(b.get("h", 0))
            if w <= 2 or h <= 2:
                continue
            layer = classify_box_layer(b)
            norm_b = normalize_box_metadata(b, force_layer=layer)
            if layer == "nipple":
                yellow_list.append(norm_b)
                yellow_total += 1
            else:
                red_list.append(norm_b)
                red_total += 1
        red_kf_map[f_idx] = red_list
        yellow_kf_map[f_idx] = yellow_list

    return red_kf_map, yellow_kf_map, red_total, yellow_total



def strip_video_mosaic_suffix(filename):
    """動画ファイル名から _MOSAIC / _FYP_PREVIEW サフィックスを除去したベースファイル名を返す"""
    if not filename:
        return ""
    stem, ext = os.path.splitext(os.path.basename(filename))
    clean_stem = stem.replace("_MOSAIC", "").replace("_FYP_PREVIEW", "")
    return f"{clean_stem}{ext or '.mp4'}"


def resolve_raw_video_filename(cand_dir, requested_filename=None, vtask=None):
    """候補フォルダ内の無修正ベース動画ファイル名（例: video_output.mp4）を安全に特定する"""
    vtask = vtask if isinstance(vtask, dict) else {}
    candidates_to_try = []

    if requested_filename:
        candidates_to_try.append(strip_video_mosaic_suffix(requested_filename))
        candidates_to_try.append(os.path.basename(requested_filename))

    for k in ("output_video", "video_file", "main_video"):
        val = vtask.get(k)
        if val:
            candidates_to_try.append(strip_video_mosaic_suffix(val))

    candidates_to_try.append("video_output.mp4")

    for cand in candidates_to_try:
        if not cand:
            continue
        stem = os.path.splitext(cand)[0].upper()
        if stem.endswith("_MOSAIC") or stem.endswith("_FYP_PREVIEW"):
            continue
        if os.path.exists(os.path.join(cand_dir, cand)):
            return cand

    if os.path.isdir(cand_dir):
        vid_exts = (".mp4", ".webm", ".mov")
        raw_vids = []
        for fn in sorted(os.listdir(cand_dir)):
            if not fn.lower().endswith(vid_exts):
                continue
            if fn.lower().endswith(".tmp.mp4"):
                continue
            stem_up = os.path.splitext(fn)[0].upper()
            if stem_up.endswith("_MOSAIC") or stem_up.endswith("_FYP_PREVIEW"):
                continue
            raw_vids.append(fn)
        if "video_output.mp4" in raw_vids:
            return "video_output.mp4"
        if raw_vids:
            return raw_vids[0]

    if requested_filename and os.path.exists(os.path.join(cand_dir, requested_filename)):
        return os.path.basename(requested_filename)

    return strip_video_mosaic_suffix(requested_filename) or vtask.get("output_video") or "video_output.mp4"


def _encode_h264_mp4(tmp_raw_mp4, out_path, audio_src_path=None):
    """ブラウザ再生＆Fansly互換のため ffmpeg があれば libx264 (yuv420p) ＋元音声引き継ぎで再エンコード"""
    try:
        if audio_src_path and os.path.exists(audio_src_path):
            cmd = [
                "ffmpeg",
                "-y",
                "-i",
                tmp_raw_mp4,
                "-i",
                audio_src_path,
                "-map",
                "0:v:0",
                "-map",
                "1:a:0?",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-crf",
                "18",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-shortest",
                out_path,
            ]
        else:
            cmd = [
                "ffmpeg",
                "-y",
                "-i",
                tmp_raw_mp4,
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-crf",
                "18",
                out_path,
            ]
        subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        if os.path.exists(tmp_raw_mp4):
            os.remove(tmp_raw_mp4)
    except Exception:
        if os.path.exists(out_path):
            os.remove(out_path)
        if os.path.exists(tmp_raw_mp4):
            os.rename(tmp_raw_mp4, out_path)


def _load_post_data(cand_dir):
    json_path = os.path.join(cand_dir, "post_data.json")
    if not os.path.exists(json_path):
        return {}, json_path
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            return json.load(f), json_path
    except Exception:
        return {}, json_path


def _save_post_data(json_path, pdata):
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(pdata, f, ensure_ascii=False, indent=2)


def apply_image_genitalia_mosaic(cand_dir, slot_id, src_file, boxes):
    """
    Step 1 画像モザイク焼き込み処理：
    - 画像は有料サブスク本編にのみ添付されるため、常に陰部モザイク（<slot_id>_MOSAIC.png）のみを生成する。
    - post_data.json が存在する場合は mosaic_slots に slot_id を記録する。
    """
    if not slot_id and src_file:
        stem = os.path.splitext(os.path.basename(src_file))[0]
        slot_id = (
            stem.replace("_HQ", "")
            .replace("_SD", "")
            .replace("_MOSAIC", "")
            .replace("_FYP_PREVIEW", "")
        )

    hq_candidate = f"{slot_id}_HQ.png"
    if os.path.exists(os.path.join(cand_dir, hq_candidate)):
        src_path = os.path.join(cand_dir, hq_candidate)
    else:
        src_path = os.path.join(cand_dir, src_file)

    if not os.path.exists(src_path):
        return None, "元画像が見つかりません"

    frame = cv2.imread(src_path)
    if frame is None:
        return None, "元画像の読み込みに失敗しました"

    genitalia_boxes = [
        b for b in (boxes or [])
        if isinstance(b, dict) and classify_box_layer(b) == "genitalia"
    ]
    target_boxes = genitalia_boxes if genitalia_boxes else (boxes or [])

    out_frame = apply_pixel_mosaic_bgr(frame, target_boxes)
    out_name = f"{slot_id}_MOSAIC.png"
    out_path = os.path.join(cand_dir, out_name)
    cv2.imwrite(out_path, out_frame)

    pdata, json_path = _load_post_data(cand_dir)
    if pdata or os.path.exists(json_path):
        mosaic_slots = pdata.get("mosaic_slots")
        if not isinstance(mosaic_slots, list):
            mosaic_slots = []
        if slot_id and slot_id not in mosaic_slots:
            mosaic_slots.append(slot_id)
        pdata["mosaic_slots"] = sorted(set(mosaic_slots))
        _save_post_data(json_path, pdata)

    return {
        "output_file": out_name,
        "slot_id": slot_id,
        "applied_count": len(target_boxes),
    }, None


def render_step2_video_mosaic(cand_dir, src_vid, keyframes_map=None, default_boxes=None):
    """
    Step 2 動画の「1クリック一括レンダリング ＆ 3パターン自動判定ロジック」:
      - パターン①: 赤枠1個以上（＋黄枠1個以上 または 黄枠0個） -> mosaic_mode: "both"
      - パターン②: 赤枠0個 ＋ 黄枠1個以上 -> mosaic_mode: "nipple_only"
      - パターン③: 赤枠0個 ＋ 黄枠0個 -> mosaic_mode: "none"
    """
    pdata, json_path = _load_post_data(cand_dir)
    vtask = pdata.get("video_task") if isinstance(pdata.get("video_task"), dict) else {}

    raw_video_name = resolve_raw_video_filename(cand_dir, src_vid, vtask)
    src_path = os.path.join(cand_dir, raw_video_name)
    if not os.path.exists(src_path):
        return None, f"元動画が見つかりません: {raw_video_name}"

    base_stem, ext = os.path.splitext(raw_video_name)
    ext = ext or ".mp4"
    mosaic_video_name = f"{base_stem}_MOSAIC{ext}"
    fyp_video_name = f"{base_stem}_FYP_PREVIEW{ext}"
    mosaic_video_path = os.path.join(cand_dir, mosaic_video_name)
    fyp_video_path = os.path.join(cand_dir, fyp_video_name)

    red_kf_map, yellow_kf_map, red_total, yellow_total = split_keyframes_by_layer(
        keyframes_map, default_boxes=default_boxes
    )

    # パターン③：赤枠0個 ＆ 黄枠0個（完全健全動画 / 無修正）
    if red_total == 0 and yellow_total == 0:
        for p in (mosaic_video_path, fyp_video_path):
            if os.path.exists(p) and os.path.abspath(p) != os.path.abspath(src_path):
                try:
                    os.remove(p)
                except OSError:
                    pass

        vtask["output_video"] = raw_video_name
        vtask["main_video"] = raw_video_name
        vtask["fyp_preview_video"] = raw_video_name
        vtask["mosaic_mode"] = "none"
        vtask.pop("mosaic_video_file", None)
        pdata["video_task"] = vtask
        if pdata or os.path.exists(json_path):
            _save_post_data(json_path, pdata)

        return {
            "pattern": 3,
            "mosaic_mode": "none",
            "output_video": raw_video_name,
            "main_video": raw_video_name,
            "fyp_preview_video": raw_video_name,
            "output_file": raw_video_name,
            "red_count": 0,
            "yellow_count": 0,
            "video_task": vtask,
        }, None

    cap = cv2.VideoCapture(src_path)
    if not cap.isOpened():
        return None, f"動画ファイルを開けませんでした: {raw_video_name}"

    fps = cap.get(cv2.CAP_PROP_FPS) or 16.0
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")

    # パターン②：赤枠0個 ＋ 黄枠1個以上（乳首のみあり）
    if red_total == 0 and yellow_total >= 1:
        tmp_fyp_mp4 = fyp_video_path + ".tmp.mp4"
        writer_fyp = cv2.VideoWriter(tmp_fyp_mp4, fourcc, fps, (w, h))

        idx = 0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            cur_yellow = interpolate_video_boxes(yellow_kf_map, idx)
            fyp_frame = apply_pixel_mosaic_bgr(frame, cur_yellow)
            writer_fyp.write(fyp_frame)
            idx += 1

        cap.release()
        writer_fyp.release()

        _encode_h264_mp4(tmp_fyp_mp4, fyp_video_path, audio_src_path=src_path)

        if os.path.exists(mosaic_video_path) and os.path.abspath(mosaic_video_path) != os.path.abspath(src_path):
            try:
                os.remove(mosaic_video_path)
            except OSError:
                pass

        vtask["output_video"] = raw_video_name
        vtask["main_video"] = raw_video_name
        vtask["fyp_preview_video"] = fyp_video_name
        vtask["mosaic_mode"] = "nipple_only"
        vtask.pop("mosaic_video_file", None)
        pdata["video_task"] = vtask
        if pdata or os.path.exists(json_path):
            _save_post_data(json_path, pdata)

        return {
            "pattern": 2,
            "mosaic_mode": "nipple_only",
            "output_video": raw_video_name,
            "main_video": raw_video_name,
            "fyp_preview_video": fyp_video_name,
            "output_file": fyp_video_name,
            "red_count": red_total,
            "yellow_count": yellow_total,
            "video_task": vtask,
        }, None

    # パターン①：赤枠1個以上（＋黄枠1個以上 または 黄枠0個）
    tmp_mosaic_mp4 = mosaic_video_path + ".tmp.mp4"
    writer_mosaic = cv2.VideoWriter(tmp_mosaic_mp4, fourcc, fps, (w, h))

    has_yellow = (yellow_total >= 1)
    tmp_fyp_mp4 = fyp_video_path + ".tmp.mp4" if has_yellow else None
    writer_fyp = cv2.VideoWriter(tmp_fyp_mp4, fourcc, fps, (w, h)) if has_yellow else None

    idx = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        cur_red = interpolate_video_boxes(red_kf_map, idx)
        mosaic_frame = apply_pixel_mosaic_bgr(frame, cur_red)
        writer_mosaic.write(mosaic_frame)

        if writer_fyp is not None:
            cur_yellow = interpolate_video_boxes(yellow_kf_map, idx)
            fyp_frame = apply_pixel_mosaic_bgr(mosaic_frame, cur_yellow)
            writer_fyp.write(fyp_frame)

        idx += 1

    cap.release()
    writer_mosaic.release()
    if writer_fyp is not None:
        writer_fyp.release()

    _encode_h264_mp4(tmp_mosaic_mp4, mosaic_video_path, audio_src_path=src_path)

    if has_yellow and tmp_fyp_mp4 is not None:
        _encode_h264_mp4(tmp_fyp_mp4, fyp_video_path, audio_src_path=src_path)
    else:
        # 赤枠1個以上＋黄枠0個のケース：1回エンコードした *_MOSAIC.mp4 を *_FYP_PREVIEW.mp4 へコピー
        shutil.copy2(mosaic_video_path, fyp_video_path)

    vtask["output_video"] = raw_video_name
    vtask["main_video"] = mosaic_video_name
    vtask["fyp_preview_video"] = fyp_video_name
    vtask["mosaic_mode"] = "both"
    vtask.pop("mosaic_video_file", None)
    pdata["video_task"] = vtask
    if pdata or os.path.exists(json_path):
        _save_post_data(json_path, pdata)

    return {
        "pattern": 1,
        "mosaic_mode": "both",
        "output_video": raw_video_name,
        "main_video": mosaic_video_name,
        "fyp_preview_video": fyp_video_name,
        "output_file": mosaic_video_name,
        "red_count": red_total,
        "yellow_count": yellow_total,
        "video_task": vtask,
    }, None


def reset_video_mosaic(cand_dir, requested_filename=None):
    """
    生成済みの *_MOSAIC.mp4 / *_FYP_PREVIEW.mp4 を削除し、
    post_data.json の video_task をパターン③（無修正状態・mosaic_mode: "none"）に戻す。
    """
    if not os.path.isdir(cand_dir):
        return None, "対象フォルダが見つかりません"

    pdata, json_path = _load_post_data(cand_dir)
    vtask = pdata.get("video_task") if isinstance(pdata.get("video_task"), dict) else {}

    raw_video_name = resolve_raw_video_filename(cand_dir, requested_filename, vtask)
    raw_video_abs = os.path.abspath(os.path.join(cand_dir, raw_video_name))

    base_stem, ext = os.path.splitext(raw_video_name)
    ext = ext or ".mp4"
    target_remove_names = {
        f"{base_stem}_MOSAIC{ext}",
        f"{base_stem}_FYP_PREVIEW{ext}",
    }

    for fn in os.listdir(cand_dir):
        fn_up = fn.upper()
        if fn_up.endswith("_MOSAIC.MP4") or fn_up.endswith("_FYP_PREVIEW.MP4") or fn_up.endswith(".TMP.MP4"):
            target_remove_names.add(fn)

    removed_files = []
    for fn in sorted(target_remove_names):
        p = os.path.join(cand_dir, fn)
        if os.path.exists(p) and os.path.abspath(p) != raw_video_abs:
            try:
                os.remove(p)
                removed_files.append(fn)
            except OSError:
                pass

    vtask["output_video"] = raw_video_name
    vtask["main_video"] = raw_video_name
    vtask["fyp_preview_video"] = raw_video_name
    vtask["mosaic_mode"] = "none"
    vtask.pop("mosaic_video_file", None)
    pdata["video_task"] = vtask
    if pdata or os.path.exists(json_path):
        _save_post_data(json_path, pdata)

    return {
        "pattern": 3,
        "mosaic_mode": "none",
        "output_video": raw_video_name,
        "main_video": raw_video_name,
        "fyp_preview_video": raw_video_name,
        "removed_files": removed_files,
        "video_task": vtask,
    }, None


def reset_image_mosaic(cand_dir, slot_id=None, filename=None):
    """Step 1 画像用のモザイクリセット（<slot_id>_MOSAIC.png を削除し mosaic_slots から除外）"""
    if not os.path.isdir(cand_dir):
        return None, "対象フォルダが見つかりません"

    if not slot_id and filename:
        stem = os.path.splitext(os.path.basename(filename))[0]
        slot_id = (
            stem.replace("_HQ", "")
            .replace("_SD", "")
            .replace("_MOSAIC", "")
            .replace("_FYP_PREVIEW", "")
        )

    if not slot_id:
        return None, "slot_id が指定されていません"

    removed_files = []
    for suffix in ("_MOSAIC.png", "_FYP_PREVIEW.png"):
        fn = f"{slot_id}{suffix}"
        p = os.path.join(cand_dir, fn)
        if os.path.exists(p):
            try:
                os.remove(p)
                removed_files.append(fn)
            except OSError:
                pass

    pdata, json_path = _load_post_data(cand_dir)
    if pdata or os.path.exists(json_path):
        mosaic_slots = pdata.get("mosaic_slots")
        if isinstance(mosaic_slots, list) and slot_id in mosaic_slots:
            pdata["mosaic_slots"] = [s for s in mosaic_slots if s != slot_id]
        _save_post_data(json_path, pdata)

    return {
        "slot_id": slot_id,
        "removed_files": removed_files,
        "mosaic_slots": pdata.get("mosaic_slots", []),
    }, None



