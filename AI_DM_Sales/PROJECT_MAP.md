# PROJECT_MAP.md - AI_DM_Sales
> Cline / OpenCode 開発用インデックス（コンテキスト節約のため、ファイル探索前に必ず本書を参照すること）

## 1. システム概要・環境変数
- **アプリ役割**: Fansly 全自動DM営業＆自己進化エンジン（ポート `5051`）
- **親ディレクトリ**: `C:\Fansly_Empire`
- **共通資産パス (`SHARED_ASSETS_DIR`)**: `C:\Fansly_Empire\shared_assets`（環境変数で上書き可）
- **統合SQLite DB (`DB_PATH`)**: `C:\Fansly_Empire\shared_assets\database\empire_core.db`（WALモード有効・`AI_Factory` / `AI_Analytics_Console` と共有）
- **ローカルLLM (`OLLAMA_BASE_URL`)**: `http://127.0.0.1:11434/v1`（Qwen3系ローカルモデル使用・NSFW対応）

## 2. ディレクトリ・ファイル構成

### ルート直下
- `run_dm_sales.py`: [実装済] Flask Web管理画面（ポート5051）およびバックグラウンド監視・追撃ワーカーの起動エントリーポイント
- `PROJECT_MAP.md`: 本ファイル（開発用ファイルインデックス）
- `.gitignore`: [実装済] 顧客個人情報・DB・メディア・認証情報の除外設定

### ログ (`logs/`)
- `logs/`: アプリケーション実行ログ出力ディレクトリ

### ドキュメント (`docs/`)
- `docs/SPEC_DM_SALES.md`: システム全体仕様書 v1.0（DB設計・営業ロジック・UI詳細仕様）
- `docs/HANDOVER.md`: [実装済] 作業引き継ぎログ・実装進捗管理（Phase 1 & Phase 2 全完了）
- `docs/2026-11-18_phase1_core_db_vault_webui.md`: [実装済] Phase 1 第1回実装の振り返り・設計判断レポート
- `docs/2026-11-18_phase1_sales_engine_and_profiler.md`: [実装済] Phase 1 第2回実装（S.E.L.L.営業エンジン・リアルタイム顧客カルテ抽出・シミュレーター直結）の振り返りレポート
- `docs/2026-11-18_phase1_playwright_bot_and_followup_worker.md`: [実装済] Phase 1 第3回実装（Playwright全キャラ同時並行DM監視ボット・手動ログイン連携・未開封FOMOリマインド＆Step 2追撃ワーカー）の振り返りレポート
- `docs/2026-11-18_fansly_dom_selectors_live_fix.md`: [実装済] 実機Fansly DOM構造検査に基づく `FANSLY_SELECTORS`・未読DM取得・Media Upload v2 PPV価格ロック送信改修レポート
- `docs/2026-11-18_phase2_self_evolution_and_dormant_revival.md`: [実装済] Phase 2 一括実装（自己進化バッチ分析＆A/B自動昇格・チップ誘導フック・休眠ファン掘り起こし個別DMワーカー・管理画面統合）の振り返りレポート
- `docs/2026-11-18_ux_monitoring_vault_and_decision_log_upgrade.md`: [実装済] v1.2 UX・監視継続・待機カウントダウン手動切替・AI検討ログ可視化＆自己進化連携・PPV動画Range配信/ライトボックス・運用マニュアルモーダル改修レポート
- `docs/2026-11-18_vision_ppv_tagging_bubble_preview_and_delay_settings.md`: [実装済] v1.3 ローカルVision LLMによるPPV日本語タグ＆売り込みメモ自動生成・シミュレーター検討ログ完全統一・トーク吹き出しPPVメディア＆ホバー詳細カード・PPV直接ジャンプ・検討ログスクロール/リサイズ・返信待機時間範囲設定レポート
- `docs/2026-11-18_sell_guard_proportional_delay_ppv_fix_and_vault_2col.md`: [実装済] v1.4 S.E.L.L.厳格遵守＆絵文字多様化・受信文字数比例の返信待機アルゴリズム＆基準設定UI・PPV購入済/チップ誘導誤判定の徹底修正・手動PPV＋ダイナミックプライシング送信・PPV武器庫2カラム黒帯ゼロUI刷新レポート
- `docs/USER_MANUAL.md`: [実装済] 人間用運用・操作マニュアル（設定モーダル最下部からアプリ内検索付き閲覧可能 / v1.4対応済）

