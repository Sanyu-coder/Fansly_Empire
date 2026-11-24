"""
リファクタリング後パイプラインの結合テスト（実環境・結合レベル）。

実行方法:
    python -m pytest tests/test_integration_pipeline.py -v

検証項目:
    系統1: ステート遷移 × DB永続化 × _state_step_turns 整合性
           (process_fan_message を実際に走らせ、DB の sales_phase /
            psych_profile_json._state_step_turns / fans.state_step_turns
            が遷移ルールと一致することを検証)
    系統2: 耳役LLM・長期記憶・PPV選定・ログ記録の連携
           (モックLLM で process_fan_message 全パイプライン実行し、
            facts / conversation_summary / record_ppv_offer /
            append_fan_decision_log / unmatched_needs が欠損なく記録されることを検証)
    系統3: LLMキュー・排他ロック・デバウンス・ゾンビ検知の結合挙動
           (PriorityLock 優先度 / _sending_lock 排他 /
            _enqueue_debounced_fan_message の連投バッファリング /
            ゾンビ検知条件ロジック)
    系統4: バックエンドAPI & 管理画面UI の結合疎通
           (Flask テストクライアントで /api/monitoring/thread が新ステート・
            state_step_turns / matched_tags / facts / conversation_summary /
            next_transition_condition を正しく返し、/api/settings/get・save・
            test_discord の7キー正規化・保存・保持・バリデーション400を検証)

安全ルール:
    - 本番DBを一切触れず、すべてのテストが一時DB (tempfile) を使用
    - 外部LLM / Fansly / Discord への実通信はすべてモック・ドライラン
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from unittest import mock

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from app.core import config  # noqa: E402
from app.core.database import (  # noqa: E402
    init_db,
    get_db_connection,
    get_fan_record,
    get_fan_ppv_transactions,
)
from app.core.llm_client import (  # noqa: E402
    Priority,
    PriorityLock,
    LLMResilientRetryExhaustedError,
)
from app.services.sales_engine import (  # noqa: E402
    SalesEngine,
    evaluate_state_transition,
    STATE_COLLECT,
    STATE_PITCH,
    STATE_COOLDOWN,
    STATE_ESCALATE,
    STATE_LOW_PRIORITY,
)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _seed_vault(db_path: str, char_id: str, rows) -> None:
    conn = get_db_connection(db_path)
    try:
        for r in rows:
            conn.execute(
                """
                INSERT OR REPLACE INTO ppv_vault (
                    ppv_id, char_id, filename, media_type, ladder_step,
                    base_price_usd, tags_json, category_tags_json, is_general,
                    is_active, created_at
                ) VALUES (?, ?, ?, 'video', ?, 9.99, ?, ?, ?, 1, ?);
                """,
                (
                    r["ppv_id"], char_id, f"{r['ppv_id']}.mp4",
                    r.get("ladder_step", 1),
                    json.dumps(r.get("tags", []), ensure_ascii=False),
                    json.dumps(r.get("category_tags", {}), ensure_ascii=False),
                    r.get("is_general", 0),
                    _iso(datetime.now()),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _seed_tx(db_path: str, char_id: str, fan_id: str, ppv_id: str,
             is_unlocked: int, offered_dt: datetime) -> None:
    conn = get_db_connection(db_path)
    try:
        conn.execute(
            """
            INSERT INTO ppv_transactions (
                fan_id, char_id, ppv_id, tx_type, offered_price,
                is_unlocked, offered_at
            ) VALUES (?, ?, ?, 'offer', 9.99, ?, ?);
            """,
            (fan_id, char_id, ppv_id, is_unlocked, _iso(offered_dt)),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_fan(db_path: str, char_id: str, fan_id: str,
              tier: str = "free_follower", spent: float = 0.0) -> None:
    conn = get_db_connection(db_path)
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO fans (
                fan_id, char_id, username, current_tier, total_spent_usd,
                sales_phase, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?);
            """,
            (fan_id, char_id, f"user_{fan_id}", tier, spent,
             "1_Collect_Tags", _iso(datetime.now())),
        )
        conn.commit()
    finally:
        conn.close()


def _set_fan_field(db_path: str, char_id: str, fan_id: str,
                   field: str, value: Any) -> None:
    conn = get_db_connection(db_path)
    try:
        conn.execute(
            f"UPDATE fans SET {field} = ? WHERE char_id = ? AND fan_id = ?;",
            (value, char_id, fan_id),
        )
        conn.commit()
    finally:
        conn.close()


def _get_fan_profile_dict(db_path: str, char_id: str, fan_id: str) -> Dict[str, Any]:
    rec = get_fan_record(char_id, fan_id, db_path=db_path)
    if not rec:
        return {}
    raw = rec.get("psych_profile_json") or "{}"
    try:
        prof = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
    except Exception:
        prof = {}
    return prof if isinstance(prof, dict) else {}


def _get_fan_column(db_path: str, char_id: str, fan_id: str, column: str) -> Any:
    rec = get_fan_record(char_id, fan_id, db_path=db_path)
    return (rec or {}).get(column)


