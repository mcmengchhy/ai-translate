"""Independent narration bundles and replacement visuals from saved Khmer voices."""

import asyncio
import copy
import json
import math
import re
import shutil
import subprocess
import tempfile
import time
import uuid
import wave
import zipfile
from pathlib import Path

from . import pipeline
from .cache import cache_in_use
from .checkpoints import ACTIVE_STATUSES
from .dubber import (get_media_duration, schedule_non_overlapping_clips,
                     generate_srt_file, _video_encoding_args)
from .job_identity import atomic_json_write
from .resources import resource_slot, run_blocking
from .segments import require_translations, voice_summary
from .subtitles import render_caption_track

EXPORT_DIR = Path(__file__).resolve().parents[3] / "storage" / "exports"
TASKS = {}


def export_directory(export_id):
    if not re.fullmatch(r"[a-f0-9]{32}", export_id):
        raise FileNotFoundError("Export not found")
    return EXPORT_DIR / export_id


def read_export(export_id):
    path = export_directory(export_id) / "state.json"
    if not path.is_file():
        raise FileNotFoundError("Export not found")
    return json.loads(path.read_text(encoding="utf-8"))


def save_export(state):
    atomic_json_write(export_directory(state["id"]) / "state.json", state)


def snapshot_job(job_id):
    job = pipeline.get_job_status(job_id)
    if not job:
        raise KeyError(job_id)
    if job.get("status") in ACTIVE_STATUSES or job_id in pipeline.RUNNING_JOBS:
        raise ValueError(f"{job_id}: wait for the task to finish or pause it first")
    segments = copy.deepcopy(job.get("segments") or [])
    if not segments:
        raise ValueError(f"{job_id}: no saved dialogue is available")
    require_translations(segments)
    summary = voice_summary(segments)
    if not summary["voice_cues"] or summary["missing_voices"]:
        raise ValueError(f"{job_id}: generate or retry the missing Khmer voices before exporting")
    segments = [seg for seg in segments if (seg.get("text") or seg.get("translation") or "").strip()]
    for seg in segments:
        start = float(seg.get("target_start", seg.get("start", 0)))
        end = float(seg.get("target_end", seg.get("end", 0)))
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
            raise ValueError(f"{job_id}: cue {seg.get('id')} has invalid timing")
        seg["_export_audio_version"] = _audio_version(seg["audio_file"])
    return {"id": job_id, "title": job.get("title") or job_id, "segments": segments,
            "video_path": job.get("video_path"), "duration": (job.get("result") or {}).get("duration")}


def _audio_version(path):
    stat = Path(path).stat()
    return stat.st_ino, stat.st_size, stat.st_mtime_ns


def _freeze_voices(snapshot, directory):
    """Keep queued snapshots consistent even when another task regenerates voice files."""
    directory.mkdir(parents=True, exist_ok=True)
    result = copy.deepcopy(snapshot)
    for index, segment in enumerate(result["segments"]):
        source = Path(segment["audio_file"])
        expected = segment["_export_audio_version"]
        try:
            if _audio_version(source) != expected:
                raise ValueError("Voice changed")
            target = directory / f"{index}{source.suffix}"
            shutil.copyfile(source, target)
            if _audio_version(source) != expected:
                raise ValueError("Voice changed")
        except (OSError, ValueError) as error:
            raise ValueError(f"{snapshot['id']}: Khmer voices changed while this export was queued. Create a new export.") from error
        segment["audio_file"] = str(target)
    return result


def create_export(snapshots, kind="bundle", **options):
    export_id = uuid.uuid4().hex
    state = {"id": export_id, "kind": kind, "status": "queued", "progress": 0,
             "stage": "Waiting for export worker", "created_at": time.time(),
             "jobs": [{"id": job["id"], "title": job["title"]} for job in snapshots],
             **options}
    save_export(state)
    return state


def _write_narration(clips, duration, output):
    """Stream every clip into a PCM timeline, without loading hours of audio into RAM."""
    rate, frame_bytes = 48000, 4
    cursor = 0
    silence = bytes(rate * frame_bytes)
    with wave.open(str(output), "wb") as target:
        target.setparams((2, 2, rate, 0, "NONE", "not compressed"))

        def pad(frames):
            nonlocal cursor
            while frames > 0:
                count = min(frames, rate)
                target.writeframesraw(silence[:count * frame_bytes])
                cursor += count
                frames -= count

        for clip in clips:
            start = round(clip["start"] * rate)
            pad(max(0, start - cursor))
            # Tiny rounding overlaps are cropped at the boundary, preserving the timeline.
            skip = max(0, cursor - start) * frame_bytes
            with tempfile.TemporaryFile() as errors:
                process = subprocess.Popen([
                    "ffmpeg", "-v", "error", "-i", str(clip["path"]), "-vn",
                    "-ac", "2", "-ar", str(rate), "-f", "s16le", "pipe:1",
                ], stdout=subprocess.PIPE, stderr=errors)
                decoded = 0
                try:
                    while block := process.stdout.read(65536):
                        decoded += len(block)
                        cut = min(skip, len(block))
                        block, skip = block[cut:], skip - cut
                        target.writeframesraw(block)
                        cursor += len(block) // frame_bytes
                    code = process.wait()
                    if code or not decoded:
                        errors.seek(0)
                        raise ValueError(f"Cannot decode Khmer voice for cue {clip.get('seg_id')}: " +
                                         errors.read(1000).decode(errors="replace"))
                finally:
                    process.stdout.close()
                    if process.poll() is None:
                        process.kill()
                        process.wait()
        pad(max(0, math.ceil(duration * rate) - cursor))
    return cursor / rate


