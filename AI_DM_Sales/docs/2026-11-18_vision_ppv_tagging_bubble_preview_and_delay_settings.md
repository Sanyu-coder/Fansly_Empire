# 2026-11-18 v1.3 Vision自動タグ・売り込みメモ生成、シミュレーター検討ログ統一、PPV吹き出し＆ホバー詳細、返信待機時間設定 実装レポート

## 1. 概要・日付・対象ファイル
- **作業日**: 2026-11-18
- **対象アプリ**: `AI_DM_Sales`（ポート `5051`）
- **変更・追加ファイル**:
  - `app/core/database.py`（`ppv_vault` テーブル・`system_settings` テーブル新設、`dm_messages` への `ppv_id` / `ppv_price_usd` / `tx_id` 自動マイグレーション、メッセージへの `ppv_attachment` 解決付与、返信待機秒数設定CRUD）
  - `app/core/llm_client.py`（ローカルOllama Visionモデル優先自動検出＆未導入時の自動プル、OpenCV/FFmpegによる動画代表フレーム抽出＆画像リサイズBase64化、日本語属性タグ＆日本語売り込みメモ自動生成 `analyze_ppv_media_with_vision`）
  - `app/services/character_loader.py`（`settings.json` への `min_reply_delay_sec` / `max_reply_delay_sec` 統合、`vault_index.json` への `tags_json` / `description_memo` / `pitch_note` 追加と `ppv_vault` DB双方向同期）
  - `app/services/sales_engine.py`（顧客カルテの好み・フックとPPV日本語タグ・売り込みメモの照合スコアリング選定、システムプロンプトへの売り込みメモ注入、検討ログへの詳細出力）
  - `app/services/fansly_dm_bot.py`（`system_settings` / `settings.json` の最小〜最大待機秒数に基づくランダム返信待機カウントダウン算出、AI返信時の `dm_messages` へのPPV・トランザクションID紐付け）
  - `app/services/followup_worker.py`（Step 2 自動追撃送信時の `dm_messages` PPV紐付け）
  - `app/routes/vault_routes.py`（`POST /api/vault/analyze_vision` 新設、アップロード時の自動Vision解析、設定保存APIの返信待機時間対応）
  - `app/routes/dashboard.py`（シミュレーターAPIへの `decision_log` / `sent_ppv_attachment` / `ab_group` 出力統一、手動介入送信時の任意PPV添付対応）
  - `app/templates/index.html`（`[✨ AIでタグ・メモを自動生成]` ボタン、シミュレーター検討ログアコーディオン、トーク吹き出しPPVサムネイル＆ホバーツールチップ、PPVファイル名クリックによるタブ2該当素材ジャンプ＆ハイライト、検討ログスクロール＆縦リサイズ、設定モーダルの返信待機時間範囲UI）
  - `tests/test_phase4_vision_ui_and_delay.py`（v1.3 全6項目統合テスト＆全ファイル `py_compile` 検証スクリプト）
  - `PROJECT_MAP.md`, `docs/HANDOVER.md`, `docs/USER_MANUAL.md`

---

## 2. 背景・解決したかった課題（Before）
1. **PPV登録時の属性設定コストと情報量不足**: これまでPPV追加時はファイル名ベースで英語の簡易タグ・誘い文句を生成するのみであり、画像や動画に実際に何が映っているか（衣装・色・場所・ポーズ・雰囲気）をAIが視覚的に読み取って日本語の属性タグや「どう煽ると売れるか」の売り込みメモを自動付与する仕組みが無かった。
2. **シミュレーターと本番モニタリングの情報格差**: 営業シミュレーター（タブ4）でテスト対話を行う際、`api_simulator_reply` が `decision_log` を返却しておらず、本番画面と同じ展開式アコーディオンで「なぜこの回答・価格・PPVを選んだか」を検証できなかった。
3. **トーク画面での送信済みPPVの視認性**: AIまたは手動でPPVを送信した際、吹き出しにはテキストしか表示されず、どの画像/動画をいくらで送り、今「未購入」なのか「購入済」なのかを一目で確認できなかった。
4. **PPV提案履歴から素材編集への導線**: 右下のPPV提案履歴等から該当素材の価格やメモを調整したい時、手動でタブを切り替えて目視で探す必要があった。
5. **検討ログの長文化による見切れ**: `🧠 AIカルテ根拠・返答検討ログを見る` を開いた際、過去ターン履歴を含めると縦に長くなりスクロールや高さ調整ができなかった。
6. **返信待機時間の固定化**: DM検知後の返信待機時間（60〜300秒）がコード内ハードコードだったため、時間帯や運用方針に合わせて画面から柔軟に遅延範囲を調整できなかった。

---

## 3. 採用した設計方針・理由（Why / ADR）
- **ローカルOllama Visionモデルの優先自動検出＋OpenCV/FFmpegハイブリッド動画フレーム抽出**:
  - `/api/tags` および `ollama list` から `qwen2.5vl` / `qwen3-vl` / `qwen2-vl` / `minicpm-v` / `llava` 等を優先順で自動検出し、未導入環境では `ollama pull` で自動導入する設計を採用。
  - 動画ファイルは冒頭0秒目が暗転しているケースが多いため、`cv2.VideoCapture` で全体の25%・50%・12%地点の輝度を確認して鮮明な代表1フレームを抽出（失敗時は `ffmpeg` へ自動フォールバック）し、長辺960px以下にリサイズして渡すことでVRAM消費と推論時間を最小化した。
  - データは既存の `vault_index.json` と新設した `empire_core.db` 内 `ppv_vault` テーブル（`tags_json`, `description_memo`, `pitch_note`）の双方へ自動同期し、後方互換とDBクエリ性の両立を実現した。
