import os
import sys
import json
import time
import shutil
import argparse
from datetime import datetime
from playwright.sync_api import sync_playwright
from app.services.comfy_progress import emit_progress

if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

# ==========================================
# ★ 総督の環境設定エリア ★
# ==========================================
BASE_DIR = os.environ.get("AI_FACTORY_BASE_DIR") or os.path.dirname(os.path.abspath(__file__))
SHARED_ASSETS_DIR = os.environ.get("SHARED_ASSETS_DIR") or r"C:\Fansly_Empire\shared_assets"
CHARACTERS_ROOT = os.path.join(SHARED_ASSETS_DIR, "characters")

FANSLY_HOME_URL = "https://fansly.com/home"

# 【安全装置】
# False: キャプション・メディア・プレビュー・予約日時セットまで全自動で行い、
#        最後の青い「Schedule」ボタン前で一時停止して総督の目視確認を待つ
# True : メディアのサーバー承認完了を監視し、最後の「Schedule」ボタンまで完全自動でクリックする
AUTO_CLICK_SUBMIT = True
# ==========================================

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")
VIDEO_EXTS = (".mp4", ".webm", ".mov")

MONTH_NAMES = [
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December"
]

parser = argparse.ArgumentParser(
    description="AI Content Factory - Line 2 (Fixed 1-Pattern Video+Images & Exact Media Matching)"
)
parser.add_argument("--char", default="queen_lilith", help="Character ID")
parser.add_argument("--headless", dest="headless", action="store_true", default=None, help="Run Playwright in headless mode")
parser.add_argument("--headed", "--no-headless", dest="headless", action="store_false", help="Run Playwright with visible browser window")
_args, _ = parser.parse_known_args()


def resolve_line2_headless(cli_flag=None) -> bool:
    """CLI引数 > 環境変数 LINE2_HEADLESS > app_settings.json > False の優先順でヘッドレス有無を解決する"""
    if cli_flag is not None:
        return bool(cli_flag)
    env_val = os.environ.get("LINE2_HEADLESS")
    if env_val is not None and str(env_val).strip() != "":
        return str(env_val).strip().lower() in ("1", "true", "yes", "on")
    try:
        from app.config import get_line2_headless
        return bool(get_line2_headless())
    except Exception:
        return False

CHAR_ID = _args.char
CHAR_DIR = os.path.join(CHARACTERS_ROOT, CHAR_ID)
READY_ROOT = os.path.join(CHAR_DIR, "02_ready_to_post")
POSTED_ROOT = os.path.join(CHAR_DIR, "03_posted")
PROFILE_DIR = os.path.join(CHAR_DIR, "browser_profile")

os.makedirs(READY_ROOT, exist_ok=True)
os.makedirs(POSTED_ROOT, exist_ok=True)
os.makedirs(PROFILE_DIR, exist_ok=True)


def get_char_paths(char_id: str) -> dict:
    c_dir = os.path.join(CHARACTERS_ROOT, char_id)
    return {
        "char_dir": c_dir,
        "profile_path": os.path.join(c_dir, "profile.json"),
        "ready_dir": os.path.join(c_dir, "02_ready_to_post"),
        "posted_dir": os.path.join(c_dir, "03_posted"),
        "browser_profile": os.path.join(c_dir, "browser_profile"),
    }


def load_character_display_name(char_id: str = CHAR_ID) -> str:
    prof_path = os.path.join(CHARACTERS_ROOT, char_id, "profile.json")
    if os.path.exists(prof_path):
        try:
            with open(prof_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("display_name") or data.get("name") or char_id
        except Exception:
            pass
    return char_id


def build_full_text_fallback(item: dict) -> str:
    """full_text が未設定の場合に caption と hashtags から構築する"""
    full_text = str(item.get("full_text") or "").strip()
    if full_text:
        return full_text

    caption = str(item.get("caption") or "").strip()
    hashtags = item.get("hashtags") or []
    if isinstance(hashtags, str):
        hashtags = [t for t in hashtags.split() if t]
    elif not isinstance(hashtags, list):
        hashtags = []

    tags_str = " ".join(str(t).strip() for t in hashtags if str(t).strip())
    if caption and tags_str:
        return f"{caption}\n\n{tags_str}"
    return caption or tags_str


def extract_media_files(folder_path: str, item: dict) -> tuple[list[str], list[str], str | None]:
    """
    SPEC_LATEST.md 4.2 準拠で、本編メディア（paid_video_paths [1本], paid_image_paths [0〜N枚]）と
    FYP無料プレビュー動画（fyp_preview_path [1本]）を抽出する。
    ※ パターン③で main_video と fyp_preview_video が同一ファイル名でも除外せず両方返す。
    """
    all_files = sorted(os.listdir(folder_path)) if os.path.isdir(folder_path) else []

    # 1. 本編動画（main_video: 1本必須）の解決
    main_v_name = (
        item.get("main_video")
        or item.get("video_file")
        or item.get("video")
        or ""
    ).strip()

    main_video_path = None
    if main_v_name:
        cand_p = os.path.join(folder_path, main_v_name)
        if os.path.isfile(cand_p):
            main_video_path = cand_p

    if not main_video_path:
        for fname in all_files:
            if (
                fname.lower().endswith(VIDEO_EXTS)
                and not fname.lower().endswith(".tmp.mp4")
                and "FYP_PREVIEW" not in fname.upper()
            ):
                main_video_path = os.path.join(folder_path, fname)
                break

    paid_video_paths = [main_video_path] if main_video_path else []

    # 2. FYP無料プレビュー動画（fyp_preview_video: 1本必須）の解決
    fyp_v_name = (item.get("fyp_preview_video") or "").strip()
    fyp_preview_path = None
    if fyp_v_name:
        cand_fyp = os.path.join(folder_path, fyp_v_name)
        if os.path.isfile(cand_fyp):
            fyp_preview_path = cand_fyp

    if not fyp_preview_path:
        for fname in all_files:
            if (
                fname.lower().endswith(VIDEO_EXTS)
                and not fname.lower().endswith(".tmp.mp4")
                and "FYP_PREVIEW" in fname.upper()
            ):
                fyp_preview_path = os.path.join(folder_path, fname)
                break

    if not fyp_preview_path and main_video_path:
        fyp_preview_path = main_video_path

    # 3. 本編採用画像（images: 0〜N枚）の解決
    paid_image_paths = []
    raw_images = item.get("images")
    if isinstance(raw_images, list):
        for img_name in raw_images:
            if not img_name:
                continue
            ip = os.path.join(folder_path, str(img_name))
            if os.path.isfile(ip) and ip not in paid_image_paths:
                paid_image_paths.append(ip)

    return paid_video_paths, paid_image_paths, fyp_preview_path


def load_ready_queue(ready_root: str = READY_ROOT) -> list[dict]:
    """
    02_ready_to_post/<連番>/final_post.json（および互換サブフォルダ pending/scheduled）を読み込み、
    scheduled_at 昇順にソートして返す。
    """
    if not ready_root or not os.path.isdir(ready_root):
        return []

    candidate_dirs: list[tuple[str, str]] = []
    seen_folders: set[str] = set()
    reserved_sub_names = {"pending", "scheduled", "posted", "done", "completed"}

    # 1. v3.0 標準フラット構造: 02_ready_to_post/<連番>/
    for name in sorted(os.listdir(ready_root)):
        if name.lower() in reserved_sub_names:
            continue
        p = os.path.join(ready_root, name)
        if os.path.isdir(p):
            candidate_dirs.append((name, p))
            seen_folders.add(name)

    # 2. 互換維持のための配下構造: 02_ready_to_post/pending/<連番>/, 02_ready_to_post/scheduled/<連番>/
    for sub in ("pending", "scheduled"):
        sub_root = os.path.join(ready_root, sub)
        if not os.path.isdir(sub_root):
            continue
        for name in sorted(os.listdir(sub_root)):
            if name in seen_folders or name.lower() in reserved_sub_names:
                continue
            p = os.path.join(sub_root, name)
            if os.path.isdir(p):
                candidate_dirs.append((name, p))
                seen_folders.add(name)

    queue: list[dict] = []
    for folder_name, folder_path in candidate_dirs:
        json_path = os.path.join(folder_path, "final_post.json")
        if not os.path.isfile(json_path):
            continue

        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"⚠️ [No.{folder_name}] final_post.json の読み込みに失敗したためスキップします: {e}")
            continue

        if not isinstance(data, dict):
            continue

        # すでに投稿・予約完了済みのものはスキップ
        if data.get("posted") is True or str(data.get("status") or "").lower() == "posted":
            continue

        v_paths, i_paths, fyp_path = extract_media_files(folder_path, data)
        if not v_paths or not fyp_path:
            print(
                f"⚠️ [No.{folder_name}] 必須動画（main_video または fyp_preview_video）が見つからないためスキップします。"
            )
            continue

        resolved_folder = str(data.get("folder") or folder_name).strip() or folder_name
        data["folder"] = resolved_folder
        data["char_id"] = str(data.get("char_id") or CHAR_ID).strip() or CHAR_ID
        data["full_text"] = build_full_text_fallback(data)
        data["main_video"] = os.path.basename(v_paths[0])
        data["fyp_preview_video"] = os.path.basename(fyp_path)
        data["images"] = [os.path.basename(p) for p in i_paths]

        data["_folder_name"] = folder_name
        data["_folder_path"] = folder_path
        data["_paid_video_paths"] = v_paths
        data["_paid_image_paths"] = i_paths
        data["_fyp_preview_path"] = fyp_path
        queue.append(data)

    queue.sort(key=lambda x: (str(x.get("scheduled_at") or "").strip() or "9999-99-99 99:99", x["_folder_name"]))
    return queue


