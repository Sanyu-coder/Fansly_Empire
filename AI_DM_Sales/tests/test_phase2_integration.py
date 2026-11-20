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

LOG_FILE = os.path.join(BASE_DIR, "logs", "phase2_test_output.log")
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)


def _log(msg: str) -> None:
    print(msg, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as fp:
        fp.write(msg + "\n")


def check_syntax_all_files() -> None:
    targets = [os.path.join(BASE_DIR, "run_dm_sales.py")]
    targets.extend(glob.glob(os.path.join(BASE_DIR, "app", "**", "*.py"), recursive=True))
    targets.append(os.path.abspath(__file__))
    for path in sorted(set(targets)):
        py_compile.compile(path, doraise=True)
    _log(f"=== [SYNTAX CHECK] py_compile passed for all {len(set(targets))} Python files ===")


from app import create_app
from app.core import config
from app.core.database import (
    add_dm_message,
    get_active_playbooks,
    get_fan_ppv_transactions,
    get_thread_detail,
    init_db,
    mark_ppv_unlocked,
    record_ppv_offer,
    upsert_fan_sales_state,
)
from app.services.dormant_revival_worker import run_dormant_revival
from app.services.fansly_dm_bot import BotManager
from app.services.optimizer_batch import (
    evaluate_and_promote_ab_test,
    run_playbook_optimization,
)
from app.services.sales_engine import sales_engine


def run_all_phase2_tests() -> None:
    if os.path.exists(LOG_FILE):
        os.remove(LOG_FILE)
    check_syntax_all_files()

    tmp_dir = tempfile.mkdtemp(prefix="ai_dm_sales_phase2_test_")
    test_db = os.path.join(tmp_dir, "test_empire_core.db")
    orig_db = config.DB_PATH
    config.DB_PATH = test_db
    init_db(test_db)

    char_id = "Sylphy-Mia"
    test_bot_mgr = BotManager(fast_mode=True, headless=True)

    try:
        _log("=== [TEST 1] optimizer_batch ルール生成・A/B均等振り分け・CVR集計＆自動昇格 ===")
        assigned_groups = []
        for i in range(4):
            f = upsert_fan_sales_state(
                char_id=char_id,
                fan_id=f"new_fan_{i}",
                username=f"NewFan{i}",
                db_path=test_db,
            )
            assigned_groups.append(f["ab_group"])
        assert assigned_groups.count("A") == 2 and assigned_groups.count("B") == 2, f"Unbalanced AB: {assigned_groups}"

        upsert_fan_sales_state(
            char_id=char_id,
            fan_id="winner_1",
            username="WhaleWinner",
            current_tier="silver_vip",
            total_spent_usd=29.99,
            sales_phase="5_Cooldown",
            ab_group="A",
            db_path=test_db,
        )
        add_dm_message(char_id, "winner_1", "fan", "Show me your secret bedroom lingerie video!", content_ja="秘密の動画見たい！", db_path=test_db)
        add_dm_message(char_id, "winner_1", "ai", "Filmed this teasing bedroom clip just for you at $14.99!", content_ja="特別に撮ったよ♡", db_path=test_db)
        w_tx = record_ppv_offer(char_id, "winner_1", "ppv_001", 14.99, tx_type="ppv", db_path=test_db)
        mark_ppv_unlocked(char_id, "winner_1", tx_id=w_tx, db_path=test_db)

        upsert_fan_sales_state(
            char_id=char_id,
            fan_id="loser_1",
            username="GhostedFreebie",
            current_tier="free_follower",
            total_spent_usd=0.0,
            sales_phase="6_Low_Priority",
            consecutive_ppv_ignored=2,
            is_low_priority=1,
            ab_group="A",
            db_path=test_db,
        )
        add_dm_message(char_id, "loser_1", "fan", "Send free pics", content_ja="無料で送って", db_path=test_db)
        add_dm_message(char_id, "loser_1", "ai", "Unlock this clip for $9.99!", content_ja="$9.99で解除してね", db_path=test_db)
        record_ppv_offer(char_id, "loser_1", "ppv_001", 9.99, tx_type="ppv", db_path=test_db)

        opt_res = run_playbook_optimization(char_id=char_id, db_path=test_db)
        assert opt_res["ok"] is True
        pb_b = opt_res["new_playbook_b"]
        assert pb_b is not None and pb_b["ab_group"] == "B" and pb_b["is_active"] == 1
        assert len(pb_b["rules_text"]) > 20 and len(pb_b.get("rules_ja") or "") > 10
        b_ver_id = int(pb_b["version_id"])
        _log(f"  -> Generated Playbook B v{b_ver_id} | Summary: {opt_res['analysis_summary_ja'][:80]}")

        upsert_fan_sales_state(char_id=char_id, fan_id="b_tester", username="BTester", ab_group="B", db_path=test_db)
        eng_b = sales_engine.process_fan_message(
            char_id=char_id,
            user_message="Hey cutie, what are you wearing tonight?",
            fan_id="b_tester",
            persist_db=True,
            db_path=test_db,
        )
        assert eng_b["ab_group"] == "B"
        assert f"AB Group B / v{b_ver_id}" in eng_b["system_prompt_preview"]

        for idx in range(11):
            tx_a = record_ppv_offer(char_id, "new_fan_0", f"ppv_a_{idx}", 9.99, tx_type="ppv", db_path=test_db)
            if idx < 2:
                mark_ppv_unlocked(char_id, "new_fan_0", tx_id=tx_a, db_path=test_db)

        for idx in range(12):
            tx_b = record_ppv_offer(char_id, "b_tester", f"ppv_b_{idx}", 9.99, tx_type="ppv", db_path=test_db)
            if idx < 8:
                mark_ppv_unlocked(char_id, "b_tester", tx_id=tx_b, db_path=test_db)

        eval_res = evaluate_and_promote_ab_test(char_id=char_id, min_samples=10, db_path=test_db)
        assert eval_res["promoted"] is True, f"Expected promotion, got: {eval_res}"
        pbs_after = get_active_playbooks(char_id, db_path=test_db)
        assert int(pbs_after["A"]["version_id"]) == b_ver_id
        assert pbs_after["B"] is None
        _log(f"  -> Auto-Promoted v{b_ver_id} to Group A! Reason: {eval_res['reason']}")

        _log("=== [TEST 2] チップ（投げ銭）誘導フック (tx_type='tip') 発動検証 ===")
        tip_res = sales_engine.process_fan_message(
            char_id=char_id,
            user_message="You're such a gorgeous goddess! Can you do something special just for me tonight?",
            fan_id="tip_fan_1",
            current_tier="silver_vip",
            total_spent_usd=25.0,
            sales_phase="2_Explore",
            persist_db=True,
            db_path=test_db,
        )
        assert tip_res["tip_hook"]["triggered"] is True
        assert tip_res["tip_hook"]["suggested_amount_usd"] in (5.0, 10.0)
        assert "$" in tip_res["ai_en"]
        tip_txs = get_fan_ppv_transactions(char_id, "tip_fan_1", db_path=test_db)
        assert any(t["tx_type"] == "tip" for t in tip_txs), f"No tip tx recorded: {tip_txs}"
        _log(f"  -> Tip Hook EN: {tip_res['ai_en']}")
        _log(f"  -> Tip Hook JA: {tip_res['ai_ja']}")

        # 5_Cooldown (PPV購入直後・感謝フェーズ) でのチップ誘導も検証
        tip_cd = sales_engine.process_fan_message(
            char_id=char_id,
            user_message="Just unlocked it, loved that video so much!",
            fan_id="tip_fan_cooldown",
            current_tier="gold_goddess",
            total_spent_usd=60.0,
            sales_phase="5_Cooldown",
            persist_db=True,
            db_path=test_db,
        )
        assert tip_cd["tip_hook"]["triggered"] is True
        assert any(
            t["tx_type"] == "tip"
            for t in get_fan_ppv_transactions(char_id, "tip_fan_cooldown", db_path=test_db)
        )

        _log("=== [TEST 3] 7日以上未連絡の課金済み休眠ファン抽出・カルテ連動DM・送信キュー検証 ===")
        now_dt = datetime.now()
        old_iso = (now_dt - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%S")
        recent_iso = (now_dt - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S")

        upsert_fan_sales_state(
            char_id=char_id,
            fan_id="dormant_vip_1",
            username="QuietWhale",
            current_tier="gold_goddess",
            total_spent_usd=85.0,
            sales_phase="5_Cooldown",
            psych_profile_dict={
                "preferences": ["コスプレ・ランジェリー", "寝室・ベッドルーム秘密動画"],
                "psych_type": "甘え・特別扱い重視VIPファン",
                "budget_tier": "高単価VIP層 ($85.00)",
                "hooks": ["黒レースのランジェリー", "名前呼びささやき"],
                "notes": "黒ランジェリーと甘いささやきに高反応",
            },
            last_contact_at=old_iso,
            db_path=test_db,
        )
        upsert_fan_sales_state(
            char_id=char_id,
            fan_id="recent_vip",
            username="ActiveVIP",
            total_spent_usd=50.0,
            last_contact_at=recent_iso,
            db_path=test_db,
        )
        upsert_fan_sales_state(
            char_id=char_id,
            fan_id="dormant_free",
            username="FreeDormant",
            total_spent_usd=0.0,
            last_contact_at=old_iso,
            db_path=test_db,
        )
        upsert_fan_sales_state(
            char_id=char_id,
            fan_id="dormant_esc",
            username="EscalatedPaid",
            total_spent_usd=30.0,
            is_escalated=1,
            escalation_reason="危険ワード",
            last_contact_at=old_iso,
            db_path=test_db,
        )

        rev_res = run_dormant_revival(
            char_id=char_id,
            inactive_days=7,
            max_targets=10,
            now_dt=now_dt,
            db_path=test_db,
            bot_mgr=test_bot_mgr,
        )
        assert rev_res["revived_count"] == 1, f"Expected 1 dormant VIP, got: {rev_res}"
        rev_item = rev_res["results"][0]
        assert rev_item["fan_id"] == "dormant_vip_1"
        assert len(rev_item["content"]) > 15 and len(rev_item["content_ja"]) > 5
        assert rev_item["queued"]["fan_id"] == "dormant_vip_1"
        assert rev_item["queued"]["source"] == "dormant_revival"
        assert test_bot_mgr.get_status(char_id)["char_status"]["queued_count"] == 1

        detail_after = get_thread_detail(char_id, "dormant_vip_1", db_path=test_db)
        assert len(detail_after["messages"]) >= 1

        rev_again = run_dormant_revival(
            char_id=char_id,
            inactive_days=7,
            max_targets=10,
            now_dt=now_dt,
            db_path=test_db,
            bot_mgr=test_bot_mgr,
        )
        assert rev_again["revived_count"] == 0, "Duplicate send prevention failed!"
        _log(f"  -> Revival DM EN: {rev_item['content']}")
        _log(f"  -> Revival DM JA: {rev_item['content_ja']}")

        _log("=== [TEST 4] Flask API (/api/evolution/status, /optimize, /revive_dormant) 検証 ===")
        upsert_fan_sales_state(
            char_id=char_id,
            fan_id="dormant_vip_api",
            username="ApiDormantFan",
            total_spent_usd=40.0,
            psych_profile_dict={"preferences": ["プライベート写真セット"], "hooks": ["優しい近況伺い"]},
            last_contact_at=old_iso,
            db_path=test_db,
        )
        app = create_app()
        client = app.test_client()

        r_status = client.get(f"/api/evolution/status?char_id={char_id}")
        assert r_status.status_code == 200
        st_json = r_status.get_json()
        assert st_json["ok"] is True and st_json["dormant_candidates_count"] == 1

        r_opt = client.post("/api/evolution/optimize", json={"char_id": char_id})
        assert r_opt.status_code == 200
        opt_json = r_opt.get_json()
        assert opt_json["ok"] is True and opt_json["status"]["playbook_b"] is not None

        r_rev = client.post("/api/evolution/revive_dormant", json={"char_id": char_id, "inactive_days": 7})
        assert r_rev.status_code == 200
        rev_json = r_rev.get_json()
        assert rev_json["ok"] is True and rev_json["revived_count"] == 1
        assert rev_json["status"]["dormant_candidates_count"] == 0

        _log("ALL 4 PHASE 2 INTEGRATION TESTS PASSED SUCCESSFULLY!")
    finally:
        config.DB_PATH = orig_db


if __name__ == "__main__":
    import traceback

    try:
        run_all_phase2_tests()
    except Exception:
        _log("TEST FAILED WITH EXCEPTION:\n" + traceback.format_exc())
        raise


