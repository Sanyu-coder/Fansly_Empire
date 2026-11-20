# Phase 1 第3回実装（最終回）：Playwright 全キャラ同時並行DM監視・送信ボット＆未開封FOMOリマインド・Step 2追撃ワーカー

## 1. 概要・日付・対象ファイル
- **作業日**: 2026-11-18
- **概要**: `AI_DM_Sales`（ポート `5051`）の Phase 1 第3回実装（最終回）として、Playwright による全キャラクター同時並行DM監視・送信ボット（`fansly_dm_bot.py`）、手動ログイン用ヘッドフルブラウザ起動連携、および未開封PPVへのFOMOリマインド＆Step 1購入直後のStep 2追撃ワーカー（`followup_worker.py`）を実装し、Web管理コンソールAPI（`dashboard.py`, `index.html`, `run_dm_sales.py`）へ直結した。
- **対象ファイル**:
  - `AI_DM_Sales/app/services/fansly_dm_bot.py` (新規)
  - `AI_DM_Sales/app/services/followup_worker.py` (新規)
  - `AI_DM_Sales/app/core/database.py` (更新)
  - `AI_DM_Sales/app/routes/dashboard.py` (更新)
  - `AI_DM_Sales/app/templates/index.html` (更新)
  - `AI_DM_Sales/run_dm_sales.py` (更新)
  - `AI_DM_Sales/PROJECT_MAP.md` (更新)
  - `AI_DM_Sales/docs/HANDOVER.md` (更新)
  - `AI_DM_Sales/docs/2026-11-18_phase1_playwright_bot_and_followup_worker.md` (新規・本書)

---

## 2. 背景・解決したかった課題（Before）
- Phase 1 第2回までに S.E.L.L. 営業エンジン（`sales_engine.py`）とリアルタイム顧客カルテ抽出（`profiler_service.py`）、Web管理画面・PPV武器庫が構築されていたが、実際に Fansly 上のDMをキャラ別セッションで巡回監視・送信するボット（`fansly_dm_bot.py`）および、送信後未開封のPPVに対するFOMOリマインド・Step 1購入直後のStep 2自動追撃を行うバックグラウンドワーカー（`followup_worker.py`）が未実装だった。
- また、Web管理画面ヘッダーの `[▶ 全キャラ一括稼働 / ⏹ 全停止]` や個別キャラトグルスイッチ、`[⚙️ 設定]` モーダルの「🔑 ブラウザを開いて手動ログイン / 再認証」ボタンを、設定ファイル書き換えだけでなく実際の `BotManager` スレッド制御と連動させる必要があった。

---

## 3. 採用した設計方針・理由（Why / ADR）
1. **キャラ別永続ブラウザプロファイル完全分離とスレッド独立設計 (`app/services/fansly_dm_bot.py`)**:
   - 複数キャラ同時並行稼働時のCookie・セッション衝突やPlaywrightのgreenletスレッド制約問題を排除するため、キャラごとに `config.PROFILES_DIR / <char_id>` を `user_data_dir` とする `CharacterBotWorker` スレッドを独立生成し、`BotManager`（シングルトン `bot_manager`）が `threading.RLock()` でスレッドセーフに統括するアーキテクチャを採用した。
   - 手動ログイン（`launch_manual_login(char_id)`）実行時は、当該キャラの自動監視ワーカーが稼働中であれば一時停止してプロファイルロック競合を回避した上で、`headless=False` で `https://fansly.com/messages` を開き、ユーザーがログインしてブラウザを閉じるまで待機してセッションを保存する設計とした。
2. **Fansly DOMセレクタのモジュール上部集約と非クラッシュ堅牢設計 (`FANSLY_SELECTORS`)**:
   - Fanslyのフロントエンド改修時に1箇所の修正で即対応できるよう、スレッド一覧・未読バッジ・サブスクプランバッジ・PPV開封表示・チャット入力欄・ファイル添付input・ロック価格設定ボタン等のセレクタ候補リストを `FANSLY_SELECTORS` 定数に集約した。DOM未検出時は警告ログと `last_error` 記録のみを行い、例外でワーカースレッドを落とさない安全設計とした。
