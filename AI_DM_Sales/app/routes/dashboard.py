import os
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional

from flask import Blueprint, current_app, jsonify, render_template, request

from app.core import config
from app.core.database import (
    add_dm_message,
    get_dashboard_summary,
    get_escalated_fans,
    get_fan_record,
    get_fan_threads,
    get_system_reply_delay_settings,
    get_system_stage_thresholds,
    get_thread_detail,
    get_unlocked_ppv_ids,
    mute_fan,
    record_ppv_offer,
    resolve_fan_escalation,
    save_system_stage_thresholds,
    set_fan_manual_hold,
    update_dm_message_ja,
    validate_stage_thresholds_strict,
)

from app.core.llm_client import llm_client
from app.services.character_loader import (
    ensure_character_files,
    get_char_paths,
    is_vault_item_sendable_to_customer,
    list_all_characters_overview,
    list_character_ids,
    load_dm_persona,
    load_plans,
    load_settings,
    load_vault_items,
    resolve_customer_safe_ppv_media_path,
    save_dm_persona,
    save_plans,
    save_settings,
)
from app.services.profiler_service import (
    format_psych_profile_for_prompt,
    update_fan_psych_profile,
)
from app.services.dormant_revival_worker import dormant_revival_worker, run_dormant_revival
from app.services.fansly_dm_bot import bot_manager
from app.services.followup_worker import followup_worker
from app.services.optimizer_batch import (
    evaluate_and_promote_ab_test,
    get_evolution_status,
    run_playbook_optimization,
)
from app.services.sales_engine import sales_engine

dashboard_bp = Blueprint("dashboard_bp", __name__)

_client_activity_lock = threading.Lock()
_last_client_activity_ts: float = time.time()
_window_close_timer: Optional[threading.Timer] = None


def _touch_client_activity() -> None:
    global _last_client_activity_ts, _window_close_timer
    with _client_activity_lock:
        _last_client_activity_ts = time.time()
        if _window_close_timer is not None:
            _window_close_timer.cancel()
            _window_close_timer = None


def _attach_pending_and_overdue_info(
    threads: List[Dict[str, Any]],
    summary: Optional[Dict[str, Any]] = None,
    char_id: str = "all",
) -> List[Dict[str, Any]]:
    pending_map = bot_manager.get_pending_replies(char_id=char_id if char_id != "all" else None)
    overdue_cnt = 0
    manual_hold_cnt = 0
    for t in threads:
        key = f"{t.get('char_id')}::{t.get('fan_id')}"
        t["pending_reply"] = pending_map.get(key)
        if t.get("manual_hold_active") or int(t.get("manual_hold") or 0) == 1:
            manual_hold_cnt += 1
        if t.get("is_manual_overdue"):
            overdue_cnt += 1
    if isinstance(summary, dict):
        summary["manual_hold_count"] = manual_hold_cnt
        summary["manual_overdue_alerts"] = overdue_cnt
    return threads


def _build_merged_characters_overview() -> List[Dict[str, Any]]:
    """character_loader の概要情報と bot_manager のリアルタイム稼働・睡眠ステータスを結合する"""
    characters = list_all_characters_overview()
    bot_st = bot_manager.get_status()
    workers_map = bot_st.get("workers", {})
    for c in characters:
        cid = c.get("char_id")
        w = workers_map.get(cid, {})
        c["running"] = bool(w.get("running", False))
        c["monitoring_active"] = bool(w.get("monitoring_active", w.get("running", False)))
        c["sleeping"] = bool(w.get("sleeping", c.get("is_sleeping", False)))
        c["is_sleeping"] = c["sleeping"]
        c["profile_dir"] = w.get("profile_dir")
        c["queued_count"] = int(w.get("queued_count", 0))
        c["pending_replies_count"] = int(w.get("pending_replies_count", 0))
    return characters


@dashboard_bp.route("/", methods=["GET"])
def index():
    _touch_client_activity()
    return render_template("index.html")


@dashboard_bp.route("/api/system/state", methods=["GET"])
def api_system_state():
    """ヘッダーおよび初期ロード用の全キャラ状態・稼働トグル・サマリー一括取得"""
    _touch_client_activity()
    char_id = str(request.args.get("char_id") or "all").strip()
    characters = _build_merged_characters_overview()
    any_enabled = any(c.get("bot_enabled") for c in characters)
    all_enabled = bool(characters) and all(c.get("bot_enabled") for c in characters)

    summary = get_dashboard_summary(char_id)
    alerts = get_escalated_fans(char_id)
    threads = _attach_pending_and_overdue_info(get_fan_threads(char_id), summary=summary, char_id=char_id)

    return jsonify(
        {
            "ok": True,
            "selected_char_id": char_id,
            "any_bot_enabled": any_enabled,
            "all_bots_enabled": all_enabled,
            "characters": characters,
            "summary": summary,
            "alerts": alerts,
            "threads": threads,
        }
    )


@dashboard_bp.route("/api/bot/status", methods=["GET"])
def api_bot_status():
    """ボット稼働状態・睡眠判定・追撃ワーカー状態の一括取得API"""
    char_id = str(request.args.get("char_id") or "all").strip()
    st = bot_manager.get_status(char_id=char_id if char_id != "all" else None)
    characters = _build_merged_characters_overview()
    st["characters"] = characters
    st["followup_worker"] = followup_worker.get_status()
    st["dormant_revival_worker"] = dormant_revival_worker.get_status()
    return jsonify(st)


@dashboard_bp.route("/api/bot/start_all", methods=["POST"])
def api_bot_start_all():
    """[▶ 全キャラ一括稼働] API"""
    st = bot_manager.start_all()
    characters = _build_merged_characters_overview()
    return jsonify(
        {
            "ok": True,
            "enabled": True,
            "any_bot_enabled": st.get("any_bot_enabled", True),
            "all_bots_enabled": st.get("all_bots_enabled", True),
            "any_running": st.get("any_running", True),
            "all_running": st.get("all_running", True),
            "characters": characters,
            "workers": st.get("workers", {}),
        }
    )


