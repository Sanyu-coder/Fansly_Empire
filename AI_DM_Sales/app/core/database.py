import json
import os
import re
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple, Union

from app.core import config

MANUAL_HOLD_OVERDUE_SECONDS = 600


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def get_db_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    """
    empire_core.db への接続を取得し、必ず WAL モードを有効化する。
    AI_Factory / AI_DM_Sales / AI_Analytics_Console の3アプリ同時共有に対応。
    """
    target_path = db_path or config.DB_PATH
    os.makedirs(os.path.dirname(os.path.abspath(target_path)), exist_ok=True)
    conn = sqlite3.connect(target_path, timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=10000;")
    return conn


def init_db(db_path: Optional[str] = None, seed_demo: bool = False) -> str:
    """
    empire_core.db の6テーブルを初期化し、WALモードが有効であることを保証する。
    既存DBに dm_messages.content_ja カラムがない場合は ALTER TABLE で自動マイグレーションを行う。
    ※本番稼働に伴いダミーシード処理は完全に廃止（seed_demo=False デフォルト・シード処理無効化）。
    """
    conn = get_db_connection(db_path)
    try:
        cur = conn.cursor()
        mode_row = cur.execute("PRAGMA journal_mode=WAL;").fetchone()
        journal_mode = mode_row[0] if mode_row else "unknown"


        # 2.1 daily_account_metrics
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_account_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                char_id TEXT NOT NULL,
                followers_count INTEGER DEFAULT 0,
                subscribers_count INTEGER DEFAULT 0,
                total_likes INTEGER DEFAULT 0,
                total_revenue_usd REAL DEFAULT 0.0,
                UNIQUE(date, char_id)
            );
            """
        )

        # 2.2 posts_log
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS posts_log (
                post_id TEXT PRIMARY KEY,
                char_id TEXT NOT NULL,
                posted_at TEXT NOT NULL,
                media_type TEXT,
                likes_count INTEGER DEFAULT 0,
                views_count INTEGER DEFAULT 0,
                is_fyp INTEGER DEFAULT 0
            );
            """
        )

        # 2.3 fans
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS fans (
                fan_id TEXT NOT NULL,
                char_id TEXT NOT NULL,
                username TEXT NOT NULL,
                current_tier TEXT DEFAULT 'free_follower',
                total_spent_usd REAL DEFAULT 0.0,
                psych_profile_json TEXT DEFAULT '{}',
                sales_phase TEXT DEFAULT '1_Icebreak',
                consecutive_unanswered INTEGER DEFAULT 0,
                consecutive_ppv_ignored INTEGER DEFAULT 0,
                is_low_priority INTEGER DEFAULT 0,
                is_escalated INTEGER DEFAULT 0,
                escalation_reason TEXT,
                is_muted INTEGER DEFAULT 0,
                ab_group TEXT DEFAULT 'A',
                manual_hold INTEGER DEFAULT 0,
                manual_hold_at TEXT,
                decision_log_json TEXT DEFAULT '[]',
                last_contact_at TEXT,
                created_at TEXT,
                PRIMARY KEY (fan_id, char_id)
            );
            """
        )

        fan_cols = {
            str(r["name"])
            for r in cur.execute("PRAGMA table_info(fans);").fetchall()
        }
        if "ab_group" not in fan_cols:
            cur.execute("ALTER TABLE fans ADD COLUMN ab_group TEXT DEFAULT 'A';")
        if "manual_hold" not in fan_cols:
            cur.execute("ALTER TABLE fans ADD COLUMN manual_hold INTEGER DEFAULT 0;")
        if "manual_hold_at" not in fan_cols:
            cur.execute("ALTER TABLE fans ADD COLUMN manual_hold_at TEXT;")
        if "decision_log_json" not in fan_cols:
            cur.execute("ALTER TABLE fans ADD COLUMN decision_log_json TEXT DEFAULT '[]';")
        # 新ステートマシン (Ticket 1.2): 往復カウント・最終PPV提案時刻 を追加
        if "state_step_turns" not in fan_cols:
            cur.execute("ALTER TABLE fans ADD COLUMN state_step_turns INTEGER DEFAULT 0;")
        if "last_pitch_at" not in fan_cols:
            cur.execute("ALTER TABLE fans ADD COLUMN last_pitch_at TEXT;")

        # 2.4 dm_messages (content: 英語原文, content_ja: 日本語訳, decision_log_json: AI検討プロセスログ, ppv添付情報)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS dm_messages (
                msg_id INTEGER PRIMARY KEY AUTOINCREMENT,
                fan_id TEXT NOT NULL,
                char_id TEXT NOT NULL,
                sender TEXT NOT NULL,
                content TEXT NOT NULL,
                content_ja TEXT,
                sales_phase TEXT,
                prompt_version TEXT,
                decision_log_json TEXT,
                ppv_id TEXT,
                ppv_price_usd REAL,
                tx_id INTEGER,
                created_at TEXT NOT NULL
            );
            """
        )

        # 既存DB向けマイグレーション: dm_messages に content_ja / decision_log_json / PPV添付カラムが無ければ追加
        col_rows = cur.execute("PRAGMA table_info(dm_messages);").fetchall()
        existing_cols = {r["name"] for r in col_rows}
        if "content_ja" not in existing_cols:
            cur.execute("ALTER TABLE dm_messages ADD COLUMN content_ja TEXT;")
        if "decision_log_json" not in existing_cols:
            cur.execute("ALTER TABLE dm_messages ADD COLUMN decision_log_json TEXT;")
        if "ppv_id" not in existing_cols:
            cur.execute("ALTER TABLE dm_messages ADD COLUMN ppv_id TEXT;")
        if "ppv_price_usd" not in existing_cols:
            cur.execute("ALTER TABLE dm_messages ADD COLUMN ppv_price_usd REAL;")
        if "tx_id" not in existing_cols:
            cur.execute("ALTER TABLE dm_messages ADD COLUMN tx_id INTEGER;")

        # 2.5 ppv_transactions
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS ppv_transactions (
                tx_id INTEGER PRIMARY KEY AUTOINCREMENT,
                fan_id TEXT NOT NULL,
                char_id TEXT NOT NULL,
                ppv_id TEXT,
                tx_type TEXT NOT NULL,
                offered_price REAL NOT NULL,
                is_unlocked INTEGER DEFAULT 0,
                reminder_sent INTEGER DEFAULT 0,
                offered_at TEXT NOT NULL,
                unlocked_at TEXT
            );
            """
        )

        # 2.6 sales_playbooks
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS sales_playbooks (
                version_id INTEGER PRIMARY KEY AUTOINCREMENT,
                char_id TEXT NOT NULL,
                rules_text TEXT NOT NULL,
                rules_ja TEXT,
                analysis_summary_ja TEXT,
                ab_group TEXT NOT NULL,
                cvr_score REAL DEFAULT 0.0,
                offers_count INTEGER DEFAULT 0,
                purchases_count INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1,
                created_at TEXT NOT NULL
            );
            """
        )

        # 既存DB向け sales_playbooks カラム自動マイグレーション
        pb_cols = {
            str(r["name"])
            for r in cur.execute("PRAGMA table_info(sales_playbooks);").fetchall()
        }
        if "rules_ja" not in pb_cols:
            cur.execute("ALTER TABLE sales_playbooks ADD COLUMN rules_ja TEXT;")
        if "analysis_summary_ja" not in pb_cols:
            cur.execute("ALTER TABLE sales_playbooks ADD COLUMN analysis_summary_ja TEXT;")
        if "offers_count" not in pb_cols:
            cur.execute("ALTER TABLE sales_playbooks ADD COLUMN offers_count INTEGER DEFAULT 0;")
        if "purchases_count" not in pb_cols:
            cur.execute("ALTER TABLE sales_playbooks ADD COLUMN purchases_count INTEGER DEFAULT 0;")

        # 2.7 ppv_vault (PPV素材メタデータ・日本語属性タグ tags_json・日本語売り込みメモ description_memo / pitch_note)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS ppv_vault (
                ppv_id TEXT NOT NULL,
                char_id TEXT NOT NULL,
                filename TEXT NOT NULL,
                media_type TEXT DEFAULT 'video',
                ladder_step INTEGER DEFAULT 1,
                base_price_usd REAL DEFAULT 9.99,
                tripwire_price_usd REAL DEFAULT 5.99,
                whale_price_usd REAL DEFAULT 19.99,
                tags_json TEXT DEFAULT '[]',
                description_memo TEXT DEFAULT '',
                pitch_note TEXT DEFAULT '',
                pitch_hint TEXT DEFAULT '',
                is_active INTEGER DEFAULT 1,
                created_at TEXT,
                updated_at TEXT,
                PRIMARY KEY (char_id, ppv_id)
            );
            """
        )
        pv_cols = {
            str(r["name"])
            for r in cur.execute("PRAGMA table_info(ppv_vault);").fetchall()
        }
        if "tags_json" not in pv_cols:
            cur.execute("ALTER TABLE ppv_vault ADD COLUMN tags_json TEXT DEFAULT '[]';")
        if "description_memo" not in pv_cols:
            cur.execute("ALTER TABLE ppv_vault ADD COLUMN description_memo TEXT DEFAULT '';")
        if "pitch_note" not in pv_cols:
            cur.execute("ALTER TABLE ppv_vault ADD COLUMN pitch_note TEXT DEFAULT '';")
        if "pitch_hint" not in pv_cols:
            cur.execute("ALTER TABLE ppv_vault ADD COLUMN pitch_hint TEXT DEFAULT '';")
        if "updated_at" not in pv_cols:
            cur.execute("ALTER TABLE ppv_vault ADD COLUMN updated_at TEXT;")
        # 新PPV選定 (Ticket 1.2): 汎用おすすめフラグ・カテゴリ別タグJSON を追加
        if "is_general" not in pv_cols:
            cur.execute("ALTER TABLE ppv_vault ADD COLUMN is_general INTEGER DEFAULT 0;")
        if "category_tags_json" not in pv_cols:
            cur.execute("ALTER TABLE ppv_vault ADD COLUMN category_tags_json TEXT DEFAULT '{}';")

        # 2.8 system_settings (返信待機時間範囲 min_reply_delay_sec / max_reply_delay_sec 等の共通・システム設定)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS system_settings (
                setting_key TEXT PRIMARY KEY,
                setting_value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        now_init = _now_iso()
        cur.execute(
            "INSERT OR IGNORE INTO system_settings (setting_key, setting_value, updated_at) VALUES (?, ?, ?);",
            ("min_reply_delay_sec", "60", now_init),
        )
        cur.execute(
            "INSERT OR IGNORE INTO system_settings (setting_key, setting_value, updated_at) VALUES (?, ?, ?);",
            ("max_reply_delay_sec", "300", now_init),
        )
        cur.execute(
            "INSERT OR IGNORE INTO system_settings (setting_key, setting_value, updated_at) VALUES (?, ?, ?);",
            ("short_msg_chars_threshold", "20", now_init),
        )
        cur.execute(
            "INSERT OR IGNORE INTO system_settings (setting_key, setting_value, updated_at) VALUES (?, ?, ?);",
            ("long_msg_chars_threshold", "150", now_init),
        )
        cur.execute(
            "INSERT OR IGNORE INTO system_settings (setting_key, setting_value, updated_at) VALUES (?, ?, ?);",
            ("stage_threshold_icebreak", "40", now_init),
        )
        cur.execute(
            "INSERT OR IGNORE INTO system_settings (setting_key, setting_value, updated_at) VALUES (?, ?, ?);",
            ("stage_threshold_explore", "70", now_init),
        )
        cur.execute(
            "INSERT OR IGNORE INTO system_settings (setting_key, setting_value, updated_at) VALUES (?, ?, ?);",
            ("stage_threshold_lead_pitch", "85", now_init),
        )
        # 新ステートマシン・Discord通知・DM推論個別設定 (Ticket 1.2)
        cur.execute(
            "INSERT OR IGNORE INTO system_settings (setting_key, setting_value, updated_at) VALUES (?, ?, ?);",
            ("discord_webhook_url", "", now_init),
        )
        cur.execute(
            "INSERT OR IGNORE INTO system_settings (setting_key, setting_value, updated_at) VALUES (?, ?, ?);",
            ("debounce_seconds", "18", now_init),
        )
        cur.execute(
            "INSERT OR IGNORE INTO system_settings (setting_key, setting_value, updated_at) VALUES (?, ?, ?);",
            ("tag_collect_target_count", "1", now_init),
        )
        cur.execute(
            "INSERT OR IGNORE INTO system_settings (setting_key, setting_value, updated_at) VALUES (?, ?, ?);",
            ("tag_collect_max_turns", "5", now_init),
        )
        cur.execute(
            "INSERT OR IGNORE INTO system_settings (setting_key, setting_value, updated_at) VALUES (?, ?, ?);",
            ("cooldown_turns_threshold", "3", now_init),
        )
        cur.execute(
            "INSERT OR IGNORE INTO system_settings (setting_key, setting_value, updated_at) VALUES (?, ?, ?);",
            ("dm_ollama_base_url", "", now_init),
        )
        cur.execute(
            "INSERT OR IGNORE INTO system_settings (setting_key, setting_value, updated_at) VALUES (?, ?, ?);",
            ("dm_ollama_model_name", "", now_init),
        )

        # 2.9 unmatched_needs (Ticket 1.2: 耳役LLMが抽出した未登録ファン要望の蓄積テーブル)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS unmatched_needs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                char_id TEXT NOT NULL,
                fan_id TEXT NOT NULL,
                keyword TEXT NOT NULL,
                raw_quote TEXT NOT NULL,
                created_at TEXT NOT NULL,
                promoted_to_tag INTEGER DEFAULT 0,
                UNIQUE(char_id, fan_id, keyword)
            );
            """
        )

        cur.execute("CREATE INDEX IF NOT EXISTS idx_fans_char_contact ON fans(char_id, last_contact_at DESC);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_dm_messages_fan_char ON dm_messages(char_id, fan_id, msg_id ASC);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ppv_tx_char_ppv ON ppv_transactions(char_id, ppv_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_playbooks_char_active ON sales_playbooks(char_id, ab_group, is_active);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ppv_vault_char_step ON ppv_vault(char_id, ladder_step, is_active);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_unmatched_needs_char_kw ON unmatched_needs(char_id, keyword);")

        conn.commit()
    finally:
        conn.close()

    try:
        repair_false_positive_transactions(db_path=db_path)
    except Exception:
        pass
    # 本番移行に伴いダミーシード処理は完全廃止
    return journal_mode


