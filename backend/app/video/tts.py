"""Khmer Text-to-Speech (TTS) synthesizer using Edge-TTS Neural Voices.

Voice Quality Features:
- Smart prosody: auto-adjusts rate/pitch/volume per segment based on text length,
  segment duration, punctuation cues (questions, exclamations), and speech tempo.
- SSML-style edge-tts prosody via Communicate(rate, pitch, volume) kwargs.
- Post-synthesis FFmpeg EQ chain: high-pass 80Hz, presence boost 2.5kHz,
  warmth boost 250Hz, loudness normalization (EBU R128 -16 LUFS).
"""

import asyncio
import logging
import re
import subprocess
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
import edge_tts

from ..khmer import normalize_khmer_unicode

logger = logging.getLogger(__name__)

KHMER_VOICES = {
    "male": "km-KH-PisethNeural",
    "female": "km-KH-SreymomNeural",
}

# ─────────────────────────────────────────────
#  Prosody Auto-Detection Helpers
# ─────────────────────────────────────────────

def _count_khmer_syllables(text: str) -> int:
    """Rough syllable count: count Khmer consonant clusters (base chars)."""
    return len(re.findall(r"[\u1780-\u17A2]", text))


def _sanitize_edge_tts_knob(val: Optional[str], default_unit: str = "%") -> str:
    """Normalizes Edge-TTS rate/pitch/volume strings into valid ^[+-]\\d+(%|Hz)$."""
    if not val:
        return f"+0{default_unit}"
    val = str(val).strip()
    if val == "auto":
        return f"+0{default_unit}"
    if not val.endswith(default_unit):
        val = f"{val}{default_unit}"
    if not val.startswith("+") and not val.startswith("-"):
        val = f"+{val}"
    return val


def _detect_prosody(
    text: str,
    seg_duration: Optional[float] = None,
) -> Tuple[str, str, str]:
    """
    Auto-determines Edge-TTS prosody knobs from text content and timing.
    Dynamically adjusts speaking rate based on segment duration and syllable count
    so the Khmer speech naturally fits the character's mouth duration.
    """
    clean = text.strip()

    # Rate: Fluent, conversational pacing (+10% eliminates slow dragging)
    rate_pct = 10
    syllables = _count_khmer_syllables(clean)

    # Dynamic Lip-Sync Pacing:
    # Khmer has ~4.0 syllables/sec at normal speed.
    # If the visual mouth window (seg_duration) is tight, increase rate up to +28%
    # so Edge-TTS synthesizes the voice fast enough naturally without clipping words!
    if seg_duration and seg_duration > 0.3:
        estimated_natural_dur = max(0.4, syllables / 4.0)
        if estimated_natural_dur > seg_duration:
            ratio = estimated_natural_dur / seg_duration
            rate_pct = min(28, max(10, int((ratio - 1.0) * 70) + 10))

    # Pitch: Subtle emotion cues from punctuation
    pitch_hz = 0
    if clean.endswith("?"):
        pitch_hz = +2   # rising intonation for questions
    elif clean.endswith("!"):
        pitch_hz = +1   # slight energy for exclamations
    elif clean.endswith("..."):
        pitch_hz = -1   # trailing / thoughtful
    elif len(clean) <= 8:
        pitch_hz = +1   # very short utterance

    # Volume: short clips can sound quieter, boost slightly
    volume_pct = +5 if syllables <= 5 else 0

    rate_str   = f"+{rate_pct}%" if rate_pct >= 0 else f"{rate_pct}%"
    pitch_str  = f"+{pitch_hz}Hz" if pitch_hz >= 0 else f"{pitch_hz}Hz"
    volume_str = f"+{volume_pct}%" if volume_pct >= 0 else f"{volume_pct}%"

    return rate_str, pitch_str, volume_str


# ─────────────────────────────────────────────
#  FFmpeg Audio Enhancement
# ─────────────────────────────────────────────

def _enhance_audio_clip(input_path: str) -> str:
    """
    Applies a post-processing FFmpeg EQ + loudnorm chain to an Edge-TTS MP3:

      1. High-pass  80 Hz  → removes low-frequency rumble
      2. Presence  +3 dB @ 2.5 kHz (q=2.0) → speech intelligibility & crispness
      3. Warmth    +2 dB @ 250 Hz  (q=1.5) → natural vocal body
      4. loudnorm  −16 LUFS / TP −1.5 dB   → consistent perceived loudness

    Overwrites the original file in-place (writes to a temp file first).
    Returns the path to the enhanced file (same as input).
    """
    in_path  = Path(input_path)
    tmp_path = in_path.parent / f"_eq_{in_path.name}"

    eq_chain = (
        "silenceremove=start_periods=1:start_duration=0.02:start_threshold=-40dB,"
        "areverse,silenceremove=start_periods=1:start_duration=0.02:start_threshold=-40dB,areverse,"
        "highpass=f=80,"
        "equalizer=f=2500:width_type=o:width=2.0:g=3,"
        "equalizer=f=250:width_type=o:width=1.5:g=2,"
        "loudnorm=I=-16:TP=-1.5:LRA=7"
    )

    cmd = [
        "ffmpeg", "-y",
        "-i", str(in_path),
        "-af", eq_chain,
        "-c:a", "libmp3lame", "-q:a", "2",
        str(tmp_path),
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        tmp_path.replace(in_path)   # atomic rename → overwrite original
    except Exception as e:
        logger.warning(f"Audio enhancement failed for {input_path}: {e}. Using raw TTS output.")
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)

    return str(in_path)