@dashboard_bp.route("/api/bot/stop_all", methods=["POST"])
def api_bot_stop_all():
    """
    [⏹ 全停止] API:
    - 通常操作（UIボタン）では pause_all_auto_sales() を呼び、AI自動返信・PPV送信・追撃のみ停止して
      Playwright DM巡回監視・DB保存・翻訳・カルテ更新は常時継続する。
    - terminate=True 明示時（ウィンドウ閉鎖等）は stop_all() で全プロセスを終了する。
    """
    data = request.get_json(silent=True) or {}
    terminate = bool(data.get("terminate", False))
    st = bot_manager.stop_all() if terminate else bot_manager.pause_all_auto_sales()
    characters = _build_merged_characters_overview()
    return jsonify(
        {
            "ok": True,
            "enabled": False,
            "terminated": terminate,
            "any_bot_enabled": st.get("any_bot_enabled", False),
            "all_bots_enabled": st.get("all_bots_enabled", False),
            "any_running": st.get("any_running", False),
            "all_running": st.get("all_running", False),
            "characters": characters,
            "workers": st.get("workers", {}),
        }
    )


@dashboard_bp.route("/api/bot/toggle_all", methods=["POST"])
def api_bot_toggle_all():
    """[▶ 全キャラ一括稼働 / ⏹ 全停止] マスターボタン制御"""
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled", False))
    terminate = bool(data.get("terminate", False))
    if enabled:
        st = bot_manager.start_all()
    else:
        st = bot_manager.stop_all() if terminate else bot_manager.pause_all_auto_sales()
    characters = _build_merged_characters_overview()
    return jsonify(
        {
            "ok": True,
            "enabled": enabled,
            "any_bot_enabled": st.get("any_bot_enabled", enabled),
            "all_bots_enabled": st.get("all_bots_enabled", enabled),
            "any_running": st.get("any_running", enabled),
            "all_running": st.get("all_running", enabled),
            "characters": characters,
            "workers": st.get("workers", {}),
        }
    )


@dashboard_bp.route("/api/bot/toggle_char", methods=["POST"])
def api_bot_toggle_char():
    """個別キャラの稼働ON/OFFトグルスイッチ制御"""
    data = request.get_json(silent=True) or {}
    char_id = str(data.get("char_id") or "").strip()
    if not char_id:
        return jsonify({"ok": False, "error": "char_id is required"}), 400

    enabled = data.get("enabled") if "enabled" in data else None
    if enabled is not None:
        enabled = bool(enabled)

    st = bot_manager.toggle_char(char_id, enabled=enabled)
    characters = _build_merged_characters_overview()
    char_st = st.get("char_status", {})
    return jsonify(
        {
            "ok": True,
            "char_id": char_id,
            "enabled": bool(char_st.get("bot_enabled", False)),
            "running": bool(char_st.get("running", False)),
            "sleeping": bool(char_st.get("sleeping", False)),
            "any_bot_enabled": st.get("any_bot_enabled", False),
            "all_bots_enabled": st.get("all_bots_enabled", False),
            "characters": characters,
            "char_status": char_st,
        }
    )



@dashboard_bp.route("/api/monitoring/overview", methods=["GET"])
def api_monitoring_overview():
    """営業モニタリングタブ用：サマリー・アラート枠・左ペインスレッド一覧"""
    _touch_client_activity()
    char_id = str(request.args.get("char_id") or "all").strip()
    summary = get_dashboard_summary(char_id)
    alerts = get_escalated_fans(char_id)
    threads = _attach_pending_and_overdue_info(get_fan_threads(char_id), summary=summary, char_id=char_id)
    return jsonify(
        {
            "ok": True,
            "char_id": char_id,
            "summary": summary,
            "alerts": alerts,
            "threads": threads,
        }
    )


@dashboard_bp.route("/api/monitoring/poll", methods=["GET"])
def api_monitoring_poll():
    """
    モニタリング画面の軽量リアルタイム差分ポーリングAPI（3〜5秒間隔）。
    スレッド一覧・サマリー・アラート・待機カウントダウン・選択中スレッド詳細を一括返却する。
    """
    _touch_client_activity()
    char_id = str(request.args.get("char_id") or "all").strip()
    active_char_id = str(request.args.get("active_char_id") or "").strip()
    active_fan_id = str(request.args.get("active_fan_id") or "").strip()

    characters = _build_merged_characters_overview()
    any_enabled = any(c.get("bot_enabled") for c in characters)
    all_enabled = bool(characters) and all(c.get("bot_enabled") for c in characters)

    summary = get_dashboard_summary(char_id)
    alerts = get_escalated_fans(char_id)
    threads = _attach_pending_and_overdue_info(get_fan_threads(char_id), summary=summary, char_id=char_id)

    active_thread = None
    if active_char_id and active_fan_id:
        detail = get_thread_detail(active_char_id, active_fan_id)
        if detail:
            for m in detail.get("messages") or []:
                if not m.get("content_ja") and m.get("content"):
                    ja_text = llm_client.translate_to_japanese(m["content"])
                    m["content_ja"] = ja_text
                    if m.get("msg_id"):
                        update_dm_message_ja(int(m["msg_id"]), ja_text)
            pending_map = bot_manager.get_pending_replies(char_id=active_char_id, fan_id=active_fan_id)
            detail["pending_reply"] = pending_map.get(f"{active_char_id}::{active_fan_id}")
            # TICKET-04: 送信予定ドラフト（AI返信EN/JA・PPV・残り待機秒数）をトップレベルで露出
            detail["pending_draft"] = (detail.get("pending_reply") or {}).get("pending_draft")
            active_thread = detail

    return jsonify(
        {
            "ok": True,
            "char_id": char_id,
            "any_bot_enabled": any_enabled,
            "all_bots_enabled": all_enabled,
            "characters": characters,
            "summary": summary,
            "alerts": alerts,
            "threads": threads,
            "active_thread": active_thread,
            "server_time_hms": time.strftime("%H:%M:%S"),
        }
    )


