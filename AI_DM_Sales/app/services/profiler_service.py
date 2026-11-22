import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Union

from app.core.database import get_fan_record, save_fan_psych_profile_db
from app.core.llm_client import llm_client, execute_with_resilient_retry, LLMResilientRetryExhaustedError


def _parse_existing_profile(existing_profile_json: Union[str, Dict[str, Any], None]) -> Dict[str, Any]:
    """既存の psych_profile_json（文字列または辞書）を安全に辞書へ変換する"""
    if isinstance(existing_profile_json, dict):
        return dict(existing_profile_json)
    if isinstance(existing_profile_json, str) and existing_profile_json.strip():
        try:
            parsed = json.loads(existing_profile_json)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return {}


def _normalize_str_list(val: Any, max_items: int = 8) -> List[str]:
    """文字列や配列から重複のないクリーンな文字列リストを生成する"""
    items: List[str] = []
    if isinstance(val, list):
        raw_list = val
    elif isinstance(val, str):
        raw_list = [x.strip() for x in re.split(r"[,、/;\n]+", val) if x.strip()]
    else:
        raw_list = []

    seen = set()
    for item in raw_list:
        s = str(item or "").strip()
        if not s or s.lower() in ("none", "null", "unknown", "n/a", "-", "未抽出", "未判定"):
            continue
        key = s.lower()
        if key not in seen:
            seen.add(key)
            items.append(s)
        if len(items) >= max_items:
            break
    return items


def _normalize_recent_messages(recent_messages: Any) -> List[Dict[str, str]]:
    """recent_messages を [{"sender": "fan"|"ai", "content": "..."}] に正規化する"""
    normalized: List[Dict[str, str]] = []
    if isinstance(recent_messages, str):
        if recent_messages.strip():
            normalized.append({"sender": "fan", "content": recent_messages.strip()})
        return normalized

    if not isinstance(recent_messages, list):
        return normalized

    for msg in recent_messages[-12:]:
        if isinstance(msg, dict):
            sender = str(msg.get("sender") or msg.get("role") or "fan").strip().lower()
            content = str(msg.get("content") or msg.get("message") or msg.get("text") or "").strip()
            if content:
                normalized.append({"sender": sender, "content": content})
        elif isinstance(msg, str) and msg.strip():
            normalized.append({"sender": "fan", "content": msg.strip()})
    return normalized


