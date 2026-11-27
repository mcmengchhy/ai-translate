"""Upload-only video editing: flip, synchronized speed, color, and generated effects."""

import asyncio
import json
import math
import subprocess
import tempfile
import time
import zipfile
from fractions import Fraction
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from . import media_exports
from .audio_mix import generated_effect_filters
from .cache import cache_in_use
from .dubber import _video_encoding_args
from .resources import resource_slot, run_blocking


ColorFilter = Literal[
    "normal", "vivid", "warm", "cool", "grayscale", "sepia",
    "cinematic", "vintage", "soft", "high_contrast", "muted",
    "bright", "dark", "golden_hour", "teal_orange", "fade",
]


class ClipEditOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    flip: Literal["none", "horizontal", "vertical", "both"] = "horizontal"
    speed: float = Field(default=1, ge=.25, le=4)
    color_filter: ColorFilter = "normal"
    sound_effect: Literal["none", "ambience", "transitions", "both"] = "none"
    original_volume: int = Field(default=100, ge=0, le=100)
    effect_volume: int = Field(default=8, ge=0, le=100)


COLOR_FILTERS = {
    "normal": "",
    "vivid": "eq=contrast=1.12:saturation=1.35:brightness=0.01",
    "warm": "colorbalance=rs=0.08:rm=0.06:rh=0.04:bs=-0.08:bm=-0.06:bh=-0.04",
    "cool": "colorbalance=rs=-0.08:rm=-0.06:rh=-0.04:bs=0.08:bm=0.06:bh=0.04",
    "grayscale": "hue=s=0",
    "sepia": "colorchannelmixer=.393:.769:.189:0:.349:.686:.168:0:.272:.534:.131",
    "cinematic": "eq=contrast=1.18:saturation=0.82:brightness=-0.02,colorbalance=bs=0.05:rh=0.04",
    "vintage": "eq=contrast=0.90:saturation=0.72:brightness=0.035,colorbalance=rs=0.045:gs=0.02:bs=-0.045",
    "soft": "eq=contrast=0.88:saturation=0.92:brightness=0.025",
    "high_contrast": "eq=contrast=1.35:saturation=1.10:brightness=-0.025",
    "muted": "eq=saturation=0.55:contrast=0.98",
    "bright": "eq=brightness=0.08:gamma=1.06:saturation=1.05",
    "dark": "eq=brightness=-0.06:gamma=0.92:saturation=0.90",
    "golden_hour": "colorbalance=rs=0.08:rm=0.10:rh=0.05:gs=0.025:gm=0.025:bs=-0.10:bm=-0.08,eq=saturation=1.12:brightness=0.015",
    "teal_orange": "colorbalance=rs=-0.05:gs=0.025:bs=0.08:rh=0.09:gh=0.025:bh=-0.06,eq=saturation=1.12:contrast=1.08",
    "fade": "eq=contrast=0.76:brightness=0.065:saturation=0.78",
}


def _probe_clip(path):
    try:
        result = subprocess.run([
            "ffprobe", "-v", "error", "-show_entries",
            "stream=codec_type,duration,width,height,avg_frame_rate:format=duration", "-of", "json", str(path),
        ], capture_output=True, timeout=30)
        if result.returncode:
            raise ValueError
        info = json.loads(result.stdout)
        video = next((stream for stream in info.get("streams", []) if stream.get("codec_type") == "video"), None)
        if not video or not video.get("width") or not video.get("height"):
            raise ValueError
        # A video stream's duration avoids extending visuals to an unrelated longer audio track.
        duration = float(video.get("duration") or info.get("format", {}).get("duration") or 0)
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError
        try:
            frame_rate = float(Fraction(video.get("avg_frame_rate") or "30"))
        except (ValueError, ZeroDivisionError):
            frame_rate = 30
        if not math.isfinite(frame_rate) or frame_rate <= 0:
            frame_rate = 30
        return duration, any(stream.get("codec_type") == "audio" for stream in info.get("streams", [])), min(120, frame_rate)
    except (ValueError, TypeError, OSError, subprocess.TimeoutExpired) as error:
        raise ValueError("Upload a playable video clip with a valid duration.") from error


def _tempo_filters(speed):
    """Use moderate tempo steps to preserve speech quality at 0.25x through 4x."""
    factors = []
    while speed < .5:
        factors.append(.5)
        speed /= .5
    while speed > 2:
        factors.append(2)
        speed /= 2
    factors.append(speed)
    return ",".join(f"atempo={factor:.8f}" for factor in factors)