@dashboard_bp.route("/api/monitoring/thread", methods=["GET"])
def api_monitoring_thread():
    """右ペイン用：選択スレッドの会話ログ（英語原文＋日本語訳）・AI顧客カルテ・検討ログ・待機カウントダウン・PPV履歴"""
    _touch_client_activity()
    char_id = str(request.args.get("char_id") or "").strip()
    fan_id = str(request.args.get("fan_id") or "").strip()
    if not char_id or not fan_id:
        return jsonify({"ok": False, "error": "char_id and fan_id are required"}), 400

    detail = get_thread_detail(char_id, fan_id)
    if not detail:
        return jsonify({"ok": False, "error": "Thread not found"}), 404

    for m in detail.get("messages") or []:
        if not m.get("content_ja") and m.get("content"):
            ja_text = llm_client.translate_to_japanese(m["content"])
            m["content_ja"] = ja_text
            if m.get("msg_id"):
                update_dm_message_ja(int(m["msg_id"]), ja_text)

    pending_map = bot_manager.get_pending_replies(char_id=char_id, fan_id=fan_id)
    detail["pending_reply"] = pending_map.get(f"{char_id}::{fan_id}")
    # TICKET-04: 送信予定ドラフト（AI返信EN/JA・PPV・残り待機秒数）をトップレベルで露出
    detail["pending_draft"] = (detail.get("pending_reply") or {}).get("pending_draft")
    if "decision_logs" not in detail:
        fan_obj = detail.get("fan") or {}
        detail["decision_logs"] = fan_obj.get("decision_logs") or []
    return jsonify({"ok": True, **detail})


@dashboard_bp.route("/api/monitoring/skip_to_manual", methods=["POST"])
def api_monitoring_skip_to_manual():
    """[⏭️ スキップして手動返信へ]：待機中の自動返信カウントダウンをキャンセルし、手動対応保留中へ切り替える"""
    data = request.get_json(silent=True) or {}
    char_id = str(data.get("char_id") or "").strip()
    fan_id = str(data.get("fan_id") or "").strip()
    if not char_id or not fan_id:
        return jsonify({"ok": False, "error": "char_id and fan_id are required"}), 400

    res = bot_manager.skip_to_manual(char_id=char_id, fan_id=fan_id)
    detail = get_thread_detail(char_id, fan_id) or {}
    detail["pending_reply"] = None
    res["thread"] = detail
    return jsonify(res)


@dashboard_bp.route("/api/monitoring/resume_auto_reply", methods=["POST"])
def api_monitoring_resume_auto_reply():
    """[🤖 やっぱりAI自動返信に戻す]：手動保留を解除し、直ちにAI自動返信カウントダウンを再セットする"""
    data = request.get_json(silent=True) or {}
    char_id = str(data.get("char_id") or "").strip()
    fan_id = str(data.get("fan_id") or "").strip()
    if not char_id or not fan_id:
        return jsonify({"ok": False, "error": "char_id and fan_id are required"}), 400

    custom_delay = data.get("delay_seconds")
    delay_val = float(custom_delay) if custom_delay is not None else None
    res = bot_manager.resume_auto_reply(
        char_id=char_id,
        fan_id=fan_id,
        custom_delay_sec=delay_val,
        start_worker=not bool(current_app.config.get("TESTING")),
    )
    detail = get_thread_detail(char_id, fan_id) or {}
    detail["pending_reply"] = res.get("pending_reply")
    res["thread"] = detail
    return jsonify(res)


@dashboard_bp.route("/api/monitoring/translate", methods=["POST"])
def api_monitoring_translate():
    """日本語入力 → ローカルLLMによる英語＋キャラ口調自動変換（send_now指定時はそのままDB保存も対応）"""
    data = request.get_json(silent=True) or {}
    char_id = str(data.get("char_id") or "").strip()
    fan_id = str(data.get("fan_id") or "").strip()
    japanese_text = llm_client.normalize_dm_newlines(str(data.get("japanese_text") or ""))
    send_now = bool(data.get("send_now", False))

    if not char_id or char_id == "all":
        chars = list_character_ids()
        char_id = chars[0] if chars else "Sylphy-Mia"

    if not japanese_text:
        return jsonify({"ok": False, "error": "japanese_text is required"}), 400

    persona = load_dm_persona(char_id)
    fan_context = None
    if fan_id:
        detail = get_thread_detail(char_id, fan_id)
        if detail:
            fan_context = detail.get("fan")

    english_text = llm_client.translate_jp_to_character_en(
        japanese_text=japanese_text,
        persona=persona,
        fan_context=fan_context,
    )

    saved_msg = None
    if send_now and fan_id:
        bot_manager._get_or_create_worker(char_id).cancel_pending_reply(fan_id)
        saved_msg = add_dm_message(
            char_id=char_id,
            fan_id=fan_id,
            sender="human_manual",
            content=english_text,
            content_ja=japanese_text,
            resolve_escalation=True,
            clear_manual_hold=True,
        )
        w_state = (bot_manager.get_status(char_id=char_id).get("workers") or {}).get(char_id, {})
        if w_state.get("running"):
            bot_manager.enqueue_outbound_message(
                char_id=char_id,
                fan_id=fan_id,
                message_en=english_text,
                message_ja=japanese_text,
                source="human_manual",
            )

    return jsonify(
        {
            "ok": True,
            "char_id": char_id,
            "japanese_text": japanese_text,
            "english_text": english_text,
            "content": english_text,
            "content_ja": japanese_text,
            "message": saved_msg,
        }
    )


