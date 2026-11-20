# Phase 1 第2回実装：S.E.L.L. 営業エンジン本体・リアルタイム顧客カルテ抽出・営業シミュレーター直結

## 1. 概要・日付・対象ファイル
- **作業日**: 2026-11-18
- **概要**: `AI_DM_Sales`（ポート `5051`）の Phase 1 第2回実装として、リアルタイム顧客カルテ抽出サービス（`profiler_service.py`）および S.E.L.L. 営業エンジン本体（`sales_engine.py`）を構築し、営業シミュレーター（`dashboard.py`, `index.html`）を本番エンジンへ直結した。すべてのローカルOllama推論は `LLMClient` のネイティブ `/api/generate` + `"think": False` パス経由で高速実行される。
- **対象ファイル**:
  - `AI_DM_Sales/app/services/profiler_service.py` (新規)
  - `AI_DM_Sales/app/services/sales_engine.py` (新規)
  - `AI_DM_Sales/app/core/database.py` (更新)
  - `AI_DM_Sales/app/routes/dashboard.py` (更新)
  - `AI_DM_Sales/app/templates/index.html` (更新)
  - `AI_DM_Sales/PROJECT_MAP.md` (更新)
  - `AI_DM_Sales/docs/HANDOVER.md` (更新)
  - `AI_DM_Sales/docs/2026-11-18_phase1_sales_engine_and_profiler.md` (新規・本書)

---

## 2. 背景・解決したかった課題（Before）
- Phase 1 第1回実装時点では、営業シミュレーターのロジックが `app/routes/dashboard.py` 内に簡易的なインライン処理として記述されており、本番DM監視ボット（Phase 1 第3回）と共有できる独立したサービスモジュール（`sales_engine.py`, `profiler_service.py`）になっていなかった。
- また、顧客カルテ（`fans.psych_profile_json`）を直近の会話履歴からLLMで構造化抽出・マージしてDBへ永続化する処理や、`ppv_transactions` テーブルの購入済み（`is_unlocked == 1`）履歴に基づく既購入PPV除外・階段式ステップ選定の連携が必要だった。

---

## 3. 採用した設計方針・理由（Why / ADR）
1. **Ollamaネイティブ `/api/generate` + `"think": False` によるリアルタイム顧客カルテ抽出と長期記憶マージ (`app/services/profiler_service.py`)**:
   - `update_fan_psych_profile(char_id, fan_id, recent_messages, existing_profile_json)` において、`LLMClient.generate_json()`（内部で `/api/generate` + `"think": False` + `"format": "json"` を使用）を呼び出し、`preferences`（好み・フェチ）、`psych_type`（心理タイプ）、`budget_tier`（推定予算感）、`hooks`（反応が良い話題）の4項目を構造化抽出（1回約0.9秒）。
   - 過去の会話で抽出した `preferences` や `hooks` が上書き消失しないよう、既存プロファイル・LLM抽出結果・会話キーワードシグナルの3者を重複排除マージして `fans.psych_profile_json` へ保存する方式を採用した。
   - また、既存のWeb UIコンポーネントが参照するキー（`fetishes`, `personality_type`, `budget_level`, `hot_topics`）も同一JSON内に同期保持することで、後方互換性を100%維持した。
2. **リソース節約型ガードレール優先評価と S.E.L.L. ステートマシン (`app/services/sales_engine.py`)**:
   - `SalesEngine.process_fan_message()` の冒頭で、まず「危険ワード・連続未読スルー上限による緊急停止判定（`is_escalated = 1`）」および「無料フォロワー＋PPV 2回連続スルー客の省エネ降格判定（`6_Low_Priority`, `is_low_priority = 1`）」を評価し、該当時はフルの営業LLM推論を即座にスキップする設計とした。これにより、荒らし客やタダ乗り客にGPU計算資源を浪費せず、課金見込み客へリソースを集中させる仕様書（4.2 / 4.5）の要件を厳密に実現した。
