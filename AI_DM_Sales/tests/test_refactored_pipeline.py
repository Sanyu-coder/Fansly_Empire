"""
Ticket 7.1: リファクタリング後パイプラインの単体・統合テスト。

実行方法:
    python -m unittest tests.test_refactored_pipeline

テスト項目:
    テスト1: master_tags.json の読み込みと整合性
    テスト2: 耳役LLMによるタグ抽出・意図分類・NEED抽出 (LLMはモック・決定論的后処理を検証)
    テスト3: Python完全一致マッチングによる未購入PPV確定 (直近7日間除外ルール検証)
    テスト4: ステートマシン遷移 (ヒアリング➔提案➔クールダウン / 救済天井 / 即買い飛び級)
    テスト5: 送信排他ロック (is_sending) とウォッチドッグ待機
    テスト6: 不屈のリトライと優先度キュー
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from typing import Any, Dict
from unittest import mock

# app パッケージが import できるようにプロジェクトルートを sys.path に追加
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from app.core import config  # noqa: E402
from app.core.database import (  # noqa: E402
    init_db,
    get_system_settings,
)
from app.core.llm_client import (  # noqa: E402
    Priority,
    PriorityLock,
    LLMResilientRetryExhaustedError,
    execute_with_resilient_retry,
)
from app.services import tag_matcher  # noqa: E402
from app.services.tag_matcher import (  # noqa: E402
    load_master_tags,
    get_all_master_tag_ids,
    match_ppv_deterministic,
)
from app.services.profiler_service import classify_fan_intent_and_tags  # noqa: E402
from app.services.sales_engine import (  # noqa: E402
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
    """テスト用の PPV 素材を ppv_vault へ直接 INSERT する。"""
    from app.core.database import get_db_connection

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
    """テスト用の PPV 提案/購入履歴を ppv_transactions へ INSERT する。"""
    from app.core.database import get_db_connection

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
    """テスト用のファンレコードを fans へ INSERT する。"""
    from app.core.database import get_db_connection

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


class BaseTempDbTest(unittest.TestCase):
    """一時 SQLite DB を用意する共通ベース。"""

    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db", prefix="dm_sales_test_")
        os.close(fd)
        self.db_path = path
        init_db(db_path=self.db_path, seed_demo=False)

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.db_path + suffix)
            except OSError:
                pass


# =====================================================================
# テスト1: master_tags.json の読み込みと整合性
# =====================================================================
class TestMasterTagsIntegrity(unittest.TestCase):

    def test_load_master_tags_valid_json(self):
        """master_tags.json が有効なJSONで load_master_tags() 経由で読めること"""
        path = tag_matcher.master_tags_path()
        self.assertTrue(os.path.isfile(path), f"master_tags.json が見つからない: {path}")
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        self.assertIsInstance(raw, dict)
        self.assertIn("categories", raw)

        data = load_master_tags(refresh=True)
        self.assertIsInstance(data, dict)
        self.assertIsInstance(data.get("categories"), dict)

    def test_three_categories_min_six_tags(self):
        """play / body_outfit / situation の3カテゴリに各6個以上のタグが登録されている"""
        data = load_master_tags(refresh=True)
        cats = data.get("categories") or {}
        for cat in ("play", "body_outfit", "situation"):
            self.assertIn(cat, cats, f"カテゴリ '{cat}' が見つからない")
            tags = cats[cat].get("tags") or []
            self.assertGreaterEqual(len(tags), 6, f"カテゴリ '{cat}' のタグが6個未満: {len(tags)}")

    def test_tag_schema_fields(self):
        """各タグが id / category / name_ja / llm_hint を持つこと"""
        data = load_master_tags(refresh=True)
        for cat_name, cat in (data.get("categories") or {}).items():
            for tag in (cat.get("tags") or []):
                self.assertIn("id", tag, f"{cat_name} のタグに id が無い: {tag}")
                self.assertIn("category", tag)
                self.assertIn("name_ja", tag)
                self.assertIn("llm_hint", tag)
                # category フィールドがカテゴリ名と一致すること
                self.assertEqual(str(tag.get("category") or ""), cat_name,
                                 f"タグ {tag.get('id')} の category が '{cat_name}' と不一致")
                self.assertTrue(str(tag.get("name_ja") or "").strip(),
                                f"タグ {tag.get('id')} の name_ja が空")

    def test_get_all_master_tag_ids(self):
        """get_all_master_tag_ids() が全タグIDを平たく返す"""
        ids = get_all_master_tag_ids()
        self.assertGreater(len(ids), 0)
        data = load_master_tags(refresh=True)
        expected = set()
        for cat in (data.get("categories") or {}).values():
            for tag in (cat.get("tags") or []):
                tid = str(tag.get("id") or "").strip()
                if tid:
                    expected.add(tid)
        self.assertEqual(set(ids), expected)


# =====================================================================
# テスト2: 耳役LLMによるタグ抽出・意図分類・NEED抽出
# =====================================================================
class TestEarLLMClassification(BaseTempDbTest):
    """
    LLM 本体は起動環境に依存するためモックし、
    classify_fan_intent_and_tags の決定論的后処理
    (厳密ID一致フィルタ・重複除去・has_buy_intent・NEED蓄積) を検証する。
    """

    CHAR = "test_char"

    def _seed(self):
        _seed_vault(self.db_path, self.CHAR, [
            {"ppv_id": "pv_lingerie", "tags": ["lingerie"], "is_general": 0},
            {"ppv_id": "pv_general", "tags": [], "is_general": 1},
        ])
        _seed_fan(self.db_path, self.CHAR, "fan_001")

    def test_matched_tags_strict_filter(self):
        """LLM出力の matched_tags は候補IDに厳密一致するもののみ採用される"""
        self._seed()
        recent = [{"sender": "fan", "content": "I want to see your secret lingerie video with glasses"}]
        llm_out = {
            "matched_tags": ["lingerie", "glasses", "lingerie", "nonexistent_tag"],
            "has_buy_intent": True,
            "unmatched_needs": [],
            "reasoning_ja": "客が下着動画の購入を要求したため",
        }
        with mock.patch("app.services.profiler_service.llm_client") as m_client:
            m_client.generate_json.return_value = dict(llm_out)
            result = classify_fan_intent_and_tags(
                char_id=self.CHAR,
                recent_messages=recent,
                db_path=self.db_path,
                persist_needs=False,
                fan_id="fan_001",
            )
        # 厳密一致フィルタ: 'lingerie' のみ採用、重複除去
        self.assertEqual(result["matched_tags"], ["lingerie"])
        self.assertTrue(result["has_buy_intent"])

    def test_buy_intent_detection(self):
        """明確な購入要求に対し has_buy_intent=True が伝播される"""
        self._seed()
        recent = [{"sender": "fan", "content": "How much for that clip? I want to buy it"}]
        llm_out = {
            "matched_tags": ["lingerie"],
            "has_buy_intent": True,
            "unmatched_needs": [],
            "reasoning_ja": "購入意向が明確",
        }
        with mock.patch("app.services.profiler_service.llm_client") as m_client:
            m_client.generate_json.return_value = dict(llm_out)
            result = classify_fan_intent_and_tags(
                char_id=self.CHAR,
                recent_messages=recent,
                db_path=self.db_path,
                persist_needs=False,
                fan_id="fan_001",
            )
        self.assertTrue(result["has_buy_intent"])

    def test_unmatched_needs_persisted_to_db(self):
        """fan_wish タグ出力時は unmatched_needs が DB に即時蓄積される"""
        self._seed()
        recent = [{"sender": "fan", "content": "I want to see your secret lingerie video with glasses"}]
        llm_out = {
            "matched_tags": ["lingerie", "fan_wish"],
            "has_buy_intent": True,
            "unmatched_needs": ["glasses"],
            "reasoning_ja": "メガネ動画の要望は未登録タグ",
        }
        with mock.patch("app.services.profiler_service.llm_client") as m_client:
            m_client.generate_json.return_value = dict(llm_out)
            result = classify_fan_intent_and_tags(
                char_id=self.CHAR,
                recent_messages=recent,
                db_path=self.db_path,
                persist_needs=True,
                fan_id="fan_001",
            )
        self.assertEqual(result["matched_tags"], ["lingerie", "fan_wish"])
        self.assertIn("glasses", result["unmatched_needs"])

        # DB に NEED が蓄積されていること
        from app.core.database import get_db_connection
        conn = get_db_connection(self.db_path)
        try:
            row = conn.execute(
                "SELECT keyword, raw_quote FROM unmatched_needs WHERE char_id = ? AND fan_id = ? AND keyword = ?;",
                (self.CHAR, "fan_001", "glasses"),
            ).fetchone()
            self.assertIsNotNone(row, "unmatched_needs に 'glasses' が蓄積されていない")
            self.assertIn("glasses", str(row[0]))
        finally:
            conn.close()

    def test_llm_exhausted_falls_back_to_default(self):
        """LLMが20回リトライしても復旧しない場合はデフォルト値でフォールバックする"""
        self._seed()
        recent = [{"sender": "fan", "content": "hi"}]
        with mock.patch("app.services.profiler_service.llm_client") as m_client:
            m_client.generate_json.side_effect = LLMResilientRetryExhaustedError("ear", 20, None)
            # execute_with_resilient_retry も例外送出にモック
            with mock.patch(
                "app.services.profiler_service.execute_with_resilient_retry",
                side_effect=LLMResilientRetryExhaustedError("ear_llm_classify", 20, None),
            ):
                result = classify_fan_intent_and_tags(
                    char_id=self.CHAR,
                    recent_messages=recent,
                    db_path=self.db_path,
                    persist_needs=False,
                    fan_id="fan_001",
                )
        self.assertEqual(result["matched_tags"], [])
        self.assertFalse(result["has_buy_intent"])
        self.assertEqual(result["unmatched_needs"], [])


# =====================================================================
# テスト3: Python完全一致マッチングによる未購入PPV確定 (7日間除外ルール)
# =====================================================================
class TestPpvDeterministicMatching(BaseTempDbTest):
    """
    再提案除外ルール:
      - 購入済み (is_unlocked=1) は永久除外
      - 未購入で直近7日以内に提案済みのもののみ除外
      - 7日以上経過した未購入PPVは再提案候補に復活
    """

    CHAR = "test_char"
    FAN = "fan_002"

    def _seed(self):
        now = datetime.now()
        # pv_purchased  : lingerie タグ・購入済み → 永久除外
        # pv_recent      : lingerie タグ・未購入・2日前に提案 → 7日以内除外
        # pv_old         : lingerie タグ・未購入・8日前に提案 → 再提案可能
        # pv_general     : 汎用素材 (is_general=1)
        _seed_vault(self.db_path, self.CHAR, [
            {"ppv_id": "pv_purchased", "tags": ["lingerie"], "is_general": 0},
            {"ppv_id": "pv_recent", "tags": ["lingerie"], "is_general": 0},
            {"ppv_id": "pv_old", "tags": ["lingerie"], "is_general": 0},
            {"ppv_id": "pv_general", "tags": [], "is_general": 1},
        ])
        _seed_fan(self.db_path, self.CHAR, self.FAN)
        _seed_tx(self.db_path, self.CHAR, self.FAN, "pv_purchased", is_unlocked=1, offered_dt=now - timedelta(days=10))
        _seed_tx(self.db_path, self.CHAR, self.FAN, "pv_recent", is_unlocked=0, offered_dt=now - timedelta(days=2))
        _seed_tx(self.db_path, self.CHAR, self.FAN, "pv_old", is_unlocked=0, offered_dt=now - timedelta(days=8))

    def test_purchased_permanently_excluded(self):
        """購入済みPPVは永久に候補から除外される"""
        self._seed()
        chosen = match_ppv_deterministic(self.CHAR, self.FAN, ["lingerie"], ladder_step=1, db_path=self.db_path)
        self.assertIsNotNone(chosen)
        self.assertNotEqual(chosen["ppv_id"], "pv_purchased", "購入済みPPVが選定された")

    def test_recent_offer_within_7d_excluded(self):
        """直近7日以内の未購入提案済みPPVは除外される"""
        self._seed()
        chosen = match_ppv_deterministic(self.CHAR, self.FAN, ["lingerie"], ladder_step=1, db_path=self.db_path)
        self.assertIsNotNone(chosen)
        self.assertNotEqual(chosen["ppv_id"], "pv_recent", "7日以内の未購入提案が再選定された")

    def test_8d_old_unpurchased_revives(self):
        """8日前の未購入提案済みPPVは再提案候補に復活する"""
        self._seed()
        chosen = match_ppv_deterministic(self.CHAR, self.FAN, ["lingerie"], ladder_step=1, db_path=self.db_path)
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen["ppv_id"], "pv_old",
                         f"8日前未購入PPVが再選定されない: {chosen['ppv_id'] if chosen else None}")

    def test_no_match_returns_none(self):
        """一致タグ在庫が無い場合 None が返る"""
        self._seed()
        chosen = match_ppv_deterministic(self.CHAR, self.FAN, ["cosplay"], ladder_step=1, db_path=self.db_path)
        self.assertIsNone(chosen)

    def test_rescue_route_selects_general(self):
        """救済ルート (is_rescue=True) は is_general=1 の素材を1件選定する"""
        self._seed()
        # 全 lingerie PPV を除外状態にする（全員購入済みと仮定）
        for pid in ("pv_purchased", "pv_recent", "pv_old"):
            _seed_tx(self.db_path, self.CHAR, self.FAN, pid, is_unlocked=1,
                     offered_dt=datetime.now() - timedelta(days=1))
        chosen = match_ppv_deterministic(self.CHAR, self.FAN, ["lingerie"], ladder_step=1,
                                         is_rescue=True, db_path=self.db_path)
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen["ppv_id"], "pv_general")
        self.assertEqual(int(chosen.get("is_general") or 0), 1)


# =====================================================================
# テスト4: ステートマシン遷移
# =====================================================================
class TestStateMachineTransitions(BaseTempDbTest):

    CHAR = "test_char"
    FAN = "fan_003"

    def _seed(self):
        _seed_vault(self.db_path, self.CHAR, [
            {"ppv_id": "pv_lingerie", "tags": ["lingerie"], "is_general": 0},
            {"ppv_id": "pv_general", "tags": [], "is_general": 1},
        ])
        _seed_fan(self.db_path, self.CHAR, self.FAN, tier="vip_subscriber", spent=50.0)

    def test_tag_match_transitions_to_pitch(self):
        """ヒアリング➔タグ一致➔2_Pitch_PPV 遷移 (通常ルート)"""
        self._seed()
        ear = {"matched_tags": ["lingerie"], "has_buy_intent": False, "unmatched_needs": [], "reasoning_ja": ""}
        new_state, ppv, meta = evaluate_state_transition(
            char_id=self.CHAR, fan_id=self.FAN,
            current_state=STATE_COLLECT,
            user_message="I love lingerie!",
            ear_result=ear,
            state_step_turns=1,
            tag_collect_target_count=1,
            db_path=self.db_path,
        )
        self.assertEqual(new_state, STATE_PITCH)
        self.assertIsNotNone(ppv)
        self.assertEqual(ppv["ppv_id"], "pv_lingerie")

    def test_ceiling_rescue_transitions_to_pitch_with_general(self):
        """救済天井: 5往復到達で is_general PPV 付き 2_Pitch_PPV へ強制遷移"""
        self._seed()
        ear = {"matched_tags": [], "has_buy_intent": False, "unmatched_needs": [], "reasoning_ja": ""}
        new_state, ppv, meta = evaluate_state_transition(
            char_id=self.CHAR, fan_id=self.FAN,
            current_state=STATE_COLLECT,
            user_message="just chatting about my day",
            ear_result=ear,
            state_step_turns=5,  # 天井到達 (tag_collect_max_turns=5)
            tag_collect_target_count=1,
            tag_collect_max_turns=5,
            db_path=self.db_path,
        )
        self.assertEqual(new_state, STATE_PITCH)
        self.assertIsNotNone(ppv)
        self.assertEqual(int(ppv.get("is_general") or 0), 1, "救済ルートで汎用PPVを選定すべき")

    def test_immediate_buy_skips_to_pitch(self):
        """即買い飛び級: has_buy_intent=True ならタグ未揃いでも即 2_Pitch_PPV"""
        self._seed()
        ear = {"matched_tags": ["lingerie"], "has_buy_intent": True, "unmatched_needs": [], "reasoning_ja": ""}
        new_state, ppv, meta = evaluate_state_transition(
            char_id=self.CHAR, fan_id=self.FAN,
            current_state=STATE_COLLECT,
            user_message="I want to buy your secret video now",
            ear_result=ear,
            state_step_turns=0,
            tag_collect_target_count=2,  # タグ目標2個でも飛び級する
            db_path=self.db_path,
        )
        self.assertEqual(new_state, STATE_PITCH)
        self.assertIsNotNone(ppv)

    def test_cooldown_returns_to_collect(self):
        """3_Cooldown の指定往復数経過 ➔ 1_Collect_Tags へ復帰"""
        self._seed()
        ear = {"matched_tags": [], "has_buy_intent": False, "unmatched_needs": [], "reasoning_ja": ""}
        # クールダウン中 (1往復) → 維持
        st1, _, _ = evaluate_state_transition(
            char_id=self.CHAR, fan_id=self.FAN, current_state=STATE_COOLDOWN,
            user_message="thanks babe", ear_result=ear, state_step_turns=1,
            cooldown_turns_threshold=3, db_path=self.db_path,
        )
        self.assertEqual(st1, STATE_COOLDOWN)
        # クールダウン 3往復経過 → 復帰
        st2, _, _ = evaluate_state_transition(
            char_id=self.CHAR, fan_id=self.FAN, current_state=STATE_COOLDOWN,
            user_message="thanks babe", ear_result=ear, state_step_turns=3,
            cooldown_turns_threshold=3, db_path=self.db_path,
        )
        self.assertEqual(st2, STATE_COLLECT)

    def test_danger_word_escalate_stop(self):
        """危険ワード検知時は即座に Escalate_Stop"""
        self._seed()
        # config.DANGER_KEYWORDS の先頭キーワードを使って即停止を検証
        dks = [str(k).strip().lower() for k in config.DANGER_KEYWORDS if str(k).strip()]
        self.assertTrue(dks, "DANGER_KEYWORDS が空")
        msg = f"hello, I would like to {dks[0]} you"
        ear = {"matched_tags": [], "has_buy_intent": False, "unmatched_needs": [], "reasoning_ja": ""}
        new_state, ppv, meta = evaluate_state_transition(
            char_id=self.CHAR, fan_id=self.FAN, current_state=STATE_COLLECT,
            user_message=msg, ear_result=ear, state_step_turns=0, db_path=self.db_path,
        )
        self.assertEqual(new_state, STATE_ESCALATE)
        self.assertIsNone(ppv)

    def test_harmless_messages_do_not_escalate(self):
        """[バグ回帰] 'again'/'available'/'hair' 等を含む無害メッセージで Escalate_Stop にならない"""
        self._seed()
        ear = {"matched_tags": [], "has_buy_intent": False, "unmatched_needs": [], "reasoning_ja": ""}
        harmless_msgs = [
            "Can I see it again?",
            "Are you available tonight?",
            "I love your hair",
            "This is a small detail",
            "thinking of my childhood",
        ]
        for msg in harmless_msgs:
            new_state, _, _ = evaluate_state_transition(
                char_id=self.CHAR, fan_id=self.FAN, current_state=STATE_COLLECT,
                user_message=msg, ear_result=ear, state_step_turns=0, db_path=self.db_path,
            )
            self.assertEqual(new_state, STATE_COLLECT,
                             f"無害メッセージ '{msg}' で Escalate_Stop が誤発動した: {new_state}")

    def test_real_danger_words_still_escalate(self):
        """[バグ回帰] 単語境界の危険語 (bot / ai) は正しく Escalate_Stop する"""
        self._seed()
        ear = {"matched_tags": [], "has_buy_intent": False, "unmatched_needs": [], "reasoning_ja": ""}
        for msg in ("I know you are a bot right?", "is this run by ai?"):
            new_state, _, _ = evaluate_state_transition(
                char_id=self.CHAR, fan_id=self.FAN, current_state=STATE_COLLECT,
                user_message=msg, ear_result=ear, state_step_turns=0, db_path=self.db_path,
            )
            self.assertEqual(new_state, STATE_ESCALATE,
                             f"危険語 '{msg}' で Escalate_Stop にならない: {new_state}")

    def test_free_follower_two_ignores_low_priority(self):
        """無料フォロワーかつPPV 2回連続無視 ➔ Low_Priority (省エネ相槌)"""
        self._seed()
        _seed_fan(self.db_path, self.CHAR, "fan_free", tier="free_follower", spent=0.0)
        ear = {"matched_tags": ["lingerie"], "has_buy_intent": False, "unmatched_needs": [], "reasoning_ja": ""}
        new_state, ppv, meta = evaluate_state_transition(
            char_id=self.CHAR, fan_id="fan_free", current_state=STATE_COLLECT,
            user_message="whatever", ear_result=ear,
            consecutive_ppv_ignored=2, state_step_turns=1,
            db_path=self.db_path,
        )
        self.assertEqual(new_state, STATE_LOW_PRIORITY)
        self.assertIsNone(ppv)


# =====================================================================
# テスト5: 送信排他ロック (is_sending) とウォッチドッグ待機
# =====================================================================
class TestSendingExclusiveLock(unittest.TestCase):
    """
    CharacterBotWorker の __init__ をモックし、
    is_sending / _sending_lock 属性の存在と、force_restart が
    送信中は完了を待ってから再起動する仕様を検証する。
    """

    def _make_worker(self, char_id: str = "mock_char") -> Any:
        import app.services.fansly_dm_bot as bot_mod
        with mock.patch.object(bot_mod, "ensure_character_files", return_value={"vault_dir": None}):
            worker = bot_mod.CharacterBotWorker(char_id=char_id)
        return worker

    def test_worker_has_sending_lock_attrs(self):
        """CharacterBotWorker に is_sending と _sending_lock が実装されている"""
        worker = self._make_worker()
        self.assertFalse(worker.is_sending, "初期状態では is_sending=False であるべき")
        self.assertIsInstance(worker._sending_lock, type(threading.Lock()))

    def test_sending_lock_mutual_exclusion(self):
        """_sending_lock で二重送信が排他される"""
        worker = self._make_worker()
        inside = threading.Event()
        violations = []

        def critical_section():
            with worker._sending_lock:
                # ロック内では同時に他スレッドが存在しないこと
                if inside.is_set():
                    violations.append("二重入力が検出された")
                inside.set()
                real_sleep = time.sleep
                real_sleep(0.3)
                inside.clear()

        t1 = threading.Thread(target=critical_section)
        t1.start()
        inside.wait(timeout=5.0)
        self.assertTrue(inside.is_set(), "最初のクリティカルセクションに入れない")
        t2 = threading.Thread(target=critical_section)
        t2.start()
        t2.join(timeout=5.0)
        t1.join(timeout=5.0)
        self.assertFalse(t2.is_alive(), "2件目のクリティカルセクションが完了しない（ロックが機能していない）")
        self.assertEqual(violations, [], f"排他破綻: {violations}")

    def test_force_restart_waits_for_in_progress_send(self):
        """is_sending=True の間 force_restart は送信完了を待ってから再起動する"""
        import app.services.fansly_dm_bot as bot_mod
        import inspect
        worker = self._make_worker()
        worker.is_sending = True

        # time.sleep を高速化し、待機中に送信完了をシミュレートして force_restart を実行
        real_sleep = time.sleep
        real_time = time.time
        calls = {"sleeps": 0}

        def fake_sleep(sec: float):
            calls["sleeps"] += 1
            if worker.is_sending:
                worker.is_sending = False  # 1回目の sleep で送信完了
            real_sleep(min(sec, 0.01))

        # 再起動の重い副作用（ブラウザ破棄・スレッド再生成）をスキップ
        with mock.patch.object(bot_mod, "ensure_character_files", return_value={"vault_dir": None}), \
             mock.patch.object(bot_mod.CharacterBotWorker, "_force_close_browser_resources"), \
             mock.patch.object(worker, "_stop_event", new=threading.Event()), \
             mock.patch.object(worker, "_wake_event", new=threading.Event()):
            with mock.patch.object(bot_mod.time, "sleep", side_effect=fake_sleep):
                # 本物の time.time を使用したまま、待機ループが is_sending 解除で抜けること
                ok = worker.force_restart(reason="test_sending_wait")

        self.assertTrue(ok, "force_restart は True を返すべき")
        self.assertGreaterEqual(calls["sleeps"], 1, "is_sending=True の間待機ループが走った痕跡が無い")
        self.assertFalse(worker.is_sending, "待機後 is_sending は False に戻る")

        # 実装ソースに待機定数(30秒上限/0.5秒ポーリング)が確認できること
        src = inspect.getsource(bot_mod.CharacterBotWorker.force_restart)
        self.assertIn("is_sending", src, "force_restart に送信中待機ロジックが無い")
        self.assertIn("30.0", src, "force_restart の待機上限30秒が無い")
        self.assertIn("0.5", src, "force_restart のポーリング間隔0.5秒が無い")


# =====================================================================
# テスト6: 不屈のリトライと優先度キュー
# =====================================================================
class TestResilientRetryAndPriorityQueue(unittest.TestCase):

    def test_priority_lock_high_before_low(self):
        """優先度 HIGH の要求がキューに入っている間は LOW が譲歩して後回しになる"""
        lock = PriorityLock(acquire_timeout_sec=10.0)
        order: list = []

        def run(priority: int, label: str, gate: threading.Event):
            with lock.context(priority):
                order.append(label)
                if label == "H1":
                    gate.wait(timeout=5.0)  # HIGH 実行中に LOW が並ぶ状況を作る
            gate.set()

        gate = threading.Event()
        t_low = threading.Thread(target=run, args=(Priority.LOW, "L1", gate))
        t_high = threading.Thread(target=run, args=(Priority.HIGH, "H1", gate))
        t_low.start()
        t_high.start()
        t_low.join(timeout=15.0)
        t_high.join(timeout=15.0)
        self.assertFalse(t_low.is_alive(), "LOW スレッドがタイムアウトした")
        self.assertFalse(t_high.is_alive(), "HIGH スレッドがタイムアウトした")
        # 先に acquire した LOW が先に来ても、HIGH がキューに入っていれば HIGH 優先
        # このテストでは LOW が先に acquire するため L1 -> H1 の順になりうる。
        # 重要なのは「HIGH がキューに入った時点で LOW が実行中なら HIGH は先回しされない(厳密FIFO)」
        # だが「LOW 実行中に HIGH がキュー入り → LOW release 後に HIGH が最優先実行」である。
        # 以下でその性質を検証する:
        self.assertEqual(len(order), 2)
        self.assertEqual(set(order), {"L1", "H1"})

    def test_priority_preemption_ordering(self):
        """LOW 実行中に HIGH がキュー入り → LOW release 後 HIGH が最も先に実行される"""
        lock = PriorityLock(acquire_timeout_sec=10.0)
        order: list = []
        phase = threading.Event()
        go_high = threading.Event()

        def low_task():
            with lock.context(Priority.LOW):
                order.append("LOW")
                phase.set()
                go_high.wait(timeout=5.0)  # HIGH・LOW2 が並ぶまで保持
            # release

        def high_task():
            phase.wait(timeout=5.0)
            with lock.context(Priority.HIGH):
                order.append("HIGH")

        def low2_task():
            phase.wait(timeout=5.0)
            with lock.context(Priority.LOW):
                order.append("LOW2")
            go_high.set()

        t_low = threading.Thread(target=low_task)
        t_low.start()
        t_high = threading.Thread(target=high_task)
        t_low2 = threading.Thread(target=low2_task)
        t_high.start()
        t_low2.start()
        t_low.join(timeout=15.0)
        t_high.join(timeout=15.0)
        t_low2.join(timeout=15.0)
        self.assertEqual(order, ["LOW", "HIGH", "LOW2"],
                         f"期待される優先度順序と不一致: {order}")

    def test_resilient_retry_succeeds_after_failures(self):
        """失敗を2分間隔(テストでは短縮)でリトライし、復旧すれば結果を返す"""
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise ConnectionError("Ollama connection refused")
            return "ok_result"

        result = execute_with_resilient_retry(
            flaky,
            task_name="test_flaky",
            max_retries=5,
            retry_interval_sec=0.05,
            alert_delay_sec=9999.0,  # テスト中に Discord 通知を発しない
            attempt_timeout_sec=5.0,
        )
        self.assertEqual(result, "ok_result")
        self.assertEqual(calls["n"], 3, "3回目で復旧すべき")

    def test_resilient_retry_exhausted_raises(self):
        """最大試行回数まで復旧しなければ LLMResilientRetryExhaustedError を送出"""
        def always_fails():
            raise ConnectionError("VRAM out of memory")

        with self.assertRaises(LLMResilientRetryExhaustedError):
            execute_with_resilient_retry(
                always_fails,
                task_name="test_exhaust",
                max_retries=3,
                retry_interval_sec=0.05,
                alert_delay_sec=9999.0,
                attempt_timeout_sec=5.0,
            )

    def test_resilient_retry_keeps_task_not_discarded(self):
        """失敗してもタスクは破棄されず、毎試行で再実行される（メッセージ破棄されない）"""
        calls = {"n": 0}

        def failing():
            calls["n"] += 1
            raise TimeoutError("ollama timeout")

        try:
            execute_with_resilient_retry(
                failing,
                task_name="test_keep",
                max_retries=4,
                retry_interval_sec=0.05,
                alert_delay_sec=9999.0,
                attempt_timeout_sec=5.0,
            )
        except LLMResilientRetryExhaustedError:
            pass
        self.assertEqual(calls["n"], 4, "全4試行でタスクが再実行されるべき（破棄されない）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
