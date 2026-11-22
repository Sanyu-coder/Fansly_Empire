# -*- coding: utf-8 -*-
"""
Phase 2 (Ticket 2.1 / 2.2) 単体テスト

- Ticket 2.1:
  - Test1: generate_text() のペイロードに "keep_alive": "60s" が含まれること
  - Test2: PriorityLock で HIGH が LOW より先に実行されること
  - Test3: get_effective_llm_config() が system_settings の個別URL/モデル名を優先すること
- Ticket 2.2:
  - Test4: execute_with_resilient_retry() が失敗を破棄せず2分間隔（テストでは短縮）で
    リトライを継続し、最大回数到達のみで例外送出すること
  - Test5: 15分滞留（テストでは短縮）時に Discord 滞留通知が1回だけ送られること
  - Test6: Ollama停止（接続拒否）でもタスクが破棄されずリトライが継続すること

実行: python -m unittest tests.test_phase2_llm_priority_retry -v
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core import llm_client
from app.core.llm_client import (
    LLMResilientRetryExhaustedError,
    Priority,
    PriorityLock,
    execute_with_resilient_retry,
    get_effective_llm_config,
    llm_client as _module_client,
)


# ---------------------------------------------------------------------------
# HTTP モックヘルパー
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, body: dict):
        self._body = json.dumps(body).encode("utf-8")
        self.status = 200

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _UrllibPatch:
    """urllib.request.urlopen / Request をモックし、tearDownで元に戻す。"""

    def __init__(self, captured: dict, body: dict | None = None):
        self.captured = captured
        self.body = body or {"response": "OK_REPLY", "done": True}
        self._orig_urlopen = None
        self._orig_request = None

    def start(self):
        self._orig_urlopen = urllib.request.urlopen
        self._orig_request = urllib.request.Request

        def fake_urlopen(req, timeout=None):
            self.captured["url"] = req.full_url
            self.captured["payload"] = json.loads(req.data.decode("utf-8"))
            self.captured["timeout"] = timeout
            return _FakeResp(self.body)

        urllib.request.urlopen = fake_urlopen

    def stop(self):
        urllib.request.urlopen = self._orig_urlopen
        urllib.request.Request = self._orig_request


# ---------------------------------------------------------------------------
# Ticket 2.1
# ---------------------------------------------------------------------------

class TestKeepAlivePayload(unittest.TestCase):
    """generate_text() のリクエストペイロードに keep_alive: 60s が含まれること"""

    def test_generate_text_payload_contains_keep_alive(self):
        captured: dict = {}
        patch = _UrllibPatch(captured)
        patch.start()
        try:
            out = _module_client.generate_text("sys", "user", temperature=0.5, max_tokens=128)
            self.assertEqual(out, "OK_REPLY")
            self.assertIn("payload", captured)
            payload = captured["payload"]
            self.assertEqual(payload.get("keep_alive"), "60s")
            self.assertEqual(payload.get("model"), _module_client.model_name)
            self.assertFalse(payload.get("stream"))
        finally:
            patch.stop()

    def test_generate_json_payload_contains_keep_alive(self):
        captured: dict = {}
        patch = _UrllibPatch(captured, body={"response": '{"a": 1}', "done": True})
        patch.start()
        try:
            data = _module_client.generate_json("sys", "user", default_data={"a": 0})
            self.assertEqual(data, {"a": 1})
            self.assertEqual(captured["payload"].get("keep_alive"), "60s")
            self.assertEqual(captured["payload"].get("format"), "json")
        finally:
            patch.stop()


class TestPriorityLockOrdering(unittest.TestCase):
    """HIGH の要求がキューにある間は LOW が譲歩し、HIGH が先に実行されること"""

    def test_high_runs_before_low(self):
        lock = PriorityLock(acquire_timeout_sec=10.0)
        order: list = []
        high_started = threading.Event()

        def high_task():
            with lock.context(Priority.HIGH):
                order.append("HIGH")
                high_started.set()
                time.sleep(0.2)

        def low_task():
            high_started.wait(timeout=10)  # HIGH が実行中であることを保証してから LOW 開始
            with lock.context(Priority.LOW):
                order.append("LOW")
                time.sleep(0.1)

        t_high = threading.Thread(target=high_task)
        t_low = threading.Thread(target=low_task)
        t_high.start()
        t_low.start()
        t_high.join(timeout=10)
        t_low.join(timeout=10)
        self.assertFalse(t_high.is_alive(), "HIGHタスクが完了しない（デッドロック）")
        self.assertFalse(t_low.is_alive(), "LOWタスクが完了しない（デッドロック）")
        self.assertEqual(order, ["HIGH", "LOW"])
        self.assertEqual(lock.waiting_count(), 0)

    def test_low_yields_to_high_waiting_in_queue(self):
        lock = PriorityLock(acquire_timeout_sec=10.0)
        order: list = []
        # LOW が先にロックを占有
        with lock.context(Priority.LOW):
            order.append("LOW_START")
            # この待機中に HIGH がキューへ投入される
            barrier = threading.Barrier(2)
            high_done = threading.Event()

            def high_task():
                barrier.wait()
                with lock.context(Priority.HIGH):
                    order.append("HIGH")
                    time.sleep(0.1)
                high_done.set()

            th = threading.Thread(target=high_task)
            th.start()
            barrier.wait()
            # LOW は HIGH の完了まで release しない（譲歩の検証）
            high_done.wait(timeout=10)
        order.append("LOW_END")
        th.join(timeout=10)
        self.assertEqual(order, ["LOW_START", "HIGH", "LOW_END"])


class TestGetEffectiveLlmConfig(unittest.TestCase):
    """[⚙️ 設定] の dm_ollama_base_url / dm_ollama_model_name が優先されること"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "test_empire_core.db")

    def tearDown(self):
        self._tmp.cleanup()

    def test_override_from_system_settings(self):
        from app.core import database
        database.init_db(db_path=self.db_path)
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT OR REPLACE INTO system_settings (setting_key, setting_value, updated_at) "
            "VALUES (?, ?, datetime('now'));",
            ("dm_ollama_base_url", "http://192.168.1.50:11434/v1/"),
        )
        conn.execute(
            "INSERT OR REPLACE INTO system_settings (setting_key, setting_value, updated_at) "
            "VALUES (?, ?, datetime('now'));",
            ("dm_ollama_model_name", "gpu2-qwen:7b"),
        )
        conn.commit()
        conn.close()

        cfg = get_effective_llm_config(db_path=self.db_path)
        self.assertEqual(cfg["base_url"], "http://192.168.1.50:11434/v1")
        self.assertEqual(cfg["model_name"], "gpu2-qwen:7b")

    def test_fallback_to_config_defaults(self):
        from app.core import database
        database.init_db(db_path=self.db_path)
        # dm_ollama_* はデフォルト空文字 -> config デフォルトにフォールバック
        cfg = get_effective_llm_config(db_path=self.db_path)
        self.assertEqual(cfg["base_url"], llm_client.config.OLLAMA_BASE_URL.rstrip("/"))
        self.assertEqual(cfg["model_name"], llm_client.config.MODEL_NAME)

    def test_invalid_db_path_falls_back(self):
        cfg = get_effective_llm_config(db_path=r"C:\nonexistent_dir_xyz\never.db")
        self.assertEqual(cfg["base_url"], llm_client.config.OLLAMA_BASE_URL.rstrip("/"))
        self.assertEqual(cfg["model_name"], llm_client.config.MODEL_NAME)