def _extract_heuristic_signals(
    messages: List[Dict[str, str]],
    existing: Dict[str, Any],
    total_spent_usd: Optional[float] = None,
    current_tier: Optional[str] = None,
) -> Dict[str, Any]:
    """会話テキストからキーワードシグナルと日本語抽出根拠を抽出し、LLM出力の補完またはフォールバックとして使用する（日本語）"""
    fan_msgs = [
        m["content"] for m in messages if m.get("sender") in ("fan", "user", "test_fan") and m.get("content")
    ]
    latest_fan_msg = fan_msgs[-1] if fan_msgs else (messages[-1]["content"] if messages else "")
    fan_texts = " ".join(fan_msgs).lower()
    all_texts = " ".join(m["content"] for m in messages).lower()
    target_text = fan_texts or all_texts

    detected_prefs: List[str] = []
    matched_pref_clues: List[str] = []
    pref_keywords = [
        (["bedroom", "bed", "sleep", "night", "ベッド", "寝室", "夜"], "寝室・ベッドルーム秘密動画"),
        (["uncensored", "nude", "naked", "full version", "explicit", "無修正", "フル", "裸"], "無修正・フル尺コンテンツ"),
        (["video", "clip", "movie", "watch", "動画", "ムービー"], "限定シークレット動画"),
        (["photo", "pic", "picture", "selfie", "写真", "自撮り", "画像"], "プライベート写真セット"),
        (["cosplay", "outfit", "costume", "lingerie", "stockings", "コスプレ", "ランジェリー", "衣装"], "コスプレ・ランジェリー"),
        (["pov", "close up", "closeup", "主観", "ドアップ"], "主観POV・至近距離アングル"),
        (["feet", "foot", "toes", "足", "足裏"], "美脚・足フェチ"),
        (["sub", "dom", "mistress", "queen", "step on", "obey", "worship", "女王", "ご主人様", "跪", "服従"], "女王様・主従関係"),
        (["sweet", "girlfriend", "gfe", "cute", "love", "kiss", "cuddle", "彼女", "甘え", "可愛い", "すき"], "恋人気分・甘々トーク"),
        (["dirty", "naughty", "tease", "climax", "moan", "エッチ", "焦らし", "絶頂"], "淫らなおねだり・焦らしトーク"),
    ]
    for kws, label in pref_keywords:
        hits = [k for k in kws if k in target_text]
        if hits:
            detected_prefs.append(label)
            matched_pref_clues.append(f"「{hits[0]}」→{label}")

    detected_hooks: List[str] = []
    matched_hook_clues: List[str] = []
    hook_keywords = [
        (["secret", "private", "only for you", "exclusive", "秘密", "特別", "二人だけ"], "二人だけの秘密の共有"),
        (["discount", "special", "deal", "how much", "price", "安", "いくら", "特別価格"], "特別割引・期間限定オファー"),
        (["vip", "upgrade", "gold", "silver", "tier", "plan", "プラン", "上位"], "VIP限定アップグレード特典"),
        (["cute", "gorgeous", "beautiful", "hot", "sexy", "可愛い", "綺麗", "美人", "スタイル"], "褒め言葉への甘いリアクション"),
        (["unlock", "buy", "send", "right now", "want", "今すぐ", "見たい", "買う", "送って"], "即時アンロックの直球提案"),
    ]
    for kws, label in hook_keywords:
        hits = [k for k in kws if k in target_text]
        if hits:
            detected_hooks.append(label)
            matched_hook_clues.append(f"「{hits[0]}」→{label}")

    psych_type = str(
        existing.get("psych_type")
        or existing.get("personality_type")
        or ""
    ).strip()
    psych_reason = "既存プロファイルの心理傾向を継続適用"
    if any(k in target_text for k in ["free", "for free", "no money", "broke", "無料", "タダ"]):
        psych_type = "無料おねだり・価格敏感タイプ"
        psych_reason = "無料志向・価格懸念ワードから「無料おねだり・価格敏感タイプ」と判定"
    elif any(k in target_text for k in ["unlock", "buy", "how much", "send me", "right now", "今すぐ", "買う", "見たい"]):
        psych_type = "即決行動派・ビジュアル重視タイプ"
        psych_reason = "即時購入・視聴意欲ワードから「即決行動派・ビジュアル重視タイプ」と判定"
    elif any(k in target_text for k in ["queen", "goddess", "worship", "obey", "please mistress", "女王", "ご主人様", "命令"]):
        psych_type = "熱狂的従属・忠誠ファン"
        psych_reason = "崇拝・主従志向ワードから「熱狂的従属・忠誠ファン」と判定"
    elif any(k in target_text for k in ["secret", "uncensored", "bedroom", "naughty", "video", "秘密", "無修正", "ベッド"]):
        psych_type = "好奇心旺盛・限定裏コンテンツ探求タイプ"
        psych_reason = "秘密・無修正・ベッドルーム関連ワードから「限定裏コンテンツ探求タイプ」と判定"
    elif any(k in target_text for k in ["cute", "love", "miss you", "followed", "hi", "hello", "可愛い", "好き", "フォロー"]):
        psych_type = psych_type or "親密対話・関係構築重視ファン"
        psych_reason = "好意・挨拶・フォロー報告から「親密対話・関係構築重視ファン」と判定"
    elif not psych_type:
        psych_type = "新規・対話探求型見込み客"
        psych_reason = "序盤メッセージのため対話探求型見込み客として初期設定"

    budget_tier = str(
        existing.get("budget_tier")
        or existing.get("budget_level")
        or ""
    ).strip()
    budget_reason = "既存の課金ティア判定を維持"
    if total_spent_usd is not None:
        if total_spent_usd >= 50.0 or current_tier == "gold_goddess":
            budget_tier = f"高額課金（太客・クジラ層 / 累計 ${total_spent_usd:.2f}）"
            budget_reason = f"累計課金額 ${total_spent_usd:.2f} / プラン '{current_tier}' に基づき太客層と判定"
        elif total_spent_usd > 0.0 or current_tier == "silver_vip":
            budget_tier = f"中間課金（安定リピーター層 / 累計 ${total_spent_usd:.2f}）"
            budget_reason = f"累計課金額 ${total_spent_usd:.2f} / プラン '{current_tier}' の実績からリピーター層と判定"
        else:
            budget_tier = "無料フォロワー（未課金 $0.00 / トリップワイヤー対象）"
            budget_reason = "累計課金額 $0.00 の無料フォロワーのため初回お試し割引対象と判定"
    elif not budget_tier:
        if any(k in target_text for k in ["free", "broke", "expensive", "無料", "高い"]):
            budget_tier = "低予算（割引・低価格トリップワイヤー待ち）"
            budget_reason = "価格への懸念発言から低価格トリップワイヤー対象と判定"
        elif any(k in target_text for k in ["vip", "gold", "unlock all", "anything for you"]):
            budget_tier = "高額課金（太客ポテンシャル $50+）"
            budget_reason = "上位プラン・全開放への強い関心から太客ポテンシャルと判定"
        elif any(k in target_text for k in ["buy", "how much", "unlock", "いくら", "買う"]):
            budget_tier = "標準購買意欲（中価格帯 $10〜$35）"
            budget_reason = "購入意思確認ワードから中価格帯の購買意欲ありと判定"
        else:
            budget_tier = "エントリー層（お試し $5.99〜$9.99）"
            budget_reason = "初期対話のためエントリー層として推定"

    if not detected_prefs:
        detected_prefs = ["気さくなイチャイチャ対話", "限定クリエイター近況"]
    if not detected_hooks:
        detected_hooks = ["個別ネーム呼び・甘えトーク", "裏側シークレットチラ見せ"]

    pref_reason = (
        f"発言シグナル（{', '.join(matched_pref_clues[:3])}）から好みを抽出"
        if matched_pref_clues
        else "明示的なフェチ単語が未出現のため、親密対話ベースで維持"
    )
    hooks_reason = (
        f"発言シグナル（{', '.join(matched_hook_clues[:3])}）から有効フックを抽出"
        if matched_hook_clues
        else "関係構築に有効なネーム呼び・シークレットチラ見せフックを採用"
    )

    return {
        "preferences": detected_prefs,
        "psych_type": psych_type,
        "budget_tier": budget_tier,
        "hooks": detected_hooks,
        "reasoning": {
            "trigger_fan_message": latest_fan_msg[:220],
            "preferences_reason": pref_reason,
            "psych_type_reason": psych_reason,
            "budget_tier_reason": budget_reason,
            "hooks_reason": hooks_reason,
        },
    }




