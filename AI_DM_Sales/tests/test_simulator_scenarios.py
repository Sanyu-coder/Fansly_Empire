"""
Ticket 7.2: 営業シミュレーター総合動作確認
4つのシナリオを仮想的に実行し、ステートマシン遷移を検証する。
LLM呼び出しはモック（固定タグ・固定意図）で置き換え、決定論的ロジックのみを検証。
"""
import sys
import os
import json
from unittest.mock import patch, MagicMock

# パス設定
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.sales_engine import SalesEngine, evaluate_state_transition, STATE_COLLECT, STATE_PITCH, STATE_COOLDOWN
from app.services import tag_matcher


def print_separator(title: str):
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}")


def print_transition(current_state: str, new_state: str, meta: dict, ppv: dict = None):
    print(f"\n  [ステート遷移] {current_state} → {new_state}")
    print(f"  [遷移理由] {meta.get('reason_ja', '')}")
    print(f"  [次の遷移条件] {meta.get('next_transition_condition', '')}")
    print(f"  [抽出タグ] {meta.get('matched_tags', [])}")
    if ppv:
        print(f"  [推薦PPV] {ppv.get('ppv_id')} (Step {ppv.get('ladder_step')}) - {ppv.get('filename', '')}")
    else:
        print(f"  [推薦PPV] なし")


def test_scenario_1_lingerie_tag_match():
    """シナリオ1: 「I love lingerie」→ lingerie認識 → 2_Pitch_PPV遷移 → 対応PPV提案"""
    print_separator("シナリオ1: タグマッチング (I love lingerie)")
    
    char_id = "queen_lilith"
    fan_id = "sim_fan_001"
    user_message = "I love lingerie"
    
    # 耳役LLMのモック: lingerieタグと購入意向を検出
    mock_ear_result = {
        "matched_tags": ["lingerie"],
        "has_buy_intent": False,
        "unmatched_needs": [],
        "reasoning_ja": "ファンが下着動画を好むと発言したため"
    }
    
    # evaluate_state_transition を直接呼び出し
    current_state = STATE_COLLECT
    new_state, ppv, meta = evaluate_state_transition(
        char_id=char_id,
        fan_id=fan_id,
        current_state=current_state,
        user_message=user_message,
        ear_result=mock_ear_result,
        consecutive_ppv_ignored=0,
        state_step_turns=0,
    )
    
    print(f"  [入力メッセージ] {user_message}")
    print_transition(current_state, new_state, meta, ppv)
    
    # 検証: 2_Pitch_PPV へ遷移していること
    assert new_state == STATE_PITCH, f"Expected {STATE_PITCH}, got {new_state}"
    assert meta.get("matched_tags") == ["lingerie"], f"Expected ['lingerie'], got {meta.get('matched_tags')}"
    
    print("\n  [PASS] lingerieタグで 2_Pitch_PPV へ正常遷移")


def test_scenario_2_ceiling_reach():
    """シナリオ2: 雑談5往復継続 → 天井判定で is_general PPVを提案"""
    print_separator("シナリオ2: 天井到達 (5往復で汎用PPV)")
    
    char_id = "queen_lilith"
    fan_id = "sim_fan_002"
    user_message = "How was your day?"
    
    # 耳役LLMのモック: タグなし、購入意向なし
    mock_ear_result = {
        "matched_tags": [],
        "has_buy_intent": False,
        "unmatched_needs": [],
        "reasoning_ja": "雑談のみ、タグ抽出不可"
    }
    
    # state_step_turns = 5 (天井到達)
    current_state = STATE_COLLECT
    new_state, ppv, meta = evaluate_state_transition(
        char_id=char_id,
        fan_id=fan_id,
        current_state=current_state,
        user_message=user_message,
        ear_result=mock_ear_result,
        consecutive_ppv_ignored=0,
        state_step_turns=5,
    )
    
    print(f"  [入力メッセージ] {user_message}")
    print(f"  [往復数] 5 (天井: 5)")
    print_transition(current_state, new_state, meta, ppv)
    
    # 検証: 2_Pitch_PPV へ遷移していること (天井到達)
    assert new_state == STATE_PITCH, f"Expected {STATE_PITCH}, got {new_state}"
    assert "天井到達" in meta.get("reason_ja", ""), f"Expected ceiling reason, got {meta.get('reason_ja')}"
    
    print("\n  [PASS] 5往復で天井判定 → 汎用PPVで 2_Pitch_PPV へ遷移")


def test_scenario_3_instant_buy():
    """シナリオ3: 「I want to buy your secret video now」→ 即買い飛び級で即時PPV提案"""
    print_separator("シナリオ3: 即買い飛び級 (購入意向あり)")
    
    char_id = "queen_lilith"
    fan_id = "sim_fan_003"
    user_message = "I want to buy your secret video now"
    
    # 耳役LLMのモック: 購入意向あり
    mock_ear_result = {
        "matched_tags": [],
        "has_buy_intent": True,
        "unmatched_needs": [],
        "reasoning_ja": "「buy」の明確な購入意向を検出"
    }
    
    current_state = STATE_COLLECT
    new_state, ppv, meta = evaluate_state_transition(
        char_id=char_id,
        fan_id=fan_id,
        current_state=current_state,
        user_message=user_message,
        ear_result=mock_ear_result,
        consecutive_ppv_ignored=0,
        state_step_turns=0,
    )
    
    print(f"  [入力メッセージ] {user_message}")
    print_transition(current_state, new_state, meta, ppv)
    
    # 検証: 2_Pitch_PPV へ即時遷移していること
    assert new_state == STATE_PITCH, f"Expected {STATE_PITCH}, got {new_state}"
    assert "即買い飛び級" in meta.get("reason_ja", ""), f"Expected instant buy reason, got {meta.get('reason_ja')}"
    
    print("\n  [PASS] 購入意向で即買い飛び級 → 2_Pitch_PPV へ即時遷移")