# ---------------------------------------------------------------------------
# Ticket 2.2
# ---------------------------------------------------------------------------

class TestResilientRetry(unittest.TestCase):
    """execute_with_resilient_retry の不屈リトライ挙動"""

    def test_success_after_failures(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise ConnectionError("ollama down")
            return "DONE"

        result = execute_with_resilient_retry(
            flaky, "test_flaky", max_retries=5, retry_interval_sec=0.02,
            alert_delay_sec=999.0, attempt_timeout_sec=2.0,
        )
        self.assertEqual(result, "DONE")
        self.assertEqual(calls["n"], 3)

    def test_exhausts_and_raises_only_at_max(self):
        calls = {"n": 0}

        def always_fail():
            calls["n"] += 1
            raise ConnectionError("ollama down")

        with self.assertRaises(LLMResilientRetryExhaustedError) as ctx:
            execute_with_resilient_retry(
                always_fail, "test_exhaust", max_retries=4, retry_interval_sec=0.02,
                alert_delay_sec=999.0, attempt_timeout_sec=2.0,
            )
        # 20回（ここでは4回）すべて試行されたこと＝メッセージが破棄されず保持されたこと
        self.assertEqual(calls["n"], 4)
        self.assertEqual(ctx.exception.attempts, 4)
        self.assertIsInstance(ctx.exception.last_error, ConnectionError)

    def test_retries_continue_on_connection_refused(self):
        """Ollama停止（接続拒否）でもタスクが破棄されずリトライが継続されること"""

        def real_call():
            # urlopen が接続拒否を投げるようモック
            def refuse(req, timeout=None):
                raise urllib.error.URLError("Connection refused (Ollama stopped)")
            orig = urllib.request.urlopen
            urllib.request.urlopen = refuse
            try:
                _module_client._call_chat_completions("sys", "user")
            finally:
                urllib.request.urlopen = orig
            raise RuntimeError("llm call failed")

        calls = {"n": 0}
        start = time.time()
        with self.assertRaises(LLMResilientRetryExhaustedError):
            execute_with_resilient_retry(
                lambda: (calls.__setitem__("n", calls["n"] + 1) or real_call()),
                "test_conn_refused", max_retries=3, retry_interval_sec=0.15,
                alert_delay_sec=999.0, attempt_timeout_sec=2.0,
            )
        elapsed = time.time() - start
        self.assertEqual(calls["n"], 3)
        # 2回の間隔(0.15s×2)以上経過＝リトライが継続された証拠
        self.assertGreaterEqual(elapsed, 0.25)

    def test_stagnation_alert_sent_once(self):
        """経過時間が alert_delay_sec を超えると Discord 通知が1回だけ送られること"""
        import types
        sent_alerts: list = []

        fake_notifier = types.ModuleType("app.core.discord_notifier")

        def fake_send_discord_alert(title, message, level="ERROR",
                                    extra_fields=None, db_path=None):
            sent_alerts.append({"title": title, "level": level})
            return True

        fake_notifier.send_discord_alert = fake_send_discord_alert
        sys.modules["app.core.discord_notifier"] = fake_notifier
        try:
            def slow_fail():
                raise ConnectionError("ollama down")

            with self.assertRaises(LLMResilientRetryExhaustedError):
                execute_with_resilient_retry(
                    slow_fail, "test_alert", max_retries=3, retry_interval_sec=0.05,
                    alert_delay_sec=0.08, attempt_timeout_sec=2.0,
                )
        finally:
            # 元のモジュールを復元
            from app.core import discord_notifier as _real
            sys.modules["app.core.discord_notifier"] = _real

        self.assertEqual(len(sent_alerts), 1, "滞留通知は1回だけ送られるべき")
        self.assertIn("15分以上滞留中", sent_alerts[0]["title"])
        self.assertEqual(sent_alerts[0]["level"], "WARNING")

    def test_attempt_timeout_triggers_retry(self):
        """1試行が attempt_timeout_sec を超えるとタイムアウトとして再試行されること"""
        calls = {"n": 0}

        def hang():
            calls["n"] += 1
            time.sleep(0.5)
            return "NEVER"

        with self.assertRaises(LLMResilientRetryExhaustedError) as ctx:
            execute_with_resilient_retry(
                hang, "test_hang", max_retries=2, retry_interval_sec=0.02,
                alert_delay_sec=999.0, attempt_timeout_sec=0.15,
            )
        self.assertEqual(calls["n"], 2)
        self.assertIsInstance(ctx.exception.last_error, TimeoutError)


if __name__ == "__main__":
    unittest.main(verbosity=2)
