# Phase 2 実装レポート：自己進化バッチ分析・A/B自動昇格・チップ誘導フック・休眠ファン掘り起こし

## 1. 概要・日付・対象ファイル
- **作業日**: 2026-11-18
- **概要**: `AI_DM_Sales`（ポート `5051`）の Phase 2（v1.1 自己進化・LTV最大化拡張）全機能を一括設計・実装した。すべてのローカルLLM推論は `app/core/llm_client.py` の Ollama ネイティブ `/api/generate` + `"think": False` 経由で高速実行される。
- **対象ファイル**:
  - `AI_DM_Sales/app/services/optimizer_batch.py` (新規)
  - `AI_DM_Sales/app/services/dormant_revival_worker.py` (新規)
  - `AI_DM_Sales/tests/test_phase2_integration.py` (新規)
  - `AI_DM_Sales/app/core/database.py` (更新)
  - `AI_DM_Sales/app/core/llm_client.py` (更新)
  - `AI_DM_Sales/app/services/sales_engine.py` (更新)
  - `AI_DM_Sales/app/services/fansly_dm_bot.py` (更新)
  - `AI_DM_Sales/app/routes/dashboard.py` (更新)
  - `AI_DM_Sales/app/templates/index.html` (更新)
  - `AI_DM_Sales/run_dm_sales.py` (更新)
  - `AI_DM_Sales/PROJECT_MAP.md` (更新)
  - `AI_DM_Sales/docs/HANDOVER.md` (更新)
  - `AI_DM_Sales/docs/2026-11-18_phase2_self_evolution_and_dormant_revival.md` (新規・本書)

---

## 2. 背景・解決したかった課題（Before）
- Phase 1（v1.0）完了時点で、S.E.L.L.営業ステートマシン・リアルタイム顧客カルテ・動的プライシング・階段式PPV・Playwright自動監視送信ボット・FOMO追撃ワーカーが稼働していたが、以下のLTV最大化・自己改善サイクルが未実装だった：
  1. 蓄積された成約/離脱会話データから営業プロンプトルール自体を自律改善し、A/BテストでCVRが高いルールを自動昇格させる仕組みがなかった。
  2. PPVロック販売以外に、強い称賛・甘え・特別リクエスト時や購入直後の感謝フェーズ（`5_Cooldown`）に自然な `$5〜$10` チップ（投げ銭）を引き出す導線がなかった。
  3. 過去に課金実績があるにもかかわらず7日以上連絡が途絶えた優良顧客（太客・VIP）に対し、顧客カルテ（好み・フェチ）を踏まえた個別再会DMで掘り起こす仕組みがなかった。

---

## 3. 採用した設計方針・理由（Why / ADR）
1. **既存DB互換性を維持した `sales_playbooks` 自動マイグレーションと 50:50 A/B均等振り分け (`database.py`)**:
   - 稼働中の `empire_core.db` を破壊しないよう、`PRAGMA table_info(sales_playbooks)` で未存在カラム（`rules_ja`, `analysis_summary_ja`, `offers_count`, `purchases_count`）を検知して自動 `ALTER TABLE` する方式を採用した。
   - 新規ファン登録時（`add_dm_message`, `save_fan_psych_profile_db`, `upsert_fan_sales_state`）にキャラごとの既存 A/B 人数を集計し、少ない方へ自動割り当てする `_assign_balanced_ab_group_with_cursor` を導入し、偏りのない 50:50 比較検証を実現した。
2. **分析生成（`run_playbook_optimization`）と昇格判定（`evaluate_and_promote_ab_test`）の責務分離 (`optimizer_batch.py`)**:
   - 新ルール `B` の生成処理と、既存 A/B の CVR 比較・昇格処理（各群10件以上かつ `B > A` で新 `A` へ昇格）を独立関数として分離した。管理画面の `POST /api/evolution/optimize` では「①既存A/Bの評価・昇格判定 → ②最新スレッド比較分析による次世代ルールB生成」の順で実行することで、勝利したBを確実に本番Aへ昇格させつつ次の検証サイクルへ進める設計とした。
