"""Video Splitter & Clip Cutter Engine using FFmpeg.

Splits multi-hour videos into clean, exact-minute clips (e.g. 10m, 15m, 30m, 60m)
using fast stream copying (-c copy) for zero-loss, near-instant cuts.
Automatically packages all clips into a downloadable .zip archive.
"""

import logging
import math
import re
import subprocess
import zipfile
from pathlib import Path
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

ROOT_PROJECT_DIR = Path(__file__).resolve().parent.parent.parent.parent
SPLITS_DIR = ROOT_PROJECT_DIR / "storage" / "splits"
SPLITS_DIR.mkdir(parents=True, exist_ok=True)


def get_media_duration(file_path: str) -> float:
    """Uses ffprobe to get exact duration in seconds."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(file_path)
    ]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL).decode().strip()
        return float(out)
    except Exception as e:
        logger.error(f"Error getting duration for {file_path}: {e}")
        return 0.0


def format_seconds_to_hhmmss(seconds: float) -> str:
    """Formats float seconds into HH:MM:SS string."""
    seconds = max(0.0, float(seconds))
    hrs = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    return f"{hrs:02d}:{mins:02d}:{secs:02d}"


def format_time_label(seconds: float) -> str:
    """Formats float seconds into a concise filename label (e.g., '00m', '15m', '01h30m')."""
    seconds = max(0.0, float(seconds))
    hrs = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = int(seconds % 60)

    if hrs > 0:
        if secs > 0:
            return f"{hrs:02d}h{mins:02d}m{secs:02d}s"
        return f"{hrs:02d}h{mins:02d}m"
    else:
        if secs > 0:
            return f"{mins:02d}m{secs:02d}s"
        return f"{mins:02d}m"


def sanitize_filename(name: str) -> str:
    """Sanitizes filename for clean cross-platform filenames."""
    s = re.sub(r'[^\w\-\.\s]', '_', name)
    s = re.sub(r'\s+', '_', s)
    return s.strip('._') or "video"


def split_video_by_minutes(
    video_path: str,
    clip_minutes: float = 30.0,
    output_dir: Optional[Path] = None,
    prefix: Optional[str] = None,
    on_progress: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Splits any video file into consecutive clips of exact duration in minutes.
    Creates a combined .zip archive containing all cut clips.

    Args:
        video_path:   Path to source video file.
        clip_minutes: Duration per clip in minutes (e.g. 15.0, 30.0, 60.0).
        output_dir:   Destination directory (defaults to storage/splits).
        prefix:       Optional filename prefix.
        on_progress:  Optional callback(current_part, total_parts, filename).

    Returns:
        Dict with list of parts, total parts, sizes, and zip archive path & download URL.
    """
    v_path = Path(video_path).resolve()
    if not v_path.exists():
        raise FileNotFoundError(f"Source video file not found: {video_path}")

    target_dir = Path(output_dir) if output_dir else SPLITS_DIR
    target_dir.mkdir(parents=True, exist_ok=True)

    total_duration = get_media_duration(str(v_path))
    if total_duration <= 0:
        raise ValueError(f"Could not determine valid duration for video: {video_path}")

    # Clip duration in seconds (minimum 10s)
    clip_seconds = max(10.0, float(clip_minutes) * 60.0)
    total_parts = math.ceil(total_duration / clip_seconds)
    if total_parts > 1 and total_duration - (total_parts - 1) * clip_seconds < 3.0:
        total_parts -= 1
    base_name = sanitize_filename(prefix if prefix else v_path.stem)

    logger.info(
        f"Splitting video '{v_path.name}' ({total_duration:.1f}s / {format_seconds_to_hhmmss(total_duration)}) "
        f"into {total_parts} clips (~{clip_minutes} mins / {clip_seconds:.0f}s each)..."
    )

    parts: List[Dict[str, Any]] = []
    current_start = 0.0
    part_idx = 1

    while current_start < total_duration:
        current_end = min(current_start + clip_seconds, total_duration)
        # Include a short final tail in this clip instead of discarding it.
        if 0 < total_duration - current_end < 3.0:
            current_end = total_duration
        part_dur = current_end - current_start

        start_lbl = format_time_label(current_start)
        end_lbl = format_time_label(current_end)
        part_filename = f"{base_name}_part{part_idx:02d}_{start_lbl}-{end_lbl}.mp4"
        part_file = target_dir / part_filename

        logger.info(
            f"Extracting Clip {part_idx}/{total_parts}: "
            f"[{format_seconds_to_hhmmss(current_start)} -> {format_seconds_to_hhmmss(current_end)}] "
            f"to {part_file.name}..."
        )

        if on_progress:
            try:
                on_progress(part_idx, total_parts, part_filename)
            except Exception:
                pass

        # 1. Fast Stream Copy with FFmpeg (Zero quality loss, 0.2s speed)
        # -avoid_negative_ts make_zero ensures presentation timestamps start at 00:00:00
        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{current_start:.3f}",
            "-i", str(v_path),
            "-t", f"{part_dur:.3f}",
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            "-movflags", "+faststart",
            str(part_file),
        ]

        try:
            subprocess.run(
                cmd,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except subprocess.CalledProcessError as e:
            logger.warning(f"Fast stream copy failed for part {part_idx}: {e}. Retrying with fast transcode...")
            fallback_cmd = [
                "ffmpeg", "-y",
                "-ss", f"{current_start:.3f}",
                "-i", str(v_path),
                "-t", f"{part_dur:.3f}",
                "-c:v", "libx264", "-preset", "ultrafast",
                "-c:a", "aac", "-b:a", "192k",
                "-avoid_negative_ts", "make_zero",
                "-movflags", "+faststart",
                str(part_file),
            ]
            subprocess.run(fallback_cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        if not part_file.exists() or part_file.stat().st_size == 0:
            raise RuntimeError(f"Failed to generate part file: {part_file.name}")

        part_size_mb = round(part_file.stat().st_size / (1024 * 1024), 2)
        actual_part_dur = get_media_duration(str(part_file)) or part_dur

        parts.append({
            "part_index": part_idx,
            "filename": part_filename,
            "file_path": str(part_file),
            "download_url": f"/static/splits/{part_filename}",
            "start_seconds": round(current_start, 2),
            "end_seconds": round(current_end, 2),
            "start_time_formatted": format_seconds_to_hhmmss(current_start),
            "end_time_formatted": format_seconds_to_hhmmss(current_end),
            "duration_seconds": round(actual_part_dur, 1),
            "duration_formatted": format_seconds_to_hhmmss(actual_part_dur),
            "size_mb": part_size_mb,
        })

        current_start = current_end
        part_idx += 1

    # 2. Package all generated clips into a .ZIP archive
    dur_str = f"{int(clip_minutes)}min" if float(clip_minutes).is_integer() else f"{clip_minutes:.1f}min"
    zip_filename = f"{base_name}_split_{dur_str}_clips.zip"
    zip_path = target_dir / zip_filename

    logger.info(f"Packaging {len(parts)} clips into ZIP archive: {zip_filename}...")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as zipf:
        for p in parts:
            pf = Path(p["file_path"])
            if pf.exists():
                zipf.write(pf, arcname=pf.name)

    zip_size_mb = round(zip_path.stat().st_size / (1024 * 1024), 2)
    logger.info(f"✅ Successfully created ZIP archive '{zip_filename}' ({zip_size_mb} MB) with {len(parts)} clips.")

    return {
        "status": "completed",
        "original_filename": v_path.name,
        "total_duration": total_duration,
        "total_duration_formatted": format_seconds_to_hhmmss(total_duration),
        "clip_minutes": clip_minutes,
        "clip_seconds": clip_seconds,
        "total_parts": len(parts),
        "parts": parts,
        "zip_filename": zip_filename,
        "zip_path": str(zip_path),
        "zip_download_url": f"/static/splits/{zip_filename}",
        "zip_size_mb": zip_size_mb,
    }

