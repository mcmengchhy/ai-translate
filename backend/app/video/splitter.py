"""Video Splitter & Episode Cutter Engine using FFmpeg.

Splits long videos (e.g. 7h+ Bilbili/YouTube videos) into clean, consecutive
episodes/parts (e.g. 1 hour each, 30 minutes each) with matched Khmer subtitle tracks.
Uses lightning-fast stream copying (-c copy) for instantaneous cuts without quality loss.
"""

import logging
import math
import subprocess
from pathlib import Path
from typing import List, Dict, Any, Optional

from .dubber import get_media_duration, generate_srt_file, generate_vtt_file

logger = logging.getLogger(__name__)

ROOT_PROJECT_DIR = Path(__file__).resolve().parent.parent.parent.parent
DUBBED_DIR = ROOT_PROJECT_DIR / "storage" / "dubbed"
DUBBED_DIR.mkdir(parents=True, exist_ok=True)


def format_seconds_to_hhmmss(seconds: float) -> str:
    """Formats float seconds into HH:MM:SS string."""
    seconds = max(0.0, float(seconds))
    hrs = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    return f"{hrs:02d}:{mins:02d}:{secs:02d}"


def split_video_into_parts(
    video_path: str,
    output_dir: Optional[Path] = None,
    split_duration: float = 3600.0,  # 3600s = 1 hour default
    prefix: str = "part",
    segments: Optional[List[Dict[str, Any]]] = None,
    on_progress: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Splits any video file into consecutive parts/episodes.

    Args:
        video_path:     Absolute or relative path to the source video.
        output_dir:     Destination directory (defaults to storage/dubbed).
        split_duration: Duration of each slice in seconds (e.g. 3600 for 1h).
        prefix:         Prefix for output filenames (e.g. 'dubbed_a8e82864').
        segments:       Optional dialogue cues list to slice matching .srt & .vtt subtitles.
        on_progress:    Optional callback(done_parts, total_parts, current_part_name).

    Returns:
        Dict with list of parts, download URLs, time ranges, and sizes.
    """
    v_path = Path(video_path)
    if not v_path.exists():
        raise FileNotFoundError(f"Source video not found: {video_path}")

    target_dir = Path(output_dir) if output_dir else DUBBED_DIR
    target_dir.mkdir(parents=True, exist_ok=True)

    total_duration = get_media_duration(str(v_path))
    if total_duration <= 0:
        raise ValueError(f"Could not determine valid duration for video: {video_path}")

    # Minimum slice size 60s
    slice_dur = max(60.0, float(split_duration))
    total_parts = math.ceil(total_duration / slice_dur)

    logger.info(
        f"Splitting video '{v_path.name}' ({total_duration:.1f}s) into "
        f"{total_parts} parts (~{slice_dur/60:.0f} mins each)..."
    )

    parts: List[Dict[str, Any]] = []
    current_start = 0.0
    part_idx = 1

    while current_start < total_duration:
        current_end = min(current_start + slice_dur, total_duration)
        part_dur = current_end - current_start

        # If less than 2 seconds remaining at tail, append to previous part if possible
        if part_dur < 2.0 and parts:
            break

        start_h = int(current_start // 3600)
        end_h = int(math.ceil(current_end / 3600))
        part_filename = f"{prefix}_ep{part_idx:02d}_{start_h:02d}h-{end_h:02d}h.mp4"
        part_file = target_dir / part_filename

        logger.info(
            f"Extracting Part {part_idx}/{total_parts}: "
            f"[{format_seconds_to_hhmmss(current_start)} -> {format_seconds_to_hhmmss(current_end)}] "
            f"to {part_file.name}..."
        )

        # 1. Fast Stream Copy Split with FFmpeg
        # -avoid_negative_ts make_zero aligns timestamps starting at 00:00:00 for each part
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
            # Fallback with videotoolbox / libx264
            fallback_cmd = [
                "ffmpeg", "-y",
                "-ss", f"{current_start:.3f}",
                "-i", str(v_path),
                "-t", f"{part_dur:.3f}",
                "-c:v", "h264_videotoolbox", "-b:v", "3000k",
                "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart",
                str(part_file),
            ]
            try:
                subprocess.run(
                    fallback_cmd,
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception as fb_err:
                logger.error(f"Fallback transcode failed for part {part_idx}: {fb_err}")
                # Try standard libx264 veryfast
                lib_cmd = [
                    "ffmpeg", "-y",
                    "-ss", f"{current_start:.3f}",
                    "-i", str(v_path),
                    "-t", f"{part_dur:.3f}",
                    "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "aac", "-b:a", "192k",
                    "-movflags", "+faststart",
                    str(part_file),
                ]
                subprocess.run(lib_cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # 2. Slice matching subtitle files (.srt & .vtt)
        srt_file = target_dir / f"{part_file.stem}.srt"
        vtt_file = target_dir / f"{part_file.stem}.vtt"
        part_segments_count = 0

        if segments:
            part_segs = []
            for s in segments:
                s_start = float(s.get("target_start", s.get("start", 0)))
                s_end = float(s.get("target_end", s.get("end", 0)))

                # Check if segment falls within this part's window
                if s_start < current_end and s_end > current_start:
                    rebased_seg = dict(s)
                    rebased_seg["start"] = max(0.0, s_start - current_start)
                    rebased_seg["end"] = min(part_dur, s_end - current_start)
                    rebased_seg["target_start"] = rebased_seg["start"]
                    rebased_seg["target_end"] = rebased_seg["end"]
                    part_segs.append(rebased_seg)

            if part_segs:
                generate_srt_file(part_segs, str(srt_file))
                generate_vtt_file(part_segs, str(vtt_file))
                part_segments_count = len(part_segs)

        file_size_mb = 0.0
        if part_file.exists():
            file_size_mb = round(part_file.stat().st_size / (1024 * 1024), 2)

        part_info = {
            "part_number": part_idx,
            "title": f"Part {part_idx} ({format_seconds_to_hhmmss(current_start)} - {format_seconds_to_hhmmss(current_end)})",
            "start_time": format_seconds_to_hhmmss(current_start),
            "end_time": format_seconds_to_hhmmss(current_end),
            "start_seconds": current_start,
            "end_seconds": current_end,
            "duration": part_dur,
            "filename": part_file.name,
            "video_path": str(part_file),
            "download_url": f"/static/dubbed/{part_file.name}",
            "srt_url": f"/static/dubbed/{srt_file.name}" if srt_file.exists() else None,
            "vtt_url": f"/static/dubbed/{vtt_file.name}" if vtt_file.exists() else None,
            "size_mb": file_size_mb,
            "segments_count": part_segments_count,
        }
        parts.append(part_info)

        if on_progress:
            on_progress(part_idx, total_parts, part_info["title"])

        current_start = current_end
        part_idx += 1

    logger.info(f"Successfully split video into {len(parts)} parts in {target_dir}")

    return {
        "status": "completed",
        "video_path": str(v_path),
        "total_duration": total_duration,
        "split_duration": slice_dur,
        "total_parts": len(parts),
        "parts": parts,
    }


def slice_source_video_into_episodes(
    video_path: str,
    episode_duration: float = 1800.0,
    output_dir: Optional[Path] = None,
    prefix: str = "ep",
) -> List[Dict[str, Any]]:
    """
    Slices raw uploaded/downloaded video upfront into consecutive episodes/parts
    (e.g. 30-minute chunks) using zero-loss FFmpeg stream copy.

    Returns a list of episode dicts containing index, title, start, end, duration, and file path.
    """
    v_path = Path(video_path)
    if not v_path.exists():
        raise FileNotFoundError(f"Source video not found: {video_path}")

    total_duration = get_media_duration(str(v_path))
    if total_duration <= 0:
        raise ValueError(f"Could not determine valid duration for video: {video_path}")

    # If shorter than 1.15 * slice_duration, keep as a single episode
    if episode_duration <= 0 or total_duration <= episode_duration * 1.15:
        return [
            {
                "index": 1,
                "title": f"Episode 01 (00:00:00 - {format_seconds_to_hhmmss(total_duration)})",
                "start": 0.0,
                "end": total_duration,
                "duration": total_duration,
                "file": v_path,
                "is_sliced": False,
            }
        ]

    slice_dur = max(60.0, float(episode_duration))
    target_dir = Path(output_dir) if output_dir else v_path.parent / f"episodes_{v_path.stem}"
    target_dir.mkdir(parents=True, exist_ok=True)

    episodes: List[Dict[str, Any]] = []
    current_start = 0.0
    ep_idx = 1

    while current_start < total_duration:
        current_end = min(current_start + slice_dur, total_duration)
        part_dur = current_end - current_start

        if part_dur < 10.0 and episodes:
            # If less than 10 seconds remaining at end, skip tiny fragment
            break

        start_str = format_seconds_to_hhmmss(current_start)
        end_str = format_seconds_to_hhmmss(current_end)
        ep_file = target_dir / f"{prefix}_{ep_idx:02d}.mp4"

        logger.info(f"Pre-slicing Episode {ep_idx}: [{start_str} -> {end_str}] ({part_dur:.1f}s) to {ep_file.name}")

        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{current_start:.3f}",
            "-i", str(v_path),
            "-t", f"{part_dur:.3f}",
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            "-movflags", "+faststart",
            str(ep_file),
        ]

        try:
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            logger.warning(f"Fast copy failed for episode {ep_idx}: {e}. Retrying with fast hardware transcode...")
            transcode_cmd = [
                "ffmpeg", "-y",
                "-ss", f"{current_start:.3f}",
                "-i", str(v_path),
                "-t", f"{part_dur:.3f}",
                "-c:v", "h264_videotoolbox", "-b:v", "3500k",
                "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart",
                str(ep_file),
            ]
            try:
                subprocess.run(transcode_cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                fallback_cmd = [
                    "ffmpeg", "-y",
                    "-ss", f"{current_start:.3f}",
                    "-i", str(v_path),
                    "-t", f"{part_dur:.3f}",
                    "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "aac", "-b:a", "192k",
                    "-movflags", "+faststart",
                    str(ep_file),
                ]
                subprocess.run(fallback_cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        episodes.append({
            "index": ep_idx,
            "title": f"Episode {ep_idx:02d} ({start_str} - {end_str})",
            "start": current_start,
            "end": current_end,
            "duration": part_dur,
            "file": ep_file,
            "is_sliced": True,
        })

        current_start = current_end
        ep_idx += 1

    return episodes


def merge_episodes_into_single_video(
    episode_files: List[str],
    output_path: str,
    segments_per_episode: Optional[List[List[Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    """
    Merges selected episode MP4 files into a single continuous video clip using zero-loss FFmpeg stream copy.
    Also concatenates and re-aligns matching Khmer subtitles (.srt & .vtt).
    """
    if not episode_files:
        raise ValueError("No episode files provided to merge")

    out_p = Path(output_path)
    out_p.parent.mkdir(parents=True, exist_ok=True)

    # 1. Build FFmpeg concat list
    concat_list_file = out_p.parent / f"concat_{out_p.stem}.txt"
    concat_lines = []
    valid_files = []
    durations = []

    for f_str in episode_files:
        f_p = Path(f_str).resolve()
        if not f_p.exists():
            logger.warning(f"Merge skipped missing file: {f_str}")
            continue
        valid_files.append(f_p)
        dur = get_media_duration(str(f_p))
        durations.append(dur)
        # Escape single quotes for ffmpeg concat demuxer format
        escaped_path = str(f_p).replace("'", "'\\''")
        concat_lines.append(f"file '{escaped_path}'")

    if not valid_files:
        raise FileNotFoundError("None of the specified episode video files exist on disk to merge")

    concat_list_file.write_text("\n".join(concat_lines), encoding="utf-8")

    logger.info(f"Merging {len(valid_files)} video clips into '{out_p.name}' using concat demuxer...")

    # Fast stream copy concat (zero re-encoding, finishes in 1-2 seconds!)
    cmd = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(concat_list_file),
        "-c", "copy",
        "-movflags", "+faststart",
        str(out_p),
    ]

    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        logger.warning(f"Fast stream copy merge failed: {e}. Retrying with fast hardware transcode...")
        # Fallback with videotoolbox / libx264
        fallback_cmd = [
            "ffmpeg", "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_list_file),
            "-c:v", "h264_videotoolbox", "-b:v", "4000k",
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            str(out_p),
        ]
        try:
            subprocess.run(fallback_cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            lib_cmd = [
                "ffmpeg", "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", str(concat_list_file),
                "-c:v", "libx264", "-preset", "ultrafast",
                "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart",
                str(out_p),
            ]
            subprocess.run(lib_cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    finally:
        concat_list_file.unlink(missing_ok=True)

    # 2. Merge and offset Khmer Subtitles
    merged_segments = []
    current_time_offset = 0.0

    if segments_per_episode:
        for seg_list, ep_dur in zip(segments_per_episode, durations):
            if seg_list:
                for s in seg_list:
                    s_copy = dict(s)
                    s_copy["start"] = round(float(s.get("start", 0)) + current_time_offset, 3)
                    s_copy["end"] = round(float(s.get("end", 0)) + current_time_offset, 3)
                    merged_segments.append(s_copy)
            current_time_offset += ep_dur
    else:
        from .subtitle_reader import parse_srt_or_vtt
        for v_file, ep_dur in zip(valid_files, durations):
            srt_candidate = v_file.with_name(v_file.stem + "_kh.srt")
            if not srt_candidate.exists():
                srt_candidate = v_file.with_suffix(".srt")
            if srt_candidate.exists():
                try:
                    cues = parse_srt_or_vtt(str(srt_candidate))
                    for c in cues:
                        c_copy = dict(c)
                        c_copy["start"] = round(float(c.get("start", 0)) + current_time_offset, 3)
                        c_copy["end"] = round(float(c.get("end", 0)) + current_time_offset, 3)
                        merged_segments.append(c_copy)
                except Exception as srt_err:
                    logger.warning(f"Could not parse srt {srt_candidate}: {srt_err}")
            current_time_offset += ep_dur

    srt_path = out_p.with_name(out_p.stem + "_kh.srt")
    vtt_path = out_p.with_name(out_p.stem + "_kh.vtt")

    if merged_segments:
        generate_srt_file(merged_segments, srt_path)
        generate_vtt_file(merged_segments, vtt_path)

    total_dur = get_media_duration(str(out_p))
    file_size_mb = round(out_p.stat().st_size / (1024 * 1024), 1)

    logger.info(f"✅ Successfully merged into '{out_p.name}' ({total_dur:.1f}s, {file_size_mb} MB) with {len(merged_segments)} subtitles")

    return {
        "status": "completed",
        "merged_video_path": str(out_p),
        "filename": out_p.name,
        "download_url": f"/static/dubbed/{out_p.name}",
        "srt_url": f"/static/dubbed/{srt_path.name}" if srt_path.exists() else None,
        "vtt_url": f"/static/dubbed/{vtt_path.name}" if vtt_path.exists() else None,
        "duration": total_dur,
        "duration_formatted": format_seconds_to_hhmmss(total_dur),
        "size_mb": file_size_mb,
        "episodes_count": len(valid_files),
        "subtitles_count": len(merged_segments),
    }


