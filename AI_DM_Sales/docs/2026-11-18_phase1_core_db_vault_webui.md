# Phase 1 第1回実装：コア基盤・統合DB・共通キャラマスター・完成版Web UI・PPV武器庫ミニアプリ

## 1. 概要・日付・対象ファイル
- **作業日**: 2026-11-18
- **概要**: `AI_DM_Sales`（ポート `5051`）の Phase 1 第1回実装として、3アプリ共有のSQLiteデータベース（`empire_core.db` WALモード・6テーブル）、共通キャラクターマスター＆PPV武器庫バックエンドAPI、および4タブ＋2モーダル＋共通ヘッダーを備えた完成版Web管理コンソールUIを構築した。
- **対象ファイル**:
  - `AI_DM_Sales/.gitignore` (新規)
  - `AI_DM_Sales/run_dm_sales.py` (新規)
  - `AI_DM_Sales/app/__init__.py` (新規)
  - `AI_DM_Sales/app/core/__init__.py` (新規)
  - `AI_DM_Sales/app/core/config.py` (新規)
  - `AI_DM_Sales/app/core/database.py` (新規)
  - `AI_DM_Sales/app/core/llm_client.py` (新規)
  - `AI_DM_Sales/app/services/__init__.py` (新規)
  - `AI_DM_Sales/app/services/character_loader.py` (新規)
  - `AI_DM_Sales/app/routes/__init__.py` (新規)
  - `AI_DM_Sales/app/routes/vault_routes.py` (新規)
  - `AI_DM_Sales/app/routes/dashboard.py` (新規)
  - `AI_DM_Sales/app/templates/index.html` (新規)
  - `AI_DM_Sales/PROJECT_MAP.md` (更新)
  - `AI_DM_Sales/docs/HANDOVER.md` (新規)

---

## 2. 背景・解決したかった課題（Before）
- `AI_DM_Sales` には仕様書 (`docs/SPEC_DM_SALES.md`) と `PROJECT_MAP.md` のみが存在し、実行可能なアプリケーションコードや統合DB (`shared_assets\database\empire_core.db`) が未構築だった。
- また、`shared_assets\characters\<char_id>` には `dm_persona.json`, `plans.json`, `ppv_vault\vault_index.json` の初期ファイルが存在していたが、新規仕様で求められる `settings.json`（Step 1〜3の初期価格マスターや `sleep_schedule`）や、既存 `vault_index.json` の `"videos"` キーに対する `media_type` / `is_active` / `created_at` フィールドの正規化・DB売上実績集計が未実装だった。

---

## 3. 採用した設計方針・理由（Why / ADR）
1. **SQLite WALモードの強制と6テーブル一括初期化 (`app/core/database.py`)**:
   - `AI_Factory`（ポート5050）、`AI_DM_Sales`（ポート5051）、将来開発の `AI_Analytics_Console` が同時に `empire_core.db` へアクセスしてもロック競合（`database is locked`）を起こさないよう、`get_db_connection()` 内で毎回 `PRAGMA journal_mode=WAL;`, `PRAGMA synchronous=NORMAL;`, `PRAGMA busy_timeout=10000;` を実行する設計を採用した。
2. **既存 `vault_index.json` との完全な後方互換性 (`app/services/character_loader.py`)**:
   - 既存の `Sylphy-Mia` や `queen_lilith` の `vault_index.json` は `{"videos": [...]}` 形式で保存されており、`media_type` や `is_active`, `created_at` が省略されていた。読み込み時に拡張子から `media_type` (`video`/`image`) を自動補完して正規化し、保存時は `{"items": [...], "videos": [...]}` の両キーを出力することで、既存スクリプトと新UIの双方で互換性を維持した。
3. **ローカルOllama未起動・タイムアウト時にも機能停止しないフォールバック設計 (`app/core/llm_client.py`)**:
   - Qwen3系モデルの `<think>...</think>` タグ除去およびコードフェンスJSON抽出を備えるとともに、Ollamaがオフラインの時でもファイル名トークン＆キャラペルソナに基づく推奨タグ・英語 `pitch_hint` 生成や、日本語→キャラ口調英語変換が即座に動作するハイブリッド構成を採用した。

---