3. **原子的な二重送信防止を備えた追撃ワーカー (`app/services/followup_worker.py`)**:
   - 未開封PPVのFOMOリマインドでは、`ppv_transactions` から「送信後6〜12時間経過」「`is_unlocked == 0`」「`reminder_sent == 0`」かつ顧客が「緊急停止・ミュート・省エネ降格でない」レコードを抽出し、送信キュー投入前に `mark_fomo_reminder_sent(tx_id)`（`WHERE tx_id = ? AND reminder_sent = 0`）を実行することで、複数サイクル実行時にも2回以上リマインドが送られないことをDBレベルで保証した。
   - Step 1購入後のStep 2追撃提案でも、Step 1開封（`unlocked_at`）から30分〜6時間経過した顧客に対し、既にStep 2以上のPPV提案レコードが存在しないことを確認した上で未購入のStep 2素材を選定し、即座に `record_ppv_offer()` で記録することで重複提案を防止した。

---

## 4. 実装内容・変更点サマリ（After）
- **`app/services/fansly_dm_bot.py` (新規)**:
  - `FANSLY_SELECTORS` 定数、`calculate_human_delays()`（文字数比例既読遅延 60〜300秒＋タイピング演出 10〜30秒、テスト高速モード対応）、`check_char_sleep_state()`（JST睡眠時間帯判定）、`resolve_fan_tier_from_badge()` を実装。
  - `CharacterBotWorker` および `BotManager`（シングルトン `bot_manager`）を実装。`start_all()`, `stop_all()`, `toggle_char()`, `get_status()`, `launch_manual_login()`, `handle_incoming_message()`, `enqueue_outbound_message()` を提供。
- **`app/services/followup_worker.py` (新規)**:
  - `FollowupWorker`（シングルトン `followup_worker`）を実装。
  - `process_unopened_ppv_reminders()`（6〜12時間経過未開封PPVへの日英FOMOリマインド生成・送信キュー投入・`reminder_sent = 1` 更新）および `process_step2_followups()`（Step 1購入30分〜6時間後のStep 2自動追撃提案）を実装。
- **`app/core/database.py` (更新)**:
  - `record_ppv_offer()`, `mark_ppv_unlocked()`, `mark_fomo_reminder_sent()`, `get_pending_fomo_reminders()`, `get_unlocked_ppv_transactions()`, `get_fan_ppv_transactions()` を追加。
- **`app/routes/dashboard.py`, `app/templates/index.html`, `run_dm_sales.py` (更新)**:
  - `GET /api/bot/status`, `POST /api/bot/start_all`, `POST /api/bot/stop_all`, `POST /api/bot/toggle_char`, `POST /api/bot/manual_login` を `bot_manager` ＆ `followup_worker` に直結。
  - `run_dm_sales.py` 起動時に `bot_manager.sync_workers_with_characters()` と `followup_worker.start()` を自動初期化（DMボットの自動稼働自体はダッシュボードの稼働ボタン操作で開始する安全設計）。

---

## 5. 作業中に発生した問題・ハマりポイントと解決策（Troubleshooting）
- **問題1**: 新規ファンからの初回DM受信時に `get_thread_detail()` が `None` を返すケースがあり、`thread_detail.get("messages", [])` で `AttributeError` が発生した。また、`ensure_character_files()` の返却辞書キーは `vault_dir` であったが、初期コードで `ppv_vault_dir` を参照していた。
- **解決策1**: `(get_thread_detail(...) or {}).get("messages", [])` に修正し、Vaultパスも `paths.get("vault_dir")` を参照するよう修正した。またブラウザプロファイルパスは仕様通り `os.path.join(config.PROFILES_DIR, char_id)` に統一した。
- **問題2**: 統合テストで `FollowupWorker.run_once()` を実行した際、ローカルOllamaへのLLM生成リクエスト待ちでテスト実行時間が数秒かかった。
- **解決策2**: `FollowupWorker` のコンストラクタで任意の `llm` クライアントおよび `bot_mgr` をDI（依存性注入）可能にし、単体テスト時は高速フォールバック設定の `LLMClient` を注入できるようにしたことで、本番時はローカルOllama推論、テスト時は0.02秒での決定論的検証を両立した。

---

## 6. 今後の注意点・残タスク
- これにて **Phase 1（v1.0 コア構築）の全機能が100%実装完了** となった。
- 次フェーズ（Phase 2：v1.1 自己進化・LTV最大化拡張）では、`app/services/optimizer_batch.py` による「成約/離脱会話の比較分析」と「A/Bテスト式プロンプト自動昇格エンジン（`sales_playbooks`）」、および休眠ファン掘り起こしバッチを実装予定。
