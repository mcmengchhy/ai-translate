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
    Speeds up or slows down audio to fit within target duration (between 0.8x and 1.35x).
    """
    current_dur = get_media_duration(clip_path)
    if current_dur <= 0 or target_duration <= 0:
        return clip_path

    # If current audio is within 5% of target, keep original tempo
    if current_dur <= target_duration * 1.05:
        return clip_path

    speed = current_dur / target_duration
    # Clamp speed between 0.8x and 1.35x so speech sounds natural
    speed = max(0.8, min(1.35, speed))

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


def schedule_non_overlapping_clips(
    segments: List[Dict[str, Any]],
    min_gap: float = 0.08,  # 80ms breathing space between speech turns
) -> List[Dict[str, Any]]:
    """
    Guarantees that NO TWO audio clips ever play simultaneously.
    Adjusts tempos and timestamps so each speech cue finishes before the next begins.
    """
    valid_clips = []
    current_end_time = 0.0

    for i, seg in enumerate(segments):
        audio_file = seg.get("audio_file")
        if not audio_file or not Path(audio_file).exists():
            continue

        raw_dur = get_media_duration(audio_file)
        if raw_dur <= 0:
            continue

        orig_start = float(seg.get("start", 0.0))
        orig_end = float(seg.get("end", orig_start + raw_dur))
        slot_duration = max(0.4, orig_end - orig_start)

        # 1. Prevent collision with previous clip:
        # Start at earliest when previous finishes + min_gap
        effective_start = max(orig_start, round(current_end_time + min_gap, 2))

        # 2. Fit tempo if needed
        next_start = float(segments[i + 1].get("start", 999999.0)) if i + 1 < len(segments) else 999999.0
        available_window = max(0.4, next_start - effective_start - min_gap)
        target_slot = min(slot_duration, available_window)

        fitted_audio = fit_audio_tempo(audio_file, target_slot)
        actual_dur = get_media_duration(fitted_audio)

        valid_clips.append({
            "path": fitted_audio,
            "start": round(effective_start, 2),
            "duration": round(actual_dur, 2),
            "seg_id": seg.get("id"),
        })

        # Update segment's actual start and end for subtitle synchronization
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
    Builds the full-length master voice track using batching.
    Guarantees that 5h, 10h, or longer videos with thousands of dialogue cues
    never exceed OS file descriptor or command-line buffer limits.
    """
    if not valid_clips:
        subprocess.run([
            "ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=stereo",
            "-t", str(max(1.0, video_duration)), str(output_path)
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return output_path

    # Fast single-pass for standard length videos
    if len(valid_clips) <= batch_size:
        silence_path = temp_dir / "silence.wav"
        subprocess.run([
            "ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=stereo",
            "-t", str(max(1.0, video_duration)), str(silence_path)
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        inputs = ["-i", str(silence_path)]
        filter_parts = []
        mix_inputs = ["[0:a]"]

        for i, clip in enumerate(valid_clips, start=1):
            inputs.extend(["-i", clip["path"]])
            start_ms = int(clip["start"] * 1000)
            filter_parts.append(f"[{i}:a]adelay={start_ms}|{start_ms}[d{i}]")
            mix_inputs.append(f"[d{i}]")

        # Mix then apply a gentle master EQ pass on the final voice track:
        # - highpass 80Hz: remove sub-bass rumble from TTS
        # - loudnorm -16 LUFS: ensure consistent voice level across all clips
        filter_str = (
            ";".join(filter_parts)
            + f";{''.join(mix_inputs)}amix=inputs={len(valid_clips)+1}:normalize=0[mixed]"
            + ";[mixed]highpass=f=80,loudnorm=I=-16:TP=-1.5:LRA=7[voice_out]"
        )
        cmd_master = ["ffmpeg", "-y"] + inputs + ["-filter_complex", filter_str, "-map", "[voice_out]", str(output_path)]
        logger.info(f"Generating collision-free master voice track ({len(valid_clips)} cues)...")
        subprocess.run(cmd_master, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return output_path

    # Batched mixing for long videos (5h, 10h+) with thousands of cues
    batch_files = []
    num_batches = (len(valid_clips) + batch_size - 1) // batch_size
    logger.info(f"Long video detected: {len(valid_clips)} cues. Processing in {num_batches} audio batches...")

    for b in range(num_batches):
        batch_clips = valid_clips[b * batch_size : (b + 1) * batch_size]
        batch_out = temp_dir / f"batch_voice_{b}.wav"
        silence_b = temp_dir / f"silence_b{b}.wav"

        subprocess.run([
            "ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=stereo",
            "-t", str(max(1.0, video_duration)), str(silence_b)
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        inputs = ["-i", str(silence_b)]
        filter_parts = []
        mix_inputs = ["[0:a]"]

        for i, clip in enumerate(batch_clips, start=1):
            inputs.extend(["-i", clip["path"]])
            start_ms = int(clip["start"] * 1000)
            filter_parts.append(f"[{i}:a]adelay={start_ms}|{start_ms}[d{i}]")
            mix_inputs.append(f"[d{i}]")

        filter_str = ";".join(filter_parts) + f";{''.join(mix_inputs)}amix=inputs={len(batch_clips)+1}:normalize=0[voice_out]"
        cmd = ["ffmpeg", "-y"] + inputs + ["-filter_complex", filter_str, "-map", "[voice_out]", str(batch_out)]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        batch_files.append(batch_out)

    # Combine sub-batches
    final_inputs = []
    final_mix = []
    for b, bfile in enumerate(batch_files):
        final_inputs.extend(["-i", str(bfile)])
        final_mix.append(f"[{b}:a]")

    final_filter = f"{''.join(final_mix)}amix=inputs={len(batch_files)}:normalize=0[voice_out]"
    cmd_final = ["ffmpeg", "-y"] + final_inputs + ["-filter_complex", final_filter, "-map", "[voice_out]", str(output_path)]
    subprocess.run(cmd_final, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return output_path


async def assemble_dubbed_video(
    video_path: str,
    segments: List[Dict[str, Any]],
    output_filename: Optional[str] = None,
    audio_mode: str = "ducking",  # "ducking", "mute", "duck_subtle", "replace"
    bg_volume: Optional[float] = None,
    burn_subtitles: bool = False,
) -> Dict[str, Any]:
    """
    Synchronizes synthesized Khmer audio clips onto the video timeline and
    mixes audio using FFmpeg with strict overlap prevention. Supports arbitrary length (5h, 10h+).
    """
    video = Path(video_path)
    video_duration = get_media_duration(str(video))

    if not output_filename:
        out_name = f"dubbed_{video.stem}.mp4"
    else:
        out_name = output_filename

    final_video_path = DUBBED_DIR / out_name
    temp_dir = DUBBED_DIR / f"temp_{video.stem}"
    temp_dir.mkdir(parents=True, exist_ok=True)

    # 1. Schedule clips with collision-free sequencer (no two voices overlap!)
    valid_clips = schedule_non_overlapping_clips(segments, min_gap=0.08)

    # 2. Build master voice track with batching support for 5h - 10h+ videos
    master_voice_path = temp_dir / "master_khmer_voice.wav"
    build_master_audio_track(
        valid_clips=valid_clips,
        video_duration=video_duration,
        temp_dir=temp_dir,
        output_path=master_voice_path,
        batch_size=50,
    )

    # 3. Generate Subtitles .srt synchronized with collision-free timestamps
    srt_path = DUBBED_DIR / f"{final_video_path.stem}.srt"
    generate_srt_file(segments, str(srt_path))

    # 4. Final Video Assembly
    # Audio balance configuration:
    final_cmd = ["ffmpeg", "-y", "-i", str(video), "-i", str(master_voice_path)]

    if bg_volume is not None:
        if bg_volume <= 0.001:
            audio_filter = "[1:a]volume=1.0[aout]"
        elif bg_volume < 0:
            # Special mode: Smart Sidechain Auto-Ducking (mutes original during speech, keeps music & sfx during pauses)
            audio_filter = "[0:a][1:a]sidechaincompress=threshold=0.015:ratio=20:attack=10:release=250[bg];[bg][1:a]amix=inputs=2:duration=first[aout]"
        else:
            audio_filter = f"[0:a]volume={bg_volume:.3f}[bg];[1:a]volume=1.1[khmer];[bg][khmer]amix=inputs=2:duration=first[aout]"
    elif audio_mode in ("smart_sidechain", "smart"):
        # Cinema-quality: smooth sidechain ducking (fast attack, natural 400ms release)
        audio_filter = "[0:a][1:a]sidechaincompress=threshold=0.012:ratio=18:attack=5:release=400[bg];[bg][1:a]amix=inputs=2:duration=first[aout]"
    elif audio_mode in ("mute", "replace"):
        # Mute original completely: only Khmer dubbing audible!
        audio_filter = "[1:a]volume=1.0[aout]"
    elif audio_mode == "duck_subtle":
        # 5% subtle background audio with normalized Khmer voice
        audio_filter = "[0:a]volume=0.05[bg];[1:a]volume=1.1,loudnorm=I=-16:TP=-1.5[khmer];[bg][khmer]amix=inputs=2:duration=first[aout]"
    else:
        # Default: Cinema-quality sidechain ducking (smooth 5ms attack, 400ms release)
        audio_filter = "[0:a][1:a]sidechaincompress=threshold=0.012:ratio=18:attack=5:release=400[bg];[bg][1:a]amix=inputs=2:duration=first[aout]"

    if burn_subtitles:
        final_cmd.extend([
            "-i", str(srt_path),
            "-filter_complex", audio_filter,
            "-map", "0:v",
            "-map", "[aout]",
            "-map", "2:s",
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "256k",
            "-c:s", "mov_text",
            "-metadata:s:s:0", "language=khm",
            str(final_video_path)
        ])
    else:
        final_cmd.extend([
            "-filter_complex", audio_filter,
            "-map", "0:v",
            "-map", "[aout]",
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "256k",
            str(final_video_path)
        ])

    logger.info(f"Rendering collision-free dubbed video ({audio_mode}): {' '.join(final_cmd)}")
    subprocess.run(final_cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    return {
        "dubbed_video_path": str(final_video_path),
        "srt_path": str(srt_path),
        "download_url": f"/static/dubbed/{final_video_path.name}",
        "srt_url": f"/static/dubbed/{srt_path.name}",
        "duration": video_duration,
        "segments_count": len(segments),
    }
