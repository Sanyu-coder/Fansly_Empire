# 振り返り・設計判断レポート：Fansly DM実機DOM構造検査に基づくセレクタ・取得送信処理の完全一致改修

## 1. 概要・日付・対象ファイル
- **作業日**: 2026-11-18
- **概要**: ログイン済みブラウザプロファイル（`AI_DM_Sales/profiles/`）を用いてヘッドフル（`headless=False`）の Playwright で実際の `https://fansly.com/messages`（Angular 19 SPA）を詳細検査し、スレッド一覧・未読バッジ・会話メッセージ送受信者判定・チャット入力欄・送信ボタン・ローカルPPVメディア添付＆価格ロック設定モーダル（Upload v2 + Set price モーダル）の全セレクタおよびDOM解析・送信ロジックを実画面構造へ完全一致させた。
- **対象ファイル**:
  - `AI_DM_Sales/app/services/fansly_dm_bot.py` (更新)
  - `AI_DM_Sales/app/routes/dashboard.py` (更新：手動介入送信時の稼働中ボット送信キュー連携)
  - `AI_DM_Sales/.gitignore` (更新：`logs/` 追加)
  - `AI_DM_Sales/PROJECT_MAP.md` (更新)
  - `AI_DM_Sales/docs/HANDOVER.md` (更新)
  - `AI_DM_Sales/docs/2026-11-18_fansly_dom_selectors_live_fix.md` (新規)

---

## 2. 背景・解決したかった課題（Before）
- サーバー（ポート `5051`）およびバックグラウンドの `chrome-headless-shell.exe` の起動・停止は正常に動作していたが、実際の `https://fansly.com/messages` から未読スレッドやテストDMが取得できなかった。
- ヘッドフルブラウザで実DOMを検査した結果、以下の不一致が判明した：
  1. **スレッド一覧要素の不一致**: 旧 `app-group-message-selection` / `.message-thread-item` は存在せず、実際は `.message-list a[href^="/messages/"]` 配下の **`app-messaging-group`** であった。
  2. **未読バッジ要素の不一致**: 旧 `.unread-badge` / `.badge-unread` は存在せず、実際は `app-messaging-group` 内の **`.badge-container .badge`** であった。
  3. **SPA非同期描画待機の欠如**: `page.goto(..., wait_until="domcontentloaded")` 直後に要素検索していたため、Angular SPAのAPI取得完了前（`0件` 状態）で抜けていた。また右ペインの `app-messages-default-conversation-route` は左ペインの `app-messaging-group` より先に描画されるため、`app-messaging-group` 自体の出現待機が必須であった。
  4. **スレッドクリック時のプロフィール誤遷移リスク**: `app-messaging-group` 内のアバターや名前部分は `<a href="/<username>">`（プロフィールリンク）になっているため、中央クリックではなく `/messages/<group_id>` への遷移またはプレビュー部（`.eclipse`）クリックが必要であった。
  5. **会話メッセージと送信者判定の不一致**: 実際は **`app-group-message`** が使われ、クリエイター自身の送信分には **`my-message`** クラスが付与され、ファン受信分には `my-message` が付かない（`app-group-message:not(.my-message) .message-text`）構造であった。
  6. **PPV購入済み判定とPPVアップロード価格設定モーダル（Upload v2）の不一致**:
     - 購入済み表示は `app-group-message.my-message app-group-message-attachment .purchased-content .purchased-avatar:not(.not-purchased)`（未購入時は `.not-purchased` クラス付き）。
     - ファイル添付時は **`app-account-media-upload-v2`** モーダルが開き、デフォルトの `Subscribe (any tier)` チップ（`button.perm-cond-remove`）を外して `.perm-cond-add` → `Pay` を選択すると **`app-balance-input-modal`** が開き、`input[aria-label="Amount"]` 入力 → `app-button.solid-blue` (`Confirm`) → `button.upload-primary` (`Attach`) の手順が必要であった。

---

## 3. 採用した設計方針・理由（Why / ADR）
1. **未読バッジ優先＋直近上位スレッドのスマート確認による取りこぼしゼロ設計**:
   - `.badge-container .badge` を持つ未読スレッドを最優先で処理するのに加え、ユーザーがブラウザで一度テストDMを開いて未読バッジが消えた直後のケースでも取りこぼさないよう、上位5件のうち時刻表記が直近（`Just now`, `Xm`, `Xh` 等）のスレッドは1回だけ会話末尾を確認し、最後の `app-group-message` がファン送信（`:not(.my-message)`）かつDB未返信であれば処理する2段構えを採用した。一方、`Nov 12` 等の日付表記の過去スレッドは対象外とし、古い会話への誤返信を防止した。
