"""
run_dm_sales_watcher.py (Ticket 1.4)

`python run_dm_sales.py` を子プロセスとして起動・監視し、異常終了 (終了コード0以外)
を検知したら5秒待機して自動再起動するラッパー。

- 親プロセスに SIGINT (Ctrl+C) / SIGTERM が送られた場合、子プロセスのツリーを安全に
  terminate() し、再起動ループを抜けて終了コード 0 で正常終了する。
- 子プロセスが短時間 (1分以内) に5回連続クラッシュした場合は暴走防止のため
  停止し、Discord Webhook へ緊急停止通知を飛ばす。

実装上の要点:
- 子プロセスは CREATE_NEW_PROCESS_GROUP で独立プロセスグループに起動するため、
  コンソールの Ctrl+C が子 (run_dm_sales.py / Chrome) に直接届かず、
  ウォッチャーが意図的に子ツリーを停止できる。
- メインループは proc.wait() ではなく短いポーリングで子を監視するため、
  シグナルハンドラが即座に実行され、安全停止がブロックされない。
"""

import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from typing import Optional

# app.core (discord_notifier) をインポートできるようプロジェクトルートを sys.path に追加
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

CHILD_SCRIPT = os.path.join(_SCRIPT_DIR, "run_dm_sales.py")

RESTART_DELAY_SEC = 5.0
CRASH_WINDOW_SEC = 60.0
MAX_CRASHES_IN_WINDOW = 5

# Windows: 子プロセスを独立プロセスグループに起動 (Ctrl+C の直接伝播を遮断)
_CREATE_NEW_PROCESS_GROUP = 0x00000200

# シグナルハンドラから参照される共有状態 (main() 内で初期化される)
_STATE: Optional["WatcherState"] = None


class WatcherState:
    """再起動ループの制御用状態。シグナルハンドラとメインループが共有する。"""

    def __init__(self) -> None:
        self.stop_requested = False
        self.child: Optional["subprocess.Popen"] = None
        self.child_lock = threading.Lock()


def _emit_emergency_stop_alert() -> None:
    """暴走防止による緊急停止を Discord Webhook へ通知する (非同期・例外耐性)。"""
    try:
        from app.core import discord_notifier

        discord_notifier.send_discord_alert(
            title="🚨 AI_DM_Sales 緊急停止 (クラッシュ暴走防止)",
            message=(
                "run_dm_sales.py が1分以内に5回連続でクラッシュしたため、"
                "暴走防止のためウォッチャーが再起動ループを停止しました。"
                "手動での確認・再起動をお願いします。"
            ),
            level="ERROR",
            extra_fields={
                "target": "run_dm_sales.py",
                "reason": "5 crashes within 60s",
            },
        )
    except Exception:
        # 通知失敗でもプロセスは停止する
        pass


def _handle_shutdown(signum: int, _frame) -> None:
    """SIGINT / SIGTERM 受信時に再起動ループ停止フラグを設定する。"""
    print(f"[watcher] 受信シグナル {signum} -> 安全停止へ移行します", flush=True)
    if _STATE is not None:
        _STATE.stop_requested = True


def _kill_child_tree(proc: "subprocess.Popen") -> None:
    """子プロセスのツリー (Python + Chrome 等) を安全に停止する。"""
    if proc is None:
        return
    pid = proc.pid
    if pid is None:
        return
    if proc.poll() is not None:
        return
    # 独立プロセスグループ内の全プロセスを taskkill /T で停止
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15.0,
        )
    except Exception:
        pass
    # 万一 taskkill で倒れなかった場合は直接 terminate/kill
    try:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=10.0)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def main() -> int:
    global _STATE
    _STATE = WatcherState()
    signal.signal(signal.SIGINT, _handle_shutdown)
    try:
        signal.signal(signal.SIGTERM, _handle_shutdown)
    except (AttributeError, ValueError):
        # Windows で SIGTERM がサポートされない環境のフォールバック
        pass

    crash_times: "deque[float]" = deque()
    restart_count = 0

    print("[watcher] run_dm_sales.py の監視を開始します。", flush=True)

    while not _STATE.stop_requested:
        try:
            proc = subprocess.Popen(
                [sys.executable, CHILD_SCRIPT],
                cwd=_SCRIPT_DIR,
                creationflags=_CREATE_NEW_PROCESS_GROUP,
            )
        except Exception as exc:  # 子プロセス起動自体の失敗
            print(f"[watcher] 子プロセス起動失敗: {exc}", flush=True)
            if _STATE.stop_requested:
                break
            time.sleep(RESTART_DELAY_SEC)
            continue

        with _STATE.child_lock:
            _STATE.child = proc

        # 短いポーリングで子を監視 (シグナルハンドラが即実行される)
        exit_code: Optional[int] = None
        while not _STATE.stop_requested:
            if proc.poll() is not None:
                exit_code = proc.returncode
                break
            time.sleep(0.25)

        with _STATE.child_lock:
            _STATE.child = None

        if _STATE.stop_requested:
            # 安全停止: 子ツリーを停止して終了
            _kill_child_tree(proc)
            print("[watcher] 停止要求により全プロセスを停止しました。", flush=True)
            return 0

        if exit_code == 0:
            print("[watcher] run_dm_sales.py が正常終了 (exit=0)。監視を終了します。", flush=True)
            return 0

        # 異常終了 (クラッシュ) -> 暴走判定
        now = time.time()
        crash_times.append(now)
        while crash_times and now - crash_times[0] > CRASH_WINDOW_SEC:
            crash_times.popleft()

        if len(crash_times) >= MAX_CRASHES_IN_WINDOW:
            print(
                "[watcher] 1分以内に5回連続クラッシュを検出。暴走防止のため停止します。",
                flush=True,
            )
            _emit_emergency_stop_alert()
            time.sleep(1.0)  # 通知スレッドの完了を短時間待つ
            return 0

        restart_count += 1
        print(
            f"[watcher] run_dm_sales.py が異常終了 (exit={exit_code})。"
            f"{RESTART_DELAY_SEC:.0f}秒後に再起動します "
            f"(クラッシュ{len(crash_times)}回目 / 再起動#{restart_count})。",
            flush=True,
        )
        time.sleep(RESTART_DELAY_SEC)

    # ループ終了 (停止要求) のフォールバック: 子ツリーを停止
    with _STATE.child_lock:
        child = _STATE.child
    _kill_child_tree(child)
    print("[watcher] 全プロセスを停止しました。", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