def _assign_balanced_ab_group_with_cursor(cur: sqlite3.Cursor, char_id: str) -> str:
    """
    指定キャラクターの既存ファン数を A群 / B群 ごとにカウントし、
    少ない方のグループ（同数の場合は 'A'）を返して 50:50 均等振り分けを行う。
    """
    rows = cur.execute(
        """
        SELECT COALESCE(ab_group, 'A') AS grp, COUNT(*) AS cnt
        FROM fans
        WHERE char_id = ?
        GROUP BY COALESCE(ab_group, 'A');
        """,
        (char_id,),
    ).fetchall()
    counts = {"A": 0, "B": 0}
    for r in rows:
        g = str(r["grp"] or "A").upper()
        if g in counts:
            counts[g] += int(r["cnt"] or 0)
    return "A" if counts["A"] <= counts["B"] else "B"


def assign_next_ab_group(char_id: str, db_path: Optional[str] = None) -> str:
    """新規ファン登録時に A (現行本番ルール) または B (新検証ルール) を均等自動振り分けする"""
    conn = get_db_connection(db_path)
    try:
        return _assign_balanced_ab_group_with_cursor(conn.cursor(), char_id)
    finally:
        conn.close()


def get_ppv_stats_by_char(char_id: str, db_path: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """指定キャラクターの各 ppv_id ごとの「提案回数・購入回数・売上金額（USD）」を集計して返す"""
    conn = get_db_connection(db_path)
    try:
        rows = conn.execute(
            """
            SELECT
                ppv_id,
                COUNT(*) AS offers_count,
                SUM(CASE WHEN is_unlocked = 1 THEN 1 ELSE 0 END) AS purchases_count,
                SUM(CASE WHEN is_unlocked = 1 THEN offered_price ELSE 0.0 END) AS revenue_usd
            FROM ppv_transactions
            WHERE char_id = ? AND ppv_id IS NOT NULL AND ppv_id != ''
            GROUP BY ppv_id;
            """,
            (char_id,),
        ).fetchall()

        result: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            ppv_id = row["ppv_id"]
            offers = int(row["offers_count"] or 0)
            purchases = int(row["purchases_count"] or 0)
            revenue = round(float(row["revenue_usd"] or 0.0), 2)
            cvr = round((purchases / offers) * 100.0, 1) if offers > 0 else 0.0
            result[ppv_id] = {
                "offers_count": offers,
                "purchases_count": purchases,
                "revenue_usd": revenue,
                "cvr_percent": cvr,
            }
        return result
    finally:
        conn.close()


def get_dashboard_summary(char_id: Optional[str] = None, db_path: Optional[str] = None) -> Dict[str, Any]:
    """営業モニタリング上段のサマリーカード用統計を返す"""
    conn = get_db_connection(db_path)
    try:
        today_prefix = _today_str()
        params_char: List[Any] = []
        where_char = ""
        if char_id and char_id != "all":
            where_char = "WHERE char_id = ?"
            params_char = [char_id]

        tx_query = f"""
            SELECT
                COUNT(CASE WHEN tx_type = 'ppv' THEN 1 END) AS total_offers,
                SUM(CASE WHEN tx_type = 'ppv' AND is_unlocked = 1 THEN 1 ELSE 0 END) AS total_purchases,
                SUM(CASE WHEN is_unlocked = 1 THEN offered_price ELSE 0.0 END) AS total_revenue_usd,
                SUM(CASE WHEN is_unlocked = 1 AND (
                    (unlocked_at IS NOT NULL AND substr(unlocked_at, 1, 10) = ?)
                    OR (unlocked_at IS NULL AND substr(offered_at, 1, 10) = ?)
                ) THEN offered_price ELSE 0.0 END) AS today_revenue_usd
            FROM ppv_transactions
            {where_char};
        """
        tx_params = [today_prefix, today_prefix] + params_char
        tx_row = conn.execute(tx_query, tx_params).fetchone()

        total_offers = int((tx_row["total_offers"] if tx_row else 0) or 0)
        total_purchases = int((tx_row["total_purchases"] if tx_row else 0) or 0)
        total_revenue_usd = round(float((tx_row["total_revenue_usd"] if tx_row else 0.0) or 0.0), 2)
        today_revenue_usd = round(float((tx_row["today_revenue_usd"] if tx_row else 0.0) or 0.0), 2)
        cvr_percent = round((total_purchases / total_offers) * 100.0, 1) if total_offers > 0 else 0.0

        fans_query = f"""
            SELECT
                COUNT(*) AS total_fans,
                SUM(CASE WHEN is_muted = 0 AND is_low_priority = 0 THEN 1 ELSE 0 END) AS active_fans,
                SUM(CASE WHEN is_low_priority = 1 AND is_muted = 0 THEN 1 ELSE 0 END) AS low_priority_fans,
                SUM(CASE WHEN is_escalated = 1 AND is_muted = 0 THEN 1 ELSE 0 END) AS escalated_alerts
            FROM fans
            {where_char};
        """
        fans_row = conn.execute(fans_query, params_char).fetchone()

        return {
            "today_revenue_usd": today_revenue_usd,
            "total_revenue_usd": total_revenue_usd,
            "total_offers": total_offers,
            "total_purchases": total_purchases,
            "cvr_percent": cvr_percent,
            "total_fans": int((fans_row["total_fans"] if fans_row else 0) or 0),
            "active_fans": int((fans_row["active_fans"] if fans_row else 0) or 0),
            "low_priority_fans": int((fans_row["low_priority_fans"] if fans_row else 0) or 0),
            "escalated_alerts": int((fans_row["escalated_alerts"] if fans_row else 0) or 0),
        }
    finally:
        conn.close()


def get_escalated_fans(char_id: Optional[str] = None, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """緊急停止・要介入アラート中 (is_escalated=1 AND is_muted=0) の顧客リストを返す"""
    conn = get_db_connection(db_path)
    try:
        if char_id and char_id != "all":
            rows = conn.execute(
                "SELECT * FROM fans WHERE char_id = ? AND is_escalated = 1 AND is_muted = 0 ORDER BY last_contact_at DESC;",
                (char_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM fans WHERE is_escalated = 1 AND is_muted = 0 ORDER BY last_contact_at DESC;"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_fan_threads(
    char_id: Optional[str] = None,
    include_muted: bool = False,
    db_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """左右2ペインの左ペイン用：顧客スレッド一覧を最新メッセージ日時順で返す"""
    conn = get_db_connection(db_path)
    try:
        conditions = []
        params: List[Any] = []
        if char_id and char_id != "all":
            conditions.append("f.char_id = ?")
            params.append(char_id)
        if not include_muted:
            conditions.append("f.is_muted = 0")

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"""
            SELECT
                f.*,
                (
                    SELECT m.content
                    FROM dm_messages m
                    WHERE m.fan_id = f.fan_id AND m.char_id = f.char_id
                    ORDER BY m.msg_id DESC
                    LIMIT 1
                ) AS last_message_content,
                (
                    SELECT m.content_ja
                    FROM dm_messages m
                    WHERE m.fan_id = f.fan_id AND m.char_id = f.char_id
                    ORDER BY m.msg_id DESC
                    LIMIT 1
                ) AS last_message_content_ja,
                (
                    SELECT m.sender
                    FROM dm_messages m
                    WHERE m.fan_id = f.fan_id AND m.char_id = f.char_id
                    ORDER BY m.msg_id DESC
                    LIMIT 1
                ) AS last_message_sender,
                (
                    SELECT COUNT(*)
                    FROM dm_messages m
                    WHERE m.fan_id = f.fan_id AND m.char_id = f.char_id
                ) AS message_count
            FROM fans f
            {where_clause}
            ORDER BY COALESCE(f.last_contact_at, f.created_at, '') DESC;
        """
        rows = conn.execute(query, params).fetchall()
        threads: List[Dict[str, Any]] = []
        for r in rows:
            item = dict(r)
            try:
                item["psych_profile"] = json.loads(item.get("psych_profile_json") or "{}")
            except Exception:
                item["psych_profile"] = {}
            _enrich_fan_manual_and_decision_fields(item)
            item["has_unanswered_fan"] = str(item.get("last_message_sender") or "") == "fan"
            threads.append(item)
        return threads
    finally:
        conn.close()


def _parse_decision_logs_list(raw_val: Any) -> List[Dict[str, Any]]:
    if isinstance(raw_val, list):
        return [x for x in raw_val if isinstance(x, dict)]
    if isinstance(raw_val, dict):
        return [raw_val]
    if isinstance(raw_val, str) and raw_val.strip():
        try:
            parsed = json.loads(raw_val)
            if isinstance(parsed, list):
                return [x for x in parsed if isinstance(x, dict)]
            if isinstance(parsed, dict):
                return [parsed]
        except Exception:
            pass
    return []


def _enrich_fan_manual_and_decision_fields(
    fan_dict: Dict[str, Any],
    now_dt: Optional[datetime] = None,
    overdue_seconds: int = MANUAL_HOLD_OVERDUE_SECONDS,
) -> Dict[str, Any]:
    """手動対応保留状態・10分放置アラート判定・AI検討プロセスログ配列を辞書へ付与する"""
    ref_now = now_dt or datetime.now()
    manual_hold = int(fan_dict.get("manual_hold") or 0) == 1
    hold_at_str = fan_dict.get("manual_hold_at")
    elapsed_sec = 0.0
    if manual_hold and hold_at_str:
        dt = None
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
            try:
                dt = datetime.strptime(str(hold_at_str).strip(), fmt)
                break
            except ValueError:
                continue
        if dt is not None:
            elapsed_sec = max(0.0, (ref_now - dt).total_seconds())
    is_overdue = bool(manual_hold and elapsed_sec >= float(overdue_seconds))
    fan_dict["manual_hold"] = 1 if manual_hold else 0
    fan_dict["manual_hold_active"] = manual_hold
    fan_dict["manual_hold_at"] = hold_at_str
    fan_dict["manual_hold_elapsed_sec"] = int(elapsed_sec)
    fan_dict["is_manual_overdue"] = is_overdue

    dlogs = _parse_decision_logs_list(fan_dict.get("decision_log_json"))
    fan_dict["decision_logs"] = dlogs
    fan_dict["latest_decision_log"] = dlogs[-1] if dlogs else None
    return fan_dict


def append_fan_decision_log(
    char_id: str,
    fan_id: str,
    decision_entry: Dict[str, Any],
    max_entries: int = 25,
    db_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """fans.decision_log_json に1ターン分のAI検討プロセスログを追記保存する"""
    if not char_id or not fan_id or not isinstance(decision_entry, dict):
        return []
    now_str = _now_iso()
    entry = dict(decision_entry)
    entry.setdefault("recorded_at", now_str)
    conn = get_db_connection(db_path)
    try:
        cur = conn.cursor()
        row = cur.execute(
            "SELECT decision_log_json FROM fans WHERE char_id = ? AND fan_id = ?;",
            (char_id, fan_id),
        ).fetchone()
        if not row:
            assigned_ab = _assign_balanced_ab_group_with_cursor(cur, char_id)
            logs = [entry]
            cur.execute(
                """
                INSERT INTO fans (
                    fan_id, char_id, username, current_tier, total_spent_usd,
                    psych_profile_json, sales_phase, ab_group, decision_log_json, last_contact_at, created_at
                ) VALUES (?, ?, ?, 'free_follower', 0.0, '{}', '1_Icebreak', ?, ?, ?, ?);
                """,
                (fan_id, char_id, fan_id, assigned_ab, json.dumps(logs, ensure_ascii=False), now_str, now_str),
            )
        else:
            logs = _parse_decision_logs_list(row["decision_log_json"])
            logs.append(entry)
            if len(logs) > max_entries:
                logs = logs[-max_entries:]
            cur.execute(
                "UPDATE fans SET decision_log_json = ? WHERE char_id = ? AND fan_id = ?;",
                (json.dumps(logs, ensure_ascii=False), char_id, fan_id),
            )
        conn.commit()
        return logs
    finally:
        conn.close()


def set_fan_manual_hold(
    char_id: str,
    fan_id: str,
    hold: bool,
    hold_at: Optional[str] = None,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """指定スレッドの手動対応保留フラグ (manual_hold / manual_hold_at) を設定・解除する"""
    now_str = hold_at or _now_iso()
    conn = get_db_connection(db_path)
    try:
        cur = conn.cursor()
        row = cur.execute(
            "SELECT fan_id FROM fans WHERE char_id = ? AND fan_id = ?;",
            (char_id, fan_id),
        ).fetchone()
        if not row:
            assigned_ab = _assign_balanced_ab_group_with_cursor(cur, char_id)
            cur.execute(
                """
                INSERT INTO fans (
                    fan_id, char_id, username, current_tier, total_spent_usd,
                    psych_profile_json, sales_phase, ab_group, manual_hold, manual_hold_at, last_contact_at, created_at
                ) VALUES (?, ?, ?, 'free_follower', 0.0, '{}', '1_Icebreak', ?, ?, ?, ?, ?);
                """,
                (
                    fan_id,
                    char_id,
                    fan_id,
                    assigned_ab,
                    1 if hold else 0,
                    now_str if hold else None,
                    now_str,
                    now_str,
                ),
            )
        else:
            cur.execute(
                "UPDATE fans SET manual_hold = ?, manual_hold_at = ? WHERE char_id = ? AND fan_id = ?;",
                (1 if hold else 0, now_str if hold else None, char_id, fan_id),
            )
        conn.commit()
    finally:
        conn.close()
    rec = get_fan_record(char_id, fan_id, db_path=db_path) or {}
    return _enrich_fan_manual_and_decision_fields(rec)


def get_thread_detail(
    char_id: str,
    fan_id: str,
    now_dt: Optional[datetime] = None,
    overdue_seconds: int = MANUAL_HOLD_OVERDUE_SECONDS,
    db_path: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """左右2ペインの右ペイン用：指定スレッドの顧客情報・AI顧客カルテ・検討ログ・全会話ログ（content & content_ja）・PPV履歴を取得"""
    conn = get_db_connection(db_path)
    try:
        fan_row = conn.execute(
            "SELECT * FROM fans WHERE char_id = ? AND fan_id = ?;",
            (char_id, fan_id),
        ).fetchone()
        if not fan_row:
            return None

        fan_dict = dict(fan_row)
        try:
            fan_dict["psych_profile"] = json.loads(fan_dict.get("psych_profile_json") or "{}")
        except Exception:
            fan_dict["psych_profile"] = {}
        _enrich_fan_manual_and_decision_fields(fan_dict, now_dt=now_dt, overdue_seconds=overdue_seconds)

        msg_rows = conn.execute(
            """
            SELECT msg_id, fan_id, char_id, sender, content, content_ja, sales_phase,
                   prompt_version, decision_log_json, ppv_id, ppv_price_usd, tx_id, created_at
            FROM dm_messages
            WHERE char_id = ? AND fan_id = ?
            ORDER BY msg_id ASC;
            """,
            (char_id, fan_id),
        ).fetchall()

        tx_rows = conn.execute(
            "SELECT * FROM ppv_transactions WHERE char_id = ? AND fan_id = ? ORDER BY tx_id DESC;",
            (char_id, fan_id),
        ).fetchall()
        transactions_list: List[Dict[str, Any]] = [dict(t) for t in tx_rows]

        vault_map = _load_char_vault_lookup_map(char_id, conn=conn)
        tx_by_id: Dict[int, Dict[str, Any]] = {}
        ppv_txs_asc: List[Dict[str, Any]] = []
        for tx in reversed(transactions_list):
            tid = int(tx.get("tx_id") or 0)
            if tid:
                tx_by_id[tid] = tx
            pid = str(tx.get("ppv_id") or "").strip()
            if pid and pid in vault_map:
                v_meta = vault_map[pid]
                tx["filename"] = v_meta.get("filename")
                tx["media_type"] = v_meta.get("media_type")
                tx["ladder_step"] = v_meta.get("ladder_step")
                tx["tags"] = v_meta.get("tags") or []
                tx["description_memo"] = v_meta.get("description_memo") or v_meta.get("pitch_note") or ""
                tx["pitch_hint"] = v_meta.get("pitch_hint") or ""
                tx["media_url"] = v_meta.get("media_url")
                tx["file_exists"] = v_meta.get("file_exists", True)
            if tx.get("tx_type") == "ppv" and pid:
                ppv_txs_asc.append(tx)

        used_fallback_tx_ids = {
            int(m["tx_id"]) for m in msg_rows if m["tx_id"] is not None and int(m["tx_id"] or 0) > 0
        }

        messages: List[Dict[str, Any]] = []
        msg_dlogs: List[Dict[str, Any]] = []
        for m in msg_rows:
            md = dict(m)
            raw_dl = md.get("decision_log_json")
            parsed_dl: Optional[Dict[str, Any]] = None
            if raw_dl:
                try:
                    cand_dl = json.loads(raw_dl) if isinstance(raw_dl, str) else raw_dl
                    if isinstance(cand_dl, dict):
                        parsed_dl = cand_dl
                        md["decision_log"] = parsed_dl
                        msg_dlogs.append(parsed_dl)
                except Exception:
                    pass

            if md.get("sender") in ("ai", "human_manual"):
                att = _resolve_message_ppv_attachment(
                    char_id=char_id,
                    msg_dict=md,
                    parsed_dl=parsed_dl,
                    vault_map=vault_map,
                    tx_by_id=tx_by_id,
                    ppv_txs_asc=ppv_txs_asc,
                    used_fallback_tx_ids=used_fallback_tx_ids,
                )
                if att:
                    md["ppv_attachment"] = att

            messages.append(md)

        fan_logs = fan_dict.get("decision_logs") or []
        merged_logs = fan_logs if fan_logs else msg_dlogs
        return {
            "fan": fan_dict,
            "messages": messages,
            "transactions": transactions_list,
            "decision_logs": merged_logs,
            "latest_decision_log": merged_logs[-1] if merged_logs else None,
        }
    finally:
        conn.close()



def update_dm_message_ja(msg_id: int, content_ja: str, db_path: Optional[str] = None) -> bool:
    """既存メッセージの日本語訳 (content_ja) を更新する"""
    conn = get_db_connection(db_path)
    try:
        cur = conn.execute(
            "UPDATE dm_messages SET content_ja = ? WHERE msg_id = ?;",
            (content_ja, int(msg_id)),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def add_dm_message(
    char_id: str,
    fan_id: str,
    sender: str,
    content: str,
    content_ja: Optional[str] = None,
    sales_phase: Optional[str] = None,
    prompt_version: str = "v1.0-manual",
    resolve_escalation: bool = False,
    decision_log: Optional[Union[Dict[str, Any], str]] = None,
    clear_manual_hold: bool = False,
    ppv_id: Optional[str] = None,
    ppv_price_usd: Optional[float] = None,
    tx_id: Optional[int] = None,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    DMメッセージ（content: 英語原文, content_ja: 日本語訳, decision_log_json: AI検討ログ, PPV添付情報）を dm_messages に記録し、
    fans の last_contact_at を更新する。手動介入送信時は manual_hold も自動解除する。
    """
    now_str = _now_iso()
    decision_log_str: Optional[str] = None
    if isinstance(decision_log, dict):
        decision_log_str = json.dumps(decision_log, ensure_ascii=False)
    elif isinstance(decision_log, str) and decision_log.strip():
        decision_log_str = decision_log.strip()

    norm_ppv_id = str(ppv_id).strip() if ppv_id else None
    norm_ppv_price = round(float(ppv_price_usd), 2) if ppv_price_usd is not None else None
    norm_tx_id = int(tx_id) if tx_id is not None else None

    conn = get_db_connection(db_path)
    try:
        cur = conn.cursor()
        fan_row = cur.execute(
            "SELECT sales_phase FROM fans WHERE char_id = ? AND fan_id = ?;",
            (char_id, fan_id),
        ).fetchone()

        effective_phase = sales_phase or (fan_row["sales_phase"] if fan_row else "1_Icebreak")

        if not fan_row:
            assigned_ab = _assign_balanced_ab_group_with_cursor(cur, char_id)
            cur.execute(
                """
                INSERT INTO fans (
                    fan_id, char_id, username, current_tier, total_spent_usd,
                    psych_profile_json, sales_phase, ab_group, last_contact_at, created_at
                ) VALUES (?, ?, ?, 'free_follower', 0.0, '{}', ?, ?, ?, ?);
                """,
                (fan_id, char_id, fan_id, effective_phase, assigned_ab, now_str, now_str),
            )

        cur.execute(
            """
            INSERT INTO dm_messages (
                fan_id, char_id, sender, content, content_ja, sales_phase,
                prompt_version, decision_log_json, ppv_id, ppv_price_usd, tx_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                fan_id,
                char_id,
                sender,
                content,
                content_ja,
                effective_phase,
                prompt_version,
                decision_log_str,
                norm_ppv_id,
                norm_ppv_price,
                norm_tx_id,
                now_str,
            ),
        )
        msg_id = cur.lastrowid

        should_clear_hold = bool(clear_manual_hold or sender == "human_manual")
        if resolve_escalation and should_clear_hold:
            cur.execute(
                """
                UPDATE fans
                SET last_contact_at = ?, is_escalated = 0, escalation_reason = NULL,
                    consecutive_unanswered = 0, manual_hold = 0, manual_hold_at = NULL
                WHERE char_id = ? AND fan_id = ?;
                """,
                (now_str, char_id, fan_id),
            )
        elif resolve_escalation:
            cur.execute(
                """
                UPDATE fans
                SET last_contact_at = ?, is_escalated = 0, escalation_reason = NULL, consecutive_unanswered = 0
                WHERE char_id = ? AND fan_id = ?;
                """,
                (now_str, char_id, fan_id),
            )
        elif should_clear_hold:
            cur.execute(
                """
                UPDATE fans
                SET last_contact_at = ?, manual_hold = 0, manual_hold_at = NULL
                WHERE char_id = ? AND fan_id = ?;
                """,
                (now_str, char_id, fan_id),
            )
        else:
            cur.execute(
                "UPDATE fans SET last_contact_at = ? WHERE char_id = ? AND fan_id = ?;",
                (now_str, char_id, fan_id),
            )

        conn.commit()
        return {
            "msg_id": msg_id,
            "fan_id": fan_id,
            "char_id": char_id,
            "sender": sender,
            "content": content,
            "content_ja": content_ja,
            "sales_phase": effective_phase,
            "prompt_version": prompt_version,
            "decision_log_json": decision_log_str,
            "ppv_id": norm_ppv_id,
            "ppv_price_usd": norm_ppv_price,
            "tx_id": norm_tx_id,
            "created_at": now_str,
        }
    finally:
        conn.close()



def resolve_fan_escalation(char_id: str, fan_id: str, db_path: Optional[str] = None) -> bool:
    """緊急停止アラートおよび手動保留フラグを解除し、AI自動返信を再開させる"""
    conn = get_db_connection(db_path)
    try:
        cur = conn.execute(
            """
            UPDATE fans
            SET is_escalated = 0, escalation_reason = NULL, consecutive_unanswered = 0,
                manual_hold = 0, manual_hold_at = NULL
            WHERE char_id = ? AND fan_id = ?;
            """,
            (char_id, fan_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def mute_fan(char_id: str, fan_id: str, muted: bool = True, db_path: Optional[str] = None) -> bool:
    """指定顧客を永久ミュート（またはミュート解除）する"""
    conn = get_db_connection(db_path)
    try:
        cur = conn.execute(
            "UPDATE fans SET is_muted = ?, is_escalated = 0 WHERE char_id = ? AND fan_id = ?;",
            (1 if muted else 0, char_id, fan_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def record_ppv_transaction(
    char_id: str,
    fan_id: str,
    ppv_id: Optional[str],
    offered_price: float,
    tx_type: str = "ppv",
    is_unlocked: int = 0,
    db_path: Optional[str] = None,
) -> int:
    """PPV提案または購入トランザクションを記録する"""
    now_str = _now_iso()
    unlocked_at = now_str if is_unlocked else None
    conn = get_db_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO ppv_transactions (
                fan_id, char_id, ppv_id, tx_type, offered_price,
                is_unlocked, reminder_sent, offered_at, unlocked_at
            ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?);
            """,
            (fan_id, char_id, ppv_id, tx_type, float(offered_price), int(is_unlocked), now_str, unlocked_at),
        )
        tx_id = cur.lastrowid
        if is_unlocked:
            cur.execute(
                """
                UPDATE fans
                SET total_spent_usd = COALESCE(total_spent_usd, 0.0) + ?,
                    consecutive_ppv_ignored = 0,
                    is_low_priority = 0
                WHERE char_id = ? AND fan_id = ?;
                """,
                (float(offered_price), char_id, fan_id),
            )
        conn.commit()
        return int(tx_id)
    finally:
        conn.close()


def get_fan_record(char_id: str, fan_id: str, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """指定した (char_id, fan_id) のファンレコードを辞書で取得する"""
    conn = get_db_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM fans WHERE char_id = ? AND fan_id = ?;",
            (char_id, fan_id),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["psych_profile"] = json.loads(d.get("psych_profile_json") or "{}")
        except Exception:
            d["psych_profile"] = {}
        return d
    finally:
        conn.close()


def get_unlocked_ppv_ids(char_id: str, fan_id: str, db_path: Optional[str] = None) -> List[str]:
    """ppv_transactions から当該顧客が既に購入済み (is_unlocked == 1) の ppv_id リストを返す"""
    if not char_id or not fan_id:
        return []
    conn = get_db_connection(db_path)
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT ppv_id
            FROM ppv_transactions
            WHERE char_id = ? AND fan_id = ? AND is_unlocked = 1 AND ppv_id IS NOT NULL;
            """,
            (char_id, fan_id),
        ).fetchall()
        return [str(r["ppv_id"]) for r in rows if r["ppv_id"]]
    finally:
        conn.close()


def save_fan_psych_profile_db(
    char_id: str,
    fan_id: str,
    profile_dict: Dict[str, Any],
    username: Optional[str] = None,
    db_path: Optional[str] = None,
) -> bool:
    """fans.psych_profile_json を更新する（未存在時は新規作成）"""
    if not char_id or not fan_id:
        return False
    now_str = _now_iso()
    profile_json_str = json.dumps(profile_dict or {}, ensure_ascii=False)
    conn = get_db_connection(db_path)
    try:
        cur = conn.cursor()
        exists = cur.execute(
            "SELECT 1 FROM fans WHERE char_id = ? AND fan_id = ?;",
            (char_id, fan_id),
        ).fetchone()
        if exists:
            cur.execute(
                """
                UPDATE fans
                SET psych_profile_json = ?, last_contact_at = COALESCE(last_contact_at, ?)
                WHERE char_id = ? AND fan_id = ?;
                """,
                (profile_json_str, now_str, char_id, fan_id),
            )
        else:
            assigned_ab = _assign_balanced_ab_group_with_cursor(cur, char_id)
            cur.execute(
                """
                INSERT INTO fans (
                    fan_id, char_id, username, current_tier, total_spent_usd,
                    psych_profile_json, sales_phase, ab_group, last_contact_at, created_at
                ) VALUES (?, ?, ?, 'free_follower', 0.0, ?, '1_Icebreak', ?, ?, ?);
                """,
                (fan_id, char_id, username or fan_id, profile_json_str, assigned_ab, now_str, now_str),
            )
        conn.commit()
        return True
    finally:
        conn.close()


def upsert_fan_sales_state(
    char_id: str,
    fan_id: str,
    username: Optional[str] = None,
    current_tier: Optional[str] = None,
    total_spent_usd: Optional[float] = None,
    sales_phase: Optional[str] = None,
    consecutive_unanswered: Optional[int] = None,
    consecutive_ppv_ignored: Optional[int] = None,
    is_low_priority: Optional[int] = None,
    is_escalated: Optional[int] = None,
    escalation_reason: Optional[str] = None,
    psych_profile_dict: Optional[Dict[str, Any]] = None,
    ab_group: Optional[str] = None,
    last_contact_at: Optional[str] = None,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """fans テーブルの営業ステータス・フラグ・カルテを一括作成または更新する"""
    now_str = last_contact_at or _now_iso()
    conn = get_db_connection(db_path)
    try:
        cur = conn.cursor()
        existing = cur.execute(
            "SELECT * FROM fans WHERE char_id = ? AND fan_id = ?;",
            (char_id, fan_id),
        ).fetchone()

        if not existing:
            init_profile_str = json.dumps(psych_profile_dict or {}, ensure_ascii=False)
            norm_ab = (
                str(ab_group).strip().upper()
                if ab_group and str(ab_group).strip().upper() in ("A", "B")
                else _assign_balanced_ab_group_with_cursor(cur, char_id)
            )
            cur.execute(
                """
                INSERT INTO fans (
                    fan_id, char_id, username, current_tier, total_spent_usd,
                    psych_profile_json, sales_phase, consecutive_unanswered,
                    consecutive_ppv_ignored, is_low_priority, is_escalated,
                    escalation_reason, ab_group, last_contact_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    fan_id,
                    char_id,
                    username or fan_id,
                    current_tier or "free_follower",
                    float(total_spent_usd or 0.0),
                    init_profile_str,
                    sales_phase or "1_Icebreak",
                    int(consecutive_unanswered or 0),
                    int(consecutive_ppv_ignored or 0),
                    int(is_low_priority or 0),
                    int(is_escalated or 0),
                    escalation_reason,
                    norm_ab,
                    now_str,
                    now_str,
                ),
            )
        else:
            updates: List[str] = ["last_contact_at = ?"]
            params: List[Any] = [now_str]
            if username is not None:
                updates.append("username = ?")
                params.append(username)
            if current_tier is not None:
                updates.append("current_tier = ?")
                params.append(current_tier)
            if total_spent_usd is not None:
                updates.append("total_spent_usd = ?")
                params.append(float(total_spent_usd))
            if sales_phase is not None:
                updates.append("sales_phase = ?")
                params.append(sales_phase)
            if consecutive_unanswered is not None:
                updates.append("consecutive_unanswered = ?")
                params.append(int(consecutive_unanswered))
            if consecutive_ppv_ignored is not None:
                updates.append("consecutive_ppv_ignored = ?")
                params.append(int(consecutive_ppv_ignored))
            if is_low_priority is not None:
                updates.append("is_low_priority = ?")
                params.append(int(is_low_priority))
            if is_escalated is not None:
                updates.append("is_escalated = ?")
                params.append(int(is_escalated))
                updates.append("escalation_reason = ?")
                params.append(escalation_reason)
            if psych_profile_dict is not None:
                updates.append("psych_profile_json = ?")
                params.append(json.dumps(psych_profile_dict, ensure_ascii=False))
            if ab_group is not None and str(ab_group).strip().upper() in ("A", "B"):
                updates.append("ab_group = ?")
                params.append(str(ab_group).strip().upper())

            params.extend([char_id, fan_id])
            cur.execute(
                f"UPDATE fans SET {', '.join(updates)} WHERE char_id = ? AND fan_id = ?;",
                tuple(params),
            )
        conn.commit()
    finally:
        conn.close()

    return get_fan_record(char_id, fan_id, db_path=db_path) or {}


def _parse_iso_datetime(dt_str: Optional[str]) -> Optional[datetime]:
    """ISO8601日時文字列 ('YYYY-MM-DDTHH:MM:SS' または 'YYYY-MM-DD HH:MM:SS') を datetime に変換する"""
    if not dt_str:
        return None
    s = str(dt_str).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1]
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def record_ppv_offer(
    char_id: str,
    fan_id: str,
    ppv_id: Optional[str],
    offered_price: float,
    tx_type: str = "ppv",
    offered_at: Optional[str] = None,
    is_unlocked: int = 0,
    reminder_sent: int = 0,
    db_path: Optional[str] = None,
) -> int:
    """ppv_transactions にPPV提案レコードを追加し、tx_id を返す"""
    ts = offered_at or _now_iso()
    conn = get_db_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO ppv_transactions (
                fan_id, char_id, ppv_id, tx_type, offered_price,
                is_unlocked, reminder_sent, offered_at, unlocked_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                fan_id,
                char_id,
                ppv_id,
                tx_type or "ppv",
                float(offered_price or 0.0),
                int(is_unlocked or 0),
                int(reminder_sent or 0),
                ts,
                ts if int(is_unlocked or 0) == 1 else None,
            ),
        )
        tx_id = int(cur.lastrowid or 0)
        conn.commit()
        return tx_id
    finally:
        conn.close()


def mark_ppv_unlocked(
    char_id: str,
    fan_id: str,
    ppv_id: Optional[str] = None,
    tx_id: Optional[int] = None,
    unlocked_at: Optional[str] = None,
    db_path: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    未解除のPPVトランザクションを購入済み (is_unlocked = 1) に更新し、
    該当顧客の total_spent_usd 加算および consecutive_ppv_ignored = 0 リセットを行う。
    """
    ts = unlocked_at or _now_iso()
    conn = get_db_connection(db_path)
    try:
        cur = conn.cursor()
        row = None
        if tx_id is not None:
            row = cur.execute(
                "SELECT * FROM ppv_transactions WHERE tx_id = ? AND char_id = ? AND fan_id = ?;",
                (int(tx_id), char_id, fan_id),
            ).fetchone()
        elif ppv_id:
            row = cur.execute(
                """
                SELECT * FROM ppv_transactions
                WHERE char_id = ? AND fan_id = ? AND ppv_id = ? AND is_unlocked = 0
                ORDER BY offered_at DESC, tx_id DESC LIMIT 1;
                """,
                (char_id, fan_id, ppv_id),
            ).fetchone()
        else:
            row = cur.execute(
                """
                SELECT * FROM ppv_transactions
                WHERE char_id = ? AND fan_id = ? AND is_unlocked = 0 AND tx_type = 'ppv'
                ORDER BY offered_at DESC, tx_id DESC LIMIT 1;
                """,
                (char_id, fan_id),
            ).fetchone()

        if not row:
            return None

        target_tx_id = int(row["tx_id"])
        was_unlocked = int(row["is_unlocked"] or 0)
        price = float(row["offered_price"] or 0.0)

        if was_unlocked == 0:
            cur.execute(
                "UPDATE ppv_transactions SET is_unlocked = 1, unlocked_at = ? WHERE tx_id = ?;",
                (ts, target_tx_id),
            )
            cur.execute(
                """
                UPDATE fans
                SET total_spent_usd = COALESCE(total_spent_usd, 0.0) + ?,
                    consecutive_ppv_ignored = 0,
                    is_low_priority = 0,
                    last_contact_at = ?
                WHERE char_id = ? AND fan_id = ?;
                """,
                (price, ts, char_id, fan_id),
            )
            conn.commit()

        updated = cur.execute(
            "SELECT * FROM ppv_transactions WHERE tx_id = ?;",
            (target_tx_id,),
        ).fetchone()
        return dict(updated) if updated else None
    finally:
        conn.close()


def mark_fomo_reminder_sent(tx_id: int, db_path: Optional[str] = None) -> bool:
    """ppv_transactions.reminder_sent = 1 に更新し、二重送信を防止する"""
    conn = get_db_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE ppv_transactions SET reminder_sent = 1 WHERE tx_id = ? AND reminder_sent = 0;",
            (int(tx_id),),
        )
        changed = cur.rowcount > 0
        conn.commit()
        return changed
    finally:
        conn.close()


def mark_fan_fomo_reminders_sent(
    char_id: str,
    fan_id: str,
    tx_ids: Optional[List[int]] = None,
    db_path: Optional[str] = None,
) -> int:
    """
    指定した (char_id, fan_id) の未追撃PPVレコード (tx_type == 'ppv' AND is_unlocked == 0 AND reminder_sent == 0)
    をすべて一括で reminder_sent = 1 に更新し、更新件数を返す。
    tx_ids が指定されている場合でも、同一ファンの未追撃レコードをすべて取りこぼしなく一括更新する。
    """
    if not char_id or not fan_id:
        return 0
    conn = get_db_connection(db_path)
    try:
        cur = conn.cursor()
        if tx_ids:
            clean_ids = sorted({int(tid) for tid in tx_ids if int(tid) > 0})
            if clean_ids:
                placeholders = ",".join("?" for _ in clean_ids)
                cur.execute(
                    f"""
                    UPDATE ppv_transactions
                    SET reminder_sent = 1
                    WHERE char_id = ?
                      AND fan_id = ?
                      AND reminder_sent = 0
                      AND (
                          (tx_type = 'ppv' AND is_unlocked = 0)
                          OR tx_id IN ({placeholders})
                      );
                    """,
                    tuple([char_id, fan_id] + clean_ids),
                )
            else:
                cur.execute(
                    """
                    UPDATE ppv_transactions
                    SET reminder_sent = 1
                    WHERE char_id = ? AND fan_id = ? AND tx_type = 'ppv' AND is_unlocked = 0 AND reminder_sent = 0;
                    """,
                    (char_id, fan_id),
                )
        else:
            cur.execute(
                """
                UPDATE ppv_transactions
                SET reminder_sent = 1
                WHERE char_id = ? AND fan_id = ? AND tx_type = 'ppv' AND is_unlocked = 0 AND reminder_sent = 0;
                """,
                (char_id, fan_id),
            )
        updated_count = int(cur.rowcount or 0)
        conn.commit()
        return updated_count
    finally:
        conn.close()


def get_pending_fomo_reminders(
    min_hours: float = 6.0,
    max_hours: Optional[float] = 12.0,
    char_id: Optional[str] = None,
    now_dt: Optional[datetime] = None,
    db_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    ppv_transactions から以下を満たす未開封PPVレコードを抽出する：
    - tx_type == 'ppv'
    - is_unlocked == 0
    - reminder_sent == 0
    - offered_at から min_hours (6時間) 以上経過 (max_hours 指定時は max_hours 以内)
    - 顧客が緊急停止 (is_escalated == 1)・ミュート (is_muted == 1)・省エネ (is_low_priority == 1) でないこと
    """
    ref_now = now_dt or datetime.now()
    conn = get_db_connection(db_path)
    try:
        sql = """
            SELECT
                t.tx_id,
                t.fan_id,
                t.char_id,
                t.ppv_id,
                t.tx_type,
                t.offered_price,
                t.is_unlocked,
                t.reminder_sent,
                t.offered_at,
                t.unlocked_at,
                COALESCE(f.username, t.fan_id) AS username,
                COALESCE(f.current_tier, 'free_follower') AS current_tier,
                COALESCE(f.total_spent_usd, 0.0) AS total_spent_usd,
                COALESCE(f.sales_phase, '4_Lock_Close') AS sales_phase,
                COALESCE(f.is_escalated, 0) AS is_escalated,
                COALESCE(f.is_muted, 0) AS is_muted,
                COALESCE(f.is_low_priority, 0) AS is_low_priority,
                COALESCE(f.psych_profile_json, '{}') AS psych_profile_json
            FROM ppv_transactions t
            LEFT JOIN fans f ON f.char_id = t.char_id AND f.fan_id = t.fan_id
            WHERE t.tx_type = 'ppv'
              AND t.is_unlocked = 0
              AND t.reminder_sent = 0
        """
        params: List[Any] = []
        if char_id and char_id != "all":
            sql += " AND t.char_id = ?"
            params.append(char_id)
        sql += " ORDER BY t.offered_at ASC, t.tx_id ASC;"

        rows = conn.execute(sql, tuple(params)).fetchall()
        results: List[Dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            if int(d.get("is_escalated") or 0) == 1:
                continue
            if int(d.get("is_muted") or 0) == 1:
                continue
            if int(d.get("is_low_priority") or 0) == 1:
                continue

            offered_dt = _parse_iso_datetime(d.get("offered_at"))
            if not offered_dt:
                continue
            elapsed_hours = (ref_now - offered_dt).total_seconds() / 3600.0
            if elapsed_hours < float(min_hours):
                continue
            if max_hours is not None and elapsed_hours > float(max_hours):
                continue

            d["elapsed_hours"] = round(elapsed_hours, 2)
            results.append(d)
        return results
    finally:
        conn.close()


def get_unlocked_ppv_transactions(
    char_id: Optional[str] = None,
    db_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """購入済み (is_unlocked == 1) のPPVトランザクション一覧を顧客情報付きで取得する"""
    conn = get_db_connection(db_path)
    try:
        sql = """
            SELECT
                t.tx_id,
                t.fan_id,
                t.char_id,
                t.ppv_id,
                t.tx_type,
                t.offered_price,
                t.is_unlocked,
                t.reminder_sent,
                t.offered_at,
                t.unlocked_at,
                COALESCE(f.username, t.fan_id) AS username,
                COALESCE(f.current_tier, 'free_follower') AS current_tier,
                COALESCE(f.total_spent_usd, 0.0) AS total_spent_usd,
                COALESCE(f.sales_phase, '4_Lock_Close') AS sales_phase,
                COALESCE(f.is_escalated, 0) AS is_escalated,
                COALESCE(f.is_muted, 0) AS is_muted,
                COALESCE(f.is_low_priority, 0) AS is_low_priority,
                COALESCE(f.psych_profile_json, '{}') AS psych_profile_json
            FROM ppv_transactions t
            LEFT JOIN fans f ON f.char_id = t.char_id AND f.fan_id = t.fan_id
            WHERE t.tx_type = 'ppv'
              AND t.is_unlocked = 1
        """
        params: List[Any] = []
        if char_id and char_id != "all":
            sql += " AND t.char_id = ?"
            params.append(char_id)
        sql += " ORDER BY COALESCE(t.unlocked_at, t.offered_at) DESC, t.tx_id DESC;"
        rows = conn.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_fan_ppv_transactions(
    char_id: str,
    fan_id: str,
    db_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """特定顧客の全PPVトランザクション履歴を取得する"""
    conn = get_db_connection(db_path)
    try:
        rows = conn.execute(
            """
            SELECT * FROM ppv_transactions
            WHERE char_id = ? AND fan_id = ?
            ORDER BY offered_at ASC, tx_id ASC;
            """,
            (char_id, fan_id),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ==============================================================================
# Phase 2: 自己進化プレイブック (sales_playbooks) / A/B CVR集計 / 休眠掘り起こし DBヘルパー
# ==============================================================================

DEFAULT_PLAYBOOK_A_RULES_EN = (
    "1. Mirror the fan's emotional energy in 1-2 short, intimate sentences before pitching.\n"
    "2. Frame every PPV as a private secret filmed just for them rather than a generic paid product.\n"
    "3. Reference at least one preference or fetish from their psychological profile to deepen attachment.\n"
    "4. End each reply with a playful open question or soft scarcity tease to keep reply momentum high."
)

DEFAULT_PLAYBOOK_A_RULES_JA = (
    "1. 提案前に1〜2文の親密な相槌でファンの熱量に同調する。\n"
    "2. PPVは汎用商品ではなく「あなただけにこっそり見せる秘密」としてフレーミングする。\n"
    "3. 顧客カルテの好み・フェチ要素を最低1つ織り交ぜて特別感を演出する。\n"
    "4. 返信の末尾は軽い質問または希少性の焦らしで締め、会話の往復を維持する。"
)


def ensure_default_playbook_a(char_id: str, db_path: Optional[str] = None) -> Dict[str, Any]:
    """対象キャラにアクティブな A群（現行本番ルール）が無ければ初期ベースライン A を自動生成する"""
    conn = get_db_connection(db_path)
    try:
        cur = conn.cursor()
        row = cur.execute(
            """
            SELECT * FROM sales_playbooks
            WHERE char_id = ? AND ab_group = 'A' AND is_active = 1
            ORDER BY version_id DESC LIMIT 1;
            """,
            (char_id,),
        ).fetchone()
        if row:
            return dict(row)

        now_str = _now_iso()
        cur.execute(
            """
            INSERT INTO sales_playbooks (
                char_id, rules_text, rules_ja, analysis_summary_ja,
                ab_group, cvr_score, offers_count, purchases_count, is_active, created_at
            ) VALUES (?, ?, ?, ?, 'A', 0.0, 0, 0, 1, ?);
            """,
            (
                char_id,
                DEFAULT_PLAYBOOK_A_RULES_EN,
                DEFAULT_PLAYBOOK_A_RULES_JA,
                "初期ベースライン S.E.L.L. 営業ルール（Group A 本番稼働中）",
                now_str,
            ),
        )
        new_id = int(cur.lastrowid or 0)
        conn.commit()
        created = cur.execute("SELECT * FROM sales_playbooks WHERE version_id = ?;", (new_id,)).fetchone()
        return dict(created) if created else {}
    finally:
        conn.close()


def get_active_playbooks(char_id: str, db_path: Optional[str] = None) -> Dict[str, Optional[Dict[str, Any]]]:
    """対象キャラクターの現在アクティブな Group A / Group B プレイブックを返す"""
    ensure_default_playbook_a(char_id, db_path=db_path)
    conn = get_db_connection(db_path)
    try:
        out: Dict[str, Optional[Dict[str, Any]]] = {"A": None, "B": None}
        for grp in ("A", "B"):
            row = conn.execute(
                """
                SELECT * FROM sales_playbooks
                WHERE char_id = ? AND ab_group = ? AND is_active = 1
                ORDER BY version_id DESC LIMIT 1;
                """,
                (char_id, grp),
            ).fetchone()
            if row:
                out[grp] = dict(row)
        return out
    finally:
        conn.close()


def get_active_playbook_for_group(
    char_id: str,
    ab_group: str = "A",
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """ファンの ab_group に対応するアクティブなプレイブックを返す（B未生成時はAへフォールバック）"""
    grp = str(ab_group or "A").strip().upper()
    if grp not in ("A", "B"):
        grp = "A"
    playbooks = get_active_playbooks(char_id, db_path=db_path)
    chosen = playbooks.get(grp) or playbooks.get("A")
    return chosen or ensure_default_playbook_a(char_id, db_path=db_path)


def save_new_playbook(
    char_id: str,
    rules_text: str,
    ab_group: str = "B",
    rules_ja: str = "",
    analysis_summary_ja: str = "",
    cvr_score: float = 0.0,
    deactivate_same_group: bool = True,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """新しい営業プレイブックを sales_playbooks に保存し、同グループの旧版を非アクティブ化する"""
    grp = str(ab_group or "B").strip().upper()
    if grp not in ("A", "B"):
        grp = "B"
    now_str = _now_iso()
    conn = get_db_connection(db_path)
    try:
        cur = conn.cursor()
        if deactivate_same_group:
            cur.execute(
                "UPDATE sales_playbooks SET is_active = 0 WHERE char_id = ? AND ab_group = ? AND is_active = 1;",
                (char_id, grp),
            )
        cur.execute(
            """
            INSERT INTO sales_playbooks (
                char_id, rules_text, rules_ja, analysis_summary_ja,
                ab_group, cvr_score, offers_count, purchases_count, is_active, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 0, 0, 1, ?);
            """,
            (
                char_id,
                rules_text.strip(),
                (rules_ja or "").strip(),
                (analysis_summary_ja or "").strip(),
                grp,
                round(float(cvr_score or 0.0), 2),
                now_str,
            ),
        )
        new_id = int(cur.lastrowid or 0)
        conn.commit()
        row = cur.execute("SELECT * FROM sales_playbooks WHERE version_id = ?;", (new_id,)).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def compute_ab_cvr_stats(
    char_id: str,
    db_path: Optional[str] = None,
) -> Dict[str, Dict[str, Any]]:
    """A群 / B群ごとのファン人数・送信数・成約数 (is_unlocked == 1)・CVRスコアを集計し sales_playbooks を更新する"""
    conn = get_db_connection(db_path)
    try:
        cur = conn.cursor()
        fan_rows = cur.execute(
            """
            SELECT COALESCE(ab_group, 'A') AS grp, COUNT(*) AS fan_cnt
            FROM fans WHERE char_id = ? GROUP BY COALESCE(ab_group, 'A');
            """,
            (char_id,),
        ).fetchall()
        fan_counts = {"A": 0, "B": 0}
        for r in fan_rows:
            g = str(r["grp"] or "A").upper()
            if g in fan_counts:
                fan_counts[g] += int(r["fan_cnt"] or 0)

        tx_rows = cur.execute(
            """
            SELECT
                COALESCE(f.ab_group, 'A') AS grp,
                COUNT(t.tx_id) AS offers_cnt,
                SUM(CASE WHEN t.is_unlocked = 1 THEN 1 ELSE 0 END) AS purchases_cnt,
                SUM(CASE WHEN t.is_unlocked = 1 THEN t.offered_price ELSE 0.0 END) AS revenue_usd
            FROM ppv_transactions t
            INNER JOIN fans f ON f.char_id = t.char_id AND f.fan_id = t.fan_id
            WHERE t.char_id = ?
            GROUP BY COALESCE(f.ab_group, 'A');
            """,
            (char_id,),
        ).fetchall()

        stats: Dict[str, Dict[str, Any]] = {
            grp: {
                "ab_group": grp,
                "fans_count": fan_counts[grp],
                "offers_count": 0,
                "purchases_count": 0,
                "revenue_usd": 0.0,
                "cvr_score": 0.0,
                "cvr_rate": 0.0,
            }
            for grp in ("A", "B")
        }

        for r in tx_rows:
            g = str(r["grp"] or "A").upper()
            if g not in stats:
                continue
            offers = int(r["offers_cnt"] or 0)
            purchases = int(r["purchases_cnt"] or 0)
            stats[g].update(
                {
                    "offers_count": offers,
                    "purchases_count": purchases,
                    "revenue_usd": round(float(r["revenue_usd"] or 0.0), 2),
                    "cvr_score": round((purchases / offers) * 100.0, 2) if offers > 0 else 0.0,
                    "cvr_rate": round((purchases / offers), 4) if offers > 0 else 0.0,
                }
            )

        for grp in ("A", "B"):
            cur.execute(
                """
                UPDATE sales_playbooks
                SET cvr_score = ?, offers_count = ?, purchases_count = ?
                WHERE char_id = ? AND ab_group = ? AND is_active = 1;
                """,
                (
                    stats[grp]["cvr_score"],
                    stats[grp]["offers_count"],
                    stats[grp]["purchases_count"],
                    char_id,
                    grp,
                ),
            )
        conn.commit()
        return stats
    finally:
        conn.close()


def promote_playbook_b_to_a(
    char_id: str,
    b_version_id: int,
    b_cvr_score: float = 0.0,
    b_offers_count: int = 0,
    b_purchases_count: int = 0,
    db_path: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """検証勝利した Group B を新しい Group A (現行本番ルール) へ自動昇格させる"""
    conn = get_db_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE sales_playbooks SET is_active = 0 WHERE char_id = ? AND ab_group = 'A' AND is_active = 1;",
            (char_id,),
        )
        cur.execute(
            """
            UPDATE sales_playbooks
            SET ab_group = 'A',
                is_active = 1,
                cvr_score = ?,
                offers_count = ?,
                purchases_count = ?
            WHERE version_id = ? AND char_id = ?;
            """,
            (
                round(float(b_cvr_score or 0.0), 2),
                int(b_offers_count or 0),
                int(b_purchases_count or 0),
                int(b_version_id),
                char_id,
            ),
        )
        conn.commit()
        promoted = cur.execute(
            "SELECT * FROM sales_playbooks WHERE version_id = ?;",
            (int(b_version_id),),
        ).fetchone()
        return dict(promoted) if promoted else None
    finally:
        conn.close()


def get_dormant_fans(
    char_id: Optional[str] = None,
    inactive_days: int = 7,
    max_targets: int = 10,
    now_dt: Optional[datetime] = None,
    db_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    休眠課金ファン抽出：
    total_spent_usd > 0 / last_contact_at から inactive_days 日以上経過 /
    is_escalated == 0, is_muted == 0, is_low_priority == 0
    """
    ref_now = now_dt or datetime.now()
    threshold_sec = max(0.0, float(inactive_days)) * 86400.0
    conn = get_db_connection(db_path)
    try:
        sql = """
            SELECT *
            FROM fans
            WHERE COALESCE(total_spent_usd, 0.0) > 0.0
              AND COALESCE(is_escalated, 0) = 0
              AND COALESCE(is_muted, 0) = 0
              AND COALESCE(is_low_priority, 0) = 0
        """
        params: List[Any] = []
        if char_id and char_id != "all":
            sql += " AND char_id = ?"
            params.append(char_id)
        sql += " ORDER BY total_spent_usd DESC, last_contact_at ASC;"

        rows = conn.execute(sql, tuple(params)).fetchall()
        candidates: List[Dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            ref_str = d.get("last_contact_at") or d.get("created_at")
            last_dt = _parse_iso_datetime(ref_str)
            if not last_dt:
                continue
            elapsed_sec = (ref_now - last_dt).total_seconds()
            if elapsed_sec < threshold_sec:
                continue
            try:
                d["psych_profile"] = json.loads(d.get("psych_profile_json") or "{}")
            except Exception:
                d["psych_profile"] = {}
            d["inactive_days_actual"] = round(elapsed_sec / 86400.0, 1)
            candidates.append(d)
            if len(candidates) >= max(1, int(max_targets)):
                break
        return candidates
    finally:
        conn.close()


def get_tip_stats(char_id: Optional[str] = None, db_path: Optional[str] = None) -> Dict[str, Any]:
    """チップ誘導 (tx_type = 'tip') の提案回数・成約回数・売上金額を集計する"""
    conn = get_db_connection(db_path)
    try:
        sql = """
            SELECT
                COUNT(*) AS tip_offers_count,
                SUM(CASE WHEN is_unlocked = 1 THEN 1 ELSE 0 END) AS tip_unlocked_count,
                SUM(CASE WHEN is_unlocked = 1 THEN offered_price ELSE 0.0 END) AS tip_revenue_usd
            FROM ppv_transactions
            WHERE tx_type = 'tip'
        """
        params: List[Any] = []
        if char_id and char_id != "all":
            sql += " AND char_id = ?"
            params.append(char_id)
        row = conn.execute(sql, tuple(params)).fetchone()
        return {
            "tip_offers_count": int(row["tip_offers_count"] or 0) if row else 0,
            "tip_unlocked_count": int(row["tip_unlocked_count"] or 0) if row else 0,
            "tip_revenue_usd": round(float(row["tip_revenue_usd"] or 0.0), 2) if row else 0.0,
        }
    finally:
        conn.close()


# ==========================================
# PPV Vault DB 同期 ＆ メッセージPPV添付解決 ＆ 返信待機時間設定 (system_settings)
# ==========================================

def _step_label_ja(step_num: int) -> str:
    if int(step_num) == 1:
        return "Step 1: 導入"
    if int(step_num) == 2:
        return "Step 2: 本命"
    return "Step 3: 超秘蔵"


def upsert_ppv_vault_record(
    char_id: str,
    item: Dict[str, Any],
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """ppv_vault テーブルに1件のPPV素材（日本語 tags_json / description_memo / pitch_note 含む）を保存・更新する"""
    now_str = _now_iso()
    ppv_id = str(item.get("ppv_id") or "").strip()
    if not char_id or not ppv_id:
        return {}
    filename = str(item.get("filename") or "").replace("\\", "/").split("/")[-1].strip()
    media_type = str(item.get("media_type") or "video").strip().lower()
    ladder_step = max(1, min(3, int(item.get("ladder_step") or 1)))
    base_price = round(float(item.get("base_price_usd", 9.99)), 2)
    tripwire_price = round(float(item.get("tripwire_price_usd", 5.99)), 2)
    whale_price = round(float(item.get("whale_price_usd", 19.99)), 2)

    raw_tags = item.get("tags")
    if raw_tags is None and item.get("tags_json"):
        try:
            raw_tags = json.loads(str(item.get("tags_json")))
        except Exception:
            raw_tags = []
    if isinstance(raw_tags, str):
        tags_list = [t.strip().lstrip("#") for t in raw_tags.replace("、", ",").split(",") if t.strip()]
    elif isinstance(raw_tags, list):
        tags_list = [str(t).strip().lstrip("#") for t in raw_tags if str(t).strip()]
    else:
        tags_list = []
    tags_json_str = json.dumps(tags_list, ensure_ascii=False)

    desc_memo = str(
        item.get("description_memo")
        if item.get("description_memo") is not None
        else (item.get("pitch_note") or "")
    ).strip()
    pitch_note = str(item.get("pitch_note") if item.get("pitch_note") is not None else desc_memo).strip()
    if not desc_memo and pitch_note:
        desc_memo = pitch_note
    pitch_hint = str(item.get("pitch_hint") or "").strip()
    is_active = 1 if bool(item.get("is_active", True)) else 0
    created_at = str(item.get("created_at") or now_str)
    # [Ticket 4.6] 汎用フラグ ＋ カテゴリ別タグ
    is_general = 1 if int(item.get("is_general") or 0) == 1 else 0
    category_tags_json = str(item.get("category_tags_json") or "{}")

    conn = get_db_connection(db_path)
    try:
        conn.execute(
            """
            INSERT INTO ppv_vault (
                ppv_id, char_id, filename, media_type, ladder_step,
                base_price_usd, tripwire_price_usd, whale_price_usd,
                tags_json, description_memo, pitch_note, pitch_hint,
                is_general, category_tags_json,
                is_active, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(char_id, ppv_id) DO UPDATE SET
                filename = excluded.filename,
                media_type = excluded.media_type,
                ladder_step = excluded.ladder_step,
                base_price_usd = excluded.base_price_usd,
                tripwire_price_usd = excluded.tripwire_price_usd,
                whale_price_usd = excluded.whale_price_usd,
                tags_json = excluded.tags_json,
                description_memo = excluded.description_memo,
                pitch_note = excluded.pitch_note,
                pitch_hint = excluded.pitch_hint,
                is_general = excluded.is_general,
                category_tags_json = excluded.category_tags_json,
                is_active = excluded.is_active,
                updated_at = excluded.updated_at;
            """,
            (
                ppv_id, char_id, filename, media_type, ladder_step,
                base_price, tripwire_price, whale_price,
                tags_json_str, desc_memo, pitch_note, pitch_hint,
                is_general, category_tags_json,
                is_active, created_at, now_str,
            ),
        )
        conn.commit()
    except sqlite3.OperationalError:
        conn.close()
        init_db(db_path)
        return upsert_ppv_vault_record(char_id, item, db_path=db_path)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return {
        "ppv_id": ppv_id,
        "char_id": char_id,
        "filename": filename,
        "media_type": media_type,
        "ladder_step": ladder_step,
        "base_price_usd": base_price,
        "tripwire_price_usd": tripwire_price,
        "whale_price_usd": whale_price,
        "tags": tags_list,
        "tags_json": tags_json_str,
        "description_memo": desc_memo,
        "pitch_note": pitch_note,
        "pitch_hint": pitch_hint,
        "is_active": bool(is_active),
        "created_at": created_at,
        "updated_at": now_str,
    }
def sync_ppv_vault_items_db(
    char_id: str,
    items: List[Dict[str, Any]],
    db_path: Optional[str] = None,
) -> None:
    """vault_index.json の全アイテムリストを ppv_vault DB テーブルへ一括同期する"""
    if not char_id:
        return
    try:
        keep_ids = []
        for it in items or []:
            if isinstance(it, dict) and it.get("ppv_id"):
                upsert_ppv_vault_record(char_id, it, db_path=db_path)
                keep_ids.append(str(it["ppv_id"]))
        conn = get_db_connection(db_path)
        try:
            if keep_ids:
                placeholders = ",".join("?" for _ in keep_ids)
                conn.execute(
                    f"DELETE FROM ppv_vault WHERE char_id = ? AND ppv_id NOT IN ({placeholders});",
                    tuple([char_id] + keep_ids),
                )
            else:
                conn.execute("DELETE FROM ppv_vault WHERE char_id = ?;", (char_id,))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def get_ppv_vault_records_db(
    char_id: Optional[str] = None,
    db_path: Optional[str] = None,
) -> Dict[str, Dict[str, Any]]:
    """ppv_vault テーブルから ppv_id -> レコード辞書のマップを取得する"""
    conn = get_db_connection(db_path)
    try:
        if char_id and char_id != "all":
            rows = conn.execute(
                "SELECT * FROM ppv_vault WHERE char_id = ?;",
                (char_id,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM ppv_vault;").fetchall()
        out: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            d = dict(r)
            try:
                d["tags"] = json.loads(d.get("tags_json") or "[]")
                if not isinstance(d["tags"], list):
                    d["tags"] = []
            except Exception:
                d["tags"] = []
            out[str(d["ppv_id"])] = d
        return out
    except Exception:
        return {}
    finally:
        conn.close()


def _load_char_vault_lookup_map(
    char_id: str,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Dict[str, Any]]:
    """
    指定キャラクターの vault_index.json と ppv_vault DB の両方を統合し、
    ppv_id -> メタデータ（media_url, file_exists, tags, description_memo 等）の辞書を返す。
    """
    lookup: Dict[str, Dict[str, Any]] = {}
    vault_dir = os.path.join(config.CHARACTERS_ROOT, os.path.basename(char_id), "ppv_vault")
    index_path = os.path.join(vault_dir, "vault_index.json")

    if os.path.isfile(index_path):
        try:
            with open(index_path, "r", encoding="utf-8") as fp:
                raw = json.load(fp)
            raw_list = (
                raw.get("items")
                if isinstance(raw, dict) and isinstance(raw.get("items"), list)
                else (raw.get("videos") if isinstance(raw, dict) and isinstance(raw.get("videos"), list) else [])
            )
            for idx, it in enumerate(raw_list or []):
                if not isinstance(it, dict):
                    continue
                pid = str(it.get("ppv_id") or f"{char_id}_vault_{idx + 1:03d}")
                fname = os.path.basename(str(it.get("filename") or "").replace("\\", "/").strip())
                step_num = max(1, min(3, int(it.get("ladder_step") or 1)))
                tags = it.get("tags") if isinstance(it.get("tags"), list) else []
                memo = str(it.get("description_memo") or it.get("pitch_note") or "").strip()
                hint = str(it.get("pitch_hint") or "").strip()
                fpath = os.path.join(vault_dir, fname) if fname else ""
                f_exists = bool(fpath and os.path.isfile(fpath) and os.path.getsize(fpath) > 0)
                lookup[pid] = {
                    "ppv_id": pid,
                    "char_id": char_id,
                    "filename": fname,
                    "media_type": str(it.get("media_type") or ("image" if os.path.splitext(fname)[1].lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"} else "video")),
                    "ladder_step": step_num,
                    "step_label": _step_label_ja(step_num),
                    "base_price_usd": float(it.get("base_price_usd", 9.99)),
                    "tripwire_price_usd": float(it.get("tripwire_price_usd", 5.99)),
                    "whale_price_usd": float(it.get("whale_price_usd", 19.99)),
                    "tags": tags,
                    "description_memo": memo,
                    "pitch_note": memo,
                    "pitch_hint": hint,
                    "file_exists": f_exists,
                    "media_url": f"/api/vault/media/{char_id}/{fname}" if fname else "",
                }
        except Exception:
            pass
    if conn is not None:
        try:
            db_rows = conn.execute(
                "SELECT * FROM ppv_vault WHERE char_id = ?;",
                (char_id,),
            ).fetchall()
            for r in db_rows:
                d = dict(r)
                pid = str(d.get("ppv_id") or "")
                if not pid:
                    continue
                fname = os.path.basename(str(d.get("filename") or "").replace("\\", "/").strip())
                step_num = max(1, min(3, int(d.get("ladder_step") or 1)))
                try:
                    db_tags = json.loads(d.get("tags_json") or "[]")
                    if not isinstance(db_tags, list):
                        db_tags = []
                except Exception:
                    db_tags = []
                memo = str(d.get("description_memo") or d.get("pitch_note") or "").strip()
                hint = str(d.get("pitch_hint") or "").strip()
                fpath = os.path.join(vault_dir, fname) if fname else ""
                f_exists = bool(fpath and os.path.isfile(fpath) and os.path.getsize(fpath) > 0)
                if pid in lookup:
                    if db_tags and not lookup[pid].get("tags"):
                        lookup[pid]["tags"] = db_tags
                    if memo and not lookup[pid].get("description_memo"):
                        lookup[pid]["description_memo"] = memo
                        lookup[pid]["pitch_note"] = memo
                    if hint and not lookup[pid].get("pitch_hint"):
                        lookup[pid]["pitch_hint"] = hint
                else:
                    lookup[pid] = {
                        "ppv_id": pid,
                        "char_id": char_id,
                        "filename": fname,
                        "media_type": str(d.get("media_type") or "video"),
                        "ladder_step": step_num,
                        "step_label": _step_label_ja(step_num),
                        "base_price_usd": float(d.get("base_price_usd", 9.99)),
                        "tripwire_price_usd": float(d.get("tripwire_price_usd", 5.99)),
                        "whale_price_usd": float(d.get("whale_price_usd", 19.99)),
                        "tags": db_tags,
                        "description_memo": memo,
                        "pitch_note": memo,
                        "pitch_hint": hint,
                        "file_exists": f_exists,
                        "media_url": f"/api/vault/media/{char_id}/{fname}" if fname else "",
                    }
        except Exception:
            pass

    return lookup


def _resolve_message_ppv_attachment(
    char_id: str,
    msg_dict: Dict[str, Any],
    parsed_dl: Optional[Dict[str, Any]],
    vault_map: Dict[str, Dict[str, Any]],
    tx_by_id: Dict[int, Dict[str, Any]],
    ppv_txs_asc: List[Dict[str, Any]],
    used_fallback_tx_ids: set,
) -> Optional[Dict[str, Any]]:
    """
    AIまたは手動メッセージに紐づく送信済みPPVメディア詳細を構築する。
    """
    matched_tx: Optional[Dict[str, Any]] = None
    explicit_tx_id = int(msg_dict.get("tx_id") or 0)
    explicit_ppv_id = str(msg_dict.get("ppv_id") or "").strip()

    if explicit_tx_id and explicit_tx_id in tx_by_id:
        matched_tx = tx_by_id[explicit_tx_id]
        if not explicit_ppv_id:
            explicit_ppv_id = str(matched_tx.get("ppv_id") or "").strip()

    if not matched_tx and not explicit_ppv_id:
        msg_dt = _parse_iso_datetime(msg_dict.get("created_at"))
        if msg_dt and ppv_txs_asc:
            for cand_tx in ppv_txs_asc:
                cid_tx = int(cand_tx.get("tx_id") or 0)
                if cid_tx in used_fallback_tx_ids:
                    continue
                tx_dt = _parse_iso_datetime(cand_tx.get("offered_at"))
                if tx_dt and abs((tx_dt - msg_dt).total_seconds()) <= 90.0:
                    matched_tx = cand_tx
                    explicit_ppv_id = str(cand_tx.get("ppv_id") or "").strip()
                    if cid_tx:
                        used_fallback_tx_ids.add(cid_tx)
                    break

    if not explicit_ppv_id:
        return None

    v_meta = vault_map.get(explicit_ppv_id) or {}
    if not matched_tx:
        for cand_tx in reversed(ppv_txs_asc):
            if str(cand_tx.get("ppv_id") or "").strip() == explicit_ppv_id:
                matched_tx = cand_tx
                break

    price_val = (
        msg_dict.get("ppv_price_usd")
        if msg_dict.get("ppv_price_usd") is not None
        else (matched_tx.get("offered_price") if matched_tx else v_meta.get("base_price_usd", 9.99))
    )
    step_num = int(v_meta.get("ladder_step") or 1)
    fname = str(v_meta.get("filename") or explicit_ppv_id)
    is_unlocked = int(matched_tx.get("is_unlocked") or 0) if matched_tx else 0

    return {
        "ppv_id": explicit_ppv_id,
        "char_id": char_id,
        "tx_id": int(matched_tx.get("tx_id") or explicit_tx_id or 0) if (matched_tx or explicit_tx_id) else None,
        "filename": fname,
        "media_type": str(v_meta.get("media_type") or ("image" if os.path.splitext(fname)[1].lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"} else "video")),
        "media_url": v_meta.get("media_url") or f"/api/vault/media/{char_id}/{fname}",
        "file_exists": bool(v_meta.get("file_exists", False)),
        "ladder_step": step_num,
        "step_label": _step_label_ja(step_num),
        "offered_price_usd": round(float(price_val or 0.0), 2),
        "tags": v_meta.get("tags") or [],
        "description_memo": str(v_meta.get("description_memo") or v_meta.get("pitch_note") or ""),
        "pitch_hint": str(v_meta.get("pitch_hint") or ""),
        "is_unlocked": is_unlocked,
        "unlock_status_ja": "購入済" if is_unlocked == 1 else "未購入",
        "unlocked_at": matched_tx.get("unlocked_at") if matched_tx else None,
    }
def normalize_reply_delay_bounds(min_sec: Any, max_sec: Any) -> Tuple[int, int]:
    """返信待機秒数 (min_reply_delay_sec, max_reply_delay_sec) を安全に正規化する"""
    try:
        mn = int(round(float(min_sec)))
    except Exception:
        mn = 60
    try:
        mx = int(round(float(max_sec)))
    except Exception:
        mx = 300
    mn = max(0, min(3600, mn))
    mx = max(0, min(3600, mx))
    if mn > mx:
        mn, mx = mx, mn
    return mn, mx


def normalize_char_threshold_bounds(short_chars: Any, long_chars: Any) -> Tuple[int, int]:
    """受信メッセージ文字数基準 (short_msg_chars_threshold < long_msg_chars_threshold) を安全に正規化する"""
    try:
        sc = int(round(float(short_chars)))
    except Exception:
        sc = 20
    try:
        lc = int(round(float(long_chars)))
    except Exception:
        lc = 150
    sc = max(1, min(2000, sc))
    lc = max(2, min(5000, lc))
    if sc >= lc:
        lc = sc + 20
    return sc, lc


def get_system_reply_delay_settings(db_path: Optional[str] = None) -> Dict[str, int]:
    """
    system_settings テーブルから返信待機時間・文字数基準を取得する。
    デフォルト: min=60秒, max=300秒, short_msg_chars_threshold=20文字, long_msg_chars_threshold=150文字
    """
    conn = get_db_connection(db_path)
    try:
        rows = conn.execute(
            """
            SELECT setting_key, setting_value
            FROM system_settings
            WHERE setting_key IN (
                'min_reply_delay_sec',
                'max_reply_delay_sec',
                'short_msg_chars_threshold',
                'long_msg_chars_threshold'
            );
            """
        ).fetchall()
        kv = {str(r["setting_key"]): str(r["setting_value"]) for r in rows}
        mn, mx = normalize_reply_delay_bounds(kv.get("min_reply_delay_sec", 60), kv.get("max_reply_delay_sec", 300))
        sc, lc = normalize_char_threshold_bounds(
            kv.get("short_msg_chars_threshold", 20),
            kv.get("long_msg_chars_threshold", 150),
        )
        return {
            "min_reply_delay_sec": mn,
            "max_reply_delay_sec": mx,
            "short_msg_chars_threshold": sc,
            "long_msg_chars_threshold": lc,
        }
    except Exception:
        return {
            "min_reply_delay_sec": 60,
            "max_reply_delay_sec": 300,
            "short_msg_chars_threshold": 20,
            "long_msg_chars_threshold": 150,
        }
    finally:
        conn.close()


def save_system_reply_delay_settings(
    min_reply_delay_sec: Any,
    max_reply_delay_sec: Any,
    short_msg_chars_threshold: Any = None,
    long_msg_chars_threshold: Any = None,
    db_path: Optional[str] = None,
) -> Dict[str, int]:
    """system_settings テーブルへ待機秒数範囲および受信文字数しきい値を保存する"""
    current = get_system_reply_delay_settings(db_path=db_path)
    mn, mx = normalize_reply_delay_bounds(min_reply_delay_sec, max_reply_delay_sec)
    sc, lc = normalize_char_threshold_bounds(
        current["short_msg_chars_threshold"] if short_msg_chars_threshold is None else short_msg_chars_threshold,
        current["long_msg_chars_threshold"] if long_msg_chars_threshold is None else long_msg_chars_threshold,
    )
    now_str = _now_iso()
    conn = get_db_connection(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS system_settings (
                setting_key TEXT PRIMARY KEY,
                setting_value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        pairs = (
            ("min_reply_delay_sec", str(mn)),
            ("max_reply_delay_sec", str(mx)),
            ("short_msg_chars_threshold", str(sc)),
            ("long_msg_chars_threshold", str(lc)),
        )
        for k, v in pairs:
            conn.execute(
                """
                INSERT INTO system_settings (setting_key, setting_value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(setting_key) DO UPDATE SET
                    setting_value = excluded.setting_value,
                    updated_at = excluded.updated_at;
                """,
                (k, v, now_str),
            )
        conn.commit()
    finally:
        conn.close()
    return {
        "min_reply_delay_sec": mn,
        "max_reply_delay_sec": mx,
        "short_msg_chars_threshold": sc,
        "long_msg_chars_threshold": lc,
    }


DEFAULT_STAGE_THRESHOLDS: Dict[str, int] = {
    "1_Icebreak": 40,
    "2_Explore": 70,
    "3_Lead_Pitch": 85,
}


def _extract_raw_stage_thresholds(
    raw_thresholds: Optional[Dict[str, Any]] = None,
    icebreak: Any = None,
    explore: Any = None,
    lead_pitch: Any = None,
) -> Dict[str, Any]:
    src = raw_thresholds if isinstance(raw_thresholds, dict) else {}
    nested = src.get("stage_thresholds") if isinstance(src.get("stage_thresholds"), dict) else src

    raw_ice = (
        icebreak
        if icebreak is not None
        else nested.get("1_Icebreak", src.get("stage_threshold_icebreak"))
    )
    raw_exp = (
        explore
        if explore is not None
        else nested.get(
            "2_Explore",
            nested.get("2_Discovery", src.get("stage_threshold_explore")),
        )
    )
    raw_pitch = (
        lead_pitch
        if lead_pitch is not None
        else nested.get("3_Lead_Pitch", src.get("stage_threshold_lead_pitch"))
    )
    return {
        "1_Icebreak": raw_ice,
        "2_Explore": raw_exp,
        "3_Lead_Pitch": raw_pitch,
    }


def validate_stage_thresholds_strict(
    raw_data: Optional[Dict[str, Any]] = None,
    icebreak: Any = None,
    explore: Any = None,
    lead_pitch: Any = None,
) -> Tuple[bool, Optional[str], Dict[str, int]]:
    """
    APIリクエスト用バリデーション：
    指定された各ステージ突破閾値が 10〜100 の数値であることを厳密検証する。
    """
    extracted = _extract_raw_stage_thresholds(
        raw_thresholds=raw_data,
        icebreak=icebreak,
        explore=explore,
        lead_pitch=lead_pitch,
    )
    validated: Dict[str, int] = {}
    for stage_key, raw_val in extracted.items():
        if raw_val is None:
            continue
        if isinstance(raw_val, bool) or (isinstance(raw_val, str) and not raw_val.strip()):
            return False, f"{stage_key} の突破閾値が空欄または不正です（10〜100の数値を指定してください）", {}
        try:
            num = int(round(float(raw_val)))
        except Exception:
            return False, f"{stage_key} の突破閾値（{raw_val}）が数値ではありません（10〜100を指定してください）", {}
        if num < 10 or num > 100:
            return False, f"{stage_key} の突破閾値（{num}pt）は 10〜100pt の範囲で指定してください", {}
        validated[stage_key] = num
    return True, None, validated


def normalize_stage_thresholds(
    raw_thresholds: Optional[Dict[str, Any]] = None,
    icebreak: Any = None,
    explore: Any = None,
    lead_pitch: Any = None,
    fallback: Optional[Dict[str, int]] = None,
) -> Dict[str, int]:
    """
    S.E.L.L. 各ステージの突破閾値 (pt) を 10〜100 の整数へ安全に正規化する。
    デフォルト: 1_Icebreak=40, 2_Explore=70, 3_Lead_Pitch=85
    """
    base = dict(DEFAULT_STAGE_THRESHOLDS)
    if isinstance(fallback, dict):
        for k in ("1_Icebreak", "2_Explore", "3_Lead_Pitch"):
            if fallback.get(k) is not None:
                try:
                    base[k] = max(10, min(100, int(round(float(fallback[k])))))
                except Exception:
                    pass

    extracted = _extract_raw_stage_thresholds(
        raw_thresholds=raw_thresholds,
        icebreak=icebreak,
        explore=explore,
        lead_pitch=lead_pitch,
    )
    out: Dict[str, int] = {}
    for stage_key in ("1_Icebreak", "2_Explore", "3_Lead_Pitch"):
        val = extracted.get(stage_key)
        if val is None or isinstance(val, bool) or (isinstance(val, str) and not val.strip()):
            out[stage_key] = base[stage_key]
            continue
        try:
            out[stage_key] = max(10, min(100, int(round(float(val)))))
        except Exception:
            out[stage_key] = base[stage_key]
    return out


def get_system_stage_thresholds(db_path: Optional[str] = None) -> Dict[str, int]:
    """
    system_settings テーブルから S.E.L.L. ステージ突破閾値 (pt) を取得する。
    デフォルト: {"1_Icebreak": 40, "2_Explore": 70, "3_Lead_Pitch": 85}
    """
    conn = get_db_connection(db_path)
    try:
        rows = conn.execute(
            """
            SELECT setting_key, setting_value
            FROM system_settings
            WHERE setting_key IN (
                'stage_threshold_icebreak',
                'stage_threshold_explore',
                'stage_threshold_lead_pitch'
            );
            """
        ).fetchall()
        kv = {str(r["setting_key"]): str(r["setting_value"]) for r in rows}
        return normalize_stage_thresholds(
            icebreak=kv.get("stage_threshold_icebreak"),
            explore=kv.get("stage_threshold_explore"),
            lead_pitch=kv.get("stage_threshold_lead_pitch"),
        )
    except Exception:
        return dict(DEFAULT_STAGE_THRESHOLDS)
    finally:
        conn.close()


def save_system_stage_thresholds(
    thresholds: Optional[Dict[str, Any]] = None,
    icebreak: Any = None,
    explore: Any = None,
    lead_pitch: Any = None,
    db_path: Optional[str] = None,
) -> Dict[str, int]:
    """
    system_settings テーブルへ S.E.L.L. ステージ突破閾値 (pt) を永続化する。
    """
    current = get_system_stage_thresholds(db_path=db_path)
    norm = normalize_stage_thresholds(
        raw_thresholds=thresholds,
        icebreak=icebreak,
        explore=explore,
        lead_pitch=lead_pitch,
        fallback=current,
    )
    now_str = _now_iso()
    conn = get_db_connection(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS system_settings (
                setting_key TEXT PRIMARY KEY,
                setting_value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        pairs = (
            ("stage_threshold_icebreak", str(norm["1_Icebreak"])),
            ("stage_threshold_explore", str(norm["2_Explore"])),
            ("stage_threshold_lead_pitch", str(norm["3_Lead_Pitch"])),
        )
        for k, v in pairs:
            conn.execute(
                """
                INSERT INTO system_settings (setting_key, setting_value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(setting_key) DO UPDATE SET
                    setting_value = excluded.setting_value,
                    updated_at = excluded.updated_at;
                """,
                (k, v, now_str),
            )
        conn.commit()
    finally:
        conn.close()
    return norm
def has_eligible_locked_ppv_for_unlock(
    char_id: str,
    fan_id: str,
    min_elapsed_sec: float = 15.0,
    now_dt: Optional[datetime] = None,
    db_path: Optional[str] = None,
) -> bool:
    """
    DOM開封照合用ガード：
    指定ファンに「未開封 (is_unlocked=0) のPPVトランザクション」が存在し、
    かつ提案送信時刻 (offered_at) から min_elapsed_sec 秒以上経過している場合のみ True を返す。
    """
    ref_now = now_dt or datetime.now()
    conn = get_db_connection(db_path)
    try:
        rows = conn.execute(
            """
            SELECT offered_at
            FROM ppv_transactions
            WHERE char_id = ? AND fan_id = ? AND tx_type = 'ppv' AND is_unlocked = 0
            ORDER BY tx_id DESC;
            """,
            (char_id, fan_id),
        ).fetchall()
        if not rows:
            return False
        for r in rows:
            dt = _parse_iso_datetime(r["offered_at"])
            if dt is None or (ref_now - dt).total_seconds() >= float(min_elapsed_sec):
                return True
        return False
    finally:
        conn.close()


def repair_false_positive_transactions(db_path: Optional[str] = None) -> Dict[str, int]:
    """
    既存DBの誤判定トランザクション整合性点検・自動修復：
    1. 提案直後 (offered_at -> unlocked_at が 5秒以内) にファンからの返信もないまま誤って is_unlocked=1 になったPPVを未購入 (is_unlocked=0) に戻し、fans.total_spent_usd を正確に再計算する。
    2. 対応するAI送信メッセージにチップ要求表現が存在しない未決済の幽霊 tip レコードを削除する。
    """
    repaired_ppv = 0
    removed_phantom_tips = 0
    tip_kw_re = re.compile(r"\b(tip|tips|spoil|treat|coffee|gift)\b", re.IGNORECASE)
    conn = get_db_connection(db_path)
    try:
        cur = conn.cursor()
        unlocked_rows = cur.execute(
            """
            SELECT tx_id, char_id, fan_id, offered_at, unlocked_at
            FROM ppv_transactions
            WHERE tx_type = 'ppv' AND is_unlocked = 1 AND offered_at IS NOT NULL AND unlocked_at IS NOT NULL;
            """
        ).fetchall()

        affected_fans = set()
        for r in unlocked_rows:
            o_dt = _parse_iso_datetime(r["offered_at"])
            u_dt = _parse_iso_datetime(r["unlocked_at"])
            if o_dt and u_dt and 0.0 <= (u_dt - o_dt).total_seconds() <= 5.0:
                fan_between = cur.execute(
                    """
                    SELECT 1 FROM dm_messages
                    WHERE char_id = ? AND fan_id = ? AND sender = 'fan'
                      AND created_at >= ? AND created_at <= ?;
                    """,
                    (r["char_id"], r["fan_id"], r["offered_at"], r["unlocked_at"]),
                ).fetchone()
                if not fan_between:
                    cur.execute(
                        "UPDATE ppv_transactions SET is_unlocked = 0, unlocked_at = NULL WHERE tx_id = ?;",
                        (r["tx_id"],),
                    )
                    affected_fans.add((str(r["char_id"]), str(r["fan_id"])))
                    repaired_ppv += 1

        for cid, fid in affected_fans:
            sum_row = cur.execute(
                """
                SELECT COALESCE(SUM(offered_price), 0.0) AS total_ok
                FROM ppv_transactions
                WHERE char_id = ? AND fan_id = ? AND is_unlocked = 1;
                """,
                (cid, fid),
            ).fetchone()
            new_total = round(float(sum_row["total_ok"] or 0.0), 2) if sum_row else 0.0
            cur.execute(
                "UPDATE fans SET total_spent_usd = ? WHERE char_id = ? AND fan_id = ?;",
                (new_total, cid, fid),
            )

        tip_rows = cur.execute(
            "SELECT tx_id, char_id, fan_id, offered_at FROM ppv_transactions WHERE tx_type = 'tip' AND is_unlocked = 0;"
        ).fetchall()
        for tr in tip_rows:
            t_dt = _parse_iso_datetime(tr["offered_at"])
            if not t_dt:
                continue
            ai_msgs = cur.execute(
                "SELECT content, created_at FROM dm_messages WHERE char_id = ? AND fan_id = ? AND sender = 'ai';",
                (tr["char_id"], tr["fan_id"]),
            ).fetchall()
            has_matching_ai_tip = False
            for am in ai_msgs:
                m_dt = _parse_iso_datetime(am["created_at"])
                if m_dt and abs((m_dt - t_dt).total_seconds()) <= 600.0:
                    if tip_kw_re.search(str(am["content"] or "")):
                        has_matching_ai_tip = True
                        break
            if not has_matching_ai_tip:
                cur.execute("DELETE FROM ppv_transactions WHERE tx_id = ?;", (tr["tx_id"],))
                removed_phantom_tips += 1

        if repaired_ppv > 0 or removed_phantom_tips > 0:
            conn.commit()
    finally:
        conn.close()
    return {"repaired_ppv_count": repaired_ppv, "removed_phantom_tips_count": removed_phantom_tips}


# ==============================================================================
# unmatched_needs ヘルパー (Ticket 1.2)
# 耳役LLMが抽出した「マスタータグに未登録のファン要望」の蓄積・ランキング・昇格処理
# ==============================================================================

def record_unmatched_need(
    char_id: str,
    fan_id: str,
    keyword: str,
    raw_quote: str,
    db_path: Optional[str] = None,
) -> bool:
    """
    未登録のファン要望キーワードを unmatched_needs テーブルへ蓄積する。
    UNIQUE(char_id, fan_id, keyword) により同一ファン・同一キーワードの二重登録は抑制される。
    新規に挿入できた場合は True、既存だった場合は False を返す。
    """
    keyword = str(keyword or "").strip().lower()
    if not keyword:
        return False
    conn = get_db_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS unmatched_needs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                char_id TEXT NOT NULL,
                fan_id TEXT NOT NULL,
                keyword TEXT NOT NULL,
                raw_quote TEXT NOT NULL,
                created_at TEXT NOT NULL,
                promoted_to_tag INTEGER DEFAULT 0,
                UNIQUE(char_id, fan_id, keyword)
            );
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_unmatched_needs_char_kw ON unmatched_needs(char_id, keyword);",
        )
        now_str = _now_iso()
        cur.execute(
            """
            INSERT OR IGNORE INTO unmatched_needs
                (char_id, fan_id, keyword, raw_quote, created_at, promoted_to_tag)
            VALUES (?, ?, ?, ?, ?, 0);
            """,
            (str(char_id), str(fan_id), keyword, str(raw_quote or ""), now_str),
        )
        conn.commit()
        return cur.rowcount > 0
    except Exception:
        return False
    finally:
        conn.close()


def get_unmatched_needs_ranking(
    char_id: str,
    limit: int = 20,
    db_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    指定キャラクターの未登録要望を「要望ファン数」降順でランキングする。
    各要素: {keyword, count, latest_quote, fans: [fan_id, ...]}
    """
    conn = get_db_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS unmatched_needs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                char_id TEXT NOT NULL,
                fan_id TEXT NOT NULL,
                keyword TEXT NOT NULL,
                raw_quote TEXT NOT NULL,
                created_at TEXT NOT NULL,
                promoted_to_tag INTEGER DEFAULT 0,
                UNIQUE(char_id, fan_id, keyword)
            );
            """
        )
        rows = cur.execute(
            """
            SELECT keyword, fan_id, raw_quote, created_at
            FROM unmatched_needs
            WHERE char_id = ? AND promoted_to_tag = 0
            ORDER BY created_at DESC;
            """,
            (str(char_id),),
        ).fetchall()
        ranking: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            kw = str(r["keyword"] or "").strip().lower()
            if not kw:
                continue
            entry = ranking.setdefault(
                kw,
                {
                    "keyword": kw,
                    "count": 0,
                    "latest_quote": str(r["raw_quote"] or ""),
                    "fans": [],
                },
            )
            fid = str(r["fan_id"] or "")
            if fid and fid not in entry["fans"]:
                entry["fans"].append(fid)
        for entry in ranking.values():
            entry["count"] = len(entry["fans"])
        result = sorted(ranking.values(), key=lambda e: (e["count"], e["keyword"]), reverse=True)
        return result[: max(1, int(limit))]
    except Exception:
        return []
    finally:
        conn.close()


def mark_unmatched_need_promoted(keyword: str, db_path: Optional[str] = None) -> int:
    """
    指定キーワードの未登録要望を「マスタータグへ昇格済み」(promoted_to_tag=1) とマークする。
    対象行の更新数を返す。
    """
    keyword = str(keyword or "").strip().lower()
    if not keyword:
        return 0
    conn = get_db_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS unmatched_needs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                char_id TEXT NOT NULL,
                fan_id TEXT NOT NULL,
                keyword TEXT NOT NULL,
                raw_quote TEXT NOT NULL,
                created_at TEXT NOT NULL,
                promoted_to_tag INTEGER DEFAULT 0,
                UNIQUE(char_id, fan_id, keyword)
            );
            """
        )
        cur.execute(
            "UPDATE unmatched_needs SET promoted_to_tag = 1 WHERE keyword = ? AND promoted_to_tag = 0;",
            (keyword,),
        )
        conn.commit()
        return cur.rowcount
    except Exception:
        return 0
    finally:
        conn.close()










