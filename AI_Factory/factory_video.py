import os
import re
import json
import time
import uuid
import random
import shutil
import argparse
import traceback
import subprocess
import requests
from datetime import datetime
from app.services.comfy_progress import emit_progress, wait_comfy_prompt_with_progress

# ==========================================
# ★ 動画生成ライン設定エリア ★
# ==========================================
COMFY_URL = "http://127.0.0.1:8188"
BASE_DIR = os.environ.get("AI_FACTORY_BASE_DIR") or os.path.dirname(os.path.abspath(__file__))
SHARED_ASSETS_DIR = os.environ.get("SHARED_ASSETS_DIR") or r"C:\Fansly_Empire\shared_assets"
CHARACTERS_ROOT = os.path.join(SHARED_ASSETS_DIR, "characters")
WORKFLOWS_DIR = os.path.join(BASE_DIR, "workflows")
COMFY_INPUT_DIR = r"C:\ComfyUI\input"

DEFAULT_VIDEO_WF = "I2V LLM（Wan2.2 量子化）.json"
DEFAULT_VIDEO_S2V_WF = "queen_lilith_S2V.json"

VIDEO_MODEL_I2V = "WAN 2.2 I2V"
VIDEO_MODEL_S2V = "WAN 2.2 S2V"

# Wan2.2 I2V ワークフロー内の標準ノードID
NODE_VID_LOAD_IMG = "97"      # LoadImage (I2V)
NODE_VID_RES = "129:98"       # WanImageToVideo (width / height)
NODE_VID_PROMPT = "129:177"   # OllamaGenerateV2 (モーション指示)
NODE_VID_SEED = "129:86"      # KSamplerAdvanced (noise_seed)

# Wan2.2 S2V ワークフロー内の標準ノードID
NODE_S2V_LOAD_IMG = "52"      # LoadImage (S2V)
NODE_S2V_RES = "120:55"       # WanSoundImageToVideo (width / height)
NODE_S2V_PROMPT = "120:114"   # OllamaGenerateV2 (日本語セリフ・状況指示)
NODE_S2V_SEED = "120:3"       # KSampler (seed)
NODE_S2V_TTS = "120:109"      # UnifiedTTSSRTNode (TTS seed)
# ==========================================


def read_json(path: str, default=None):
    if default is None:
        default = {}
    if not path or not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path: str, data: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def normalize_video_model(raw_val) -> str:
    """動画生成モデル指定値を正規化する ('WAN 2.2 I2V' | 'WAN 2.2 S2V')"""
    s = str(raw_val or "").strip().lower()
    if "s2v" in s:
        return VIDEO_MODEL_S2V
    return VIDEO_MODEL_I2V


def resolve_default_s2v_workflow(char_id: str) -> str:
    cid = str(char_id or "").strip()
    if cid and os.path.isdir(WORKFLOWS_DIR):
        target_lower = f"{cid}_s2v.json".lower()
        for fname in sorted(os.listdir(WORKFLOWS_DIR)):
            if fname.lower() == target_lower:
                return fname
    return DEFAULT_VIDEO_S2V_WF


def resolve_task_workflow_name(char_id: str, profile: dict, video_model: str, vtask: dict = None) -> str:
    vtask = vtask if isinstance(vtask, dict) else {}
    profile = profile if isinstance(profile, dict) else {}
    if normalize_video_model(video_model) == VIDEO_MODEL_S2V:
        return (
            str(vtask.get("wf_video_s2v") or "").strip()
            or str(profile.get("wf_video_s2v") or "").strip()
            or resolve_default_s2v_workflow(char_id)
        )
    return (
        str(vtask.get("wf_video") or "").strip()
        or str(profile.get("wf_video") or "").strip()
        or DEFAULT_VIDEO_WF
    )


def resolve_task_prompt(video_model: str, vtask: dict, pdata: dict) -> str:
    vtask = vtask if isinstance(vtask, dict) else {}
    pdata = pdata if isinstance(pdata, dict) else {}
    if normalize_video_model(video_model) == VIDEO_MODEL_S2V:
        return (
            str(vtask.get("s2v_prompt") or "").strip()
            or str(pdata.get("s2v_prompt") or "").strip()
            or str(pdata.get("caption_ja") or "").strip()
            or str(vtask.get("motion_prompt") or "").strip()
            or str(pdata.get("video_motion_jp") or "").strip()
            or "恥じらいながらこちらを見つめ、熱い吐息混じりに語りかける"
        )
    return (
        str(vtask.get("motion_prompt") or "").strip()
        or str(pdata.get("video_prompt") or "").strip()
        or str(pdata.get("video_motion_jp") or "").strip()
        or "slow breathing, subtle natural movement, looking at camera"
    )


