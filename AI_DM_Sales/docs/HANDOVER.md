# HANDOVER.md - AI_DM_Sales 作業引き継ぎ＆進捗管理ドキュメント

## 1. 現在の実装ステータス（2026-11-18 時点）
### ✅ 完了済み：Phase 1 第1回実装（土台・統合DB・共通マスター・完成版Web UI・PPV武器庫ミニアプリ）
7. **本番データ保護・AI顧客カルテ日本語化＆ダミーシード完全撤廃（2026-11-18改修）**
   - `database.py` / `__init__.py`: 初期起動時のダミーデータシード（`_seed_demo_data_if_empty`）を完全無効化・廃止し、本番DB（`empire_core.db`）からダミー顧客5名（`Lucas_VIP`, `Mike_NekoFan`, `SuspiciousUser99`, `GhostWatcher`, `DarkKnight_77`）およびダミー会話履歴・ダミーPPVトランザクションを完全削除。実機データ（`@Sanyu28394`）のみを保護。
   - `profiler_service.py` / `sales_engine.py`: AI顧客カルテ（`psych_profile_json` の `preferences`, `psych_type`, `budget_tier`, `hooks`, `notes`）の抽出プロンプトおよびフォールバック/省エネ/緊急停止時プロファイルをすべて自然で分かりやすい日本語出力に統一。
   - `format_psych_profile_for_prompt()`: 日本語の顧客カルテをシステムプロンプトへ注入し、英語営業文生成指示と連動して「日本語カルテから自然な英語営業返信を生成する」ガイドを付与。
   - `index.html`: タブ1「営業モニタリング」右ペインおよびタブ4「営業シミュレーター」右ペインインスペクターで、日本語顧客カルテおよび営業メモ（`notes`）が綺麗に描画されることを確認。

8. **コア基盤・統合DB (`app/core/`)**
   - `config.py`: `SHARED_ASSETS_DIR` (`C:\Fansly_Empire\shared_assets`)、`CHARACTERS_ROOT`、`DB_PATH` (`shared_assets\database\empire_core.db`)、`PROFILES_DIR`、`OLLAMA_BASE_URL` (`http://127.0.0.1:11434/v1`)、`MODEL_NAME` (`my-qwen3:latest`)、ポート `5051` を定義。
   - `database.py`: 接続時に必ず `PRAGMA journal_mode=WAL;` を有効化し、`daily_account_metrics`, `posts_log`, `fans`, `dm_messages`（`content` 英語原文 ＋ `content_ja` 日本語訳カラム、`ALTER TABLE` 自動マイグレーション対応）, `ppv_transactions`, `sales_playbooks` の6テーブルを初期化。初回空DB時のダミー顧客5名＆日英併記サンプルチャット履歴シード (`_seed_demo_data_if_empty`)、PPV実績集計、2ペインチャットタイムライン用のDBヘルパーを実装。
   - `llm_client.py`: ローカルOllama API呼び出し（`/api/generate` への `think: False` 指定によるQwen3 `<think>` トークン切れ防止・テキスト生成・JSON構造化抽出・PPVタグ/英語 `pitch_hint` 自動生成・日本語→キャラ口調英語変換 `translate_jp_to_character_en`・英語→日本語翻訳 `translate_to_japanese`・テスト客日本語→英語翻訳 `translate_fan_ja_to_en`）およびオフライン時のフォールバック処理を実装。



2. **共通キャラクター＆PPV武器庫バックエンド (`app/services/character_loader.py`, `app/routes/vault_routes.py`)**
   - `shared_assets\characters\<char_id>` 配下の `dm_persona.json`, `plans.json`, `settings.json`（初期値: `default_prices` [Step 1〜3] および `sleep_schedule: {"enabled": false, "start": "19:00", "end": "22:00"}`）、`ppv_vault\vault_index.json` のCRUD・自動生成・正規化を実装。
   - PPV武器庫API一式（`/api/vault/preview_draft`, `/api/vault/upload`, `/api/vault/list`, `/api/vault/update`, `/api/vault/delete`, `/api/settings/save`）を実装。
   - `/api/vault/list` は階段レベル (`all`/`1`/`2`/`3`)、メディア種別 (`all`/`video`/`image`)、5種ソート (`newest`, `oldest`, `offers_desc`, `purchases_desc`, `revenue_desc`)、および `ppv_transactions` とのJOIN集計（提案回数・購入回数・売上金額 USD）に完全対応。

