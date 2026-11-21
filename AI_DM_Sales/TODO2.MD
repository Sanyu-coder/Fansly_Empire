```markdown
# AI_DM_Sales 安定化 ＆ 営業エンジン刷新 TODO.md（第1弾：インフラ・通信・ブラウザ鉄壁化）

> **実装エージェントへの厳守事項**：
> 各チケットにはそのまま適用できる `【置換前】` と `【置換後】` のコードブロックが記載されています。
> **独自のアレンジや周辺コードの書き換えは一切行わず、指定された箇所のみを正確に置換・適用してください。**

---

### - [ ] TICKET-01: [DB・データ構造・API基盤] サーバー起動スーパーバイザー化と異常終了時自動復旧・クラッシュログ記録 【実行モデル：build (Qwen 3.8 32K)】
- **対象ファイル**:
  - `AI_DM_Sales/run_dm_sales_起動.bat`
- **現状の課題と達成したい目的**:
  - ワンショット起動のため、PythonプロセスやChromiumが例外・メモリ不足等で落ちると無言終了する。
  - 異常終了（終了コード `0` 以外）を検知したら `logs\server_supervisor.log` にクラッシュ時刻と終了コードを記録し、4秒後に自動再起動する。正常終了（終了コード `0`）時は自動再起動せず終了する。
- **具体的な改修内容（全文置換）**:
  - `AI_DM_Sales/run_dm_sales_起動.bat` の内容を以下のコードに丸ごと置き換えてください。

```bat
@echo off
chcp 65001 > nul
cd /d "%~dp0"

if not exist "logs" mkdir "logs"

echo ========================================================
echo   Fansly AI DM Sales 営業司令室 - スーパーバイザー起動
echo ========================================================
echo.

start "" cmd /c "timeout /t 2 /nobreak > nul && start "" [http://127.0.0.1:5051](http://127.0.0.1:5051)"

echo ブラウザが開きます。異常終了時は自動的に再起動します。
echo ※サーバーを完全停止する場合は、このウィンドウを閉じるか Ctrl+C を押してください。
echo.

:SUPERVISOR_LOOP
echo [%DATE% %TIME%] [SUPERVISOR] Starting run_dm_sales.py ... >> "logs\server_supervisor.log"
python run_dm_sales.py >> "logs\server_supervisor.log" 2>&1
set EXIT_CODE=%ERRORLEVEL%

if "%EXIT_CODE%"=="0" goto NORMAL_EXIT

echo [%DATE% %TIME%] [SUPERVISOR] CRASH DETECTED (exit_code=%EXIT_CODE%). Restarting in 4s... >> "logs\server_supervisor.log"
echo [%DATE% %TIME%] 異常終了を検知しました (code=%EXIT_CODE%)。4秒後に自動復旧します...
timeout /t 4 /nobreak > nul
goto SUPERVISOR_LOOP

:NORMAL_EXIT
echo [%DATE% %TIME%] [SUPERVISOR] Normal exit (exit_code=0). >> "logs\server_supervisor.log"
echo サーバーを正常終了しました。

```

* **完了条件**:
* `logs\server_supervisor.log` に起動・再起動ログが出力され、強制終了時に4秒後に自動再起動すること。



---

### - [ ] TICKET-02: [DB・データ構造・API基盤] Ollama推論のグローバル直列化ロック・自動リトライ・エラー可視化 【実行モデル：build (Qwen 3.8 32K)】

* **対象ファイル**:
* `AI_DM_Sales/app/core/llm_client.py`


* **現状の課題と達成したい目的**:
* 複数スレッドから同時にOllamaへリクエストが飛び、VS Code側のローカルLLM稼働中にタイムアウト（45秒）やビジーエラーが起きると `except Exception: pass` で握りつぶしてしまう。
* プロセス全体共有の直列化ロック（`_GLOBAL_OLLAMA_LOCK`）・タイムアウト延長（90秒）・指数バックオフ付き最大2回リトライ・構造化ログ出力・`last_error` 状態記録を追加する。


* **具体的な改修内容（全文置換）**:
* `AI_DM_Sales/app/core/llm_client.py` の内容を以下のコードに丸ごと置き換えてください。



```python
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from app.core import config

logger = logging.getLogger(__name__)

