# 振り返り・設計判断レポート: 稼働モニターの直感的プログレスバー化

## 1. 概要・日付・対象ファイル
- **タスク実施日**: 2026年11月18日
- **対象ファイル**:
  - `app/services/worker_service.py`
  - `app/routes/worker_routes.py`
  - `app/templates/index.html`

## 2. 背景・解決したかった課題（Before）
- 画面最下部の稼働モニター（`#log-drawer`）は、これまでComfyUIやバッチスクリプトの生標準出力ログ（コマンドプロンプト風の文字列）がそのまま流れるだけであり、以下の課題があった：
  1. 全体で何件予定されており現在何件完了したのか（全体進捗）が直感的に分からない。
  2. 現在処理中の1件が今どのフェーズ（企画中、画像生成中、高画質化中、動画生成中、Fansly予約中など）にあり、どれくらい進んでいるのか（個別進行度）が分からない。
  3. テキストログ領域が画面下部を占有し、視覚的な負担が大きかった。

## 3. 採用した設計方針・理由（Why / ADR）
- **2段階プログレスバー表示の導入**:
  - **全体進捗（Overall Progress）**: `全体進捗: X / Y 件完了 (Z%)` のテキストバッジと小型プログレスバーを常時表示し、バッチ全体の達成度を一目で把握可能にした。
  - **現在処理中の1件の横長ゲージ（Item Progress）**: 0%〜100%のアニメーション付きゲージを中央に大きく配置。ComfyUIやアップスケール、Playwrightの各工程に応じて段階的に進行し、100%に達すると全体カウントが進んで次の1件が0%から開始するスムーズなUI挙動を採用。
  - **短く明確なフェーズラベル**: バー上部に「シチュエーション生成中...」「ComfyUI 画像生成中...」「高画質化 (HQ処理中)...」「動画レンダリング中 (Wan2.2)...」「Fanslyへ予約投稿完了！」などの短い日本語ラベルを1行表示。
- **生ログの折りたたみ格納**:
  - 普段はプログレスゲージのみをコンパクト（高さ約52px）に表示し、トラブルシューティングや詳細確認が必要な時のみ「詳細ログを見る ▼」ボタンで下部に開閉できるトグル構造を採用。
- **MacBook Air M1（1280px〜1440px）対応**:
  - 横並びのレイアウトに `min-width: 0` や `flex-shrink: 0`、`text-overflow: ellipsis` を適用し、画面幅が狭くても横スクロールや段落ちが発生しないレスポンシブ・コンパクト設計を徹底。
- **状態維持とチラつき防止**:
  - ワーカー完了時に即座に 0/0件 (0%) に戻さず、完了状態（100%、全工程完了バッジ）を画面上に維持。次回起動時に新パラメータでリセットする設計とした。

## 4. 実装内容・変更点サマリ（After）
- **`app/services/comfy_progress.py`（新規追加）**:
  - 外部ライブラリ（`websocket-client`等）不要・Python標準ライブラリのみで動作する軽量WebSocketクライアント (`_SimpleWsClient`) と `wait_comfy_prompt_with_progress()` を実装。
  - `/prompt` 送信と同じ `client_id` で `ws://127.0.0.1:8188/ws?clientId=...` を購読し、ComfyUIの `executing`（実行中ノード遷移）と `progress`（KSamplerの `Step val/max`）をリアルタイムに取得。
  - 標準出力へ `@@PROGRESS@@ {"pct": ..., "completed": ..., "total": ..., "stage": "..."}` を即時フラッシュ出力する `emit_progress()` を提供。