3. **完成版Web管理UI (`app/routes/dashboard.py`, `app/templates/index.html`, `run_dm_sales.py`)**
   - ダークモード基調・王冠ファビコン（👑）付きUIを構築。
   - 共通ヘッダー：`[▶ 全キャラ一括稼働 / ⏹ 全停止]` マスターボタン ＋ クリック展開式キャラドロップダウン（表示キャラ切替 ＋ 各キャラ横の個別稼働ON/OFFトグルスイッチ ＋ 睡眠中 `💤` バッジ） ＋ `[⚙️ 設定]` ボタン ＋ `[＋ PPV素材追加]` ボタン。
   - 4タブ構成（1.営業モニタリング［サマリー・緊急停止アラート枠・左右2ペインチャットタイムライン・日本語→英語キャラ口調変換介入ボックス］ / 2.PPV武器庫一覧［フィルタ・5種ソート・サムネイル＋4要素＋実績カードグリッド］ / 3.キャラ・プラン設定GUI / 4.営業シミュレーター）および2モーダルを実装。

### ✅ 完了済み：Phase 1 第2回実装（S.E.L.L. 営業エンジン・リアルタイム顧客カルテ抽出・シミュレーター本番直結）
4. **リアルタイム顧客カルテ抽出サービス (`app/services/profiler_service.py`)**
   - `update_fan_psych_profile(char_id, fan_id, recent_messages, existing_profile_json)`: Ollamaネイティブ `/api/generate` + `"think": False` 経由で、直近の会話履歴と既存プロファイルから `preferences`（好み・フェチ）、`psych_type`（心理タイプ）、`budget_tier`（推定予算感）、`hooks`（反応が良い話題）をJSON抽出・マージし、DBの `fans.psych_profile_json` を更新。
   - `format_psych_profile_for_prompt(profile_data)`: 顧客カルテを次回営業応答のシステムプロンプトへ注入する構造化テキストブロックに整形。

5. **S.E.L.L. 営業エンジン本体 (`app/services/sales_engine.py`) & シミュレーター直結 (`app/routes/dashboard.py`, `app/templates/index.html`)**
   - `SalesEngine`: 危険ワード・連続未読スルーによる緊急停止ロック（`is_escalated = 1`）、無料会員＋PPV 2回連続無視客の省エネ降格（`6_Low_Priority` / `is_low_priority = 1`）、S.E.L.L.フェーズ自動遷移（`1_Icebreak` → `2_Explore` → `3_Lead_Pitch` → `4_Lock_Close` → `5_Cooldown`）、DB `ppv_transactions` の既購入除外＋階段式PPV（Step 1→2→3）選定、動的プライシング（`tripwire` / `base` / `whale`）、およびキャラ口調・プラン・顧客カルテ連動プロンプトによる英語営業メッセージ＋日本語訳生成を統合。
   - `POST /api/simulator/reply`: `SalesEngine` および `profiler_service` に完全直結（日英2段表示＆日本語→英語変換送信機能を維持）。