3. **PPV提案と競合しないチップ誘導フック設計 (`sales_engine.py`)**:
   - 1通のメッセージ内でPPVロック価格とチップ要求を同時に出すと押し売りに見えCVRが低下するため、`detect_tip_opportunity()` が発動したターンでは `recommended_ppv = None` とし、キャラ口調を維持した自然な `$5`（通常層）または `$10`（VIP/リピーター層）のチップ誘導に専念させた上で、`ppv_transactions` に `tx_type = 'tip'` として自動記録する設計を採用した。
4. **二重送信防止と睡眠制御を両立した休眠掘り起こしワーカー (`dormant_revival_worker.py`)**:
   - `total_spent_usd > 0`・未連絡 `inactive_days` 日以上・非エスカレーション・非ミュート・非省エネの条件で抽出し、日本語顧客カルテ（`psych_profile_json`）のフェチ・話題を織り込んだ英語原文＋日本語訳を生成。`dm_messages` 保存と同時に `last_contact_at` を更新してから `bot_manager.enqueue_outbound_message()` へ投入することで、連続実行時の二重送信を完全に防止した。

---

## 4. 実装内容・変更点サマリ（After）
- **`app/services/optimizer_batch.py` (新規)**:
  - `extract_win_loss_threads()`, `run_playbook_optimization()`, `evaluate_and_promote_ab_test()`, `get_evolution_status()` を実装。
- **`app/services/dormant_revival_worker.py` (新規)**:
  - `generate_personalized_revival_dm()`, `run_dormant_revival()`, およびバックグラウンド定期実行デーモン `DormantRevivalWorker`（シングルトン `dormant_revival_worker`）を実装。
- **`app/services/sales_engine.py` & `app/services/fansly_dm_bot.py` (更新)**:
  - 対象ファンの `ab_group` に対応するアクティブな `sales_playbooks.rules_text` をシステムプロンプトへ自動注入し、`dm_messages.prompt_version`（`v{id}-{A|B}`）へ記録。
  - `detect_tip_opportunity()` および `tip_hook` プロンプト指示・返信補完・`ppv_transactions`（`tx_type = 'tip'`）記録を実装。
- **`app/routes/dashboard.py`, `app/templates/index.html`, `run_dm_sales.py` (更新)**:
  - `GET /api/evolution/status`, `POST /api/evolution/optimize`, `POST /api/evolution/revive_dormant` を追加。
  - タブ3（「3. キャラ・プラン設定」）および `[⚙️ 設定]` モーダル内に、A/Bルール比較・CVR表示・即時分析ボタン・休眠ファン候補一覧・即時掘り起こし実行ボタンを追加。

---

## 5. 作業中に発生した問題・ハマりポイントと解決策（Troubleshooting）
- **問題1**: 初回モデルロード時（`my-qwen3:latest` 約8.7GB）に `run_playbook_optimization` のJSON比較分析を実行すると、従来の15秒タイムアウトではVRAMロード時間を含めてギリギリになる可能性があった。
- **解決策1**: `LLMClient` のデフォルトタイムアウトを `45.0` 秒へ拡張し、`generate_json` の `max_tokens` 引数対応（`800` トークン）を追加したことで、Ollamaネイティブ `/api/generate` + `"think": False` による安定した高速JSON出力を確認した。
- **問題2**: 統合テストで `bot_manager.enqueue_outbound_message()` の戻り値辞書に対して `queued_count` キーを参照していたが、同メソッドは投入したアイテム辞書を返し、キュー残数は `bot_manager.get_status(char_id)["char_status"]["queued_count"]` で取得する仕様だった。
- **解決策2**: 既存の `BotManager` インターフェース後方互換性を維持したまま、統合テスト側で投入アイテム内容（`fan_id`, `source == 'dormant_revival'`）と `get_status()` の `queued_count == 1` の双方を検証するよう修正し、全4テストがパスすることを確認した。

---

## 6. 今後の注意点・残タスク
- これにて **`AI_DM_Sales` の Phase 1（コア構築）および Phase 2（自己進化・LTV最大化拡張）の全機能が100%実装完了** となった。
- 将来のアプリ③（`AI_Analytics_Console`）開発時には、`empire_core.db` の `sales_playbooks`（A/B別 `cvr_score` 推移）および `ppv_transactions`（`tx_type = 'ppv'` / `'tip'` 別売上）をそのまま横断集計に活用できる。
