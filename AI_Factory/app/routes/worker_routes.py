import os
import json
import shutil
from datetime import datetime
from flask import Blueprint, request, jsonify, Response

from app.config import (
    BASE_DIR,
    LINE1_MAX_REPEAT,
    load_app_settings,
    save_app_settings,
    get_line2_headless,
    set_line2_headless,
)
from app.services.worker_service import (
    worker_state,
    line2_state,
    worker_lock,
    enqueue_gpu_task,
    cancel_queued_gpu_task,
    clear_queued_gpu_tasks,
    get_gpu_queue_snapshot,
    start_line2_parallel,
    stop_workers,
)
from app.services.character_service import get_char_paths
from app.services.file_service import get_videos_in_dir, get_images_in_dir, normalize_video_model

worker_bp = Blueprint("worker_bp", __name__)


@worker_bp.route("/api/worker/status")
def api_worker_status():
    since = int(request.args.get("since", 0))
    with worker_lock:
        logs_list = list(worker_state["logs"])
        l2_logs_list = list(line2_state["logs"])
        gpu_snap = {
            "running": worker_state["running"],
            "task_id": worker_state.get("task_id"),
            "char_id": worker_state["char_id"],
            "all_chars": bool(worker_state.get("all_chars")),
            "line": worker_state["line"],
            "current_iter": worker_state["current_iter"],
            "total_iter": worker_state["total_iter"],
            "total_items": worker_state["total_items"],
            "completed_items": worker_state["completed_items"],
            "item_progress": worker_state["item_progress"],
            "current_stage": worker_state["current_stage"],
        }
        l2_snap = {
            "running": line2_state["running"],
            "char_id": line2_state["char_id"],
            "all_chars": bool(line2_state.get("all_chars")),
            "headless": bool(line2_state.get("headless", False)),
            "line": "line2",
            "current_iter": line2_state["current_iter"],
            "total_iter": line2_state["total_iter"],
            "total_items": line2_state["total_items"],
            "completed_items": line2_state["completed_items"],
            "item_progress": line2_state["item_progress"],
            "current_stage": line2_state["current_stage"],
            "logs": l2_logs_list,
        }

    queue_list = get_gpu_queue_snapshot()
    saved_headless = get_line2_headless()
    # トップレベル互換性: GPU稼働中はGPUを優先表示し、GPU待機中で第2ラインのみ稼働中は第2ラインを反映
    primary = gpu_snap if (gpu_snap["running"] or not l2_snap["running"]) else l2_snap

    return jsonify({
        "running": bool(gpu_snap["running"] or l2_snap["running"]),
        "gpu_running": gpu_snap["running"],
        "line2_running": l2_snap["running"],
        "line2_headless": saved_headless,
        "char_id": primary["char_id"],
        "all_chars": primary["all_chars"],
        "line": primary["line"],
        "current_iter": primary["current_iter"],
        "total_iter": primary["total_iter"],
        "logs": logs_list[since:],
        "next_index": len(logs_list),
        "total_items": primary["total_items"],
        "completed_items": primary["completed_items"],
        "item_progress": primary["item_progress"],
        "current_stage": primary["current_stage"],
        "gpu_worker": gpu_snap,
        "gpu_queue": queue_list,
        "queue_count": len(queue_list),
        "line2_worker": l2_snap,
        "max_repeat": LINE1_MAX_REPEAT,
    })