### ✅ 完了済み：Phase 1 第3回実装（最終回：Playwright全キャラ同時並行DM監視ボット＆未開封FOMOリマインド・Step 2追撃ワーカー）
6. **Playwright全キャラ同時並行DM監視・送信ボット (`app/services/fansly_dm_bot.py`)**
   - `BotManager`（シングルトン `bot_manager`）および `CharacterBotWorker`:
     - キャラごとに `config.PROFILES_DIR / <char_id>` の永続ブラウザプロファイル（`launch_persistent_context`）を完全分離。
     - `launch_manual_login(char_id)`: `[⚙️ 設定]` モーダルから `POST /api/bot/manual_login` で呼び出され、`headless=False` で `https://fansly.com/messages` を開き、ログイン完了後にブラウザを閉じるまで待機・セッション保存。
     - 全キャラ一括稼働/停止（`POST /api/bot/start_all`, `POST /api/bot/stop_all`）およびキャラ別トグル（`POST /api/bot/toggle_char`）、稼働ステータス取得（`GET /api/bot/status`）に完全連動。
     - JST睡眠時間判定（`sleep_schedule`）による自動返信保留と `sleeping: true`（`💤` バッジ表示）返却。
     - 未読DM検知・顧客サブスクプランバッジ判定・PPV開封検知（`is_unlocked = 1` 更新）・`sales_engine.process_fan_message()` 連携・文字数比例既読遅延（1〜5分）＋タイピング演出（10〜30秒）・ローカル `ppv_vault\<filename>` アップロード＆ロック価格送信・`ppv_transactions` 記録を実装。
7. **未開封FOMOリマインド＆Step 2追撃ワーカー (`app/services/followup_worker.py`)**
   - `FollowupWorker`（シングルトン `followup_worker`）:
     - 送信後6〜12時間経過・`is_unlocked == 0`・`reminder_sent == 0` のレコードを抽出し、緊急停止・ミュート・省エネ客を除外の上、希少性（FOMO）を煽る日英追撃メッセージを1回だけ送信キューへ投入し `reminder_sent = 1` に更新（二重送信防止）。
     - Step 1 PPV開封（`is_unlocked == 1`）から30分〜6時間経過した熱量の高い顧客に対し、未購入の Step 2 素材を動的価格で自動追撃提案。

### ✅ 完了済み：実機 Fansly DOM構造検査に基づくセレクタ＆DM取得・PPVロック送信完全一致改修
8. **実機 Angular 19 DOM完全対応 (`app/services/fansly_dm_bot.py`, `app/routes/dashboard.py`)**
   - `profiles/` のログイン済みセッションを用いたヘッドフル実機検査に基づき、`FANSLY_SELECTORS` およびDOM解析・送信ロジックを全面改修：
     - スレッド一覧（`.message-list a[href^='/messages/']` / `app-messaging-group`）、完全ハンドル抽出（`a.username-wrapper` の `href`）、未読バッジ（`.badge-container .badge`）＋直近テストDM取りこぼし防止検知。
     - 会話画面（`app-messages-conversation-route` / `app-group-message`）での送信者判定（`:not(.my-message)` vs `.my-message`）、連続ファンメッセージ結合、PPV購入済み検知（`.purchased-content .purchased-avatar:not(.not-purchased)`）、DM送信制限（`.no-dm-allowed`）検知。
     - 最新 Media Upload v2 モーダル（`app-account-media-upload-v2` → `Subscribe` 条件解除 → `Pay` → `app-balance-input-modal` の `input[aria-label='Amount']` → `Confirm` → `button.upload-primary` `Attach`）によるPPVロック価格設定送信。
     - ローテーションログ出力（`AI_DM_Sales/logs/fansly_dm_bot.log`）および管理画面からの手動介入送信時のボット送信キュー連動を追加。

---

### ✅ 完了済み：Phase 2（v1.1 自己進化・LTV最大化拡張 一括実装）
9. **自己進化バッチ分析 ＆ A/Bテスト自動昇格エンジン (`app/services/optimizer_batch.py`, `app/core/database.py`)**
   - `run_playbook_optimization(char_id)`: `empire_core.db` から勝ちスレッド（PPV開封・課金）と負けスレッド（離脱・2回連続スルー・省エネ降格）を抽出し、Ollamaネイティブ `/api/generate` + `"think": False` で比較分析を実行。英語営業鉄則プロンプト（`rules_text`）＋日本語解説（`rules_ja`）＋分析サマリ（`analysis_summary_ja`）を `sales_playbooks` へ `ab_group = 'B'` として保存。
   - 新規ファン登録時の `ab_group`（`A` / `B`）50:50均等自動振り分け、`SalesEngine` へのアクティブ `sales_playbooks.rules_text` 自動注入、および `evaluate_and_promote_ab_test(char_id)` によるCVR集計・自動昇格（各群10件以上かつ `B > A` で新 `A` へ自動昇格）を実装。