@dashboard_bp.route("/api/monitoring/ppv_candidates", methods=["GET"])
def api_monitoring_ppv_candidates():
    """
    モニタリング画面の手動PPV送信コントロール用：
    選択中キャラクターの有効PPV素材一覧と、対象ファンの顧客カルテ（予算層・課金額・加入プラン）に基づく
    ダイナミックプライシング推奨モード（tripwire / base / whale）＆推奨価格を返却する。
    """
    char_id = str(request.args.get("char_id") or "").strip()
    fan_id = str(request.args.get("fan_id") or "").strip()
    if not char_id or char_id == "all":
        chars = list_character_ids()
        char_id = chars[0] if chars else "Sylphy-Mia"

    plans = load_plans(char_id)
    vault_dir = get_char_paths(char_id)["vault_dir"]
    vault_items = [
        it
        for it in load_vault_items(char_id)
        if bool(it.get("is_active", True)) and is_vault_item_sendable_to_customer(char_id, it)
    ]

    fan_rec = get_fan_record(char_id, fan_id) if fan_id else None
    total_spent = float((fan_rec or {}).get("total_spent_usd") or 0.0)
    current_tier = str((fan_rec or {}).get("current_tier") or "free_follower")
    unlocked_set = set(get_unlocked_ppv_ids(char_id, fan_id)) if fan_id else set()

    pricing_mode, price_key = sales_engine.determine_pricing_mode(
        total_spent_usd=total_spent,
        current_tier=current_tier,
        plans=plans,
    )
    budget_label_ja = ""
    if fan_rec and fan_rec.get("psych_profile_json"):
        try:
            import json as _json

            prof = _json.loads(str(fan_rec["psych_profile_json"]))
            budget_label_ja = str(prof.get("budget_tier") or prof.get("budget_level") or "")
            if any(k in budget_label_ja for k in ("VIP", "太客", "高単価")):
                pricing_mode, price_key = "whale", "whale_price_usd"
            elif any(k in budget_label_ja for k in ("標準", "リピーター")) and pricing_mode == "tripwire":
                pricing_mode, price_key = "base", "base_price_usd"
            elif any(k in budget_label_ja for k in ("無料", "節約", "エントリー", "ゼロ課金")) and total_spent <= 0:
                pricing_mode, price_key = "tripwire", "tripwire_price_usd"
        except Exception:
            pass

    mode_label_map = {
        "tripwire": "お試し割引 (節約・初回層)",
        "base": "標準価格 (通常層)",
        "whale": "太客VIP価格 (高予算層)",
    }

    enriched: List[Dict[str, Any]] = []
    for it in vault_items:
        pid = str(it.get("ppv_id") or "")
        fname = os.path.basename(str(it.get("filename") or "").replace("\\", "/").strip())
        fpath = os.path.join(vault_dir, fname) if fname else ""
        f_exists = bool(fpath and os.path.isfile(fpath) and os.path.getsize(fpath) > 0)
        step_num = int(it.get("ladder_step") or 1)
        step_label = "Step 1: 導入" if step_num == 1 else ("Step 2: 本命" if step_num == 2 else "Step 3: 超秘蔵")
        rec_price = round(float(it.get(price_key) or it.get("base_price_usd") or 9.99), 2)
        enriched.append(
            {
                **it,
                "char_id": char_id,
                "filename": fname,
                "file_exists": f_exists,
                "media_url": f"/api/vault/media/{char_id}/{fname}" if fname else "",
                "step_label": step_label,
                "recommended_pricing_mode": pricing_mode,
                "recommended_mode_label_ja": mode_label_map.get(pricing_mode, pricing_mode),
                "recommended_price_usd": rec_price,
                "already_unlocked": pid in unlocked_set,
            }
        )

    return jsonify(
        {
            "ok": True,
            "char_id": char_id,
            "fan_id": fan_id,
            "recommended_pricing_mode": pricing_mode,
            "recommended_mode_label_ja": mode_label_map.get(pricing_mode, pricing_mode),
            "fan_budget_tier_ja": budget_label_ja or f"累計 ${total_spent:.2f}",
            "items": enriched,
        }
    )