def _prepare_job(snapshot, directory):
    directory.mkdir(parents=True, exist_ok=True)
    segments = sorted(copy.deepcopy(snapshot["segments"]),
                      key=lambda seg: float(seg.get("target_start", seg.get("start", 0))))
    clips = schedule_non_overlapping_clips(segments, temp_dir=directory / "work")
    if len(clips) != len(segments):
        raise ValueError(f"{snapshot['id']}: a saved Khmer voice is unreadable; retry voices first")
    duration = max(float(snapshot.get("duration") or 0), max(seg["end"] for seg in segments))
    source = snapshot.get("video_path")
    if source and Path(source).is_file():
        duration = max(duration, get_media_duration(source))
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Cannot determine the narration duration")
    duration = _write_narration(clips, duration, directory / "khmer.wav")
    generate_srt_file(segments, str(directory / "khmer.srt"), duration)
    return {"id": snapshot["id"], "title": snapshot["title"], "duration": duration,
            "segments_count": len(segments)}, segments


def _render_replacement(video, directory, segments, duration, ending, burn_subtitles):
    """Only the uploaded replacement's video stream enters the new output."""
    info = json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
        "stream=width,height", "-of", "json", str(video),
    ]))
    if not info.get("streams") or get_media_duration(str(video)) <= 0:
        raise ValueError("The replacement file must contain a playable video stream")
    command = ["ffmpeg", "-y", "-v", "error"]
    if ending == "loop":
        command += ["-stream_loop", "-1"]
    command += ["-i", str(video), "-i", str(directory / "khmer.wav")]
    filters = ["[0:v:0]setpts=PTS-STARTPTS,setsar=1" +
               (f",tpad=stop_mode=clone:stop_duration={duration:.6f}" if ending == "freeze" else "") + "[visual]"]
    label = "visual"
    if burn_subtitles:
        dimensions = info["streams"][0]
        track = render_caption_track(segments, dimensions["width"], dimensions["height"], duration,
                                     directory / "captions")
        command += ["-f", "concat", "-safe", "0", "-i", str(track)]
        filters.append("[visual][2:v]overlay=x=0:y=main_h-overlay_h:eof_action=pass[captioned]")
        label = "captioned"
    command += ["-filter_complex", ";".join(filters), "-map", f"[{label}]", "-map", "1:a:0",
                *_video_encoding_args(video), "-c:a", "aac", "-b:a", "256k",
                "-t", f"{duration:.6f}", "-movflags", "+faststart", str(directory / "replacement.mp4")]
    with tempfile.TemporaryFile() as errors:
        result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=errors)
        if result.returncode:
            errors.seek(0)
            raise ValueError("Replacement render failed: " + errors.read(1500).decode(errors="replace"))


def _build_export(state, snapshots, replacement):
    root = export_directory(state["id"])
    state.update(status="processing", stage="Preparing Khmer narration")
    save_export(state)
    snapshots = [_freeze_voices(snapshot, root / str(index) / "voices")
                 for index, snapshot in enumerate(snapshots)]
    manifest = []
    for index, snapshot in enumerate(snapshots):
        state.update(progress=round(index / len(snapshots) * 90),
                     stage=f"Exporting {index + 1} of {len(snapshots)}: {snapshot['title']}")
        save_export(state)
        directory = root / str(index)
        entry, segments = _prepare_job(snapshot, directory)
        if replacement:
            state.update(stage="Rendering replacement video", progress=75)
            save_export(state)
            _render_replacement(replacement, directory, segments, entry["duration"],
                                state["ending"], state["burn_subtitles"])
        manifest.append(entry)
    state.update(stage="Packing download", progress=95)
    save_export(state)
    with zipfile.ZipFile(root / "khmer-bundle.zip", "w", zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
        for index, entry in enumerate(manifest):
            # Titles are user supplied; archive member paths use server-generated indexes only.
            for name in ("khmer.wav", "khmer.srt", "replacement.mp4"):
                path = root / str(index) / name
                if path.is_file():
                    archive.write(path, f"{index + 1:03d}/{name}")
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    state.update(status="completed", progress=100, stage="Ready to download", jobs=manifest)
    save_export(state)


async def run_export(state, snapshots, replacement=None):
    leased = [Path(seg["audio_file"]).parent for job in snapshots for seg in job["segments"]]
    try:
        with cache_in_use(*leased, export_directory(state["id"])):
            async with resource_slot("video_render", 1):
                await run_blocking(_build_export, state, snapshots, replacement)
    except asyncio.CancelledError:
        state.update(status="interrupted", stage="Export interrupted; create the export again")
        save_export(state)
        raise
    except Exception as error:
        state.update(status="failed", stage="Export failed", error=str(error))
        save_export(state)


def launch_export(state, snapshots, replacement=None):
    task = asyncio.create_task(run_export(state, snapshots, replacement))
    TASKS[state["id"]] = task
    task.add_done_callback(lambda _: TASKS.pop(state["id"], None))


def mark_interrupted_exports():
    if not EXPORT_DIR.exists():
        return
    for path in EXPORT_DIR.glob("*/state.json"):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            if state.get("status") in {"queued", "processing"}:
                state.update(status="interrupted", stage="Export interrupted; create the export again")
                save_export(state)
        except (OSError, ValueError, KeyError):
            continue


async def stop_exports():
    tasks = list(TASKS.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
