import glob
import io
import os
import py_compile
import sys
import tempfile

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

LOG_FILE = os.path.join(BASE_DIR, "logs", "phase4_test_output.log")
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)


def _log(msg: str) -> None:
    print(msg, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as fp:
        fp.write(msg + "\n")


def check_syntax_all_files() -> None:
    targets = [os.path.join(BASE_DIR, "run_dm_sales.py")]
    targets.extend(glob.glob(os.path.join(BASE_DIR, "app", "**", "*.py"), recursive=True))
    targets.extend(glob.glob(os.path.join(BASE_DIR, "tests", "*.py")))
    for path in sorted(set(targets)):
        py_compile.compile(path, doraise=True)
    _log(f"=== [SYNTAX CHECK] py_compile passed for all {len(set(targets))} Python files ===")


from app import create_app
from app.core import config
from app.core.database import (
    get_ppv_vault_records_db,
    get_system_reply_delay_settings,
    get_thread_detail,
    init_db,
    mark_ppv_unlocked,
    save_system_reply_delay_settings,
)
from app.core.llm_client import llm_client
from app.services.character_loader import (
    get_char_paths,
    list_character_ids,
    load_settings,
    load_vault_items,
    save_settings,
    save_vault_items,
)
from app.services.fansly_dm_bot import bot_manager


def _make_sample_jpeg_bytes() -> bytes:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (512, 512), color=(38, 18, 52))
    draw = ImageDraw.Draw(img)
    draw.rectangle([40, 60, 472, 452], fill=(82, 34, 98), outline=(236, 72, 153), width=5)
    draw.ellipse([160, 120, 352, 312], fill=(245, 208, 224))
    draw.text((130, 360), "Bedroom Lingerie Selfie", fill=(255, 240, 250))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return buf.getvalue()


