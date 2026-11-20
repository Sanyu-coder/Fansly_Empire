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

LOG_FILE = os.path.join(BASE_DIR, "logs", "phase5_test_output.log")
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
    get_fan_ppv_transactions,
    get_fan_record,
    get_system_reply_delay_settings,
    get_thread_detail,
    has_eligible_locked_ppv_for_unlock,
    init_db,
    mark_ppv_unlocked,
    record_ppv_offer,
    repair_false_positive_transactions,
    save_system_reply_delay_settings,
    upsert_fan_sales_state,
)
from app.services.character_loader import list_character_ids, load_settings, load_vault_items, save_settings
from app.services.fansly_dm_bot import _compute_human_reply_delay_sec, bot_manager
from app.services.sales_engine import sales_engine


class _MockEl:
    def __init__(self, text: str = ""):
        self._text = text

    def inner_text(self) -> str:
        return self._text


class _MockLockedDomPage:
    """未購入PPVバブル（.purchased-avatar 要素はあるが .not-purchased / .fa-lock も存在するDOM）を模倣"""

    def __init__(self, has_lock_marker: bool = True):
        self.has_lock_marker = has_lock_marker

    def query_selector(self, sel: str):
        if "purchased-avatar" in sel:
            return _MockEl("")
        if self.has_lock_marker and sel in (".not-purchased", ".fa-lock"):
            return _MockEl("Locked $14.99")
        return None

    def query_selector_all(self, sel: str):
        if "message-text" in sel and "not(.my-message)" in sel:
            return [_MockEl("Hey there!")]
        return []


