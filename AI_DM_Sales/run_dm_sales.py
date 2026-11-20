import atexit
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from app import create_app
from app.core import config
from app.services.dormant_revival_worker import dormant_revival_worker
from app.services.fansly_dm_bot import bot_manager
from app.services.followup_worker import followup_worker

app = create_app()


def _cleanup_all_workers() -> None:
    try:
        followup_worker.stop()
    except Exception:
        pass
    try:
        dormant_revival_worker.stop()
    except Exception:
        pass
    try:
        bot_manager.stop_all()
    except Exception:
        pass


atexit.register(_cleanup_all_workers)

if __name__ == "__main__":
    # キャラクター別プロファイル初期化＆常時DM巡回監視（稼働OFF時も受信・翻訳・カルテ更新のみ継続）＋ウォッチドッグ自動復旧を起動
    bot_manager.start_monitoring_all()
    bot_manager.start_watchdog()
    followup_worker.start()
    dormant_revival_worker.start()

    print("==================================================")
    print("👑 Fansly AI DM Sales Engine (AI_DM_Sales v1.4) 起動！")
    print(f"👉 ブラウザで http://127.0.0.1:{config.PORT} を開いてください")
    print(f"📂 共有資産パス: {config.SHARED_ASSETS_DIR}")
    print(f"🗄️ 統合SQLite DB: {config.DB_PATH}")
    print(f"🌐 ブラウザプロファイル: {config.PROFILES_DIR}")
    print("👁️ DM巡回監視ワーカー＆ウォッチドッグ生存監視（ハング検知・自動再起動付き）開始")
    print("⏰ FollowupWorker (FOMOリマインド & Step 2追撃) バックグラウンド待機開始")
    print("💌 DormantRevivalWorker (休眠課金ファン掘り起こし) バックグラウンド待機開始")
    print("==================================================")
    app.run(host="127.0.0.1", port=config.PORT, debug=False)