def _is_interactive_stdin() -> bool:
    try:
        return bool(sys.stdin and sys.stdin.isatty())
    except Exception:
        return False


def ensure_logged_in(
    page, char_display_name: str, char_id: str = CHAR_ID, profile_dir: str = PROFILE_DIR, headless: bool = False
) -> bool:
    print("🌐 Fanslyへアクセス中...")
    page.goto(FANSLY_HOME_URL, wait_until="domcontentloaded")
    time.sleep(4)

    if "login" in page.url.lower() or page.get_by_text("Log In").count() > 0:
        if headless:
            print(
                f"❌ [{char_id}] ヘッドレスモードで未ログイン状態を検知しました。"
                "設定画面から『画面表示あり（ヘッドレスOFF）』に切り替えて一度ログインしてください。"
            )
            return False
        print("\n" + "=" * 55)
        print(f"🔑 【初回ログイン待機：{char_display_name} ({char_id})】")
        print("開いたブラウザで、このキャラクター用の Fansly アカウントにログインしてください！")
        print(f"（ログイン状態は {profile_dir} に保存されるので次回から不要です）")
        print("=" * 55)
        if _is_interactive_stdin():
            input("👉 ログインが完了してホーム画面が表示されたら [Enter] を押してください: ")
            page.goto(FANSLY_HOME_URL, wait_until="domcontentloaded")
            time.sleep(3)
        else:
            print("⏳ バックグラウンド実行モードのため、ログイン完了を自動待機しています（最大180秒）...")
            for _ in range(90):
                time.sleep(2)
                if "login" not in page.url.lower() and page.get_by_text("Log In").count() == 0:
                    break
            page.goto(FANSLY_HOME_URL, wait_until="domcontentloaded")
            time.sleep(3)

    print("✅ ログインセッション確認OK！")
    return True