def run_all_phase4_tests() -> None:
    if os.path.exists(LOG_FILE):
        os.remove(LOG_FILE)
    check_syntax_all_files()

    tmp_dir = tempfile.mkdtemp(prefix="ai_dm_sales_phase4_test_")
    test_db = os.path.join(tmp_dir, "test_empire_core.db")
    orig_db = config.DB_PATH
    config.DB_PATH = test_db
    init_db(test_db)

    char_id = "queen_lilith"
    all_chars_orig = {cid: load_settings(cid) for cid in list_character_ids()}
    orig_vault_items = load_vault_items(char_id)
    orig_fast_mode = bot_manager.fast_mode
    created_files = []

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    try:
        _log("=== [TEST 1] ローカルOllama Visionモデル自動検出 ＆ 画像/動画からの日本語タグ・売り込みメモ自動生成 ===")
        detected_vision = llm_client.detect_or_ensure_vision_model(auto_pull=True)
        _log(f"  -> Detected local Vision model: {detected_vision}")
        assert detected_vision is not None, "Vision model must be detected or pulled on Ollama"

        jpg_bytes = _make_sample_jpeg_bytes()
        up_res = client.post(
            "/api/vault/upload",
            data={
                "char_id": char_id,
                "ladder_step": "1",
                "file": (io.BytesIO(jpg_bytes), "test_black_lace_selfie.jpg"),
            },
            content_type="multipart/form-data",
        )
        assert up_res.status_code == 200, f"Upload failed: {up_res.data}"
        up_json = up_res.get_json()
        assert up_json["ok"] is True
        new_item = up_json["item"]
        created_path = os.path.join(get_char_paths(char_id)["vault_dir"], new_item["filename"])
        created_files.append(created_path)

        assert len(new_item.get("tags") or []) >= 1, f"Tags must be generated: {new_item}"
        assert bool(new_item.get("description_memo")), f"description_memo must be generated: {new_item}"
        db_vault = get_ppv_vault_records_db(char_id, db_path=test_db)
        assert new_item["ppv_id"] in db_vault, "Uploaded PPV must be synced to ppv_vault SQLite table"
        assert bool(db_vault[new_item["ppv_id"]]["description_memo"]), "DB description_memo must be non-empty"
        _log(
            f"  -> Image Vision result (model={up_json.get('vision_model')}, used_vision={up_json.get('used_vision')}): "
            f"tags={new_item['tags']} | memo={new_item['description_memo'][:70]}..."
        )
        existing_video_item = next(
            (it for it in orig_vault_items if str(it.get("filename", "")).lower().endswith(".mp4")),
            None,
        )
        assert existing_video_item is not None, "Expected an existing mp4 item in queen_lilith vault"
        vid_full = os.path.join(get_char_paths(char_id)["vault_dir"], existing_video_item["filename"])
        frame_b64 = llm_client.extract_media_frame_base64(vid_full, media_type="video")
        assert frame_b64 and len(frame_b64) > 500, "Video representative frame extraction must succeed"

        vis_vid_res = client.post(
            "/api/vault/analyze_vision",
            json={
                "char_id": char_id,
                "ppv_id": existing_video_item["ppv_id"],
                "ladder_step": existing_video_item.get("ladder_step", 1),
                "save_to_vault": True,
            },
        )
        assert vis_vid_res.status_code == 200
        vis_vid_json = vis_vid_res.get_json()
        assert vis_vid_json["ok"] is True
        assert len(vis_vid_json.get("tags") or []) >= 1
        assert bool(vis_vid_json.get("description_memo"))
        _log(
            f"  -> Video Vision result (model={vis_vid_json.get('vision_model')}, used_vision={vis_vid_json.get('used_vision')}): "
            f"tags={vis_vid_json['tags']} | memo={vis_vid_json['description_memo'][:70]}..."
        )

        _log("=== [TEST 2] 営業シミュレーターでの decision_log 出力 ＆ PPV添付メディア返却 ===")
        sim_res = client.post(
            "/api/simulator/reply",
            json={
                "char_id": char_id,
                "message": "秘密のベッドルーム動画はある？今すぐ見たい！",
                "auto_translate_ja": True,
                "current_tier": "silver_vip",
                "total_spent_usd": 15.0,
                "sales_phase": "2_Discovery",
                "ab_group": "A",
            },
        )
        assert sim_res.status_code == 200
        sim_json = sim_res.get_json()
        assert sim_json["ok"] is True
        assert isinstance(sim_json.get("decision_log"), dict), f"Simulator must return decision_log: {sim_json}"
        assert "profile_reasoning" in sim_json["decision_log"]
        assert "strategy_reasoning" in sim_json["decision_log"]
        assert sim_json["sales_phase"] in ("3_Lead_Pitch", "4_Lock_Close")
        assert isinstance(sim_json.get("sent_ppv_attachment"), dict), "Simulator must attach PPV preview on pitch phase"
        assert sim_json["sent_ppv_attachment"]["unlock_status_ja"] == "未購入"
        _log(f"  -> Passed: Simulator returned decision_log & sent_ppv_attachment ({sim_json['sent_ppv_attachment']['filename']})")

        _log("=== [TEST 3] トーク吹き出しへのPPVメディア紐付け・取得（AI自動送信 ＆ 手動介入送信 ＆ 開封ステータス更新） ===")
        save_settings(char_id, {"bot_enabled": True})
        worker = bot_manager._get_or_create_worker(char_id)
        worker.fast_mode = True
        bot_manager.fast_mode = True

        r1 = worker.handle_incoming_fan_message(
            fan_id="ppv_bubble_fan",
            username="PpvBubbleFan",
            fan_message_en="Do you have a secret video I can unlock right now?",
            raw_tier_badge="Silver VIP",
            db_path=test_db,
            respect_bot_enabled=True,
        )
        assert r1["ok"] is True and r1["action"] == "replied"
        assert r1.get("selected_ppv") is not None

        th_detail = get_thread_detail(char_id, "ppv_bubble_fan", db_path=test_db)
        ai_msgs = [m for m in th_detail["messages"] if m["sender"] == "ai"]
        assert len(ai_msgs) >= 1 and isinstance(ai_msgs[-1].get("ppv_attachment"), dict), f"Missing ppv_attachment: {ai_msgs}"
        att_ai = ai_msgs[-1]["ppv_attachment"]
        assert att_ai["is_unlocked"] == 0 and att_ai["unlock_status_ja"] == "未購入"
        assert bool(att_ai["filename"]) and bool(att_ai["step_label"]) and att_ai["offered_price_usd"] > 0

        mark_ppv_unlocked(char_id, "ppv_bubble_fan", ppv_id=att_ai["ppv_id"], tx_id=att_ai["tx_id"], db_path=test_db)
        th_after_unlock = get_thread_detail(char_id, "ppv_bubble_fan", db_path=test_db)
        att_unlocked = [m for m in th_after_unlock["messages"] if m["sender"] == "ai"][-1]["ppv_attachment"]
        assert att_unlocked["is_unlocked"] == 1 and att_unlocked["unlock_status_ja"] == "購入済"
        man_res = client.post(
            "/api/monitoring/send_manual",
            json={
                "char_id": char_id,
                "fan_id": "ppv_bubble_fan",
                "content": "Here is an extra private photo just for you 🖤",
                "japanese_text": "あなただけに特別な写真も送るね🖤",
                "ppv_id": new_item["ppv_id"],
                "ppv_price_usd": 12.50,
            },
        )
        assert man_res.status_code == 200
        man_json = man_res.get_json()
        assert man_json["ok"] is True
        manual_msgs = [m for m in man_json["thread"]["messages"] if m["sender"] == "human_manual"]
        assert len(manual_msgs) >= 1 and manual_msgs[-1]["ppv_attachment"]["ppv_id"] == new_item["ppv_id"]
        assert float(manual_msgs[-1]["ppv_attachment"]["offered_price_usd"]) == 12.50
        _log("  -> Passed: AI & Manual PPV bubble attachment + unlock status reflection verified")

        _log("=== [TEST 4] 返信待機時間範囲 (min_reply_delay_sec 〜 max_reply_delay_sec) の保存とボット待機秒数への反映 ===")
        save_r = client.post(
            "/api/settings/save",
            json={
                "char_id": char_id,
                "apply_all": True,
                "min_reply_delay_sec": 22,
                "max_reply_delay_sec": 36,
            },
        )
        assert save_r.status_code == 200 and save_r.get_json()["ok"] is True

        get_r = client.get(f"/api/settings/get?char_id={char_id}")
        get_j = get_r.get_json()
        assert get_j["settings"]["min_reply_delay_sec"] == 22
        assert get_j["settings"]["max_reply_delay_sec"] == 36
        sys_d = get_system_reply_delay_settings(db_path=test_db)
        assert sys_d["min_reply_delay_sec"] == 22 and sys_d["max_reply_delay_sec"] == 36

        worker.fast_mode = False
        bot_manager.fast_mode = False
        sched_res = worker.handle_incoming_fan_message(
            fan_id="delay_range_fan",
            username="DelayRangeFan",
            fan_message_en="Hey gorgeous, what are you doing tonight? I was thinking about you all day and wanted to chat with you!",
            db_path=test_db,
            respect_bot_enabled=True,
            async_schedule=True,
        )
        assert sched_res["ok"] is True and sched_res["action"] == "scheduled_countdown"
        pending_info = sched_res["pending_reply"]
        total_delay = float(pending_info["total_delay_sec"])
        assert 22.0 <= total_delay <= 36.0, f"Expected delay within [22, 36]s, got {total_delay}s"
        worker.cancel_pending_reply("delay_range_fan")
        _log(f"  -> Passed: Configured delay [22s..36s] reflected in bot countdown ({total_delay}s)")

        _log("=== [TEST 5] index.html 全6項目のUI要素・CSS・JS関数トークン検証 ===")
        html_txt = client.get("/").get_data(as_text=True)
        for token in [
            "✨ AIでタグ・メモを自動生成",
            "simDecisionLogAccordion",
            "ppvHoverFloatingCard",
            "showPpvHoverTooltip",
            "renderMessagePpvAttachmentHtml",
            "jumpToVaultItem",
            "ppv-jump-link",
            "highlight-jump",
            "resize: vertical",
            "overflow-y: auto",
            "max-height: 360px",
            "⏱️ AI返信待機時間",
            "setMinReplyDelaySec",
            "setMaxReplyDelaySec",
        ]:
            assert token in html_txt, f"Missing UI token in index.html: {token}"
        _log("  -> Passed: All UI tokens verified in index.html")

        _log("ALL PHASE 4 (6 ITEMS) TESTS PASSED SUCCESSFULLY!")
    finally:
        for p in created_files:
            if os.path.isfile(p):
                try:
                    os.remove(p)
                except Exception:
                    pass
        config.DB_PATH = orig_db
        init_db(orig_db)
        save_vault_items(char_id, orig_vault_items)
        for cid, orig_s in all_chars_orig.items():
            save_settings(cid, orig_s)
        first_orig = next(iter(all_chars_orig.values()), {"min_reply_delay_sec": 60, "max_reply_delay_sec": 300})
        save_system_reply_delay_settings(
            first_orig.get("min_reply_delay_sec", 60),
            first_orig.get("max_reply_delay_sec", 300),
            db_path=orig_db,
        )
        bot_manager.fast_mode = orig_fast_mode


if __name__ == "__main__":
    import traceback

    try:
        run_all_phase4_tests()
    except Exception:
        _log("PHASE 4 TEST FAILED:\n" + traceback.format_exc())
        raise
