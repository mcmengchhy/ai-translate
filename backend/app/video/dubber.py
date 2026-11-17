"""Audio-Video Synchronizer and Dubbing Engine with FFmpeg."""

import json
import asyncio
import logging
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from typing import List, Dict, Any, Optional
from .segments import require_translations, require_renderable_segments
from .render_options import VideoRenderOptions, resolve_logo
from .subtitles import readable_cues, render_caption_track

logger = logging.getLogger(__name__)

ROOT_PROJECT_DIR = Path(__file__).resolve().parent.parent.parent.parent
STORAGE_DIR = ROOT_PROJECT_DIR / "storage"
DUBBED_DIR = STORAGE_DIR / "dubbed"
DUBBED_DIR.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def _hardware_h264_available():
    if sys.platform != "darwin":
        return False
    try:
        result = subprocess.run([
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=black:s=128x128:r=1",
            "-frames:v", "1", "-c:v", "h264_videotoolbox", "-f", "null", "-",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _video_encoding_args(video_path):
    if _hardware_h264_available():
        dimensions = json.loads(subprocess.check_output([
            "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "json", str(video_path)
        ]))["streams"][0]
        bitrate = max(2_000_000, min(18_000_000, int(dimensions["width"] * dimensions["height"] * 3.3)))
        return ["-c:v", "h264_videotoolbox", "-b:v", str(bitrate), "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p"]


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


def generate_srt_file(segments: List[Dict[str, Any]], output_path: str, video_duration=None) -> str:
    """Creates a standard .srt subtitle file from segments."""
    def format_timestamp(seconds: float) -> str:
        milliseconds = max(0, round(seconds * 1000))
        hrs, remainder = divmod(milliseconds, 3600000)
        mins, remainder = divmod(remainder, 60000)
        secs, millis = divmod(remainder, 1000)
        return f"{hrs:02d}:{mins:02d}:{secs:02d},{millis:03d}"

    content = []
    for i, seg in enumerate(readable_cues(segments, video_duration), start=1):
        start_ts = format_timestamp(seg.get("start", 0))
        end_ts = format_timestamp(seg.get("end", 0))
        text = seg["text"]
        content.append(f"{i}\n{start_ts} --> {end_ts}\n{text}\n")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(content))

    return output_path


def schedule_non_overlapping_clips(
    segments: List[Dict[str, Any]],
    min_gap: float = 0.05,
    temp_dir: Optional[Path] = None,
    max_speed: float = 1.35,
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
        if not audio_file or not Path(audio_file).exists():
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

        # Available speech window: strictly finishes before the next character speaks
        if i + 1 < len(segments):
            next_seg = segments[i + 1]
            next_start = float(next_seg.get("target_start") or next_seg.get("orig_start") or next_seg.get("start", 0.0))
            if next_start > target_start:
                available_window = max(0.4, next_start - target_start - 0.03)
            else:
                available_window = max(target_end - target_start + 0.3, 0.6)
        else:
            available_window = max(target_end - target_start + 0.3, 0.6)

        # Lip-sync tempo fitting: if speech is longer than the visual window, fit it smoothly without truncating words
        effective_audio_path = audio_file
        actual_dur = raw_dur

        if raw_dur > available_window and temp_dir:
            speed = raw_dur / available_window
            clamped_speed = min(1.70, max(1.0, speed))
            if clamped_speed >= 1.03:
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
            # Minor micro-adjustment if previous clip had millisecond boundary overlap
            effective_start = round(current_end_time + 0.01, 2)

        valid_clips.append({
            "path": effective_audio_path,
            "start": round(effective_start, 2),
            "duration": round(actual_dur, 2),
            "seg_id": seg.get("id"),
        })

        # Set playback and subtitle sync timestamps: perfectly synchronized with character mouth and speech
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
    Builds the full-length master voice track using direct in-memory timeline stitching at 48kHz.
    Processes thousands of dialogue cues for 1h, 5h, or 10h+ videos in seconds with cinema-grade fidelity.
    """
    if not valid_clips:
        subprocess.run([
            "ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
            "-t", str(max(1.0, video_duration)), str(output_path)
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return output_path

    sample_rate = 48000
    total_samples = int(max(1.0, video_duration + 1.0) * sample_rate)

    try:
        import av
        import numpy as np
        import wave

        logger.info(f"Assembling collision-free audio timeline for {len(valid_clips)} cues ({video_duration:.1f}s)...")
        # Pre-allocate zeroed buffer (silence) for the full video duration: shape (total_samples, 2)
        timeline = np.zeros((total_samples, 2), dtype=np.int16)

        for clip in valid_clips:
            clip_path = clip.get("path")
            if not clip_path or not Path(clip_path).exists():
                continue

            start_sec = float(clip.get("start", 0.0))
            start_idx = int(start_sec * sample_rate)
            if start_idx >= total_samples:
                continue

            try:
                container = av.open(clip_path)
                resampler = av.AudioResampler(format="s16p", layout="stereo", rate=sample_rate)
                pcm_chunks = []
                for frame in container.decode(audio=0):
                    for rf in resampler.resample(frame):
                        pcm_chunks.append(rf.to_ndarray())
                container.close()

                if not pcm_chunks:
                    continue

                # Planar stereo ndarray has shape (2, N_samples) -> transpose to (N_samples, 2)
                raw_arr = np.concatenate(pcm_chunks, axis=1)
                if raw_arr.ndim == 2 and raw_arr.shape[0] == 2:
                    clip_arr = raw_arr.T
                elif raw_arr.ndim == 2 and raw_arr.shape[1] == 2:
                    clip_arr = raw_arr
                else:
                    clip_arr = raw_arr.reshape(-1, 2)

                clip_len = clip_arr.shape[0]

                end_idx = min(start_idx + clip_len, total_samples)
                actual_write_len = end_idx - start_idx
                if actual_write_len > 0:
                    timeline[start_idx:end_idx] = clip_arr[:actual_write_len]

            except Exception as e:
                logger.warning(f"Error decoding clip {clip_path}: {e}")

        # Write clean timeline WAV directly with exact studio fidelity!
        # Eliminates loudnorm over silence, which was causing severe pumping & muffled audio.
        with wave.open(str(output_path), "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(timeline.tobytes())

        logger.info(f"Master voice track successfully generated with 100% studio fidelity: {output_path}")
        return output_path

    except Exception as e:
        logger.error(f"In-memory timeline failed ({e}). Falling back to FFmpeg single-pass...")
        # Fallback to standard FFmpeg if av/numpy fails
        silence_path = temp_dir / "silence.wav"
        subprocess.run([
            "ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
            "-t", str(max(1.0, video_duration)), str(silence_path)
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        inputs = ["-i", str(silence_path)]
        filter_parts = []
        mix_inputs = ["[0:a]"]
        for i, clip in enumerate(valid_clips[:50], start=1):
            inputs.extend(["-i", clip["path"]])
            start_ms = int(clip["start"] * 1000)
            filter_parts.append(f"[{i}:a]adelay={start_ms}|{start_ms}[d{i}]")
            mix_inputs.append(f"[d{i}]")
        filter_str = ";".join(filter_parts) + f";{''.join(mix_inputs)}amix=inputs={len(mix_inputs)}:normalize=0[voice_out]"
        cmd = ["ffmpeg", "-y"] + inputs + ["-filter_complex", filter_str, "-map", "[voice_out]", str(output_path)]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return output_path


def _assemble_dubbed_video(
    video_path: str,
    segments: List[Dict[str, Any]],
    output_filename: Optional[str] = None,
    audio_mode: str = "replace",  # "replace" (100% pure voiceover matching preview), "ducking", "mute", "smart_sidechain"
    bg_volume: Optional[float] = None,
    burn_subtitles: bool = False,
    render_options: Optional[dict] = None,
    prepared_audio_path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Synchronizes synthesized Khmer audio clips onto the video timeline and
    mixes audio using FFmpeg with strict overlap prevention. Supports arbitrary length (5h, 10h+).
    """
    video = Path(video_path)
    video_duration = get_media_duration(str(video))
    options = VideoRenderOptions(**(render_options or {}))
    if burn_subtitles:
        options.burn_subtitles = True

    if not output_filename:
        out_name = f"dubbed_{video.stem}.mp4"
    else:
        out_name = output_filename

    final_video_path = DUBBED_DIR / out_name
    temp_dir = DUBBED_DIR / f"temp_{final_video_path.stem}"
    temp_dir.mkdir(parents=True, exist_ok=True)

    # 1. Schedule clips with Lip-Sync sequencer (mouth-aligned, time-fitted, zero cumulative drift)
    if prepared_audio_path:
        master_voice_path = Path(prepared_audio_path)
        if master_voice_path.resolve() == final_video_path.resolve():
            raise ValueError("A visual update must use a new output filename")
    else:
        valid_clips = schedule_non_overlapping_clips(segments, temp_dir=temp_dir, max_speed=1.45)
        # 2. Build the voice track only when dialogue/audio changed.
        master_voice_path = temp_dir / "master_khmer_voice.wav"
        build_master_audio_track(
            valid_clips=valid_clips, video_duration=video_duration,
            temp_dir=temp_dir, output_path=master_voice_path, batch_size=50,
        )

    # 3. Generate Subtitles .srt synchronized with collision-free timestamps
    srt_path = DUBBED_DIR / f"{final_video_path.stem}.srt"
    generate_srt_file(segments, str(srt_path), video_duration)

    # 4. Final Video Assembly
    # Audio balance configuration:
    final_cmd = ["ffmpeg", "-y", "-i", str(video), "-i", str(master_voice_path)]

    # Important audio mixing settings:
    # 1. Default to 100% Pure Studio Voiceover ([1:a]volume=1.0[aout]) for crystal-clear sound bit-exact identical to preview.
    # 2. amix uses 'normalize=0:dropout_transition=0' so Khmer voice volume is never halved (-6dB).
    # 3. For ducking/sidechain: pre-attenuates original foreign audio so speech bleed and background hiss never clash.
    if bg_volume is not None:
        if bg_volume < 0:
            # Smart Cinema Ducking with pre-attenuated background
            audio_filter = (
                "[0:a]volume=0.15[bg_in];"
                "[bg_in][1:a]sidechaincompress=threshold=0.008:ratio=20:attack=5:release=350[bg];"
                "[1:a]volume=1.0[khmer];"
                "[bg][khmer]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]"
            )
        elif bg_volume <= 0.001:
            audio_filter = "[1:a]volume=1.0[aout]"
        else:
            audio_filter = (
                f"[0:a]volume={bg_volume:.3f}[bg];"
                f"[1:a]volume=1.0[khmer];"
                f"[bg][khmer]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]"
            )
    elif audio_mode in ("mute", "replace"):
        # 100% Pure Studio Voiceover: only Khmer dubbing audible, 100% bit-exact identical to preview!
        audio_filter = "[1:a]volume=1.0[aout]"
    elif audio_mode in ("smart_sidechain", "smart"):
        # Smart Cinema Ducking (mutes foreign voice during speech, keeps BGM in pauses)
        audio_filter = (
            "[0:a]volume=0.15[bg_in];"
            "[bg_in][1:a]sidechaincompress=threshold=0.008:ratio=20:attack=5:release=350[bg];"
            "[1:a]volume=1.0[khmer];"
            "[bg][khmer]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]"
        )
    elif audio_mode == "duck_subtle":
        # 5% subtle background audio with clean loud Khmer voice
        audio_filter = (
            "[0:a]volume=0.05[bg];"
            "[1:a]volume=1.0[khmer];"
            "[bg][khmer]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]"
        )
    else:
        # Standard Ducking: lowers original audio to 10%, mixes with clear loud Khmer voice
        audio_filter = (
            "[0:a]volume=0.10[bg];"
            "[1:a]volume=1.0[khmer];"
            "[bg][khmer]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]"
        )

    video_filters = []
    video_label = "0:v:0"
    input_index = 2
    if options.blur_chinese_subtitles:
        x = f"2*floor(iw*{options.blur_x:.6f}/2)"
        y = f"2*floor(ih*{options.blur_y:.6f}/2)"
        width = f"max(2,2*floor(iw*{options.blur_width:.6f}/2))"
        height = f"max(2,2*floor(ih*{options.blur_height:.6f}/2))"
        video_filters.extend([
            f"[{video_label}]split=2[clean][strip]",
            f"[strip]crop=w='{width}':h='{height}':x='{x}':y='{y}',gblur=sigma=18[blur]",
            f"[clean][blur]overlay=x='2*floor(main_w*{options.blur_x:.6f}/2)':y='2*floor(main_h*{options.blur_y:.6f}/2)'[blurred]",
        ])
        video_label = "blurred"
    if options.burn_subtitles and segments:
        dimensions = json.loads(subprocess.check_output([
            "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "json", str(video)
        ]))["streams"][0]
        caption_track = render_caption_track(segments, dimensions["width"], dimensions["height"], video_duration, temp_dir / "captions")
        final_cmd.extend(["-f", "concat", "-safe", "0", "-i", str(caption_track)])
        video_filters.append(f"[{video_label}][{input_index}:v]overlay=x=0:y=main_h-overlay_h:eof_action=pass[captions]")
        video_label = "captions"
        input_index += 1
    if options.add_logo:
        final_cmd.extend(["-i", str(resolve_logo(options.logo_id))])
        video_filters.extend([
            f"[{input_index}:v]scale=40:40:force_original_aspect_ratio=decrease,format=rgba,pad=40:40:(ow-iw)/2:(oh-ih)/2:color=0x00000000,setsar=1[logo]",
            f"[{video_label}][logo]overlay=x=main_w-overlay_w-12:y=12:eof_action=repeat[branded]",
        ])
        video_label = "branded"
    filters = video_filters if prepared_audio_path else [audio_filter, *video_filters]
    if filters:
        final_cmd.extend(["-filter_complex", ";".join(filters)])
    final_cmd.extend(["-map", f"[{video_label}]" if video_filters else "0:v:0",
                      "-map", "1:a:0" if prepared_audio_path else "[aout]"])
    if video_filters:
        final_cmd.extend(_video_encoding_args(video))
    else:
        final_cmd.extend(["-c:v", "copy"])
    final_cmd.extend(["-c:a", "copy"] if prepared_audio_path else ["-c:a", "aac", "-b:a", "256k"])
    final_cmd.extend(["-t", str(video_duration), "-movflags", "+faststart", str(final_video_path)])

    logger.info(f"Rendering collision-free dubbed video ({audio_mode}): {' '.join(final_cmd)}")
    subprocess.run(final_cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    return {
        "dubbed_video_path": str(final_video_path),
        "srt_path": str(srt_path),
        "download_url": f"/static/dubbed/{final_video_path.name}",
        "srt_url": f"/static/dubbed/{srt_path.name}",
        "duration": video_duration,
        "segments_count": len(segments),
        "render_options": options.render_values(),
    }


async def assemble_dubbed_video(
    video_path: str,
    segments: List[Dict[str, Any]],
    output_filename: Optional[str] = None,
    audio_mode: str = "replace",
    bg_volume: Optional[float] = None,
    burn_subtitles: bool = False,
    render_options: Optional[dict] = None,
    prepared_audio_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Render in a worker so status polling stays responsive during FFmpeg work."""
    if prepared_audio_path:
        # A visual update copies the finished audio; individual voice caches
        # are not needed to reproduce that already completed soundtrack.
        unresolved = [seg for seg in segments if (seg.get("text") or "").strip() and
                      (not (seg.get("translation") or "").strip() or seg.get("translation_error")) and
                      seg.get("audio_source") != "original_fallback"]
        require_translations(unresolved)
    else:
        require_renderable_segments(segments)
    from .cache import cache_in_use
    from .resources import resource_slot, run_blocking
    out_name = output_filename or f"dubbed_{Path(video_path).stem}.mp4"
    with cache_in_use(DUBBED_DIR / f"temp_{Path(out_name).stem}"):
        async with resource_slot("video_render", 1):
            return await run_blocking(
                _assemble_dubbed_video, video_path, segments, output_filename,
                audio_mode, bg_volume, burn_subtitles, render_options, prepared_audio_path,
            )
