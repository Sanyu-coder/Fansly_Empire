- [ ] TICKET-09: [UI・文言微調整] 営業モニタリング画面の未返信絞り込み・タグソート・監視インジケーター一括実装 【実行モデル：local-long (Qwen 3.6 128K)】
対象ファイル（確定）

app/templates/index.html（4393行）

app/routes/dashboard.py（1148行 ※必要時のみスレッド属性追加）
現状の課題と達成したい目的

未返信スレッドの即時抽出、タグ・段階別の絞り込みと並び替え、およびヘッダーでのDM監視稼働アニメーション表示を実装し、モニタリング画面の視認性と対応速度を向上させる。

具体的な改修要件・例外処理のポイント

上部サマリーへの「未返信件数」追加＆ワンクリック絞り込み：summary-cards-row に「未返信件数」カード（またはバッジ）を追加し、クリックすると左ペインのスレッド一覧を「未返信のみ」にトグル絞り込みできるようにすること。

スレッド一覧のフィルター＆ソートバー：スレッド一覧ヘッダー直下に、キャラ名・プラン・S.E.L.L.フェーズ・未返信状態での絞り込み、および「未返信優先 / 新着順 / 課金額順」の並び替えセレクトをコンパクトに配置すること。

ヘッダー監視アニメーションインジケーター　　　　　　　　：top-header の空きスペースに横幅を取らないコンパクトな監視インジケーター（稼働中は電波パルスCSSアニメーション、停止中はグレー表示）を追加すること。
必ず、DMの監視機能が正常動作（もし届いたらアプリ側へ持ってきて返信作成という通常の動きができているか否か）しているかどうか2連動してアニメーションを表示して

完了条件

未返信件数クリックでの抽出、フィルター・ソート機能、およびヘッダーの電波アニメーション付き監視インジケーターがすべて正常に動作すること。


•
編集
•
削除

sano.yuichi.an@gmail.comさんがコメントしました。2026/11/22 5:25

- [ ] TICKET-05: [新機能追加] 営業シミュレーターのPPV選定・擬似送信API統合 【実行モデル：build (Qwen 3.8 32K)】
対象ファイル（確定）

app/routes/dashboard.py（1148行 ※@dashboard_bp.route("/api/simulator/reply", methods=["POST"]) 周辺のみ）

現状の課題と達成したい目的

営業シミュレーターでS.E.L.L.ステージが提案・クロージング段階（3_Lead_Pitch / 4_Lock_Close 等）に到達した際、本番と同様にPPV動画・画像と動的価格が選定されてレスポンスに返るようにする。

具体的な改修要件・例外処理のポイント

app/routes/dashboard.py の /api/simulator/reply 処理において、sales_engine.py が返す推薦PPV・ダイナミックプライシング（tripwire / base / whale）結果から、実際のFansly外部送信やDB売上集計を行わずに sent_ppv_attachment オブジェクト（ppv_id, char_id, filename, media_type, media_url, offered_price_usd, step_label, is_unlocked: 0）を組み立てて返却すること（app/templates/index.html 側の既存 renderMessagePpvAttachmentHtml がそのまま描画できる形式に合わせること）。

完了条件

/api/simulator/reply を叩いて提案フェーズに到達した際、キャラクターの ppv_vault から適切なPPV素材と価格が選定され sent_ppv_attachment として返却されること。