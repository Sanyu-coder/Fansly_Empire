# 2026-11-18 v1.4 S.E.L.L.厳格化・文字数比例待機アルゴリズム・PPV/チップ誤判定根絶・手動PPVダイナミック送信・PPV武器庫2カラムUI刷新レポート

## 1. 概要・日付・対象ファイル
- **作業日**: 2026-11-18
- **対象アプリ**: `AI_DM_Sales`（ポート `5051`）
- **変更・追加ファイル**:
  - `app/services/sales_engine.py`（新規未課金ファンの1〜2往復目PPV飛び級防止ハードガード、`5_Cooldown` 急かし文言の厳格禁止＆浄化ガード、16種絵文字パレット＆直前使用絵文字の連続重複回避、最終英文検査 `verify_tip_request_in_reply` によるチップ誘導誤記録の完全防止）
  - `app/core/database.py`（`system_settings` への `short_msg_chars_threshold` / `long_msg_chars_threshold` 追加、開封照合15秒経過＆未開封存在ガード `has_eligible_locked_ppv_for_unlock`、既存誤判定トランザクション自動修復 `repair_false_positive_transactions`）
  - `app/services/character_loader.py` & `shared_assets/characters/*/dm_persona.json`（文字数基準設定統合、豊かな絵文字パレットと非催促スタンスへの刷新）
  - `app/services/fansly_dm_bot.py`（受信文字数比例＋±15%ランダムゆらぎ待機秒数算出 `_compute_human_reply_delay_sec` ＆ `decision_log_json` 記録、最新添付バブル限定の厳格DOM開封判定、実送信完了時のみのチップDB記録、手動PPVカスタム価格のPlaywright送信キュー引き渡し）
  - `app/routes/vault_routes.py`（文字数基準2項目を含む返信待機設定の取得・バリデーション・保存）
  - `app/routes/dashboard.py`（顧客カルテ連動ダイナミックプライシング候補API `GET /api/monitoring/ppv_candidates`、手動PPV＋カスタム価格送信API強化）
  - `app/templates/index.html`（手動PPV選択＋ミニサムネ＋ダイナミック価格プリセットUI、設定モーダルの文字数基準入力欄＆バリデーション、タブ2 PPV武器庫の「左右黒帯ゼロ・左サムネ＋右編集」2カラムコンパクトカード＆完全レスポンシブCSS）
  - `tests/test_phase5_sell_delay_ppv_and_ui.py`（v1.4 全6項目統合テスト＆全ファイル `py_compile` 検証スクリプト）
  - `PROJECT_MAP.md`, `docs/HANDOVER.md`, `docs/USER_MANUAL.md`

---

## 2. 背景・解決したかった課題（Before）
1. **営業タイミングの早すぎ・急かし・単調な絵文字**: 会話1〜2往復目の新規ファンに対してアイスブレイクなしにPPV提案へ飛び級したり、PPV送信直後に急かすような文面が出たり、毎回 `🖤` や `😈` ばかりの機械的な絵文字になっていた。
2. **受信文量と待機時間のミスマッチ**: 一言の「Hey」に対しても長文の相談に対しても似たランダム待機になりやすく、受信文字数の基準（何文字以下なら最短・何文字以上なら最長か）を管理画面から調整できなかった。
3. **PPV「購入済み」誤判定 ＆ チップ「誘導済み」誤記録**:
   - 本番ログ・DB調査により、PPV送信2秒後にページ全体セレクタ `document.querySelector('... .purchased-avatar:not(.not-purchased)')` が誤反応して `is_unlocked=1` になっていたこと、および `5_Cooldown` 滞在時に返信送信前の段階で無条件に `tx_type='tip'` が先行INSERTされていた根本原因を特定した。
4. **手動介入時のPPV価格最適化**: モニタリング画面から手動でPPVを送る際、相手の予算層に応じたダイナミックプライシング推奨価格やプリセット切り替え、サムネイル確認をその場で行えなかった。
5. **PPV武器庫一覧の左右黒帯と縦長カード**: 横長枠に縦長動画/画像を `contain` 表示していたため左右に大きな黒帯余白が生じ、1カードの縦幅も長く一覧性が低かった。

