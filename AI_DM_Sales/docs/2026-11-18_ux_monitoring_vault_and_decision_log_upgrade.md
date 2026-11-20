# 2026-11-18 UX・監視継続・カウントダウン手動切替・AI検討ログ可視化・PPV武器庫＆マニュアル改修レポート

## 1. 概要・日付・対象ファイル
- **作業日**: 2026-11-18
- **対象アプリ**: `AI_DM_Sales`（ポート `5051` / `C:\Fansly_Empire\AI_DM_Sales`）
- **対象ファイル**:
  - `app/core/database.py`
  - `app/services/profiler_service.py`
  - `app/services/sales_engine.py`
  - `app/services/optimizer_batch.py`
  - `app/services/fansly_dm_bot.py`
  - `app/services/followup_worker.py`
  - `app/services/dormant_revival_worker.py`
  - `app/routes/dashboard.py`
  - `app/routes/vault_routes.py`
  - `app/templates/index.html`
  - `run_dm_sales.py`
  - `tests/test_phase3_ux_and_evolution.py`
  - `PROJECT_MAP.md`, `docs/HANDOVER.md`, `docs/USER_MANUAL.md`

---

## 2. 背景・解決したかった課題（Before）
1. **稼働トグル `OFF` 時の監視停止**: 個別キャラや一括稼働スイッチを `OFF` にすると Playwright 監視スレッド自体が停止するため、新着DMの受信確認・日本語翻訳・AI顧客カルテ更新も行われず、手動対応モードとして使いづらかった。
2. **返信待機中のブラックボックス化と返信忘れリスク**: 既読遅延・タイピング待機（1〜5分）の残り秒数が見えず、途中でキャンセルして手動介入へ切り替える手段や、手動保留にしたまま返信を忘れた際のアラート・AI自動返信への復帰手段がなかった。
3. **AIカルテ・営業判断の根拠不可視**: 抽出された顧客カルテや選ばれた営業フェーズ・PPV価格の「判断根拠」が記録されておらず、オペレーターによる妥当性監査や、`optimizer_batch.py` でのAI自身の判断クセ改善ができなかった。
4. **手動リロード依存**: バックグラウンドで新着DMを取得しても、モニタリング画面を手動更新しないと最新状態が反映されなかった。
5. **PPV一覧での動画真っ黒表示・価格未連動・見切れ**:
   - `index.html` の `renderVaultGrid()` が動画ファイルに対しても `<img src="...">` を描画していたため、実在する `.mp4` ファイルがデコード不能となり真っ黒・破損表示になっていた。
   - メディア配信APIが HTTP `Range` リクエスト（`206 Partial Content`）に明示対応しておらず、また一覧カードの階段レベル変更時に価格3種が自動連動していなかった。さらに `object-fit: cover` により縦長メディアが見切れていた。
6. **狭幅画面でのレイアウト崩れ・運用マニュアル導線の不足**: 半画面分割やノートPC幅でヘッダーや6列サマリーが窮屈になり、また管理画面内から `USER_MANUAL.md` をすぐ参照・検索できるビューアがなかった。

---

## 3. 採用した設計方針・理由（Why / ADR）
1. **監視スレッド常時稼働と `bot_enabled`（自動営業許可フラグ）の責務分離**:
   - UIのボタン表記・デザイン（`[▶ 全キャラ一括稼働]` / `[⏹ 全停止]`、個別トグル）は現場の慣れを損なわないよう完全維持しつつ、内部では `bot_enabled == False` を「監視・翻訳・カルテ更新のみ継続し、AI自動返信・PPV送信・自動追撃をスキップするモード」と再定義した。
   - 一方、独立アプリウィンドウを「×」で閉じた際は `pagehide` ビーコン（F5リロード誤検知防止の3秒猶予付き）＋ `atexit` から `bot_manager.stop_all()` を呼び出し、Playwright/Chromiumプロセスを確実にクリーン終了する設計とした。
2. **非ブロッキング待機スケジュールと `manual_hold` 状態管理**:
   - `CharacterBotWorker._pending_replies` に送信予定時刻（`scheduled_ts`）と個別 `threading.Event`（`cancel_event`）を保持することで、他ファンの監視をブロックせず、かつ既読遅延中・タイピング演出中のどの瞬間でも即座にキャンセルして `manual_hold = 1` へ移行できるようにした。
   - `manual_hold_at` から600秒（10分）以上経過したスレッドを `is_manual_overdue = True` として自動検知し、手動送信または `[🤖 やっぱりAI自動返信に戻す]` で即時解除される安全ガードを導入した。
3. **`decision_log_json` の構造化保存と自己進化ループへの還流**:
   - カルテ更新根拠（`profile_reasoning`）と営業戦略検討過程（`strategy_reasoning`）を1ターンごとの構造化JSONとして `dm_messages` および `fans` テーブルに保存し、UIのアコーディオン可視化と `optimizer_batch.py` の勝ち/負け比較プロンプトの双方で同一スキーマを再利用する設計とした。