def update_fan_psych_profile(
    char_id: str,
    fan_id: str,
    recent_messages: Any,
    existing_profile_json: Union[str, Dict[str, Any], None] = None,
    total_spent_usd: Optional[float] = None,
    current_tier: Optional[str] = None,
    db_path: Optional[str] = None,
    persist_db: bool = True,
) -> Dict[str, Any]:
    """
    直近の会話履歴と既存の psych_profile_json を LLMClient
    (Ollamaネイティブ /api/generate + "think": False) に渡し、
    顧客の以下4項目を構造化JSONで抽出・マージして返す：
      - 好み・フェチ (preferences)
      - 心理タイプ (psych_type)
      - 推定予算感 (budget_tier)
      - 反応が良い話題 (hooks)
    さらに DB の fans.psych_profile_json を更新する。
    """
    if existing_profile_json is None and char_id and fan_id:
        fan_rec = get_fan_record(char_id, fan_id, db_path=db_path)
        if fan_rec:
            existing_profile = _parse_existing_profile(fan_rec.get("psych_profile_json"))
            if total_spent_usd is None:
                total_spent_usd = float(fan_rec.get("total_spent_usd") or 0.0)
            if current_tier is None:
                current_tier = str(fan_rec.get("current_tier") or "free_follower")
        else:
            existing_profile = {}
    else:
        existing_profile = _parse_existing_profile(existing_profile_json)

    norm_messages = _normalize_recent_messages(recent_messages)
    heuristic = _extract_heuristic_signals(
        norm_messages,
        existing_profile,
        total_spent_usd=total_spent_usd,
        current_tier=current_tier,
    )

    prev_prefs = _normalize_str_list(
        existing_profile.get("preferences") or existing_profile.get("fetishes")
    )
    prev_hooks = _normalize_str_list(
        existing_profile.get("hooks") or existing_profile.get("hot_topics")
    )

    default_profile = {
        "preferences": _normalize_str_list(heuristic["preferences"] + prev_prefs),
        "psych_type": heuristic["psych_type"],
        "budget_tier": heuristic["budget_tier"],
        "hooks": _normalize_str_list(heuristic["hooks"] + prev_hooks),
        "notes": str(existing_profile.get("notes") or "直近のDM会話から自動更新しました。"),
        "reasoning": heuristic.get("reasoning") or {},
    }

    convo_lines = [
        f"{'Fan' if m['sender'] in ('fan', 'user', 'test_fan') else 'Creator'}: {m['content']}"
        for m in norm_messages
    ]
    convo_text = "\n".join(convo_lines) if convo_lines else "No recent messages."

    system_prompt = (
        "You are an expert psychological profiler for a top Fansly creator's DM sales engine.\n"
        "Analyze the recent conversation and merge insights with the existing fan profile.\n"
        "IMPORTANT: You MUST write ALL JSON field values in natural, clear JAPANESE (日本語).\n"
        "Return ONLY a valid JSON object with the exact keys:\n"
        "- \"preferences\": array of short Japanese strings describing the fan's kinks, fetishes, or content preferences (e.g. [\"寝室・ベッドルーム秘密動画\", \"コスプレ・ランジェリー\", \"恋人気分・甘々トーク\"])\n"
        "- \"psych_type\": short Japanese string describing the fan's psychological buyer persona (e.g. \"熱狂的従属・忠誠ファン\", \"即決行動派・ビジュアル重視\", \"慎重な初心者ファン\", \"親密対話・関係構築重視\")\n"
        "- \"budget_tier\": short Japanese string estimating their spending capacity / pricing tier (e.g. \"高額課金（太客・クジラ層）\", \"標準・安定リピーター層\", \"無料フォロワー（トリップワイヤー対象）\")\n"
        "- \"hooks\": array of short Japanese strings describing topics or triggers that excite this fan (e.g. [\"二人だけの秘密の共有\", \"特別割引・限定オファー\", \"個別ネーム呼び・甘えトーク\"])\n"
        "- \"notes\": 1 short sentence summary in Japanese for the sales agent\n"
        "- \"reasoning\": JSON object in Japanese explaining the exact evidence from the fan's statements with keys: "
        "\"preferences_reason\", \"psych_type_reason\", \"budget_tier_reason\", \"hooks_reason\""
    )
    user_prompt = (
        f"Character ID: {char_id}\n"
        f"Fan ID: {fan_id}\n"
        f"Current Tier: {current_tier or 'free_follower'}, Total Spent: ${(total_spent_usd or 0.0):.2f}\n"
        f"Existing Profile JSON: {json.dumps(existing_profile, ensure_ascii=False)}\n"
        f"Recent Conversation:\n{convo_text}\n\n"
        "Extract and merge the updated psychological profile as JSON with all values in clear Japanese."
    )

    try:
        llm_extracted = execute_with_resilient_retry(
            lambda: llm_client.generate_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                default_data=default_profile,
                temperature=0.3,
            ),
            task_name="profiler_generate_json",
        )
    except LLMResilientRetryExhaustedError:
        llm_extracted = dict(default_profile)

    llm_prefs = _normalize_str_list(
        llm_extracted.get("preferences") or llm_extracted.get("fetishes")
    )
    merged_preferences = _normalize_str_list(llm_prefs + heuristic["preferences"] + prev_prefs)
    if not merged_preferences:
        merged_preferences = default_profile["preferences"]

    llm_hooks = _normalize_str_list(
        llm_extracted.get("hooks") or llm_extracted.get("hot_topics")
    )
    merged_hooks = _normalize_str_list(llm_hooks + heuristic["hooks"] + prev_hooks)
    if not merged_hooks:
        merged_hooks = default_profile["hooks"]

    merged_psych_type = str(
        llm_extracted.get("psych_type")
        or llm_extracted.get("personality_type")
        or default_profile["psych_type"]
    ).strip()
    if not merged_psych_type or merged_psych_type.lower() in ("unknown", "none", "null"):
        merged_psych_type = default_profile["psych_type"]

    merged_budget_tier = str(
        llm_extracted.get("budget_tier")
        or llm_extracted.get("budget_level")
        or default_profile["budget_tier"]
    ).strip()
    if not merged_budget_tier or merged_budget_tier.lower() in ("unknown", "none", "null"):
        merged_budget_tier = default_profile["budget_tier"]
    if total_spent_usd is not None and "$" not in merged_budget_tier and "課金" not in merged_budget_tier:
        merged_budget_tier = f"{merged_budget_tier} (累計 ${total_spent_usd:.2f})"

    merged_notes = str(
        llm_extracted.get("notes")
        or existing_profile.get("notes")
        or f"「{', '.join(merged_preferences[:2])}」に関心あり。"
    ).strip()

    now_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    raw_reasoning = llm_extracted.get("reasoning") if isinstance(llm_extracted.get("reasoning"), dict) else {}
    heur_reasoning = heuristic.get("reasoning") or {}
    profile_reasoning = {
        "trigger_fan_message": str(heur_reasoning.get("trigger_fan_message") or ""),
        "preferences_reason": str(
            raw_reasoning.get("preferences_reason") or heur_reasoning.get("preferences_reason") or ""
        ).strip(),
        "psych_type_reason": str(
            raw_reasoning.get("psych_type_reason") or heur_reasoning.get("psych_type_reason") or ""
        ).strip(),
        "budget_tier_reason": str(
            raw_reasoning.get("budget_tier_reason") or heur_reasoning.get("budget_tier_reason") or ""
        ).strip(),
        "hooks_reason": str(
            raw_reasoning.get("hooks_reason") or heur_reasoning.get("hooks_reason") or ""
        ).strip(),
        "updated_at": now_iso,
    }

    final_profile: Dict[str, Any] = {
        "preferences": merged_preferences,
        "psych_type": merged_psych_type,
        "budget_tier": merged_budget_tier,
        "hooks": merged_hooks,
        "fetishes": merged_preferences,
        "personality_type": merged_psych_type,
        "budget_level": merged_budget_tier,
        "hot_topics": merged_hooks,
        "notes": merged_notes,
        "last_profile_reasoning": profile_reasoning,
        "updated_at": now_iso,
    }

    if persist_db and char_id and fan_id:
        save_fan_psych_profile_db(
            char_id=char_id,
            fan_id=fan_id,
            profile_dict=final_profile,
            db_path=db_path,
        )

    return final_profile


