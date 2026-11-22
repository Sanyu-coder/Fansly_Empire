"""
Ticket 4.2: マスタータグローダー ＆ 未購入7日間除外PPV機械選定モジュール。

設計思想（決定論的制御）：
- タグ分類は LLM（耳役）に任せるが、PPV 選定・再提案除外ロジックは 100% Python で
  機械的に制御する。
- 再提案除外ルール：
    * 購入済み (is_unlocked == 1) の素材は永久除外。
    * 未購入 (is_unlocked == 0) かつ提案履歴がある素材は、直近7日間以内
      (offered_at >= now - 7days) のもののみ除外。7日を超えた未購入PPVは
      再提案候補に復活する（在庫枯渇防止）。
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from app.core import config
from app.core.database import (
    _parse_iso_datetime,
    get_fan_ppv_transactions,
    get_ppv_vault_records_db,
)

_MASTER_TAGS_CACHE: Dict[str, Any] = {"mtime": 0.0, "data": None}


def master_tags_path() -> str:
    return os.path.join(config.SHARED_ASSETS_DIR, "master_tags.json")


def load_master_tags(refresh: bool = False) -> Dict[str, Any]:
    """shared_assets/master_tags.json を mtime キャッシュ付きで読み込む。"""
    path = master_tags_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0.0

    if (
        not refresh
        and _MASTER_TAGS_CACHE["data"] is not None
        and _MASTER_TAGS_CACHE["mtime"] == mtime
    ):
        return _MASTER_TAGS_CACHE["data"]

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {"version": 0, "categories": {}}
    _MASTER_TAGS_CACHE["data"] = data
    _MASTER_TAGS_CACHE["mtime"] = mtime
    return data


def get_all_master_tag_ids() -> List[str]:
    """全カテゴリのタグ ID を平たく返す（耳役LLMの候補リスト用）。"""
    data = load_master_tags()
    ids: List[str] = []
    for cat in (data.get("categories") or {}).values():
        for tag in (cat.get("tags") or []):
            tid = str(tag.get("id") or "").strip()
            if tid:
                ids.append(tid)
    return ids


def get_active_ppv_tags(char_id: str, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    耳役LLMに提示する候補タグリスト。
    現在この char_id のアクティブな PPV に紐づいているタグのみを返す
    （存在しないタグに分類しても在庫が当たらないため）。
    """
    vault = get_ppv_vault_records_db(char_id, db_path=db_path)
    present_ids: set = set()
    for rec in vault.values():
        if not int(rec.get("is_active") or 1):
            continue
        for src in ("tags_json", "category_tags_json"):
            raw = rec.get(src)
            if not raw:
                continue
            try:
                parsed = json.loads(str(raw))
            except Exception:
                continue
            if isinstance(parsed, list):
                for t in parsed:
                    t = str(t).strip().lstrip("#")
                    if t:
                        present_ids.add(t)
            elif isinstance(parsed, dict):
                for v in parsed.values():
                    if isinstance(v, str) and v.strip():
                        present_ids.add(v.strip())

    data = load_master_tags()
    out: List[Dict[str, Any]] = []
    for cat in (data.get("categories") or {}).values():
        for tag in (cat.get("tags") or []):
            tid = str(tag.get("id") or "").strip()
            if tid and tid in present_ids:
                out.append(
                    {
                        "id": tid,
                        "category": tag.get("category"),
                        "name_ja": tag.get("name_ja"),
                        "llm_hint": tag.get("llm_hint"),
                    }
                )
    return out


def _parse_category_tags(rec: Dict[str, Any]) -> List[str]:
    """PPV 素材の全タグ（category_tags_json 優先、tags_json フォールバック）を平たく返す。"""
    tags: List[str] = []
    for src in ("category_tags_json", "tags_json"):
        raw = rec.get(src)
        if not raw:
            continue
        try:
            parsed = json.loads(str(raw))
        except Exception:
            continue
        if isinstance(parsed, list):
            tags.extend(str(t).strip().lstrip("#") for t in parsed if str(t).strip())
        elif isinstance(parsed, dict):
            for v in parsed.values():
                if isinstance(v, str) and v.strip():
                    tags.append(v.strip())
        # どちらか一方にタグがあれば十分
        if tags:
            break
    return [t for t in tags if t]