10. **チップ（投げ銭）誘導フック (`app/services/sales_engine.py`, `app/services/fansly_dm_bot.py`)**
    - 強い称賛・甘え・特別リクエスト（「もっと見たい」「特別にやってほしい」等）およびPPV購入直後の感謝フェーズ（`5_Cooldown`）を検知する `detect_tip_opportunity()` を実装。キャラ口調を崩さず自然に `$5〜$10` のチップを誘導し、`ppv_transactions` に `tx_type = 'tip'` として自動記録。
11. **休眠ファン掘り起こし個別DMバッチ (`app/services/dormant_revival_worker.py`)**
    - `run_dormant_revival(char_id, inactive_days=7, max_targets=10)` および定期実行デーモン `DormantRevivalWorker` を実装。`total_spent_usd > 0`・最終連絡から `inactive_days` 日以上経過・非エスカレーション・非ミュート・非省エネの休眠優良ファンを抽出し、日本語顧客カルテ（`psych_profile_json`）と `dm_persona.json` から非定型の個別再会DM（日英）を生成して `dm_messages` 保存・`last_contact_at` 更新（二重送信防止）・`bot_manager.enqueue_outbound_message()` 投入を実行。
12. **管理画面UI・API統合 (`app/routes/dashboard.py`, `app/templates/index.html`, `run_dm_sales.py`)**
    - `GET /api/evolution/status`, `POST /api/evolution/optimize`, `POST /api/evolution/revive_dormant` を追加。
    - タブ3（「3. キャラ・プラン設定」）および `[⚙️ 設定]` モーダル内に、A/Bテスト状況・CVRスコア比較・即時分析ボタン・休眠ファン候補人数＆即時掘り起こし実行ボタンをダークモードUIで統合。

---

## 2. 起動方法・検証コマンド
- **起動**:
  ```powershell
  python C:\Fansly_Empire\AI_DM_Sales\run_dm_sales.py
  ```
  ブラウザで `http://127.0.0.1:5051` を開く。
- **Phase 2〜Phase 5 (v1.4) 統合テスト＆全ファイル構文チェック**:
  ```powershell
  python C:\Fansly_Empire\AI_DM_Sales\tests\test_phase5_sell_delay_ppv_and_ui.py
  python C:\Fansly_Empire\AI_DM_Sales\tests\test_phase4_vision_ui_and_delay.py
  python C:\Fansly_Empire\AI_DM_Sales\tests\test_phase3_ux_and_evolution.py
  python C:\Fansly_Empire\AI_DM_Sales\tests\test_phase2_integration.py
  ```

---

### ✅ 完了済み：v1.4 S.E.L.L.厳格化・受信文字数比例待機・PPV/チップ誤判定根絶・手動PPVダイナミック送信・PPV武器庫2カラムUI刷新（2026-11-18 一括実装）
22. **S.E.L.L. 営業フレームワークの厳格遵守 ＆ 催促禁止 ＆ 絵文字パレット拡充 (`sales_engine.py`, `character_loader.py`, `dm_persona.json`)**
    - 新規未課金ファンの会話初期（1〜2往復目）では `1_Icebreak` / `2_Discovery` を優先し、PPV提案・クロージングへの飛び級をブロックするハードガードを実装。`5_Cooldown` での急かし・催促を厳格禁止し、16種の絵文字パレットと直前使用絵文字の連続重複回避指示を導入。
23. **受信メッセージ分量（文字数）に応じた返信待機時間の比例計算 ＆ 基準設定UI (`database.py`, `fansly_dm_bot.py`, `vault_routes.py`, `index.html`)**
    - `short_msg_chars_threshold`（初期20文字）〜 `long_msg_chars_threshold`（初期150文字）による線形補間＋±15%ゆらぎ＋`[min, max]` クランプアルゴリズム `_compute_human_reply_delay_sec()` を実装し、算出根拠を `decision_log_json` および設定モーダルUIへ反映。
