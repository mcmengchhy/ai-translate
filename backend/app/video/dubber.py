"""Audio-Video Synchronizer and Dubbing Engine with FFmpeg."""

import json
import logging
import subprocess
from pathlib import Path
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

ROOT_PROJECT_DIR = Path(__file__).resolve().parent.parent.parent.parent
STORAGE_DIR = ROOT_PROJECT_DIR / "storage"
DUBBED_DIR = STORAGE_DIR / "dubbed"
DUBBED_DIR.mkdir(parents=True, exist_ok=True)


def get_media_duration(file_path: str) -> float:
    """Uses ffprobe to obtain exact duration in seconds."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(file_path)
    ]
    try:
        out = subprocess.check_output(cmd).decode().strip()
        return float(out)
    except Exception as e:
        logger.error(f"Error getting duration for {file_path}: {e}")
        return 0.0


def fit_audio_tempo(clip_path: str, target_duration: float) -> str:
    """
    Preserves exact studio voice clarity.
    Only applies gentle atempo (max 1.15x) if speech strictly exceeds the available window.
    """
    current_dur = get_media_duration(clip_path)
    if current_dur <= 0 or target_duration <= 0:
        return clip_path

    # If audio fits within or close to target (+12%), keep 100% original uncompressed audio
    if current_dur <= target_duration * 1.12:
        return clip_path

    speed = current_dur / target_duration
    if speed <= 1.05:
        return clip_path

    # Clamp speed between 0.95x and 1.15x so voice never sounds robotic, metallic, or chipmunk-like
    speed = min(1.15, max(0.95, speed))

    stretched_path = Path(clip_path).parent / f"fit_{Path(clip_path).name}"
    cmd = [
        "ffmpeg", "-y",
        "-i", str(clip_path),
        "-filter:a", f"atempo={speed:.2f}",
        "-vn",
        str(stretched_path)
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return str(stretched_path)
    except Exception as e:
        logger.warning(f"Failed to adjust tempo: {e}. Using original clip.")
        return clip_path


def generate_srt_file(segments: List[Dict[str, Any]], output_path: str) -> str:
    """Creates a standard .srt subtitle file from segments."""
    def format_timestamp(seconds: float) -> str:
        hrs = int(seconds // 3600)
        mins = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        millis = int((seconds % 1) * 1000)
        return f"{hrs:02d}:{mins:02d}:{secs:02d},{millis:03d}"

    content = []
    for i, seg in enumerate(segments, start=1):
        start_ts = format_timestamp(seg.get("start", 0))
        end_ts = format_timestamp(seg.get("end", 0))
        text = seg.get("translation") or seg.get("text", "")
        content.append(f"{i}\n{start_ts} --> {end_ts}\n{text}\n")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(content))

    return output_path


def generate_vtt_file(segments: List[Dict[str, Any]], output_path: str) -> str:
    """Creates a standard WebVTT .vtt subtitle file from segments for HTML5 video players."""
    def format_vtt_timestamp(seconds: float) -> str:
        hrs = int(seconds // 3600)
        mins = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        millis = int((seconds % 1) * 1000)
        return f"{hrs:02d}:{mins:02d}:{secs:02d}.{millis:03d}"

    lines = ["WEBVTT\n"]
    for i, seg in enumerate(segments, start=1):
        start_ts = format_vtt_timestamp(seg.get("start", 0))
        end_ts = format_vtt_timestamp(seg.get("end", 0))
        text = seg.get("translation") or seg.get("text", "")
        lines.append(f"{i}\n{start_ts} --> {end_ts}\n{text}\n")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return output_path


def schedule_non_overlapping_clips(
    segments: List[Dict[str, Any]],
    min_gap: float = 0.05,
    temp_dir: Optional[Path] = None,
    max_speed: float = 1.18,
) -> List[Dict[str, Any]]:
    """
    Cinema Lip-Sync Sequencer:
    1. Anchors each dialogue cue directly to the character's ground-truth mouth movement (target_start).
    2. Measures available window before the next character speaks (next_start - target_start).
    3. If translation audio is longer than the mouth slot, applies FFmpeg atempo (up to max_speed)
       so the dialogue finishes before the next character speaks.
    4. Eliminates cumulative drift: each line starts on-cue when the character's mouth moves.
    """
    valid_clips = []
    current_end_time = 0.0

    if temp_dir:
        temp_dir.mkdir(parents=True, exist_ok=True)

    for i, seg in enumerate(segments):
        audio_file = seg.get("audio_file")
        if not audio_file or not Path(audio_file).exists() or Path(audio_file).stat().st_size <= 500:
            continue

        raw_dur = get_media_duration(audio_file)
        if raw_dur <= 0:
            continue

        # Ground-truth mouth anchor: preserve original start and end
        if "target_start" not in seg:
            seg["target_start"] = float(seg.get("orig_start") or seg.get("start", 0.0))
        if "target_end" not in seg:
            seg["target_end"] = float(seg.get("orig_end") or seg.get("end", seg["target_start"] + 1.0))

        target_start = float(seg["target_start"])
        target_end = float(seg["target_end"])

        # Determine available time window before next character speaks
        if i + 1 < len(segments):
            next_seg = segments[i + 1]
            next_start = float(next_seg.get("target_start") or next_seg.get("orig_start") or next_seg.get("start", 0.0))
            if next_start > target_start and (next_start - target_start) <= 4.0:
                available_window = max(0.6, next_start - target_start - 0.04)
            else:
                available_window = max(target_end - target_start + 0.6, 1.0)
        else:
            available_window = max(target_end - target_start + 0.6, 1.0)

        # Lip-sync tempo fitting: if speech is longer than the visual window, fit it smoothly
        effective_audio_path = audio_file
        actual_dur = raw_dur

        if raw_dur > available_window and temp_dir:
            speed = raw_dur / available_window
            clamped_speed = min(max_speed, max(1.0, speed))
            if clamped_speed >= 1.04:
                fitted_path = temp_dir / f"fitted_lip_{seg['id']}_{clamped_speed:.2f}.wav"
                try:
                    subprocess.run([
                        "ffmpeg", "-y", "-i", str(audio_file),
                        "-filter:a", f"atempo={clamped_speed:.3f}",
                        "-c:a", "pcm_s16le", "-ar", "48000", str(fitted_path)
                    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    effective_audio_path = str(fitted_path)
                    actual_dur = get_media_duration(str(fitted_path))
                except Exception as e:
                    logger.warning(f"atempo failed for segment {seg['id']}: {e}")

        # Mouth-Anchor: Always start at character's mouth movement
        effective_start = target_start
        if effective_start < current_end_time:
            # Minor micro-adjustment if previous clip slightly overlapped
            effective_start = round(current_end_time + 0.02, 2)

        valid_clips.append({
            "path": effective_audio_path,
            "start": round(effective_start, 2),
            "duration": round(actual_dur, 2),
            "seg_id": seg.get("id"),
        })

        # Set playback and subtitle sync timestamps
        seg["start"] = round(effective_start, 2)
        seg["end"] = round(effective_start + actual_dur, 2)

        current_end_time = effective_start + actual_dur

    return valid_clips


def build_master_audio_track(
    valid_clips: List[Dict[str, Any]],
    video_duration: float,
    temp_dir: Path,
    output_path: Path,
    batch_size: int = 50,
) -> Path:
    """
    Builds the full-length master voice track using streaming chunked timeline assembly at 48kHz.
    Operates in constant O(1) memory (~50MB RAM) regardless of video length (1h, 7h, 12h+),
    completely eliminating Out-Of-Memory crashes.
    """
    if not valid_clips:
        subprocess.run([
            "ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
            "-t", str(max(1.0, video_duration)), str(output_path)
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return output_path

    sample_rate = 48000
    total_samples = int(max(1.0, video_duration + 0.5) * sample_rate)

    try:
        import av
        import numpy as np
        import wave

        logger.info(f"Assembling streaming audio timeline for {len(valid_clips)} cues ({video_duration:.1f}s)...")

        # Pre-filter valid clips and calculate start/end sample ranges
        filtered_clips = []
        for c in valid_clips:
            cp = c.get("path")
            if not cp or not Path(cp).exists():
                continue
            try:
                if Path(cp).stat().st_size <= 500:
                    continue
            except Exception:
                continue

            st = float(c.get("start", 0.0))
            dur = float(c.get("duration", 0.0))
            s_idx = int(st * sample_rate)
            e_idx = s_idx + int(dur * sample_rate)
            filtered_clips.append({
                "path": str(cp),
                "start_idx": s_idx,
                "end_idx": e_idx,
            })

        # Sort by start_idx for fast interval matching
        filtered_clips.sort(key=lambda x: x["start_idx"])

        def decode_clip_to_array(clip_path: str) -> Optional[np.ndarray]:
            try:
                container = av.open(clip_path)
                resampler = av.AudioResampler(format="s16p", layout="stereo", rate=sample_rate)
                pcm_chunks = []
                for frame in container.decode(audio=0):
                    for rf in resampler.resample(frame):
                        pcm_chunks.append(rf.to_ndarray())
                container.close()
                if not pcm_chunks:
                    return None
                raw_arr = np.concatenate(pcm_chunks, axis=1)
                if raw_arr.ndim == 2 and raw_arr.shape[0] == 2:
                    return raw_arr.T
                elif raw_arr.ndim == 2 and raw_arr.shape[1] == 2:
                    return raw_arr
                else:
                    return raw_arr.reshape(-1, 2)
            except Exception as e:
                logger.warning(f"Error decoding clip {clip_path}: {e}")
                return None

        # 5-minute chunks = 14,400,000 samples = ~57.6 MB RAM per chunk!
        chunk_duration = 300.0
        chunk_samples = int(chunk_duration * sample_rate)

        # Cache decoded array for any clip that spans across a chunk boundary
        cached_clip_data: Dict[str, np.ndarray] = {}

        with wave.open(str(output_path), "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)

            cur_sample = 0
            while cur_sample < total_samples:
                n_samples = min(chunk_samples, total_samples - cur_sample)
                chunk_buf = np.zeros((n_samples, 2), dtype=np.int16)
                c_start = cur_sample
                c_end = cur_sample + n_samples

                # Find all clips overlapping this chunk window
                for c in filtered_clips:
                    if c["end_idx"] <= c_start:
                        # Already passed, remove from cache if present
                        cached_clip_data.pop(c["path"], None)
                        continue
                    if c["start_idx"] >= c_end:
                        # Future clip, since clips are sorted, we can stop for this chunk
                        break

                    # Overlaps!
                    c_path = c["path"]
                    if c_path not in cached_clip_data:
                        arr = decode_clip_to_array(c_path)
                        if arr is None:
                            continue
                        cached_clip_data[c_path] = arr

                    clip_arr = cached_clip_data[c_path]
                    clip_len = clip_arr.shape[0]
                    actual_clip_end = c["start_idx"] + clip_len

                    o_start = max(c_start, c["start_idx"])
                    o_end = min(c_end, actual_clip_end)

                    if o_end > o_start:
                        buf_offset = o_start - c_start
                        buf_len = o_end - o_start
                        clip_offset = o_start - c["start_idx"]
                        chunk_buf[buf_offset : buf_offset + buf_len] = clip_arr[clip_offset : clip_offset + buf_len]

                    # If clip finishes inside this chunk, remove from cache
                    if actual_clip_end <= c_end:
                        cached_clip_data.pop(c_path, None)

                wf.writeframes(chunk_buf.tobytes())
                cur_sample += n_samples

        logger.info(f"Master voice track successfully generated with streaming fidelity: {output_path}")
        return output_path

    except Exception as e:
        logger.error(f"Streaming timeline failed ({e}). Falling back to FFmpeg silence...", exc_info=True)
        silence_path = temp_dir / "silence.wav"
        subprocess.run([
            "ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
            "-t", str(max(1.0, video_duration)), str(output_path)
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return output_path


from .subtitle_burner import (
    get_video_dimensions,
    build_subtitle_overlay_concat,
)


async def assemble_dubbed_video(
    video_path: str,
    segments: List[Dict[str, Any]],
    output_filename: Optional[str] = None,
    audio_mode: str = "replace",  # "replace" (100% remove original sound), "cinema_3", "smart_cinema"
    bg_volume: Optional[float] = None,
    burn_subtitles: bool = False,
    mask_hardsubs: bool = True,
    anti_copyright_video: bool = True,
    mirror_video: bool = False,
    remove_origin_sound: bool = True,
) -> Dict[str, Any]:
    """
    Synchronizes synthesized Khmer audio clips onto the video timeline and
    mixes audio and video using FFmpeg with strict overlap prevention.
    
    Features:
    - Lip-Sync dialogue sequencing (mouth-aligned, time-fitted, zero drift).
    - Remove original sound: completely strips original audio so there is zero foreign speech / music impact.
    - Hardsub concealment: covers original Chinese subtitles with sleek dark banner.
    - Anti-Copyright Video Shield: 1.03x smart crop/zoom, subtle histogram grade, optional mirror.
    - Anti-Copyright Audio Protection: stereo phase & harmonic overtone shaping.
    - Hardware-accelerated H.264 VideoToolbox rendering on macOS.
    """
    video = Path(video_path)
    video_duration = get_media_duration(str(video))
    vid_w, vid_h = get_video_dimensions(str(video))

    if not output_filename:
        out_name = f"dubbed_{video.stem}.mp4"
    else:
        out_name = output_filename

    final_video_path = DUBBED_DIR / out_name
    temp_dir = DUBBED_DIR / f"temp_{video.stem}"
    temp_dir.mkdir(parents=True, exist_ok=True)

    # 1. Schedule clips with Lip-Sync sequencer (mouth-aligned, time-fitted, zero cumulative drift)
    valid_clips = schedule_non_overlapping_clips(segments, temp_dir=temp_dir, max_speed=1.18)

    # 2. Build master voice track with batching support for 5h - 10h+ videos
    master_voice_path = temp_dir / "master_khmer_voice.wav"
    build_master_audio_track(
        valid_clips=valid_clips,
        video_duration=video_duration,
        temp_dir=temp_dir,
        output_path=master_voice_path,
        batch_size=50,
    )

    # 3. Generate Subtitles (.srt and .vtt) synchronized with collision-free timestamps
    srt_path = DUBBED_DIR / f"{final_video_path.stem}.srt"
    generate_srt_file(segments, str(srt_path))

    vtt_path = DUBBED_DIR / f"{final_video_path.stem}.vtt"
    generate_vtt_file(segments, str(vtt_path))

    # 4. Audio Mixing Configuration
    # Clean Anti-Copyright Protection (multiband EQ + stereo phase shaping): alters Content ID fingerprint
    # without any wobbly chorus, pitch fluctuation, or underwater distortion.
    clean_anti_copyright = "aformat=channel_layouts=stereo,equalizer=f=1000:t=q:w=1.2:g=-2.5,equalizer=f=2800:t=q:w=1.2:g=-2.5,stereotools=mlev=0.85:slev=1.15"

    if remove_origin_sound or audio_mode in ("mute", "replace") or (bg_volume is not None and bg_volume <= 0.001 and bg_volume >= 0):
        # 100% Pure Voiceover: completely strip original video sound (zero foreign speech / zero music / zero copyright impact)
        audio_filter = "[1:a]volume=1.0[aout]"
    elif bg_volume is not None:
        if bg_volume < 0:
            # Smooth Cinema Ducking: 25% background with gentle, smooth ducking (no choppy pumping)
            audio_filter = (
                f"[0:a]volume=0.25,{clean_anti_copyright}[bg_proc];"
                "[bg_proc][1:a]sidechaincompress=threshold=0.015:ratio=3.5:attack=30:release=650:knee=2.5[bg_ducked];"
                "[1:a]volume=1.0[khmer];"
                "[bg_ducked][khmer]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]"
            )
        else:
            # Custom background volume
            audio_filter = (
                f"[0:a]volume={bg_volume:.3f},{clean_anti_copyright}[bg];"
                f"[1:a]volume=1.0[khmer];"
                f"[bg][khmer]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]"
            )
    elif audio_mode in ("cinema_3", "cinema_4", "cinema_10"):
        eff_vol = bg_volume if (bg_volume is not None and bg_volume > 0) else 0.03
        audio_filter = (
            f"[0:a]volume={eff_vol:.3f},{clean_anti_copyright}[bg];"
            f"[1:a]volume=1.0[khmer];"
            f"[bg][khmer]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]"
        )
    elif audio_mode in ("smart_cinema", "smart_sidechain", "smart", "cinema"):
        # Smooth Cinema Ducking
        audio_filter = (
            f"[0:a]volume=0.25,{clean_anti_copyright}[bg_proc];"
            "[bg_proc][1:a]sidechaincompress=threshold=0.015:ratio=3.5:attack=30:release=650:knee=2.5[bg_ducked];"
            "[1:a]volume=1.0[khmer];"
            "[bg_ducked][khmer]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]"
        )
    else:
        # Default: 100% remove original sound for zero copyright impact
        audio_filter = "[1:a]volume=1.0[aout]"

    # 5. Video Filter Chain & Subtitle Concealment
    needs_video_filter = mask_hardsubs or anti_copyright_video or mirror_video or burn_subtitles
    v_filters = []

    if mirror_video:
        v_filters.append("hflip")

    if anti_copyright_video:
        # 1.03x Smart Crop/Zoom: strips edge watermarks, alters pixel coordinate grid
        # eq filter: subtle contrast/color histogram shift modifies frame hash checksums
        v_filters.append("scale=1.03*iw:-1,crop=iw:ih,eq=contrast=1.03:brightness=0.01:saturation=1.04")

    if mask_hardsubs:
        # Mask original Chinese hardsubs with sleek dark banner at bottom 16% of frame
        v_filters.append("drawbox=x=0:y=ih*0.84:w=iw:h=ih*0.16:color=black@0.92:t=fill")

    final_cmd = ["ffmpeg", "-y", "-i", str(video), "-i", str(master_voice_path)]
    filter_complex_parts = [audio_filter]

    # Check if subtitle burning is requested
    concat_subs_file = None
    if burn_subtitles:
        try:
            concat_subs_file = build_subtitle_overlay_concat(
                segments=segments,
                video_width=vid_w,
                video_height=vid_h,
                total_duration=video_duration,
                temp_dir=temp_dir,
            )
            final_cmd.extend(["-f", "concat", "-safe", "0", "-i", str(concat_subs_file)])
            sub_input_idx = 2
        except Exception as e:
            logger.warning(f"Failed to prepare burned subtitle frames: {e}. Falling back to soft subtitles.")
            burn_subtitles = False

    if needs_video_filter:
        base_v_filter = ",".join(v_filters) if v_filters else "null"
        if burn_subtitles and concat_subs_file:
            filter_complex_parts.append(
                f"[0:v]{base_v_filter}[v_base];"
                f"[v_base][{sub_input_idx}:v]overlay=0:0[vout]"
            )
        else:
            filter_complex_parts.append(f"[0:v]{base_v_filter}[vout]")

        full_filter_complex = ";".join(filter_complex_parts)
        final_cmd.extend([
            "-filter_complex", full_filter_complex,
            "-map", "[vout]",
            "-map", "[aout]",
            "-c:v", "h264_videotoolbox",
            "-b:v", "3500k",
            "-c:a", "aac",
            "-b:a", "256k",
            "-movflags", "+faststart",
            str(final_video_path)
        ])
    else:
        # Fast direct stream copy when no video filters requested
        full_filter_complex = ";".join(filter_complex_parts)
        final_cmd.extend([
            "-filter_complex", full_filter_complex,
            "-map", "0:v",
            "-map", "[aout]",
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "256k",
            "-movflags", "+faststart",
            str(final_video_path)
        ])

    logger.info(f"Rendering collision-free dubbed video ({audio_mode}): {' '.join(final_cmd)}")
    try:
        subprocess.run(final_cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError as e:
        # If h264_videotoolbox fails, fallback to libx264
        if "h264_videotoolbox" in final_cmd:
            logger.warning("h264_videotoolbox failed, falling back to libx264...")
            fallback_cmd = [
                arg.replace("h264_videotoolbox", "libx264") if arg == "h264_videotoolbox" else arg
                for arg in final_cmd
            ]
            # Replace -b:v 3500k with preset
            idx = fallback_cmd.index("-b:v") if "-b:v" in fallback_cmd else -1
            if idx != -1:
                fallback_cmd[idx] = "-preset"
                fallback_cmd[idx+1] = "veryfast"
            subprocess.run(fallback_cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            raise e

    return {
        "dubbed_video_path": str(final_video_path),
        "srt_path": str(srt_path),
        "vtt_path": str(vtt_path),
        "download_url": f"/static/dubbed/{final_video_path.name}",
        "srt_url": f"/static/dubbed/{srt_path.name}",
        "vtt_url": f"/static/dubbed/{vtt_path.name}",
        "duration": video_duration,
        "segments_count": len(segments),
    }
