from app import create_app, config
from app.config import (
    BASE_DIR,
    SHARED_ASSETS_DIR,
    CHARACTERS_ROOT,
    WORKFLOWS_DIR,
    LINE1_SCRIPT,
    LINE2_SCRIPT,
    VIDEO_SCRIPT,
    PORT,
    DEFAULT_FIXED_TAGS,
)
from app.services.mosaic_service import (
    get_nude_detector,
    detect_boxes_on_image,
    apply_pixel_mosaic_bgr,
    interpolate_video_boxes,
)
from app.services.character_service import (
    get_default_lilith_profile,
    migrate_legacy_structure,
    get_char_paths,
    list_all_characters,
)
from app.services.file_service import (
    get_images_in_dir,
    get_videos_in_dir,
    group_candidate_slots,
    resolve_queue_folder_path,
    collect_queue_item,
)
from app.services.worker_service import (
    worker_state,
    worker_lock,
    append_log,
    run_line1_worker,
    run_video_worker,
    run_line2_worker,
)

# resolve_queue_folder_path は app.services.file_service の実装を共通利用する
app = create_app()

if __name__ == "__main__":
    print("==================================================")
    print(f"👑 AI Content Factory 半自動AIモザイク＆FYP動画統合コンソール起動！")
    print(f"👉 ブラウザで http://127.0.0.1:{config.PORT} を開いてください")
    print("==================================================")
    app.run(host="127.0.0.1", port=config.PORT, debug=False)