4. **入力保護付き軽量差分ポーリング（4秒間隔）**:
   - JSON署名（メッセージID列・カルテ・検討ログ・取引履歴・スレッド状態）に差分がある領域のみDOM更新し、`.intervention-box`（日本語/英語入力欄）には一切触れないことで、オペレーターのタイピング中テキストやカーソル位置を100%保護した。
5. **実ファイル有無判定 × `<video #t=0.1>` × HTTP 206 `Range` 配信**:
   - Windowsパス区切りを正規化して実ファイル存在有無（`file_exists`）を返し、実在動画には `<video preload="metadata" src="...#t=0.1">` ＋ HTTP `206 Partial Content` で1コマ目を確実に描画させ、未配置テスト項目にはSVGプレースホルダーを維持するハイブリッド方式を採用した。

---

## 4. 実装内容・変更点サマリ（After）
- **DB (`app/core/database.py`)**: `fans` に `manual_hold`, `manual_hold_at`, `decision_log_json`、`dm_messages` に `decision_log_json` を自動マイグレーション追加。10分放置判定・検討ログ追記・手動送信時の保留自動解除を実装。
- **思考ログ＆自己進化 (`profiler_service.py`, `sales_engine.py`, `optimizer_batch.py`)**: カルテ各項目の抽出根拠とフェーズ/ステップ/AB群/チップ/PPV判定理由を日本語で構造化記録し、`run_playbook_optimization()` で会話履歴とあわせてOllamaへ渡して次世代 `Playbook B` を生成するよう統合。
- **DM監視ボット＆ワーカー (`fansly_dm_bot.py`, `followup_worker.py`, `dormant_revival_worker.py`, `run_dm_sales.py`)**: 稼働 `OFF` 時の常時監視継続・自動返信/追撃スキップ、カウントダウン管理、手動スキップ、AI自動返信復帰、手動送信時の即時キュードレイン、終了時の `stop_all()` を実装。
- **API＆メディア配信 (`dashboard.py`, `vault_routes.py`)**:
  - `GET /api/monitoring/poll`, `POST /api/monitoring/skip_to_manual`, `POST /api/monitoring/resume_auto_reply`, `GET /api/system/manual`, `POST /api/system/heartbeat`, `POST /api/system/window_closed` を追加。
  - `/api/vault/media/<char_id>/<filename>` を HTTP `Range` (`206 Partial Content`) 完全対応に改修。
- **管理コンソールUI (`app/templates/index.html`)**:
  - 右ペイン上部のリアルタイムカウントダウン・スキップ/自動復帰バー・10分放置アラートバッジ。
  - 右ペイン「リアルタイムAI顧客カルテ」直下の `🧠 AIカルテ根拠・返答検討ログを見る` アコーディオン。
  - 4秒自動ポーリング（オペレーター入力保護・スクロール位置保護・アコーディオン開閉保護）。
  - PPV一覧の動画1コマ目プレビュー、`object-fit: contain` 全体表示、3段階レベル（導入/本命/超秘蔵）変更時の価格3種即時自動連動、音声付き拡大ライトボックス。
  - ヘッダー折り返し＆コンパクト売上サマリーのレスポンシブ対応、設定モーダル最下部の `[📖 運用マニュアルを開く]` ＆検索付きマークダウンビューア。

---

## 5. 作業中に発生した問題・ハマりポイントと解決策（Troubleshooting）
1. **既存手動送信APIの `enqueue_outbound_message` 引数不一致の発見と修正**:
   - `dashboard.py` の既存コードで `bot_manager.enqueue_outbound_message(char_id, {...})` と辞書を第2引数に渡していた箇所があり、ワーカー稼働中に `TypeError` となる潜在不具合を発見したため、キーワード引数形式（`char_id=..., fan_id=..., message_en=..., message_ja=..., source="human_manual"`）へ統一して解消した。
2. **PPV一覧で実在動画のみ真っ黒になりダミー項目だけ表示されていた現象の切り分け**:
   - 実ファイル不在時はバックエンドが `image/svg+xml` を返すため `<img>` で表示できていた一方、実在する `.mp4` は `video/mp4` が `<img>` に渡って真っ黒になっていた。`file_exists` フラグと `media_type` の組み合わせで `<video preload="metadata" src="...#t=0.1">` と `<img>` を出し分け、HTTP `206` 対応を入れることで両方とも正常表示させた。
3. **ブラウザのF5リロードと「×」ウィンドウ閉鎖の識別**:
   - `pagehide` 単体ではリロード時にも発火するため、3秒の猶予タイマーを設け、再ロードによる `/api/system/state` やポーリングが届けばシャットダウンをキャンセルし、本当にウィンドウを閉じた場合のみ `stop_all()` で全終了する安全設計とした。

---

## 6. 今後の注意点・残タスク
- 新規キャラクター追加時は `[⚙️ 設定]` の「初期価格マスター」を保存すると、PPV追加モーダルおよびPPV一覧の階段レベル切替時の自動連動価格にも即座に反映されます。
- 将来 `AI_Analytics_Console`（横断BI）を実装する際、`dm_messages.decision_log_json` / `fans.decision_log_json` を集計することで「どのフックやフェーズ遷移が最も成約に寄与したか」の定量分析にも活用可能です。
