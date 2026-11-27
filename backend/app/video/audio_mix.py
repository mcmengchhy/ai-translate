"""Per-video audio levels and synthesized, sample-free background effects."""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class AudioMixSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    original_volume: int = Field(default=2, ge=0, le=100, strict=True)
    sound_effect: Literal["none", "ambience", "transitions", "both"] = "none"
    effect_volume: int = Field(default=8, ge=0, le=100, strict=True)
    duck_original: bool = False


def saved_audio_mix(job):
    """Display the actual legacy mix until a user saves explicit card settings."""
    if job.get("audio_mix"):
        return AudioMixSettings(**job["audio_mix"]).model_dump()
    result = job.get("result") or {}
    if result.get("audio_mix"):
        return AudioMixSettings(**result["audio_mix"]).model_dump()
    options = job.get("request_options") or {}
    mode = options.get("audio_mode", "replace")
    volume = options.get("bg_volume")
    duck = volume is not None and volume < 0 or volume is None and mode in {"smart", "smart_sidechain"}
    if volume is None:
        volume = {"replace": 0, "mute": 0, "duck_subtle": .05, "smart": .15, "smart_sidechain": .15}.get(mode, .10)
    return AudioMixSettings(original_volume=15 if duck else max(0, min(100, round(volume * 100))),
                            duck_original=duck).model_dump()


def audio_mix_locked(job):
    return job.get("status") in {"processing", "running", "rendering", "pausing"} and (
        job.get("pipeline_phase") == "rendering" or job.get("status") == "pausing")


def audio_mix_filter(settings, duration, has_original=True):
    """Build bounded FFmpeg sources directly; no music recordings are used."""
    mix = AudioMixSettings(**settings)
    filters = []
    inputs = ["[khmer]"]
    if has_original and mix.original_volume:
        filters.append(f"[0:a]volume={mix.original_volume / 100:.3f}[original]")
        if mix.duck_original:
            filters.extend([
                "[1:a]asplit=2[khmer][sidechain]",
                "[original][sidechain]sidechaincompress=threshold=0.008:ratio=20:attack=5:release=350[background]",
            ])
            inputs.append("[background]")
        else:
            filters.append("[1:a]anull[khmer]")
            inputs.append("[original]")
    else:
        filters.append("[1:a]anull[khmer]")
    effect_filters, effect_inputs = generated_effect_filters(mix.sound_effect, mix.effect_volume, duration)
    filters.extend(effect_filters)
    inputs.extend(effect_inputs)
    filters.append("".join(inputs) + f"amix=inputs={len(inputs)}:duration=first:dropout_transition=0:normalize=0[aout]")
    return ";".join(filters)


def generated_effect_filters(sound_effect, effect_volume, duration):
    """Share sample-free effects between dubbing and standalone clip editing."""
    filters, inputs = [], []
    gain = effect_volume / 100
    if gain and sound_effect in {"ambience", "both"}:
        filters.append(f"anoisesrc=color=pink:amplitude=0.08:r=48000:d={duration:.6f},"
                       f"highpass=f=70,lowpass=f=600,volume={gain:.3f}[ambience]")
        inputs.append("[ambience]")
    if gain and sound_effect in {"transitions", "both"}:
        # A short synthesized sweep at the beginning and every thirty seconds.
        filters.append(f"anoisesrc=color=white:amplitude=0.3:r=48000:d={duration:.6f},"
                       "highpass=f=600,lowpass=f=4500,"
                       f"volume='if(lt(mod(t,30),0.6),{gain:.3f}*sin(PI*mod(t,30)/0.6),0)':eval=frame[transitions]")
        inputs.append("[transitions]")
    return filters, inputs
