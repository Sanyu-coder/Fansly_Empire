import os
import sys
import json
import uuid
import threading
import subprocess
from collections import deque
from datetime import datetime
from app.config import (
    BASE_DIR,
    SHARED_ASSETS_DIR,
    LINE1_SCRIPT,
    LINE2_SCRIPT,
    VIDEO_SCRIPT,
    LINE1_MAX_REPEAT,
    ALL_CHARS_SENTINEL,
    get_line2_headless,
    parse_bool_flag,
)
from app.services.character_service import get_char_paths, list_all_characters

# GPU占有タスク（Line 1 / 動画バッチ）の直列FIFO実行状態
worker_state = {
    "running": False,
    "task_id": None,
    "char_id": None,
    "all_chars": False,
    "line": None,
    "current_iter": 0,
    "total_iter": 0,
    "stop_requested": False,
    "process": None,
    "logs": deque(maxlen=250),
    "total_items": 0,
    "completed_items": 0,
    "item_progress": 0,
    "current_stage": "",
}
gpu_queue = deque()  # list of dict: {"id", "line", "char_id", "all_chars", "repeat", "queued_at", "label"}
gpu_consumer_running = False

# GPU非使用タスク（第2ライン: Fansly予約投稿）の独立並列実行状態
line2_state = {
    "running": False,
    "char_id": None,
    "all_chars": False,
    "headless": False,
    "line": "line2",
    "current_iter": 0,
    "total_iter": 1,
    "stop_requested": False,
    "process": None,
    "logs": deque(maxlen=250),
    "total_items": 0,
    "completed_items": 0,
    "item_progress": 0,
    "current_stage": "",
}

worker_lock = threading.Lock()


def append_log(msg, target="gpu"):
    ts = datetime.now().strftime("%H:%M:%S")
    formatted = f"[{ts}] {msg}"
    with worker_lock:
        worker_state["logs"].append(formatted)
        if target == "line2":
            line2_state["logs"].append(formatted)


def _terminate_process(proc):
    if proc and proc.poll() is None:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass
        try:
            proc.terminate()
        except Exception:
            pass


def _resolve_target_char_ids(char_id: str, all_chars: bool = False) -> list:
    is_all = bool(all_chars) or str(char_id or "").strip().upper() in (ALL_CHARS_SENTINEL, "ALL_CHARS", "__ALL__")
    if is_all:
        chars = list_all_characters()
        ids = [c.get("id") for c in chars if c.get("id")]
        return ids or ["queen_lilith"]
    return [char_id or "queen_lilith"]


def _consume_progress_signal(clean_line: str, stage_suffix: str = "", state_dict=None, item_offset: int = 0, fixed_total: int = None) -> bool:
    """
    サブプロセスが出力した `@@PROGRESS@@ {...}` シグナル行を検知して対象 state を即時更新する。
    シグナル行であれば True を返し、生ログへの追加をスキップさせる。
    """
    prefix = "@@PROGRESS@@ "
    if prefix not in clean_line:
        return False
    target = state_dict if state_dict is not None else worker_state
    try:
        json_part = clean_line.split(prefix, 1)[1].strip()
        pdata = json.loads(json_part)
        with worker_lock:
            if fixed_total is not None:
                target["total_items"] = int(fixed_total)
            elif "total" in pdata and pdata["total"] is not None:
                target["total_items"] = int(pdata["total"])
            if "completed" in pdata and pdata["completed"] is not None:
                target["completed_items"] = item_offset + int(pdata["completed"])
            if "pct" in pdata and pdata["pct"] is not None:
                target["item_progress"] = max(0, min(100, int(pdata["pct"])))
            if "stage" in pdata and pdata["stage"]:
                target["current_stage"] = f"{pdata['stage']}{stage_suffix}"
        return True
    except Exception:
        return True