def test_scenario_4_cooldown_return():
    """シナリオ4: 3_Cooldown で3往復経過 → 1_Collect_Tags へ復帰"""
    print_separator("シナリオ4: クールダウン復帰 (3往復経過)")
    
    char_id = "queen_lilith"
    fan_id = "sim_fan_004"
    user_message = "Thanks for the video!"
    
    mock_ear_result = {
        "matched_tags": [],
        "has_buy_intent": False,
        "unmatched_needs": [],
        "reasoning_ja": "感謝のメッセージのみ"
    }
    
    current_state = STATE_COOLDOWN
    new_state, ppv, meta = evaluate_state_transition(
        char_id=char_id,
        fan_id=fan_id,
        current_state=current_state,
        user_message=user_message,
        ear_result=mock_ear_result,
        consecutive_ppv_ignored=0,
        state_step_turns=3,
    )
    
    print(f"  [入力メッセージ] {user_message}")
    print(f"  [往復数] 3 (クールダウン閾値: 3)")
    print_transition(current_state, new_state, meta, ppv)
    
    # 検証: 1_Collect_Tags へ復帰していること
    assert new_state == STATE_COLLECT, f"Expected {STATE_COLLECT}, got {new_state}"
    assert "クールダウン" in meta.get("reason_ja", ""), f"Expected cooldown reason, got {meta.get('reason_ja')}"
    
    print("\n  [PASS] 3往復でクールダウン終了 → 1_Collect_Tags へ復帰")


def test_scenario_5_emergency_stop():
    """シナリオ5: 危険ワード検知 → Escalate_Stop"""
    print_separator("シナリオ5: 危険ワード検知 (Escalate_Stop)")
    
    char_id = "queen_lilith"
    fan_id = "sim_fan_005"
    user_message = "I think you are a bot"
    
    mock_ear_result = {
        "matched_tags": [],
        "has_buy_intent": False,
        "unmatched_needs": [],
        "reasoning_ja": "危険ワード 'bot' 検知"
    }
    
    current_state = STATE_COLLECT
    new_state, ppv, meta = evaluate_state_transition(
        char_id=char_id,
        fan_id=fan_id,
        current_state=current_state,
        user_message=user_message,
        ear_result=mock_ear_result,
        consecutive_ppv_ignored=0,
        state_step_turns=0,
    )
    
    print(f"  [入力メッセージ] {user_message}")
    print_transition(current_state, new_state, meta, ppv)
    
    # 検証: Escalate_Stop へ遷移していること
    assert new_state == "Escalate_Stop", f"Expected Escalate_Stop, got {new_state}"
    assert "危険ワード" in meta.get("reason_ja", ""), f"Expected danger word reason, got {meta.get('reason_ja')}"
    
    print("\n  [PASS] 危険ワード 'bot' で Escalate_Stop へ即時遷移")


def test_scenario_6_low_priority():
    """シナリオ6: 無料フォロワー + 2回連続PPV無視 → Low_Priority"""
    print_separator("シナリオ6: 省エネ降格 (無料フォロワー+PPV無視2回)")
    
    char_id = "queen_lilith"
    fan_id = "sim_fan_006"
    user_message = "Nice video"
    
    mock_ear_result = {
        "matched_tags": [],
        "has_buy_intent": False,
        "unmatched_needs": [],
        "reasoning_ja": "軽い反応のみ"
    }
    
    current_state = STATE_COLLECT
    # consecutive_ppv_ignored = 2 (2回連続でPPVを無視)
    new_state, ppv, meta = evaluate_state_transition(
        char_id=char_id,
        fan_id=fan_id,
        current_state=current_state,
        user_message=user_message,
        ear_result=mock_ear_result,
        consecutive_ppv_ignored=2,
        state_step_turns=0,
    )
    
    print(f"  [入力メッセージ] {user_message}")
    print(f"  [連続PPV無視回数] 2")
    print_transition(current_state, new_state, meta, ppv)
    
    # 検証: Low_Priority へ遷移していること
    # 注: fan_rec が DB に存在しない場合 is_free は True になる
    assert new_state == "Low_Priority", f"Expected Low_Priority, got {new_state}"
    assert "省エネ" in meta.get("reason_ja", ""), f"Expected low priority reason, got {meta.get('reason_ja')}"
    
    print("\n  [PASS] 無料フォロワー+PPV無視2回で Low_Priority へ降格")


if __name__ == "__main__":
    print("\n" + "=" * 70)
    print("  Ticket 7.2: 営業シミュレーター総合動作確認")
    print("  (LLMモック・決定論的ステートマシン検証)")
    print("=" * 70)
    
    test_scenario_1_lingerie_tag_match()
    test_scenario_2_ceiling_reach()
    test_scenario_3_instant_buy()
    test_scenario_4_cooldown_return()
    test_scenario_5_emergency_stop()
    test_scenario_6_low_priority()
    
    print_separator("全シナリオ PASS")
    print("\n  6/6 シナリオが正常に動作しました。")