def _inspect_media_modal_state(page, target_filename: str = "") -> dict:
    """
    FanslyのMediaモーダル（app-account-media-upload-v2）の実機DOMを走査し、
    下部サムネイル一覧（.upload-rail .fansly-media-tile.upload-tile:not(.upload-tile-add)）の
    各スロット種別（video / image）・選択状態（.selected）・Free Previewバッジ状態（.fansly-media-badge.ok / eye）、
    およびメインステージ（.upload-stage-media / .upload-stage-name / .upload-stage-switch）の状態を構造化して返す。
    """
    stem = os.path.splitext(target_filename)[0] if target_filename else ""
    return page.evaluate(
        r"""({ targetFilename, targetStem }) => {
            const isVisible = (el) => {
                if (!el || el.nodeType !== 1) return false;
                const style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden' || parseFloat(style.opacity || '1') === 0) {
                    return false;
                }
                const r = el.getBoundingClientRect();
                return r.width > 4 && r.height > 4;
            };

            const modalEl = document.querySelector(
                'app-account-media-upload-v2.active-modal, app-account-media-upload-v2, ' +
                '.modal.upload-v2, app-post-media-modal, app-media-upload-modal, [role="dialog"]'
            );
            const modalRoot = modalEl || document.body;

            // 1) メインステージ領域（.upload-stage / .upload-stage-media / .upload-stage-actions）の検査
            const stageRoot = modalRoot.querySelector('.upload-stage');
            const stageMedia = modalRoot.querySelector('.upload-stage-media') || stageRoot;
            const stageActions = modalRoot.querySelector('.upload-stage-actions');
            const stageNameEl = modalRoot.querySelector('.upload-stage-name');
            const stageFilename = (stageNameEl && isVisible(stageNameEl))
                ? (stageNameEl.innerText || stageNameEl.textContent || '').trim()
                : '';

            const stageFilenameIsVideo = /\.(mp4|webm|mov|m4v)$/i.test(stageFilename);
            const stageFilenameIsImage = /\.(png|jpe?g|webp|gif)$/i.test(stageFilename);

            // 動画選択時は app-video-element > app-videojs-player がマウントされる
            // （再生前は img.preview.local-poster が表示され div.video.preview-shown が非表示のため、コンポーネント存在も検査）
            const stageHasVideoPlayer = !!(
                stageMedia && isVisible(stageMedia) &&
                stageMedia.querySelector('app-video-element, app-videojs-player, .video-js, video')
            );
            const stageHasCoverFrameBtn = !!(
                stageActions && isVisible(stageActions) &&
                stageActions.querySelector('app-icon[name="image"], .fa-image')
            );
            const stageHasImageEditBtn = !!(
                stageActions && isVisible(stageActions) &&
                stageActions.querySelector('app-icon[name="pencil"], .fa-pencil')
            );

            // 動画プレイヤー外の純粋な <img>（画像スロット選択時は app-media-template > img が直接配置される）
            const stagePureImages = stageMedia
                ? Array.from(stageMedia.querySelectorAll('img')).filter(img => {
                    if (!isVisible(img)) return false;
                    if (img.classList.contains('local-poster')) return false;
                    if (img.closest('app-video-element, app-videojs-player, .video-js')) return false;
                    const r = img.getBoundingClientRect();
                    return r.width >= 150 && r.height >= 120;
                })
                : [];

            const hasMainVideo = stageHasVideoPlayer || stageFilenameIsVideo || (stageHasCoverFrameBtn && !stageFilenameIsImage);
            const hasMainImageOnly = !hasMainVideo && (
                stagePureImages.length > 0 || stageFilenameIsImage || stageHasImageEditBtn
            );

            // 対象動画ファイル名との一致確認（画像ファイル名との誤一致を除外）
            const tfLower = (targetFilename || '').toLowerCase();
            const stemLower = (targetStem || '').toLowerCase();
            const sfLower = stageFilename.toLowerCase();
            const filenameMatched = !stageFilenameIsImage && !!(
                (tfLower && sfLower === tfLower) ||
                (tfLower && sfLower.includes(tfLower)) ||
                (stemLower && stemLower.length >= 4 && sfLower.includes(stemLower))
            );

            // 2) 下部サムネイル一覧（.upload-rail .fansly-media-tile.upload-tile:not(.upload-tile-add)）の特定
            // ※ 末尾の「＋」追加タイル（.upload-tile-add）はメディアスロットから厳密に除外する
            let rawCards = Array.from(
                modalRoot.querySelectorAll(
                    '.upload-rail .fansly-media-tile.upload-tile:not(.upload-tile-add), ' +
                    '.fansly-media-tile.upload-tile:not(.upload-tile-add)'
                )
            ).filter(isVisible);

            if (rawCards.length === 0 && modalEl) {
                const fallbackSel = [
                    '.upload-rail [xd-dragable-id]:not(.upload-tile-add)',
                    '[xd-dragable-id]:not(.upload-tile-add)',
                    '[draggable="true"]:not(.upload-tile-add)',
                    '.media-preview-item',
                    '.attachment-item'
                ].join(', ');
                rawCards = Array.from(modalRoot.querySelectorAll(fallbackSel)).filter(el => {
                    if (!isVisible(el)) return false;
                    const r = el.getBoundingClientRect();
                    return r.width >= 28 && r.width <= 260 && r.height >= 28 && r.height <= 260;
                });
            }

            // 入れ子重複の除去（親タイルと子要素が両方マッチした場合は外側タイルを採用）
            rawCards = rawCards.filter(el => !rawCards.some(other => other !== el && other.contains(el)));

            // 画面座標順（Y行 → X列）でソート
            rawCards.sort((a, b) => {
                const ra = a.getBoundingClientRect();
                const rb = b.getBoundingClientRect();
                const rowDiff = Math.floor(ra.top / 50) - Math.floor(rb.top / 50);
                return rowDiff !== 0 ? rowDiff : ra.left - rb.left;
            });

            const durationRegex = /\b\d{1,2}:\d{2}\b/;

            const slots = rawCards.map((card, idx) => {
                const r = card.getBoundingClientRect();
                const cls = (card.className && typeof card.className === 'string') ? card.className : '';
                const text = (card.innerText || card.textContent || '').trim();

                // 実機動画タイル構造: app-media-template > app-video-element > app-videojs-player > ... > video.vjs-tech
                const hasVideoTag = !!(
                    card.tagName.toLowerCase() === 'video' ||
                    card.querySelector('app-video-element, app-videojs-player, .video-js, video')
                );
                // 実機動画バッジ構造: .fansly-media-tile-badges > .fansly-media-badge > app-icon[name="play"] > i.fa-play
                const hasVideoIcon = !!card.querySelector(
                    'app-icon[name="play"], app-icon[name="video"], app-icon[name="film"], ' +
                    '.fa-play, .fa-video, .fa-film, i[class*="fa-play"], i[class*="fa-video"], ' +
                    'app-video-thumbnail, [class*="video-indicator"], [class*="video-badge"]'
                );
                const hasDurationText = durationRegex.test(text);

                // 画像タイル構造: app-media-template > img（動画プレイヤー内の local-poster は除外）
                const pureImgs = Array.from(card.querySelectorAll('img')).filter(img => {
                    if (img.classList.contains('local-poster')) return false;
                    if (img.closest('app-video-element, app-videojs-player, .video-js')) return false;
                    return true;
                });
                const hasImgTag = card.tagName.toLowerCase() === 'img' || pureImgs.length > 0;

                const isVideoSlot = hasVideoTag || hasVideoIcon || hasDurationText;
                const isImageOnlySlot = !isVideoSlot && hasImgTag;

                // 選択中判定: タイル自身のクラス（.selected / .active）または aria-selected のみを見る
                // ※ video-js 内部の隠し要素（.quality-option.selected 等）による誤判定を防止
                const isActive = card.classList.contains('selected')
                    || card.classList.contains('active')
                    || card.getAttribute('aria-selected') === 'true';

                // Free Preview バッジ判定:
                // 未添付時: .fansly-media-badge.warn > app-icon[name="eye-slash"] > i.fa-eye-slash
                // 添付済時: .fansly-media-badge.ok   > app-icon[name="eye"]       > i.fa-eye
                const badgeBox = card.querySelector('.fansly-media-tile-badges') || card;
                const hasPreviewBadge = !!(
                    badgeBox.querySelector('.fansly-media-badge.ok') ||
                    badgeBox.querySelector('app-icon[name="eye"], i.fa-eye:not(.fa-eye-slash)')
                );
                const hasNoPreviewWarnBadge = !!(
                    badgeBox.querySelector('.fansly-media-badge.warn') ||
                    badgeBox.querySelector('app-icon[name="eye-slash"], i.fa-eye-slash')
                );

                // スロット固有識別属性を付与（クリックターゲット用）
                const slotAttr = `af-slot-${idx}`;
                card.setAttribute('data-af-media-slot', slotAttr);

                return {
                    index: idx,
                    slotAttr,
                    x: r.left + r.width / 2,
                    y: r.top + r.height / 2,
                    width: r.width,
                    height: r.height,
                    isVideo: isVideoSlot,
                    isImageOnly: isImageOnlySlot,
                    hasVideoTag,
                    hasVideoIcon,
                    hasDurationText,
                    isActive,
                    hasPreviewBadge,
                    hasNoPreviewWarnBadge,
                    textSnippet: text.slice(0, 60)
                };
            });

            // 3) ステージ上の Free Preview 操作UI状態
            // 添付前: .upload-stage-badge（"+ Add free preview" ドロップダウン）が表示
            // 添付後: .upload-stage-switch（"Media" / "Preview" 切替ボタン）および "Replace" / "Remove preview" が表示
            const stageBadgeEl = modalRoot.querySelector('.upload-stage-badge');
            const hasAddFreePreviewBtn = !!(stageBadgeEl && isVisible(stageBadgeEl));

            const stageSwitchEl = modalRoot.querySelector('.upload-stage-switch');
            const hasStagePreviewSwitch = !!(stageSwitchEl && isVisible(stageSwitchEl));

            const stageActionsText = (stageActions && isVisible(stageActions))
                ? (stageActions.innerText || stageActions.textContent || '')
                : '';
            const hasReplaceOrRemovePreview = !!(
                (stageActions && stageActions.querySelector('.upload-stage-replace') && isVisible(stageActions.querySelector('.upload-stage-replace'))) ||
                /remove\s+preview|プレビューを削除/i.test(stageActionsText)
            );

            return {
                modalFound: !!modalEl,
                stageFilename,
                stageFilenameIsVideo,
                stageFilenameIsImage,
                hasMainVideo,
                hasMainImageOnly,
                filenameMatched,
                slots,
                hasAddFreePreviewBtn,
                hasStagePreviewSwitch,
                hasReplaceOrRemovePreview,
                hasActivePreviewPanel: hasStagePreviewSwitch || hasReplaceOrRemovePreview
            };
        }""",
        {"targetFilename": target_filename or "", "targetStem": stem},
    )


def verify_main_video_selected_pre_attach(page, target_filename: str) -> tuple[bool, str]:
    """
    【二重検証ガード①：添付前検証】
    プレビュー追加ボタンを押す直前に、現在アクティブな対象が確実に「本編動画」であり、
    画像スロットが選択されていないことを検証する。
    """
    state = _inspect_media_modal_state(page, target_filename)
    if not state.get("modalFound"):
        return False, "Mediaモーダル(app-account-media-upload-v2)が表示されていません"

    slots = state.get("slots", [])
    stage_name = state.get("stageFilename") or ""
    video_slots = [s for s in slots if s.get("isVideo") and not s.get("isImageOnly")]
    image_slots = [s for s in slots if s.get("isImageOnly")]
    active_image_slots = [s for s in image_slots if s.get("isActive")]
    active_video_slots = [s for s in video_slots if s.get("isActive")]

    # 【厳格拒否条件】画像スロットが選択中、またはメインステージが画像表示・画像ファイル名の場合は即NG
    if active_image_slots:
        return (
            False,
            f"画像スロット(index={active_image_slots[0]['index']}, stage='{stage_name}')が選択状態です",
        )
    if state.get("stageFilenameIsImage"):
        return False, f"メインステージのファイル名が画像 [{stage_name}] を指しています"
    if state.get("hasMainImageOnly"):
        return False, f"メインステージ(.upload-stage-media)が画像表示状態です (stage='{stage_name}')"

    # ステージにファイル名が表示されている場合、対象本編動画と無関係な別名ならNG
    if target_filename and stage_name and not state.get("filenameMatched"):
        return False, f"ステージ表示ファイル名 [{stage_name}] が対象本編動画 [{target_filename}] と一致しません"

    # 【肯定条件】動画スロットが.selected かつ メインステージが動画プレイヤーまたは対象動画名と一致
    if active_video_slots and (state.get("hasMainVideo") or state.get("filenameMatched")):
        return (
            True,
            f"動画スロット(index={active_video_slots[0]['index']})選択＆ステージ表示 [{stage_name or target_filename}] を確認",
        )
    if len(slots) == 1 and len(video_slots) == 1 and state.get("hasMainVideo"):
        return True, f"単一動画スロット＆ステージ動画プレイヤー [{stage_name or target_filename}] を確認"
    if not slots and state.get("hasMainVideo") and state.get("filenameMatched"):
        return True, f"メインステージ動画プレイヤー＆対象ファイル名 [{stage_name}] を確認"

    return (
        False,
        f"本編動画スロットの選択状態をDOM上で確定できませんでした "
        f"(stage='{stage_name}', slots={len(slots)}, active_video={len(active_video_slots)})",
    )