def _auto_finalize_completed_videos(char_id: str):
    """
    動画生成バッチ完了後、Step1採用済みかつ動画完成済みの候補フォルダを
    6候補ランダムキャプション＋前から詰め自動スケジュールでタイムラインへ自動確定する
    """
    try:
        from app.routes.queue_routes import auto_finalize_folder_to_ready
    except Exception as e:
        append_log(f"⚠️ 自動予約モジュールの読み込みをスキップしました: {e}")
        return

    paths = get_char_paths(char_id)
    cand_dir = paths.get("candidates")
    if not cand_dir or not os.path.isdir(cand_dir):
        return

    for folder in sorted(os.listdir(cand_dir)):
        f_path = os.path.join(cand_dir, folder)
        meta_path = os.path.join(f_path, "post_data.json")
        if not os.path.isdir(f_path) or not os.path.exists(meta_path):
            continue

        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            continue

        # Step1で画像採用済みのものだけ対象
        if not (meta.get("step1_approved") or meta.get("step1_saved")):
            continue

        video_task = meta.get("video_task") or {}
        gen_vid = video_task.get("output_video") or meta.get("generated_video")
        video_done = (
            video_task.get("status") == "done"
            or bool(gen_vid and os.path.exists(os.path.join(f_path, gen_vid)))
        )

        if video_done:
            res = auto_finalize_folder_to_ready(char_id, folder)
            if res.get("ok"):
                append_log(f"🚀 [自動予約完了] No.{folder}（動画完成）→ {res['scheduled_at']} にタイムラインへ直送しました！")


def _count_video_tasks_for_char(char_id: str) -> int:
    paths = get_char_paths(char_id)
    candidates_dir = paths.get("candidates")
    ready_dir = paths.get("ready")

    total_items = 0
    for root_dir in (candidates_dir, ready_dir):
        if not root_dir or not os.path.isdir(root_dir):
            continue
        for folder in sorted(os.listdir(root_dir)):
            if folder.lower() in ("posted", "done", "completed", "pending", "scheduled"):
                continue
            fdir = os.path.join(root_dir, folder)
            pdata_path = os.path.join(fdir, "post_data.json")
            if not os.path.isdir(fdir) or not os.path.exists(pdata_path):
                continue
            try:
                with open(pdata_path, "r", encoding="utf-8") as f:
                    pdata = json.load(f)
                vtask = pdata.get("video_task") or {}
                existing_vid = vtask.get("output_video") or vtask.get("video_file") or pdata.get("generated_video")
                if vtask.get("status") == "done" and existing_vid and os.path.exists(os.path.join(fdir, existing_vid)):
                    continue
                src_img = vtask.get("source_image") or pdata.get("video_image")
                is_queued = (vtask.get("status") == "queued") or (bool(src_img) and not existing_vid)
                if is_queued and src_img:
                    total_items += 1
            except Exception:
                continue
    return total_items


def _count_line2_tasks_for_char(char_id: str) -> int:
    paths = get_char_paths(char_id)
    ready_dir = paths.get("ready")
    total_items = 0
    if ready_dir and os.path.isdir(ready_dir):
        for root, dirs, files in os.walk(ready_dir):
            for folder in dirs:
                f_path = os.path.join(root, folder)
                final_post_json = os.path.join(f_path, "final_post.json")
                if os.path.exists(final_post_json):
                    try:
                        with open(final_post_json, "r", encoding="utf-8") as f:
                            meta = json.load(f)
                        if not meta.get("posted") and meta.get("status") != "posted" and not meta.get("video_lock"):
                            total_items += 1
                    except Exception:
                        continue
    return total_items