@dashboard_bp.route("/api/monitoring/send_manual", methods=["POST"])
def api_monitoring_send_manual():
    """直接介入送信ボックスからメッセージ（および任意のPPV素材＋ダイナミック/カスタム価格）を送信"""
    data = request.get_json(silent=True) or {}
    char_id = str(data.get("char_id") or "").strip()
    fan_id = str(data.get("fan_id") or "").strip()
    content = llm_client.normalize_dm_newlines(str(data.get("content") or ""))
    japanese_text = llm_client.normalize_dm_newlines(str(data.get("japanese_text") or data.get("content_ja") or ""))
    attached_ppv_id = str(data.get("ppv_id") or "").strip() or None

    if not char_id or not fan_id:
        return jsonify({"ok": False, "error": "char_id and fan_id are required"}), 400

    if not content and japanese_text:
        persona = load_dm_persona(char_id)
        detail = get_thread_detail(char_id, fan_id)
        fan_ctx = detail.get("fan") if detail else None
        content = llm_client.translate_jp_to_character_en(japanese_text, persona, fan_ctx)

    attached_ppv_item: Optional[Dict[str, Any]] = None
    attached_price: Optional[float] = None
    manual_tx_id: Optional[int] = None

    if attached_ppv_id:
        raw_req_fname = str(data.get("filename") or "").replace("\\", "/").strip().lstrip("/")
        if any(p.lower() == "originals" for p in raw_req_fname.split("/") if p):
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "原本（originals/）ファイルの顧客送信はシステムにより禁止されています。",
                        "code": "ORIGINAL_MEDIA_BLOCKED",
                    }
                ),
                400,
            )

        vault_items = load_vault_items(char_id)
        found_v = next((v for v in vault_items if v.get("ppv_id") == attached_ppv_id), None)
        if found_v is None:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": f"指定されたPPV素材 '{attached_ppv_id}' が見つかりません。",
                        "code": "PPV_NOT_FOUND",
                    }
                ),
                404,
            )
        if not is_vault_item_sendable_to_customer(char_id, found_v):
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": f"未加工のPPV素材 '{attached_ppv_id}' は顧客へ送信できません。先にモザイク／ぼかし加工を完了してください。",
                        "code": "UNREDACTED_PPV_BLOCKED",
                    }
                ),
                400,
            )

        safe_media_path, guard_err = resolve_customer_safe_ppv_media_path(char_id, found_v)
        if safe_media_path is None:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": guard_err or "加工済みPPVメディアファイルが確認できないため送信をブロックしました。",
                        "code": "PPV_MEDIA_GUARD_BLOCKED",
                    }
                ),
                400,
            )

        raw_p = data.get("ppv_price_usd")
        pricing_preset = str(data.get("pricing_preset") or "").strip().lower()
        if raw_p not in (None, ""):
            try:
                attached_price = max(0.0, round(float(raw_p), 2))
            except Exception:
                attached_price = None
        if attached_price is None and found_v:
            if pricing_preset == "tripwire":
                attached_price = round(float(found_v.get("tripwire_price_usd") or 5.99), 2)
            elif pricing_preset == "whale":
                attached_price = round(float(found_v.get("whale_price_usd") or 19.99), 2)
            else:
                attached_price = round(float(found_v.get("base_price_usd") or 9.99), 2)
        if attached_price is None:
            attached_price = 9.99

        attached_ppv_item = {
            **found_v,
            "filename": os.path.basename(safe_media_path),
            "offered_price_usd": attached_price,
        }

        if not content:
            hint_en = str((found_v or {}).get("pitch_hint") or "").strip()
            content = hint_en or f"I saved something super private just for you (${attached_price:.2f}) 💜✨"

        manual_tx_id = record_ppv_offer(
            char_id=char_id,
            fan_id=fan_id,
            ppv_id=attached_ppv_id,
            offered_price=attached_price,
            tx_type="ppv",
        )

    if not content:
        return jsonify({"ok": False, "error": "Message content is empty"}), 400

    content_ja = japanese_text if japanese_text else llm_client.translate_to_japanese(content)

    worker = bot_manager._get_or_create_worker(char_id)
    worker.cancel_pending_reply(fan_id)
    msg = add_dm_message(
        char_id=char_id,
        fan_id=fan_id,
        sender="human_manual",
        content=content,
        content_ja=content_ja,
        resolve_escalation=True,
        clear_manual_hold=True,
        ppv_id=attached_ppv_id,
        ppv_price_usd=attached_price,
        tx_id=manual_tx_id,
    )
    queued_payload = bot_manager.enqueue_outbound_message(
        char_id=char_id,
        fan_id=fan_id,
        message_en=content,
        message_ja=content_ja,
        ppv_item=attached_ppv_item,
        offered_price=attached_price,
        source="human_manual",
    )
    detail = get_thread_detail(char_id, fan_id)
    if detail:
        detail["pending_reply"] = None
    return jsonify({"ok": True, "message": msg, "queued": queued_payload, "thread": detail})



@dashboard_bp.route("/api/monitoring/resolve_alert", methods=["POST"])
def api_monitoring_resolve_alert():
    """[手動対応済みにする（AI再開）] ボタン"""
    data = request.get_json(silent=True) or {}
    char_id = str(data.get("char_id") or "").strip()
    fan_id = str(data.get("fan_id") or "").strip()
    if not char_id or not fan_id:
        return jsonify({"ok": False, "error": "char_id and fan_id are required"}), 400

    resolve_fan_escalation(char_id, fan_id)
    return jsonify({"ok": True, "char_id": char_id, "fan_id": fan_id})


@dashboard_bp.route("/api/monitoring/mute_fan", methods=["POST"])
def api_monitoring_mute_fan():
    """[この客を永久ミュート] ボタン"""
    data = request.get_json(silent=True) or {}
    char_id = str(data.get("char_id") or "").strip()
    fan_id = str(data.get("fan_id") or "").strip()
    muted = bool(data.get("muted", True))
    if not char_id or not fan_id:
        return jsonify({"ok": False, "error": "char_id and fan_id are required"}), 400

    mute_fan(char_id, fan_id, muted=muted)
    return jsonify({"ok": True, "char_id": char_id, "fan_id": fan_id, "is_muted": muted})


@dashboard_bp.route("/api/character/config", methods=["GET"])
def api_character_config_get():
    """タブ3：キャラ・プラン設定取得 (dm_persona.json + plans.json + settings.json)"""
    char_id = str(request.args.get("char_id") or "").strip()
    if not char_id or char_id == "all":
        chars = list_character_ids()
        char_id = chars[0] if chars else "Sylphy-Mia"

    persona = load_dm_persona(char_id)
    plans = load_plans(char_id)
    settings = load_settings(char_id)
    return jsonify(
        {
            "ok": True,
            "char_id": char_id,
            "persona": persona,
            "plans": plans,
            "settings": settings,
        }
    )


@dashboard_bp.route("/api/character/save", methods=["POST"])
def api_character_config_save():
    """タブ3：dm_persona.json と plans.json をGUIフォームから保存"""
    data = request.get_json(silent=True) or {}
    char_id = str(data.get("char_id") or "").strip()
    if not char_id or char_id == "all":
        chars = list_character_ids()
        char_id = chars[0] if chars else "Sylphy-Mia"

    saved_persona = None
    saved_plans = None
    if isinstance(data.get("persona"), dict):
        saved_persona = save_dm_persona(char_id, data["persona"])
    else:
        saved_persona = load_dm_persona(char_id)

    if isinstance(data.get("plans"), dict):
        saved_plans = save_plans(char_id, data["plans"])
    else:
        saved_plans = load_plans(char_id)

    return jsonify(
        {
            "ok": True,
            "char_id": char_id,
            "persona": saved_persona,
            "plans": saved_plans,
        }
    )