def verify_free_preview_attached_post_upload(
    page, target_filename: str, fyp_preview_filename: str = ""
) -> tuple[bool, str]:
    """
    【二重検証ガード②：添付後検証】
    FYPプレビュー動画のアップロード完了後、
    1) 画像スロット側に Free Preview バッジ（.fansly-media-badge.ok / eye）が誤添付されていないこと
    2) 本編動画スロットに .fansly-media-badge.ok（eye）が点灯し、ステージが Preview 切替UI（.upload-stage-switch / Remove preview）へ遷移していること
    を実機DOM構造で検証する。
    """
    state = _inspect_media_modal_state(page, target_filename)
    if not state.get("modalFound"):
        return False, "Mediaモーダルが閉じているため添付後検証を完了できません"

    slots = state.get("slots", [])
    stage_name = state.get("stageFilename") or ""

    # 1) 全画像スロットに Free Preview バッジ（.fansly-media-badge.ok / fa-eye）が誤添付されていないか最優先で検査
    image_slots_with_preview = [
        s for s in slots if s.get("isImageOnly") and s.get("hasPreviewBadge")
    ]
    if image_slots_with_preview:
        bad_idx = [s["index"] for s in image_slots_with_preview]
        return False, f"画像スロット(index={bad_idx})にFree Previewバッジ(.fansly-media-badge.ok)が誤添付されています！"

    # 2) 現在選択中の対象が画像スロットに切り替わっていないか再確認
    active_image_slots = [s for s in slots if s.get("isImageOnly") and s.get("isActive")]
    if active_image_slots or (state.get("hasMainImageOnly") and not state.get("hasMainVideo")):
        return False, f"プレビュー添付後にアクティブ表示が画像スロット(stage='{stage_name}')になっています（誤添付の疑い）"

    # 3) 本編動画への Free Preview 紐づき確認
    video_slots_with_badge = [
        s for s in slots if s.get("isVideo") and not s.get("isImageOnly") and s.get("hasPreviewBadge")
    ]
    has_switch = bool(state.get("hasStagePreviewSwitch"))
    has_remove_or_replace = bool(state.get("hasReplaceOrRemovePreview"))
    add_btn_gone = not bool(state.get("hasAddFreePreviewBtn"))

    fyp_stem = os.path.splitext(fyp_preview_filename)[0].lower() if fyp_preview_filename else ""
    sf_lower = stage_name.lower()
    preview_filename_shown = bool(
        (fyp_preview_filename and sf_lower == fyp_preview_filename.lower())
        or (fyp_stem and len(fyp_stem) >= 4 and fyp_stem in sf_lower)
    )

    if video_slots_with_badge and (has_switch or has_remove_or_replace or add_btn_gone):
        return (
            True,
            f"本編動画スロット(index={video_slots_with_badge[0]['index']})のFree Previewバッジ(.fansly-media-badge.ok)"
            f"＆ステージ表示 [{stage_name}] を確認",
        )
    if add_btn_gone and (has_switch or has_remove_or_replace) and (state.get("hasMainVideo") or preview_filename_shown):
        return (
            True,
            f"本編動画ステージ内のPreview切替UI(.upload-stage-switch / Remove preview)＆[{stage_name}] を確認",
        )

    return (
        False,
        f"プレビュー動画アップロード後、本編動画へのFree Preview紐づきをDOM上で確認できませんでした "
        f"(video_badge={len(video_slots_with_badge)}, switch={has_switch}, add_btn_gone={add_btn_gone}, stage='{stage_name}')",
    )


def verify_post_cards_preview_on_composer(page) -> tuple[bool, str]:
    """
    Add to post 確定後の投稿作成画面（app-post-creation / app-post-create）においても、
    画像専用カードへの Free Preview 誤添付やプレビュー数の超過がないことを最終検証する。
    """
    result = page.evaluate(
        r"""() => {
            const root = document.querySelector('app-post-creation, app-post-create, .new-post');
            if (!root) {
                return { checked: false, imageMisattached: false };
            }

            // Fansly v2 投稿作成画面の添付カード単位（app-account-media-create-template.attachment）
            const attachments = Array.from(
                root.querySelectorAll('app-account-media-create-template.attachment, .attachment-wrapper')
            );
            const topCards = attachments.filter(el => !attachments.some(o => o !== el && o.contains(el)));
            if (topCards.length === 0) {
                return { checked: false, imageMisattached: false };
            }

            let imageMisattached = false;
            for (const card of topCards) {
                const items = Array.from(card.querySelectorAll('app-account-media-template.feed-item-preview-media'));
                let vidCount = 0;
                let imgCount = 0;
                for (const item of items) {
                    const isVid = !!item.querySelector('app-video-element, app-videojs-player, .video-js, video');
                    const isImg = !isVid && !!item.querySelector('img:not(.local-poster)');
                    if (isVid) vidCount++;
                    else if (isImg) imgCount++;
                }

                const teaser = card.querySelector('.audience-badge-teaser');
                if (teaser) {
                    const teaserTxt = (teaser.innerText || teaser.textContent || '').trim();
                    const m = teaserTxt.match(/(\d+)\s*\/\s*(\d+)/);
                    if (m) {
                        const prevCount = parseInt(m[1], 10);
                        const totalCount = parseInt(m[2], 10);
                        // 動画＋画像の混在カードで動画本数を超えてプレビューが付いている場合は画像にも誤添付されている
                        if (imgCount > 0 && totalCount === (vidCount + imgCount) && prevCount > vidCount) {
                            imageMisattached = true;
                        }
                    }
                    // 画像のみのカードに .ok（プレビュー設定済）が付いている場合も誤添付
                    if (vidCount === 0 && imgCount > 0 && teaser.classList.contains('ok')) {
                        imageMisattached = true;
                    }
                }
            }
            return { checked: true, imageMisattached };
        }"""
    )
    if result.get("imageMisattached"):
        return False, "投稿作成画面の画像カードにFree Previewが誤添付されています！"
    return True, "投稿作成画面のメディアカード検証OK"