def _execute_line1_task(char_id, repeat_count, all_chars=False):
    if not os.path.exists(LINE1_SCRIPT):
        append_log(f"❌ エラー: 第1ラインのスクリプトが見つかりません -> {LINE1_SCRIPT}")
        with worker_lock:
            worker_state["current_stage"] = "エラー: スクリプト不在"
        return

    target_chars = _resolve_target_char_ids(char_id, all_chars=all_chars)
    total_sets = len(target_chars) * repeat_count
    scope_label = f"全キャラ ({len(target_chars)}名)" if (all_chars or len(target_chars) > 1) else f"キャラ: {target_chars[0]}"
    append_log(f"🏭 第1ライン稼働開始 [{scope_label}]（各 {repeat_count} セット / 合計目標: {total_sets} セット）")

    with worker_lock:
        worker_state["total_iter"] = total_sets
        worker_state["total_items"] = total_sets
        worker_state["completed_items"] = 0
        worker_state["item_progress"] = 5
        worker_state["current_stage"] = f"第1ライン準備中 (0/{total_sets} セット)"

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env["SHARED_ASSETS_DIR"] = SHARED_ASSETS_DIR

    global_idx = 0
    for c_idx, cid in enumerate(target_chars, start=1):
        if worker_state["stop_requested"]:
            break
        with worker_lock:
            worker_state["char_id"] = cid
        if len(target_chars) > 1:
            append_log(f"👑 キャラクター切替 [{c_idx}/{len(target_chars)}]: {cid} の生産を開始します（{repeat_count} セット）")

        for i in range(1, repeat_count + 1):
            if worker_state["stop_requested"]:
                append_log("🛑 ユーザー操作により第1ラインを中断しました。")
                with worker_lock:
                    worker_state["current_stage"] = "ユーザー操作により中断"
                break

            global_idx += 1
            with worker_lock:
                worker_state["current_iter"] = global_idx
                worker_state["completed_items"] = global_idx - 1
                worker_state["item_progress"] = 2
                if len(target_chars) > 1:
                    worker_state["current_stage"] = f"[{cid}] シチュエーション生成準備中 ({global_idx}/{total_sets} セット)"
                else:
                    worker_state["current_stage"] = f"シチュエーション生成準備中 ({global_idx}/{total_sets} セット)"

            append_log(f"▶ 【第1ライン / {cid}】 {i} / {repeat_count} セット目（通算 {global_idx}/{total_sets}）の生成を実行中...")
            proc = subprocess.Popen(
                [sys.executable, "-u", LINE1_SCRIPT, "--char", cid],
                cwd=BASE_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1, env=env
            )
            with worker_lock:
                worker_state["process"] = proc

            if len(target_chars) > 1:
                suffix = f" [{cid} {i}/{repeat_count} | 通算 {global_idx}/{total_sets}]"
            else:
                suffix = f" ({global_idx}/{total_sets}セット)" if total_sets > 1 else ""

            for line in iter(proc.stdout.readline, ""):
                clean = line.strip()
                if not clean:
                    continue
                if _consume_progress_signal(clean, stage_suffix=suffix, state_dict=worker_state, item_offset=global_idx - 1, fixed_total=total_sets):
                    continue
                append_log(clean)

            proc.wait()
            with worker_lock:
                worker_state["process"] = None

            if worker_state["stop_requested"]:
                append_log("🛑 ユーザー操作により第1ラインを中断しました。")
                with worker_lock:
                    worker_state["current_stage"] = "ユーザー操作により中断"
                break

            with worker_lock:
                worker_state["completed_items"] = global_idx
                worker_state["item_progress"] = 100
                worker_state["current_stage"] = f"{global_idx}/{total_sets} セット生産完了！"

            append_log(f"✅ [{cid}] {i} / {repeat_count} セット目（通算 {global_idx}/{total_sets}）の生産完了！")

    if not worker_state["stop_requested"]:
        append_log(f"🎉 第1ラインの全工程（合計 {total_sets} セット）が完了しました！")
        with worker_lock:
            worker_state["completed_items"] = total_sets
            worker_state["item_progress"] = 100
            worker_state["current_stage"] = f"第1ライン全工程完了 ({total_sets}/{total_sets} セット)"