@dashboard_bp.route("/api/settings/stage_thresholds", methods=["GET", "POST"])
def api_settings_stage_thresholds():
    """
    S.E.L.L. 各ステージの突破閾値 (pt) の取得 (GET) および更新・永続化 (POST) API。
    デフォルト: {"1_Icebreak": 40, "2_Explore": 70, "3_Lead_Pitch": 85}
    """
    if request.method == "GET":
        thresholds = get_system_stage_thresholds()
        return jsonify(
            {
                "ok": True,
                "stage_thresholds": thresholds,
                "stage_threshold_icebreak": thresholds["1_Icebreak"],
                "stage_threshold_explore": thresholds["2_Explore"],
                "stage_threshold_lead_pitch": thresholds["3_Lead_Pitch"],
            }
        )

    data = request.get_json(silent=True) or {}
    has_any_key = any(
        k in data
        for k in (
            "stage_thresholds",
            "1_Icebreak",
            "2_Explore",
            "2_Discovery",
            "3_Lead_Pitch",
            "stage_threshold_icebreak",
            "stage_threshold_explore",
            "stage_threshold_lead_pitch",
        )
    )
    if not has_any_key:
        return jsonify({"ok": False, "error": "突破閾値データ (stage_thresholds) が指定されていません"}), 400

    is_valid, err_msg, _ = validate_stage_thresholds_strict(raw_data=data)
    if not is_valid:
        return jsonify({"ok": False, "error": err_msg}), 400

    saved_thresholds = save_system_stage_thresholds(thresholds=data)
    sync_payload = {
        "stage_thresholds": saved_thresholds,
        "stage_threshold_icebreak": saved_thresholds["1_Icebreak"],
        "stage_threshold_explore": saved_thresholds["2_Explore"],
        "stage_threshold_lead_pitch": saved_thresholds["3_Lead_Pitch"],
    }
    all_chars = list_character_ids()
    for cid in all_chars:
        save_settings(cid, sync_payload)

    return jsonify(
        {
            "ok": True,
            "stage_thresholds": saved_thresholds,
            "stage_threshold_icebreak": saved_thresholds["1_Icebreak"],
            "stage_threshold_explore": saved_thresholds["2_Explore"],
            "stage_threshold_lead_pitch": saved_thresholds["3_Lead_Pitch"],
            "updated_characters": all_chars,
        }
    )