def _find_node_id(workflow_dict: dict, preferred_ids: tuple, class_types: tuple) -> str | None:
    for pid in preferred_ids:
        node = workflow_dict.get(pid)
        if isinstance(node, dict) and node.get("class_type") in class_types:
            return pid
    for nid, node in workflow_dict.items():
        if isinstance(node, dict) and node.get("class_type") in class_types:
            return nid
    return None


def inject_video_workflow_inputs(
    base_wf: dict,
    image_name: str,
    prompt_text: str,
    width: int,
    height: int,
    is_s2v: bool = False,
    seed: int = None,
) -> tuple[dict, dict]:
    """
    選択された動画ワークフロー（I2V / S2V 両対応）のコピーを作成し、
    入力画像・プロンプト・解像度・シード値を動的に差し替えて返す。
    """
    if "nodes" in base_wf and isinstance(base_wf["nodes"], list):
        raise ValueError(
            "動画ワークフローJSONがUI形式のままです。ComfyUIの『Workflow -> Export (API)』形式で保存してください。"
        )

    wf = json.loads(json.dumps(base_wf))

    # 1. LoadImage ノード差し替え
    pref_img = (NODE_S2V_LOAD_IMG, NODE_VID_LOAD_IMG) if is_s2v else (NODE_VID_LOAD_IMG, NODE_S2V_LOAD_IMG)
    load_img_id = _find_node_id(wf, pref_img, ("LoadImage",))
    if not load_img_id:
        raise KeyError("動画ワークフロー内に LoadImage ノードが見つかりませんでした。")
    wf[load_img_id].setdefault("inputs", {})["image"] = image_name

    # 2. プロンプト（OllamaGenerateV2 等）ノード差し替え
    pref_prompt = (NODE_S2V_PROMPT, NODE_VID_PROMPT) if is_s2v else (NODE_VID_PROMPT, NODE_S2V_PROMPT)
    prompt_id = _find_node_id(wf, pref_prompt, ("OllamaGenerateV2", "OllamaGenerate"))
    if prompt_id:
        wf[prompt_id].setdefault("inputs", {})["prompt"] = prompt_text
    else:
        for nid, node in wf.items():
            if not isinstance(node, dict) or node.get("class_type") != "CLIPTextEncode":
                continue
            inputs = node.get("inputs") or {}
            title = str((node.get("_meta") or {}).get("title") or "").lower()
            if isinstance(inputs.get("text"), str) and "negative" not in title:
                inputs["text"] = prompt_text
                prompt_id = nid
                break

    # 3. 解像度（WanImageToVideo / WanSoundImageToVideo）差し替え
    pref_res = (NODE_S2V_RES, NODE_VID_RES) if is_s2v else (NODE_VID_RES, NODE_S2V_RES)
    res_id = _find_node_id(wf, pref_res, ("WanSoundImageToVideo", "WanImageToVideo"))
    if res_id:
        r_inputs = wf[res_id].setdefault("inputs", {})
        if "width" in r_inputs and not isinstance(r_inputs["width"], list):
            r_inputs["width"] = int(width)
        if "height" in r_inputs and not isinstance(r_inputs["height"], list):
            r_inputs["height"] = int(height)

    # 4. シード値（KSampler / KSamplerAdvanced / UnifiedTTSSRTNode）差し替え
    main_seed = int(seed) if seed is not None else random.randint(1, 10**14)
    tts_seed = random.randint(1, 2147483647)
    seeded_nodes = []

    for nid, node in wf.items():
        if not isinstance(node, dict):
            continue
        ctype = node.get("class_type")
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue

        if ctype == "KSampler":
            if "seed" in inputs and not isinstance(inputs["seed"], list):
                inputs["seed"] = main_seed
                seeded_nodes.append(nid)
        elif ctype == "KSamplerAdvanced":
            if (
                "noise_seed" in inputs
                and not isinstance(inputs["noise_seed"], list)
                and inputs.get("add_noise", "enable") != "disable"
            ):
                inputs["noise_seed"] = main_seed
                seeded_nodes.append(nid)
        elif ctype == "UnifiedTTSSRTNode":
            if "seed" in inputs and not isinstance(inputs["seed"], list):
                inputs["seed"] = tts_seed

    meta = {
        "load_image_node": load_img_id,
        "prompt_node": prompt_id,
        "res_node": res_id,
        "seeded_nodes": seeded_nodes,
        "seed": main_seed,
        "tts_seed": tts_seed,
    }
    return wf, meta