def _execute_video_task(char_id, all_chars=False):
    if not os.path.exists(VIDEO_SCRIPT):
        append_log(f"❌ エラー: 動画ワーカーが見つかりません -> {VIDEO_SCRIPT}")
        with worker_lock:
            worker_state["current_stage"] = "エラー: 動画スクリプト不在"
        return

    target_chars = _resolve_target_char_ids(char_id, all_chars=all_chars)
    char_counts = [(cid, _count_video_tasks_for_char(cid)) for cid in target_chars]
    total_items = sum(cnt for _, cnt in char_counts)

    scope_label = f"全キャラ ({len(target_chars)}名)" if (all_chars or len(target_chars) > 1) else f"キャラ: {target_chars[0]}"
    append_log(f"🎬 動画生成バッチ稼働開始 [{scope_label}]...（対象数: {total_items} 件）")

    with worker_lock:
        worker_state["total_items"] = total_items
        worker_state["completed_items"] = 0
        worker_state["item_progress"] = 5
        worker_state["current_stage"] = f"動画生成バッチ準備中 (対象: {total_items}件)..."

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env["SHARED_ASSETS_DIR"] = SHARED_ASSETS_DIR

    offset = 0
    for cid, cnt in char_counts:
        if worker_state["stop_requested"]:
            break
        with worker_lock:
            worker_state["char_id"] = cid
        if len(target_chars) > 1 and cnt == 0:
            append_log(f"ℹ️ [{cid}] 未生成の動画タスクはありません。スキップします。")
            continue

        proc = subprocess.Popen(
            [sys.executable, "-u", VIDEO_SCRIPT, "--char", cid],
            cwd=BASE_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1, env=env
        )
        with worker_lock:
            worker_state["process"] = proc

        suffix = f" [{cid}]" if len(target_chars) > 1 else ""
        for line in iter(proc.stdout.readline, ""):
            clean = line.strip()
            if not clean:
                continue
            if _consume_progress_signal(
                clean,
                stage_suffix=suffix,
                state_dict=worker_state,
                item_offset=offset,
                fixed_total=total_items if len(target_chars) > 1 else None,
            ):
                continue
            append_log(clean)

        proc.wait()
        with worker_lock:
            worker_state["process"] = None

        if worker_state["stop_requested"]:
            append_log("🛑 ユーザー操作により動画生成バッチを中断しました。")
            with worker_lock:
                worker_state["current_stage"] = "ユーザー操作により中断"
            break

        with worker_lock:
            worker_state["current_stage"] = f"[{cid}] 動画完成フォルダの自動予約処理中..." if len(target_chars) > 1 else "動画完成フォルダの自動予約処理中..."
        _auto_finalize_completed_videos(cid)
        offset += cnt

    if not worker_state["stop_requested"]:
        append_log("🏁 動画生成バッチの全工程が完了しました！Step 2 で動画確認・モザイク適用後にタイムラインへ確定してください。")
        with worker_lock:
            worker_state["completed_items"] = worker_state["total_items"]
            worker_state["item_progress"] = 100
            worker_state["current_stage"] = "動画生成バッチ全工程完了！"


def _gpu_queue_consumer_loop():
    global gpu_consumer_running
    while True:
        with worker_lock:
            if not gpu_queue:
                gpu_consumer_running = False
                worker_state["running"] = False
                worker_state["task_id"] = None
                worker_state["process"] = None
                worker_state["stop_requested"] = False
                worker_state["item_progress"] = 0
                return
            task = gpu_queue.popleft()
            worker_state["running"] = True
            worker_state["task_id"] = task["id"]
            worker_state["char_id"] = task["char_id"]
            worker_state["all_chars"] = bool(task.get("all_chars"))
            worker_state["line"] = task["line"]
            worker_state["current_iter"] = 0
            worker_state["total_iter"] = task.get("repeat", 1)
            worker_state["total_items"] = 0
            worker_state["completed_items"] = 0
            worker_state["item_progress"] = 0
            worker_state["stop_requested"] = False
            worker_state["current_stage"] = f"{task['label']} 起動準備中..."

        try:
            if task["line"] == "line1":
                _execute_line1_task(task["char_id"], task.get("repeat", 1), all_chars=bool(task.get("all_chars")))
            elif task["line"] == "video":
                _execute_video_task(task["char_id"], all_chars=bool(task.get("all_chars")))
        except Exception as e:
            append_log(f"❌ GPUワーカー実行エラー ({task['line']}): {e}")
            with worker_lock:
                worker_state["current_stage"] = f"エラー: {str(e)[:30]}"
        finally:
            with worker_lock:
                worker_state["process"] = None
                worker_state["stop_requested"] = False
                worker_state["item_progress"] = 0
                if not gpu_queue:
                    worker_state["running"] = False
                    worker_state["task_id"] = None


