# AI Factory プロジェクト全体構造マップ

## 📂 ディレクトリ構成と役割

```text
C:\Fansly_Empire/
├── shared_assets/                    # 全アプリ共通アセットディレクトリ (環境変数 SHARED_ASSETS_DIR で上書き可能)
│   └── characters/                   # キャラクターごとのデータストレージ (CHARACTERS_ROOT)
│       └── <char_id>/
│           ├── profile.json          # キャラクター設定・プロンプト・使用ワークフロー
│           ├── 01_candidates/        # Step 1 採用待ち（未選別）フォルダ
│           ├── 02_ready_to_post/     # Step 3 予約確定キュー（投稿待ち）
│           ├── 03_posted/            # 投稿済みアーカイブ
│           ├── 99_rejected/          # ボツ・アーカイブ
│           └── browser_profile/      # キャラ別Fanslyログインセッション
│
└── AI_Factory/
    ├── app/                          # Flask Webコンソール アプリケーション
    │   ├── routes/                   # Blueprint ルーティング
    │   │   ├── character_routes.py   # キャラクター管理・設定API
    │   │   ├── main_routes.py        # コンソールトップ画面配信
    │   │   ├── media_routes.py       # 画像・動画メディア配信API
    │   │   ├── mosaic_routes.py      # NudeNet検出・画像/動画2レイヤーモザイクAPI
    │   │   ├── queue_routes.py       # 候補管理・Step1/Step2遷移・タイムライン・予約管理API
    │   │   └── worker_routes.py      # 工場ワーカー（Line1/Video/Line2）起動・停止・進捗API
    │   ├── services/                 # バックエンド業務ロジック
    │   │   ├── character_service.py  # プロファイル・パス解決
    │   │   ├── file_service.py       # ファイル/画像/動画の取得・操作
    │   │   ├── mosaic_service.py     # OpenCV/PIL陰部モザイク・動画2層モザイク合成
    │   │   ├── comfy_progress.py     # ComfyUI WebSocketリアルタイム進捗取得＆構造化シグナル出力
    │   │   └── worker_service.py     # サブプロセス管理・リアルタイム進捗シグナル受信
    │   ├── templates/
    │   │   └── index.html            # SPAコンソールUI（3ステップ＋進捗モニターバー刷新）
    │   ├── config.py                 # パス・定数・環境設定 (SHARED_ASSETS_DIR / CHARACTERS_ROOT)
    │   └── __init__.py               # Flaskアプリ初期化
    │
    ├── workflows/                    # ComfyUI APIワークフロー定義 (JSON)
    │   ├── CF T2I (LLM) API.json     # 第1ライン用 画像生成
    │   ├── アップスケーラー API.json # 第1ライン用 高画質化 (HQ)
    │   └── I2V LLM (Wan2.2).json     # 動画生成用 I2V
    │
    ├── factory_line1.py              # 第1ライン: 企画立案・ComfyUI画像生成・高画質化バッチ
    ├── factory_video.py              # 動画バッチ: Step 2 動画一括生成 (Wan2.2)
    ├── factory_line2.py              # 第2ライン: Fansly自動スケジュール予約投稿 (Playwright)
    ├── console_app.py                # Webサーバー起動スクリプト (Port: 5050)
    ├── console_app_起動.bat          # Webサーバー起動用バッチ
    └── docs/                         # 開発タスク振り返り・設計判断レポート (ADR)
```

## 🔄 ワーカー稼働・進捗管理アーキテクチャ
- **バックエンド (`app/services/worker_service.py`)**:
  - `worker_state` により `total_items`, `completed_items`, `item_progress`, `current_stage`, `running`, `logs` を集中管理。
  - `factory_line1.py`, `factory_video.py`, `factory_line2.py` の標準出力を正規表現でパースし、各アイテムの0%〜100%進行度および全体件数進捗をリアルタイム更新。
- **フロントエンド (`app/templates/index.html`)**:
  - 画面最下部モニター（`#log-drawer`）をコマンドプロンプト風から「2段階直感プログレスバー」に刷新。
  - 全体進捗バッジ＆小型バー ＋ 現在1件の横長ゲージ（シマー光彩付き） ＋ 1行フェーズ表示 ＋ 生ログ折りたたみトグル。