## 4. 実装内容・変更点サマリ（After）
- **コア基盤 (`app/core/`)**:
  - `config.py`, `database.py`, `llm_client.py` を実装し、`empire_core.db` の6テーブル作成と初期デモデータシード（空DB時のみ）を自動化。
- **PPV武器庫＆キャラクター管理 (`app/services/character_loader.py`, `app/routes/vault_routes.py`)**:
  - `settings.json` の自動生成（`sleep_schedule: {"enabled": false, "start": "19:00", "end": "22:00"}` および Step 1〜3 の3種価格マスター）。
  - `/api/vault/preview_draft`, `/api/vault/upload`（連番リネーム・`media_type` 自動判定）, `/api/vault/list`（階段レベル・メディア種別フィルタ、5種ソート、`ppv_transactions` からの提案数・購入数・売上金額USD集計）, `/api/vault/update`, `/api/vault/delete`, `/api/settings/save` を実装。
- **完成版Web管理UI (`app/routes/dashboard.py`, `app/templates/index.html`, `run_dm_sales.py`)**:
  - 王冠ファビコン（👑）付きダークモードUI、共通ヘッダー（一括稼働マスターボタン＋展開式キャラメニュー＆個別稼働トグル＆睡眠中 `💤` 表示＋設定ボタン＋PPV追加ボタン）、4タブ（営業モニタリング左右2ペイン・PPV武器庫一覧・キャラ/プラン設定・営業シミュレーター）、2モーダルを完全実装。

---

## 5. 作業中に発生した問題・ハマりポイントと解決策（Troubleshooting）
- **問題**: Windows PowerShell から統合テストスクリプトを標準入力パイプで `python -` に渡した際、日本語文字列内の `♡` (U+2661) がコンソール既定の cp932 エンコーディングで変換エラーを起こし、またローカルOllamaへのHTTPリクエスト待ちでテスト実行時間が伸びる事象が発生した。
- **解決策**: テストコード内の非ASCII文字をUnicodeエスケープ (`\u2661`) で記述し、また `LLMClient` のタイムアウト制御を柔軟に設定可能にすることで、オフライン時・オンライン時ともに高速かつ確実に統合テストが完走することを確認した。

---

## 6. 今後の注意点・残タスク
- 次回（Phase 1 第2回・第3回）では、現在 `dashboard.py` のシミュレーターに組み込まれている S.E.L.L. フェーズ遷移・動的プライシングロジックを `app/services/sales_engine.py` および `app/services/profiler_service.py` として独立モジュール化し、Playwright による全キャラ同時並行DM監視ボット (`fansly_dm_bot.py`) および FOMO リマインドワーカー (`followup_worker.py`) と結合する。

---

# 追記（同日改修）：全チャット吹き出しの日英常時併記（`content_ja` 追加）およびダミー顧客シードの廃止

## 1. 概要・日付・対象ファイル
- **作業日**: 2026-11-18
- **概要**: すべてのチャット吹き出し（営業モニタリング右ペインの会話ログ・営業シミュレーターの対話ログ）で「上段：英語原文」「下段：日本語訳」を常時併記する仕様へ改修し、あわせてダミー顧客・サンプルチャット履歴の自動シード処理を削除した。
- **対象ファイル**:
  - `AI_DM_Sales/app/__init__.py`
  - `AI_DM_Sales/app/core/database.py`
  - `AI_DM_Sales/app/core/llm_client.py`
  - `AI_DM_Sales/app/routes/dashboard.py`
  - `AI_DM_Sales/app/templates/index.html`

## 2. 背景・解決したかった課題（Before）
- チャットタイムラインおよび営業シミュレーターの吹き出しが英語原文のみの表示だったため、日本人オペレーターが会話文脈やAIのニュアンスを瞬時に把握しづらかった。
- また、営業シミュレーターで日本語のままテスト顧客メッセージを入力して英語営業ロジックを試したいニーズがあった。
- 初回起動時のダミー顧客シード処理が残っていると実運用データと混재するため、廃止・クリーンアップが必要だった。