def run_all_phase5_tests() -> None:
    if os.path.exists(LOG_FILE):
        os.remove(LOG_FILE)
    check_syntax_all_files()

    tmp_dir = tempfile.mkdtemp(prefix="ai_dm_sales_phase5_test_")
    test_db = os.path.join(tmp_dir, "test_empire_core.db")
    orig_db = config.DB_PATH
    config.DB_PATH = test_db
    init_db(test_db)

    char_id = "queen_lilith"
    all_chars_orig = {cid: load_settings(cid) for cid in list_character_ids()}
    orig_fast_mode = bot_manager.fast_mode

    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    try:
        _log("=== [TEST 1] S.E.L.L. 厳格ガード（1〜2往復目のPPV押し売りブロック・3往復目解禁・5_Cooldown催促禁止・絵文字多様化） ===")
        # 1往復目（新規未課金ファン・高関心ワード入りでも必ず 1_Icebreak になりPPV推薦ブロック）
        t1 = sales_engine.process_fan_message(
            char_id=char_id,
            user_message="Hey! You look so cute, do you have secret bedroom videos?",
            fan_id="newbie_fan",
            current_tier="free_follower",
            total_spent_usd=0.0,
            sales_phase="1_Icebreak",
            recent_messages=[],
            persist_db=True,
            db_path=test_db,
        )
        assert t1["sales_phase"] == "1_Icebreak", f"Turn 1 must stay 1_Icebreak, got {t1['sales_phase']}"
        assert t1["recommended_ppv"] is None, "Turn 1 must NOT pitch PPV"

        # 2往復目（新規未課金ファン・まだ3往復未満なので必ず 2_Discovery になりPPV推薦ブロック）
        hist_t2 = [
            {"sender": "fan", "content": "Hey! You look so cute, do you have secret bedroom videos?"},
            {"sender": "ai", "content": t1["ai_en"]},
        ]
        t2 = sales_engine.process_fan_message(
            char_id=char_id,
            user_message="I love black lace lingerie! How much is a clip?",
            fan_id="newbie_fan",
            current_tier="free_follower",
            total_spent_usd=0.0,
            sales_phase=t1["sales_phase"],
            recent_messages=hist_t2,
            persist_db=True,
            db_path=test_db,
        )
        assert t2["sales_phase"] == "2_Discovery", f"Turn 2 must be 2_Discovery, got {t2['sales_phase']}"
        assert t2["recommended_ppv"] is None, "Turn 2 must NOT jump to PPV pitch"

        # 3往復目（関係構築完了：ここで初めて 3_Lead_Pitch が解禁される）
        hist_t3 = hist_t2 + [
            {"sender": "fan", "content": "I love black lace lingerie! How much is a clip?"},
            {"sender": "ai", "content": t2["ai_en"]},
        ]
        t3 = sales_engine.process_fan_message(
            char_id=char_id,
            user_message="Yes please show me your secret clip right now!",
            fan_id="newbie_fan",
            current_tier="free_follower",
            total_spent_usd=0.0,
            sales_phase=t2["sales_phase"],
            recent_messages=hist_t3,
            persist_db=True,
            db_path=test_db,
        )
        assert t3["sales_phase"] == "3_Lead_Pitch", f"Turn 3 should unlock 3_Lead_Pitch, got {t3['sales_phase']}"
        assert t3["recommended_ppv"] is not None, "Turn 3 should recommend PPV"
        assert "Available Character Emoji Palette:" in t3["system_prompt_preview"]

        # 5_Cooldown で急かし（催促）文言が出ないこと
        cd_res = sales_engine.process_fan_message(
            char_id=char_id,
            user_message="Hehe you are really fun to talk to",
            fan_id="cooldown_fan",
            current_tier="silver_vip",
            total_spent_usd=15.0,
            sales_phase="4_Lock_Close",
            recent_messages=hist_t3,
            persist_db=True,
            db_path=test_db,
        )
        assert cd_res["sales_phase"] == "5_Cooldown"
        assert not sales_engine.NAGGING_COOLDOWN_REGEX.search(cd_res["ai_en"]), f"Nagging detected: {cd_res['ai_en']}"
        sanitized = sales_engine.sanitize_cooldown_reply("Did you unlock it yet? Hurry up babe!", "babe", 1)
        assert "unlock" not in sanitized.lower() and "hurry" not in sanitized.lower()
        _log("  -> Passed: S.E.L.L. strict turn guard (Turn1=1_Icebreak, Turn2=2_Discovery, Turn3=3_Lead_Pitch) & 5_Cooldown anti-nagging verified")

        _log("=== [TEST 2] 受信メッセージ文字数に応じた待機秒数の比例算出（短文10文字 / 中文85文字 / 長文200文字） ===")
        save_r = client.post(
            "/api/settings/save",
            json={
                "char_id": char_id,
                "apply_all": True,
                "min_reply_delay_sec": 60,
                "max_reply_delay_sec": 300,
                "short_msg_chars_threshold": 20,
                "long_msg_chars_threshold": 150,
            },
        )
        assert save_r.status_code == 200 and save_r.get_json()["ok"] is True
        delay_cfg = get_system_reply_delay_settings(db_path=test_db)
        assert delay_cfg["short_msg_chars_threshold"] == 20 and delay_cfg["long_msg_chars_threshold"] == 150

        short_msg = "A" * 10   # 10文字 <= 20 -> ratio 0.0 -> base 60s
        mid_msg = "B" * 85     # 85文字 -> (85-20)/(150-20) = 0.5 -> base 180s
        long_msg = "C" * 200   # 200文字 >= 150 -> ratio 1.0 -> base 300s

        c_short = _compute_human_reply_delay_sec(short_msg, delay_cfg, fast_mode=False)
        c_mid = _compute_human_reply_delay_sec(mid_msg, delay_cfg, fast_mode=False)
        c_long = _compute_human_reply_delay_sec(long_msg, delay_cfg, fast_mode=False)

        assert c_short["length_ratio"] == 0.0 and 60.0 <= c_short["final_delay_sec"] <= 69.5, f"Short mismatch: {c_short}"
        assert c_mid["length_ratio"] == 0.5 and 150.0 <= c_mid["final_delay_sec"] <= 210.0, f"Mid mismatch: {c_mid}"
        assert c_long["length_ratio"] == 1.0 and 254.0 <= c_long["final_delay_sec"] <= 300.0, f"Long mismatch: {c_long}"
        assert c_short["final_delay_sec"] < c_mid["final_delay_sec"] < c_long["final_delay_sec"]
        _log(
            f"  -> Passed: Proportional delays verified | 10chars={c_short['final_delay_sec']}s, "
            f"85chars={c_mid['final_delay_sec']}s, 200chars={c_long['final_delay_sec']}s"
        )
        _log("=== [TEST 3] PPV未購入状態の誤判定防止 ＆ 返信文にチップ要求がない場合の tip 非記録検証 ===")
        worker = bot_manager._get_or_create_worker(char_id)
        worker.fast_mode = True
        bot_manager.fast_mode = True

        upsert_fan_sales_state(char_id, "locked_dom_fan", username="LockedFan", db_path=test_db)
        old_offer_ts = (datetime.now() - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S")
        tx_locked = record_ppv_offer(
            char_id, "locked_dom_fan", "queen_lilith_vault_003", 14.99,
            tx_type="ppv", offered_at=old_offer_ts, is_unlocked=0, db_path=test_db,
        )
        conv_locked = worker._extract_conversation_state(_MockLockedDomPage(has_lock_marker=True))
        assert conv_locked["ppv_unlocked"] is False, f"Must NOT detect unlocked while lock marker exists: {conv_locked}"

        save_settings(char_id, {"bot_enabled": False})
        worker.handle_incoming_fan_message(
            fan_id="locked_dom_fan",
            username="LockedFan",
            fan_message_en="Hey there!",
            detected_ppv_unlocked=conv_locked["ppv_unlocked"],
            db_path=test_db,
        )
        tx_list_locked = get_fan_ppv_transactions(char_id, "locked_dom_fan", db_path=test_db)
        assert tx_list_locked[0]["tx_id"] == tx_locked and int(tx_list_locked[0]["is_unlocked"]) == 0

        just_sent_ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        record_ppv_offer(char_id, "fresh_offer_fan", "queen_lilith_vault_003", 14.99, offered_at=just_sent_ts, db_path=test_db)
        assert has_eligible_locked_ppv_for_unlock(char_id, "fresh_offer_fan", min_elapsed_sec=15.0, db_path=test_db) is False

        class _NoTipStubLLM:
            def generate_text(self, **kwargs):
                return "Aww thank you so much darling, you always make me smile 💜👑"

            def translate_to_japanese(self, text):
                return "ありがとうダーリン、いつも笑顔にしてくれるね💜👑"

        orig_engine_llm = sales_engine.llm
        sales_engine.llm = _NoTipStubLLM()
        try:
            no_tip_res = sales_engine.process_fan_message(
                char_id=char_id,
                user_message="You're a gorgeous goddess! Love your style!",
                fan_id="no_tip_fan",
                current_tier="silver_vip",
                total_spent_usd=25.0,
                sales_phase="2_Discovery",
                persist_db=True,
                record_tip_in_db=True,
                db_path=test_db,
            )
        finally:
            sales_engine.llm = orig_engine_llm

        assert no_tip_res["tip_hook"]["triggered"] is False, "tip_hook must be False when reply has no tip request"
        no_tip_txs = get_fan_ppv_transactions(char_id, "no_tip_fan", db_path=test_db)
        assert len([t for t in no_tip_txs if t["tx_type"] == "tip"]) == 0, f"Unexpected tip tx recorded: {no_tip_txs}"

        upsert_fan_sales_state(char_id, "false_pos_fan", total_spent_usd=14.99, db_path=test_db)
        fp_tx = record_ppv_offer(char_id, "false_pos_fan", "ppv_fp", 14.99, offered_at="2026-11-18T22:39:55", db_path=test_db)
        mark_ppv_unlocked(char_id, "false_pos_fan", tx_id=fp_tx, unlocked_at="2026-11-18T22:39:57", db_path=test_db)
        rep = repair_false_positive_transactions(db_path=test_db)
        assert rep["repaired_ppv_count"] >= 1
        assert float(get_fan_record(char_id, "false_pos_fan", db_path=test_db)["total_spent_usd"]) == 0.0
        _log("  -> Passed: Locked DOM stays is_unlocked=0, non-tip reply creates 0 tip tx, and false-positive DB repair verified")
        _log("=== [TEST 4] 手動PPV送信（ダイナミックプライシング候補取得 ＋ カスタム価格指定送信キュー投入＆DB記録） ===")
        upsert_fan_sales_state(
            char_id, "whale_manual_fan", username="WhaleBuyer",
            current_tier="gold_goddess", total_spent_usd=80.0,
            psych_profile_dict={"budget_tier": "太客VIP層"},
            db_path=test_db,
        )
        cand_r = client.get(f"/api/monitoring/ppv_candidates?char_id={char_id}&fan_id=whale_manual_fan")
        assert cand_r.status_code == 200
        cand_j = cand_r.get_json()
        assert cand_j["ok"] is True and cand_j["recommended_pricing_mode"] == "whale"
        assert len(cand_j["items"]) >= 1
        target_ppv = cand_j["items"][0]
        assert float(target_ppv["recommended_price_usd"]) == float(target_ppv["whale_price_usd"])

        while not worker._outbox_queue.empty():
            worker._outbox_queue.get_nowait()
            worker._outbox_queue.task_done()

        send_m_res = client.post(
            "/api/monitoring/send_manual",
            json={
                "char_id": char_id,
                "fan_id": "whale_manual_fan",
                "japanese_text": "あなたのために特別な動画を用意したよ💜",
                "ppv_id": target_ppv["ppv_id"],
                "ppv_price_usd": 24.50,
            },
        )
        assert send_m_res.status_code == 200
        send_m_j = send_m_res.get_json()
        assert send_m_j["ok"] is True
        assert send_m_j["queued"]["ppv_item"]["ppv_id"] == target_ppv["ppv_id"]
        assert float(send_m_j["queued"]["offered_price"]) == 24.50
        assert worker._outbox_queue.qsize() >= 1

        th_m = get_thread_detail(char_id, "whale_manual_fan", db_path=test_db)
        last_m = th_m["messages"][-1]
        assert last_m["sender"] == "human_manual"
        assert last_m["ppv_attachment"]["ppv_id"] == target_ppv["ppv_id"]
        assert float(last_m["ppv_attachment"]["offered_price_usd"]) == 24.50
        _log("  -> Passed: Dynamic pricing candidate (whale) + manual PPV send queue & DB record ($24.50) verified")

        _log("=== [TEST 5] PPV武器庫2カラム黒帯ゼロCSS・レスポンシブ・UI要素検証 ===")
        html_txt = client.get("/").get_data(as_text=True)
        for token in [
            "minmax(560px, 1fr)",
            "196px minmax(0, 1fr)",
            "object-fit: cover",
            "@media (max-width: 680px)",
            "manualPpvPresetSelect",
            "manualPpvMiniThumb",
            "setShortMsgCharsThreshold",
            "setLongMsgCharsThreshold",
        ]:
            assert token in html_txt, f"Missing UI/CSS token: {token}"
        _log("  -> Passed: Compact 2-column zero-black-bar Vault CSS & responsive media queries verified")

        _log("ALL PHASE 5 (6 ITEMS) INTEGRATION TESTS PASSED SUCCESSFULLY!")
    finally:
        while not worker._outbox_queue.empty():
            worker._outbox_queue.get_nowait()
            worker._outbox_queue.task_done()
        config.DB_PATH = orig_db
        init_db(orig_db)
        for cid, orig_s in all_chars_orig.items():
            save_settings(cid, orig_s)
        first_orig = next(iter(all_chars_orig.values()), {})
        save_system_reply_delay_settings(
            first_orig.get("min_reply_delay_sec", 60),
            first_orig.get("max_reply_delay_sec", 300),
            first_orig.get("short_msg_chars_threshold", 20),
            first_orig.get("long_msg_chars_threshold", 150),
            db_path=orig_db,
        )
        bot_manager.fast_mode = orig_fast_mode


if __name__ == "__main__":
    import traceback

    try:
        run_all_phase5_tests()
    except Exception:
        _log("PHASE 5 TEST FAILED:\n" + traceback.format_exc())
        raise

