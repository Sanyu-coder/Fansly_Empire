import hashlib
import os
import py_compile
import shutil
import sys
import tempfile

import cv2
import numpy as np
from PIL import Image, ImageDraw

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from app.services.media_blur_service import (
    ORIGINALS_SUBDIR_NAME,
    cleanup_blur_preview,
    cleanup_original_backup,
    detect_genitalia_boxes,
    ensure_original_backup,
    get_original_media_path,
    get_original_media_relpath,
    normalize_redact_boxes,
    prepare_vault_media_assets,
    redact_media_file,
    resolve_existing_original_media_path,
)


def _log(msg: str) -> None:
    print(msg, flush=True)


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _create_checker_test_image(path: str, width: int = 512, height: int = 512) -> None:
    """高コントラストなチェッカーパターンを中央に含むテスト用PNG画像を生成する"""
    img = Image.new("RGB", (width, height), color=(28, 36, 58))
    draw = ImageDraw.Draw(img)
    # 外周の基準マーカー領域（左上）
    draw.rectangle([16, 16, 96, 96], fill=(40, 180, 95))

    # 中央 (128..384, 128..384) に細かい白黒チェッカーパターンを描画（モザイク・ぼかし検証用）
    cell = 4
    for y in range(128, 384, cell):
        for x in range(128, 384, cell):
            is_white = ((x // cell) + (y // cell)) % 2 == 0
            color = (250, 240, 250) if is_white else (15, 10, 20)
            draw.rectangle([x, y, x + cell - 1, y + cell - 1], fill=color)

    ext = os.path.splitext(path)[1].lower()
    if ext in (".jpg", ".jpeg"):
        img.save(path, format="JPEG", quality=95)
    else:
        img.save(path, format="PNG")


def _create_test_mp4_video(path: str, width: int = 320, height: int = 240, num_frames: int = 6) -> None:
    """中央に高コントラストなチェッカーパターンを持つテスト用MP4動画を生成する"""
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(path, fourcc, 12.0, (width, height))
    if not writer.isOpened():
        raise RuntimeError(f"Failed to open VideoWriter for test video: {path}")
    try:
        for idx in range(num_frames):
            frame = np.full((height, width, 3), (45, 30, 25), dtype=np.uint8)
            # 左上の非加工マーカー
            frame[10:50, 10:50] = (60, 200, 80)
            # 中央 (80..240, 60..180) に細かい白黒チェッカーを描画
            for y in range(60, 180, 4):
                for x in range(80, 240, 4):
                    is_light = (((x // 4) + (y // 4) + idx) % 2) == 0
                    frame[y : y + 4, x : x + 4] = (245, 245, 245) if is_light else (12, 12, 12)
            writer.write(frame)
    finally:
        writer.release()


class _MockGenitaliaDetector:
    """陰部ラベルと非陰部ラベルの両方を返す決定論的モックDetector"""

    def __init__(self, detections):
        self._detections = detections

    def detect(self, _image_input):
        return list(self._detections)


def run_mosaic_tests() -> None:
    _log("=== [STEP 0] Syntax check (py_compile) ===")
    for rel_mod in (
        ("app", "services", "media_blur_service.py"),
        ("app", "services", "character_loader.py"),
        ("app", "services", "sales_engine.py"),
        ("app", "services", "followup_worker.py"),
        ("app", "services", "fansly_dm_bot.py"),
        ("app", "routes", "vault_routes.py"),
        ("app", "routes", "dashboard.py"),
    ):
        py_compile.compile(os.path.join(BASE_DIR, *rel_mod), doraise=True)
    py_compile.compile(os.path.abspath(__file__), doraise=True)
    _log("  -> Passed: py_compile succeeded")

    tmp_root = tempfile.mkdtemp(prefix="ticket01_redact_test_")
    vault_dir = os.path.join(tmp_root, "ppv_vault")
    os.makedirs(vault_dir, exist_ok=True)

    try:
        # ------------------------------------------------------------------
        # TEST 1: 新規追加時 (prepare_vault_media_assets) の原本退避と二重保持
        # ------------------------------------------------------------------
        _log("=== [TEST 1] prepare_vault_media_assets backs up untouched original into originals/ ===")
        added_img_path = os.path.join(vault_dir, "upload_sample_001.jpg")
        _create_checker_test_image(added_img_path, 400, 400)
        added_orig_hash = _sha256_file(added_img_path)

        prep = prepare_vault_media_assets(added_img_path, media_type="image")
        try:
            expected_orig_path = os.path.join(vault_dir, ORIGINALS_SUBDIR_NAME, "upload_sample_001.jpg")
            assert prep["original_path"] == expected_orig_path
            assert prep["original_relpath"] == "originals/upload_sample_001.jpg"
            assert os.path.isfile(expected_orig_path), "Original backup file must exist in originals/"
            assert os.path.isfile(added_img_path), "Vault target file must not be deleted"
            assert _sha256_file(expected_orig_path) == added_orig_hash
            assert prep["blur_preview_exists"] is True
        finally:
            if prep.get("thumbnail_path") and os.path.exists(prep["thumbnail_path"]):
                os.remove(prep["thumbnail_path"])
        _log("  -> Passed: originals/ backup created alongside active media & blur preview")

        # ------------------------------------------------------------------
        # TEST 2: NudeNet 実推論で未検出時に例外を投げず {"detected": False, "boxes": []} を返すこと
        # ------------------------------------------------------------------
        _log("=== [TEST 2] Real NudeNet non-detection returns {'detected': False, 'boxes': []} without raising ===")
        real_det_res = detect_genitalia_boxes(added_img_path, media_type="image")
        assert isinstance(real_det_res, dict)
        assert real_det_res == {"detected": False, "boxes": []}, f"Unexpected result: {real_det_res}"

        missing_det_res = detect_genitalia_boxes(os.path.join(vault_dir, "does_not_exist.jpg"))
        assert missing_det_res == {"detected": False, "boxes": []}
        _log(f"  -> Passed: NudeNet non-detection returned {real_det_res}")

        # ------------------------------------------------------------------
        # TEST 3: NudeNet 陰部ラベルのみフィルタリング & 正規化座標 (x, y, w, h) 変換
        # ------------------------------------------------------------------
        _log("=== [TEST 3] AI auto-detection filters genitalia labels only & normalizes coordinates ===")
        non_genitalia_mock = _MockGenitaliaDetector(
            [
                {"class": "FACE_FEMALE", "score": 0.96, "box": [50, 40, 100, 100]},
                {"class": "FEMALE_BREAST_EXPOSED", "score": 0.89, "box": [120, 160, 140, 110]},
                {"class": "FEMALE_GENITALIA_COVERED", "score": 0.85, "box": [140, 280, 110, 80]},
            ]
        )
        filtered_empty = detect_genitalia_boxes(
            added_img_path,
            media_type="image",
            detector=non_genitalia_mock,
        )
        assert filtered_empty == {"detected": False, "boxes": []}, (
            f"Non-exposed/non-genitalia labels must be ignored, got {filtered_empty}"
        )

        genitalia_mock = _MockGenitaliaDetector(
            [
                {"class": "FACE_FEMALE", "score": 0.95, "box": [20, 20, 80, 80]},
                {"class": "FEMALE_GENITALIA_EXPOSED", "score": 0.91, "box": [128, 128, 256, 256]},
                {"class": "ANUS_EXPOSED", "score": 0.74, "box": [200, 300, 60, 50]},
            ]
        )
        det_hit = detect_genitalia_boxes(
            added_img_path,
            media_type="image",
            padding_ratio=0.0,
            detector=genitalia_mock,
        )
        assert det_hit["detected"] is True
        assert len(det_hit["boxes"]) == 2
        labels_found = {b["label"] for b in det_hit["boxes"]}
        assert labels_found == {"FEMALE_GENITALIA_EXPOSED", "ANUS_EXPOSED"}
        for b in det_hit["boxes"]:
            for key in ("x", "y", "w", "h"):
                assert key in b
                assert 0.0 <= b[key] <= 1.0
        _log(f"  -> Passed: Genitalia-only boxes extracted: {det_hit['boxes']}")

        # ------------------------------------------------------------------
        # TEST 4: 手動座標指定 + mosaic 加工 & 原本保持検証
        # ------------------------------------------------------------------
        _log("=== [TEST 4] Manual boxes + 'mosaic' redaction preserves original and pixelates target region ===")
        img_target = os.path.join(vault_dir, "redact_image_001.png")
        _create_checker_test_image(img_target, 512, 512)
        orig_img_sha = _sha256_file(img_target)

        manual_boxes = [{"x": 0.25, "y": 0.25, "w": 0.5, "h": 0.5}]
        res_mosaic = redact_media_file(
            media_path=img_target,
            redact_type="mosaic",
            boxes=manual_boxes,
            auto_detect=False,
        )
        assert res_mosaic["ok"] is True
        assert res_mosaic["detected"] is True
        assert res_mosaic["redact_type"] == "mosaic"

        orig_backup_path = get_original_media_path(img_target)
        assert resolve_existing_original_media_path(img_target) == orig_backup_path
        assert get_original_media_relpath("redact_image_001.png") == "originals/redact_image_001.png"
        assert _sha256_file(orig_backup_path) == orig_img_sha, "Original in originals/ must be 100% untouched"
        assert _sha256_file(img_target) != orig_img_sha, "Processed image must differ from untouched original"

        with Image.open(orig_backup_path) as orig_pil, Image.open(img_target) as mos_pil:
            orig_arr = np.asarray(orig_pil.convert("RGB"), dtype=np.float32)
            mos_arr = np.asarray(mos_pil.convert("RGB"), dtype=np.float32)
            # 左上マーカー領域 (16..96, 16..96) は無変更であること
            outside_diff = float(np.abs(orig_arr[20:90, 20:90] - mos_arr[20:90, 20:90]).max())
            assert outside_diff == 0.0, f"Outside region should remain unchanged, max diff={outside_diff}"
            # 中央領域 (128..384, 128..384) はモザイク化により大きく変化していること
            inside_diff = float(np.abs(orig_arr[140:370, 140:370] - mos_arr[140:370, 140:370]).mean())
            assert inside_diff > 20.0, f"Inside region must be pixelated, mean diff={inside_diff}"
        _log(f"  -> Passed: Mosaic applied cleanly (inside mean diff={inside_diff:.2f}, outside diff={outside_diff:.2f})")

        # ------------------------------------------------------------------
        # TEST 5: 同一ファイルの 'blur' への再加工でも初回原本が上書き保護されること
        # ------------------------------------------------------------------
        _log("=== [TEST 5] Switching to 'blur' re-processes from clean original without overwriting originals/ ===")
        res_blur = redact_media_file(
            media_path=img_target,
            redact_type="blur",
            boxes=manual_boxes,
            auto_detect=False,
        )
        assert res_blur["ok"] is True
        assert res_blur["redact_type"] == "blur"
        assert _sha256_file(orig_backup_path) == orig_img_sha, "Original backup must NEVER be overwritten on re-edit"

        with Image.open(img_target) as blur_pil:
            blur_arr = np.asarray(blur_pil.convert("RGB"), dtype=np.float32)
            mosaic_vs_blur_diff = float(np.abs(mos_arr[140:370, 140:370] - blur_arr[140:370, 140:370]).mean())
            assert mosaic_vs_blur_diff > 1.0, "Blur output must differ from block mosaic output"
            outside_blur_diff = float(np.abs(orig_arr[20:90, 20:90] - blur_arr[20:90, 20:90]).max())
            assert outside_blur_diff == 0.0
        _log(f"  -> Passed: Blur applied from clean original (mosaic vs blur diff={mosaic_vs_blur_diff:.2f})")

        # ------------------------------------------------------------------
        # TEST 6: AI自動検出 (auto_detect=True) による自動加工と preview_detect_only モード
        # ------------------------------------------------------------------
        _log("=== [TEST 6] auto_detect=True redaction & preview_detect_only non-destructive check ===")
        ai_img_path = os.path.join(vault_dir, "ai_detect_001.png")
        _create_checker_test_image(ai_img_path, 512, 512)
        ai_orig_sha = _sha256_file(ai_img_path)

        ai_mock_detector = _MockGenitaliaDetector(
            [{"class": "MALE_GENITALIA_EXPOSED", "score": 0.88, "box": [128, 128, 256, 256]}]
        )
        preview_only = redact_media_file(
            media_path=ai_img_path,
            preview_detect_only=True,
            detector=ai_mock_detector,
        )
        assert preview_only["detected"] is True
        assert len(preview_only["boxes"]) == 1
        assert _sha256_file(ai_img_path) == ai_orig_sha, "preview_detect_only must not modify media file"

        ai_applied = redact_media_file(
            media_path=ai_img_path,
            redact_type="mosaic",
            auto_detect=True,
            detector=ai_mock_detector,
        )
        assert ai_applied["ok"] is True
        assert ai_applied["detected"] is True
        assert _sha256_file(get_original_media_path(ai_img_path)) == ai_orig_sha
        assert _sha256_file(ai_img_path) != ai_orig_sha
        _log("  -> Passed: AI auto-detect preview & redaction verified")

        # ------------------------------------------------------------------
        # TEST 7: 動画 (MP4) の一時ファイル経由の安全なフレーム加工 (mosaic & blur) と原本保持
        # ------------------------------------------------------------------
        _log("=== [TEST 7] Video frame redaction ('mosaic' & 'blur') via temp file + original backup ===")
        vid_target = os.path.join(vault_dir, "redact_clip_001.mp4")
        _create_test_mp4_video(vid_target, width=320, height=240, num_frames=6)
        orig_vid_sha = _sha256_file(vid_target)

        vid_boxes = [{"x": 0.25, "y": 0.25, "w": 0.5, "h": 0.5}]
        for v_mode in ("mosaic", "blur"):
            v_res = redact_media_file(
                media_path=vid_target,
                redact_type=v_mode,
                boxes=vid_boxes,
                media_type="video",
            )
            assert v_res["ok"] is True
            assert v_res["media_type"] == "video"
            assert v_res["redact_type"] == v_mode
            assert os.path.isfile(vid_target) and os.path.getsize(vid_target) > 0
            assert _sha256_file(get_original_media_path(vid_target)) == orig_vid_sha

            # 一時ファイルが vault_dir 内に残留していないことを確認
            leftover_tmps = [
                fn for fn in os.listdir(vault_dir) if fn.startswith(("redact_vid_", "redact_img_", "orig_bak_"))
            ]
            assert not leftover_tmps, f"Temporary files must be cleaned up, found: {leftover_tmps}"

            # 出力動画が正常にデコードでき、フレーム数が維持されていることを確認
            cap_check = cv2.VideoCapture(vid_target)
            assert cap_check.isOpened(), f"Processed video ({v_mode}) must be readable"
            read_count = 0
            try:
                while True:
                    ok_f, frm = cap_check.read()
                    if not ok_f or frm is None:
                        break
                    read_count += 1
            finally:
                cap_check.release()
            assert read_count == 6, f"Expected 6 frames in processed video ({v_mode}), got {read_count}"
        _log("  -> Passed: Video mosaic & blur written safely via temporary files with zero leftovers")

        # ------------------------------------------------------------------
        # TEST 8: 例外・境界値ハンドリング (不正な加工種別 / NaN座標 / クリーンアップ)
        # ------------------------------------------------------------------
        _log("=== [TEST 8] Validation & edge-case handling (invalid redact_type, NaN boxes, cleanup) ===")
        bad_boxes = normalize_redact_boxes(
            [
                {"x": "NaN", "y": "NaN", "w": "NaN", "h": "NaN"},
                {"x": -0.2, "y": 0.1, "w": 0.5, "h": 0.4},
                {"x": 0.5, "y": 0.5, "w": 0.0, "h": 0.2},
            ]
        )
        assert len(bad_boxes) == 1
        assert bad_boxes[0]["x"] == 0.0 and bad_boxes[0]["w"] == 0.3

        raised_invalid_type = False
        try:
            redact_media_file(img_target, redact_type="shrinkwrap", boxes=manual_boxes)
        except ValueError:
            raised_invalid_type = True
        assert raised_invalid_type, "Invalid redact_type must raise ValueError"

        assert ensure_original_backup(img_target) == orig_backup_path
        assert cleanup_blur_preview(img_target) is True
        assert cleanup_original_backup(img_target) is True
        assert resolve_existing_original_media_path(img_target) is None
        _log("  -> Passed: Edge cases and cleanup verified")

        # ------------------------------------------------------------------
        # TEST 9 (TICKET-02): APIエンドポイント・自動非公開ロック・原本切替・顧客送信ガード統合テスト
        # ------------------------------------------------------------------
        _log("=== [TEST 9] TICKET-02 HTTP API endpoints, auto-private lock, ?version=original, and send guards ===")
        import io
        from app import create_app
        from app.core import config
        from app.core.database import get_ppv_vault_records_db, init_db
        from app.core.llm_client import llm_client
        from app.services.character_loader import get_char_paths, resolve_customer_safe_ppv_media_path
        from app.services.fansly_dm_bot import bot_manager
        from app.services.sales_engine import sales_engine

        orig_chars_root = config.CHARACTERS_ROOT
        orig_db_path = config.DB_PATH
        orig_analyze_vision = llm_client.analyze_ppv_media_with_vision
        orig_translate_ja = llm_client.translate_to_japanese
        orig_nude_det = getattr(__import__("app.services.media_blur_service", fromlist=["_NUDE_DETECTOR_INSTANCE"]), "_NUDE_DETECTOR_INSTANCE")

        api_tmp_chars = os.path.join(tmp_root, "characters_api")
        api_tmp_db = os.path.join(tmp_root, "ticket02_test.db")
        os.makedirs(api_tmp_chars, exist_ok=True)
        char_id = "ticket02_char"

        import app.services.media_blur_service as mbs_mod
        mbs_mod._NUDE_DETECTOR_INSTANCE = _MockGenitaliaDetector(
            [{"class": "FEMALE_GENITALIA_EXPOSED", "score": 0.94, "box": [128, 128, 120, 120]}]
        )
        llm_client.analyze_ppv_media_with_vision = lambda **kw: {
            "tags": ["自撮り", "テスト"],
            "description_memo": "テスト用メモ",
            "pitch_hint": "Secret private photo just for you!",
            "used_vision": False,
            "vision_model": None,
            "extracted_thumbnail_used": False,
        }
        llm_client.translate_to_japanese = lambda txt: f"[JA] {txt}"

        try:
            config.CHARACTERS_ROOT = api_tmp_chars
            config.DB_PATH = api_tmp_db
            init_db(api_tmp_db)

            app = create_app()
            app.config["TESTING"] = True
            client = app.test_client()

            src_upload_img = os.path.join(tmp_root, "raw_upload_source.png")
            _create_checker_test_image(src_upload_img, 400, 400)
            with open(src_upload_img, "rb") as fp:
                raw_png_bytes = fp.read()
            expected_orig_sha = hashlib.sha256(raw_png_bytes).hexdigest()

            # 1) 新規アップロード時は自動で is_active=False かつ is_redacted=False (未加工ロック)
            up_res = client.post(
                "/api/vault/upload",
                data={
                    "char_id": char_id,
                    "ladder_step": "1",
                    "file": (io.BytesIO(raw_png_bytes), "secret_pose.png"),
                },
                content_type="multipart/form-data",
            )
            assert up_res.status_code == 200, f"Upload failed: {up_res.data}"
            up_j = up_res.get_json()
            assert up_j["ok"] is True
            uploaded_item = up_j["item"]
            ppv_id = uploaded_item["ppv_id"]
            saved_fname = uploaded_item["filename"]
            assert uploaded_item["is_active"] is False, f"New upload must be locked (is_active=False): {uploaded_item}"
            assert uploaded_item["is_redacted"] is False

            db_map = get_ppv_vault_records_db(char_id, db_path=api_tmp_db)
            assert db_map[ppv_id]["is_active"] == 0, "SQLite ppv_vault.is_active must be 0 right after upload"

            # 2) 未加工状態で手動公開ONしようとしてもブロックされること
            upd_locked = client.post(
                "/api/vault/update",
                json={"char_id": char_id, "ppv_id": ppv_id, "is_active": True},
            )
            assert upd_locked.status_code == 400
            assert upd_locked.get_json()["ok"] is False

            # 3) 営業候補選定・手動送信API・ボット送信パス解決すべてで未加工ファイルがブロックされること
            sel_ppv, _ = sales_engine.select_ppv_and_price(
                char_id=char_id,
                fan_id="fan_guard_1",
                current_tier="free_follower",
                total_spent_usd=0.0,
                db_path=api_tmp_db,
            )
            assert sel_ppv is None, "Unredacted/inactive item must be excluded from sales_engine candidates"

            cand_res = client.get(f"/api/monitoring/ppv_candidates?char_id={char_id}&fan_id=fan_guard_1")
            assert cand_res.status_code == 200
            assert len(cand_res.get_json()["items"]) == 0, "Unredacted item must not appear in manual PPV candidates"

            blocked_manual = client.post(
                "/api/monitoring/send_manual",
                json={
                    "char_id": char_id,
                    "fan_id": "fan_guard_1",
                    "content": "Trying to send unredacted PPV",
                    "ppv_id": ppv_id,
                    "ppv_price_usd": 9.99,
                },
            )
            assert blocked_manual.status_code == 400, f"Unredacted manual send must be rejected: {blocked_manual.data}"
            assert blocked_manual.get_json()["code"] == "UNREDACTED_PPV_BLOCKED"

            safe_p_before, err_before = resolve_customer_safe_ppv_media_path(char_id, uploaded_item)
            assert safe_p_before is None and err_before is not None

            # Bot Playwright送信メソッドに強制的に未加工itemや originals/ パスを渡しても遮断されること
            worker = bot_manager._get_or_create_worker(char_id)
            worker.vault_dir = get_char_paths(char_id)["vault_dir"]

            class _DummyPage:
                def query_selector(self, _s):
                    return None

            bot_unredacted_ok = worker._send_via_playwright_page(
                page=_DummyPage(),
                fan_id="fan_guard_1",
                message_en="Test",
                selected_ppv={"ppv_id": ppv_id, "filename": saved_fname, "offered_price_usd": 9.99},
            )
            assert bot_unredacted_ok is False
            assert "blocked" in (worker.last_error or "").lower()

            bot_orig_ok = worker._send_via_playwright_page(
                page=_DummyPage(),
                fan_id="fan_guard_1",
                message_en="Test",
                selected_ppv={"ppv_id": ppv_id, "filename": f"originals/{saved_fname}", "offered_price_usd": 9.99},
            )
            assert bot_orig_ok is False

            # 4) /api/vault/redact (preview_detect_only: true) は座標のみ返し、非公開・未加工状態を維持すること
            det_res = client.post(
                "/api/vault/redact",
                json={"char_id": char_id, "ppv_id": ppv_id, "preview_detect_only": True},
            )
            assert det_res.status_code == 200
            det_j = det_res.get_json()
            assert det_j["ok"] is True and det_j["preview_detect_only"] is True
            assert det_j["detected"] is True and len(det_j["boxes"]) == 1

            # 5) /api/vault/redact 確定実行 -> mosaic適用 + ステータス更新 + is_active 自動 True + blur_preview 再生成
            commit_res = client.post(
                "/api/vault/redact",
                json={
                    "char_id": char_id,
                    "ppv_id": ppv_id,
                    "redact_type": "mosaic",
                    "boxes": det_j["boxes"],
                    "preview_detect_only": False,
                },
            )
            assert commit_res.status_code == 200, f"Redact commit failed: {commit_res.data}"
            commit_j = commit_res.get_json()
            assert commit_j["ok"] is True
            assert commit_j["is_redacted"] is True
            assert commit_j["is_active"] is True
            assert commit_j["redact_type"] == "mosaic"
            assert commit_j["blur_preview_exists"] is True

            db_map_after = get_ppv_vault_records_db(char_id, db_path=api_tmp_db)
            assert db_map_after[ppv_id]["is_active"] == 1, "SQLite ppv_vault.is_active must auto-switch to 1 after redact"

            # 6) /api/vault/media/<char_id>/<filename> と ?version=original の配信切り替え検証
            proc_media_res = client.get(f"/api/vault/media/{char_id}/{saved_fname}")
            assert proc_media_res.status_code == 200
            proc_bytes = proc_media_res.data
            proc_sha = hashlib.sha256(proc_bytes).hexdigest()
            proc_media_res.close()

            orig_media_res = client.get(f"/api/vault/media/{char_id}/{saved_fname}?version=original")
            assert orig_media_res.status_code == 200
            orig_bytes = orig_media_res.data
            orig_sha = hashlib.sha256(orig_bytes).hexdigest()
            orig_media_res.close()

            assert orig_sha == expected_orig_sha, "version=original must return the exact untouched original file"
            assert proc_sha != orig_sha, "Default media endpoint must return the redacted file, distinct from original"

            # 直接 /api/vault/media/<char_id>/originals/<filename> を ?version=original 無しで叩いても実原本は返さないこと
            direct_orig_attempt = client.get(f"/api/vault/media/{char_id}/originals/{saved_fname}")
            assert direct_orig_attempt.mimetype == "image/svg+xml"

            # 7) 加工確定後は営業候補選定・手動送信APIともに加工済みファイルのみを参照して成功すること
            sel_ppv_after, _ = sales_engine.select_ppv_and_price(
                char_id=char_id,
                fan_id="fan_guard_1",
                current_tier="free_follower",
                total_spent_usd=0.0,
                db_path=api_tmp_db,
            )
            assert sel_ppv_after is not None and sel_ppv_after["ppv_id"] == ppv_id

            safe_p_after, err_after = resolve_customer_safe_ppv_media_path(char_id, sel_ppv_after)
            assert err_after is None and safe_p_after is not None
            assert _sha256_file(safe_p_after) == proc_sha
            assert "originals" not in os.path.normpath(safe_p_after).lower().split(os.sep)

            bot_manager.fast_mode = True
            worker.fast_mode = True
            ok_manual = client.post(
                "/api/monitoring/send_manual",
                json={
                    "char_id": char_id,
                    "fan_id": "fan_guard_1",
                    "content": "Enjoy this exclusive redacted photo!",
                    "ppv_id": ppv_id,
                    "ppv_price_usd": 11.99,
                },
            )
            assert ok_manual.status_code == 200, f"Redacted manual send must succeed: {ok_manual.data}"
            ok_manual_j = ok_manual.get_json()
            assert ok_manual_j["ok"] is True
            assert ok_manual_j["queued"]["ppv_item"]["filename"] == saved_fname
            while not worker._outbox_queue.empty():
                worker._outbox_queue.get_nowait()
                worker._outbox_queue.task_done()

            _log("  -> Passed: TICKET-02 auto-lock, /api/vault/redact, ?version=original, and strict customer guards verified")
        finally:
            mbs_mod._NUDE_DETECTOR_INSTANCE = orig_nude_det
            llm_client.analyze_ppv_media_with_vision = orig_analyze_vision
            llm_client.translate_to_japanese = orig_translate_ja
            config.CHARACTERS_ROOT = orig_chars_root
            config.DB_PATH = orig_db_path
            bot_manager._workers.pop(char_id, None)

        _log("ALL TICKET-01 & TICKET-02 REDACTION TESTS PASSED SUCCESSFULLY!")
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


if __name__ == "__main__":
    import traceback

    try:
        run_mosaic_tests()
    except Exception:
        _log("MOSAIC TESTS FAILED:\n" + traceback.format_exc())
        raise