def select_target_media_in_modal(page, target_filename: str) -> bool:
    """
    【添付前ガード連動】
    画像と動画が混在する Media モーダル（app-account-media-upload-v2）において、
    画像スロットおよび末尾の「＋」追加タイル（.upload-tile-add）を厳密に除外し、
    app-video-element / playバッジを持つ「本編動画スロット」だけを特定して選択する。
    """
    if not target_filename:
        return False

    # モーダル内のサムネイルレール描画を短時間待機（大容量動画のDOMマウント遅延対策）
    for _ in range(8):
        init_state = _inspect_media_modal_state(page, target_filename)
        if init_state.get("slots") or init_state.get("hasMainVideo"):
            break
        time.sleep(0.4)

    # 1. すでに本編動画が選択状態にあるか検証（動画1本＋画像0枚の場合など）
    ok_now, reason_now = verify_main_video_selected_pre_attach(page, target_filename)
    if ok_now:
        print(f"  ✔ [添付前検証OK] すでに本編動画 [{target_filename}] が選択状態です ({reason_now})")
        return True

    # 2. DOM構造解析で動画スロットのみを抽出（画像専用スロットは完全に除外）
    state = _inspect_media_modal_state(page, target_filename)
    slots = state.get("slots", [])
    video_slots = [s for s in slots if s.get("isVideo") and not s.get("isImageOnly")]
    excluded_image_count = sum(1 for s in slots if s.get("isImageOnly"))

    if excluded_image_count > 0:
        print(f"  🛡️ 画像スロット {excluded_image_count} 件を除外し、動画スロット {len(video_slots)} 件をターゲット候補に特定しました")

    # 候補A：特定された動画スロット（.upload-rail .fansly-media-tile[data-af-media-slot]）をクリック
    for vs in video_slots:
        try:
            slot_loc = page.locator(
                f"app-account-media-upload-v2 [data-af-media-slot='{vs['slotAttr']}'], "
                f"[data-af-media-slot='{vs['slotAttr']}']"
            ).locator("visible=true").first
            if slot_loc.count() > 0:
                slot_loc.click(timeout=3000)
            else:
                page.mouse.click(vs["x"], vs["y"])

            for _ in range(5):
                time.sleep(0.35)
                ok, reason = verify_main_video_selected_pre_attach(page, target_filename)
                if ok:
                    print(
                        f"  ✔ [添付前検証OK] 動画スロット(index={vs['index']})を選択し本編動画 [{target_filename}] をアクティブ化しました ({reason})"
                    )
                    return True
        except Exception:
            continue

    # 候補B：実機DOMの動画タイルセレクタ（app-video-element / playアイコン保持タイル、かつ .upload-tile-add 除外）を直接クリック
    video_tiles = page.locator(
        "app-account-media-upload-v2 .upload-rail .fansly-media-tile.upload-tile:not(.upload-tile-add):has(app-video-element), "
        "app-account-media-upload-v2 .upload-rail .fansly-media-tile.upload-tile:not(.upload-tile-add):has(app-icon[name='play']), "
        "app-account-media-upload-v2 .upload-rail .fansly-media-tile.upload-tile:not(.upload-tile-add):has(.fa-play)"
    ).locator("visible=true")
    for idx in range(video_tiles.count()):
        try:
            vt = video_tiles.nth(idx)
            vt.click(timeout=3000)
            for _ in range(5):
                time.sleep(0.35)
                ok, reason = verify_main_video_selected_pre_attach(page, target_filename)
                if ok:
                    print(
                        f"  ✔ [添付前検証OK] 動画タイル要素(#{idx})から本編動画 [{target_filename}] を選択完了！ ({reason})"
                    )
                    return True
        except Exception:
            pass

    # 候補C：スロット種別が未確定（デコード遅延等）の非画像スロットのみ先頭試行（※画像確定スロットは絶対にクリックしない）
    ambiguous_slots = [s for s in slots if not s.get("isImageOnly") and not s.get("isVideo")]
    for amb in ambiguous_slots[:1]:
        try:
            page.mouse.click(amb["x"], amb["y"])
            for _ in range(5):
                time.sleep(0.35)
                ok, reason = verify_main_video_selected_pre_attach(page, target_filename)
                if ok:
                    print(
                        f"  ✔ [添付前検証OK] 先頭メディアスロット(index={amb['index']})で本編動画 [{target_filename}] を確認 ({reason})"
                    )
                    return True
        except Exception:
            pass

    print(f"  ❌ [添付前検証NG] 本編動画 [{target_filename}] のスロットを特定・選択できませんでした（画像誤爆防止のため停止）")
    return False


def _cancel_media_modal_safely(page):
    """誤添付検知時などに Add to post を押さず Media モーダルを安全に閉じる"""
    try:
        cancel_btn = page.locator(
            "app-account-media-upload-v2 .modal-footer button:has-text('Cancel'), "
            "app-account-media-upload-v2 .modal-footer button:has-text('キャンセル'), "
            "app-account-media-upload-v2 .upload-top button[aria-label='Close']"
        ).locator("visible=true").first
        if cancel_btn.count() > 0:
            cancel_btn.click(timeout=3000)
            time.sleep(0.6)
            return
    except Exception:
        pass
    try:
        page.keyboard.press("Escape")
        time.sleep(0.6)
    except Exception:
        pass


def attach_free_preview(page, target_media_filename: str, fyp_preview_path: str, max_retries: int = 2) -> bool:
    """
    指定した本編動画（target_media_filename）を選択状態にした上で、
    【二重検証ガード（添付前検証＋添付後検証）】を通して確実に本編動画へ
    fyp_preview_path（FYP用無料プレビュー動画）を添付する。
    万一画像側に誤添付された場合や未紐づけの場合は即座に検知・リトライし、解消できなければ False を返して停止する。
    """
    if not fyp_preview_path or not os.path.isfile(fyp_preview_path):
        print(f"  ⚠️ FYPプレビュー動画ファイルが存在しません: {fyp_preview_path}")
        return False

    fyp_basename = os.path.basename(fyp_preview_path)

    for attempt in range(1, max_retries + 1):
        try:
            if attempt > 1:
                print(f"  🔄 [リトライ {attempt}/{max_retries}] 本編動画への無料プレビュー紐づけを再試行します...")

            # 1. 本編動画スロットを選択
            selected = select_target_media_in_modal(page, target_media_filename)
            if not selected:
                print(
                    f"  ❌ [二重検証①失敗] 本編動画 [{target_media_filename}] を選択できないため、画像への誤添付を防止するため中止します。"
                )
                continue
            time.sleep(0.3)

            # リトライ時に前回アップロード分がすでに反映完了していた場合は二重検証②を通過して正常完了
            if attempt > 1:
                already_ok, already_reason = verify_free_preview_attached_post_upload(
                    page, target_media_filename, fyp_basename
                )
                if already_ok:
                    print(f"  🔒 [二重検証②クリア] すでに無料プレビューが正常添付済みです: {already_reason}")
                    return True

            # 2. 【二重検証ガード①：添付前検証】クリック直前に再度アクティブ対象が本編動画か厳密チェック
            pre_ok, pre_reason = verify_main_video_selected_pre_attach(page, target_media_filename)
            if not pre_ok:
                print(f"  ❌ [二重検証①失敗] Add free preview 押下直前の検証NG: {pre_reason}")
                continue
            print(f"  🔒 [二重検証①クリア] 添付前ターゲット確認OK: {pre_reason}")

            # 3. ステージ左上の「+ Add free preview / + 無料プレビューを追加」（.upload-stage-badge）をクリックしてドロップダウンを開く
            add_preview_btn = page.locator(
                "app-account-media-upload-v2 .upload-stage-media .upload-stage-badge, "
                "app-account-media-upload-v2 .upload-stage-badge"
            ).locator("visible=true").first
            if add_preview_btn.count() == 0:
                add_preview_btn = page.locator(
                    "text=/Add free preview|無料プレビューを追加/i"
                ).locator("visible=true").first

            add_preview_btn.wait_for(state="visible", timeout=12000)
            badge_cls = add_preview_btn.get_attribute("class") or ""
            if "dropdown-open" not in badge_cls:
                add_preview_btn.click()
                time.sleep(0.7)

            # ドロップダウン直前でもう一度だけターゲットが本編動画であることを最終確認
            recheck_ok, recheck_reason = verify_main_video_selected_pre_attach(page, target_media_filename)
            if not recheck_ok:
                print(f"  ❌ [二重検証①失敗] ドロップダウン展開後のターゲット再検証NG: {recheck_reason}")
                page.keyboard.press("Escape")
                continue

            # 4. ドロップダウン内の「Upload a photo or video / 写真や動画をアップロード」（app-icon[name='upload']）をクリックしてアップロード
            # ※ サイドパネルの "Uploading with a co-performer..." や追加タイル ".upload-tile-add" への誤クリックを防ぐため .upload-stage-badge 内に限定
            upload_opt = page.locator(
                "app-account-media-upload-v2 .upload-stage-badge .dropdown-list .dropdown-item:has(app-icon[name='upload']), "
                "app-account-media-upload-v2 .upload-stage-badge .dropdown-list .dropdown-item:has(.fa-upload), "
                "app-account-media-upload-v2 .upload-stage-badge .dropdown-list .dropdown-item:has-text('Upload'), "
                "app-account-media-upload-v2 .upload-stage-badge .dropdown-list .dropdown-item:has-text('アップロード')"
            ).locator("visible=true").first

            uploaded = False
            if upload_opt.count() > 0:
                try:
                    with page.expect_file_chooser(timeout=5000) as fc_info:
                        upload_opt.click()
                    file_chooser = fc_info.value
                    file_chooser.set_files(fyp_preview_path)
                    print(
                        f"  ✔ [{target_media_filename}] の無料プレビュー枠へFYP動画をアップロード: {fyp_basename}"
                    )
                    uploaded = True
                except Exception as fc_err:
                    print(f"  ⚠️ FileChooser経由のアップロード失敗、単一ファイルinputへフォールバックします: {fc_err}")

            if not uploaded:
                # フォールバック: モーダル内のプレビュー用単一ファイル input[type='file']:not([multiple]) を優先使用
                # （multiple付きの input は本編メディア追加用のため除外）
                preview_input = page.locator(
                    "app-account-media-upload-v2 input[type='file']:not([multiple])"
                ).first
                if preview_input.count() > 0:
                    preview_input.set_input_files(fyp_preview_path)
                    print(
                        f"  ✔ [{target_media_filename}] の無料プレビュー専用inputへFYP動画をセット: {fyp_basename}"
                    )
                    uploaded = True
                else:
                    file_inputs = page.locator("app-account-media-upload-v2 input[type='file']")
                    if file_inputs.count() >= 2:
                        file_inputs.last.set_input_files(fyp_preview_path)
                        print(
                            f"  ✔ [{target_media_filename}] のモーダル末尾inputへFYP動画をセット: {fyp_basename}"
                        )
                        uploaded = True

            if not uploaded:
                print(f"  ⚠️ [{target_media_filename}] へのFYPプレビュー動画アップロード口を検出できませんでした")
                page.keyboard.press("Escape")
                continue

            # 5. 【二重検証ガード②：添付後検証】DOMへの反映を待機し、本編動画に正しく紐づき画像に誤爆していないか検査
            post_ok = False
            post_reason = ""
            for _ in range(15):
                time.sleep(0.8)
                post_ok, post_reason = verify_free_preview_attached_post_upload(
                    page, target_media_filename, fyp_basename
                )
                if post_ok:
                    break
                if "誤添付" in post_reason:
                    break

            if post_ok:
                print(f"  🔒 [二重検証②クリア] 添付後DOM検証OK: {post_reason}")
                return True

            print(f"  ❌ [二重検証②失敗] {post_reason}")
            if "誤添付" in post_reason:
                # 画像側に誤添付された場合はモーダルをキャンセルして即座にエラー停止（誤投稿完全防止）
                _cancel_media_modal_safely(page)
                return False

        except Exception as e:
            print(f"  ⚠️ 無料プレビュー設定ステップで例外発生 (attempt {attempt}/{max_retries}): {e}")

    print(f"  🛑 本編動画 [{target_media_filename}] へのFYP無料プレビュー二重検証が通らなかったため、安全停止します。")
    _cancel_media_modal_safely(page)
    return False


