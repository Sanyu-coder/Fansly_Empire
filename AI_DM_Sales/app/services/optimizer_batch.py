import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.core.database import (
    compute_ab_cvr_stats,
    ensure_default_playbook_a,
    get_active_playbooks,
    get_db_connection,
    get_dormant_fans,
    get_system_stage_thresholds,
    get_tip_stats,
    normalize_stage_thresholds,
    promote_playbook_b_to_a,
    save_new_playbook,
    save_system_stage_thresholds,
)
from app.core.llm_client import llm_client, execute_with_resilient_retry, LLMResilientRetryExhaustedError
from app.services.character_loader import list_character_ids, load_dm_persona, save_settings

logger = logging.getLogger("AI_DM_Sales.OptimizerBatch")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)


def _resolve_target_char_id(char_id: Optional[str]) -> str:
    if char_id and str(char_id).strip() and str(char_id).strip() != "all":
        return str(char_id).strip()
    chars = list_character_ids()
    return chars[0] if chars else "Sylphy-Mia"


def extract_win_loss_threads(
    char_id: str,
    max_threads_each: int = 8,
    max_msgs_per_thread: int = 10,
    db_path: Optional[str] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """
    empire_core.db から対象キャラの
    - 勝ちパターン (PPV成約・課金に至った会話スレッド)
    - 負けパターン (離脱・2回連続スルー・省エネ降格・PPV未開封放置に至った会話スレッド)
    を抽出して返す。
    """
    conn = get_db_connection(db_path)
    try:
        win_fan_rows = conn.execute(
            """
            SELECT f.*
            FROM fans f
            WHERE f.char_id = ?
              AND (
                  COALESCE(f.total_spent_usd, 0.0) > 0.0
                  OR EXISTS (
                      SELECT 1 FROM ppv_transactions t
                      WHERE t.char_id = f.char_id AND t.fan_id = f.fan_id AND t.is_unlocked = 1
                  )
              )
            ORDER BY f.total_spent_usd DESC, f.last_contact_at DESC
            LIMIT ?;
            """,
            (char_id, int(max_threads_each)),
        ).fetchall()

        loss_fan_rows = conn.execute(
            """
            SELECT f.*
            FROM fans f
            WHERE f.char_id = ?
              AND COALESCE(f.total_spent_usd, 0.0) <= 0.0
              AND NOT EXISTS (
                  SELECT 1 FROM ppv_transactions t
                  WHERE t.char_id = f.char_id AND t.fan_id = f.fan_id AND t.is_unlocked = 1
              )
              AND (
                  COALESCE(f.is_low_priority, 0) = 1
                  OR COALESCE(f.consecutive_ppv_ignored, 0) >= 2
                  OR COALESCE(f.consecutive_unanswered, 0) >= 2
                  OR EXISTS (
                      SELECT 1 FROM ppv_transactions t2
                      WHERE t2.char_id = f.char_id AND t2.fan_id = f.fan_id AND t2.is_unlocked = 0
                  )
              )
            ORDER BY f.consecutive_ppv_ignored DESC, f.consecutive_unanswered DESC, f.last_contact_at DESC
            LIMIT ?;
            """,
            (char_id, int(max_threads_each)),
        ).fetchall()

        def _hydrate(rows: List[Any], label: str) -> List[Dict[str, Any]]:
            out: List[Dict[str, Any]] = []
            for r in rows:
                fd = dict(r)
                fid = str(fd.get("fan_id") or "")
                msgs = conn.execute(
                    """
                    SELECT sender, content, content_ja, sales_phase, prompt_version, decision_log_json, created_at
                    FROM dm_messages
                    WHERE char_id = ? AND fan_id = ?
                    ORDER BY msg_id DESC
                    LIMIT ?;
                    """,
                    (char_id, fid, int(max_msgs_per_thread)),
                ).fetchall()
                msg_list: List[Dict[str, Any]] = []
                msg_decision_logs: List[Dict[str, Any]] = []
                for m in reversed(msgs):
                    md = dict(m)
                    raw_dlog = md.get("decision_log_json")
                    if raw_dlog:
                        try:
                            parsed_dlog = json.loads(raw_dlog) if isinstance(raw_dlog, str) else raw_dlog
                            if isinstance(parsed_dlog, dict):
                                md["decision_log"] = parsed_dlog
                                msg_decision_logs.append(parsed_dlog)
                        except Exception:
                            pass
                    msg_list.append(md)

                fan_decision_logs: List[Dict[str, Any]] = []
                raw_fan_logs = fd.get("decision_log_json")
                if raw_fan_logs:
                    try:
                        parsed_fl = json.loads(raw_fan_logs) if isinstance(raw_fan_logs, str) else raw_fan_logs
                        if isinstance(parsed_fl, list):
                            fan_decision_logs = [x for x in parsed_fl if isinstance(x, dict)]
                        elif isinstance(parsed_fl, dict):
                            fan_decision_logs = [parsed_fl]
                    except Exception:
                        pass

                psych_prof: Dict[str, Any] = {}
                try:
                    psych_prof = json.loads(fd.get("psych_profile_json") or "{}")
                except Exception:
                    psych_prof = {}

                combined_logs: List[Dict[str, Any]] = []
                if msg_decision_logs and fan_decision_logs:
                    seen_keys = set()
                    for lg in msg_decision_logs + fan_decision_logs:
                        rec_key = (
                            str(lg.get("recorded_at") or ""),
                            str(lg.get("fan_message_en") or ""),
                            str(lg.get("current_stage") or (lg.get("strategy_reasoning") or {}).get("current_stage") or ""),
                        )
                        if rec_key not in seen_keys:
                            seen_keys.add(rec_key)
                            combined_logs.append(lg)
                else:
                    combined_logs = msg_decision_logs if msg_decision_logs else fan_decision_logs

                stage_scores: List[int] = []
                stages_visited: List[str] = []
                promotions_cnt = 0
                for lg in combined_logs:
                    sr_lg = lg.get("strategy_reasoning") if isinstance(lg.get("strategy_reasoning"), dict) else {}
                    c_st = lg.get("current_stage") or sr_lg.get("current_stage")
                    s_sc = lg.get("stage_score") if lg.get("stage_score") is not None else sr_lg.get("stage_score")
                    if c_st and str(c_st) not in stages_visited:
                        stages_visited.append(str(c_st))
                    if s_sc is not None:
                        try:
                            stage_scores.append(int(round(float(s_sc))))
                        except Exception:
                            pass
                    if bool(lg.get("stage_promoted") or sr_lg.get("stage_promoted")):
                        promotions_cnt += 1

                stage_summary = {
                    "latest_stage_score": stage_scores[-1] if stage_scores else None,
                    "peak_stage_score": max(stage_scores) if stage_scores else None,
                    "stages_visited": stages_visited,
                    "promotions_count": promotions_cnt,
                    "msg_decision_logs_count": len(msg_decision_logs),
                    "fan_decision_logs_count": len(fan_decision_logs),
                }

                out.append(
                    {
                        "pattern_type": label,
                        "fan_id": fid,
                        "username": fd.get("username") or fid,
                        "current_tier": fd.get("current_tier") or "free_follower",
                        "total_spent_usd": float(fd.get("total_spent_usd") or 0.0),
                        "sales_phase": fd.get("sales_phase") or "1_Icebreak",
                        "consecutive_unanswered": int(fd.get("consecutive_unanswered") or 0),
                        "consecutive_ppv_ignored": int(fd.get("consecutive_ppv_ignored") or 0),
                        "is_low_priority": int(fd.get("is_low_priority") or 0),
                        "ab_group": fd.get("ab_group") or "A",
                        "psych_profile": psych_prof,
                        "decision_logs": combined_logs[-5:],
                        "stage_summary": stage_summary,
                        "messages": msg_list,
                    }
                )
            return out

        return {
            "winning_threads": _hydrate(win_fan_rows, "win"),
            "losing_threads": _hydrate(loss_fan_rows, "loss"),
        }
    finally:
        conn.close()


def _extract_stage_fields_from_log(dl: Dict[str, Any]) -> Dict[str, Any]:
    sr = dl.get("strategy_reasoning") if isinstance(dl.get("strategy_reasoning"), dict) else {}
    return {
        "current_stage": dl.get("current_stage") or sr.get("current_stage"),
        "next_stage": dl.get("next_stage") or sr.get("next_stage"),
        "stage_score": dl.get("stage_score") if dl.get("stage_score") is not None else sr.get("stage_score"),
        "stage_threshold": (
            dl.get("stage_threshold") if dl.get("stage_threshold") is not None else sr.get("stage_threshold")
        ),
        "remaining_points": (
            dl.get("remaining_points") if dl.get("remaining_points") is not None else sr.get("remaining_points")
        ),
        "earned_points": dl.get("earned_points") if dl.get("earned_points") is not None else sr.get("earned_points"),
        "score_reason_ja": dl.get("score_reason_ja") or sr.get("score_reason_ja"),
    }


def _summarize_stage_progression_stats(
    win_threads: List[Dict[str, Any]],
    loss_threads: List[Dict[str, Any]],
    current_thresholds: Dict[str, int],
) -> Dict[str, Any]:
    def _calc_group(threads: List[Dict[str, Any]]) -> Dict[str, Any]:
        peaks: List[int] = []
        stalled_explore = 0
        reached_pitch = 0
        for t in threads:
            ss = t.get("stage_summary") or {}
            pk = ss.get("peak_stage_score")
            if pk is not None:
                peaks.append(int(pk))
            visited = set(ss.get("stages_visited") or [])
            cur_ph = str(t.get("sales_phase") or "")
            if cur_ph:
                visited.add(cur_ph)
            if any(p in visited for p in ("3_Lead_Pitch", "4_Lock_Close", "4_Closing")):
                reached_pitch += 1
            elif any(p in visited for p in ("2_Explore", "2_Discovery")):
                stalled_explore += 1
        return {
            "threads_count": len(threads),
            "avg_peak_stage_score": round(sum(peaks) / len(peaks), 1) if peaks else 0.0,
            "stalled_in_explore_count": stalled_explore,
            "reached_pitch_count": reached_pitch,
        }

    return {
        "current_thresholds": dict(current_thresholds),
        "win": _calc_group(win_threads),
        "loss": _calc_group(loss_threads),
    }


def _format_threads_for_llm(threads: List[Dict[str, Any]], title: str) -> str:
    if not threads:
        return f"[{title}]\n(No threads recorded yet)"
    blocks: List[str] = [f"[{title}]"]
    for idx, t in enumerate(threads, start=1):
        prof = t.get("psych_profile") or {}
        prefs = ", ".join((prof.get("preferences") or [])[:4]) or "未抽出"
        ss = t.get("stage_summary") or {}
        peak_pt = ss.get("peak_stage_score")
        score_meta = f", peak_score={peak_pt}pt" if peak_pt is not None else ""
        meta = (
            f"Thread #{idx} (@{t.get('username')}, AB={t.get('ab_group', 'A')}, tier={t.get('current_tier')}, "
            f"spent=${t.get('total_spent_usd', 0.0):.2f}, phase={t.get('sales_phase')}{score_meta}, "
            f"ppv_ignored={t.get('consecutive_ppv_ignored')}, unanswered={t.get('consecutive_unanswered')}, "
            f"psych_type='{prof.get('psych_type', '-')}', prefs=[{prefs}])"
        )
        msg_lines = [
            f"  {'Fan' if m.get('sender') == 'fan' else 'Creator'}: {str(m.get('content') or '')[:180]}"
            for m in (t.get("messages") or [])[-6:]
        ]
        dlog_lines: List[str] = []
        for d_idx, dl in enumerate((t.get("decision_logs") or [])[-3:], start=1):
            pr = dl.get("profile_reasoning") or {}
            sr = dl.get("strategy_reasoning") or {}
            ab_info = sr.get("ab_rule_applied") or {}
            sf = _extract_stage_fields_from_log(dl)
            stage_score_part = ""
            if sf.get("stage_score") is not None and sf.get("stage_threshold") is not None:
                e_pts = sf.get("earned_points")
                e_str = f"{int(e_pts):+d}pt" if e_pts is not None else "0pt"
                stage_score_part = (
                    f" | ステージ突破スコア=({sf.get('current_stage', '-')}→次{sf.get('next_stage', '-')}: "
                    f"{sf['stage_score']}/{sf['stage_threshold']}pt [残{sf.get('remaining_points', 0)}pt], "
                    f"{e_str}: {sf.get('score_reason_ja', '-')})"
                )
            dlog_lines.append(
                f"  [AI Decision Log #{d_idx}] "
                f"カルテ解釈=({pr.get('psych_type_reason', '-')} / {pr.get('preferences_reason', '-')}) | "
                f"戦略=({sr.get('phase_transition', '-')}: {sr.get('phase_reason_ja', '-')}){stage_score_part} | "
                f"PPV/チップ判定=({sr.get('monetization_judgment_ja', sr.get('ladder_step_reason_ja', '-'))}) | "
                f"AB群={ab_info.get('prompt_version', t.get('ab_group', 'A'))} | "
                f"返信意図=({sr.get('reply_intent_summary_ja', '-')})"
            )
        section = meta
        section += "\n" + ("\n".join(msg_lines) if msg_lines else "  (No message text)")
        if dlog_lines:
            section += "\n" + "\n".join(dlog_lines)
        blocks.append(section)
    return "\n\n".join(blocks)


def _normalize_rules_field(val: Any) -> str:
    if isinstance(val, list):
        cleaned = [str(x).strip() for x in val if str(x).strip()]
        return "\n".join(
            f"{i + 1}. {line.lstrip('0123456789.-) ')}" for i, line in enumerate(cleaned)
        )
    return val.strip() if isinstance(val, str) else ""


def run_playbook_optimization(
    char_id: Optional[str],
    db_path: Optional[str] = None,
    llm=None,
    auto_tune_thresholds: bool = False,
) -> Dict[str, Any]:
    """
    比較分析＆新プレイブック生成：
    勝ちパターン（課金・成約）と負けパターン（離脱・連続スルー・省エネ降格）および
    S.E.L.L. ステージ突破スコア推移をローカルOllamaで比較分析し、
    営業鉄則プロンプトルール (rules_text) + 日本語解説 (rules_ja) を ab_group = 'B' として保存する。
    """
    target_char = _resolve_target_char_id(char_id)
    client = llm or llm_client

    ensure_default_playbook_a(target_char, db_path=db_path)
    persona = load_dm_persona(target_char)
    display_name = persona.get("display_name") or target_char
    tone_style = persona.get("tone_and_style") or "Alluring, playful, affectionate"
    current_thresholds = get_system_stage_thresholds(db_path=db_path)

    extracted = extract_win_loss_threads(target_char, db_path=db_path)
    win_threads = extracted["winning_threads"]
    loss_threads = extracted["losing_threads"]
    stage_metrics = _summarize_stage_progression_stats(win_threads, loss_threads, current_thresholds)

    win_block = _format_threads_for_llm(win_threads, "WINNING THREADS (Unlocked PPV / Paid)")
    loss_block = _format_threads_for_llm(loss_threads, "LOSING THREADS (Ignored PPV / Ghosted / Demoted)")

    fallback_summary_ja = (
        f"勝ちスレッド({len(win_threads)}件・平均最高スコア{stage_metrics['win']['avg_peak_stage_score']}pt)では"
        f"好意加点を積み上げて {current_thresholds['2_Explore']}pt 突破後に限定提案が成約に繋がり、"
        f"負けスレッド({len(loss_threads)}件・2_Explore停滞{stage_metrics['loss']['stalled_in_explore_count']}件)では"
        f"関係構築前の早期売り込みや単調な相槌が離脱要因となっています。"
    )
    fallback_rules_en = (
        "1. Build 2 turns of playful curiosity around the fan's specific fetish before revealing a PPV price.\n"
        "2. Describe the sensory mood of the secret clip in 1 vivid sentence instead of a generic pitch.\n"
        "3. Frame first-time offers as an exclusive limited-time secret discount just for them.\n"
        "4. If a fan hesitates on price, pivot to affectionate rapport instead of repeating the offer."
    )
    fallback_rules_ja = (
        "1. 価格提示の前に、顧客固有のフェチ・好みに触れる短い焦らしを挟んで期待感を高める。\n"
        "2. 定型的な売り文句を避け、動画・画像の空気感を1文で鮮明に描写する。\n"
        "3. 未課金ファンには「今だけあなた限定の特別割引」である希少性を強調する。\n"
        "4. 価格への躊躇が見えたら即座に売り込みを引き、甘い雑談へ切り替えて離脱を防ぐ。"
    )

    decision_logs_count = sum(len(t.get("decision_logs") or []) for t in (win_threads + loss_threads))
    thresholds_context = (
        f"[Current S.E.L.L. Stage Breakthrough Thresholds]\n"
        f"- 1_Icebreak -> 2_Explore: {current_thresholds['1_Icebreak']}pt\n"
        f"- 2_Explore -> 3_Lead_Pitch: {current_thresholds['2_Explore']}pt\n"
        f"- 3_Lead_Pitch -> 4_Lock_Close: {current_thresholds['3_Lead_Pitch']}pt\n"
        f"- Stage Stats: Win avg peak={stage_metrics['win']['avg_peak_stage_score']}pt, "
        f"Loss avg peak={stage_metrics['loss']['avg_peak_stage_score']}pt "
        f"(stalled in 2_Explore={stage_metrics['loss']['stalled_in_explore_count']})"
    )

    system_prompt = (
        "You are an elite Fansly DM Conversion Rate Optimization (CRO) & AI Decision Auditor.\n"
        f"Analyze creator '{display_name}' (style: {tone_style}) by comparing WINNING vs LOSING DM threads.\n"
        "IMPORTANT: Each thread includes not only the raw conversation, but also [AI Decision Log] showing WHY the AI "
        "interpreted the fan's psychological profile that way, its S.E.L.L. stage breakthrough score progression "
        "(stage_score / stage_threshold / earned_points), and WHY it selected a given sales phase, PPV step, pricing, or tip hook.\n"
        "Identify any biases/mistakes in the AI's own decision-making habits (e.g., stalling in 2_Explore, pitching PPV before emotional rapport, "
        "misreading price-sensitive fans, under-using specific fetish hooks) and correct them in the next-generation Playbook B rules.\n"
        "Return STRICT JSON with exact keys:\n"
        '1. "analysis_summary_ja": Concise Japanese summary (2-4 sentences) comparing wins vs losses, stage breakthrough score trends, AND diagnosing AI decision habits.\n'
        '2. "rules_text": 4 numbered, actionable English system-prompt rules that correct AI decision biases and raise PPV/Tip CVR in character.\n'
        '3. "rules_ja": Clear numbered Japanese explanation matching rules_text.\n'
        '4. "recommended_stage_thresholds": Optional object {"1_Icebreak": int, "2_Explore": int, "3_Lead_Pitch": int} (10-100).'
    )
    try:
        llm_res = execute_with_resilient_retry(
            lambda: client.generate_json(
                system_prompt=system_prompt,
                user_prompt=(
                    f"{thresholds_context}\n\n{win_block}\n\n{loss_block}\n\n"
                    "Output optimization JSON reflecting conversation + S.E.L.L. stage score decision log analysis now:"
                ),
                default_data={
                    "analysis_summary_ja": fallback_summary_ja,
                    "rules_text": fallback_rules_en,
                    "rules_ja": fallback_rules_ja,
                    "recommended_stage_thresholds": dict(current_thresholds),
                },
                temperature=0.45,
                max_tokens=900,
            ),
            task_name="optimizer_generate_json",
        )
    except LLMResilientRetryExhaustedError:
        llm_res = {
            "analysis_summary_ja": fallback_summary_ja,
            "rules_text": fallback_rules_en,
            "rules_ja": fallback_rules_ja,
            "recommended_stage_thresholds": dict(current_thresholds),
        }

    rules_text = _normalize_rules_field(llm_res.get("rules_text") or llm_res.get("rules_en")) or fallback_rules_en
    rules_ja = _normalize_rules_field(llm_res.get("rules_ja")) or fallback_rules_ja
    analysis_summary_ja = str(llm_res.get("analysis_summary_ja") or fallback_summary_ja).strip()
    rec_thresh_raw = llm_res.get("recommended_stage_thresholds")
    recommended_thresholds = normalize_stage_thresholds(
        raw_thresholds=rec_thresh_raw if isinstance(rec_thresh_raw, dict) else None,
        fallback=current_thresholds,
    )

    applied_thresholds = current_thresholds
    if auto_tune_thresholds:
        applied_thresholds = save_system_stage_thresholds(thresholds=recommended_thresholds, db_path=db_path)
        for cid in list_character_ids():
            save_settings(
                cid,
                {
                    "stage_thresholds": applied_thresholds,
                    "stage_threshold_icebreak": applied_thresholds["1_Icebreak"],
                    "stage_threshold_explore": applied_thresholds["2_Explore"],
                    "stage_threshold_lead_pitch": applied_thresholds["3_Lead_Pitch"],
                },
            )

    saved_b = save_new_playbook(
        char_id=target_char,
        rules_text=rules_text,
        ab_group="B",
        rules_ja=rules_ja,
        analysis_summary_ja=analysis_summary_ja,
        cvr_score=0.0,
        deactivate_same_group=True,
        db_path=db_path,
    )
    stats = compute_ab_cvr_stats(target_char, db_path=db_path)
    playbooks = get_active_playbooks(target_char, db_path=db_path)
    logger.info(f"[{target_char}] Saved new Playbook B (v{saved_b.get('version_id')})")
    return {
        "ok": True,
        "char_id": target_char,
        "win_threads_analyzed": len(win_threads),
        "loss_threads_analyzed": len(loss_threads),
        "decision_logs_analyzed_count": decision_logs_count,
        "stage_thresholds": applied_thresholds,
        "recommended_stage_thresholds": recommended_thresholds,
        "stage_progression_metrics": stage_metrics,
        "analysis_summary_ja": analysis_summary_ja,
        "new_playbook_b": playbooks.get("B") or saved_b,
        "playbook_a": playbooks.get("A"),
        "ab_stats": stats,
    }


def evaluate_and_promote_ab_test(
    char_id: Optional[str],
    min_samples: int = 10,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    A/Bテスト CVR集計・自動昇格：
    - 各グループの送信数 (offers_count) と成約数 (purchases_count, is_unlocked == 1) から cvr_score を算出
    - 各群 min_samples (デフォルト10件) 以上に達し、かつ B の CVR が A を上回った場合、
      B を新しい A (現行本番ルール) へ自動昇格させる
    """
    target_char = _resolve_target_char_id(char_id)
    ensure_default_playbook_a(target_char, db_path=db_path)

    playbooks_before = get_active_playbooks(target_char, db_path=db_path)
    stats = compute_ab_cvr_stats(target_char, db_path=db_path)

    a_stats = stats["A"]
    b_stats = stats["B"]
    active_b = playbooks_before.get("B")

    promoted = False
    promoted_playbook: Optional[Dict[str, Any]] = None
    reason = "サンプル収集継続中"

    if not active_b:
        reason = "現在アクティブな検証ルール (Group B) が未生成です"
    elif a_stats["offers_count"] < int(min_samples) or b_stats["offers_count"] < int(min_samples):
        reason = (
            f"サンプル数未達 (必要: 各群{min_samples}件以上 / "
            f"現在 A={a_stats['offers_count']}件, B={b_stats['offers_count']}件)"
        )
    elif b_stats["cvr_score"] > a_stats["cvr_score"]:
        promoted_playbook = promote_playbook_b_to_a(
            char_id=target_char,
            b_version_id=int(active_b["version_id"]),
            b_cvr_score=b_stats["cvr_score"],
            b_offers_count=b_stats["offers_count"],
            b_purchases_count=b_stats["purchases_count"],
            db_path=db_path,
        )
        promoted = promoted_playbook is not None
        reason = (
            f"Group B (v{active_b['version_id']}) の CVR ({b_stats['cvr_score']:.2f}%) が "
            f"Group A ({a_stats['cvr_score']:.2f}%) を上回ったため、新 Group A (本番ルール) へ自動昇格しました！"
        )
        logger.info(f"[{target_char}] AUTO-PROMOTED Playbook B v{active_b['version_id']} -> Group A!")
    else:
        reason = (
            f"現行 Group A ({a_stats['cvr_score']:.2f}%) が Group B ({b_stats['cvr_score']:.2f}%) 以上のため、"
            "現行 Group A を維持します"
        )

    playbooks_after = get_active_playbooks(target_char, db_path=db_path)
    return {
        "ok": True,
        "char_id": target_char,
        "min_samples": int(min_samples),
        "promoted": promoted,
        "reason": reason,
        "promoted_playbook": promoted_playbook,
        "ab_stats": stats,
        "playbook_a": playbooks_after.get("A"),
        "playbook_b": playbooks_after.get("B"),
    }


def get_evolution_status(
    char_id: Optional[str] = None,
    inactive_days: int = 7,
    min_samples: int = 10,
    now_dt: Optional[datetime] = None,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """管理画面 GET /api/evolution/status 用の統合ステータス辞書を構築する"""
    target_char = _resolve_target_char_id(char_id)
    ensure_default_playbook_a(target_char, db_path=db_path)
    stats = compute_ab_cvr_stats(target_char, db_path=db_path)
    playbooks = get_active_playbooks(target_char, db_path=db_path)
    tip_stats = get_tip_stats(char_id if char_id == "all" else target_char, db_path=db_path)

    dormant_scope = char_id if (char_id and char_id != "all") else target_char
    dormant_list = get_dormant_fans(
        char_id=dormant_scope,
        inactive_days=inactive_days,
        max_targets=50,
        now_dt=now_dt,
        db_path=db_path,
    )

    a_cvr = stats["A"]["cvr_score"]
    b_cvr = stats["B"]["cvr_score"]
    leader = "B" if b_cvr > a_cvr else ("A" if a_cvr > b_cvr else "tie")
    ready = (
        playbooks.get("B") is not None
        and stats["A"]["offers_count"] >= int(min_samples)
        and stats["B"]["offers_count"] >= int(min_samples)
        and b_cvr > a_cvr
    )

    return {
        "ok": True,
        "char_id": target_char,
        "playbook_a": playbooks.get("A"),
        "playbook_b": playbooks.get("B"),
        "ab_stats": stats,
        "ab_comparison": {
            "min_samples": int(min_samples),
            "leader": leader,
            "ready_for_promotion": ready,
            "a_cvr": a_cvr,
            "b_cvr": b_cvr,
            "a_offers": stats["A"]["offers_count"],
            "a_purchases": stats["A"]["purchases_count"],
            "a_fans": stats["A"]["fans_count"],
            "b_offers": stats["B"]["offers_count"],
            "b_purchases": stats["B"]["purchases_count"],
            "b_fans": stats["B"]["fans_count"],
        },
        "tip_stats": tip_stats,
        "stage_thresholds": get_system_stage_thresholds(db_path=db_path),
        "dormant_candidates_count": len(dormant_list),
        "dormant_candidates": [
            {
                "char_id": f.get("char_id"),
                "fan_id": f.get("fan_id"),
                "username": f.get("username"),
                "total_spent_usd": float(f.get("total_spent_usd") or 0.0),
                "inactive_days_actual": f.get("inactive_days_actual"),
                "last_contact_at": f.get("last_contact_at"),
                "preferences": (f.get("psych_profile") or {}).get("preferences") or [],
            }
            for f in dormant_list[:15]
        ],
        "checked_at": (now_dt or datetime.now()).strftime("%Y-%m-%dT%H:%M:%S"),
    }