def enqueue_gpu_task(line: str, char_id: str, repeat: int = 1, all_chars: bool = False) -> dict:
    """
    GPU占有タスク（line1 / video）を直列FIFOキューへ追加する。
    現在GPUワーカーが未稼働なら即座にコンシューマスレッドを起動し、稼働中なら待機キューに積む。
    """
    global gpu_consumer_running
    is_all = bool(all_chars) or str(char_id or "").strip().upper() in (ALL_CHARS_SENTINEL, "ALL_CHARS", "__ALL__")
    norm_char = ALL_CHARS_SENTINEL if is_all else (char_id or "queen_lilith")
    repeat_clamped = max(1, min(LINE1_MAX_REPEAT, int(repeat or 1))) if line == "line1" else 1

    scope_txt = "全キャラ" if is_all else norm_char
    if line == "line1":
        label = f"🏭 第1ライン ({scope_txt} / {repeat_clamped}回)"
    else:
        label = f"🎬 動画生成バッチ ({scope_txt})"

    task = {
        "id": uuid.uuid4().hex[:8],
        "line": line,
        "char_id": norm_char,
        "all_chars": is_all,
        "repeat": repeat_clamped,
        "queued_at": datetime.now().strftime("%H:%M:%S"),
        "label": label,
    }

    with worker_lock:
        already_busy = gpu_consumer_running or worker_state["running"]
        gpu_queue.append(task)
        position = len(gpu_queue)
        if not already_busy:
            gpu_consumer_running = True
            worker_state["running"] = True
            worker_state["line"] = line
            worker_state["char_id"] = norm_char
            worker_state["all_chars"] = is_all
            worker_state["current_stage"] = f"{label} 起動準備中..."
            start_thread = True
        else:
            start_thread = False

    if start_thread:
        threading.Thread(target=_gpu_queue_consumer_loop, daemon=True).start()
    else:
        append_log(f"📥 キュー予約追加: {label}（待機順: #{position}）")

    return {
        "ok": True,
        "queued": not start_thread,
        "queue_position": 0 if start_thread else position,
        "task": dict(task),
    }


def cancel_queued_gpu_task(task_id: str) -> dict:
    """待機中のGPUキューから指定 task_id のタスクを取り消す（実行中タスクは対象外）"""
    removed = None
    with worker_lock:
        if not task_id:
            return {"ok": False, "error": "task_id が指定されていません"}
        new_q = deque()
        for item in gpu_queue:
            if removed is None and item.get("id") == task_id:
                removed = item
            else:
                new_q.append(item)
        if removed is not None:
            gpu_queue.clear()
            gpu_queue.extend(new_q)

    if removed is None:
        return {"ok": False, "error": "指定された待機タスクが見つかりません（すでに開始または完了しています）"}

    append_log(f"🗑️ 待機キューをキャンセルしました: {removed['label']}")
    return {"ok": True, "cancelled": removed}


def clear_queued_gpu_tasks() -> int:
    with worker_lock:
        cnt = len(gpu_queue)
        gpu_queue.clear()
    if cnt > 0:
        append_log(f"🗑️ 待機中のGPUキュー（{cnt}件）をすべてクリアしました。")
    return cnt


def get_gpu_queue_snapshot() -> list:
    with worker_lock:
        return [dict(item, position=idx + 1) for idx, item in enumerate(gpu_queue)]


def run_line1_worker(char_id, repeat_count, all_chars=False):
    """後方互換エントリポイント（直接呼び出し時も安全に動作）"""
    try:
        with worker_lock:
            worker_state["running"] = True
            worker_state["line"] = "line1"
            worker_state["char_id"] = char_id
            worker_state["all_chars"] = bool(all_chars)
            worker_state["stop_requested"] = False
        _execute_line1_task(char_id, repeat_count, all_chars=all_chars)
    finally:
        with worker_lock:
            worker_state["running"] = False
            worker_state["process"] = None
            worker_state["stop_requested"] = False
            worker_state["item_progress"] = 0