def _get_decision_logs(db_path: str, char_id: str, fan_id: str) -> List[Dict[str, Any]]:
    """fans.decision_log_json (JSON配列) を解析して決定ログリストを返す。"""
    rec = get_fan_record(char_id, fan_id, db_path=db_path)
    if not rec:
        return []
    raw = rec.get("decision_log_json") or "[]"
    try:
        logs = json.loads(raw) if isinstance(raw, str) else (raw or [])
    except Exception:
        logs = []
    return [l for l in logs if isinstance(l, dict)]


def _get_unmatched_needs(db_path: str, char_id: str, fan_id: str) -> List[Dict[str, Any]]:
    conn = get_db_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT keyword, raw_quote FROM unmatched_needs WHERE char_id = ? AND fan_id = ?;",
            (char_id, fan_id),
        ).fetchall()
    finally:
        conn.close()
    return [{"keyword": r[0], "raw_quote": r[1]} for r in rows]


# =====================================================================
# モックLLM (本番LLMへの実通信を完全に遮断)
# =====================================================================
class MockLLM:
    """SalesEngine 用のモックLLM。generate_text はデフォルト値を返す。"""

    def __init__(self):
        self.generate_text_calls: List[Dict[str, Any]] = []

    def generate_text(self, system_prompt: str, user_prompt: str,
                      temperature: float = 0.7, max_tokens: int = 200,
                      fallback_text: str = "", **kw) -> str:
        self.generate_text_calls.append({
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
        })
        return "Sure babe, tell me more about what you like best. 😘"


def _mock_ear(ear_result: Dict[str, Any]) -> mock.patch:
    """profiler_service.llm_client.generate_json をモックする (ear役LLM)。"""
    return mock.patch(
        "app.services.profiler_service.llm_client",
        **{"generate_json.return_value": dict(ear_result)},
    )


# =====================================================================
# 系統1: ステート遷移 × DB永続化 × _state_step_turns 整合性
# =====================================================================
class BasePipelineTest(unittest.TestCase):
    """一時DB + モックLLM を用意する結合テストの共通ベース。"""

    CHAR = "char_int_a"
    FAN = "fan_int_001"

    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db", prefix="dm_int_test_")
        os.close(fd)
        self.db_path = path
        init_db(db_path=self.db_path, seed_demo=False)
        self.engine = SalesEngine(llm=MockLLM())

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.db_path + suffix)
            except OSError:
                pass

    def _process(self, user_message: str, ear: Dict[str, Any],
                 consecutive_ppv_ignored: int = 0,
                 current_tier: str = "free_follower",
                 total_spent_usd: float = 0.0) -> Dict[str, Any]:
        """モックearLLMで process_fan_message を実行し結果を返す。
        sales_phase は DB の現在値を参照する (実環境のファン状態と一致)。"""
        rec = get_fan_record(self.CHAR, self.FAN, db_path=self.db_path) or {}
        sales_phase = rec.get("sales_phase") or STATE_COLLECT
        with _mock_ear(ear):
            return self.engine.process_fan_message(
                char_id=self.CHAR,
                user_message=user_message,
                fan_id=self.FAN,
                current_tier=current_tier,
                total_spent_usd=total_spent_usd,
                sales_phase=sales_phase,
                consecutive_ppv_ignored=consecutive_ppv_ignored,
                persist_db=True,
                record_tip_in_db=False,
                db_path=self.db_path,
            )

    def _fan_state(self) -> Dict[str, Any]:
        rec = get_fan_record(self.CHAR, self.FAN, db_path=self.db_path) or {}
        prof = _get_fan_profile_dict(self.db_path, self.CHAR, self.FAN)
        return {
            "sales_phase": rec.get("sales_phase"),
            "state_step_turns_col": rec.get("state_step_turns"),
            "state_step_turns_prof": prof.get("_state_step_turns"),
            "consecutive_ppv_ignored": rec.get("consecutive_ppv_ignored"),
            "is_escalated": rec.get("is_escalated"),
            "is_low_priority": rec.get("is_low_priority"),
            "profile": prof,
        }


