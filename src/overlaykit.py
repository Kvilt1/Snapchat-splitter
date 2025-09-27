# overlaykit.py
# -*- coding: utf-8 -*-
"""
OverlayKit: fast, transparent overlays via FFmpeg (ffmpeg-python + Pillow)

Key features
- Auto-detect best encoder (VideoToolbox on macOS, NVENC on NVIDIA, QSV on Intel, else libx264).
- Still overlays are resized once in Python (Pillow) -> zero per-frame scaling cost.
- Preserves transparency; places overlay at (0,0).
- Audio stream-copied.
- Parallel multi-file processing with per-resolution overlay cache.
- Graceful fallback to libx264 if requested HW encoder is unavailable.
- Preserves source container metadata and chapters (map_metadata/map_chapters).

Install:
  pip install ffmpeg-python pillow

Basic usage:
  from overlaykit import detect_system_config, overlay_one, overlay_many

  cfg = detect_system_config()  # auto-picks best encoder
  out = overlay_one("media.mp4", "overlay.png", "outdir", config=cfg)
  outs = overlay_many(["m1.mp4","m2.mp4"], "overlay.png", "outdir", config=cfg)
"""
from __future__ import annotations
import os
import sys
import mimetypes
import tempfile
import subprocess
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple
from threading import Lock
from concurrent.futures import ThreadPoolExecutor, as_completed

import ffmpeg           # type: ignore
from PIL import Image   # type: ignore


# -------------------------
# Utilities & data classes
# -------------------------

IMAGE_EXTS = {'.png', '.apng', '.webp', '.avif', '.gif', '.jpg', '.jpeg'}

def _is_image(path: str) -> bool:
    m, _ = mimetypes.guess_type(path)
    return (m or '').startswith('image/') or os.path.splitext(path.lower())[1] in IMAGE_EXTS

@dataclass
class OverlayConfig:
    """Configuration chosen by detect_system_config(), but you can also construct manually."""
    hw: str = "auto"           # 'videotoolbox'|'cuda'|'qsv'|'cpu'|'auto'
    faststart: bool = True     # MP4 +faststart (moov moved) — disable for raw throughput
    jobs: int = 2              # parallel workers for overlay_many()
    enforce_nv12_for_vt: bool = True  # feed NV12 to VideoToolbox encoder
    # You can extend with more knobs later if needed (e.g., quality/CQ/CRF, preset)

def _has_encoder(name: str) -> bool:
    """Check if FFmpeg supports a given encoder name."""
    try:
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-h", f"encoder={name}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True
        )
        return True
    except Exception:
        return False