- **`factory_line1.py` / `factory_video.py` / `factory_line2.py`**:
  - **第1ライン (`factory_line1.py`)**: STEP 1企画生成（4%→9%）→ STEP 2ベース画像5枚生成（15%→70%：1枚ごとにComfyUIのKSamplerステップ進捗で1%刻みに上昇）→ STEP 3検品（71%→74%）→ STEP 4高画質化5枚処理（75%→99%）→ 完了（100%）を `emit_progress` でリアルタイム発信。
  - **動画バッチ (`factory_video.py`)**: 待機タスク総数の通知、Wan2.2の2段階KSampler（前半パス `129:86`: 26%→58%、後半パス `129:85`: 58%→88%）、VAEデコード（90%）、MP4保存（95%→100%）をステップ単位でリアルタイム発信。
  - **第2ライン (`factory_line2.py`)**: 予約キュー総数の通知、ページ遷移（10%）→ キャプション入力（28%）→ 本編メディア投入（38%）→ FYPプレビュー添付（52%）→ メディア確定（68%）→ 予約日時セット（80%）→ エンコード承認待機ループ（経過秒数に応じて82%→94%へ進行）→ 送信・アーカイブ完了（100%）を発信。
- **`app/services/worker_service.py`**:
  - `PYTHONUNBUFFERED="1"` および `bufsize=1`、`iter(proc.stdout.readline, "")` による非バッファリング行読み取りに変更。
  - `_consume_progress_signal()` により `@@PROGRESS@@ ` 行を即座にパースして `worker_state` を更新し、生ログ（`logs`）には混入させないよう分離。
- **`app/templates/index.html`**:
  - ポーリング間隔を `800ms` に高速化し、ステップ進捗を滑らかに反映。
  - 1件完了（100%）から次のアイテム（0%付近）へ切り替わる際、逆戻りアニメーションが起きないよう一時的に `transition: none` でリセットする制御を追加。

## 5. 作業中に発生した問題・ハマりポイントと解決策（Troubleshooting）
- **問題1（プログレスバーが初期値から全く動かない現象）**:
  1. `factory_line1.py` と `factory_video.py` の ComfyUI 待機ループが `/history` を無言でポーリングするだけで、生成完了まで数分〜十数分間1文字も標準出力に `print` していなかった。
  2. `factory_line2.py` のエンコード待機が `end=""`（改行なし）でドットを出力していたため、改行が来るまで `proc.stdout` の行読み取りがブロックされていた。
  3. 実際のログ文言と `worker_service.py` の正規表現・キーワードにズレがあった。
- **解決策**:
  - 曖昧な日本語ログの文字列マッチを廃止し、ComfyUI WebSocket（`/ws?clientId=...`）からのリアルタイムステップ取得＋機械可読な構造化JSONシグナル（`@@PROGRESS@@ `）による直接通知方式へ抜本的に刷新した。
- **問題2（`index.html` 末尾の重複 `startWorkerPolling()` 定義による関数上書きバグ）**:
  - `index.html` のファイル末尾（4230〜4344行目）に以前の `updateProgressUI()` と `function startWorkerPolling()` の重複定義が残っており、JavaScriptの関数巻き上げ（hoisting）によって新しい `startWorkerPolling()` が古い関数に上書きされていた。
  - その結果、ボタン押下直後の `pollWorkerOnce()` 1回分（`2%`）しかゲージが描画されず、以降の定期ポーリングで `#pm-item-fill` や `#pm-overall-fill` が一切更新されていなかった。また終了・停止後も `2%` のバーの色や強制停止ボタンが消えない状態になっていた。
- **解決策**:
  - `index.html` 末尾の重複関数・不要 `<style>` ブロックを完全削除。
  - ワーカー停止時（`/api/worker/stop`）および通常終了時（`st.running === false`）には、`worker_state["item_progress"] = 0` にリセットするとともに、フロントエンド側でも `#pm-item-fill` と `#pm-overall-fill` の幅を `0%` に戻してバーの塗り色が即座に消えるよう修正した。
  - さらに緊急停止時は `taskkill /F /T /PID` を併用して子・孫プロセスごと即座に終了させるよう強化した。

## 6. 今後の注意点・残タスク
- ComfyUIが起動していない環境やWebSocket接続が遮断された場合でも、`wait_comfy_prompt_with_progress` は自動的に `/history` ポーリングへフォールバックして完了を検知するため安全に動作する。