@dashboard_bp.route("/api/simulator/reply", methods=["POST"])
def api_simulator_reply():
    """
    タブ4：営業シミュレーター用API（本番 SalesEngine ＆ profiler_service 直結）。
    - auto_translate_ja: true の場合、入力された日本語を translate_fan_ja_to_en() で英語へ変換し、
      客の吹き出し用に fan_en (英語) と fan_ja (日本語) の両方を返却する。
    - AIの返信についても ai_en / ai_reply (英語) と ai_ja / ai_reply_ja (日本語訳) の両方を返却する。
    """
    data = request.get_json(silent=True) or {}
    char_id = str(data.get("char_id") or "").strip()
    if not char_id or char_id == "all":
        chars = list_character_ids()
        with_vault = next((c for c in chars if load_vault_items(c)), None)
        char_id = with_vault or (chars[0] if chars else "Sylphy-Mia")

    raw_message = str(data.get("message") or "").strip()
    auto_translate_ja = bool(data.get("auto_translate_ja", False))

    if auto_translate_ja:
        fan_en = llm_client.translate_fan_ja_to_en(raw_message)
        fan_ja = llm_client.translate_to_japanese(fan_en) if not raw_message else raw_message
    else:
        fan_en = raw_message
        fan_ja = llm_client.translate_to_japanese(raw_message)

    user_message = fan_en
    fan_id = str(data.get("fan_id") or "").strip() or None
    current_tier = str(data.get("current_tier") or "free_follower").strip()
    try:
        total_spent = round(float(data.get("total_spent_usd") or 0.0), 2)
    except Exception:
        total_spent = 0.0
    try:
        ignored_count = int(data.get("consecutive_ppv_ignored") or 0)
    except Exception:
        ignored_count = 0
    try:
        unanswered_count = int(data.get("consecutive_unanswered") or 0)
    except Exception:
        unanswered_count = 0

    raw_stage_score = data.get("stage_score")
    explicit_stage_score: Optional[int] = None
    if raw_stage_score is not None and raw_stage_score != "":
        try:
            explicit_stage_score = int(round(float(raw_stage_score)))
        except Exception:
            explicit_stage_score = None

    current_phase = str(data.get("sales_phase") or "1_Icebreak").strip()
    raw_purchased = data.get("purchased_ppv_ids") or []
    if isinstance(raw_purchased, str):
        purchased_ids = [x.strip() for x in raw_purchased.split(",") if x.strip()]
    else:
        purchased_ids = [str(x).strip() for x in raw_purchased if str(x).strip()]

    recent_messages = data.get("recent_messages") if isinstance(data.get("recent_messages"), list) else []
    existing_profile = data.get("existing_profile") or data.get("psych_profile")
    persist_db = bool(data.get("persist_db", bool(fan_id)))
    record_messages = bool(data.get("record_messages", bool(fan_id and persist_db)))
    ab_group_req = str(data.get("ab_group") or "").strip().upper() or None

    result = sales_engine.process_fan_message(
        char_id=char_id,
        user_message=user_message,
        fan_id=fan_id,
        current_tier=current_tier,
        total_spent_usd=total_spent,
        sales_phase=current_phase,
        consecutive_ppv_ignored=ignored_count,
        consecutive_unanswered=unanswered_count,
        purchased_ppv_ids=purchased_ids,
        recent_messages=recent_messages,
        existing_profile_json=existing_profile,
        ab_group=ab_group_req,
        persist_db=persist_db,
        stage_score=explicit_stage_score,
    )

    next_phase_out = str(result.get("sales_phase") or current_phase)
    rec_ppv = result.get("recommended_ppv")
    sent_ppv_attachment = None
    if next_phase_out in ("3_Lead_Pitch", "4_Lock_Close", "4_Closing") and isinstance(rec_ppv, dict) and rec_ppv.get("ppv_id"):
        fname = os.path.basename(str(rec_ppv.get("filename") or "").replace("\\", "/").strip())
        vault_dir = get_char_paths(char_id)["vault_dir"]
        f_exists = bool(fname and os.path.isfile(os.path.join(vault_dir, fname)) and os.path.getsize(os.path.join(vault_dir, fname)) > 0)
        step_num = int(rec_ppv.get("ladder_step") or 1)
        sent_ppv_attachment = {
            "ppv_id": rec_ppv.get("ppv_id"),
            "char_id": char_id,
            "filename": fname or rec_ppv.get("ppv_id"),
            "media_type": rec_ppv.get("media_type") or "video",
            "media_url": f"/api/vault/media/{char_id}/{fname}" if fname else "",
            "file_exists": f_exists,
            "ladder_step": step_num,
            "step_label": "Step 1: 導入" if step_num == 1 else ("Step 2: 本命" if step_num == 2 else "Step 3: 超秘蔵"),
            "offered_price_usd": round(float(rec_ppv.get("offered_price_usd") or rec_ppv.get("base_price_usd") or 9.99), 2),
            "tags": rec_ppv.get("tags") or [],
            "description_memo": str(rec_ppv.get("description_memo") or rec_ppv.get("pitch_note") or ""),
            "pitch_hint": str(rec_ppv.get("pitch_hint") or ""),
            "is_unlocked": 0,
            "unlock_status_ja": "未購入",
        }

    if record_messages and fan_id:
        add_dm_message(
            char_id=char_id,
            fan_id=fan_id,
            sender="fan",
            content=fan_en,
            content_ja=fan_ja,
            sales_phase=current_phase,
            prompt_version="simulator-fan",
        )
        sim_ppv_id = sent_ppv_attachment.get("ppv_id") if sent_ppv_attachment else None
        sim_ppv_price = sent_ppv_attachment.get("offered_price_usd") if sent_ppv_attachment else None
        add_dm_message(
            char_id=char_id,
            fan_id=fan_id,
            sender="ai",
            content=str(result.get("ai_en") or result.get("ai_reply") or ""),
            content_ja=str(result.get("ai_ja") or result.get("ai_reply_ja") or ""),
            sales_phase=next_phase_out,
            prompt_version=str(result.get("prompt_version") or "v1-sim"),
            decision_log=result.get("decision_log"),
            ppv_id=sim_ppv_id,
            ppv_price_usd=sim_ppv_price,
        )

    return jsonify(
        {
            "ok": True,
            "char_id": char_id,
            "fan_id": fan_id,
            "is_escalated": int(result.get("is_escalated") or 0),
            "escalation_reason": result.get("escalation_reason"),
            "is_low_priority": int(result.get("is_low_priority") or 0),
            "sales_phase": next_phase_out,
            "current_stage": result.get("current_stage", next_phase_out),
            "next_stage": result.get("next_stage"),
            "stage_score": result.get("stage_score"),
            "stage_threshold": result.get("stage_threshold"),
            "remaining_points": result.get("remaining_points"),
            "earned_points": result.get("earned_points"),
            "score_reason_ja": result.get("score_reason_ja"),
            "pricing_tier": result.get("pricing_tier"),
            "ab_group": result.get("ab_group"),
            "prompt_version": result.get("prompt_version"),
            "playbook_version_id": result.get("playbook_version_id"),
            "recommended_ppv": rec_ppv,
            "sent_ppv_attachment": sent_ppv_attachment,
            "tip_hook": result.get("tip_hook"),
            "tip_tx_id": result.get("tip_tx_id"),
            "fan_en": fan_en,
            "fan_ja": fan_ja,
            "ai_en": result.get("ai_en"),
            "ai_ja": result.get("ai_ja"),
            "ai_reply": result.get("ai_reply"),
            "ai_reply_ja": result.get("ai_reply_ja"),
            "psych_profile": result.get("psych_profile"),
            "decision_log": result.get("decision_log"),
            "profile_prompt_block": format_psych_profile_for_prompt(result.get("psych_profile")),
        }
    )


@dashboard_bp.route("/api/evolution/status", methods=["GET"])
def api_evolution_status():
    """
    Phase 2: 現行の A/B プレイブック内容・CVRスコア比較・チップ統計・休眠ファン候補人数を返却する。
    """
    char_id = str(request.args.get("char_id") or "").strip()
    try:
        inactive_days = int(request.args.get("inactive_days") or 7)
    except Exception:
        inactive_days = 7
    try:
        min_samples = int(request.args.get("min_samples") or 10)
    except Exception:
        min_samples = 10

    status_data = get_evolution_status(
        char_id=char_id or None,
        inactive_days=inactive_days,
        min_samples=min_samples,
    )
    status_data["dormant_worker"] = dormant_revival_worker.get_status()
    return jsonify(status_data)