24. **PPV「購入済み」誤判定 ＆ チップ「誘導済み」誤記録の徹底修正 (`fansly_dm_bot.py`, `sales_engine.py`, `database.py`)**
    - 最新添付バブル限定・ロック要素完全消滅・提案後15秒経過ガードによりPPV未購入時の誤ロック解除を根絶。最終英文に明確なチップ要求表現が含まれ実際に送信された場合のみ `tx_type='tip'` を記録するよう厳格化し、既存DBの誤判定レコードも自動修復。
25. **モニタリング画面からの「手動PPV送信（素材選択 ＋ ミニサムネ ＋ ダイナミックプライシング選択）」 (`dashboard.py`, `fansly_dm_bot.py`, `index.html`)**
    - 顧客カルテの予算層に連動する `GET /api/monitoring/ppv_candidates` を新設し、プリセット選択・カスタム金額入力・Playwrightボット送信キュー投入を一貫実装。
26. **PPV武器庫一覧の「左右黒帯ゼロ・左サムネ＋右編集」コンパクト2カラムカード ＆ 完全レスポンシブ刷新 (`index.html`)**
    - 横並び2カラム（`196px + 1fr`・左枠 `object-fit: cover`）で左右の黒帯を消滅させ、大画面では `minmax(560px, 1fr)` の複数列、狭幅（`max-width: 680px`）では縦積みへ自動切替。

---

### ✅ 完了済み：v1.3 Vision自動タグ・売り込みメモ生成・PPV吹き出し＆ホバー詳細・検討ログ統一・返信待機時間設定（2026-11-18 一括実装）
17. **PPV素材追加・編集時のローカルVision LLMによる「日本語タグ ＆ 売り込みメモ」自動生成 (`llm_client.py`, `vault_routes.py`, `database.py`, `index.html`)**
    - ローカルOllamaのVision対応モデル（`qwen2.5vl:7b` / `qwen3-vl` / `llava` 等）を優先自動検出し、画像または動画（OpenCV/FFmpegで抽出した代表フレーム）を視覚解析。自然な日本語の「属性タグ（`tags_json`）」と「売り込みメモ・フック文（`description_memo` / `pitch_note`）」を `ppv_vault` DBおよびUIへ自動反映。既存カードにも **`[✨ AIでタグ・メモを自動生成]`** 再解析ボタンを追加。
18. **営業シミュレーターへの「AI検討ログ表示」追加 ＆ 本番パイプライン完全統一 (`dashboard.py`, `sales_engine.py`, `index.html`)**
    - シミュレーター（タブ4）が本番と完全同一の推論パイプライン（カルテ更新根拠＋戦略検討ログ `decision_log`＋A/Bプレイブック＋好みタグ連動PPV選定）を使用するように統一し、右ペインに展開式アコーディオン **`🧠 AIカルテ根拠・返答検討ログを見る`** を実装。
19. **トーク画面への「送信済みPPVメディア（画像/動画）表示」＆ マウスオーバー詳細ツールチップ (`database.py`, `fansly_dm_bot.py`, `dashboard.py`, `index.html`)**
    - モニタリング画面およびシミュレーター画面の吹き出し内に、送信したPPVの画像／動画プレイヤー（ロック価格バッジ付き）を描画。マウスオーバー時のみ浮かび上がるフローティング情報カードで「ファイル名・段階・価格・タグ・売り込みメモ・開封状況（未購入/購入済）」を確認可能に。手動介入ボックスからの任意PPV添付送信にも対応。
20. **PPV提案ファイル名クリックによる該当素材カードへの直接ジャンプ ＆ 検討ログのスクロール/リサイズ対応 (`index.html`)**
    - 右下の「💎 PPV提案・開封履歴」等のファイル名リンクをクリックすると即座にタブ2へ切り替わり、該当PPVカードまで自動スクロール＋ハイライト強調表示。また検討ログアコーディオンに `max-height: 360px`・`overflow-y: auto`・`resize: vertical` を設定。
