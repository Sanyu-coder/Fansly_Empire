import io
import json
import os
import sys
import tempfile

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from app import create_app
from app.core import config
from app.core.database import init_db
from app.services.character_loader import load_vault_items, save_vault_items, get_char_paths

# ── helpers ──────────────────────────────────────────────────────────────
def _log(msg):
    print(msg, flush=True)


def _make_test_jpeg_bytes():
    """Create a minimal valid JPEG (1x1 pixel) for media-path resolution."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (512, 512), color=(38, 18, 52))
    draw = ImageDraw.Draw(img)
    draw.rectangle([40, 60, 472, 452], fill=(82, 34, 98))
    draw.ellipse([160, 120, 352, 312], fill=(245, 208, 224))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return buf.getvalue()


def run_mosaic_tests():
    tmp_dir = tempfile.mkdtemp(prefix="mosaic_test_")
    test_db = os.path.join(tmp_dir, "test_mosaic.db")
    orig_db = config.DB_PATH
    config.DB_PATH = test_db
    init_db(test_db)

    char_id = "queen_lilith"
    orig_items = load_vault_items(char_id)
    vault_dir = get_char_paths(char_id)["vault_dir"]

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    # ── insert a test vault item ────────────────────────────────────────
    test_jpg = _make_test_jpeg_bytes()
    test_filename = "mosaic_test_001.jpg"
    test_ppv_id = f"{char_id}_vault_mosaic_test_001"
    test_filepath = os.path.join(vault_dir, test_filename)
    with open(test_filepath, "wb") as f:
        f.write(test_jpg)

    test_item = {
        "ppv_id": test_ppv_id,
        "filename": test_filename,
        "blur_preview_filename": "",
        "media_type": "image",
        "ladder_step": 1,
        "base_price_usd": 9.99,
        "tripwire_price_usd": 5.99,
        "whale_price_usd": 19.99,
        "tags": ["selfie", "lingerie"],
        "tags_json": json.dumps(["selfie", "lingerie"]),
        "description_memo": "Test image for mosaic",
        "pitch_note": "Test pitch",
        "pitch_hint": "",
        "is_active": True,
        "is_public": False,
        "processed_filename": "",
        "created_at": "2025-01-01T00:00:00",
    }
    orig_items.append(test_item)
    save_vault_items(char_id, orig_items)

    try:
        _log("=== [TEST 1] process_media missing char_id/ppv_id -> 400 ===")
        r1 = client.post("/api/vault/process_media", json={"process_type": "mosaic"})
        j1 = r1.get_json()
        assert r1.status_code == 400
        assert j1["ok"] is False
        _log(f"  -> Passed: {r1.status_code} / {j1}")

        _log("=== [TEST 2] process_media unknown ppv_id -> 404 ===")
        r2 = client.post("/api/vault/process_media", json={
            "char_id": char_id,
            "ppv_id": "nonexistent_ppv_id_999",
            "process_type": "mosaic",
        })
        j2 = r2.get_json()
        assert r2.status_code == 404
        assert j2["ok"] is False
        _log(f"  -> Passed: {r2.status_code} / {j2}")

        _log("=== [TEST 3] process_media invalid process_type -> 400 ===")
        r3 = client.post("/api/vault/process_media", json={
            "char_id": char_id,
            "ppv_id": test_ppv_id,
            "process_type": "shrinkwrap",
        })
        j3 = r3.get_json()
        assert r3.status_code == 400
        assert j3["ok"] is False
        _log(f"  -> Passed: {r3.status_code} / {j3}")

        _log("=== [TEST 4] process_media blur type with auto_detect=True ===")
        r4 = client.post("/api/vault/process_media", json={
            "char_id": char_id,
            "ppv_id": test_ppv_id,
            "process_type": "blur",
            "auto_detect": True,
        })
        j4 = r4.get_json()
        assert r4.status_code == 200
        assert j4["ok"] is True
        _log(f"  -> Passed: blur auto_detect OK / detected={j4.get('detected')} / processed={j4.get('processed_filename')}")

        _log("=== [TEST 5] process_media mosaic type with manual_rect ===")
        r5 = client.post("/api/vault/process_media", json={
            "char_id": char_id,
            "ppv_id": test_ppv_id,
            "process_type": "mosaic",
            "auto_detect": False,
            "manual_rect": {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.5},
        })
        j5 = r5.get_json()
        assert r5.status_code == 200
        assert j5["ok"] is True
        _log(f"  -> Passed: mosaic manual_rect OK / detected={j5.get('detected')} / processed={j5.get('processed_filename')}")

        _log("=== [TEST 6] process_media invalid manual_rect (non-numeric) ===")
        r6 = client.post("/api/vault/process_media", json={
            "char_id": char_id,
            "ppv_id": test_ppv_id,
            "process_type": "mosaic",
            "auto_detect": False,
            "manual_rect": {"x": "NaN", "y": "NaN", "w": "NaN", "h": "NaN"},
        })
        j6 = r6.get_json()
        assert r6.status_code == 200
        assert j6["ok"] is True
        _log(f"  -> Passed: invalid rect handled gracefully / detected={j6.get('detected')}")

        _log("=== [TEST 7] process_media is_public updated after processing ===")
        refreshed_items = load_vault_items(char_id)
        updated_item = next((it for it in refreshed_items if it["ppv_id"] == test_ppv_id), None)
        assert updated_item is not None
        assert updated_item.get("is_public") is True, f"is_public should be True after processing, got {updated_item.get('is_public')}"
        _log(f"  -> Passed: is_public=True confirmed / processed_filename={updated_item.get('processed_filename')}")

        _log("=== [TEST 8] index.html mosaic modal tokens present ===")
        html_txt = client.get("/").get_data(as_text=True)
        for token in ["mosaicModal", "mosaicCanvas", "mosaicDrawHint", "mosaicStatusBadge", "mosaicProcessBtn"]:
            assert token in html_txt, f"Missing mosaic token in index.html: {token}"
        _log("  -> Passed: All mosaic UI tokens verified in index.html")

        _log("=== [TEST 9] index.html JS functions exist in source ===")
        for fn in ["openMosaicModal", "loadMosaicPreview", "setupCanvasOverlay", "executeMosaicProcess"]:
            assert fn in html_txt, f"Missing JS function in index.html: {fn}"
        _log("  -> Passed: All mosaic JS functions verified in index.html")

        _log("ALL MOSAIC TESTS PASSED SUCCESSFULLY!")

    finally:
        # cleanup
        if os.path.isfile(test_filepath):
            os.remove(test_filepath)
        config.DB_PATH = orig_db
        init_db(orig_db)
        save_vault_items(char_id, orig_items)


if __name__ == "__main__":
    import traceback
    try:
        run_mosaic_tests()
    except Exception:
        _log("MOSAIC TESTS FAILED:\n" + traceback.format_exc())
        raise