- **固定座標フローティングカードによるホバー詳細表示**:
  - チャットタイムラインは `overflow-y: auto` コンテナ内にあるため、CSSの相対ツールチップでは上下左右で見切れる問題が起きる。そこで普段は吹き出し内にコンパクトなサムネイルとロック価格バッジのみを描画し、マウスオーバー時のみビューポート端を自動回避する固定座標フローティングカード（`#ppvHoverFloatingCard`）を表示する設計とした。
- **返信待機秒数の `system_settings` DB ＆ `settings.json` 二重保持**:
  - システム全体の共通設定テーブル `system_settings` とキャラ別 `settings.json` の双方に `min_reply_delay_sec` / `max_reply_delay_sec` を保持し、`calculate_human_delays()` が必ず指定された `[min, max]` 秒の範囲内でカウントダウン秒数を決定するように統一した。

---

## 4. 実装内容・変更点サマリ（After）
1. **Vision LLMによる日本語「タグ ＆ 売り込みメモ」自動生成**:
   - 新規PPV追加モーダルでファイル選択時、および既存PPVカードの **`[✨ AIでタグ・メモを自動生成]`** ボタン押下時に `qwen2.5vl:7b` 等のローカルVisionモデルで素材を自動解析し、日本語属性タグと日本語売り込みメモをUIおよび `ppv_vault` DBへ自動反映。`SalesEngine` も顧客カルテの好みと日本語タグの一致度をスコアリングして最適PPVを選定。
2. **営業シミュレーターの完全統一**:
   - 本番と同一の `SalesEngine` + `profiler_service` + `sales_playbooks` パイプラインから `decision_log` と `sent_ppv_attachment` を返却し、シミュレーター右ペインにも共通の **`🧠 AIカルテ根拠・返答検討ログを見る`** アコーディオンを実装。
3. **トーク吹き出し内PPVサムネイル／動画プレイヤー ＆ ホバー詳細ツールチップ**:
   - AI自動送信・追撃送信・手動介入PPV添付送信のすべてで吹き出し内にサムネイル／動画（`🔒 $XX.XX PPV` / `🔓 $XX.XX 購入済` バッジ付き）を表示。ホバー時に「ファイル名・段階・価格・タグ・売り込みメモ・開封状況」をフローティング表示。
4. **PPV提案ファイル名クリックで該当素材カードへ直接ジャンプ**:
   - 右下の「💎 PPV提案・開封履歴」、吹き出し内PPVフッター、手動添付プレビュー、シミュレーター推薦欄のファイル名リンクをクリックすると、即座にタブ2へ切り替わり該当カードへスムーズスクロール＋発光パルス強調表示。
5. **AI検討ログパネルの縦スクロール＆高さリサイズ対応**:
   - `.decision-accordion-body` に `max-height: 360px; overflow-y: auto; resize: vertical;` を設定し、右下ドラッグ時には上限を解放して自在に高さを広げられるように改善。
6. **設定モーダルからの返信待機時間（ランダム遅延範囲）変更**:
   - `[⚙️ 設定]` モーダル内に「⏱️ AI返信待機時間（人間味ランダム遅延範囲）」を追加し、`min_reply_delay_sec` 〜 `max_reply_delay_sec`（デフォルト 60〜300秒）を自由に変更・保存してボットのカウントダウンへ即時反映。

---

## 5. 作業中に発生した問題・ハマりポイントと解決策（Troubleshooting）
- **問題1**: `app/core/database.py` に `normalize_reply_delay_bounds(...) -> Tuple[int, int]` を追加した際、`typing.Tuple` が未インポートで `NameError` が発生した。
  - **解決策**: `from typing import Any, Dict, List, Optional, Tuple, Union` に修正し、全20ファイルの `py_compile` と統合テストで解消を確認した。
- **問題2**: Ollama Visionモデルによって `"think": False` パラメータを受け付けるモデル（Qwen3-VL系）と非対応のモデル（Qwen2.5-VL / LLaVA系）が混在する点。
  - **解決策**: `_call_vision_generate()` でモデル名判定および HTTP 400 時の自動フォールバックリトライを実装し、どのローカルVisionモデルでもエラーなくJSON出力を取得できるようにした。

---

## 6. 今後の注意点・残タスク
- 初回のみVisionモデル（`qwen2.5vl:7b` 等、約6GB）をVRAMへロードする際に数秒〜十数秒かかる場合があるため、UI側ではボタンの無効化と `⏳ Vision解析中...` バッジ表示で二重送信を防止している。
- 将来 `AI_Analytics_Console`（ポート5052予定）からPPV別売上分析を行う際も、`empire_core.db` の `ppv_vault` テーブル（`tags_json`, `description_memo`）と `ppv_transactions` を直接JOINして「どの属性タグのPPVが最も売れているか」を分析可能となっている。