21. **「AI返信待機時間（ランダム遅延範囲）」を設定モーダルから変更可能に (`database.py`, `character_loader.py`, `fansly_dm_bot.py`, `vault_routes.py`, `index.html`)**
    - `[⚙️ 設定]` モーダル内に「⏱️ AI返信待機時間（人間味ランダム遅延範囲）」（最小秒数 `min_reply_delay_sec` 〜 最大秒数 `max_reply_delay_sec`、デフォルト `60〜300秒`）を追加し、`system_settings` DBおよび `settings.json` へ保存してボットの新着DM返信カウントダウン秒数へ即時反映。

---

### ✅ 完了済み：v1.2 運用性・視認性・自己進化精度アップグレード（2026-11-18 一括実装）
13. **稼働スイッチ `OFF` 時の常時監視継続 ＆ ウィンドウ「×」終了時のクリーン停止 (`fansly_dm_bot.py`, `dashboard.py`, `run_dm_sales.py`)**
    - ヘッダーの `[▶ 全キャラ一括稼働]` / `[⏹ 全停止]` および個別トグルの表記・デザインはそのまま維持し、スイッチ `OFF` 時も Playwright による DM巡回・新着取得・DB保存・日本語翻訳・AI顧客カルテ更新を常時継続（「AI自動返信・PPV送信・自動追撃」のみ停止し手動返信は常時可能）。ブラウザウィンドウ「×」閉鎖時は `stop_all()` で全プロセスをクリーン終了。
14. **返信待機カウントダウン ＆ 手動切替・10分未返信放置アラート・自動復帰 (`fansly_dm_bot.py`, `database.py`, `dashboard.py`, `index.html`)**
    - 右ペイン上部に `⏳ 自動返信まで残り XX秒（送信予定 HH:MM:SS）` をリアルタイム表示。`[⏭️ スキップして手動返信へ]` で待機キャンセル＆手動保留切替＋介入欄へ自動フォーカス、`[🤖 やっぱりAI自動返信に戻す]` でカウントダウン即時再セット。10分以上未返信放置時は左右両ペインに `⚠️ 手動返信待ち（未返信放置）` アラートを表示。
15. **AI顧客カルテ根拠 ＆ 返答検討ログ (`decision_log_json`) の可視化と自己進化連携 (`profiler_service.py`, `sales_engine.py`, `optimizer_batch.py`, `index.html`)**
    - カルテ更新根拠と営業戦略検討過程を構造化保存し、右ペイン「リアルタイムAI顧客カルテ」直下の `🧠 AIカルテ根拠・返答検討ログを見る` アコーディオンで可視化。さらに `run_playbook_optimization()` で会話履歴と共に Ollama へ渡してAI自身の判断クセまで改善した `Playbook B` を生成。
16. **モニタリング4秒自動ポーリング・PPV動画Range配信＆3段階価格連動＆ライトボックス・レスポンシブ改善・アプリ内運用マニュアルモーダル (`vault_routes.py`, `dashboard.py`, `index.html`)**
    - オペレーター入力保護付きの4秒軽量差分ポーリング、`<video preload="metadata" src="...#t=0.1">` ＋ HTTP 206 `Range` 対応による動画真っ黒表示解消、`object-fit: contain` 全体表示、3段階レベル（導入/本命/超秘蔵）変更時の価格自動連動、音声付き拡大ライトボックス、狭幅レスポンシブ調整、および `[⚙️ 設定]` モーダル最下部の `[📖 運用マニュアルを開く]`（キーワード検索付き）を実装。

---

## 3. 今後の連携展望
1. **`AI_Analytics_Console`（アプリ③・将来開発）との連携**:
   - `empire_core.db` に蓄積される `sales_playbooks`（A/B別CVR推移）および `ppv_transactions`（`ppv` / `tip` 別売上）を横断BIダッシュボードから可視化可能。