### コア基盤 (`app/`, `app/core/`)
- `app/__init__.py`: [実装済] Flask アプリファクトリ (`create_app`)、ディレクトリ初期化・DB初期化（ダミーシード完全廃止）・Blueprint登録
- `app/core/config.py`: [実装済] パス解決（`SHARED_ASSETS_DIR`, `CHARACTERS_ROOT`, `DB_PATH`, `PROFILES_DIR`）、Ollama設定、ポート番号管理
- `app/core/database.py`: [実装済] `empire_core.db` 初期化（8テーブル管理：`daily_account_metrics`, `posts_log`, `fans`, `dm_messages` [`content` + `content_ja` + `decision_log_json` + `ppv_id` / `ppv_price_usd` / `tx_id` 自動マイグレーション], `ppv_transactions`, `sales_playbooks`, `ppv_vault` [`tags_json`, `description_memo`, `pitch_note` 同期], `system_settings` [`min_reply_delay_sec` / `max_reply_delay_sec`]）、`PRAGMA journal_mode=WAL;` 設定、メッセージへの送信済みPPV詳細（`ppv_attachment`）解決、A/B均等振り分け、手動保留＆10分未返信放置アラート判定、AI検討プロセスログ追記、プレイブック管理・CVR集計・自動昇格、休眠ファン抽出
- `app/core/llm_client.py`: [実装済] ローカルOllama APIラッパー（ネイティブ `/api/generate` + `"think": False` 高速推論、ローカルVisionモデル［`qwen2.5vl` / `qwen3-vl` / `llava` 等］自動検出＆自動プル `detect_or_ensure_vision_model`、OpenCV/FFmpeg動画代表フレーム抽出 `extract_media_frame_base64`、画像/動画からの日本語タグ＆売り込みメモ自動生成 `analyze_ppv_media_with_vision`、営業トーク生成、顧客心理カルテ抽出、日英翻訳）


### ビジネスロジック・ワーカー (`app/services/`)
- `app/services/character_loader.py`: [実装済] `shared_assets\characters\<char_id>` 配下の `dm_persona.json`, `plans.json`, `settings.json`（初期値: 睡眠 `OFF` / `19:00-22:00`、返信待機 `60〜300秒`）, `ppv_vault\vault_index.json`（日本語 `tags` / `description_memo` ＆ `ppv_vault` DB自動同期）のCRUD管理
- `app/services/sales_engine.py`: [実装済] `SalesEngine` クラス：危険ワード＆連続スルー緊急停止 (`is_escalated = 1`)、タダ乗り客省エネ降格 (`6_Low_Priority`)、S.E.L.L.営業フェーズ遷移、顧客カルテの好みと日本語PPVタグ・売り込みメモ照合による階段式PPV選定、動的プライシング、A/Bルール自動注入、チップ誘導フック、各ターンのカルテ根拠＋戦略検討過程の構造化生成 (`decision_log_json`)
- `app/services/profiler_service.py`: [実装済] 会話ログからのリアルタイム顧客カルテ（`psych_profile_json`）および各項目の日本語抽出根拠（`last_profile_reasoning`: 好み・心理タイプ・予算感・有効フックの根拠）抽出・マージ・DB更新 (`update_fan_psych_profile`)
- `app/services/fansly_dm_bot.py`: [実装済] Playwright全キャラ同時並行DM監視・送信ボット（稼働スイッチ `OFF` 時もDM巡回・DB保存・日本語翻訳・カルテ更新を常時継続、`system_settings` / `settings.json` の最小〜最大待機秒数に基づくランダム待機カウントダウン `_pending_replies`、AI返信時のPPVメディアDB紐付け、手動保留スキップ `skip_to_manual`、AI自動返信復帰 `resume_auto_reply`、ウィンドウ閉鎖時の `stop_all` クリーン終了、手動ログイン `launch_manual_login`）
- `app/services/followup_worker.py`: [実装済] バックグラウンド定期実行ワーカー（`FollowupWorker`：稼働 `ON` かつ非保留スレッドに対する未開封PPVへの6〜12時間後FOMOリマインド、およびStep 1購入30分〜6時間後のStep 2自動追撃提案・PPVメディア紐付け記録）
- `app/services/optimizer_batch.py`: [実装済] 自己進化バッチ分析＆A/Bテスト自動昇格エンジン（`extract_win_loss_threads`, `run_playbook_optimization` [会話履歴＋`decision_log_json` のAI思考クセ・カルテ解釈を統合比較分析して新ルールBを生成], `evaluate_and_promote_ab_test`, `get_evolution_status`）
- `app/services/dormant_revival_worker.py`: [実装済] 休眠ファン掘り起こし個別DMバッチ＆定期実行ワーカー（稼働 `ON` キャラの7日以上未連絡課金ファンへ顧客カルテ連動の再会DMを生成・送信キュー投入）