3. **DB連動の階段式PPV選定＆動的プライシング (`SalesEngine.select_ppv_and_price`)**:
   - `database.get_unlocked_ppv_ids()` により `ppv_transactions` テーブルで `is_unlocked == 1` の素材IDを取得し、候補から完全除外して同一顧客への再送信を防止。
   - 初課金前（`$0`）には `Step 1` × `tripwire_price_usd`、太客（`$50`以上または最上位プラン）には未購入の `Step 2` / `Step 3` × `whale_price_usd`、通常客には次の階段ステップ × `base_price_usd` を動的選定するアルゴリズムを実装した。

---

## 4. 実装内容・変更点サマリ（After）
- **`app/services/profiler_service.py` (新規)**:
  - `update_fan_psych_profile()`: 会話履歴＋既存カルテから `preferences`, `psych_type`, `budget_tier`, `hooks` をJSON抽出・マージし、DBの `fans.psych_profile_json` を更新。
  - `format_psych_profile_for_prompt()`: 抽出した顧客カルテを次回営業応答生成時のシステムプロンプトへ注入する構造化テキストブロックに整形。
- **`app/services/sales_engine.py` (新規)**:
  - `SalesEngine` クラス（およびシングルトン `sales_engine`）を実装。
  - `check_emergency_stop()`: `config.DANGER_KEYWORDS`（単語境界考慮）および `consecutive_unanswered >= 2` の検知・ロック。
  - `check_low_priority_demotion()` / `generate_low_priority_reply()`: `free_follower` かつ `consecutive_ppv_ignored >= 2` の自動降格と日英超短文相槌生成。
  - `determine_next_phase()`: 会話往復数・購買意欲キーワード・拒否シグナルに基づく `1_Icebreak` → `2_Explore` → `3_Lead_Pitch` → `4_Lock_Close` → `5_Cooldown` 自動遷移。
  - `select_ppv_and_price()` / `determine_pricing_mode()`: 既購入PPV除外、Step 1→2→3 階段選定、`tripwire` / `base` / `whale` 動的価格決定。
  - `build_sales_system_prompt()` / `process_fan_message()`: `dm_persona.json`・`plans.json`・顧客カルテ・提案PPVヒントを統合したシステムプロンプト構築と英語原文＋日本語訳（`content_ja`）生成。
- **`app/core/database.py` (更新)**:
  - `get_fan_record()`, `get_unlocked_ppv_ids()`, `save_fan_psych_profile_db()`, `upsert_fan_sales_state()` を追加。
- **`app/routes/dashboard.py` & `app/templates/index.html` (更新)**:
  - `POST /api/simulator/reply` を `sales_engine.process_fan_message()` に直結。
  - シミュレーターUIで連続会話履歴と `psych_profile` をステート保持し、Inspectorに4項目（`preferences`, `psych_type`, `budget_tier`, `hooks`）とガードレール理由をリアルタイム表示。既存の常時日英2段表示・日本語→英語変換送信機能も完全維持。

---

## 5. 作業中に発生した問題・ハマりポイントと解決策（Troubleshooting）
- **問題**: 初期実装で `process_fan_message()` 内の顧客カルテLLM抽出を省エネ降格判定（`6_Low_Priority`）の前に置いていたため、タダ乗り客に対しても毎回LLM HTTPリクエストが発生していた。また、`DANGER_KEYWORDS` の `"bot"` や `"ai "` を単純部分一致すると `"about"` や `"wait"` 等の一般英単語に反応する懸念があった。
- **解決策**: 緊急停止判定と省エネ降格判定を `process_fan_message()` の最優先ステップ（LLM呼び出し前）へ移動して即座に応答・ロックするように最適化し、さらに `"bot"` / `"ai"` 等の短い危険ワードには正規表現の単語境界（`\b`）検査を導入して誤爆を完全に防いだ。

---

## 6. 今後の注意点・残タスク
- 次回（Phase 1 第3回）では、今回完成した `sales_engine` と `profiler_service` を呼び出す Playwright 全キャラ同時並行DM監視ボット（`app/services/fansly_dm_bot.py`）および未開封PPVへのFOMOリマインド・Step 2追撃ワーカー（`app/services/followup_worker.py`）を実装する。
