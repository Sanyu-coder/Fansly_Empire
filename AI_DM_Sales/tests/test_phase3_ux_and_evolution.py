import glob
import os
import py_compile
import sys
import tempfile
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

LOG_FILE = os.path.join(BASE_DIR, "logs", "phase3_test_output.log")
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
    add_dm_message,
    get_fan_threads,
    get_thread_detail,
    init_db,
    mark_ppv_unlocked,
    record_ppv_offer,
    set_fan_manual_hold,
)
from app.core.llm_client import llm_client
from app.services.character_loader import load_settings, save_settings
from app.services.fansly_dm_bot import bot_manager
from app.services.optimizer_batch import extract_win_loss_threads, run_playbook_optimization
from app.services.sales_engine import sales_engine


def run_all_phase3_tests() -> None:
    if os.path.exists(LOG_FILE):
        os.remove(LOG_FILE)
    check_syntax_all_files()

    tmp_dir = tempfile.mkdtemp(prefix="ai_dm_sales_phase3_test_")
    test_db = os.path.join(tmp_dir, "test_empire_core.db")
    orig_db = config.DB_PATH
    config.DB_PATH = test_db
    init_db(test_db)

    char_id = "Sylphy-Mia"
    orig_settings = load_settings(char_id)
    orig_fast_mode = bot_manager.fast_mode
    bot_manager.fast_mode = True

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    try:
        _log("=== [TEST 1] 稼働トグル OFF 状態での監視継続（新着保存・翻訳・カルテ更新のみ実施／自動返信スキップ） ===")
        save_settings(char_id, {"bot_enabled": False})
        worker = bot_manager._get_or_create_worker(char_id)
        worker.fast_mode = True
        res_off = worker.handle_incoming_fan_message(
            fan_id="fan_off_test",
            username="OffModeFan",
            fan_message_en="Hey cutie! Do you have secret bedroom lingerie videos?",
            db_path=test_db,
            respect_bot_enabled=True,
        )
        assert res_off["ok"] is True
        assert res_off["action"] == "monitored_only_bot_off", f"Unexpected action: {res_off}"
        detail_off = get_thread_detail(char_id, "fan_off_test", db_path=test_db)
        assert len(detail_off["messages"]) == 1, "Fan message must be saved while OFF, and AI must NOT auto-reply"
        assert detail_off["messages"][0]["sender"] == "fan"
        # [オンデマンド翻訳] 受信時は content_ja が空（翻訳は表示時 API でオンデマンド生成・キャッシュ）
        assert not detail_off["messages"][0].get("content_ja"), "content_ja must be empty at save time (on-demand translation)"
        prof_off = detail_off["fan"]["psych_profile"]
        assert len(prof_off.get("preferences") or []) > 0, "Psych profile must be updated while OFF"
        assert len(detail_off["decision_logs"]) >= 1, "Decision log must record monitor-only reasoning"
        # オンデマンド生成の確認: translate_to_japanese が英語原文から日本語を返すこと
        ja_on_demand = llm_client.translate_to_japanese(detail_off["messages"][0]["content"])
        assert ja_on_demand, "On-demand translation must produce Japanese text"
        _log("  -> Passed: DM saved + content_ja empty at save (on-demand) + psych_profile updated + AI auto-reply skipped while OFF")

        _log("=== [TEST 2] 待機カウントダウン ＋ 手動切替(スキップ) ＋ 10分放置アラート ＋ AI自動返信復帰 ＆ 手動送信解除 ===")
        save_settings(char_id, {"bot_enabled": True})
        res_sched = worker.handle_incoming_fan_message(
            fan_id="fan_countdown_1",
            username="CountdownFan",
            fan_message_en="Show me your secret bedroom clip right now!",
            db_path=test_db,
            async_schedule=True,
        )
        assert res_sched["action"] == "scheduled_countdown"
        worker._pending_replies["fan_countdown_1"]["scheduled_ts"] += 45.0
        poll_r = client.get(f"/api/monitoring/poll?char_id={char_id}&active_char_id={char_id}&active_fan_id=fan_countdown_1")
        assert poll_r.status_code == 200
        poll_j = poll_r.get_json()
        assert poll_j["active_thread"]["pending_reply"]["is_waiting"] is True
        assert poll_j["active_thread"]["pending_reply"]["remaining_seconds"] > 0
        assert len(poll_j["active_thread"]["pending_reply"]["scheduled_time_hms"]) == 8

        skip_r = client.post("/api/monitoring/skip_to_manual", json={"char_id": char_id, "fan_id": "fan_countdown_1"})
        assert skip_r.status_code == 200
        skip_j = skip_r.get_json()
        assert skip_j["ok"] is True and skip_j["pending_cancelled"] is True and skip_j["manual_hold"] is True
        assert bot_manager.get_pending_replies(char_id, "fan_countdown_1") == {}

        eleven_min_ago = (datetime.now() - timedelta(minutes=11)).strftime("%Y-%m-%dT%H:%M:%S")
        set_fan_manual_hold(char_id, "fan_countdown_1", hold=True, hold_at=eleven_min_ago, db_path=test_db)
        threads_overdue = get_fan_threads(char_id, db_path=test_db)
        target_t = [t for t in threads_overdue if t["fan_id"] == "fan_countdown_1"][0]
        assert target_t["is_manual_overdue"] is True, f"Expected overdue=True, got {target_t}"

        res_resume = client.post(
            "/api/monitoring/resume_auto_reply",
            json={"char_id": char_id, "fan_id": "fan_countdown_1", "delay_seconds": 30},
        )
        assert res_resume.status_code == 200
        res_resume_j = res_resume.get_json()
        assert res_resume_j["ok"] is True and res_resume_j["manual_hold"] is False
        assert res_resume_j["pending_reply"]["is_waiting"] is True
        assert res_resume_j["thread"]["fan"]["is_manual_overdue"] is False
        set_fan_manual_hold(char_id, "fan_countdown_1", hold=True, hold_at=eleven_min_ago, db_path=test_db)
        send_r = client.post(
            "/api/monitoring/send_manual",
            json={"char_id": char_id, "fan_id": "fan_countdown_1", "content": "I'm right here babe!", "japanese_text": "ここにいるよ♡"},
        )
        assert send_r.status_code == 200
        send_j = send_r.get_json()
        assert send_j["thread"]["fan"]["manual_hold"] == 0 and send_j["thread"]["fan"]["is_manual_overdue"] is False
        _log("  -> Passed: Countdown -> Skip to manual -> 10m Overdue Alert -> Resume Auto Reply & Manual Send clear")

        _log("=== [TEST 3] カルテ根拠・検討プロセスログ (decision_log_json) ＆ optimizer_batch 連携確認 ===")
        win_res = sales_engine.process_fan_message(
            char_id=char_id,
            user_message="How much is your uncensored bedroom video? I want to unlock it right now!",
            fan_id="win_log_fan",
            current_tier="silver_vip",
            total_spent_usd=25.0,
            sales_phase="3_Lead_Pitch",
            persist_db=True,
            db_path=test_db,
        )
        assert "decision_log" in win_res
        dl = win_res["decision_log"]
        assert "profile_reasoning" in dl and "strategy_reasoning" in dl
        assert dl["strategy_reasoning"]["ab_rule_applied"]["ab_group"] in ("A", "B")
        add_dm_message(
            char_id, "win_log_fan", "ai", win_res["ai_en"], content_ja=win_res["ai_ja"],
            decision_log=dl, db_path=test_db,
        )
        w_tx = record_ppv_offer(char_id, "win_log_fan", "Sylphy-Mia_vault_001", 19.99, tx_type="ppv", db_path=test_db)
        mark_ppv_unlocked(char_id, "win_log_fan", tx_id=w_tx, db_path=test_db)

        loss_res = sales_engine.process_fan_message(
            char_id=char_id,
            user_message="Send me free pics pls, that's too expensive",
            fan_id="loss_log_fan",
            current_tier="free_follower",
            total_spent_usd=0.0,
            consecutive_ppv_ignored=2,
            sales_phase="4_Lock_Close",
            persist_db=True,
            db_path=test_db,
        )
        add_dm_message(
            char_id, "loss_log_fan", "ai", loss_res["ai_en"], content_ja=loss_res["ai_ja"],
            decision_log=loss_res["decision_log"], db_path=test_db,
        )

        wl = extract_win_loss_threads(char_id, db_path=test_db)
        assert len(wl["winning_threads"]) >= 1 and len(wl["winning_threads"][0]["decision_logs"]) >= 1
        opt_res = run_playbook_optimization(char_id=char_id, db_path=test_db)
        assert opt_res["ok"] is True
        assert opt_res["decision_logs_analyzed_count"] >= 2, f"Expected >=2 decision logs analyzed, got {opt_res}"
        _log(f"  -> Passed: decision_log saved & {opt_res['decision_logs_analyzed_count']} logs analyzed in optimizer_batch")

        _log("=== [TEST 4] PPVメディア配信 (HTTP 200 / Range 206)、3段階価格連動、/api/system/manual、HTML要素検証 ===")
        list_r = client.get("/api/vault/list?char_id=queen_lilith")
        assert list_r.status_code == 200
        list_j = list_r.get_json()
        assert "default_prices_by_char" in list_j
        real_vid = [it for it in list_j["items"] if it.get("file_exists")]
        assert len(real_vid) >= 1, f"Expected existing video in queen_lilith vault: {list_j}"
        vid_name = real_vid[0]["filename"]

        m_r200 = client.get(f"/api/vault/media/queen_lilith/{vid_name}")
        assert m_r200.status_code == 200 and m_r200.mimetype == "video/mp4"
        m_r200.close()

        m_r206 = client.get(f"/api/vault/media/queen_lilith/{vid_name}", headers={"Range": "bytes=0-1023"})
        assert m_r206.status_code == 206 and "bytes 0-1023/" in m_r206.headers.get("Content-Range", "")
        assert len(m_r206.data) == 1024

        for step_num, expected_base in [(1, 9.99), (2, 19.99), (3, 39.99)]:
            d_r = client.post("/api/vault/preview_draft", json={"char_id": char_id, "ladder_step": step_num})
            assert d_r.status_code == 200
            assert float(d_r.get_json()["base_price_usd"]) == expected_base

        man_r = client.get("/api/system/manual")
        assert man_r.status_code == 200
        man_j = man_r.get_json()
        assert man_j["ok"] is True and "AI_DM_Sales" in man_j["content"] and len(man_j["content"]) > 500

        idx_r = client.get("/")
        html_txt = idx_r.get_data(as_text=True)
        for required_token in [
            "▶ 全キャラ一括稼働",
            "⏹ 全停止 (稼働中)",
            "⏭️ スキップして手動返信へ",
            "🤖 やっぱりAI自動返信に戻す",
            "🧠 AIカルテ根拠・返答検討ログを見る",
            "📖 運用マニュアルを開く",
            "object-fit: contain",
            "#t=0.1",
            "openMediaLightbox",
        ]:
            assert required_token in html_txt, f"Missing UI token in index.html: {required_token}"

        wc_r = client.post("/api/system/window_closed", json={"immediate": True, "exit_process": False})
        assert wc_r.status_code == 200 and wc_r.get_json()["ok"] is True
        _log("  -> Passed: Media HTTP 200 & 206 Range, 3-step price sync, /api/system/manual, UI tokens, window_closed")

        _log("ALL PHASE 3 UX & EVOLUTION TESTS PASSED SUCCESSFULLY!")
    finally:
        save_settings(char_id, orig_settings)
        bot_manager.fast_mode = orig_fast_mode
        config.DB_PATH = orig_db


if __name__ == "__main__":
    import traceback

    try:
        run_all_phase3_tests()
    except Exception:
        _log("TEST FAILED WITH EXCEPTION:\n" + traceback.format_exc())
        raise