def detect_system_config(
    prefer: Optional[List[str]] = None,
    faststart: bool = True,
    jobs: Optional[int] = None
) -> OverlayConfig:
    """
    Detect and return a config tuned for this system.
    - macOS: prefer VideoToolbox if present.
    - Else prefer NVENC, then QSV, else libx264.
    You can override preference order via `prefer` (list of encoder keys).
    """
    order = prefer or (
        ["videotoolbox", "cuda", "qsv", "cpu"] if sys.platform == "darwin"
        else ["cuda", "qsv", "videotoolbox", "cpu"]
    )
    chosen = "cpu"
    for hw in order:
        if hw == "videotoolbox" and _has_encoder("h264_videotoolbox"):
            chosen = "videotoolbox"; break
        if hw == "cuda" and _has_encoder("h264_nvenc"):
            chosen = "cuda"; break
        if hw == "qsv" and _has_encoder("h264_qsv"):
            chosen = "qsv"; break
        if hw == "cpu":
            chosen = "cpu"; break

    if jobs is None:
        jobs = 2 if chosen in ("videotoolbox", "cuda", "qsv") else max(1, os.cpu_count() // 2)

    return OverlayConfig(
        hw=chosen,
        faststart=faststart,
        jobs=int(jobs),
        enforce_nv12_for_vt=True
    )


# -------------------------
# Probing / transforms
# -------------------------

def _probe(path: str) -> Tuple[int, int, Optional[float], bool]:
    """Return (width, height, fps or None, has_audio)."""
    info = ffmpeg.probe(path)
    v = next(s for s in info["streams"] if s.get("codec_type") == "video")
    w, h = int(v["width"]), int(v["height"])
    afr = v.get("avg_frame_rate", "0/0")
    try:
        n, d = afr.split("/")
        fps = float(n) / float(d) if float(d) else None
    except Exception:
        fps = None
    has_audio = any(s.get("codec_type") == "audio" for s in info["streams"])
    return w, h, fps, has_audio

def _pre_resize_overlay(overlay_path: str, target_w: int, target_h: int) -> str:
    """Resize a still overlay ONCE; save as PNG RGBA; return temp file path."""
    img = Image.open(overlay_path).convert("RGBA")
    if img.size != (target_w, target_h):
        img = img.resize((target_w, target_h), Image.BILINEAR)
    tmp = tempfile.NamedTemporaryFile(prefix="ovr_", suffix=".png", delete=False)
    tmp_path = tmp.name; tmp.close()
    img.save(tmp_path, format="PNG", compress_level=3)
    return tmp_path

def _select_vcodec(hw: str) -> Tuple[str, Dict[str, object]]:
    """
    Map hw choice to FFmpeg encoder + kwargs.
      - 'videotoolbox' => h264_videotoolbox
      - 'cuda'         => h264_nvenc (CQ mode)
      - 'qsv'          => h264_qsv
      - 'cpu'/'auto'   => libx264
    """
    hw = (hw or "auto").lower()
    if hw == "videotoolbox": return "h264_videotoolbox", {}
    if hw == "cuda":         return "h264_nvenc", {"cq": 19, "b:v": "0"}
    if hw == "qsv":          return "h264_qsv", {}
    return "libx264", {"crf": 18, "preset": "veryfast"}


# -------------------------
# Build and run a single job
# -------------------------

def _build_graph(
    media_path: str,
    overlay_path: str,
    cfg: OverlayConfig,
    pre_resized_cache: Optional[Dict[Tuple[int,int], str]] = None,
    cache_lock: Optional[Lock] = None
):
    """
    Returns (graph, base_in, out_kwargs, cleanup_paths)
    where graph is an ffmpeg-python stream ready to run.
    """
    cleanup: List[str] = []

    base_w, base_h, base_fps, has_audio = _probe(media_path)
    base_in = ffmpeg.input(media_path)

    # Decide overlay input
    if _is_image(overlay_path):
        tmp_overlay: Optional[str] = None
        key = (base_w, base_h)
        if pre_resized_cache is not None:
            if key in pre_resized_cache:
                tmp_overlay = pre_resized_cache[key]
            else:
                if cache_lock: cache_lock.acquire()
                try:
                    if key not in pre_resized_cache:
                        pre_resized_cache[key] = _pre_resize_overlay(overlay_path, base_w, base_h)
                    tmp_overlay = pre_resized_cache[key]
                finally:
                    if cache_lock: cache_lock.release()
        else:
            tmp_overlay = _pre_resize_overlay(overlay_path, base_w, base_h)
            cleanup.append(tmp_overlay)

        ov_in = ffmpeg.input(tmp_overlay, loop=1, framerate=base_fps or 30)
        ov_v = ov_in.video
    else:
        ov_in = ffmpeg.input(overlay_path)
        ov_v = (ov_in.video
                .filter("scale", base_w, base_h, flags="fast_bilinear")
                .filter("format", "rgba"))

    # Composite at (0,0)
    v = ffmpeg.overlay(base_in.video, ov_v, x=0, y=0, eof_action="pass")

    # Encoder
    vcodec, enc_kwargs = _select_vcodec(cfg.hw)
    if vcodec == "h264_videotoolbox" and cfg.enforce_nv12_for_vt:
        v = v.filter("format", "nv12")

    out_kwargs: Dict[str, object] = {
        "vcodec": vcodec,
        **enc_kwargs,
        "pix_fmt": "nv12" if vcodec == "h264_videotoolbox" else "yuv420p",
    }
    if cfg.faststart:
        out_kwargs["movflags"] = "+faststart"

    # Audio copy if present
    if has_audio:
        out_kwargs["acodec"] = "copy"

    # NEW: preserves metadata & chapters from the source container
    # (-map_metadata 0 copies global/container tags from input 0; -map_chapters 0 copies chapter table)
    out_kwargs["map_metadata"] = "0"
    out_kwargs["map_chapters"] = "0"

    return v, base_in, out_kwargs, cleanup


def _run_encode(vnode, base_in, out_kwargs, output_path) -> None:
    """Run the graph, fallback once to libx264 if it fails."""
    try:
        if "acodec" in out_kwargs:
            (ffmpeg.output(vnode, base_in.audio, output_path, **out_kwargs)
             .overwrite_output()
             .global_args("-hide_banner")
             .run())
        else:
            (ffmpeg.output(vnode, output_path, **out_kwargs)
             .overwrite_output()
             .global_args("-hide_banner")
             .run())
        return
    except ffmpeg.Error:
        pass

    # Fallback
    fb_kwargs = dict(out_kwargs)
    fb_kwargs.update({"vcodec": "libx264", "crf": 18, "preset": "veryfast", "pix_fmt": "yuv420p"})
    if "movflags" in out_kwargs:
        fb_kwargs["movflags"] = out_kwargs["movflags"]
    # keep metadata/chapters flags on fallback too
    fb_kwargs["map_metadata"] = out_kwargs.get("map_metadata", "0")
    fb_kwargs["map_chapters"] = out_kwargs.get("map_chapters", "0")

    if "acodec" in out_kwargs:
        (ffmpeg.output(vnode, base_in.audio, output_path, **fb_kwargs)
         .overwrite_output()
         .global_args("-hide_banner")
         .run())
    else:
        (ffmpeg.output(vnode, output_path, **fb_kwargs)
         .overwrite_output()
         .global_args("-hide_banner")
         .run())


# -------------------------
# Public API
# -------------------------

def overlay_one(
    media_path: str,
    overlay_path: str,
    output_dir: str,
    config: Optional[OverlayConfig] = None
) -> str:
    """
    Apply overlay to a single media and write to `output_dir` using the SAME base filename.
    Returns the output path. Original files are untouched.
    """
    cfg = config or detect_system_config()
    os.makedirs(output_dir, exist_ok=True)

    base_name = os.path.basename(media_path)
    out_path = os.path.join(output_dir, base_name)

    v, base_in, out_kwargs, cleanup = _build_graph(media_path, overlay_path, cfg)
    try:
        _run_encode(v, base_in, out_kwargs, out_path)
    finally:
        for p in cleanup:
            try: os.unlink(p)
            except OSError: pass

    return out_path


def overlay_many(
    media_paths: Iterable[str],
    overlay_path: str,
    output_dir: str,
    config: Optional[OverlayConfig] = None,
    jobs: Optional[int] = None
) -> List[str]:
    """
    Apply the SAME overlay to many media files, writing into `output_dir` while
    preserving each input filename. Returns a list of output paths (in the same order).

    Optimizations:
      - Pre-resized overlay is cached PER RESOLUTION and reused across files.
      - Tasks run in parallel (ThreadPoolExecutor) with `jobs` workers.
    """
    cfg = config or detect_system_config()
    if jobs is None:
        jobs = cfg.jobs
    jobs = max(1, int(jobs))

    os.makedirs(output_dir, exist_ok=True)

    cache: Dict[Tuple[int,int], str] = {}
    cache_lock = Lock()

    media_list = list(media_paths)
    results: Dict[int, str] = {}

    def _work(idx: int, media_path: str) -> Tuple[int, str]:
        base_name = os.path.basename(media_path)
        out_path = os.path.join(output_dir, base_name)
        v, base_in, out_kwargs, cleanup = _build_graph(
            media_path, overlay_path, cfg, pre_resized_cache=cache, cache_lock=cache_lock
        )
        try:
            _run_encode(v, base_in, out_kwargs, out_path)
        finally:
            for p in cleanup:
                try: os.unlink(p)
                except OSError: pass
        return idx, out_path

    if jobs == 1:
        for i, m in enumerate(media_list):
            idx, path = _work(i, m)
            results[idx] = path
    else:
        with ThreadPoolExecutor(max_workers=jobs) as ex:
            futs = [ex.submit(_work, i, m) for i, m in enumerate(media_list)]
            for fut in as_completed(futs):
                idx, path = fut.result()
                results[idx] = path

    for p in set(cache.values()):
        try: os.unlink(p)
        except OSError: pass

    return [results[i] for i in range(len(media_list))]