### Web API・UI (`app/routes/`, `app/templates/`, `tests/`)
- `app/routes/__init__.py`: [実装済] Blueprint (`dashboard_bp`, `vault_bp`) 登録
- `app/routes/dashboard.py`: [実装済] メイン画面、成績サマリーAPI、4秒リアルタイム差分ポーリングAPI (`GET /api/monitoring/poll`)、待機スキップ＆自動復帰API、手動PPV送信向けダイナミックプライシング候補API (`GET /api/monitoring/ppv_candidates`)、2ペインスレッド/PPV＆カスタム価格添付対応手動介入送信 (`POST /api/monitoring/send_manual`)/アラート解除/ミュートAPI、本番完全共通パイプライン営業シミュレーターAPI (`POST /api/simulator/reply`)、ボット稼働制御＆クリーン停止API、運用マニュアル取得API、自己進化＆休眠掘り起こしAPI
- `app/routes/vault_routes.py`: [実装済] PPV武器庫API（`/api/vault/analyze_vision`, `/api/vault/preview_draft`, `/api/vault/upload`, `/api/vault/list`, `/api/vault/update`, `/api/vault/delete`, `/api/settings/get`, `/api/settings/save` [`min_reply_delay_sec` / `max_reply_delay_sec` / `short_msg_chars_threshold` / `long_msg_chars_threshold` バリデーション対応], `/api/vault/media/<char_id>/<filename>` [HTTP `Range` `206 Partial Content` 配信対応]）
- `app/templates/index.html`: [実装済] ダークモードWeb管理コンソール（4タブ［1.営業モニタリング（吹き出し内送信済みPPVサムネイル＆ホバー詳細ツールチップ・手動PPV選択＋ミニサムネ＋ダイナミック価格プリセット送信・PPV直接ジャンプ・縦スクロール＆リサイズ対応検討ログ） / 2.PPV武器庫一覧（左右黒帯ゼロ「左サムネ＋右編集」2カラムコンパクトカード・`[✨ AIでタグ・メモを自動生成]`・完全レスポンシブ） / 3.キャラ・プラン設定＆自己進化 / 4.営業シミュレーター］、4モーダル［PPV追加 / システム設定（文字数比例返信待機基準・価格・睡眠・マニュアル起動） / 拡大ライトボックス / 検索付き運用マニュアル］）
- `tests/test_phase2_integration.py`: [実装済] Phase 2 統合テストスクリプト
- `tests/test_phase3_ux_and_evolution.py`: [実装済] v1.2 統合テストスクリプト
- `tests/test_phase4_vision_ui_and_delay.py`: [実装済] v1.3 統合テストスクリプト
- `tests/test_phase5_sell_delay_ppv_and_ui.py`: [実装済] v1.4 S.E.L.L.厳格ガード・文字数比例待機・誤判定防止・手動PPV送信・2カラムVault UI統合テスト＆全ファイル `py_compile` 検証スクリプト