class TestStatePersistenceIntegration(BasePipelineTest):
    """系統1: 各遷移が DB (sales_phase / _state_step_turns / state_step_turns) と整合するか"""

    def _seed_vault(self):
        _seed_vault(self.db_path, self.CHAR, [
            {"ppv_id": "pv_lingerie", "tags": ["lingerie"], "is_general": 0},
            {"ppv_id": "pv_general", "tags": [], "is_general": 1},
        ])
        _seed_fan(self.db_path, self.CHAR, self.FAN, tier="vip_subscriber", spent=50.0)

    def test_collect_to_pitch_tag_match(self):
        """タグ一致 → 2_Pitch_PPV へ遷移し、DB に sales_phase=2_Pitch_PPV・turns=0 が保存される"""
        self._seed_vault()
        ear = {"matched_tags": ["lingerie"], "has_buy_intent": False, "unmatched_needs": [], "reasoning_ja": "タグ一致"}
        res = self._process("I love lingerie!", ear)
        self.assertEqual(res["sales_phase"], STATE_PITCH)
        st = self._fan_state()
        self.assertEqual(st["sales_phase"], STATE_PITCH, "DB sales_phase が 2_Pitch_PPV であるべき")
        self.assertEqual(st["state_step_turns_prof"], 0, "遷移時は _state_step_turns=0 リセット")
        self.assertEqual(st["state_step_turns_col"], 0, "fans.state_step_turns も 0 に同期")
        # PPV オファーが記録される
        txs = get_fan_ppv_transactions(self.CHAR, self.FAN, db_path=self.db_path)
        self.assertTrue(any(t.get("ppv_id") == "pv_lingerie" for t in txs),
                        "pv_lingerie のオファー記録が無い")

    def test_collect_no_tag_stays_and_increments(self):
        """タグ未充足 → 1_Collect_Tags 滞在し _state_step_turns +1 される"""
        self._seed_vault()
        ear = {"matched_tags": [], "has_buy_intent": False, "unmatched_needs": [], "reasoning_ja": ""}
        res1 = self._process("just chatting about my day", ear)
        self.assertEqual(res1["sales_phase"], STATE_COLLECT)
        st1 = self._fan_state()
        self.assertEqual(st1["state_step_turns_prof"], 1, "滞在時 +1 (0→1)")
        res2 = self._process("still chatting", ear)
        st2 = self._fan_state()
        self.assertEqual(st2["state_step_turns_prof"], 2, "滞在時 +1 (1→2)")

    def test_immediate_buy_jump_to_pitch(self):
        """即買い飛び級: has_buy_intent=True → 2_Pitch_PPV (タグ目標未達でも)"""
        self._seed_vault()
        ear = {"matched_tags": ["lingerie"], "has_buy_intent": True, "unmatched_needs": [], "reasoning_ja": "購入意向"}
        res = self._process("I want to buy your secret video now", ear)
        self.assertEqual(res["sales_phase"], STATE_PITCH)
        st = self._fan_state()
        self.assertEqual(st["sales_phase"], STATE_PITCH)
        self.assertEqual(st["state_step_turns_prof"], 0)

    def test_ceiling_reaches_pitch_with_none_ppv_and_returns_in_3_turns(self):
        """天井到達 (turns>=5) → ppv=None で 2_Pitch_PPV へ遷移し、
        在庫0件時の 3往復後に 1_Collect_Tags 復帰 (turns=0 リセット)"""
        # 在庫0件: PPV が 1 件もない Vault → 天井到達時 recommended_ppv=None
        _seed_vault(self.db_path, self.CHAR, [])
        _seed_fan(self.db_path, self.CHAR, self.FAN, tier="vip_subscriber", spent=50.0)
        ear = {"matched_tags": [], "has_buy_intent": False, "unmatched_needs": [], "reasoning_ja": ""}

        # 天井判定は開始時 turns >= 5。
        # 往復1〜5: 開始時 turns=0〜4 → 1_Collect_Tags 滞在 (処理後 turns=1〜5)
        for i in range(5):
            res = self._process(f"chatting {i}", ear)
            self.assertEqual(res["sales_phase"], STATE_COLLECT,
                             f"往復{i+1}: 天井到達前に 1_Collect_Tags 滞在すべき")
            self.assertEqual(_get_fan_profile_dict(self.db_path, self.CHAR, self.FAN).get("_state_step_turns"), i + 1,
                             f"往復{i+1}: turns={i+1} であるべき")
        # 往復6: 開始時 turns=5 → 天井到達 → 2_Pitch_PPV (ppv=None: fan所持タグなし)
        res = self._process("still chatting", ear)
        self.assertEqual(res["sales_phase"], STATE_PITCH, "天井到達で 2_Pitch_PPV へ強制遷移")
        self.assertIsNone(res["recommended_ppv"], "在庫0件 → ppv=None")
        st = self._fan_state()
        self.assertEqual(st["sales_phase"], STATE_PITCH)
        self.assertEqual(st["state_step_turns_prof"], 0, "2_Pitch_PPV 遷移時 turns=0")

        # 2_Pitch_PPV 滞在 (前回PPVなし) → 評価時 turns>=3 で 1_Collect_Tags 復帰
        # 入場時 turns=0 → 評価 turns=0,1,2 で滞在 → 評価 turns=3 で復帰
        for i in range(3):
            res = self._process(f"waiting {i}", ear)
            self.assertEqual(res["sales_phase"], STATE_PITCH, f"在庫準備中 往復{i+1} → 2_Pitch_PPV 滞在")
        res = self._process("waiting 3", ear)
        self.assertEqual(res["sales_phase"], STATE_COLLECT,
                         "2_Pitch_PPV (ppv=None) turns=3 到達 → 1_Collect_Tags 復帰")
        st = self._fan_state()
        self.assertEqual(st["sales_phase"], STATE_COLLECT)
        self.assertEqual(st["state_step_turns_prof"], 0, "復帰時に turns=0 リセット")

    def test_pitch_buy_intent_to_cooldown(self):
        """2_Pitch_PPV 滞在中の購入意向 → 3_Cooldown 遷移 (turns=0)"""
        self._seed_vault()
        ear_pitch = {"matched_tags": ["lingerie"], "has_buy_intent": False, "unmatched_needs": [], "reasoning_ja": ""}
        self._process("I love lingerie!", ear_pitch)  # → 2_Pitch_PPV
        # 2_Pitch_PPV 状態で購入意向
        rec = get_fan_record(self.CHAR, self.FAN, db_path=self.db_path)
        self.assertEqual(rec["sales_phase"], STATE_PITCH)
        ear_buy = {"matched_tags": ["lingerie"], "has_buy_intent": True, "unmatched_needs": [], "reasoning_ja": "購入意向"}
        with _mock_ear(ear_buy):
            res = self.engine.process_fan_message(
                char_id=self.CHAR,
                user_message="yes I will buy it!",
                fan_id=self.FAN,
                current_tier="vip_subscriber",
                total_spent_usd=50.0,
                sales_phase=STATE_PITCH,
                persist_db=True,
                record_tip_in_db=False,
                db_path=self.db_path,
            )
        self.assertEqual(res["sales_phase"], STATE_COOLDOWN, "購入意向 → 3_Cooldown")
        st = self._fan_state()
        self.assertEqual(st["sales_phase"], STATE_COOLDOWN)
        self.assertEqual(st["state_step_turns_prof"], 0)

    def test_cooldown_reverts_to_collect_after_threshold(self):
        """3_Cooldown: 規定往復数 (3) 経過で 1_Collect_Tags 復帰 (turns=0)"""
        self._seed_vault()
        # 直接 3_Cooldown 状態から処理
        _set_fan_field(self.db_path, self.CHAR, self.FAN, "sales_phase", STATE_COOLDOWN)
        ear = {"matched_tags": [], "has_buy_intent": False, "unmatched_needs": [], "reasoning_ja": ""}
        # turns=0,1,2 → 3_Cooldown 滞在; turns=3 → 復帰
        for i in range(3):
            res = self._process("thanks babe", ear)
            self.assertEqual(res["sales_phase"], STATE_COOLDOWN,
                             f"クールダウン{i+1}往復: 3_Cooldown 滞在")
            st = self._fan_state()
            self.assertEqual(st["state_step_turns_prof"], i + 1,
                             f"クールダウン{i+1}往復: turns={i+1}")
        res = self._process("thanks babe", ear)
        self.assertEqual(res["sales_phase"], STATE_COLLECT, "3往復経過 → 1_Collect_Tags 復帰")
        st = self._fan_state()
        self.assertEqual(st["state_step_turns_prof"], 0)

    def test_cooldown_buy_intent_rescue_to_pitch(self):
        """[破綻2] 冷却中の購入意向 + 在庫一致 → 2_Pitch_PPV 救済復帰"""
        self._seed_vault()
        _set_fan_field(self.db_path, self.CHAR, self.FAN, "sales_phase", STATE_COOLDOWN)
        ear_buy = {"matched_tags": ["lingerie"], "has_buy_intent": True, "unmatched_needs": [], "reasoning_ja": "購入意向"}
        with _mock_ear(ear_buy):
            res = self.engine.process_fan_message(
                char_id=self.CHAR, user_message="I want to buy that lingerie clip",
                fan_id=self.FAN, current_tier="vip_subscriber", total_spent_usd=50.0,
                sales_phase=STATE_COOLDOWN, persist_db=True, record_tip_in_db=False,
                db_path=self.db_path,
            )
        self.assertEqual(res["sales_phase"], STATE_PITCH, "冷却中購入意向+在庫 → 2_Pitch_PPV 救済")
        self.assertIsNotNone(res["recommended_ppv"])
        st = self._fan_state()
        self.assertEqual(st["sales_phase"], STATE_PITCH)
        self.assertEqual(st["state_step_turns_prof"], 0)

    def test_cooldown_buy_intent_no_inventory_to_collect(self):
        """[破綻2] 冷却中の購入意向 + 在庫なし → 1_Collect_Tags 救済復帰"""
        # 在庫0件 (汎用PPVなし)
        _seed_vault(self.db_path, self.CHAR, [
            {"ppv_id": "pv_cosplay", "tags": ["cosplay"], "is_general": 0},
        ])
        _seed_fan(self.db_path, self.CHAR, self.FAN, tier="vip_subscriber", spent=50.0)
        _set_fan_field(self.db_path, self.CHAR, self.FAN, "sales_phase", STATE_COOLDOWN)
        # 購入意向だが一致タグ無し (lingerie 在庫なし)
        ear_buy = {"matched_tags": [], "has_buy_intent": True, "unmatched_needs": [], "reasoning_ja": "購入意向だが在庫なし"}
        with _mock_ear(ear_buy):
            res = self.engine.process_fan_message(
                char_id=self.CHAR, user_message="I want to buy something",
                fan_id=self.FAN, current_tier="vip_subscriber", total_spent_usd=50.0,
                sales_phase=STATE_COOLDOWN, persist_db=True, record_tip_in_db=False,
                db_path=self.db_path,
            )
        self.assertEqual(res["sales_phase"], STATE_COLLECT, "冷却中購入意向+在庫なし → 1_Collect_Tags")

    def test_danger_word_escalate_and_cleared_recovery(self):
        """危険ワード → Escalate_Stop (is_escalated=1)。
        人間の介入解除 (is_escalated=0) 後 → 1_Collect_Tags 復帰 (turns=0)"""
        self._seed_vault()
        ear = {"matched_tags": [], "has_buy_intent": False, "unmatched_needs": [], "reasoning_ja": ""}
        # 危険ワード: DANGER_KEYWORDS の単語境界語 'scam' を使用
        res = self._process("this is a scam", ear)
        self.assertEqual(res["is_escalated"], 1)
        st = self._fan_state()
        self.assertEqual(st["is_escalated"], 1, "DB is_escalated=1")
        # 未解除 → 再び危険ワード → is_escalated=1 維持
        res2 = self._process("this is also a scam", ear)
        self.assertEqual(res2["is_escalated"], 1, "危険ワード再発で is_escalated=1 維持")
        rec = get_fan_record(self.CHAR, self.FAN, db_path=self.db_path)
        self.assertEqual(rec["is_escalated"], 1, "DB is_escalated=1 維持")
        # 人間の介入解除: is_escalated=0 に設定
        _set_fan_field(self.db_path, self.CHAR, self.FAN, "is_escalated", 0)
        with _mock_ear(ear):
            res3 = self.engine.process_fan_message(
                char_id=self.CHAR, user_message="hi, thanks for checking",
                fan_id=self.FAN, current_tier="vip_subscriber", total_spent_usd=50.0,
                sales_phase=STATE_ESCALATE, persist_db=True, record_tip_in_db=False,
                db_path=self.db_path,
            )
        self.assertEqual(res3["sales_phase"], STATE_COLLECT, "解除 → 1_Collect_Tags 復帰")
        st3 = self._fan_state()
        self.assertEqual(st3["sales_phase"], STATE_COLLECT)
        self.assertEqual(st3["state_step_turns_prof"], 0)
        self.assertEqual(st3["is_escalated"], 0)

    def test_free_follower_low_priority_and_purchase_recovery(self):
        """無料フォロワー+PPV無視2回 → Low_Priority。
        購入発生 (is_unlocked=1) → 1_Collect_Tags 復帰 (turns=0)"""
        self._seed_vault()
        _seed_fan(self.db_path, self.CHAR, self.FAN, tier="free_follower", spent=0.0)
        ear = {"matched_tags": ["lingerie"], "has_buy_intent": False, "unmatched_needs": [], "reasoning_ja": ""}
        res = self._process("whatever", ear, consecutive_ppv_ignored=2)
        self.assertEqual(res["sales_phase"], STATE_LOW_PRIORITY, "無料+2回無視 → Low_Priority")
        st = self._fan_state()
        self.assertEqual(st["sales_phase"], STATE_LOW_PRIORITY)
        self.assertEqual(st["is_low_priority"], 1)
        # 購入なし → Low_Priority 維持
        res2 = self._process("hi", ear, consecutive_ppv_ignored=2)
        self.assertEqual(res2["sales_phase"], STATE_LOW_PRIORITY, "購入なし → Low_Priority 維持")
        # 購入発生: ppv_transactions に is_unlocked=1
        _seed_tx(self.db_path, self.CHAR, self.FAN, "pv_lingerie", is_unlocked=1,
                 offered_dt=datetime.now())
        res3 = self._process("hi", ear)
        self.assertEqual(res3["sales_phase"], STATE_COLLECT, "購入発生 → 1_Collect_Tags 復帰")
        st3 = self._fan_state()
        self.assertEqual(st3["sales_phase"], STATE_COLLECT)
        self.assertEqual(st3["state_step_turns_prof"], 0)
        self.assertEqual(st3["is_low_priority"], 0)