@worker_bp.route("/api/worker/queue")
def api_worker_queue():
    """現在実行中のGPUタスク・待機中FIFOキュー・第2ライン並列稼働状態を返す"""
    with worker_lock:
        current_gpu = {
            "running": worker_state["running"],
            "task_id": worker_state.get("task_id"),
            "line": worker_state["line"],
            "char_id": worker_state["char_id"],
            "all_chars": bool(worker_state.get("all_chars")),
            "current_iter": worker_state["current_iter"],
            "total_iter": worker_state["total_iter"],
            "total_items": worker_state["total_items"],
            "completed_items": worker_state["completed_items"],
            "item_progress": worker_state["item_progress"],
            "current_stage": worker_state["current_stage"],
        }
        current_line2 = {
            "running": line2_state["running"],
            "char_id": line2_state["char_id"],
            "all_chars": bool(line2_state.get("all_chars")),
            "total_items": line2_state["total_items"],
            "completed_items": line2_state["completed_items"],
            "item_progress": line2_state["item_progress"],
            "current_stage": line2_state["current_stage"],
        }
    return jsonify({
        "ok": True,
        "current_gpu": current_gpu,
        "queue": get_gpu_queue_snapshot(),
        "line2": current_line2,
    })


@worker_bp.route("/api/worker/queue/cancel", methods=["POST"])
def api_worker_queue_cancel():
    """待機中GPUキューの個別キャンセル、または clear_all=True で全件クリア"""
    data = request.json or {}
    if data.get("clear_all"):
        cleared = clear_queued_gpu_tasks()
        return jsonify({"ok": True, "cleared": cleared, "queue": get_gpu_queue_snapshot()})

    task_id = data.get("task_id") or data.get("id")
    res = cancel_queued_gpu_task(task_id)
    status_code = 200 if res.get("ok") else 404
    res["queue"] = get_gpu_queue_snapshot()
    return jsonify(res), status_code


@worker_bp.route("/api/run/line1", methods=["POST"])
def api_run_line1():
    data = request.json or {}
    char_id = data.get("char_id", "queen_lilith")
    all_chars = bool(data.get("all_chars")) or str(char_id).strip().upper() == "ALL"
    repeat_count = max(1, min(LINE1_MAX_REPEAT, int(data.get("repeat", 1))))
    res = enqueue_gpu_task("line1", char_id=char_id, repeat=repeat_count, all_chars=all_chars)
    return jsonify(res)


@worker_bp.route("/api/run/video", methods=["POST"])
def api_run_video():
    data = request.json or {}
    char_id = data.get("char_id", "queen_lilith")
    all_chars = bool(data.get("all_chars")) or str(char_id).strip().upper() == "ALL"
    res = enqueue_gpu_task("video", char_id=char_id, repeat=1, all_chars=all_chars)
    return jsonify(res)


@worker_bp.route("/api/run/line2", methods=["POST"])
def api_run_line2():
    data = request.json or {}
    char_id = data.get("char_id", "queen_lilith")
    all_chars = bool(data.get("all_chars")) or str(char_id).strip().upper() == "ALL"
    headless_override = None
    if "line2_headless" in data:
        headless_override = data.get("line2_headless")
    elif "headless" in data:
        headless_override = data.get("headless")
    res = start_line2_parallel(char_id=char_id, all_chars=all_chars, headless=headless_override)
    return jsonify(res)


@worker_bp.route("/api/worker/settings", methods=["GET", "POST"])
@worker_bp.route("/api/settings/line2", methods=["GET", "POST"])
@worker_bp.route("/api/settings", methods=["GET", "POST"])
def api_worker_settings():
    """第2ライン（Playwright）のヘッドレスモード等のアプリ共通設定を取得・保存する API"""
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        if "line2_headless" in data:
            set_line2_headless(data.get("line2_headless"))
        elif "headless" in data:
            set_line2_headless(data.get("headless"))
        elif data:
            save_app_settings(data)

    settings = load_app_settings()
    headless_val = bool(settings.get("line2_headless", False))
    return jsonify({
        "ok": True,
        "line2_headless": headless_val,
        "headless": headless_val,
        "settings": settings,
    })


