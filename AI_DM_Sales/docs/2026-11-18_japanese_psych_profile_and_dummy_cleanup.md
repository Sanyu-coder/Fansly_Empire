# 振り返り・設計判断レポート: AI顧客カルテの日本語出力化 ＆ ダミー顧客・テスト用会話ログの完全削除とシード廃止

## 1. 概要・日付・対象ファイル
- **作業日**: 2026-11-18
- **概要**: `AI_DM_Sales`（ポート `5051`）において、① ローカルOllama（`my-qwen3:latest`）によるAI顧客カルテ（`psych_profile_json`）抽出および関連ロジックをすべて分かりやすい日本語出力へ統一し、Web UI表示およびシステムプロンプトへの英語営業文反映を最適化。② DB初期化時のダミーデータシード処理（`_seed_demo_data_if_empty`）を完全廃止し、本番DB（`empire_core.db`）からダミー顧客・サンプル会話・ダミーPPV履歴を完全クリーンアップして実機データ（`@Sanyu28394`）のみが残る状態にした。
- **対象ファイル**:
  - `AI_DM_Sales/app/services/profiler_service.py`
  - `AI_DM_Sales/app/services/sales_engine.py`
  - `AI_DM_Sales/app/core/database.py`
  - `AI_DM_Sales/app/__init__.py`
  - `AI_DM_Sales/app/templates/index.html`
  - `C:\Fansly_Empire\shared_assets\database\empire_core.db`
  - `AI_DM_Sales/PROJECT_MAP.md`
  - `AI_DM_Sales/docs/HANDOVER.md`

---

## 2. 背景・解決したかった課題（Before）
1. **顧客カルテの英語出力**:
   - `update_fan_psych_profile()` でOllamaから抽出される顧客カルテ（`preferences`, `psych_type`, `budget_tier`, `hooks`, `notes`）が英語で抽出・保存されており、日本人オペレーターがWeb管理画面（タブ1「営業モニタリング」右ペイン、およびタブ4「営業シミュレーター」右ペイン）で確認する際に直感的でなかった。
   - キーワードヒューリスティクスやフォールバック、省エネ降格時・緊急停止時のプロファイルも一部英語表記となっていた。
2. **ダミー顧客・サンプル会話履歴の残留と自動シード**:
   - 初期開発段階で投入されていた5名のダミー顧客（`Lucas_VIP`, `Mike_NekoFan`, `SuspiciousUser99`, `GhostWatcher`, `DarkKnight_77`）およびダミー会話履歴・ダミーPPV取引データが `empire_core.db` に残っており、実機Fansly監視で取得した本番顧客（`@Sanyu28394`）と混在していた。
   - アプリ起動時（`init_db` / `create_app`）に `seed_demo=True` のままだと、DBリセット時などにダミーデータが復活するリスクがあった。

---

## 3. 採用した設計方針・理由（Why / ADR）
1. **顧客カルテの日本語抽出とプロンプト誘導**:
   - `profiler_service.py` のシステムプロンプトに `IMPORTANT: You MUST write ALL JSON field values in natural, clear JAPANESE (日本語).` を明記し、各キーの具体例（「寝室・ベッドルーム秘密動画」「熱狂的従属・忠誠ファン」「高額課金（太客・クジラ層）」「二人だけの秘密の共有」等）を提示。
   - `_extract_heuristic_signals()` および `sales_engine.py` の省エネ降格・緊急停止時のプロファイル辞書も同一の日本語表現に統一した。
   - `format_psych_profile_for_prompt()` では、システムプロンプトへ注入する際に「Note: Read the fan insights below (in Japanese) and tailor your seductive English reply accordingly.」を付与。日本語カルテのニュアンスをローカルLLMが正確に汲み取って、ファンの性癖や好みに合わせた魅力的な英語営業文を生成できるようにした。
2. **Web管理画面（index.html）の表示調整**:
   - タブ1（営業モニタリング右ペイン）およびタブ4（営業シミュレーター右ペインインスペクター）において、日本語カルテが文字化けなく綺麗に表示されることを確認。営業メモ（`notes`）も表示項目に追加。
3. **ダミーデータ完全削除とシード処理の永久停止**:
   - `database.py` の `_seed_demo_data_if_empty()` 内の投入処理を `pass` に変更し、`init_db(seed_demo=False)`、`create_app(seed_demo=False)` とした。
   - DB（`empire_core.db`）からダミー顧客5名および関連する `dm_messages`、`ppv_transactions`、サンプル `daily_account_metrics` を完全に `DELETE`。実データ（`Sanyu28394`）のレコードおよび最新の日本語カルテのみを保持。

---

## 4. 実装内容・変更点サマリ（After）
- **`app/services/profiler_service.py`**:
  - `_extract_heuristic_signals()`: 日英両方のキーワード（ベッド/寝室/無修正/コスプレ/足裏/女王様/甘え/焦らし等）から日本語の `preferences`, `psych_type`, `budget_tier`, `hooks` を抽出。
  - `update_fan_psych_profile()`: Ollamaプロンプトを日本語JSON出力必須に改修。累計課金額の表記も日本語化。
  - `format_psych_profile_for_prompt()`: 日本語カルテをシステムプロンプトに注入しつつ、英語営業返信を指示するフォーマットへ改修。
- **`app/services/sales_engine.py`**:
  - 緊急停止時（`is_escalated = 1`）および省エネ降格時（`6_Low_Priority`）のカルテ辞書を日本語に統一。
- **`app/core/database.py` & `app/__init__.py`**:
  - `_seed_demo_data_if_empty`: 実装を `pass` にして無効化。
  - `init_db(seed_demo=False)` / `create_app(seed_demo=False)` にデフォルト引数を変更。
- **`app/templates/index.html`**:
  - シミュレーターインスペクターに `notes`（営業メモ）表示を追加。
- **本番DBクリーンアップ**:
  - `empire_core.db` からダミー顧客5件、ダミーメッセージ12件、ダミーPPVトランザクション6件、ダミー日次メトリクス2件を完全削除。実機データ（`Sanyu28394`）のみ保持。

---

## 5. 作業中に発生した問題・ハマりポイントと解決策（Troubleshooting）
- **PowerShellインラインスクリプトでの `$` 記号エスケープ**:
  - `python -c` 内に `$`（USD表記）を直接含めるとPowerShellが変数展開しようとして構文エラーが発生。
  - **解決策**: `chr(36)` を利用するか、Pythonスクリプトファイル経由で安全に実行・更新を行った。
- **構文チェックでのインデントエラー**:
  - エディタ修正時に `get_ppv_stats_by_char` の定義が重複しインデントブロック待ちが発生。
  - **解決策**: 重複定義を削除し、`python -m py_compile` による一括構文チェックをクリア。

---

## 6. 今後の注意点・残タスク
- Fansly実機上で新しいファンからDMを受信した際、自動的に `fans` テーブルへ追加され、`update_fan_psych_profile()` によってリアルタイムに日本語の顧客カルテが蓄積される。
- ポート `5051` サーバーは再起動済みであり、Playwright DM監視ボットおよびフォローアップワーカーが正常に稼働中。