# プロセス全体でOllamaへの同時推論リクエストを1件ずつ直列化するロック
_GLOBAL_OLLAMA_LOCK = threading.Lock()


class LLMClient:
    """
    ローカル Ollama (http://localhost:11434) と通信する軽量クライアント。
    グローバル直列化ロック・自動リトライ・構造化ログを備え、
    ローカルGPU競合下やオフライン時でも安全にフォールバック動作する。
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: int = 90,
        max_retries: int = 2,
    ) -> None:
        self.base_url = (base_url or config.OLLAMA_BASE_URL).rstrip("/")
        self.model = model or config.OLLAMA_MODEL
        self.timeout = int(timeout)
        self.max_retries = max(0, int(max_retries))
        self.last_error: Optional[str] = None

    def _post_generate(self, prompt: str, num_predict: int = 512, temperature: float = 0.7) -> str:
        url = f"{self.base_url}/api/generate"
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": num_predict,
            },
        }
        data = json.dumps(payload).encode("utf-8")

        last_exc: Optional[Exception] = None
        with _GLOBAL_OLLAMA_LOCK:
            for attempt in range(self.max_retries + 1):
                t0 = time.monotonic()
                req = urllib.request.Request(
                    url,
                    data=data,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                try:
                    with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                        body = resp.read().decode("utf-8", errors="replace")
                        parsed = json.loads(body)
                        self.last_error = None
                        return str(parsed.get("response") or "").strip()
                except urllib.error.HTTPError as e:
                    elapsed = round(time.monotonic() - t0, 2)
                    last_exc = e
                    # 4xx系（429除く）はリトライしても回復しないため即終了
                    if 400 <= e.code < 500 and e.code != 429:
                        logger.error(
                            "[LLMClient] HTTP %s (non-retryable) model=%s elapsed=%.2fs",
                            e.code,
                            self.model,
                            elapsed,
                        )
                        break
                    logger.warning(
                        "[LLMClient] HTTP %s attempt=%d/%d model=%s elapsed=%.2fs",
                        e.code,
                        attempt + 1,
                        self.max_retries + 1,
                        self.model,
                        elapsed,
                    )
                except Exception as e:
                    elapsed = round(time.monotonic() - t0, 2)
                    last_exc = e
                    logger.warning(
                        "[LLMClient] Request error (%s: %s) attempt=%d/%d model=%s elapsed=%.2fs",
                        type(e).__name__,
                        e,
                        attempt + 1,
                        self.max_retries + 1,
                        self.model,
                        elapsed,
                    )

                if attempt < self.max_retries:
                    backoff = 2.0 * (2 ** attempt)
                    time.sleep(backoff)

        self.last_error = f"{type(last_exc).__name__}: {last_exc}" if last_exc else "Unknown LLM error"
        logger.error("[LLMClient] All retries failed model=%s err=%s", self.model, self.last_error)
        if last_exc is not None:
            raise last_exc
        raise RuntimeError(self.last_error)

    def generate_text(
        self,
        prompt: str,
        fallback: str = "",
        num_predict: int = 400,
        temperature: float = 0.7,
    ) -> str:
        try:
            out = self._post_generate(prompt=prompt, num_predict=num_predict, temperature=temperature)
            if out:
                self.last_error = None
                return out
            self.last_error = "Empty response from Ollama"
            logger.warning("[LLMClient] Empty response returned model=%s", self.model)
            return fallback
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return fallback

    def generate_json(
        self,
        prompt: str,
        fallback: Optional[Dict[str, Any]] = None,
        num_predict: int = 512,
        temperature: float = 0.3,
    ) -> Dict[str, Any]:
        raw = self.generate_text(prompt=prompt, fallback="", num_predict=num_predict, temperature=temperature)
        if not raw:
            return dict(fallback or {})
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.IGNORECASE | re.MULTILINE).strip()
        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if match:
            try:
                parsed = json.loads(match.group(0))
                if isinstance(parsed, dict):
                    return parsed
            except Exception as exc:
                logger.warning("[LLMClient] JSON parse failed model=%s err=%s", self.model, exc)
        return dict(fallback or {})

    def translate_to_japanese(self, english_text: str) -> str:
        text = (english_text or "").strip()
        if not text:
            return ""
        if re.search(r"[\u3040-\u30ff\u4e00-\u9fff]", text):
            self.last_error = None
            return text
        prompt = (
            "Translate the following English Fansly DM message into natural, conversational Japanese.\n"
            "Output ONLY the Japanese translation without quotes, notes, or explanations.\n\n"
            f"English: {text}\nJapanese:"
        )
        translated = self.generate_text(prompt=prompt, fallback="", num_predict=256, temperature=0.2)
        if translated:
            return translated
        logger.warning("[LLMClient] translate_to_japanese fallback to original text (err=%s)", self.last_error)
        return text

    def translate_to_english(self, japanese_text: str, persona_hint: str = "") -> str:
        text = (japanese_text or "").strip()
        if not text:
            return ""
        if not re.search(r"[\u3040-\u30ff\u4e00-\u9fff]", text):
            self.last_error = None
            return text
        hint_line = f"Persona tone: {persona_hint}\n" if persona_hint else ""
        prompt = (
            "Translate the following Japanese message into natural, flirty, engaging English for a Fansly creator DM.\n"
            f"{hint_line}"
            "Output ONLY the English translation without quotes or explanations.\n\n"
            f"Japanese: {text}\nEnglish:"
        )
        translated = self.generate_text(prompt=prompt, fallback="", num_predict=256, temperature=0.4)
        if translated:
            return translated
        logger.warning("[LLMClient] translate_to_english fallback to original text (err=%s)", self.last_error)
        return text

```

* **完了条件**:
* 複数スレッド同時呼び出し時に `_GLOBAL_OLLAMA_LOCK` で直列化され、エラー時にログが出力されること。



---

### - [ ] TICKET-03: [バグ修正] Playwrightスレッド安全停止・LLM処理中ハートビート維持・送信完了後DBコミット統合 【実行モデル：local-long (Qwen 3.6 128K)】

* **対象ファイル**:
* `AI_DM_Sales/app/services/fansly_dm_bot.py`
* `AI_DM_Sales/app/services/followup_worker.py`


* **現状の課題と達成したい目的**:
* ① Watchdogスレッドから別スレッド所有の `self._context.close()` / `self._pw.stop()` を直接叩いており、Playwright（Greenlet）のスレッド違反例外でプロセスごと即死する。
* ② `handle_incoming_fan_message()` 内の複数回LLM推論中にハートビートが更新されないため、VS Code稼働で推論が90秒を超えるとWatchdogが誤爆キルしてドラフトが消失する。
* ③ `FollowupWorker` がキュー投入直後（Playwrightの実送信前）に `record_ppv_offer` 等をDB確定するため、送信失敗時にDBと現実が食い違う。


* **具体的な改修内容（ピンポイント置換）**:

#### 1. `AI_DM_Sales/app/services/fansly_dm_bot.py` の置換箇所（計4箇所）

* **置換 1-A（`stop()` メソッドの他スレッド直接 close/stop 廃止）**
* 【置換前】



```python
    def stop(self) -> Dict[str, Any]:
        """バックグラウンド監視ループを安全停止し、ブラウザを閉じる"""
        self._stop_event.set()
        self._log("[Bot] 停止リクエストを受信しました...")
        try:
            if self._context is not None:
                self._context.close()
        except Exception:
            pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=8.0)
        self.is_running = False
        self.is_logged_in = False
        self.last_action = "停止済み"
        return self.get_status()

```

* 【置換後】

```python
    def stop(self) -> Dict[str, Any]:
        """バックグラウンド監視ループに停止シグナルを送り、Workerスレッド自身のfinallyで安全終了させる"""
        self._stop_event.set()
        self._log("[Bot] 停止リクエストを受信しました（Workerスレッドの安全終了を待機中）...")
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=20.0)
        self.is_running = False
        self.is_logged_in = False
        self.last_action = "停止済み"
        return self.get_status()

```

* **置換 1-B（`_watchdog_loop()` のGreenletスレッド違反クラッシュ根絶）**
* 【置換前】



```python
                    if self.is_running and not self._stop_event.is_set() and stalled_sec > self._stall_timeout_sec:
                        self._last_error = f"ハング検知 ({int(stalled_sec)}秒無応答) -> 自動再起動中"
                        self._log(f"[Watchdog] {self._last_error}")
                        self._stop_event.set()
                        try:
                            if self._context is not None:
                                self._context.close()
                        except Exception:
                            pass
                        try:
                            if self._pw is not None:
                                self._pw.stop()
                        except Exception:
                            pass
                        old_t = self._thread
                        if old_t and old_t.is_alive():
                            old_t.join(timeout=8.0)
                        self._stop_event.clear()
                        self._last_heartbeat = time.monotonic()
                        new_t = threading.Thread(target=self._run_loop, name=f"fansly-bot-{self.char_id}", daemon=True)
                        self._thread = new_t
                        new_t.start()

```

* 【置換後】

```python
                    if self.is_running and not self._stop_event.is_set() and stalled_sec > self._stall_timeout_sec:
                        self._last_error = f"ハング検知 ({int(stalled_sec)}秒無応答) -> 安全再起動待機中"
                        self._log(f"[Watchdog] {self._last_error}")
                        # 別スレッドからPlaywrightのclose/stopを直接叩くとGreenlet例外で即死するため停止フラグのみ通知
                        self._stop_event.set()
                        old_t = self._thread
                        if old_t and old_t.is_alive():
                            old_t.join(timeout=35.0)
                        if old_t and old_t.is_alive():
                            self._log("[Watchdog] 旧Workerスレッドがまだ終了処理中のため次回周期まで再起動を待機します")
                            continue
                        self._stop_event.clear()
                        self._last_heartbeat = time.monotonic()
                        new_t = threading.Thread(target=self._run_loop, name=f"fansly-bot-{self.char_id}", daemon=True)
                        self._thread = new_t
                        new_t.start()

```

* **置換 1-C（`_drain_outbound_queue()` での送信成功後 `on_sent` コールバック実行）**
* 【置換前】



```python
            if sent:
                self.touch_heartbeat("outbound_sent")
                self._log(f"[Queue] キュー送信完了 ({item.get('source')} -> {fan_id})")

```

* 【置換後】

```python
            if sent:
                self.touch_heartbeat("outbound_sent")
                cb = item.get("on_sent")
                if callable(cb):
                    try:
                        cb()
                    except Exception as cb_err:
                        self._log(f"[Queue] on_sentコールバック実行失敗 ({fan_id}): {cb_err}")
                self._log(f"[Queue] キュー送信完了 ({item.get('source')} -> {fan_id})")

```

* **置換 1-D（`handle_incoming_fan_message()` のハートビート保護＋空ドラフトガード ＆ `enqueue_outbound_message()` の `on_sent` 対応）**
* 【置換前】



```python
        # S.E.L.L. 心理営業エンジンで英語返信＋翻訳＋カルテ更新を実行
        sales_res = process_fan_message(
            char_id=self.char_id,
            fan_id=fan_id,
            fan_message=fan_text,
            username=username,
            llm=self.llm,
            db_path=self.db_path,
        )

        # 未翻訳のまま残っている受信ファンメッセージを日本語翻訳して保存
        if fan_msg_id and (not fan_msg_ja or fan_msg_ja == fan_text):
            translated_fan_ja = self.llm.translate_to_japanese(fan_text)
            if translated_fan_ja and translated_fan_ja != fan_text:
                database.update_dm_message_ja(fan_msg_id, translated_fan_ja, db_path=self.db_path)

        ai_en = str(sales_res.get("reply_en") or "").strip()
        ai_ja = str(sales_res.get("reply_ja") or "").strip()

```

* 【置換後】

```python
        # S.E.L.L. 心理営業エンジンで英語返信＋翻訳＋カルテ更新を実行（推論中のWatchdog誤爆防止にハートビート更新）
        self.touch_heartbeat("incoming_llm_sales_start")
        sales_res = process_fan_message(
            char_id=self.char_id,
            fan_id=fan_id,
            fan_message=fan_text,
            username=username,
            llm=self.llm,
            db_path=self.db_path,
        )
        self.touch_heartbeat("incoming_llm_sales_done")

        # 未翻訳のまま残っている受信ファンメッセージを日本語翻訳して保存
        if fan_msg_id and (not fan_msg_ja or fan_msg_ja == fan_text):
            translated_fan_ja = self.llm.translate_to_japanese(fan_text)
            self.touch_heartbeat("incoming_llm_translate_done")
            if translated_fan_ja and translated_fan_ja != fan_text:
                database.update_dm_message_ja(fan_msg_id, translated_fan_ja, db_path=self.db_path)

        ai_en = str(sales_res.get("reply_en") or "").strip()
        ai_ja = str(sales_res.get("reply_ja") or "").strip()
        if not ai_en:
            self._log(f"[Bot] AI返信生成が空（またはタイムアウト）のためドラフト登録をスキップ ({fan_id})")
            return {
                "handled": False,
                "reason": "empty_ai_reply",
                "fan_id": fan_id,
                "username": username,
                "sales_result": sales_res,
            }

```

* 【置換前（`enqueue_outbound_message` 関数）】

```python
def enqueue_outbound_message(
    char_id: str,
    fan_id: str,
    message_text: str,
    message_ja: Optional[str] = None,
    ppv_id: Optional[str] = None,
    price_usd: Optional[float] = None,
    source: str = "system",
) -> Dict[str, Any]:
    """FOMO追撃ワーカー等から、稼働中の FanslyDMBotWorker の送信キューへDM送信タスクを投入する"""
    item = {
        "char_id": char_id,
        "fan_id": fan_id,
        "message_text": message_text,
        "message_ja": message_ja,
        "ppv_id": ppv_id,
        "price_usd": price_usd,
        "source": source,
        "enqueued_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }

```

* 【置換後】

```python
def enqueue_outbound_message(
    char_id: str,
    fan_id: str,
    message_text: str,
    message_ja: Optional[str] = None,
    ppv_id: Optional[str] = None,
    price_usd: Optional[float] = None,
    source: str = "system",
    on_sent: Optional[Callable[[], None]] = None,
) -> Dict[str, Any]:
    """FOMO追撃ワーカー等から、稼働中の FanslyDMBotWorker の送信キューへDM送信タスクを投入する"""
    item = {
        "char_id": char_id,
        "fan_id": fan_id,
        "message_text": message_text,
        "message_ja": message_ja,
        "ppv_id": ppv_id,
        "price_usd": price_usd,
        "source": source,
        "on_sent": on_sent,
        "enqueued_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }

```

#### 2. `AI_DM_Sales/app/services/followup_worker.py` の置換箇所（計2箇所）

* **置換 2-A（`run_once()` 内の FOMO リマインド送信完了後DBコミット化）**
* 【置換前】



```python
            if enqueue_fn is not None:
                try:
                    enqueue_fn(
                        char_id=char_id,
                        fan_id=fan_id,
                        message_text=msg_en,
                        message_ja=msg_ja,
                        ppv_id=None,
                        price_usd=None,
                        source="fomo_reminder_worker",
                    )
                    queued_ok = True
                except Exception:
                    queued_ok = False

                if queued_ok:
                    database.mark_fan_fomo_reminders_sent(
                        char_id=char_id,
                        fan_id=fan_id,
                        tx_ids=tx_ids,
                        db_path=self.db_path,
                    )
                    database.add_dm_message(
                        char_id=char_id,
                        fan_id=fan_id,
                        sender="ai",
                        content=msg_en,
                        content_ja=msg_ja,
                        sales_phase="4_Lock_Close",
                        prompt_version="fomo-reminder-v1",
                        db_path=self.db_path,
                    )

```

* 【置換後】

```python
            if enqueue_fn is not None:
                def _commit_fomo_sent(
                    _cid: str = char_id,
                    _fid: str = fan_id,
                    _tids: List[int] = list(tx_ids),
                    _en: str = msg_en,
                    _ja: str = msg_ja,
                    _dbp: Optional[str] = self.db_path,
                ) -> None:
                    database.mark_fan_fomo_reminders_sent(
                        char_id=_cid,
                        fan_id=_fid,
                        tx_ids=_tids,
                        db_path=_dbp,
                    )
                    database.add_dm_message(
                        char_id=_cid,
                        fan_id=_fid,
                        sender="ai",
                        content=_en,
                        content_ja=_ja,
                        sales_phase="4_Lock_Close",
                        prompt_version="fomo-reminder-v1",
                        db_path=_dbp,
                    )

                try:
                    try:
                        enqueue_fn(
                            char_id=char_id,
                            fan_id=fan_id,
                            message_text=msg_en,
                            message_ja=msg_ja,
                            ppv_id=None,
                            price_usd=None,
                            source="fomo_reminder_worker",
                            on_sent=_commit_fomo_sent,
                        )
                    except TypeError:
                        # 旧シグネチャ互換（on_sent非対応モック等）
                        enqueue_fn(
                            char_id=char_id,
                            fan_id=fan_id,
                            message_text=msg_en,
                            message_ja=msg_ja,
                            ppv_id=None,
                            price_usd=None,
                            source="fomo_reminder_worker",
                        )
                        _commit_fomo_sent()
                    queued_ok = True
                except Exception:
                    queued_ok = False

```

* **置換 2-B（`run_step2_followups()` 内の Step 2 追撃送信完了後DBコミット化）**
* 【置換前】



```python
            if enqueue_fn is not None:
                try:
                    enqueue_fn(
                        char_id=char_id,
                        fan_id=fan_id,
                        message_text=msg_en,
                        message_ja=msg_ja,
                        ppv_id=ppv_id,
                        price_usd=price_usd,
                        source="step2_followup_worker",
                    )
                    queued_ok = True
                except Exception:
                    queued_ok = False

                if queued_ok:
                    tx_id = database.record_ppv_offer(
                        char_id=char_id,
                        fan_id=fan_id,
                        ppv_id=ppv_id,
                        offered_price=price_usd,
                        tx_type="ppv",
                        is_unlocked=0,
                        reminder_sent=0,
                        db_path=self.db_path,
                    )
                    database.add_dm_message(
                        char_id=char_id,
                        fan_id=fan_id,
                        sender="ai",
                        content=msg_en,
                        content_ja=msg_ja,
                        sales_phase="4_Lock_Close",
                        prompt_version="step2-followup-v1",
                        ppv_id=ppv_id,
                        ppv_price_usd=price_usd,
                        tx_id=tx_id,
                        db_path=self.db_path,
                    )
                    database.upsert_fan_sales_state(
                        char_id=char_id,
                        fan_id=fan_id,
                        sales_phase="4_Lock_Close",
                        db_path=self.db_path,
                    )

```

* 【置換後】

```python
            if enqueue_fn is not None:
                def _commit_step2_sent(
                    _cid: str = char_id,
                    _fid: str = fan_id,
                    _pid: str = ppv_id,
                    _price: float = price_usd,
                    _en: str = msg_en,
                    _ja: str = msg_ja,
                    _dbp: Optional[str] = self.db_path,
                ) -> None:
                    tx_id = database.record_ppv_offer(
                        char_id=_cid,
                        fan_id=_fid,
                        ppv_id=_pid,
                        offered_price=_price,
                        tx_type="ppv",
                        is_unlocked=0,
                        reminder_sent=0,
                        db_path=_dbp,
                    )
                    database.add_dm_message(
                        char_id=_cid,
                        fan_id=_fid,
                        sender="ai",
                        content=_en,
                        content_ja=_ja,
                        sales_phase="4_Lock_Close",
                        prompt_version="step2-followup-v1",
                        ppv_id=_pid,
                        ppv_price_usd=_price,
                        tx_id=tx_id,
                        db_path=_dbp,
                    )
                    database.upsert_fan_sales_state(
                        char_id=_cid,
                        fan_id=_fid,
                        sales_phase="4_Lock_Close",
                        db_path=_dbp,
                    )

                try:
                    try:
                        enqueue_fn(
                            char_id=char_id,
                            fan_id=fan_id,
                            message_text=msg_en,
                            message_ja=msg_ja,
                            ppv_id=ppv_id,
                            price_usd=price_usd,
                            source="step2_followup_worker",
                            on_sent=_commit_step2_sent,
                        )
                    except TypeError:
                        enqueue_fn(
                            char_id=char_id,
                            fan_id=fan_id,
                            message_text=msg_en,
                            message_ja=msg_ja,
                            ppv_id=ppv_id,
                            price_usd=price_usd,
                            source="step2_followup_worker",
                        )
                        _commit_step2_sent()
                    queued_ok = True
                except Exception:
                    queued_ok = False

```

* **完了条件**:
* Watchdog停止・再起動時にGreenletスレッド違反例外が発生しないこと。
* 送信完了後のみ `on_sent` コールバックで `record_ppv_offer` / `add_dm_message` / `sales_phase` が確定すること。