# =====================================================================
# 系統2: 耳役LLM・長期記憶・PPV選定・ログ記録の連携
# =====================================================================
class TestEarMemoryPpvLogging(BasePipelineTest):
    """系統2: パイプラインを通して各記録が欠損なく残るか"""

    def _seed(self):
        _seed_vault(self.db_path, self.CHAR, [
            {"ppv_id": "pv_lingerie", "tags": ["lingerie"], "is_general": 0},
            {"ppv_id": "pv_general", "tags": [], "is_general": 1},
        ])
        _seed_fan(self.db_path, self.CHAR, self.FAN, tier="free_follower", spent=0.0)

    def test_pipeline_records_tags_memory_ppv_and_logs(self):
        """タグ分類・構造化長期記憶 (facts/summary)・PPV選定・record_ppv_offer・
        append_fan_decision_log・unmatched_needs がすべて記録される"""
        self._seed()
        ear = {
            "matched_tags": ["lingerie", "fan_wish"],
            "has_buy_intent": True,
            "unmatched_needs": ["glasses"],
            "reasoning_ja": "下着動画の購入要求+メガネ要望",
        }
        res = self._process("I want to buy your lingerie video, also glasses", ear)
        self.assertTrue(res["ok"])
        self.assertEqual(res["sales_phase"], STATE_PITCH, "即買い飛び級 → 2_Pitch_PPV")

        prof = _get_fan_profile_dict(self.db_path, self.CHAR, self.FAN)
        # 耳役LLMのタグが psych_profile にマージされている
        self.assertIn("lingerie", prof.get("matched_tags") or [],
                      "matched_tags に lingerie がマージされていない")
        # 構造化長期記憶: facts / conversation_summary がキーとして存在
        self.assertIn("facts", prof, "facts キーが psych_profile に無い")
        self.assertIn("conversation_summary", prof, "conversation_summary キーが無い")
        # 新ステートマシンメタ情報
        self.assertIn("_new_state_machine", prof)
        self.assertEqual(prof["_new_state_machine"]["current_state"], STATE_PITCH)
        self.assertIn("_state_step_turns", prof)
        # recommended_ppv_id が記録
        self.assertEqual(prof.get("recommended_ppv_id"), "pv_lingerie")

        # PPVオファー記録 (record_ppv_offer)
        txs = get_fan_ppv_transactions(self.CHAR, self.FAN, db_path=self.db_path)
        self.assertTrue(any(t.get("ppv_id") == "pv_lingerie" and int(t.get("is_unlocked") or 0) == 0
                            for t in txs), "pv_lingerie のオファー記録 (is_unlocked=0) が無い")

        # 決定ログ (append_fan_decision_log)
        logs = _get_decision_logs(self.db_path, self.CHAR, self.FAN)
        self.assertTrue(logs, "決定ログが記録されていない")
        last = logs[-1]
        self.assertEqual(last.get("mode"), "new_state_machine")
        self.assertEqual(last.get("next_state"), STATE_PITCH)
        self.assertEqual(last.get("recommended_ppv_id"), "pv_lingerie")
        self.assertEqual(last.get("has_buy_intent"), True)

        # NEED蓄積 (unmatched_needs)
        needs = _get_unmatched_needs(self.db_path, self.CHAR, self.FAN)
        self.assertTrue(any(n["keyword"] == "glasses" for n in needs),
                        "unmatched_needs に 'glasses' が蓄積されていない")

    def test_pipeline_llm_failure_falls_back(self):
        """ear役LLMが全リトライ失敗してもフォールバック値でパイプラインが破綻しない"""
        self._seed()
        with mock.patch("app.services.profiler_service.llm_client") as m_client:
            m_client.generate_json.side_effect = LLMResilientRetryExhaustedError("ear", 20, None)
            with mock.patch(
                "app.services.profiler_service.execute_with_resilient_retry",
                side_effect=LLMResilientRetryExhaustedError("ear_llm_classify", 20, None),
            ):
                res = self._process("hi there", {})
        self.assertTrue(res["ok"], "LLM失敗でもパイプラインはフォールバックで完了する")
        self.assertEqual(res["sales_phase"], STATE_COLLECT)
        self.assertEqual(res["ear_result"]["matched_tags"], [])
        self.assertFalse(res["ear_result"]["has_buy_intent"])


