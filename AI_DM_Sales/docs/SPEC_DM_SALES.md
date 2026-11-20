# Fansly AI DM営業エンジン（AI_DM_Sales）システム仕様書 v1.0（確定版）

## 1. システム概要と全体アーキテクチャ
### 1.1 目的
複数キャラクターのペルソナおよびサブスクプラン情報を参照し、FanslyのDM受信から自然な雑談・ニーズ把握・PPV（秘蔵動画/画像）提案・アップセル・クロージングまでを完全自動化するAI営業エンジン。
単なる事務自動化ではなく、**「顧客の心理プロファイリング」「動的プライシング」「階段式PPV販売」「自己進化ルール最適化」**により、顧客の課金率（CVR）および顧客生涯価値（LTV）を最大化することを主目的とする。

### 1.2 3大アプリ統合アーキテクチャ（`C:\Fansly_Empire`）
本システムは、親ディレクトリ `C:\Fansly_Empire` 配下で以下の3アプリケーションと共通資産（`shared_assets`）を共有する疎結合モノレポ構成を採用する。
- **`shared_assets\`（全3アプリ共通の心臓部）**:
  - `characters\<char_id>\`: キャラクター別のマスター設定（`dm_persona.json`, `plans.json`, `settings.json`）およびメディア資産（`ppv_vault\`, `post_media\`）。
  - `database\empire_core.db`: 全3アプリで共有する統合SQLiteデータベース。複数プロセスからの同時読み書きによるロックエラーを防ぐため、接続時に必ず **`PRAGMA journal_mode=WAL;`** を有効化する。
- **`AI_Factory\`（アプリ①・既存・ポート5050）**: コンテンツ生産＆予約投稿工場。投稿実績を `empire_core.db` の `posts_log` へ記録可能。
- **`AI_DM_Sales\`（アプリ②・本システム・ポート5051）**: 完全自動DM営業＆自己進化エンジン。
- **`AI_Analytics_Console\`（アプリ③・将来開発）**: `empire_core.db` を読み取り、全キャラの売上・CVR・フォロワー推移・会話数・投稿エンゲージメントを一覧監視する経営・マーケティング統合BIコンソール。

---

## 2. 統合データベース設計（`empire_core.db`）
将来の `AI_Analytics_Console` での集計・分析を前提とし、以下の6テーブルを構築する。

### 2.1 `daily_account_metrics`（日次アカウント指標）
- `id` (INTEGER PK AUTOINCREMENT)
- `date` (TEXT NOT NULL): `YYYY-MM-DD`
- `char_id` (TEXT NOT NULL)
- `followers_count` (INTEGER DEFAULT 0)
- `subscribers_count` (INTEGER DEFAULT 0)
- `total_likes` (INTEGER DEFAULT 0)
- `total_revenue_usd` (REAL DEFAULT 0.0)
- `UNIQUE(date, char_id)`

### 2.2 `posts_log`（投稿実績ログ：AI_Factory / Analytics連携用）
- `post_id` (TEXT PK)
- `char_id` (TEXT NOT NULL)
- `posted_at` (TEXT NOT NULL)
- `media_type` (TEXT): `video` / `image`
- `likes_count` (INTEGER DEFAULT 0)
- `views_count` (INTEGER DEFAULT 0)
- `is_fyp` (INTEGER DEFAULT 0): 0 or 1

### 2.3 `fans`（顧客カルテ・営業ステータス管理）
- `fan_id` (TEXT NOT NULL): Fansly上のユーザー固有ID
- `char_id` (TEXT NOT NULL)
- `username` (TEXT NOT NULL)
- `current_tier` (TEXT DEFAULT 'free_follower'): 現在加入中のサブスクプランID
- `total_spent_usd` (REAL DEFAULT 0.0): 累計課金額（動的プライシング判定基準）
- `psych_profile_json` (TEXT DEFAULT '{}'): AIがリアルタイム更新する顧客カルテ（好み・フェチ・心理タイプ・予算感・反応の良い話題）
- `sales_phase` (TEXT DEFAULT '1_Icebreak'): `1_Icebreak` / `2_Explore` / `3_Lead_Pitch` / `4_Lock_Close` / `5_Cooldown` / `6_Low_Priority`
- `consecutive_unanswered` (INTEGER DEFAULT 0): 連続未読スルー回数
- `consecutive_ppv_ignored` (INTEGER DEFAULT 0): 連続PPV未購入スルー回数
- `is_low_priority` (INTEGER DEFAULT 0): タダ乗り客省エネ降格フラグ（1=省エネモード）
- `is_escalated` (INTEGER DEFAULT 0): 緊急停止・要介入アラートフラグ（1=AI自動返信停止中）
- `escalation_reason` (TEXT): 停止理由（危険ワード検知、連続スルー等）
- `is_muted` (INTEGER DEFAULT 0): 永久ミュートフラグ
- `ab_group` (TEXT DEFAULT 'A'): A/Bテスト振り分けグループ（`A` or `B`）
- `last_contact_at` (TEXT): 最終メッセージ日時（ISO8601）
- `created_at` (TEXT)
- `PRIMARY KEY (fan_id, char_id)`

### 2.4 `dm_messages`（DM会話履歴）
- `msg_id` (INTEGER PK AUTOINCREMENT)
- `fan_id` (TEXT NOT NULL)
- `char_id` (TEXT NOT NULL)
- `sender` (TEXT NOT NULL): `fan` / `ai` / `human_manual`
- `content` (TEXT NOT NULL)
- `sales_phase` (TEXT): 送信時の営業フェーズ
- `prompt_version` (TEXT): 使用した営業プレイブックバージョン（A/Bテスト効果測定用）
- `created_at` (TEXT NOT NULL)

### 2.5 `ppv_transactions`（PPV提案・購入・チップ履歴）
- `tx_id` (INTEGER PK AUTOINCREMENT)
- `fan_id` (TEXT NOT NULL)
- `char_id` (TEXT NOT NULL)
- `ppv_id` (TEXT): `vault_index.json` 内の素材ID（チップやサブスクの場合はNULL可）
- `tx_type` (TEXT NOT NULL): `ppv` / `tip` / `sub_upsell`
- `offered_price` (REAL NOT NULL): 動的プライシングで実際に提示した金額（USD）
- `is_unlocked` (INTEGER DEFAULT 0): 0=未開封/未購入, 1=開封/購入済み
- `reminder_sent` (INTEGER DEFAULT 0): 0=FOMOリマインド未送信, 1=送信済み
- `offered_at` (TEXT NOT NULL)
- `unlocked_at` (TEXT)

### 2.6 `sales_playbooks`（自己進化型営業ルール・A/Bテスト管理）
- `version_id` (INTEGER PK AUTOINCREMENT)
- `char_id` (TEXT NOT NULL)
- `rules_text` (TEXT NOT NULL): バッチ分析で抽出された営業鉄則プロンプト
- `ab_group` (TEXT NOT NULL): `A`（現行本番ルール） / `B`（新検証ルール）
- `cvr_score` (REAL DEFAULT 0.0): 成約率スコア
- `is_active` (INTEGER DEFAULT 1)
- `created_at` (TEXT NOT NULL)

---

## 3. 共通キャラクターマスター仕様（`shared_assets\characters\<char_id>\`）
- **`dm_persona.json`**: キャラ表示名、使用言語（英語）、口調・絵文字スタイル、一人称、顧客の呼び方、NGトピック、営業スタンス。
- **`plans.json`**: サブスクプラン階層（`free_follower`, `silver_vip`, `gold_goddess` 等）、各プランの価格・閲覧可能コンテンツ範囲・上位プランへのアップセル訴求文句。
- **`settings.json`**:
  - `default_prices`: 階段レベル（Step 1 / Step 2 / Step 3）ごとのデフォルト価格マスター（基本価格 `base_price_usd`、お試し割引価格 `tripwire_price_usd`、太客用価格 `whale_price_usd`）。
  - `sleep_schedule`: 睡眠時間機能の設定。**初期値は `enabled: false`, `start: "19:00"`, `end: "22:00"` (JST)**（北米西海岸 PST 02:00〜05:00 / 東海岸 EST 05:00〜08:00 に相当し、西海岸の深夜2時までの課金ピークを逃さない3時間設定）。
- **`ppv_vault\vault_index.json` ＋ メディアファイル**:
  - 各素材に `ppv_id`, `filename`, `media_type` (`video`/`image`), `ladder_step` (`1`/`2`/`3`), `base_price_usd`, `tripwire_price_usd`, `whale_price_usd`, `tags`, `pitch_hint`, `is_active`, `created_at` を保持。

---

## 4. コア営業ロジック＆課金直結ギミック

### 4.1 役割分担と全キャラ同時並行稼働
- **新規フォロワー初回挨拶**: Fansly標準の Welcome Message 機能に委譲し、返信してきた顧客を本システムがキャッチして営業を開始する。
- **全キャラ同時並行稼働**: 登録済みおよび今後追加される全キャラクターを、キャラごとに完全に独立したPlaywrightブラウザプロファイル（`profiles\<char_id>`）で同時並行稼働させる。
- **PPV送信方式**: PCローカルの `ppv_vault\` フォルダ内の動画/画像ファイルをPlaywrightでDMに直接アップロードし、ロック価格を設定して送信する。

### 4.2 S.E.L.L. 営業フェーズ遷移（ステートマシン）
ローカルLLM（Ollama / Qwen3系）に自由演技をさせず、以下の営業フェーズに従って応答を生成させる。
1. **`1_Icebreak`（警戒解除）**: 相手の最初のメッセージやプロフィールに共感し、キャラ口調で楽しく雑談（1〜2往復）。
2. **`2_Explore`（ニーズ探り）**: 相手の好み・フェチ・求めているシチュエーションを自然に聞き出し、フックを投下。
3. **`3_Lead_Pitch`（提案誘導）**: 熱量が高まったタイミングで、顧客の現在加入プラン（`plans.json`）を踏まえつつ、未購入の秘蔵PPV（`ppv_vault`）または上位サブスクプランへのアップセルを自然に提案。
4. **`4_Lock_Close`（クロージング）**: 買う理由・特別感・希少性を提示してロック付きメディアを送信。購入検知後は感謝を伝え次の階段へ繋ぐ。
5. **`5_Cooldown`（冷却期間）**: 断られた・購入直後で一息つくフェーズ。押し売りを避けて雑談へ戻す。
6. **`6_Low_Priority`（タダ乗り客省エネ降格モード）**: **「無料フォロワー（`free_follower`）かつ PPVオファーを2回連続未購入スルーした顧客」**を自動降格。LLMによる長文営業生成を停止し、数時間おきに超短文の相槌のみを返して計算リソース（GPU/時間）を課金見込み客へ集中させる。

### 4.3 リアルタイム顧客カルテ（長期記憶レイヤー）
- 生の会話ログを毎回大量にプロンプトへ注入するとローカルLLMの精度が低下（文脈ボケ）するため、会話のたびにバックグラウンドで顧客の好み・心理タイプ・予算感を構造化JSON（`psych_profile_json`）へ抽出・更新し、次回応答生成時はこの「顧客カルテ」をシステムプロンプトへ注入する。

### 4.4 課金直結セールスギミック
1. **動的プライシング（Dynamic Pricing）**:
   - 初課金前（`total_spent_usd == 0`）の客には `tripwire_price_usd`（お試し割引価格）を提示し、初課金の心理的ハードルを突破させる。
   - 通常層には `base_price_usd` を提示する。
   - リピーター・太客（`total_spent_usd >= 50.0` または最上位プラン加入者）には `whale_price_usd`（高単価設定）で特別オファーを行う。
2. **階段式PPV（PPV Ladder）**:
   - Step 1（導入）→ Step 2（本命）→ Step 3（超秘蔵）の順にエスカレーション販売を行う。
   - Step 1購入を検知した顧客に対し、30分〜数時間後の熱量が高いうちに Step 2（続編・より過激な秘蔵素材）を自動追撃提案する。同一顧客への既購入PPVの再送信は厳禁とする。
3. **未開封リマインド（FOMO追撃）**:
   - PPV送信後6〜12時間経過しても未開封（`is_unlocked == 0`）かつリマインド未送信（`reminder_sent == 0`）の顧客に対し、「恥ずかしくなってきたから今日中に消しちゃうかも…」等の希少性（FOMO）を煽る軽めの追撃メッセージを1回だけ自動送信する。

### 4.5 人間味演出＆安全装置（ガードレール）
- **ランダム遅延＆タイピング演出**: 受信検知後、文字数に応じた既読遅延（1〜5分）＋タイピング中演出（10〜30秒）を挟んで送信する。
- **睡眠時間制御**: `settings.json` の睡眠時間が `ON` の場合、指定時間帯（初期値 `19:00〜22:00` JST）は返信を保留し、起床時刻後に順次返信を再開する。
- **緊急停止エスカレーション**: 「refund」「scam」「bot」「AI」「minor」等の危険ワード検知時、または連続2回未読スルー時はその顧客へのAI自動返信を即座にロック（`is_escalated = 1`）し、管理画面にアラートを表示する。

---

## 5. Web管理コンソールUI仕様（ポート `5051` / ダークモード基調）

### 5.1 共通ヘッダー（全タブ常駐）
- **左側（稼働制御＆キャラ切替）**:
  - **`[▶ 全キャラ一括稼働 / ⏹ 全停止]`** マスターボタン。
  - **展開式ハンバーガー／ドロップダウンメニュー**: クリックで全キャラクター一覧が展開し、表示対象キャラの切り替えと、**各キャラ名のすぐ横にある個別稼働ON/OFFトグルスイッチ**（睡眠中は `💤` バッジ表示）を操作可能。
- **右側（クイックアクション）**:
  - **`[⚙️ 設定]`** ボタン（設定モーダル起動）。
  - **`[＋ PPV素材追加]`** ボタン（PPV登録モーダル起動）。

### 5.2 4タブ構成
1. **タブ1：営業モニタリング（メイン司令室）**
   - 上段：本日のDM売上、PPV成約率（CVR）、対応中顧客数、省エネ降格顧客数、要介入アラート件数のサマリーカード。
   - 中段：緊急停止・要介入アラート枠（`[手動対応済みにする（AI再開）]` / `[この客を永久ミュート]` ボタン付き）。
   - 下段：**左右2ペインのチャットタイムライン**（今リアルタイムで会話していなくても直近スレッド履歴が常時残る）。
     - 左ペイン：全キャラ横断または選択キャラの顧客スレッド一覧（最新メッセージ順に自動ソート）。
     - 右ペイン：選択スレッドの会話ログ、AI顧客カルテ表示、および**日本語入力→ローカルLLMによる英語＋キャラ口調自動変換＆直接介入送信ボックス**。
2. **タブ2：PPV武器庫一覧（ギャラリー専用）**
   - ドロップ受付枠は置かず、登録済みPPVの一覧管理に徹する。
   - 上部フィルタ・ソートバー：階段レベル絞り込み（`すべて` / `Step 1` / `Step 2` / `Step 3`）、メディア種別スイッチ（`すべて` / `🎬 動画` / `🖼️ 画像`）、ソート機能（`登録日新しい順` / `登録日古い順` / `提案回数順` / `購入回数順` / `売上金額高い順`）。
   - コンテンツエリア：**サムネイルの下に4要素（階段レベル・価格3種・タグ・売り込みヒント）と実績（提案回数・購入回数・売上金額USD）をまとめた1つのカードボックス**をレスポンシブグリッドで縦横に配置。各カードで公開ON/OFF・編集・削除が可能。
3. **タブ3：キャラ・プラン設定**
   - `dm_persona.json`（口調・一人称・NGルール）と `plans.json`（サブスクプラン階層・閲覧範囲・アップセル誘導文句）をGUIフォームで編集・保存。
4. **タブ4：営業シミュレーター**
   - テスト顧客条件（加入プラン・累計課金額・購入履歴）を設定し、本番と同じ営業ロジック・動的プライシング・フェーズ遷移・顧客カルテ更新を試し打ちできるデバッグ画面。

### 5.3 2モーダル仕様
1. **`[＋ PPV素材追加]` モーダル**:
   - 画像/動画のドラッグ＆ドロップ受付エリア。
   - ドロップ後、プレビューサムネイルとともに以下を表示：
     - **階段レベル選択**（`Step 1: 導入` / `Step 2: 本命` / `Step 3: 超秘蔵` ※手動選択の主対象）。
     - **価格3種**（`settings.json` のデフォルト価格マスターから階段レベルに応じた初期値を自動セット・手動変更可）。
     - **タグ**（Ollamaによる自動入力済み・手動修正可）。
     - **AIへの売り込みヒント（英語 `pitch_hint`）**（Ollamaがキャラ口調に合わせて自動生成・入力済み、再生成ボタン＆手動修正可）。
2. **`[⚙️ 設定]` モーダル**:
   - Step 1〜3の初期価格マスター設定（基本・割引・太客）。
   - 睡眠時間設定（ON/OFFスイッチ・開始終了時刻、初期値 `OFF` / `19:00`〜`22:00`）。
   - Fanslyアカウント連携（キャラ別ブラウザプロファイルでの手動ログイン＆セッション保存用ブラウザ起動ボタン）。

---

## 6. 開発フェーズ計画
- **Phase 1（v1.0 コア構築・最優先）**:
  1. 統合DB（`empire_core.db` WALモード）＆共通キャラクターマスター連携。
  2. 完成版Web管理コンソールUI（4タブ＋2モーダル）＆PPV武器庫ミニアプリ。
  3. S.E.L.L.営業エンジン、リアルタイム顧客カルテ、動的プライシング、階段式PPV選定、省エネ降格ロジック、営業シミュレーター。
  4. Playwright全キャラ同時並行DM監視・送信ボット、人間味遅延、睡眠時間制御、未開封FOMOリマインドワーカー。
- **Phase 2（v1.1 自己進化・LTV最大化拡張）**:
  1. 日次/週次バッチによる「売れた/逃げられた会話の比較分析」および「A/Bテスト式プロンプト自動昇格エンジン（`sales_playbooks`）」。
  2. 会話中のチップ（投げ銭）誘導フック＆休眠ファン掘り起こし個別DMバッチ。