2. **巡回後の `/messages` トップ自動復帰**:
   - 個別スレッド（`/messages/<group_id>`）を開いたまま待機すると、そのファンから追撃返信が来た瞬間にFansly側で自動既読化されてバッジが付かなくなるため、処理・送信完了後は必ず `https://fansly.com/messages`（会話未選択ルート）へ戻す設計とした。
3. **DM送信制限（`.no-dm-allowed`）の事前検知とDB整合性保護**:
   - 相手側のDM受信設定により `.no-dm-allowed` / `.message-composer.hidden` となっているスレッドを検知した際やブラウザ送信が失敗した際は、DBに架空のAI送信履歴を書き込まず明確な警告ログを残す設計とした。
4. **ファイルローテーション付きデバッグログ出力 (`logs/fansly_dm_bot.log`)**:
   - バックグラウンド稼働時でもスレッド検出数・未読バッジ有無・既読遅延秒数・送信結果を一目で追跡できるよう、`StreamHandler` に加えて `RotatingFileHandler`（`AI_DM_Sales/logs/fansly_dm_bot.log`）を追加した。

---

## 4. 実装内容・変更点サマリ（After）
- **`app/services/fansly_dm_bot.py`**:
  - `FANSLY_SELECTORS` を実機 Angular 19 DOM構造へ全面更新。
  - `_wait_for_messages_spa_ready()`, `_extract_threads_snapshot()`, `_ensure_fan_conversation_open()`, `_extract_conversation_state()`, `_attach_ppv_with_price_modal()` を実装。
  - `_poll_fansly_page_once()` で全スレッドスナップショットログ・未読＆直近未返信検知・連続ファンバブル結合・二重返信防止ガード・巡回後 `/messages` 復帰を実装。
  - `handle_incoming_fan_message()` / `_dispatch_sales_engine_reply()` で `read_delay` 中断再開時のファンメッセージ重複DB挿入防止および送信失敗時のDB不整合防止を追加。
- **`app/routes/dashboard.py`**:
  - 管理画面の直接介入送信（`/api/monitoring/send_manual` および `/api/monitoring/translate` の `send_now=True`）実行時、対象キャラのボットが稼働中であれば `bot_manager.enqueue_outbound_message()` へも投入して実ブラウザから送信されるよう連携。

---

## 5. 作業中に発生した問題・ハマりポイントと解決策（Troubleshooting）
1. **SPA初期描画時の `app-messages-default-conversation-route` 先行マウント問題**:
   - ライブ検証時、`wait_for_selector("app-messaging-group, app-messages-default-conversation-route")` とすると右ペインのデフォルト画面がAPI完了前に即座にマッチしてしまい、スレッド取得数が `0` になる現象を確認した。
   - 解決策として、`app-messages-route` 確認後に左ペインの `app-messaging-group` 自体を個別待機するよう分離し、実機検証で18件全件＋未読テストDM（`@Sanyu28394`: `"Hello"`, `unread_count: 1`）が100%取得できることを確認した。
2. **Windows ポート `5051` の複数旧Pythonプロセス残存**:
   - Windowsのソケット共有仕様により過去の `run_dm_sales.py` プロセスが複数残存していたため、`CommandLine -like '*run_dm_sales.py*'` の既存プロセスをすべてクリーンに終了させた上で最新単一プロセスを再起動し、ポート `5051` のリスニングと `/api/bot/status` 正常応答を確認した。

---

## 6. 今後の注意点・残タスク
- 本番ボットは人間味演出のため、ファン受信DMをDBへ即時保存したあと **既読遅延（1〜5分）＋タイピング演出（10〜30秒）** を経て返信を送信する仕様となっている。稼働状況やカウントダウン秒数は `AI_DM_Sales/logs/fansly_dm_bot.log` でリアルタイムに確認できる。
- 次フェーズ（Phase 2）では `optimizer_batch.py` による成約/離脱会話の比較分析および `sales_playbooks` 自動進化バッチを実装予定。
