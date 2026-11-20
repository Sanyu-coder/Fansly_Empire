# キャラクターデータ参照先パスの共通化（`shared_assets/characters` 対応）

## 1. 概要・日付・対象ファイル

- **日付**: 2026-11-18
- **概要**: `AI_Factory` 内のキャラクターデータ参照先パスを、従来の `./characters`（`BASE_DIR/characters`）から親ディレクトリの共通アセットパス `C:\Fansly_Empire\shared_assets\characters`（環境変数 `SHARED_ASSETS_DIR` で上書き可能）を参照するよう統一。
- **対象ファイル**:
  - `c:\Fansly_Empire\AI_Factory\app\config.py`
  - `c:\Fansly_Empire\AI_Factory\console_app.py`
  - `c:\Fansly_Empire\AI_Factory\factory_line1.py`
  - `c:\Fansly_Empire\AI_Factory\factory_line2.py`
  - `c:\Fansly_Empire\AI_Factory\factory_video.py`
  - `c:\Fansly_Empire\AI_Factory\app\services\worker_service.py`
  - `c:\Fansly_Empire\AI_Factory\PROJECT_MAP.md`

---

## 2. 背景・解決したかった課題（Before）

- `Fansly_Empire` 統合アーキテクチャへの移行に伴い、キャラクターデータ（`profile.json`, `01_candidates`, `02_ready_to_post`, `03_posted`, `browser_profile` 等）を複数アプリ（`AI_Factory`, `AI_DM_Sales`, `AI_Analytics_Console`）間で共有するため、`C:\Fansly_Empire\shared_assets\characters` へ集約された。
- しかし、`AI_Factory` 内の `app/config.py` や各バッチスクリプト（`factory_line1.py`, `factory_line2.py`, `factory_video.py`）では旧パス `C:\AI_Factory\characters` がハードコードされており、さらに `console_app.py` 内の `resolve_queue_folder_path` では相対パス `"characters"` がハードコードされていた。

---

## 3. 採用した設計方針・理由（Why / ADR）

- **環境変数 `SHARED_ASSETS_DIR` による柔軟な上書きとデフォルト共通パスの両立**:
  - `SHARED_ASSETS_DIR = os.environ.get("SHARED_ASSETS_DIR") or r"C:\Fansly_Empire\shared_assets"` とし、未指定時は `C:\Fansly_Empire\shared_assets\characters` を参照しつつ、テスト環境や別環境では環境変数 `SHARED_ASSETS_DIR` で任意のディレクトリへ切り替えられる設計を採用。
- **Webコンソールと単体バッチスクリプトの双方で一貫したパス解決**:
  - Flask アプリ（`app/config.py`）だけでなく、CLIから直接実行されることもある `factory_line1.py`, `factory_line2.py`, `factory_video.py` の全ファイルで同一の `SHARED_ASSETS_DIR` / `CHARACTERS_ROOT` 解決ロジックを適用。
  - `worker_service.py` からサブプロセスを起動する際も `env["SHARED_ASSETS_DIR"] = SHARED_ASSETS_DIR` を明示的に引き継ぐことで、プロセス間の不整合を防止。
- **既存の動作・UIの完全維持**:
  - APIのエンドポイント構造やレスポンス形式、フロントエンド（`index.html`）の挙動には一切変更を加えず、内部のストレージ参照先のみを差し替えた。

---

## 4. 実装内容・変更点サマリ（After）

1. **`app/config.py`**:
   - `SHARED_ASSETS_DIR = os.environ.get("SHARED_ASSETS_DIR") or r"C:\Fansly_Empire\shared_assets"` を追加。
   - `CHARACTERS_ROOT = os.path.join(SHARED_ASSETS_DIR, "characters")` に変更。
   - `BASE_DIR` をファイル位置基準（`os.path.dirname(os.path.dirname(os.path.abspath(__file__)))`、環境変数 `AI_FACTORY_BASE_DIR` で上書き可）に更新。
2. **`console_app.py`**:
   - `resolve_queue_folder_path` 内でハードコードされていた `os.path.join("characters", char_id, "02_ready_to_post")` を廃止し、`get_char_paths(char_id)` を通じて `CHARACTERS_ROOT` 配下の `02_ready_to_post` および `03_posted` を参照するように修正。
3. **`factory_line1.py`, `factory_line2.py`, `factory_video.py`**:
   - 各スクリプトの `SHARED_ASSETS_DIR` と `CHARACTERS_ROOT` を `C:\Fansly_Empire\shared_assets\characters`（環境変数 `SHARED_ASSETS_DIR` 対応）に統一。
4. **`app/services/worker_service.py`**:
   - Line1 / Video / Line2 のサブプロセス起動時に `env["SHARED_ASSETS_DIR"] = SHARED_ASSETS_DIR` を設定。

---

## 5. 作業中に発生した問題・ハマりポイントと解決策（Troubleshooting）

- **`console_app.py` における `resolve_queue_folder_path` のモンキーパッチ**:
  - `app/services/file_service.py` の `resolve_queue_folder_path` は `get_char_paths(char_id)` を使っていたが、`console_app.py` の起動時に `os.path.join("characters", ...)` を使う関数で上書きされていた。
  - `console_app.py` 側の関数も `get_char_paths(char_id)` を使うように修正し、`ready:` プレフィックスや `03_posted` フォールバックも `file_service.py` と同等に動作するよう整合させた。

---

## 6. 今後の注意点・残タスク

- 今後新たにワーカーやスクリプトを追加する場合は、`app.config` の `CHARACTERS_ROOT` または `get_char_paths(char_id)` を利用し、`./characters` のような相対パス直書きを行わないこと。
- 旧 `AI_Factory/characters` フォルダ内のデータが `C:\Fansly_Empire\shared_assets\characters` へ移行済みであることを確認の上、運用を行うこと。