@dashboard_bp.route("/api/evolution/optimize", methods=["POST"])
def api_evolution_optimize():
    """
    Phase 2: 自己進化バッチ分析（既存A/B評価・昇格判定 ＋ 勝ち/負け比較分析による新ルールB生成）を手動即時実行する。
    """
    data = request.get_json(silent=True) or {}
    char_id = str(data.get("char_id") or "").strip()
    if not char_id or char_id == "all":
        chars = list_character_ids()
        char_id = chars[0] if chars else "Sylphy-Mia"

    try:
        min_samples = int(data.get("min_samples") or 10)
    except Exception:
        min_samples = 10
    evaluate_only = bool(data.get("evaluate_only", False))
    auto_tune_thresholds = bool(data.get("auto_tune_thresholds", False))

    eval_res = evaluate_and_promote_ab_test(char_id=char_id, min_samples=min_samples)
    opt_res = None
    if not evaluate_only:
        opt_res = run_playbook_optimization(char_id=char_id, auto_tune_thresholds=auto_tune_thresholds)

    status_data = get_evolution_status(char_id=char_id, min_samples=min_samples)
    status_data["dormant_worker"] = dormant_revival_worker.get_status()
    return jsonify(
        {
            "ok": True,
            "char_id": char_id,
            "ab_evaluation": eval_res,
            "optimization": opt_res,
            "status": status_data,
        }
    )


@dashboard_bp.route("/api/evolution/revive_dormant", methods=["POST"])
def api_evolution_revive_dormant():
    """
    Phase 2: 休眠ファン掘り起こし個別DMバッチを手動即時実行する。
    """
    data = request.get_json(silent=True) or {}
    char_id = str(data.get("char_id") or "").strip() or None
    try:
        inactive_days = int(data.get("inactive_days") or 7)
    except Exception:
        inactive_days = 7
    try:
        max_targets = int(data.get("max_targets") or 10)
    except Exception:
        max_targets = 10

    revival_res = dormant_revival_worker.run_once(
        char_id=char_id,
        inactive_days=inactive_days,
        max_targets=max_targets,
        ignore_sleep=True,
    )
    status_data = get_evolution_status(char_id=char_id, inactive_days=inactive_days)
    status_data["dormant_worker"] = dormant_revival_worker.get_status()
    return jsonify(
        {
            "ok": True,
            "char_id": char_id or "all",
            "revived_count": int(revival_res.get("revived_count") or 0),
            "candidates_found": int(revival_res.get("candidates_found") or 0),
            "results": revival_res.get("results") or [],
            "status": status_data,
        }
    )




@dashboard_bp.route("/api/bot/manual_login", methods=["POST"])
@dashboard_bp.route("/api/browser/launch_login", methods=["POST"])
def api_bot_manual_login():
    """
    [⚙️ 設定] モーダル内の「🔑 ブラウザを開いて手動ログイン / 再認証」ボタン用API。
    POST /api/bot/manual_login (および後方互換 /api/browser/launch_login) から呼び出され、
    bot_manager.launch_manual_login(char_id) を実行する。
    """
    data = request.get_json(silent=True) or {}
    char_id = str(data.get("char_id") or "").strip()
    if not char_id or char_id == "all":
        chars = list_character_ids()
        char_id = chars[0] if chars else "Sylphy-Mia"

    # デフォルトで launch_browser=True（テスト環境等で launch_process=False 明示時はスキップ可）
    if "launch_browser" in data:
        launch_browser = bool(data.get("launch_browser"))
    elif "launch_process" in data:
        launch_browser = bool(data.get("launch_process"))
    else:
        launch_browser = True

    res = bot_manager.launch_manual_login(
        char_id=char_id,
        launch_browser=launch_browser,
        blocking=bool(data.get("blocking", False)),
    )
    return jsonify(res)


@dashboard_bp.route("/api/system/manual", methods=["GET"])
def api_system_manual():
    """AI_DM_Sales/docs/USER_MANUAL.md を UTF-8 で読み込んで JSON で返却する"""
    manual_path = os.path.join(config.BASE_DIR, "docs", "USER_MANUAL.md")
    if not os.path.isfile(manual_path):
        return jsonify({"ok": False, "error": f"USER_MANUAL.md not found at {manual_path}", "content": ""}), 404
    with open(manual_path, "r", encoding="utf-8") as fp:
        content = fp.read()
    return jsonify({"ok": True, "content": content})


@dashboard_bp.route("/api/system/heartbeat", methods=["POST"])
def api_system_heartbeat():
    """フロントエンド生存通知（F5リロード時とウィンドウ×閉鎖の識別に利用）"""
    _touch_client_activity()
    return jsonify({"ok": True, "ts": _last_client_activity_ts})


@dashboard_bp.route("/api/system/window_closed", methods=["POST"])
def api_system_window_closed():
    """
    ブラウザウィンドウを「×」で閉じた際の通知エンドポイント。
    F5リロード誤検知を防ぐ短い猶予（テスト時や immediate=True 時は即時）の後、
    再接続が無ければ bot_manager.stop_all() 等を呼び出して全プロセスをクリーン終了する。
    """
    global _window_close_timer
    data = request.get_json(silent=True) or {}
    if not data and request.data:
        try:
            import json as _json
            data = _json.loads(request.data.decode("utf-8"))
        except Exception:
            data = {}

    immediate = bool(data.get("immediate", False)) or bool(current_app.config.get("TESTING"))
    exit_process = bool(data.get("exit_process", not current_app.config.get("TESTING")))
    close_req_ts = time.time()

    def _perform_clean_shutdown(force: bool = False) -> None:
        with _client_activity_lock:
            if not force and _last_client_activity_ts > close_req_ts + 0.15:
                return
        try:
            followup_worker.stop()
        except Exception:
            pass
        try:
            dormant_revival_worker.stop()
        except Exception:
            pass
        try:
            bot_manager.stop_all(update_settings=False)
        except Exception:
            pass
        if exit_process:
            os._exit(0)

    if immediate:
        _perform_clean_shutdown(force=True)
        return jsonify({"ok": True, "action": "stopped_all_immediately"})

    with _client_activity_lock:
        if _window_close_timer is not None:
            _window_close_timer.cancel()
        _window_close_timer = threading.Timer(3.0, _perform_clean_shutdown, kwargs={"force": False})
        _window_close_timer.daemon = True
        _window_close_timer.start()

    return jsonify({"ok": True, "action": "scheduled_clean_shutdown", "grace_sec": 3.0})