# ─────────────────────────────────────────────
#  Public API
# ─────────────────────────────────────────────

async def synthesize_khmer_clip(
    text: str,
    output_path: str,
    voice_type: str = "male",
    rate: str = "auto",
    pitch: str = "auto",
    volume: str = "auto",
    seg_duration: Optional[float] = None,
    enhance: bool = True,
) -> str:
    """
    Synthesizes a single text string into an MP3 audio file with smart prosody.

    Args:
        text:         Khmer text to synthesize.
        output_path:  Destination .mp3 path.
        voice_type:   "male" (Piseth) or "female" (Sreymom).
        rate:         Edge-TTS rate string ("+5%") or "auto" to detect from text.
        pitch:        Edge-TTS pitch string ("+2Hz") or "auto".
        volume:       Edge-TTS volume string ("+0%") or "auto".
        seg_duration: Original segment duration (sec) — used by auto-prosody.
        enhance:      If True, applies FFmpeg EQ + loudnorm post-processing.
    """
    voice = KHMER_VOICES.get(voice_type.lower(), KHMER_VOICES["male"])
    clean_text = normalize_khmer_unicode(text.strip())
    if not clean_text:
        return ""

    # Resolve prosody
    auto_rate, auto_pitch, auto_volume = _detect_prosody(clean_text, seg_duration)
    eff_rate   = auto_rate   if (not rate or rate == "auto") else _sanitize_edge_tts_knob(rate, "%")
    eff_pitch  = auto_pitch  if (not pitch or pitch == "auto") else _sanitize_edge_tts_knob(pitch, "Hz")
    eff_volume = auto_volume if (not volume or volume == "auto") else _sanitize_edge_tts_knob(volume, "%")

    communicate = edge_tts.Communicate(
        clean_text, voice,
        rate=eff_rate,
        pitch=eff_pitch,
        volume=eff_volume,
    )
    await communicate.save(output_path)

    if enhance:
        _enhance_audio_clip(output_path)

    return output_path


async def synthesize_segments(
    segments: List[Dict[str, Any]],
    output_dir: Path,
    voice_type: str = "male",
    rate: str = "auto",
    pitch: str = "auto",
    volume: str = "auto",
    max_concurrency: int = 4,
    enhance: bool = True,
    on_progress=None,
) -> List[Dict[str, Any]]:
    """
    Synthesizes speech for a list of timestamped segments in parallel.

    Each segment gets individually-tuned prosody (rate / pitch / volume).
    Uses an asyncio Semaphore to cap parallel Edge-TTS calls, with retry backoff for 100% reliability.

    Args:
        on_progress: Optional callable(done: int, total: int) — called after
                     each segment finishes so callers can report live progress.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(max_concurrency)
    total = len(segments)
    done_count = 0
    lock = asyncio.Lock()

    async def _synth_one(seg: Dict[str, Any]):
        nonlocal done_count
        text = seg.get("translation") or seg.get("text", "")
        if not text.strip():
            seg["audio_file"] = None
            async with lock:
                done_count += 1
                if on_progress:
                    on_progress(done_count, total)
            return

        clean = normalize_khmer_unicode(text)
        seg_dur = float(seg.get("end", 0)) - float(seg.get("start", 0))
        seg_dur = seg_dur if seg_dur > 0.1 else None

        # Allow per-segment voice override or global voice
        seg_voice_type = seg.get("voice_type") or voice_type
        voice = KHMER_VOICES.get(seg_voice_type.lower(), KHMER_VOICES["male"])

        auto_rate, auto_pitch, auto_volume = _detect_prosody(clean, seg_dur)

        req_rate = seg.get("rate") or rate
        req_pitch = seg.get("pitch") or pitch
        req_volume = seg.get("volume") or volume

        eff_rate   = auto_rate   if (not req_rate or req_rate == "auto") else _sanitize_edge_tts_knob(req_rate, "%")
        eff_pitch  = auto_pitch  if (not req_pitch or req_pitch == "auto") else _sanitize_edge_tts_knob(req_pitch, "Hz")
        eff_volume = auto_volume if (not req_volume or req_volume == "auto") else _sanitize_edge_tts_knob(req_volume, "%")

        clip_path = output_dir / f"seg_{seg['id']}_{seg_voice_type}.mp3"

        # Robust synthesis loop with 3 retries against Edge-TTS rate limits
        for attempt in range(3):
            try:
                async with semaphore:
                    communicate = edge_tts.Communicate(
                        clean, voice,
                        rate=eff_rate,
                        pitch=eff_pitch,
                        volume=eff_volume,
                    )
                    await communicate.save(str(clip_path))

                    if enhance:
                        # Run studio EQ + loudnorm in executor to match preview audio exactly
                        loop = asyncio.get_event_loop()
                        await loop.run_in_executor(None, _enhance_audio_clip, str(clip_path))

                    seg["audio_file"] = str(clip_path)
                    seg["voice_type"] = seg_voice_type
                    seg["prosody"] = {
                        "rate": eff_rate,
                        "pitch": eff_pitch,
                        "volume": eff_volume,
                    }
                    break
            except Exception as e:
                if attempt == 2:
                    logger.error(f"Error synthesizing segment {seg['id']}: {e}")
                    seg["audio_file"] = None
                else:
                    await asyncio.sleep(0.5 * (attempt + 1))

        async with lock:
            done_count += 1
            if on_progress:
                on_progress(done_count, total)

    tasks = [_synth_one(seg) for seg in segments]
    await asyncio.gather(*tasks)
    return segments