# =====================================================================
# 系統3: LLMキュー・排他ロック・デバウンス・ゾンビ検知
# =====================================================================
class TestQueueLockDebounceZombie(unittest.TestCase):

    def _make_worker(self, char_id: str = "int_char"):
        import app.services.fansly_dm_bot as bot_mod
        with mock.patch.object(bot_mod, "ensure_character_files", return_value={"vault_dir": None}):
            return bot_mod.CharacterBotWorker(char_id=char_id)

    def test_priority_queue_high_preempts_low(self):
        """PriorityLock: LOW実行中にHIGHがキュー入り → LOW release後 HIGHが先回し実行"""
        lock = PriorityLock(acquire_timeout_sec=10.0)
        order: list = []
        phase = threading.Event()
        go_high = threading.Event()

        def low_task():
            with lock.context(Priority.LOW):
                order.append("LOW")
                phase.set()
                go_high.wait(timeout=5.0)
        def high_task():
            phase.wait(timeout=5.0)
            with lock.context(Priority.HIGH):
                order.append("HIGH")
        def low2_task():
            phase.wait(timeout=5.0)
            with lock.context(Priority.LOW):
                order.append("LOW2")
            go_high.set()

        t1 = threading.Thread(target=low_task); t1.start()
        t2 = threading.Thread(target=high_task); t2.start()
        t3 = threading.Thread(target=low2_task); t3.start()
        t1.join(15); t2.join(15); t3.join(15)
        self.assertFalse(t1.is_alive() or t2.is_alive() or t3.is_alive(), "デッドロックでスレッドが未完了")
        self.assertEqual(order, ["LOW", "HIGH", "LOW2"], f"優先度順序破綻: {order}")

    def test_sending_lock_mutual_exclusion(self):
        """_sending_lock: 二重送信が排他される (デッドロックなし)"""
        worker = self._make_worker()
        inside = threading.Event()
        violations = []

        def critical():
            with worker._sending_lock:
                if inside.is_set():
                    violations.append("二重入力")
                inside.set()
                time.sleep(0.3)
                inside.clear()

        t1 = threading.Thread(target=critical); t1.start()
        inside.wait(5)
        t2 = threading.Thread(target=critical); t2.start()
        t1.join(10); t2.join(10)
        self.assertFalse(t2.is_alive(), "ロックが機能していない (2件目未完了)")
        self.assertEqual(violations, [])

    def test_debounce_buffers_burst_and_flushes_once(self):
        """受信連投デバウンス: 短時間に3件受信 → バッファに3件蓄積、
        タイマー満後に1回だけ handle_incoming_fan_message が呼ばれる"""
        worker = self._make_worker()
        # debounce_seconds を短く (DB未設定 → 18秒がデフォルト。mockで0.5秒に)
        with mock.patch.object(type(worker), "_get_debounce_seconds", return_value=0.5):
            worker._enqueue_debounced_fan_message("fan_x", "user_x", "msg1", "free", False, None, None)
            worker._enqueue_debounced_fan_message("fan_x", "user_x", "msg2", "free", False, None, None)
            worker._enqueue_debounced_fan_message("fan_x", "user_x", "msg3", "free", False, None, None)
            # バッファに3件蓄積
            buf = worker._incoming_debounce_buffer.get("fan_x")
            self.assertIsNotNone(buf, "デバウンスバッファが作成されていない")
            self.assertEqual(len(buf["messages"]), 3, "3件バッファリングされていない")
            # タイマー満後 flush → handle_incoming_fan_message が1回だけ呼ばれる
            with mock.patch.object(worker, "handle_incoming_fan_message") as m_handle, \
                 mock.patch("app.core.database.add_dm_message"):
                time.sleep(0.7)
            self.assertTrue(m_handle.called, "flush 時 handle_incoming_fan_message が呼ばれない")
            self.assertEqual(m_handle.call_count, 1, "flush が複数回呼ばれた (連投デバウンス破綻)")
            # 結合メッセージが3行で改行結合
            kw = m_handle.call_args.kwargs
            self.assertEqual(kw["fan_message_en"], "msg1\nmsg2\nmsg3")
            self.assertNotIn("fan_x", worker._incoming_debounce_buffer, "flush 後バッファが未クリア")

    def test_zombie_detection_logic(self):
        """ゾンビ検知条件ロジック: DOM巡回停止 (stale>120s) / アウトボックス頭 stale>120s"""
        # fansly_dm_bot のゾンビ判定ロジックをソースレベルで検証
        import app.services.fansly_dm_bot as bot_mod
        import inspect
        src = inspect.getsource(bot_mod)
        self.assertIn("zombie_dom_stale", src, "ゾンビDOM検知ロジックが無い")
        self.assertIn("zombie_outbox_head_stale", src, "アウトボックス頭ゾンビ検知ロジックが無い")
        self.assertIn("_run_zombie_recovery", src, "ゾンビ3段階リカバリが無い")
        self.assertIn("_zombie_recovery_checked_ts", src, "ゾンビ検知基準時刻記録が無い")