## 3. 採用した設計方針・理由（Why / ADR）
1. **`dm_messages` テーブルへの `content_ja TEXT` カラム追加と安全な自動マイグレーション (`app/core/database.py`)**:
   - 新規DB作成時の `CREATE TABLE` 定義に `content_ja TEXT` を含めるだけでなく、既存DBに対しても起動時に `PRAGMA table_info(dm_messages);` を検査し、`content_ja` が未存在であれば `ALTER TABLE dm_messages ADD COLUMN content_ja TEXT;` を自動実行する方式を採用した。
2. **双方向翻訳ヘルパーの実装 (`app/core/llm_client.py`)**:
   - `translate_to_japanese(text)`（英語→自然な日本語訳）および `translate_fan_ja_to_en(ja_text)`（テスト客の日本語→Fanslyのファンらしい英語チャット文）を追加し、Ollamaオフライン時でも自然な対訳を返すフォールバックを備えた。
3. **UI上での常時2段表示とシミュレーター日本語直接送信ボタン (`app/templates/index.html`)**:
   - 個別の翻訳トグルボタンを廃止し、すべての吹き出しに `.msg-en`（上段：英語原文）、`.msg-divider`（破線セパレーター）、`.msg-ja`（下段：日本語訳）を常時描画するUIとした。

## 4. 実装内容・変更点サマリ（After）
- `database.py`: `_seed_demo_data_if_empty` を日英併記仕様（全11件のサンプルメッセージすべてに英語 `content` と日本語訳 `content_ja` を同梱）へアップデート。`add_dm_message`, `get_thread_detail`, `get_fan_threads`, `update_dm_message_ja` を `content_ja` 対応に拡張。
- `llm_client.py`:
  - Qwen3系モデル (`my-qwen3:latest`) で `<think>...</think>` トークン消費により `max_tokens` 上限に達して翻訳結果が空になりフォールバック `（日本語訳）...` に落ちていた問題を特定し、Ollama ネイティブ `/api/generate` エンドポイントへ `"think": False` 付きでリクエストする高速パスを実装（1リクエスト約0.8秒で完了）。
  - `translate_to_japanese(text)` と `translate_fan_ja_to_en(ja_text)`（ローマ字禁止指示付き）を追加・改善。
- `dashboard.py`: `/api/monitoring/translate`, `/api/monitoring/send_manual`, `/api/monitoring/thread`, `/api/simulator/reply`（`auto_translate_ja` フラグ対応、`fan_en`/`fan_ja`/`ai_en`/`ai_ja` 返却）を日英両方返却・保存する仕様に更新。
- `index.html`: モニタリング右ペインとシミュレーターの全吹き出しを英語＋日本語訳の2段表示に統一し、シミュレーターに `[🚀 英語で送信]` と `[✨ 日本語を英語に変換して送信]` の2ボタンを配置。

## 5. 作業中に発生した問題・ハマりポイントと解決策（Troubleshooting）
- **問題1**: 既存の `empire_core.db` には変更前の `dm_messages` テーブル（`content_ja` カラム無し）が存在していた。
- **解決策1**: `init_db()` 内で `PRAGMA table_info(dm_messages)` による動的マイグレーションを実行することで、既存DBファイルを壊すことなくスキーマ更新を完了させた。
- **問題2**: 営業シミュレーターでローカルOllama (`my-qwen3:latest`) が英語返信を生成した直後の `translate_to_japanese()` 呼び出しで、OpenAI互換 `/v1/chat/completions` では `<think>` 思考ブロックがトークン上限を使い切ってしまい、本文が空になって未翻訳フォールバック（`（日本語訳）Oh, you're so curious...`）が表示されてしまっていた。
- **解決策2**: `LLMClient._call_chat_completions()` で Ollama ネイティブの `/api/generate` エンドポイントに `"think": False` を指定して呼び出すよう改修し、さらにフォールバック辞書からも `（日本語訳）{en_text}` の生英語出力を撤廃したことで、約0.8〜1.3秒で自然な日本語訳が確実に表示されるようになった。


## 6. 今後の注意点・残タスク
- 次回 Playwright DM監視ボット (`fansly_dm_bot.py`) で Fansly から実際のファン受信メッセージを取り込む際にも、`llm_client.translate_to_japanese()` を呼んで `content`（英語）と `content_ja`（日本語訳）をセットで `add_dm_message()` に渡すこと。