def run_video_worker(char_id, all_chars=False):
    """後方互換エントリポイント（直接呼び出し時も安全に動作）"""
    try:
        with worker_lock:
            worker_state["running"] = True
            worker_state["line"] = "video"
            worker_state["char_id"] = char_id
            worker_state["all_chars"] = bool(all_chars)
            worker_state["stop_requested"] = False
        _execute_video_task(char_id, all_chars=all_chars)
    finally:
        with worker_lock:
            worker_state["running"] = False
            worker_state["process"] = None
            worker_state["stop_requested"] = False
            worker_state["item_progress"] = 0


def start_line2_parallel(char_id: str, all_chars: bool = False, headless=None) -> dict:
    """GPUキューとは独立したワーカースレッドで第2ライン（Fansly予約投稿）を並列起動する"""
    is_all = bool(all_chars) or str(char_id or "").strip().upper() in (ALL_CHARS_SENTINEL, "ALL_CHARS", "__ALL__")
    norm_char = ALL_CHARS_SENTINEL if is_all else (char_id or "queen_lilith")
    resolved_headless = get_line2_headless() if headless is None else parse_bool_flag(headless, get_line2_headless())

    with worker_lock:
        if line2_state["running"]:
            return {"ok": False, "error": "第2ラインはすでに稼働中です！"}
        line2_state["running"] = True
        line2_state["char_id"] = norm_char
        line2_state["all_chars"] = is_all
        line2_state["headless"] = resolved_headless
        line2_state["line"] = "line2"
        line2_state["current_iter"] = 0
        line2_state["total_iter"] = 1
        line2_state["total_items"] = 0
        line2_state["completed_items"] = 0
        line2_state["item_progress"] = 0
        line2_state["current_stage"] = "第2ライン起動準備中..."
        line2_state["stop_requested"] = False
        line2_state["logs"].clear()

    threading.Thread(
        target=run_line2_worker,
        args=(norm_char, is_all, resolved_headless),
        daemon=True,
    ).start()
    return {
        "ok": True,
        "char_id": norm_char,
        "all_chars": is_all,
        "headless": resolved_headless,
        "line2_headless": resolved_headless,
    }