# =====================================================================
# 系統4: バックエンドAPI & 管理画面UI の結合疎通
# =====================================================================
class TestApiIntegration(unittest.TestCase):
    """Flask テストクライアントで API 結合を疎通確認。
    本番DBを触れず、DB_PATH / AI_DM_SALES_BASE_DIR 環境変数で一時環境に隔離。"""

    @classmethod
    def setUpClass(cls):
        fd, cls.db_path = tempfile.mkstemp(suffix=".db", prefix="dm_int_api_")
        os.close(fd)
        cls.base_dir = os.path.join(tempfile.gettempdir(), f"dm_int_api_base_{os.getpid()}_{int(time.time()*1000)}")
        os.makedirs(cls.base_dir, exist_ok=True)
        # 本番設定を一切読まないよう環境変数で隔離
        cls._saved_env = {
            "DB_PATH": os.environ.get("DB_PATH"),
            "AI_DM_SALES_BASE_DIR": os.environ.get("AI_DM_SALES_BASE_DIR"),
            "SHARED_ASSETS_DIR": os.environ.get("SHARED_ASSETS_DIR"),
            "CHARACTERS_ROOT": os.environ.get("CHARACTERS_ROOT"),
            "PROFILES_DIR": os.environ.get("PROFILES_DIR"),
        }
        os.environ["DB_PATH"] = cls.db_path
        os.environ["AI_DM_SALES_BASE_DIR"] = cls.base_dir
        # shared_assets 配下の database/characters も一時環境へ
        shared = os.path.join(cls.base_dir, "shared_assets")
        os.makedirs(os.path.join(shared, "database"), exist_ok=True)
        os.makedirs(os.path.join(shared, "characters"), exist_ok=True)
        os.environ["SHARED_ASSETS_DIR"] = shared
        os.environ["CHARACTERS_ROOT"] = os.path.join(shared, "characters")
        os.environ["PROFILES_DIR"] = os.path.join(cls.base_dir, "profiles")
        os.makedirs(os.path.join(cls.base_dir, "profiles"), exist_ok=True)
        # config をリロードして環境変数を反映
        import importlib
        import app.core.config as _cfg
        importlib.reload(_cfg)

        from app import create_app
        cls.app = create_app(seed_demo=False)
        cls.client = cls.app.test_client()
        # テスト用キャラファイル作成 (character_loader が char_test_api を認識)
        from app.services.character_loader import ensure_character_files
        ensure_character_files("char_test_api")

    @classmethod
    def tearDownClass(cls):
        # 環境変数復元
        for k, v in cls._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import importlib
        import app.core.config as _cfg
        importlib.reload(_cfg)
        # 一時DB・一時ベースディレクトリ削除
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(cls.db_path + suffix)
            except OSError:
                pass
        import shutil
        shutil.rmtree(cls.base_dir, ignore_errors=True)

    def test_thread_api_returns_new_state_fields(self):
        """/api/monitoring/thread が新ステート・state_step_turns・matched_tags・
        facts・conversation_summary・next_transition_condition を正しく返す"""
        from app.core.database import upsert_fan_sales_state
        char_id = "char_test_api"
        fan_id = "fan_api_001"
        # 2_Pitch_PPV 状態・turns=2・タグ1個・facts・summary を持つファンをDBへ投入
        profile = {
            "matched_tags": ["lingerie"],
            "facts": ["ファンは下着動画を好む", "予算 $50"],
            "conversation_summary": "下着動画への関心が高いファン",
            "_state_step_turns": 2,
            "_new_state_machine": {"current_state": "2_Pitch_PPV"},
            "recommended_ppv_id": "pv_lingerie",
        }
        upsert_fan_sales_state(
            char_id=char_id, fan_id=fan_id,
            current_tier="free_follower", total_spent_usd=0.0,
            sales_phase="2_Pitch_PPV", state_step_turns=2,
            psych_profile_dict=profile,
        )
        # DMメッセージ1件投入 (get_thread_detail がスレッドを返す条件)
        from app.core.database import add_dm_message
        add_dm_message(char_id=char_id, fan_id=fan_id, sender="fan", content="hi")
        add_dm_message(char_id=char_id, fan_id=fan_id, sender="ai", content="hello babe")

        res = self.client.get(f"/api/monitoring/thread?char_id={char_id}&fan_id={fan_id}")
        self.assertEqual(res.status_code, 200, f"thread API が 200 を返さない: {res.status_code}")
        data = res.get_json()
        self.assertTrue(data.get("ok"))
        # 新ステートフィールドがトップレベルに露出
        self.assertEqual(data.get("sales_phase"), "2_Pitch_PPV")
        self.assertEqual(data.get("state_step_turns"), 2)
        self.assertEqual(data.get("matched_tags"), ["lingerie"])
        self.assertEqual(data.get("facts"), ["ファンは下着動画を好む", "予算 $50"])
        self.assertEqual(data.get("conversation_summary"), "下着動画への関心が高いファン")
        # next_transition_condition: had_ppv=True → 3_Cooldown 遷移条件
        self.assertIn("3_Cooldown", data.get("next_transition_condition") or "",
                       f"next_transition_condition が 2_Pitch_PPV+had_ppv を反映していない: {data.get('next_transition_condition')}")

    def test_thread_api_400_without_params(self):
        res = self.client.get("/api/monitoring/thread")
        self.assertEqual(res.status_code, 400)

    def test_settings_get_has_seven_keys(self):
        """/api/settings/get が新7キーを system_settings に含める"""
        res = self.client.get("/api/settings/get?char_id=char_test_api")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data.get("ok"))
        sys_settings = data.get("system_settings") or {}
        for key in ("discord_webhook_url", "debounce_seconds",
                    "tag_collect_target_count", "tag_collect_max_turns",
                    "cooldown_turns_threshold", "dm_ollama_base_url",
                    "dm_ollama_model_name"):
            self.assertIn(key, sys_settings, f"system_settings に {key} が無い")

    def test_settings_save_valid_seven_keys_persisted(self):
        """/api/settings/save で新7キーを保存 → /api/settings/get で保持される"""
        payload = {
            "char_id": "char_test_api",
            "discord_webhook_url": "https://discord.com/api/webhooks/123/test",
            "debounce_seconds": 12,
            "tag_collect_target_count": 2,
            "tag_collect_max_turns": 8,
            "cooldown_turns_threshold": 5,
            "dm_ollama_base_url": "http://127.0.0.1:11434/v1",
            "dm_ollama_model_name": "test-model:latest",
        }
        res = self.client.post("/api/settings/save", json=payload)
        self.assertEqual(res.status_code, 200, f"save 失敗: {res.get_json()}")
        data = res.get_json()
        self.assertTrue(data.get("ok"))
        # 保存後 get で保持確認
        res2 = self.client.get("/api/settings/get?char_id=char_test_api")
        sys_settings = (res2.get_json() or {}).get("system_settings") or {}
        self.assertEqual(sys_settings.get("discord_webhook_url"), payload["discord_webhook_url"])
        self.assertEqual(int(sys_settings.get("debounce_seconds")), 12)
        self.assertEqual(int(sys_settings.get("tag_collect_target_count")), 2)
        self.assertEqual(int(sys_settings.get("tag_collect_max_turns")), 8)
        self.assertEqual(int(sys_settings.get("cooldown_turns_threshold")), 5)
        self.assertEqual(sys_settings.get("dm_ollama_base_url"), payload["dm_ollama_base_url"])
        self.assertEqual(sys_settings.get("dm_ollama_model_name"), payload["dm_ollama_model_name"])

    def test_settings_save_invalid_debounce_400(self):
        """debounce_seconds=999 (1〜300超) → 400 バリデーションエラー"""
        res = self.client.post("/api/settings/save", json={
            "char_id": "char_test_api",
            "debounce_seconds": 999,
        })
        self.assertEqual(res.status_code, 400,
                         f"debounce_seconds=999 で 400 を返すべき: {res.status_code}")
        self.assertFalse(res.get_json().get("ok"))

    def test_settings_save_invalid_target_count_400(self):
        """tag_collect_target_count=5 (1〜2超) → 400"""
        res = self.client.post("/api/settings/save", json={
            "char_id": "char_test_api",
            "tag_collect_target_count": 5,
        })
        self.assertEqual(res.status_code, 400,
                         f"tag_collect_target_count=5 で 400 を返すべき: {res.status_code}")

    def test_settings_test_discord_invalid_url_400(self):
        """無効なWebhook URL → 400 (実通信なし)"""
        res = self.client.post("/api/settings/test_discord", json={
            "discord_webhook_url": "https://example.com/webhooks/bad",
        })
        self.assertEqual(res.status_code, 400)

    def test_settings_test_discord_no_url_400(self):
        """Webhook URL 未設定 → 400 (実通信なし・誤送信防止)"""
        res = self.client.post("/api/settings/test_discord", json={})
        self.assertEqual(res.status_code, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