def ensure_post_to_fyp_enabled(page):
    """無料プレビュー添付後、FYP投稿（Post to FYP / FYPに投稿する）のトグル・チップがOFFならONにする"""
    try:
        fyp_btn = page.locator(
            "app-post-creation .post-option-chip:has(app-icon[name='fire']), "
            "app-post-creation .post-option-chip:has(.fa-fire), "
            "app-post-create button:has-text('Post to FYP'), "
            "app-post-create .btn:has-text('Post to FYP')"
        ).locator("visible=true").first
        if fyp_btn.count() > 0:
            cls = fyp_btn.get_attribute("class") or ""
            aria_pressed = fyp_btn.get_attribute("aria-pressed") or ""
            if "active" not in cls.lower() and "selected" not in cls.lower() and aria_pressed != "true":
                fyp_btn.click()
                time.sleep(0.4)
                print("  ✔ 「Post to FYP（FYPに投稿する）」をONに有効化！")
    except Exception:
        pass


def open_schedule_modal(page) -> bool:
    toolbar_icons = page.locator(
        "app-post-create i.fal, app-post-create i.far, app-post-create i.fas"
    ).locator("visible=true")
    if toolbar_icons.count() == 0:
        toolbar_icons = page.locator(
            ".fa-calendar, .fa-calendar-clock, .fa-calendar-plus, .fa-clock"
        ).locator("visible=true")

    total = toolbar_icons.count()
    for i in range(total):
        icon = toolbar_icons.nth(i)
        box = icon.bounding_box()
        if not box or box["y"] < 250:
            continue

        icon.click()
        time.sleep(0.8)

        has_schedule_header = page.locator("text=/^Schedule$|^スケジュール$/i").locator("visible=true").count() > 0
        has_confirm_date = page.locator("text=/Confirm Date|日付を確認|確定/i").locator("visible=true").count() > 0
        is_expire = page.locator("text=/Expire|有効期限/i").locator("visible=true").count() > 0

        if (has_schedule_header or has_confirm_date) and not is_expire:
            return True

        cancel_btn = page.locator("text=/^Cancel$|^キャンセル$/i").locator("visible=true").first
        if cancel_btn.count() > 0:
            cancel_btn.click()
            time.sleep(0.4)

    return False