def _resolve_manual_path() -> str:
    candidates = [
        os.path.join(BASE_DIR, "docs", "USER_MANUAL.md"),
        os.path.join(BASE_DIR, "USER_MANUAL.md"),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    return candidates[0]


def _render_manual_standalone_html(md_text: str) -> str:
    safe_json = json.dumps(md_text, ensure_ascii=False).replace("</", "<\\/")
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>📖 AI Content Factory 操作マニュアル</title>
  <style>
    :root {{
      --bg-dark: #0e0c15;
      --bg-panel: #161321;
      --bg-card: #1f1b2e;
      --border: #2e2842;
      --accent: #a855f7;
      --text-main: #f1f5f9;
      --text-muted: #94a3b8;
      --sky: #38bdf8;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: 'Segoe UI', 'Hiragino Sans', 'Meiryo', sans-serif;
      background: var(--bg-dark);
      color: var(--text-main);
      line-height: 1.7;
    }}
    header {{
      position: sticky;
      top: 0;
      z-index: 100;
      background: rgba(18, 15, 28, 0.96);
      border-bottom: 1px solid var(--border);
      padding: 10px 24px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      backdrop-filter: blur(8px);
    }}
    .title {{ font-weight: 800; font-size: 15px; color: #e9d5ff; }}
    .close-btn {{
      background: var(--bg-card);
      color: #e2e8f0;
      border: 1px solid var(--border);
      border-radius: 7px;
      padding: 6px 14px;
      font-size: 12px;
      font-weight: 700;
      cursor: pointer;
    }}
    .close-btn:hover {{ border-color: var(--accent); color: #fff; }}
    main {{
      max-width: 980px;
      margin: 24px auto 64px;
      background: var(--bg-panel);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 28px 36px;
      box-shadow: 0 12px 36px rgba(0, 0, 0, 0.55);
    }}
    h1 {{ font-size: 22px; color: #f3e8ff; border-bottom: 2px solid var(--accent); padding-bottom: 10px; margin-top: 0; }}
    h2 {{ font-size: 18px; color: #e9d5ff; border-left: 4px solid var(--accent); padding-left: 10px; margin-top: 30px; }}
    h3 {{ font-size: 15px; color: #7dd3fc; margin-top: 22px; }}
    p, li {{ font-size: 13.5px; color: #e2e8f0; }}
    ul, ol {{ padding-left: 22px; }}
    li {{ margin: 5px 0; }}
    hr {{ border: none; border-top: 1px solid var(--border); margin: 24px 0; }}
    blockquote {{
      margin: 14px 0;
      padding: 10px 16px;
      background: rgba(168, 85, 247, 0.1);
      border-left: 4px solid var(--accent);
      border-radius: 6px;
      color: #ddd6fe;
      font-size: 13px;
    }}
    code {{
      font-family: Consolas, monospace;
      background: #0b0912;
      color: #f9a8d4;
      border: 1px solid var(--border);
      border-radius: 4px;
      padding: 1px 6px;
      font-size: 12.5px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      margin: 14px 0;
      font-size: 13px;
    }}
    th, td {{
      border: 1px solid var(--border);
      padding: 8px 12px;
      text-align: left;
      vertical-align: top;
    }}
    th {{ background: var(--bg-card); color: #e9d5ff; font-weight: 700; }}
    tr:nth-child(even) td {{ background: rgba(255, 255, 255, 0.02); }}
    a {{ color: var(--sky); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
  </style>
</head>
<body>
  <header>
    <div class="title">📖 AI Content Factory 運用・操作マニュアル</div>
    <button type="button" class="close-btn" onclick="window.close()">✖ ウィンドウを閉じる</button>
  </header>
  <main id="manual-root"></main>
  <script>
    const RAW_MD = {safe_json};
    function esc(s) {{
      return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }}
    function slugify(text) {{
      return String(text || '')
        .trim()
        .toLowerCase()
        .replace(/[`*_~]/g, '')
        .replace(/[^\\w\\u3000-\\u9fff\\uff00-\\uffef\\s-]/g, '')
        .replace(/\\s+/g, '-');
    }}
    function inlineMd(s) {{
      let out = esc(s);
      out = out.replace(/`([^`]+)`/g, '<code>$1</code>');
      out = out.replace(/\\*\\*([^*]+)\\*\\*/g, '<strong>$1</strong>');
      out = out.replace(/\\*([^*]+)\\*/g, '<em>$1</em>');
      out = out.replace(/\\[([^\\]]+)\\]\\(([^)]+)\\)/g, '<a href="$2">$1</a>');
      return out;
    }}
    function renderMd(md) {{
      const cleaned = String(md || '').replace(/<!--[\\s\\S]*?-->/g, '');
      const lines = cleaned.split(/\\r?\\n/);
      let html = '';
      let inUl = false, inOl = false, inTable = false, tableHeadDone = false;
      const closeLists = () => {{
        if (inUl) {{ html += '</ul>'; inUl = false; }}
        if (inOl) {{ html += '</ol>'; inOl = false; }}
      }};
      const closeTable = () => {{
        if (inTable) {{ html += '</tbody></table>'; inTable = false; tableHeadDone = false; }}
      }};
      for (let i = 0; i < lines.length; i++) {{
        const raw = lines[i];
        const t = raw.trim();
        if (!t) {{ closeLists(); closeTable(); continue; }}
        if (/^---+$/.test(t)) {{ closeLists(); closeTable(); html += '<hr>'; continue; }}
        if (t.startsWith('|') && t.endsWith('|')) {{
          closeLists();
          const cells = t.slice(1, -1).split('|').map(c => c.trim());
          if (cells.every(c => /^:?-{2,}:?$/.test(c))) continue;
          if (!inTable) {{
            html += '<table><thead><tr>' + cells.map(c => '<th>' + inlineMd(c) + '</th>').join('') + '</tr></thead><tbody>';
            inTable = true;
            tableHeadDone = true;
          }} else {{
            html += '<tr>' + cells.map(c => '<td>' + inlineMd(c) + '</td>').join('') + '</tr>';
          }}
          continue;
        }} else {{
          closeTable();
        }}
        const hm = t.match(/^(#{{1,3}})\\s+(.+)$/);
        if (hm) {{
          closeLists();
          const lv = hm[1].length;
          const id = slugify(hm[2]);
          html += `<h${{lv}} id="${{id}}">${{inlineMd(hm[2])}}</h${{lv}}>`;
          continue;
        }}
        if (t.startsWith('>')) {{
          closeLists();
          html += '<blockquote>' + inlineMd(t.replace(/^>\\s?/, '')) + '</blockquote>';
          continue;
        }}
        const olm = t.match(/^\\d+\\.\\s+(.+)$/);
        if (olm) {{
          if (inUl) {{ html += '</ul>'; inUl = false; }}
          if (!inOl) {{ html += '<ol>'; inOl = true; }}
          html += '<li>' + inlineMd(olm[1]) + '</li>';
          continue;
        }}
        const ulm = t.match(/^[*+-]\\s+(.+)$/);
        if (ulm) {{
          if (inOl) {{ html += '</ol>'; inOl = false; }}
          if (!inUl) {{ html += '<ul>'; inUl = true; }}
          html += '<li>' + inlineMd(ulm[1]) + '</li>';
          continue;
        }}
        closeLists();
        html += '<p>' + inlineMd(t) + '</p>';
      }}
      closeLists();
      closeTable();
      return html;
    }}
    document.getElementById('manual-root').innerHTML = renderMd(RAW_MD);
  </script>
</body>
</html>"""


@worker_bp.route("/api/manual")
def api_manual():
    """アプリ操作マニュアル（docs/USER_MANUAL.md）の内容を JSON で返す API"""
    manual_path = _resolve_manual_path()
    if not os.path.isfile(manual_path):
        return jsonify({"ok": False, "error": "マニュアルファイル (docs/USER_MANUAL.md) が見つかりません"}), 404
    try:
        with open(manual_path, "r", encoding="utf-8") as f:
            content = f.read()
        return jsonify({
            "ok": True,
            "title": "AI Content Factory 運用・操作マニュアル",
            "path": os.path.relpath(manual_path, BASE_DIR),
            "content": content,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": f"マニュアル読込エラー: {str(e)}"}), 500


@worker_bp.route("/manual")
def view_manual_page():
    """アプリ操作マニュアルを別タブ／独立ウィンドウでも綺麗に閲覧できる HTML エンドポイント"""
    manual_path = _resolve_manual_path()
    if not os.path.isfile(manual_path):
        return Response("マニュアルファイル (docs/USER_MANUAL.md) が見つかりません。", status=404, mimetype="text/plain; charset=utf-8")
    try:
        with open(manual_path, "r", encoding="utf-8") as f:
            content = f.read()
        return Response(_render_manual_standalone_html(content), mimetype="text/html; charset=utf-8")
    except Exception as e:
        return Response(f"マニュアル読込エラー: {str(e)}", status=500, mimetype="text/plain; charset=utf-8")


@worker_bp.route("/api/worker/stop", methods=["POST"])
def api_worker_stop():
    data = request.get_json(silent=True) or {}
    target = data.get("target", "all")  # 'gpu' | 'line2' | 'all'
    clear_queue = bool(data.get("clear_queue", False))
    res = stop_workers(target=target, clear_queue=clear_queue)
    return jsonify(res)


# ==========================================
# Phase 2: Step 2 動画キュー管理 API
# ==========================================
@worker_bp.route("/api/video_queue/<char_id>")
def api_video_queue(char_id):
    """Step1で動画ベース指定された候補フォルダと、確定済み動画ロック中フォルダの一覧を返す"""
    paths = get_char_paths(char_id)
    cand_root = paths["candidates"]
    ready_root = paths["ready"]
    items = []

    # 1. 01_candidates 内の動画タスク
    if os.path.exists(cand_root):
        for folder in sorted(os.listdir(cand_root)):
            f_path = os.path.join(cand_root, folder)
            if not os.path.isdir(f_path):
                continue
            post_json = os.path.join(f_path, "post_data.json")
            if not os.path.exists(post_json):
                continue
            try:
                with open(post_json, "r", encoding="utf-8") as f:
                    pdata = json.load(f)
            except Exception:
                continue

            vt = pdata.get("video_task") or {}
            if not vt.get("enabled"):
                continue

            vids = get_videos_in_dir(f_path)
            imgs = get_images_in_dir(f_path)
            status = vt.get("status", "queued")
            if vids and status not in ("done", "error"):
                status = "done"

            v_model = normalize_video_model(
                vt.get("video_model")
                or vt.get("model")
                or pdata.get("video_model")
                or pdata.get("model")
            )
            s2v_prompt = (
                vt.get("s2v_prompt")
                or pdata.get("s2v_prompt")
                or pdata.get("caption_ja")
                or ""
            )
            items.append({
                "location": "candidates",
                "folder": folder,
                "status": status,
                "video_model": v_model,
                "source_image": vt.get("source_image") or (imgs[0] if imgs else ""),
                "motion_prompt": vt.get("motion_prompt") or pdata.get("video_motion_prompt") or "",
                "s2v_prompt": s2v_prompt,
                "jp_scenario": pdata.get("jp_scenario") or "",
                "video_file": vt.get("video_file") or (vids[0] if vids else ""),
                "mosaic_video_file": vt.get("mosaic_video_file") or "",
                "fyp_preview_video": vt.get("fyp_preview_video") or "",
                "videos": vids,
                "error_msg": vt.get("error_msg") or "",
                "step1_saved": bool(pdata.get("step1_saved")),
            })

    # 2. 02_ready_to_post 内の動画未完ロック中フォルダ
    if os.path.exists(ready_root):
        for status_sub in ("pending", "scheduled", ""):
            base = os.path.join(ready_root, status_sub) if status_sub else ready_root
            if not os.path.isdir(base):
                continue
            for folder in sorted(os.listdir(base)):
                if status_sub == "" and folder in ("pending", "scheduled"):
                    continue
                f_path = os.path.join(base, folder)
                if not os.path.isdir(f_path):
                    continue
                fjson = os.path.join(f_path, "final_post.json")
                if not os.path.exists(fjson):
                    continue
                try:
                    with open(fjson, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                except Exception:
                    continue

                if meta.get("video_lock"):
                    vids = get_videos_in_dir(f_path)
                    imgs = meta.get("images") or get_images_in_dir(f_path)
                    items.append({
                        "location": "ready_locked",
                        "item_key": f"{status_sub}/{folder}" if status_sub else folder,
                        "folder": folder,
                        "status": "done" if vids else "locked_waiting",
                        "source_image": imgs[0] if imgs else "",
                        "motion_prompt": "",
                        "jp_scenario": "",
                        "video_file": vids[0] if vids else "",
                        "mosaic_video_file": "",
                        "fyp_preview_video": "",
                        "videos": vids,
                        "scheduled_at": meta.get("scheduled_at", ""),
                        "error_msg": "",
                        "step1_saved": True,
                    })

    return jsonify({"ok": True, "char_id": char_id, "items": items})


@worker_bp.route("/api/video_task/queue", methods=["POST"])
def api_video_task_queue():
    data = request.json or {}
    char_id = data.get("char_id", "queen_lilith")
    folder = data.get("folder")
    source_image = data.get("source_image")

    paths = get_char_paths(char_id)
    f_path = os.path.join(paths["candidates"], folder)
    post_json = os.path.join(f_path, "post_data.json")
    if not os.path.exists(post_json):
        return jsonify({"ok": False, "error": "post_data.json が見つかりません"}), 404

    with open(post_json, "r", encoding="utf-8") as f:
        pdata = json.load(f)

    vt = pdata.get("video_task") or {}
    vt["enabled"] = True
    if source_image:
        vt["source_image"] = source_image
    if "video_model" in data and data.get("video_model"):
        norm_model = normalize_video_model(data.get("video_model"))
        vt["video_model"] = norm_model
        pdata["video_model"] = norm_model
    if "motion_prompt" in data:
        vt["motion_prompt"] = (data.get("motion_prompt") or "").strip()
    if "s2v_prompt" in data:
        s2v_p = (data.get("s2v_prompt") or "").strip()
        vt["s2v_prompt"] = s2v_p
        pdata["s2v_prompt"] = s2v_p
    if vt.get("status") not in ("done", "running"):
        vt["status"] = "queued"
    vt["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    pdata["video_task"] = vt
    with open(post_json, "w", encoding="utf-8") as f:
        json.dump(pdata, f, ensure_ascii=False, indent=2)

    return jsonify({"ok": True, "video_task": vt})


@worker_bp.route("/api/video_task/unqueue", methods=["POST"])
def api_video_task_unqueue():
    data = request.json or {}
    char_id = data.get("char_id", "queen_lilith")
    folder = data.get("folder")

    paths = get_char_paths(char_id)
    f_path = os.path.join(paths["candidates"], folder)
    post_json = os.path.join(f_path, "post_data.json")
    if not os.path.exists(post_json):
        return jsonify({"ok": False, "error": "post_data.json が見つかりません"}), 404

    with open(post_json, "r", encoding="utf-8") as f:
        pdata = json.load(f)

    vt = pdata.get("video_task") or {}
    vt["enabled"] = False
    vt["status"] = "none"
    pdata["video_task"] = vt

    with open(post_json, "w", encoding="utf-8") as f:
        json.dump(pdata, f, ensure_ascii=False, indent=2)

    return jsonify({"ok": True})


@worker_bp.route("/api/video_task/reset", methods=["POST"])
def api_video_task_reset():
    """生成済み動画を削除して再生成待ち（queued）に戻す"""
    data = request.json or {}
    char_id = data.get("char_id", "queen_lilith")
    folder = data.get("folder")

    paths = get_char_paths(char_id)
    f_path = os.path.join(paths["candidates"], folder)
    post_json = os.path.join(f_path, "post_data.json")
    if not os.path.exists(post_json):
        return jsonify({"ok": False, "error": "post_data.json が見つかりません"}), 404

    for v in get_videos_in_dir(f_path):
        try:
            os.remove(os.path.join(f_path, v))
        except Exception:
            pass

    with open(post_json, "r", encoding="utf-8") as f:
        pdata = json.load(f)

    vt = pdata.get("video_task") or {}
    vt["status"] = "queued"
    vt["video_file"] = ""
    vt["mosaic_video_file"] = ""
    vt["fyp_preview_video"] = ""
    vt["error_msg"] = ""
    pdata["video_task"] = vt

    with open(post_json, "w", encoding="utf-8") as f:
        json.dump(pdata, f, ensure_ascii=False, indent=2)

    return jsonify({"ok": True})


@worker_bp.route("/api/video_task/finalize_now", methods=["POST"])
def api_video_task_finalize_now():
    """Step2で動画完成済みの候補フォルダを6候補ランダムキャプション＋前から詰め予約で即座にタイムラインへ直送する"""
    data = request.json or {}
    char_id = data.get("char_id", "queen_lilith")
    folder = data.get("folder")
    if not folder:
        return jsonify({"ok": False, "error": "folder が指定されていません"}), 400

    try:
        from app.routes.queue_routes import auto_finalize_folder_to_ready
        res = auto_finalize_folder_to_ready(char_id, folder)
        if not res.get("ok"):
            return jsonify(res), 400
        return jsonify({
            "ok": True,
            "scheduled_at": res["scheduled_at"],
            "caption": res["caption"],
            "message": f"🚀 動画付き投稿を [{res['scheduled_at']}] に自動予約確定しました！",
        })
    except Exception as e:
        # エラー発生時にJSONでエラー内容を返すように修正
        return jsonify({"ok": False, "error": f"finalize_now 内部エラー: {str(e)}"}), 500


@worker_bp.route("/api/video_task/unlock_ready", methods=["POST"])
def api_video_task_unlock_ready():
    """確定済み(02_ready_to_post)で動画未完ロック中の投稿に動画を取り込みロック解除する"""
    data = request.json or {}
    char_id = data.get("char_id", "queen_lilith")
    folder = data.get("folder")

    paths = get_char_paths(char_id)
    ready_dir = None
    for sub in ("pending", "scheduled", ""):
        cand = os.path.join(paths["ready"], sub, folder) if sub else os.path.join(paths["ready"], folder)
        if os.path.isdir(cand):
            ready_dir = cand
            break

    if not ready_dir:
        return jsonify({"ok": False, "error": "確定済みフォルダが見つかりません"}), 404

    archive_dir = os.path.join(paths["rejected"], f"processed_{folder}")
    copied_videos = []
    if os.path.isdir(archive_dir):
        for v in get_videos_in_dir(archive_dir):
            shutil.copy2(os.path.join(archive_dir, v), os.path.join(ready_dir, v))
            copied_videos.append(v)

    fjson = os.path.join(ready_dir, "final_post.json")
    if os.path.exists(fjson):
        with open(fjson, "r", encoding="utf-8") as f:
            meta = json.load(f)
        if copied_videos:
            meta["videos"] = copied_videos
        meta["video_lock"] = False
        meta["ready_for_publish"] = True
        with open(fjson, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

    return jsonify({"ok": True, "videos": copied_videos})