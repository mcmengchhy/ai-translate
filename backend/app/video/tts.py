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


def _detect_prosody(
    text: str,
    seg_duration: Optional[float] = None,
) -> Tuple[str, str, str]:
    """
    Auto-determines Edge-TTS prosody knobs from text content and timing.

    Returns: (rate, pitch, volume)  as Edge-TTS-compatible strings, e.g.:
        rate:   "-10%"  slower  /  "+15%"  faster
        pitch:  "-5Hz"  lower   /  "+5Hz"  higher
        volume: "+0%"   normal  /  "+10%"  louder
    """
    clean = text.strip()

    # --- Rate: match TTS speed to available window ---
    rate_pct = 0
    if seg_duration and seg_duration > 0:
        syllables = _count_khmer_syllables(clean)
        # Native Khmer speech: ~3.5 syllables/sec at default rate
        natural_dur = syllables / 3.5 if syllables > 0 else seg_duration
        ratio = natural_dur / seg_duration  # >1 means too slow for slot → speed up
        if ratio > 1.25:
            rate_pct = min(25, int((ratio - 1.0) * 100 * 0.8))   # cap +25%
        elif ratio < 0.75:
            rate_pct = max(-15, int((ratio - 1.0) * 100 * 0.6))  # cap -15%

    # --- Pitch: emotion cues from punctuation ---
    pitch_hz = 0
    if clean.endswith("?"):
        pitch_hz = +3   # rising intonation for questions
    elif clean.endswith("!"):
        pitch_hz = +2   # slight energy for exclamations
    elif clean.endswith("..."):
        pitch_hz = -2   # trailing / melancholy
    elif len(clean) <= 8:
        pitch_hz = +1   # very short utterance — slight lift to prevent flat read

    # --- Volume: short clips can sound quieter, boost them a bit ---
    syllables = _count_khmer_syllables(clean)
    volume_pct = +5 if syllables <= 4 else 0

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
    eff_rate   = auto_rate   if rate   == "auto" else rate
    eff_pitch  = auto_pitch  if pitch  == "auto" else pitch
    eff_volume = auto_volume if volume == "auto" else volume

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
    max_concurrency: int = 8,
    enhance: bool = True,
) -> List[Dict[str, Any]]:
    """
    Synthesizes speech for a list of timestamped segments in parallel.

    Each segment gets individually-tuned prosody (rate / pitch / volume)
    based on its translated text and its target time slot duration.
    Uses an asyncio Semaphore to cap parallel Edge-TTS calls.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    voice = KHMER_VOICES.get(voice_type.lower(), KHMER_VOICES["male"])
    semaphore = asyncio.Semaphore(max_concurrency)

    async def _synth_one(seg: Dict[str, Any]):
        text = seg.get("translation") or seg.get("text", "")
        if not text.strip():
            seg["audio_file"] = None
            return

        clean = normalize_khmer_unicode(text)
        seg_dur = float(seg.get("end", 0)) - float(seg.get("start", 0))
        seg_dur = seg_dur if seg_dur > 0.1 else None

        auto_rate, auto_pitch, auto_volume = _detect_prosody(clean, seg_dur)

        clip_path = output_dir / f"seg_{seg['id']}.mp3"
        async with semaphore:
            try:
                communicate = edge_tts.Communicate(
                    clean, voice,
                    rate=auto_rate,
                    pitch=auto_pitch,
                    volume=auto_volume,
                )
                await communicate.save(str(clip_path))

                if enhance:
                    # Run heavy FFmpeg work in a thread pool so it doesn't block the event loop
                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(None, _enhance_audio_clip, str(clip_path))

                seg["audio_file"] = str(clip_path)
                seg["prosody"] = {
                    "rate": auto_rate,
                    "pitch": auto_pitch,
                    "volume": auto_volume,
                }
            except Exception as e:
                logger.error(f"Error synthesizing segment {seg['id']}: {e}")
                seg["audio_file"] = None

    tasks = [_synth_one(seg) for seg in segments]
    await asyncio.gather(*tasks)
    return segments