def select_time_by_keyboard(page, click_x: float, click_y: float, target_int: int):
    """
    Fanslyの時・分プルダウンのキーボード操作仕様に対応：
    1. トグルをクリックして開く
    2. `Home` キーで一番上（00）へ移動
    3. 10の位の数字キー（'0', '1', '2', ...）を 1 + (target_int % 10) 回連打
    4. `Enter` キーで確定
    """
    tens_digit = str(target_int // 10)
    ones_digit = target_int % 10
    press_count = 1 + ones_digit

    page.mouse.click(click_x, click_y)
    time.sleep(0.35)

    page.keyboard.press("Home")
    time.sleep(0.15)

    for _ in range(press_count):
        page.keyboard.press(tens_digit)
        time.sleep(0.08)

    page.keyboard.press("Enter")
    time.sleep(0.25)


def fill_fansly_schedule_modal(page, scheduled_at_str: str) -> bool:
    dt = datetime.strptime(scheduled_at_str.strip()[:16], "%Y-%m-%d %H:%M")
    target_month_label = f"{MONTH_NAMES[dt.month]} {dt.year}"
    target_day_str = str(dt.day)

    for _ in range(12):
        if page.get_by_text(target_month_label, exact=True).locator("visible=true").count() > 0:
            break
        next_btn = page.locator(".fa-chevron-right, .fa-angle-right").locator("visible=true").first
        if next_btn.count() > 0:
            next_btn.click()
            time.sleep(0.4)
        else:
            break

    day_cells = page.get_by_text(target_day_str, exact=True).locator("visible=true")
    valid_cells = []
    for idx in range(day_cells.count()):
        cell = day_cells.nth(idx)
        box = cell.bounding_box()
        if box and 150 < box["y"] < 520:
            valid_cells.append(cell)

    if valid_cells:
        target_cell = valid_cells[0] if dt.day <= 15 else valid_cells[-1]
        target_cell.click()
        time.sleep(0.4)

    btn_24h = page.get_by_text("24h", exact=True).locator("visible=true").first
    if btn_24h.count() > 0:
        btn_24h.click()
        time.sleep(0.3)

    time_label = page.locator("text=/^Time$|^時間$/i").locator("visible=true").first
    t_box = time_label.bounding_box()
    if t_box:
        hour_click_x = t_box["x"] + 35
        min_click_x = t_box["x"] + 135
        box_click_y = t_box["y"] + 42

        select_time_by_keyboard(page, hour_click_x, box_click_y, dt.hour)
        select_time_by_keyboard(page, min_click_x, box_click_y, dt.minute)
        print(f"  ✔ 時刻を [{dt.hour:02d}:{dt.minute:02d}] にセット完了（Home + 数字キー確定）")
    else:
        print("  ⚠️ Timeラベル座標の取得に失敗")

    confirm_btn = page.locator(".modal-footer button, .modal-footer .btn").locator("visible=true").last
    if confirm_btn.count() == 0:
        confirm_btn = page.locator("text=/Confirm Date|日付を確認/i").locator("visible=true").first

    if confirm_btn.count() > 0:
        confirm_btn.click(force=True)
        time.sleep(0.8)
        return True

    return False


def wait_for_media_ready_and_submit(
    page, timeout_sec: int = 180, idx: int = 1, total_q: int = 1, folder_name: str = ""
) -> bool:
    start_t = time.time()
    print("  ⏳ メディアの処理・サーバーエンコード承認完了を監視中...")

    while time.time() - start_t < timeout_sec:
        elapsed_sec = int(time.time() - start_t)
        wait_pct = min(94, 82 + elapsed_sec // 2)
        emit_progress(
            pct=wait_pct,
            completed=idx - 1,
            total=total_q,
            stage=f"[{idx}/{total_q}] No.{folder_name} メディアエンコード待機中 ({elapsed_sec}秒)...",
        )

        spinners = page.locator(
            "app-post-create .fa-spinner, app-post-create .fa-spin, "
            "app-post-create mat-spinner, app-post-create .progress-bar"
        ).locator("visible=true")
        has_spinner = spinners.count() > 0

        candidates = page.locator(
            "button:has-text('Schedule'), .btn:has-text('Schedule'), "
            "button:has-text('スケジュール'), .btn:has-text('スケジュール'), "
            "button:has-text('Post'), .btn:has-text('Post'), "
            "button:has-text('投稿'), .btn:has-text('投稿')"
        ).locator("visible=true")

        target_btn = None
        for i in range(candidates.count()):
            btn = candidates.nth(i)
            txt = btn.inner_text().strip()
            if txt not in ("Schedule", "Post", "スケジュール", "投稿"):
                continue
            box = btn.bounding_box()
            if box and box["y"] > 220 and box["width"] >= 60:
                target_btn = btn

        if target_btn and not has_spinner:
            is_disabled = target_btn.evaluate(
                """el => el.disabled === true
                      || el.classList.contains('disabled')
                      || el.getAttribute('aria-disabled') === 'true'
                      || window.getComputedStyle(el).pointerEvents === 'none'
                      || parseFloat(window.getComputedStyle(el).opacity) < 0.7"""
            )
            if not is_disabled:
                elapsed = time.time() - start_t
                print(f"  ✔ エンコード承認完了！ ({elapsed:.1f}秒)")
                time.sleep(0.5)
                target_btn.click()
                emit_progress(
                    pct=96,
                    completed=idx - 1,
                    total=total_q,
                    stage=f"[{idx}/{total_q}] No.{folder_name} 予約データ送信中...",
                )
                print("  ⏳ 予約データ送信中（完了待機）...")
                time.sleep(4)
                return True

        time.sleep(1.0)

    print("  ❌ タイムアウト！")
    return False


def archive_posted_folder(folder_path: str, folder_name: str, posted_root: str = POSTED_ROOT) -> str:
    """予約確定後のフォルダに posted メタ情報を記録して 03_posted/ へ移動する"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    final_json_path = os.path.join(folder_path, "final_post.json")
    if os.path.isfile(final_json_path):
        try:
            with open(final_json_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            if isinstance(meta, dict):
                meta["status"] = "posted"
                meta["posted"] = True
                meta["posted_at"] = now_str
                with open(final_json_path, "w", encoding="utf-8") as f:
                    json.dump(meta, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    os.makedirs(posted_root, exist_ok=True)
    dst_path = os.path.join(posted_root, f"{folder_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    if os.path.exists(dst_path):
        shutil.rmtree(dst_path)
    shutil.move(folder_path, dst_path)
    return dst_path


def process_single_post(
    page, item: dict, char_id: str = CHAR_ID, posted_root: str = POSTED_ROOT, idx: int = 1, total_q: int = 1
) -> bool:
    """
    SPEC_LATEST.md「6. 第2ライン（factory_line2.py）固定1パターン実行仕様」に基づき、
    条件分岐のない完全固定シーケンスで Fansly 自動予約を実行する。
    """
    folder_name = item["_folder_name"]
    folder_path = item["_folder_path"]
    full_text = build_full_text_fallback(item)
    scheduled_at = str(item.get("scheduled_at") or "").strip()
    paid_videos = item.get("_paid_video_paths", [])
    paid_images = item.get("_paid_image_paths", [])
    fyp_preview_path = item.get("_fyp_preview_path")

    if not paid_videos or not fyp_preview_path:
        print(f"⚠️ [No.{folder_name}] 必須動画（main_video / fyp_preview_video）が揃っていないためスキップします。")
        emit_progress(
            pct=100,
            completed=idx,
            total=total_q,
            stage=f"[{idx}/{total_q}] No.{folder_name} スキップ (必須動画不足)",
        )
        return False

    main_video_path = paid_videos[0]
    preview_target_filename = os.path.basename(main_video_path)
    paid_media_paths = [main_video_path] + list(paid_images)

    emit_progress(
        pct=10,
        completed=idx - 1,
        total=total_q,
        stage=f"[{idx}/{total_q}] No.{folder_name} Fansly投稿ページを開いています...",
    )

    print("\n" + "-" * 55)
    print(f"🚀 [{char_id}] キュー [No.{folder_name}] の予約セット開始 ({idx}/{total_q})")
    print(f"   ⏰ 予定日時       : {scheduled_at}")
    print(f"   🔒 本編メディア   : 動画 1 本 ({preview_target_filename}) ＋ 画像 {len(paid_images)} 枚")
    print(f"   🎯 プレビュー紐付先: {preview_target_filename}")
    print(f"   🌐 FYP枠専用動画  : {os.path.basename(fyp_preview_path)}")
    print(f"   📝 本文抜粋       : {full_text[:45]}...")
    print("-" * 55)

    # 1. https://fansly.com/home を開く
    page.goto(FANSLY_HOME_URL, wait_until="domcontentloaded")
    time.sleep(3)

    # 2. 投稿テキストエリアに full_text を入力
    emit_progress(
        pct=20,
        completed=idx - 1,
        total=total_q,
        stage=f"[{idx}/{total_q}] No.{folder_name} キャプション・タグを入力中...",
    )
    try:
        composer = page.locator("textarea, [contenteditable='true']").locator("visible=true").first
        composer.click()
        time.sleep(0.5)
        composer.fill(full_text)
        print("  ✔ キャプション＆ハッシュタグ入力完了")
        emit_progress(
            pct=28,
            completed=idx - 1,
            total=total_q,
            stage=f"[{idx}/{total_q}] No.{folder_name} キャプション入力完了",
        )
    except Exception as e:
        print(f"  ⚠️ テキスト入力で手動補助が必要かも: {e}")

    # 3. 本編メディア群（[main_video] + images）を一括アップロード → Mediaモーダル表示待機
    # 4. main_video サムネイルを選択 → + Add free preview → Upload から fyp_preview_video を添付
    # 5. Add to post をクリック → Post to FYP トグルをON確認・有効化
    try:
        emit_progress(
            pct=38,
            completed=idx - 1,
            total=total_q,
            stage=f"[{idx}/{total_q}] No.{folder_name} 本編動画＋画像({len(paid_images)}枚)をアップロード中...",
        )
        file_input = page.locator("input[type='file']").first
        file_input.set_input_files(paid_media_paths)
        print(f"  ✔ 本編メディア（動画 1 本 ＋ 画像 {len(paid_images)} 枚）をアップロード中...")

        add_to_post_btn = page.locator("text=/Add to post|投稿に追加/i").locator("visible=true").first
        add_to_post_btn.wait_for(state="visible", timeout=30000)
        time.sleep(2.5)

        emit_progress(
            pct=52,
            completed=idx - 1,
            total=total_q,
            stage=f"[{idx}/{total_q}] No.{folder_name} FYP無料プレビュー動画を添付中...",
        )
        preview_attached = attach_free_preview(
            page,
            target_media_filename=preview_target_filename,
            fyp_preview_path=fyp_preview_path
        )
        if not preview_attached:
            print(
                f"  🛑 [No.{folder_name}] 二重検証ガードにより無料プレビューの本編動画紐づけ失敗を検知！"
                "誤投稿を防止するためこの投稿の予約処理を即座に中止します。"
            )
            emit_progress(
                pct=100,
                completed=idx,
                total=total_q,
                stage=f"[{idx}/{total_q}] No.{folder_name} エラー停止 (FYPプレビュー二重検証NG)",
            )
            return False

        add_to_post_btn.click()
        print("  ✔ 「投稿に追加 / Add to post」をクリックしてメディア添付を確定！")
        time.sleep(1.5)

        composer_ok, composer_reason = verify_post_cards_preview_on_composer(page)
        if not composer_ok:
            print(f"  🛑 [No.{folder_name}] 添付確定後の最終検証NG: {composer_reason} 誤投稿防止のため中止します。")
            emit_progress(
                pct=100,
                completed=idx,
                total=total_q,
                stage=f"[{idx}/{total_q}] No.{folder_name} エラー停止 (画像誤添付検知)",
            )
            return False

        ensure_post_to_fyp_enabled(page)
        emit_progress(
            pct=68,
            completed=idx - 1,
            total=total_q,
            stage=f"[{idx}/{total_q}] No.{folder_name} メディア添付確定・FYP二重検証完了",
        )
    except Exception as e:
        print(f"  🛑 メディアアップロード / 無料プレビュー作成中にエラーが発生したため安全停止します: {e}")
        emit_progress(
            pct=100,
            completed=idx,
            total=total_q,
            stage=f"[{idx}/{total_q}] No.{folder_name} エラー停止 (メディア/プレビュー処理失敗)",
        )
        return False

    # 6. Schedule モーダルを開き、年月送り・日付セル・時分キーボード入力・Confirm Date クリック
    if scheduled_at:
        try:
            emit_progress(
                pct=74,
                completed=idx - 1,
                total=total_q,
                stage=f"[{idx}/{total_q}] No.{folder_name} 予約日時 ({scheduled_at}) を設定中...",
            )
            opened = open_schedule_modal(page)
            if opened:
                print("  ✔ 『Schedule』モーダルをオープン！")
                ok = fill_fansly_schedule_modal(page, scheduled_at)
                if ok:
                    print(f"  ✔ 予約日時 [{scheduled_at}] のセット＆Confirm Date完了！")
                    emit_progress(
                        pct=80,
                        completed=idx - 1,
                        total=total_q,
                        stage=f"[{idx}/{total_q}] No.{folder_name} 予約日時セット完了",
                    )
                else:
                    print(f"  ⚠️ 日時入力の途中で手動補助が必要です（目標: {scheduled_at}）")
            else:
                print(f"  ⚠️ Scheduleアイコン自動判別に失敗。ブラウザ上で [{scheduled_at}] を手動セットしてください")
        except Exception as e:
            print(f"  ⚠️ スケジュール設定エラー: {e}")

    # 7. エンコード・アップロード完了待機後に Schedule ボタンをクリックして確定
    if AUTO_CLICK_SUBMIT:
        try:
            success = wait_for_media_ready_and_submit(
                page, timeout_sec=180, idx=idx, total_q=total_q, folder_name=folder_name
            )
            if success:
                print(f"  🎉 [No.{folder_name}] 完全自動予約投稿が完了しました！")
            else:
                print(f"  ❌ [No.{folder_name}] サーバー承認タイムアウトまたは確定ボタン未検出のためスキップします。")
        except Exception as e:
            print(f"  ❌ 確定処理エラー: {e}")
            success = False
    else:
        print("\n🛑 【安全装置：最終目視確認ストップ】")
        print(f"   ブラウザ画面で「メディア・テキスト・予約日時 [{scheduled_at}]」を確認し、")
        print("   右下の青い「Schedule」ボタンを押して予約を完了させてください！")
        if _is_interactive_stdin():
            ans = input("👉 予約完了したら [Enter]（スキップして次へ行くなら s + Enter）: ").strip().lower()
            success = (ans != "s")
        else:
            print("⚠️ 非対話モードで AUTO_CLICK_SUBMIT=False のため、未確定のまま中断します。")
            success = False

    # 8. 完了した対象フォルダを 03_posted/ へ移動
    if success:
        dst_path = archive_posted_folder(folder_path, folder_name, posted_root=posted_root)
        print(f"  📦 アーカイブ完了 -> {dst_path}")
        emit_progress(
            pct=100,
            completed=idx,
            total=total_q,
            stage=f"[{idx}/{total_q}] No.{folder_name} Fansly予約投稿完了！",
        )
        return True

    return False


def main():
    args, _ = parser.parse_known_args()
    char_id = args.char or CHAR_ID
    paths = get_char_paths(char_id)
    ready_root = paths["ready_dir"]
    posted_root = paths["posted_dir"]
    profile_dir = paths["browser_profile"]

    os.makedirs(ready_root, exist_ok=True)
    os.makedirs(posted_root, exist_ok=True)
    os.makedirs(profile_dir, exist_ok=True)

    char_display_name = load_character_display_name(char_id)
    is_headless = resolve_line2_headless(args.headless)
    mode_str = "【完全無人・自動確定モード】" if AUTO_CLICK_SUBMIT else "【安全装置・目視確認モード】"
    browser_mode_str = "👻 ヘッドレス（バックグラウンド）" if is_headless else "🖥️ 画面表示あり"

    print("==================================================")
    print(f"👑 {char_display_name} ({char_id}) - 第2ライン (Fansly予約ボット)")
    print(f"   🤖 モード: {mode_str} / {browser_mode_str}")
    print("==================================================")

    queue = load_ready_queue(ready_root)
    total_q = len(queue)
    emit_progress(
        pct=0,
        completed=0,
        total=total_q,
        stage=f"第2ライン開始 (予約待機: {total_q}件)" if total_q > 0 else "予約待ちの投稿はありません",
    )
    if not queue:
        print(f"📭 [{char_id}] の 02_ready_to_post に予約待ちの投稿はありません。")
        return

    print(f"📋 予約待機中の投稿: {total_q} 件")
    for idx, q in enumerate(queue, 1):
        v_name = os.path.basename(q["_paid_video_paths"][0]) if q.get("_paid_video_paths") else "なし"
        i_cnt = len(q.get("_paid_image_paths", []))
        fyp_name = os.path.basename(q["_fyp_preview_path"]) if q.get("_fyp_preview_path") else "なし"
        print(
            f"  {idx}. [No.{q['_folder_name']}] ⏰ {q.get('scheduled_at')} "
            f"(本編: 🎬{v_name} + 🖼️{i_cnt}枚 | FYP枠: {fyp_name})"
        )

    emit_progress(pct=5, completed=0, total=total_q, stage="Fanslyブラウザを起動・ログイン確認中...")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=is_headless,
            viewport={"width": 1360, "height": 900},
            args=["--disable-blink-features=AutomationControlled"]
        )
        page = context.pages[0] if context.pages else context.new_page()

        if not ensure_logged_in(page, char_display_name, char_id=char_id, profile_dir=profile_dir, headless=is_headless):
            emit_progress(
                pct=100,
                completed=0,
                total=total_q,
                stage=f"エラー: [{char_id}] 未ログイン（画面表示モードでログインが必要）",
            )
            context.close()
            return

        completed = 0
        for idx, item in enumerate(queue, 1):
            if process_single_post(
                page, item, char_id=char_id, posted_root=posted_root, idx=idx, total_q=total_q
            ):
                completed += 1

        print("\n==================================================")
        print(f"🏁 [{char_display_name}] 第2ライン稼働完了！ 処理済み: {completed} / {len(queue)} 件")
        print("==================================================")
        time.sleep(2)
        context.close()


if __name__ == "__main__":
    main()