def match_ppv_deterministic(
    char_id: str,
    fan_id: str,
    matched_tags: List[str],
    ladder_step: int,
    is_rescue: bool = False,
    db_path: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    決定論的 PPV 選定。

    除外ルール：
      - 購入済み (is_unlocked == 1) は永久除外。
      - 未購入 (is_unlocked == 0) で直近7日以内に提案済み (offered_at >= now-7d) のみ除外。
    救済ルート (is_rescue=True):
      - 上記除外後の is_general == 1 素材から対象 ladder_step (または直近ステップ) を1件。
    通常ルート:
      - category_tags_json/tags_json と matched_tags の一致タグ数が最大で、
        対象 ladder_step のPPVを1件確定。一致在庫がなければ None。
    """
    matched = [str(t).strip().lstrip("#") for t in (matched_tags or []) if str(t).strip()]

    vault = get_ppv_vault_records_db(char_id, db_path=db_path)
    if not vault:
        return None

    now = datetime.now()
    cutoff = now - timedelta(days=7)

    txs = get_fan_ppv_transactions(char_id, fan_id, db_path=db_path)
    # ppv_id -> 最新オファー日時・購入状態
    tx_map: Dict[str, Dict[str, Any]] = {}
    for tx in txs:
        pid = str(tx.get("ppv_id") or "")
        if not pid:
            continue
        prev = tx_map.get(pid)
        if prev is None:
            tx_map[pid] = tx
        else:
            if str(tx.get("offered_at") or "") >= str(prev.get("offered_at") or ""):
                tx_map[pid] = tx

    def is_excluded(rec: Dict[str, Any]) -> bool:
        pid = str(rec.get("ppv_id") or "")
        tx = tx_map.get(pid)
        if not tx:
            return False
        if int(tx.get("is_unlocked") or 0) == 1:
            return True  # 購入済みは永久除外
        offered_dt = _parse_iso_datetime(str(tx.get("offered_at") or ""))
        if offered_dt is not None and offered_dt >= cutoff:
            return True  # 未購入・直近7日以内の提案は除外
        return False  # 7日を超えた未購入は再提案可能

    candidates = [
        rec
        for rec in vault.values()
        if int(rec.get("is_active") or 1) == 1 and not is_excluded(rec)
    ]
    if not candidates:
        return None

    if is_rescue:
        # 救済: is_general == 1 優先。対象 step がなければ直近 step。
        general = [r for r in candidates if int(r.get("is_general") or 0) == 1]
        pool = general if general else candidates
        step_pool = [r for r in pool if int(r.get("ladder_step") or 1) == int(ladder_step)]
        if not step_pool:
            # 直近ステップ（最も近い）へフォールバック
            steps = sorted({int(r.get("ladder_step") or 1) for r in pool})
            nearest = min(steps, key=lambda s: abs(s - int(ladder_step)))
            step_pool = [r for r in pool if int(r.get("ladder_step") or 1) == nearest]
        if not step_pool:
            return None
        chosen = min(step_pool, key=lambda r: (int(r.get("ladder_step") or 1), str(r.get("ppv_id") or "")))
        return dict(chosen)

    # 通常ルート: matched_tags と PPV タグの一致数をスコア化
    scored: List[Dict[str, Any]] = []
    for rec in candidates:
        rec_tags = set(_parse_category_tags(rec))
        overlap = len(rec_tags & set(matched))
        if overlap == 0:
            continue
        scored.append({"rec": rec, "score": overlap})

    if not scored:
        return None

    # スコア降順、同点なら ladder_step が近いもの、最後に ppv_id で決定論的ソート
    scored.sort(key=lambda x: (-x["score"], abs(int(x["rec"].get("ladder_step") or 1) - int(ladder_step)), str(x["rec"].get("ppv_id") or "")))
    return dict(scored[0]["rec"])