def _clip_filters(options, duration, has_audio, frame_rate=30):
    visual = [f"setpts=(PTS-STARTPTS)/{options.speed:.8f}"]
    if options.flip in {"horizontal", "both"}:
        visual.append("hflip")
    if options.flip in {"vertical", "both"}:
        visual.append("vflip")
    if COLOR_FILTERS[options.color_filter]:
        visual.append(COLOR_FILTERS[options.color_filter])
    # H.264 requires even dimensions; preserve the picture by adding at most one edge pixel.
    visual.extend([f"fps={frame_rate:.8f}", "pad=ceil(iw/2)*2:ceil(ih/2)*2", "setsar=1",
                   f"tpad=stop_mode=clone:stop_duration={duration:.6f}", f"trim=duration={duration:.6f}"])
    filters = ["[0:v:0]" + ",".join(visual) + "[visual]"]
    if has_audio and options.original_volume:
        filters.append("[0:a:0]asetpts=PTS-STARTPTS," + _tempo_filters(options.speed) +
                       f",aresample=48000,volume={options.original_volume / 100:.3f},"
                       f"apad,atrim=duration={duration:.6f}[clip_audio]")
    else:
        filters.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={duration:.6f}[clip_audio]")
    effects, effect_inputs = generated_effect_filters(options.sound_effect, options.effect_volume, duration)
    filters.extend(effects)
    inputs = ["[clip_audio]", *effect_inputs]
    filters.append("".join(inputs) + f"amix=inputs={len(inputs)}:duration=first:dropout_transition=0:normalize=0,"
                   "alimiter=limit=0.95:level=false:latency=true[audio]")
    return ";".join(filters)


def _render_clip(state, source):
    options = ClipEditOptions(**state["edit_options"])
    duration, has_audio, frame_rate = _probe_clip(source)
    output_duration = duration / options.speed
    root = media_exports.export_directory(state["id"])
    directory = root / "0"
    directory.mkdir(parents=True, exist_ok=True)
    state.update(status="processing", progress=3, stage="Rendering clip")
    media_exports.save_export(state)
    command = ["ffmpeg", "-y", "-v", "error", "-nostats", "-progress", "pipe:1", "-i", str(source),
               "-filter_complex", _clip_filters(options, output_duration, has_audio, frame_rate),
               "-map", "[visual]", "-map", "[audio]", *_video_encoding_args(source),
               "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
               "-t", f"{output_duration:.6f}", "-movflags", "+faststart", str(directory / "edited.mp4")]
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors, text=True)
        last_update = time.monotonic()
        try:
            for line in process.stdout:
                key, _, value = line.strip().partition("=")
                if key == "out_time_us":
                    try:
                        progress = min(94, max(3, round(float(value) / 1_000_000 / output_duration * 90) + 3))
                    except ValueError:
                        continue
                    if time.monotonic() - last_update >= 1:
                        state["progress"] = progress
                        media_exports.save_export(state)
                        last_update = time.monotonic()
            if process.wait():
                errors.seek(0)
                raise ValueError("Clip render failed: " + errors.read(1500).decode(errors="replace"))
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.kill()
                process.wait()
    entry = {"id": "clip", "title": source.name, "duration": output_duration,
             "original_duration": duration, "edit_options": options.model_dump()}
    state.update(progress=96, stage="Packing clip download")
    media_exports.save_export(state)
    with zipfile.ZipFile(root / "khmer-bundle.zip", "w", zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
        archive.write(directory / "edited.mp4", "edited.mp4")
        archive.writestr("manifest.json", json.dumps(entry, ensure_ascii=False, indent=2))
    state.update(status="completed", progress=100, stage="Ready to download", jobs=[entry])
    media_exports.save_export(state)


async def run_clip_edit(state, source):
    try:
        with cache_in_use(media_exports.export_directory(state["id"])):
            async with resource_slot("video_render", 1):
                await run_blocking(_render_clip, state, Path(source))
    except asyncio.CancelledError:
        state.update(status="interrupted", stage="Clip render interrupted; upload again to retry")
        media_exports.save_export(state)
        raise
    except Exception as error:
        state.update(status="failed", stage="Clip render failed", error=str(error))
        media_exports.save_export(state)


def launch_clip_edit(state, source):
    task = asyncio.create_task(run_clip_edit(state, source))
    media_exports.TASKS[state["id"]] = task
    task.add_done_callback(lambda _: media_exports.TASKS.pop(state["id"], None))