---

## 3. 採用した設計方針・理由（Why / ADR）
- **二重ガードによるS.E.L.L.規律保証**: プロンプト指示だけに頼らず、Pythonのステートマシン（`determine_next_phase` / `process_fan_message`）側で「未課金・無料フォロワーかつ `turn_count < 3` の間は `1_Icebreak` → `2_Discovery` に固定し `recommended_ppv = None` にする」ハードガードと、`5_Cooldown` 返信文の催促表現サニタイズ処理を併用することで100%確実に押し売り・急かしを排除した。
- **DOM局所検査＋経過秒数ガード＋事後テキスト検証による誤判定ゼロ化**:
  - PPV開封判定は「直近の自分の添付バブル1件に購入者アバターが存在し、かつロックアイコン・未購入クラス・価格タグが完全消滅しており、さらにDB上で提案から15秒以上経過した未開封レコードがある場合のみ」に限定した。
  - チップ誘導は「LLMが生成した最終英文に `tip` / `spoil me` 等の明確な要求表現が含まれ、かつ実際にボットが送信完了した瞬間のみ」DB記録する方式へ刷新し、既存DBの誤判定レコードも `repair_false_positive_transactions()` で自動修復した。
- **左縦長カバーサムネ＋右4列価格統合による一覧性最大化**:
  - 縦長比率が多いFansly素材に合わせ、カードを `196px + 1fr` の2カラムグリッドにして左枠を `object-fit: cover; object-position: center top;` とすることで左右の黒帯を完全消滅させ（全体確認はワンクリックの拡大ライトボックスで担保）、1600px超の大画面では2〜3列が美しく並ぶレスポンシブ設計とした。

---

## 4. 実装内容・変更点サマリ（After）
1. **S.E.L.L.厳格化・非催促・絵文字16種パレット**: 新規客1往復目=`1_Icebreak`、2往復目=`2_Discovery`、3往復目以降=`3_Lead_Pitch`解禁を徹底。直前AI返信の絵文字を自動抽出して連続重複を防止。
2. **受信文字数比例の返信待機アルゴリズム**: `short_msg_chars_threshold`（初期20文字）〜 `long_msg_chars_threshold`（初期150文字）で線形補間＋±15%ゆらぎ＋クランプを実装し、算出根拠を検討ログ（`reply_delay_reasoning`）と設定モーダルUIへ統合。
3. **PPV/チップ誤判定根絶＆DB整合性修復**: 未購入誤ロック解除と幽霊チップ記録を根絶し、`empire_core.db` 内の既存誤判定データも自動正常化。
4. **手動PPV送信（ダイナミックプライシング連動）**: `GET /api/monitoring/ppv_candidates` で顧客カルテ連動の推奨価格を算出し、ミニサムネイル・4種価格プリセット・自由金額入力からPlaywright送信キューへ直結。
5. **PPV武器庫コンパクト2カラムUI**: 左右黒帯ゼロの横並び2カラムカード（狭幅680px以下では自動で縦積み切替）へ刷新。

---

## 5. 作業中に発生した問題・ハマりポイントと解決策（Troubleshooting）
- **問題**: `fansly_dm_bot.py` の `page.evaluate` 内JavaScript正規表現 `/unlocked\s+for/` が通常のPython三連引用符内で Python 3.12 の `SyntaxWarning: invalid escape sequence '\s'` を出した。
  - **解決策**: JavaScriptコード文字列を raw string `r"""() => { ... }"""` に変更し、警告ゼロでJS側にも正確なバックスラッシュが渡るように修正した。

---

## 6. 今後の注意点・残タスク
- サーバー再起動時は複数の監視ブラウザプロセスが競合しないよう、既存の `run_dm_sales.py` プロセスを確実に終了してから単一インスタンスで起動すること（本タスクの再起動手順でも徹底済み）。