def format_psych_profile_for_prompt(profile_data: Union[str, Dict[str, Any], None]) -> str:
    """
    顧客カルテ（psych_profile_json）を次回応答生成時のシステムプロンプトへ
    そのまま注入できる構造化テキストブロックに整形するヘルパー。
    カルテ内容（日本語）をLLMがそのまま理解し、英語DM営業文へ自然に反映できるように構成。
    """
    prof = _parse_existing_profile(profile_data)
    if not prof:
        return (
            "[Fan Psychological Profile (Long-Term Memory)]\n"
            "- Preferences / Fetishes: 未特定 (explore gently)\n"
            "- Psychological Type: 新規ファン (rapport building)\n"
            "- Estimated Budget Tier: 無料・エントリー層\n"
            "- Effective Conversation Hooks: 気さくな挨拶・興味付けの焦らし"
        )

    prefs = _normalize_str_list(prof.get("preferences") or prof.get("fetishes"))
    psych_type = str(prof.get("psych_type") or prof.get("personality_type") or "親密対話・関係構築重視ファン").strip()
    budget_tier = str(prof.get("budget_tier") or prof.get("budget_level") or "標準購買層").strip()
    hooks = _normalize_str_list(prof.get("hooks") or prof.get("hot_topics"))
    notes = str(prof.get("notes") or "").strip()

    prefs_str = ", ".join(prefs) if prefs else "気さくなイチャイチャ対話"
    hooks_str = ", ".join(hooks) if hooks else "二人だけの秘密の共有"

    lines = [
        "[Fan Psychological Profile (Long-Term Memory)]",
        "Note: Read the fan insights below (in Japanese) and tailor your seductive English reply accordingly.",
        f"- Preferences / Fetishes: {prefs_str}",
        f"- Psychological Type: {psych_type}",
        f"- Estimated Budget Tier: {budget_tier}",
        f"- Effective Conversation Hooks: {hooks_str}",
    ]
    if notes:
        lines.append(f"- Sales Notes: {notes}")
    return "\n".join(lines)