def free_comfy_cache(reason: str = "", settle_sec: float = 2.5) -> bool:
    """
    ComfyUI のモデル・ノードキャッシュ解放（"Free model and node cache" 相当）を実行する。
    S2V ワークフロー内の F5-TTS（UnifiedTTSSRTNode）が VRAM オフロード後に CPU/CUDA デバイス不整合を
    起こして無音フォールバックになる現象を防ぐため、S2V 実行前後に呼び出す。
    """
    label = f" ({reason})" if reason else ""
    try:
        resp = requests.post(
            f"{COMFY_URL}/free",
            json={"unload_models": True, "free_memory": True},
            timeout=15,
        )
        resp.raise_for_status()
        if settle_sec > 0:
            time.sleep(settle_sec)
        print(f"   🧹 ComfyUI キャッシュ解放完了 (/free: unload_models=True, free_memory=True){label}")
        return True
    except Exception as e:
        print(f"   ⚠️ ComfyUI キャッシュ解放 (/free) 呼び出し失敗{label}: {e}")
        return False


def inspect_video_audio_track(video_path: str) -> tuple[bool, float | None]:
    """
    動画ファイル内の音声ストリーム有無と最大音量(dB)を検証する。
    戻り値: (has_audible_audio, max_volume_db)
      - S2V で F5-TTS が内部エラーを起こした場合、音声ストリーム自体は存在するが
        max_volume == -91.0 dB（完全無音）になるため、音量も合わせて判定する。
    """
    if not video_path or not os.path.exists(video_path):
        return False, None

    try:
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a",
                "-show_entries",
                "stream=index,codec_name",
                "-of",
                "json",
                video_path,
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        streams = (json.loads(probe.stdout or "{}") or {}).get("streams") or []
        if not streams:
            return False, None
    except FileNotFoundError:
        # ffprobe 未導入環境ではファイル存在のみで通過させる
        return True, None
    except Exception:
        return False, None

    try:
        vol_proc = subprocess.run(
            ["ffmpeg", "-i", video_path, "-af", "volumedetect", "-f", "null", "-"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        m = re.search(r"max_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", vol_proc.stderr or "")
        if m:
            max_vol = float(m.group(1))
            return (max_vol > -70.0), max_vol
        if "max_volume: -inf dB" in (vol_proc.stderr or ""):
            return False, -99.0
    except Exception:
        pass

    return True, None


def run_comfy_video_workflow(workflow_dict: dict, on_node=None, on_step=None, prefer_audio: bool = False):
    outputs = wait_comfy_prompt_with_progress(
        COMFY_URL,
        workflow_dict,
        on_node=on_node,
        on_step=on_step,
        poll_interval=1.0,
    )
    candidates = []
    for node_id, node_output in outputs.items():
        for key in ("videos", "gifs", "images"):
            if key in node_output and len(node_output[key]) > 0:
                for info in node_output[key]:
                    fname = str(info.get("filename") or "")
                    if fname.lower().endswith((".mp4", ".webm", ".mkv", ".mov", ".gif")):
                        has_audio_flag = (
                            "-audio." in fname.lower()
                            or "_audio." in fname.lower()
                            or bool(info.get("has_audio"))
                        )
                        is_mp4 = fname.lower().endswith(".mp4")
                        candidates.append((has_audio_flag, is_mp4, fname, info.get("subfolder", ""), info.get("type", "output")))

    if not candidates:
        raise RuntimeError("ComfyUI video workflow did not return a video file")

    # 音声付きファイル（*-audio.mp4 等）および MP4 形式を最優先で選択
    candidates.sort(key=lambda x: (1 if x[0] else 0, 1 if x[1] else 0), reverse=True)
    has_aud, _, chosen_fname, chosen_sub, chosen_type = candidates[0]

    # S2V 等で VHS_VideoCombine が音声なし名（WanS2V_XXXX.mp4）を返した場合でも、
    # 同名の *-audio.mp4 が生成されていればそちらを優先取得する
    if prefer_audio and not has_aud and chosen_fname.lower().endswith(".mp4"):
        stem, ext = os.path.splitext(chosen_fname)
        audio_variant = f"{stem}-audio{ext}"
        try:
            chk = requests.get(
                f"{COMFY_URL}/view",
                params={"filename": audio_variant, "subfolder": chosen_sub, "type": chosen_type},
                stream=True,
                timeout=10,
            )
            if chk.status_code == 200:
                chk.close()
                return audio_variant, chosen_sub, chosen_type
            chk.close()
        except Exception:
            pass

    return chosen_fname, chosen_sub, chosen_type


def collect_pending_video_tasks(cand_root: str, ready_root: str) -> list:
    """
    01_candidates と 02_ready_to_post（Step 4 先行確定済み）の両方から
    動画生成待ちタスクを収集する。
    """
    tasks = []
    search_stages = [
        ("01_candidates", cand_root),
        ("02_ready_to_post", ready_root),
    ]

    for stage_label, root_dir in search_stages:
        if not root_dir or not os.path.exists(root_dir):
            continue

        for folder in sorted(os.listdir(root_dir)):
            if folder.lower() in ("posted", "done", "completed"):
                continue
            fdir = os.path.join(root_dir, folder)
            pdata_path = os.path.join(fdir, "post_data.json")
            if not os.path.isdir(fdir) or not os.path.exists(pdata_path):
                continue

            pdata = read_json(pdata_path, {})
            vtask = pdata.get("video_task") or {}

            # すでに生成完了済みでファイルが存在する場合はスキップ
            existing_vid = vtask.get("output_video") or vtask.get("video_file") or pdata.get("generated_video")
            if vtask.get("status") == "done" and existing_vid and os.path.exists(os.path.join(fdir, existing_vid)):
                continue

            # キュー登録済み、または Step 1 で video_image が指定されているフォルダを対象化
            src_img = vtask.get("source_image") or pdata.get("video_image")
            is_queued = (vtask.get("status") == "queued") or (bool(src_img) and not existing_vid)

            if is_queued and src_img:
                tasks.append((stage_label, folder, fdir, pdata_path, pdata))

    return tasks


def mark_video_completed(
    stage_label: str,
    fdir: str,
    pdata_path: str,
    pdata: dict,
    saved_video_name: str,
    video_model: str = None,
    auto_unlock: bool = False,
):
    """
    動画生成完了時に post_data.json と final_post.json（存在時）を更新し、
    Step 5 の第2ライン投稿ロック（video_lock）を自動解除しない。
    """
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    vtask = pdata.get("video_task") if isinstance(pdata.get("video_task"), dict) else {}
    vtask["status"] = "done"
    if video_model:
        norm_model = normalize_video_model(video_model)
        vtask["video_model"] = norm_model
        pdata["video_model"] = norm_model
    vtask["output_video"] = saved_video_name
    vtask["video_file"] = saved_video_name
    if str(vtask.get("mosaic_mode") or "none").lower() == "none":
        vtask["main_video"] = saved_video_name
        vtask["fyp_preview_video"] = saved_video_name
    vtask["completed_at"] = now_str

    pdata["video_task"] = vtask
    pdata["generated_video"] = saved_video_name

    # ロック解除は自動に行わず、STEP2での確定時のみ行う
    # if auto_unlock:
    #     pdata["video_lock"] = False
    #     pdata["ready_for_publish"] = True

    write_json(pdata_path, pdata)

    # 02_ready_to_post 側（final_post.json あり）の場合はロック解除＆動画ファイル名を同期
    final_path = os.path.join(fdir, "final_post.json")
    if os.path.exists(final_path):
        final_meta = read_json(final_path, {})
        final_meta["video"] = saved_video_name
        final_meta["video_file"] = saved_video_name
        final_meta["has_video"] = True
        # if auto_unlock:
        #     final_meta["video_lock"] = False
        #     final_meta["ready_for_publish"] = True
        final_meta["video_completed_at"] = now_str
        write_json(final_path, final_meta)


def main():
    parser = argparse.ArgumentParser(description="AI Content Factory - Video Line (FYP)")
    parser.add_argument("--char", default="queen_lilith", help="Character ID")
    args = parser.parse_args()

    char_dir = os.path.join(CHARACTERS_ROOT, args.char)
    cand_root = os.path.join(char_dir, "01_candidates")
    ready_root = os.path.join(char_dir, "02_ready_to_post")
    profile_path = os.path.join(char_dir, "profile.json")

    prof = read_json(profile_path, {}) if os.path.exists(profile_path) else {}
    wf_i2v_default = resolve_task_workflow_name(args.char, prof, VIDEO_MODEL_I2V)
    wf_s2v_default = resolve_task_workflow_name(args.char, prof, VIDEO_MODEL_S2V)
    wf_cache = {}

    tasks = collect_pending_video_tasks(cand_root, ready_root)
    total_tasks = len(tasks)

    print("==================================================")
    print(f"🎬 FYP動画生成バッチ稼働開始 | キャラ: {args.char}")
    print(f"   I2V WF: {wf_i2v_default} | S2V WF: {wf_s2v_default}")
    print(f"   待機タスク: {total_tasks} 件")
    print("==================================================")

    emit_progress(
        pct=0,
        completed=0,
        total=total_tasks,
        stage=f"動画生成バッチ開始 (対象: {total_tasks}件)" if total_tasks > 0 else "動画化待機中のタスクはありません",
    )

    if not tasks:
        print("📭 動画化待機中のタスクはありません。")
        return

    os.makedirs(COMFY_INPUT_DIR, exist_ok=True)

    for idx, (stage_label, folder, fdir, pdata_path, pdata) in enumerate(tasks, 1):
        vtask = pdata.get("video_task") or {}
        src_img_name = vtask.get("source_image") or pdata.get("video_image")
        src_img_path = os.path.join(fdir, src_img_name)

        video_model = normalize_video_model(vtask.get("video_model") or pdata.get("video_model"))
        is_s2v = (video_model == VIDEO_MODEL_S2V)
        wf_name = resolve_task_workflow_name(args.char, prof, video_model, vtask)
        wf_path = os.path.join(WORKFLOWS_DIR, wf_name)

        target_prompt = resolve_task_prompt(video_model, vtask, pdata)
        vid_width = int(vtask.get("width", 512))
        vid_height = int(vtask.get("height", 768))

        emit_progress(
            pct=5,
            completed=idx - 1,
            total=total_tasks,
            stage=f"[{idx}/{total_tasks}] No.{folder} [{video_model}] 動画化準備中...",
        )

        print(f"\n▶ [{idx}/{total_tasks}] [{stage_label}] フォルダ [{folder}] の動画化を開始")
        print(f"   🤖 使用モデル: {video_model} (WF: {wf_name})")
        print(f"   🖼️ 元画像   : {src_img_name} ({vid_width}x{vid_height})")
        print(f"   🎭 指示内容 : {target_prompt}")

        if not os.path.exists(wf_path):
            print(f"   ❌ 動画ワークフローが見つからないためスキップ: {wf_path}")
            emit_progress(
                pct=100,
                completed=idx,
                total=total_tasks,
                stage=f"[{idx}/{total_tasks}] No.{folder} スキップ (WF不在: {wf_name})",
            )
            continue

        if wf_name not in wf_cache:
            wf_cache[wf_name] = read_json(wf_path, {})
        base_vid_wf = wf_cache[wf_name]

        if not os.path.exists(src_img_path):
            print(f"   ⚠️ 元画像が見つからないためスキップ: {src_img_path}")
            emit_progress(
                pct=100,
                completed=idx,
                total=total_tasks,
                stage=f"[{idx}/{total_tasks}] No.{folder} スキップ (元画像不在)",
            )
            continue

        t0 = time.perf_counter()
        temp_prefix = "temp_s2v" if is_s2v else "temp_i2v"
        temp_in_name = f"{temp_prefix}_{args.char}_{folder}_{src_img_name}"
        temp_in_path = os.path.join(COMFY_INPUT_DIR, temp_in_name)

        try:
            shutil.copy2(src_img_path, temp_in_path)

            if is_s2v:
                emit_progress(
                    pct=8,
                    completed=idx - 1,
                    total=total_tasks,
                    stage=f"[{idx}/{total_tasks}] No.{folder} S2V実行前キャッシュ解放中 (/free)...",
                )
                free_comfy_cache(reason=f"No.{folder} S2V開始前", settle_sec=2.5)

            max_attempts = 2 if is_s2v else 1
            saved_video_name = ""

            for attempt in range(1, max_attempts + 1):
                wf, injected_meta = inject_video_workflow_inputs(
                    base_wf=base_vid_wf,
                    image_name=temp_in_name,
                    prompt_text=target_prompt,
                    width=vid_width,
                    height=vid_height,
                    is_s2v=is_s2v,
                )
                prompt_node_id = injected_meta.get("prompt_node")
                res_node_id = injected_meta.get("res_node")

                emit_progress(
                    pct=12,
                    completed=idx - 1,
                    total=total_tasks,
                    stage=f"[{idx}/{total_tasks}] No.{folder} ComfyUI ({video_model}) へ投入中...",
                )

                sampler_stage = {"pass": 0}

                def _on_vid_node(nid, cur_i=idx, fl=folder, s2v_mode=is_s2v, p_nid=prompt_node_id, r_nid=res_node_id):
                    if nid == p_nid or nid in (NODE_VID_PROMPT, NODE_S2V_PROMPT):
                        emit_progress(
                            pct=16,
                            completed=cur_i - 1,
                            total=total_tasks,
                            stage=(
                                f"[{cur_i}/{total_tasks}] No.{fl} セリフ・映像プロンプト生成中 (LLM)..."
                                if s2v_mode
                                else f"[{cur_i}/{total_tasks}] No.{fl} モーションプロンプト生成中 (LLM)..."
                            ),
                        )
                    elif nid in (NODE_S2V_TTS, "120:108", "120:56"):
                        emit_progress(
                            pct=20,
                            completed=cur_i - 1,
                            total=total_tasks,
                            stage=f"[{cur_i}/{total_tasks}] No.{fl} キャラクター音声合成・リップシンク解析中 (TTS)...",
                        )
                    elif nid == r_nid or nid in (NODE_VID_RES, NODE_S2V_RES):
                        emit_progress(
                            pct=24,
                            completed=cur_i - 1,
                            total=total_tasks,
                            stage=f"[{cur_i}/{total_tasks}] No.{fl} 動画潜在空間を初期化中...",
                        )
                    elif nid == NODE_S2V_SEED:
                        sampler_stage["pass"] = 0
                        emit_progress(
                            pct=28,
                            completed=cur_i - 1,
                            total=total_tasks,
                            stage=f"[{cur_i}/{total_tasks}] No.{fl} リップシンク動画レンダリング中 (Wan2.2 S2V)...",
                        )
                    elif nid == "129:86":
                        sampler_stage["pass"] = 1
                        emit_progress(
                            pct=26,
                            completed=cur_i - 1,
                            total=total_tasks,
                            stage=f"[{cur_i}/{total_tasks}] No.{fl} 動画レンダリング中 (Wan2.2 前半パス)...",
                        )
                    elif nid == "129:85":
                        sampler_stage["pass"] = 2
                        emit_progress(
                            pct=58,
                            completed=cur_i - 1,
                            total=total_tasks,
                            stage=f"[{cur_i}/{total_tasks}] No.{fl} 動画レンダリング中 (Wan2.2 後半パス)...",
                        )
                    elif nid in ("129:87", "120:8"):
                        emit_progress(
                            pct=90,
                            completed=cur_i - 1,
                            total=total_tasks,
                            stage=f"[{cur_i}/{total_tasks}] No.{fl} フレームデコード中 (VAE)...",
                        )
                    elif nid in ("129:94", "108", "62"):
                        emit_progress(
                            pct=95,
                            completed=cur_i - 1,
                            total=total_tasks,
                            stage=f"[{cur_i}/{total_tasks}] No.{fl} MP4動画をエンコード・保存中...",
                        )

                def _on_vid_step(val, mx, nid, cur_i=idx, fl=folder, s2v_mode=is_s2v):
                    ratio = min(1.0, max(0.0, val / max(1, mx)))
                    if s2v_mode or nid == NODE_S2V_SEED:
                        cur_p = 28 + ratio * 60.0
                        emit_progress(
                            pct=cur_p,
                            completed=cur_i - 1,
                            total=total_tasks,
                            stage=f"[{cur_i}/{total_tasks}] No.{fl} S2Vレンダリング中 (Step {val}/{mx})",
                        )
                    elif nid == "129:86" or sampler_stage["pass"] == 1:
                        cur_p = 26 + ratio * 32.0
                        emit_progress(
                            pct=cur_p,
                            completed=cur_i - 1,
                            total=total_tasks,
                            stage=f"[{cur_i}/{total_tasks}] No.{fl} レンダリング前半 (Step {val}/{mx})",
                        )
                    elif nid == "129:85" or sampler_stage["pass"] == 2:
                        cur_p = 58 + ratio * 30.0
                        emit_progress(
                            pct=cur_p,
                            completed=cur_i - 1,
                            total=total_tasks,
                            stage=f"[{cur_i}/{total_tasks}] No.{fl} レンダリング後半 (Step {val}/{mx})",
                        )
                    else:
                        cur_p = 26 + ratio * 62.0
                        emit_progress(
                            pct=cur_p,
                            completed=cur_i - 1,
                            total=total_tasks,
                            stage=f"[{cur_i}/{total_tasks}] No.{fl} 動画レンダリング中 (Step {val}/{mx})",
                        )

                out_file, out_sub, out_type = run_comfy_video_workflow(
                    wf,
                    on_node=_on_vid_node,
                    on_step=_on_vid_step,
                    prefer_audio=is_s2v,
                )
                print(f"   📥 ComfyUI 出力取得: {out_file}")
                vid_bytes = requests.get(
                    f"{COMFY_URL}/view",
                    params={"filename": out_file, "subfolder": out_sub, "type": out_type},
                    timeout=60,
                ).content

                ext = os.path.splitext(out_file)[1] or ".mp4"
                saved_video_name = f"{folder}_FYP_VIDEO{ext}"
                saved_video_path = os.path.join(fdir, saved_video_name)
                with open(saved_video_path, "wb") as f:
                    f.write(vid_bytes)

                if is_s2v:
                    has_audible, max_vol_db = inspect_video_audio_track(saved_video_path)
                    vol_str = f"{max_vol_db:.1f} dB" if max_vol_db is not None else "計測省略"
                    if has_audible:
                        print(f"   🔊 S2V 音声トラック確認OK (max_volume: {vol_str})")
                        break
                    if attempt < max_attempts:
                        print(
                            f"   ⚠️ S2V 出力が無音または音声欠落 (max_volume: {vol_str}) のため、"
                            f"ComfyUI キャッシュを再解放してリトライします ({attempt}/{max_attempts})..."
                        )
                        free_comfy_cache(reason=f"No.{folder} 無音検知リトライ前", settle_sec=3.5)
                        continue
                    raise RuntimeError(
                        f"S2V 動画の音声トラックが無音でした (max_volume: {vol_str})。"
                        "ComfyUI の F5-TTS ノード状態を確認してください。"
                    )
                break

            elapsed = time.perf_counter() - t0
            print(f"   ✅ 動画生成完了！ [{video_model}] ({elapsed:.1f}秒) -> {saved_video_name}")

            # ステータス完了更新 ＆ Step 5 第2ライン投稿ロック自動解除
            mark_video_completed(
                stage_label=stage_label,
                fdir=fdir,
                pdata_path=pdata_path,
                pdata=pdata,
                saved_video_name=saved_video_name,
                video_model=video_model,
                auto_unlock=True,
            )
            if stage_label == "02_ready_to_post":
                print(f"   🔓 [Step 5 連携] {folder} の第2ライン投稿ロック（video_lock）を自動解除しました！")

            emit_progress(
                pct=100,
                completed=idx,
                total=total_tasks,
                stage=f"[{idx}/{total_tasks}] No.{folder} 動画生成完了！ ({elapsed:.0f}秒)",
            )

        except Exception as e:
            print(f"   ❌ 動画生成エラー [{folder}]: {e}")
            print(traceback.format_exc())
            emit_progress(
                pct=100,
                completed=idx,
                total=total_tasks,
                stage=f"[{idx}/{total_tasks}] No.{folder} エラー発生",
            )
        finally:
            if is_s2v:
                free_comfy_cache(reason=f"No.{folder} S2V完了後", settle_sec=2.0)
            if os.path.exists(temp_in_path):
                try:
                    os.remove(temp_in_path)
                except Exception:
                    pass

    print("\n==================================================")
    print("🏁 FYP動画生成バッチの全工程が完了しました！")
    print("==================================================")


if __name__ == "__main__":
    main()