def run_line2_worker(char_id, all_chars=False, headless=None):
    try:
        if not os.path.exists(LINE2_SCRIPT):
            append_log(f"❌ エラー: 第2ラインのスクリプトが見つかりません -> {LINE2_SCRIPT}", target="line2")
            with worker_lock:
                line2_state["current_stage"] = "エラー: 第2ラインスクリプト不在"
            return

        use_headless = get_line2_headless() if headless is None else parse_bool_flag(headless, get_line2_headless())
        with worker_lock:
            line2_state["headless"] = use_headless

        target_chars = _resolve_target_char_ids(char_id, all_chars=all_chars)
        char_counts = [(cid, _count_line2_tasks_for_char(cid)) for cid in target_chars]
        total_items = sum(cnt for _, cnt in char_counts)

        scope_label = f"全キャラ ({len(target_chars)}名)" if (all_chars or len(target_chars) > 1) else f"キャラ: {target_chars[0]}"
        mode_label = "ヘッドレス" if use_headless else "画面表示"
        append_log(
            f"🚀 第2ライン稼働開始 [{scope_label} / {mode_label}]！予約キューが空になるまで全件処理します...（対象数: {total_items} 件）",
            target="line2",
        )

        with worker_lock:
            line2_state["total_items"] = total_items
            line2_state["completed_items"] = 0
            line2_state["item_progress"] = 5
            line2_state["current_stage"] = f"第2ライン準備中 [{mode_label}] (対象: {total_items}件)..."

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        env["SHARED_ASSETS_DIR"] = SHARED_ASSETS_DIR
        env["LINE2_HEADLESS"] = "1" if use_headless else "0"

        headless_flag = "--headless" if use_headless else "--headed"

        offset = 0
        for cid, cnt in char_counts:
            if line2_state["stop_requested"]:
                break
            with worker_lock:
                line2_state["char_id"] = cid
            if len(target_chars) > 1 and cnt == 0:
                append_log(f"ℹ️ [第2ライン / {cid}] 予約待ちアイテムはありません。スキップします。", target="line2")
                continue

            proc = subprocess.Popen(
                [sys.executable, "-u", LINE2_SCRIPT, "--char", cid, headless_flag],
                cwd=BASE_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1, env=env
            )
            with worker_lock:
                line2_state["process"] = proc

            suffix = f" [{cid}]" if len(target_chars) > 1 else ""
            for line in iter(proc.stdout.readline, ""):
                clean = line.strip()
                if not clean:
                    continue
                if _consume_progress_signal(
                    clean,
                    stage_suffix=suffix,
                    state_dict=line2_state,
                    item_offset=offset,
                    fixed_total=total_items if len(target_chars) > 1 else None,
                ):
                    continue
                append_log(clean, target="line2")

            proc.wait()
            with worker_lock:
                line2_state["process"] = None

            if line2_state["stop_requested"]:
                append_log("🛑 ユーザー操作により第2ラインを中断しました。", target="line2")
                with worker_lock:
                    line2_state["current_stage"] = "ユーザー操作により中断"
                break

            offset += cnt

        if not line2_state["stop_requested"]:
            append_log("🏁 第2ラインの全予約処理が完了しました！", target="line2")
            with worker_lock:
                line2_state["completed_items"] = line2_state["total_items"]
                line2_state["item_progress"] = 100
                line2_state["current_stage"] = "第2ライン全予約投稿完了！"
    except Exception as e:
        append_log(f"❌ 第2ライン実行エラー: {e}", target="line2")
        with worker_lock:
            line2_state["current_stage"] = f"エラー: {str(e)[:30]}"
    finally:
        with worker_lock:
            line2_state["running"] = False
            line2_state["process"] = None
            line2_state["stop_requested"] = False
            line2_state["item_progress"] = 0


def stop_workers(target: str = "all", clear_queue: bool = False) -> dict:
    """
    実行中ワーカーの緊急停止。
    target: 'gpu' (Line1/動画のみ) | 'line2' (第2ラインのみ) | 'all' (稼働中すべて)
    clear_queue: True の場合、待機中のGPUキューもクリアする
    """
    stopped_any = False
    gpu_proc = None
    l2_proc = None

    with worker_lock:
        if target in ("all", "gpu") and worker_state["running"]:
            stopped_any = True
            worker_state["stop_requested"] = True
            worker_state["item_progress"] = 0
            worker_state["current_stage"] = "🛑 緊急停止しました"
            gpu_proc = worker_state.get("process")
            if clear_queue:
                gpu_queue.clear()

        if target in ("all", "line2") and line2_state["running"]:
            stopped_any = True
            line2_state["stop_requested"] = True
            line2_state["item_progress"] = 0
            line2_state["current_stage"] = "🛑 緊急停止しました"
            l2_proc = line2_state.get("process")

        if not stopped_any and clear_queue and gpu_queue:
            gpu_queue.clear()
            stopped_any = True

    if not stopped_any:
        return {"ok": False, "error": "現在稼働中のラインはありません"}

    append_log(f"⚠️ 緊急停止リクエストを受信しました (対象: {target})...")
    if gpu_proc:
        _terminate_process(gpu_proc)
        append_log("🛑 GPUワーカーの実行中プロセスを強制終了しました。")
    if l2_proc:
        _terminate_process(l2_proc)
        append_log("🛑 第2ラインの実行中プロセスを強制終了しました。", target="line2")

    with worker_lock:
        if target in ("all", "gpu") and gpu_proc:
            worker_state["process"] = None
            if not gpu_queue:
                worker_state["running"] = False
        if target in ("all", "line2") and l2_proc:
            line2_state["process"] = None
            line2_state["running"] = False

    return {"ok": True}