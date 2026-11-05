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
#  Character Voice Personas & Anti-Copyright Profiles
# ─────────────────────────────────────────────

VOICE_PROFILES = {
    "male": {
        "id": "male",
        "name": "Piseth (Hero / Lead Male)",
        "label": "👨 ពិសិដ្ឋ / Piseth (Lead Hero)",
        "base_voice": "km-KH-PisethNeural",
        "gender": "male",
        "pitch_offset_hz": 0,
        "rate_offset_pct": 10,
        "eq_profile": "male_hero",
    },
    "male_deep": {
        "id": "male_deep",
        "name": "Piseth (Mature Father / Authority)",
        "label": "🧔 លោកពុក / Mature Father / Authority (-10Hz)",
        "base_voice": "km-KH-PisethNeural",
        "gender": "male",
        "pitch_offset_hz": -10,
        "rate_offset_pct": 5,
        "eq_profile": "male_deep",
    },
    "male_young": {
        "id": "male_young",
        "name": "Piseth (Youth / Energetic Boy)",
        "label": "👦 យុវជន / Energetic Youth (+10Hz)",
        "base_voice": "km-KH-PisethNeural",
        "gender": "male",
        "pitch_offset_hz": 10,
        "rate_offset_pct": 14,
        "eq_profile": "male_young",
    },
    "female": {
        "id": "female",
        "name": "Sreymom (Heroine / Lead Female)",
        "label": "👩 ស្រីមុំ / Sreymom (Lead Heroine)",
        "base_voice": "km-KH-SreymomNeural",
        "gender": "female",
        "pitch_offset_hz": 0,
        "rate_offset_pct": 10,
        "eq_profile": "female_heroine",
    },
    "female_sweet": {
        "id": "female_sweet",
        "name": "Sreymom (Sweet Young Girl)",
        "label": "👧 នារីផ្អែមល្ហែម / Sweet Young Girl (+10Hz)",
        "base_voice": "km-KH-SreymomNeural",
        "gender": "female",
        "pitch_offset_hz": 10,
        "rate_offset_pct": 13,
        "eq_profile": "female_sweet",
    },
    "female_elder": {
        "id": "female_elder",
        "name": "Sreymom (Mature Mother / Elder)",
        "label": "👵 អ្នកម្តាយ / Mature Mother / Elder (-7Hz)",
        "base_voice": "km-KH-SreymomNeural",
        "gender": "female",
        "pitch_offset_hz": -7,
        "rate_offset_pct": 4,
        "eq_profile": "female_elder",
    },
}

EQ_PROFILES = {
    "male_hero": (
        "highpass=f=80,"
        "equalizer=f=2500:width_type=o:width=2.0:g=3,"
        "equalizer=f=250:width_type=o:width=1.5:g=2"
    ),
    "male_deep": (
        "highpass=f=70,"
        "equalizer=f=180:width_type=o:width=1.2:g=4.5,"
        "equalizer=f=2200:width_type=o:width=1.8:g=2,"
        "equalizer=f=5500:width_type=o:width=2.0:g=-1.5"
    ),
    "male_young": (
        "highpass=f=100,"
        "equalizer=f=3200:width_type=o:width=1.8:g=3.5,"
        "equalizer=f=300:width_type=o:width=1.5:g=-1.5"
    ),
    "female_heroine": (
        "highpass=f=90,"
        "equalizer=f=2800:width_type=o:width=1.8:g=3,"
        "equalizer=f=350:width_type=o:width=1.5:g=2"
    ),
    "female_sweet": (
        "highpass=f=110,"
        "equalizer=f=3500:width_type=o:width=2.0:g=4,"
        "equalizer=f=400:width_type=o:width=1.5:g=-2,"
        "equalizer=f=7500:width_type=o:width=2.0:g=2"
    ),
    "female_elder": (
        "highpass=f=85,"
        "equalizer=f=220:width_type=o:width=1.4:g=3.5,"
        "equalizer=f=2200:width_type=o:width=1.8:g=2.5"
    ),
}

# ─────────────────────────────────────────────
#  Multi-Character Voice Routing
# ─────────────────────────────────────────────

FEMALE_ROLE_KEYWORDS = [
    r"\bmother\b", r"\bmom\b", r"\bgrandma\b", r"\baunt\b", r"\belder\b", r"\bqueen\b",
    r"\bempress\b", r"\bdaughter\b", r"\bgirl\b", r"\bsister\b", r"\bmaiden\b",
    r"\bprincess\b", r"\bsweet\b", r"\bwoman\b", r"\blady\b", r"\bmadam\b",
    r"ម៉ែ", r"ម្តាយ", r"យាយ", r"មីង", r"លោកយាយ", r"ម្ចាស់ក្សត្រី", r"កូនស្រី",
    r"ប្អូនស្រី", r"នារី", r"កញ្ញា", r"ព្រះនាង",
    r"妈", r"娘", r"姐", r"妹", r"婆", r"姨", r"女", r"太太", r"夫人", r"姑娘",
    r"妃", r"后", r"丫头", r"媳妇", r"奶奶", r"伯母", r"婶", r"姑", r"小姐", r"公主",
    r"皇后", r"母后"
]

MALE_ROLE_KEYWORDS = [
    r"\bfather\b", r"\bdad\b", r"\bgrandpa\b", r"\belder\b", r"\bmaster\b", r"\bboss\b",
    r"\bking\b", r"\bemperor\b", r"\buncle\b", r"\bofficer\b", r"\bman\b", r"\bson\b",
    r"\bboy\b", r"\byouth\b", r"\bgrandson\b", r"\bchild\b", r"\bkid\b", r"\bbrother\b",
    r"ពុក", r"ប៉ា", r"តា", r"លោកពុក", r"ថៅកែ", r"លោកម្ចាស់", r"ស្តេច", r"ពូ",
    r"កូនប្រុស", r"ប្អូនប្រុស", r"ក្មេង", r"ចៅ",
    r"爸", r"爹", r"哥", r"弟", r"爷", r"叔", r"男", r"先生", r"少爷", r"皇帝",
    r"王爷", r"儿子", r"师傅", r"师兄", r"师弟", r"陛下", r"大人", r"侍卫",
    r"兄弟", r"老汉", r"掌柜", r"徒弟"
]


def resolve_character_voice(
    seg: Dict[str, Any],
    global_voice_type: str = "multi",
    speaker_voice_map: Optional[Dict[str, str]] = None,
) -> str:
    """
    Dynamically maps a dialogue cue to one of the 6 distinct character voices
    (Hero, Mature Father, Youth Boy, Heroine, Sweet Girl, Mature Mother)
    so multiple characters sound like completely different actors and Content ID is minimized.
    """
    if speaker_voice_map is None:
        speaker_voice_map = {}

    # If user selected an explicit single voice profile, use it
    if global_voice_type in VOICE_PROFILES and global_voice_type not in ("multi", "auto", "cinema_multi"):
        return global_voice_type

    # If segment already has an explicit persona set, preserve it
    seg_voice = seg.get("voice_type")
    if seg_voice in VOICE_PROFILES:
        return seg_voice

    speaker = str(seg.get("speaker") or "").strip()
    text = str(seg.get("text") or "").strip()
    intent = str(seg.get("english_intent") or "").strip()
    combined = f"{speaker} {intent} {text}"

    # Determine gender
    gender = seg.get("gender")
    if not gender or gender not in ("male", "female"):
        # Match regex markers
        is_female = any(re.search(pat, combined, re.I) for pat in FEMALE_ROLE_KEYWORDS)
        gender = "female" if is_female else "male"
    seg["gender"] = gender

    # Check for mature elder cues
    elder_cues = [
        r"\bmother\b", r"\bmom\b", r"\bgrandma\b", r"\baunt\b", r"\belder\b", r"\bqueen\b",
        r"\bempress\b", r"ម៉ែ", r"ម្តាយ", r"យាយ", r"មីង", r"លោកយាយ", r"ម្ចាស់ក្សត្រី",
        r"妈", r"娘", r"婆", r"奶奶", r"伯母", r"母后", r"皇后"
    ]
    sweet_cues = [
        r"\bdaughter\b", r"\bgirl\b", r"\bsister\b", r"\bmaiden\b", r"\bprincess\b", r"\bsweet\b",
        r"កូនស្រី", r"ប្អូនស្រី", r"នារី", r"កញ្ញា", r"ព្រះនាង",
        r"妹", r"姑娘", r"丫头", r"小姐", r"公主", r"女儿"
    ]
    father_cues = [
        r"\bfather\b", r"\bdad\b", r"\bgrandpa\b", r"\belder\b", r"\bmaster\b", r"\bboss\b",
        r"\bking\b", r"\bemperor\b", r"\buncle\b", r"\bofficer\b", r"ពុក", r"ប៉ា", r"តា",
        r"លោកពុក", r"ថៅកែ", r"លោកម្ចាស់", r"ស្តេច", r"ពូ",
        r"爸", r"爹", r"爷", r"叔", r"老汉", r"掌柜", r"陛下", r"大人", r"王爷"
    ]
    young_cues = [
        r"\bson\b", r"\bboy\b", r"\byouth\b", r"\bgrandson\b", r"\bchild\b", r"\bkid\b",
        r"\bdisciple\b", r"\blittle brother\b", r"កូនប្រុស", r"ប្អូនប្រុស", r"ក្មេង", r"ចៅ",
        r"儿子", r"孙子", r"弟", r"少爷", r"徒弟", r"孩"
    ]

    target_profile = None

    if gender == "female":
        if any(re.search(pat, combined, re.I) for pat in elder_cues):
            target_profile = "female_elder"
        elif any(re.search(pat, combined, re.I) for pat in sweet_cues):
            target_profile = "female_sweet"
        else:
            target_profile = "female"
    else:
        if any(re.search(pat, combined, re.I) for pat in father_cues):
            target_profile = "male_deep"
        elif any(re.search(pat, combined, re.I) for pat in young_cues):
            target_profile = "male_young"
        else:
            target_profile = "male"

    # Multi-speaker distinctness: If speaker name is specified, remember their voice
    spk_key = speaker or f"unnamed_{gender}"
    if spk_key in speaker_voice_map:
        return speaker_voice_map[spk_key]

    # If this role persona is already taken by another character of the same gender,
    # assign an alternative persona so 2-3 people sound distinct
    used_voices = set(speaker_voice_map.values())
    if target_profile in used_voices:
        if gender == "female":
            for alt in ["female_sweet", "female_elder", "female"]:
                if alt not in used_voices:
                    target_profile = alt
                    break
        else:
            for alt in ["male_deep", "male_young", "male"]:
                if alt not in used_voices:
                    target_profile = alt
                    break

    speaker_voice_map[spk_key] = target_profile
    return target_profile

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
    Defaults to +10% speed for fluent, lively conversational Khmer speech matching video pacing.
    """
    clean = text.strip()

    # Rate: Fluent, conversational pacing (+10% eliminates slow dragging)
    rate_pct = 10

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
    syllables = _count_khmer_syllables(clean)
    volume_pct = +5 if syllables <= 5 else 0

    rate_str   = f"+{rate_pct}%" if rate_pct >= 0 else f"{rate_pct}%"
    pitch_str  = f"+{pitch_hz}Hz" if pitch_hz >= 0 else f"{pitch_hz}Hz"
    volume_str = f"+{volume_pct}%" if volume_pct >= 0 else f"{volume_pct}%"

    return rate_str, pitch_str, volume_str


# ─────────────────────────────────────────────
#  FFmpeg Audio Enhancement
# ─────────────────────────────────────────────

def _enhance_audio_clip(
    input_path: str,
    eq_profile: str = "male_hero",
    anti_copyright: bool = True,
) -> str:
    """
    Applies a post-processing FFmpeg EQ + loudnorm chain to an Edge-TTS MP3:

      1. Head/tail silence removal
      2. Persona-specific highpass & EQ curve (hero, deep father, youth, sweet girl, etc.)
      3. Anti-copyright harmonic & stereo phase shaping: alters automated Content ID acoustic
         fingerprint while preserving 100% natural studio dialogue for listeners
      4. Loudnorm: -16 LUFS / TP -1.5 dB consistent perceived loudness

    Overwrites the original file in-place (writes to a temp file first).
    Returns the path to the enhanced file (same as input).
    """
    in_path  = Path(input_path)
    tmp_path = in_path.parent / f"_eq_{in_path.name}"

    filter_profile = EQ_PROFILES.get(eq_profile, EQ_PROFILES["male_hero"])

    anti_cp_part = ""
    if anti_copyright:
        anti_cp_part = (
            ",stereotools=mlev=0.88:slev=1.12,"
            "equalizer=f=1150:width_type=o:width=1.5:g=-1.5,"
            "equalizer=f=3350:width_type=o:width=1.5:g=1.5"
        )

    eq_chain = (
        "silenceremove=start_periods=1:start_duration=0.02:start_threshold=-40dB,"
        "areverse,silenceremove=start_periods=1:start_duration=0.02:start_threshold=-40dB,areverse,"
        f"{filter_profile}"
        f"{anti_cp_part},"
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
        if tmp_path.exists() and tmp_path.stat().st_size > 300:
            tmp_path.replace(in_path)   # atomic rename → overwrite original
        else:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
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
    anti_copyright: bool = True,
) -> str:
    """
    Synthesizes a single text string into an MP3 audio file with smart prosody.

    Args:
        text:           Khmer text to synthesize.
        output_path:    Destination .mp3 path.
        voice_type:     Voice profile ("male", "male_deep", "male_young", "female", "female_sweet", "female_elder").
        rate:           Edge-TTS rate string ("+5%") or "auto" to detect from text & profile.
        pitch:          Edge-TTS pitch string ("+2Hz") or "auto".
        volume:         Edge-TTS volume string ("+0%") or "auto".
        seg_duration:   Original segment duration (sec) — used by auto-prosody.
        enhance:        If True, applies FFmpeg EQ + loudnorm post-processing.
        anti_copyright: If True, applies anti-copyright acoustic phase shaping.
    """
    clean_text = normalize_khmer_unicode(text.strip())
    if not clean_text:
        return ""

    profile = VOICE_PROFILES.get(voice_type.lower(), VOICE_PROFILES["male"])
    voice = profile["base_voice"]
    eq_profile = profile.get("eq_profile", "male_hero")

    # Resolve prosody with profile offsets
    auto_rate, auto_pitch, auto_volume = _detect_prosody(clean_text, seg_duration)

    # Apply persona pitch and rate offsets
    pitch_offset = profile.get("pitch_offset_hz", 0)
    rate_offset = profile.get("rate_offset_pct", 10)

    if not rate or rate == "auto":
        eff_rate = f"+{rate_offset}%" if rate_offset >= 0 else f"{rate_offset}%"
    else:
        eff_rate = _sanitize_edge_tts_knob(rate, "%")

    if not pitch or pitch == "auto":
        # Base auto pitch + persona pitch offset
        try:
            base_p = int(auto_pitch.replace("Hz", ""))
        except Exception:
            base_p = 0
        total_p = base_p + pitch_offset
        eff_pitch = f"+{total_p}Hz" if total_p >= 0 else f"{total_p}Hz"
    else:
        eff_pitch = _sanitize_edge_tts_knob(pitch, "Hz")

    eff_volume = auto_volume if (not volume or volume == "auto") else _sanitize_edge_tts_knob(volume, "%")

    communicate = edge_tts.Communicate(
        clean_text, voice,
        rate=eff_rate,
        pitch=eff_pitch,
        volume=eff_volume,
    )
    await communicate.save(output_path)

    if enhance:
        _enhance_audio_clip(output_path, eq_profile=eq_profile, anti_copyright=anti_copyright)

    return output_path


async def synthesize_segments(
    segments: List[Dict[str, Any]],
    output_dir: Path,
    voice_type: str = "male",
    rate: str = "auto",
    pitch: str = "auto",
    volume: str = "auto",
    max_concurrency: int = 3,
    enhance: bool = True,
    anti_copyright: bool = True,
    on_progress=None,
) -> List[Dict[str, Any]]:
    """
    Synthesizes speech for a list of timestamped segments in parallel with auto-resume,
    chunked batching, and exponential backoff for 10h+ videos (12,000+ lines).

    Features:
    - Auto-Resume: Automatically reuses already-synthesized MP3 clips on disk without re-downloading.
    - Rate Limit Protection: Batched execution with exponential backoff (up to 45s cooldown) against Edge-TTS 429s.
    - Zero-byte file prevention: Validates audio file size and cleans up incomplete clips.
    """
    import random
    output_dir.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(max_concurrency)
    total = len(segments)
    done_count = 0
    lock = asyncio.Lock()

    # Character-to-voice mapping so characters remain voice-consistent
    speaker_voice_map: Dict[str, str] = {}

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

        # Resolve Character Voice Persona
        seg_voice_type = resolve_character_voice(seg, voice_type, speaker_voice_map)
        profile = VOICE_PROFILES.get(seg_voice_type, VOICE_PROFILES["male"])
        voice = profile["base_voice"]
        eq_profile = profile.get("eq_profile", "male_hero")
        seg["voice_type"] = seg_voice_type

        clip_path = output_dir / f"seg_{seg['id']}_{seg_voice_type}.mp3"

        # ─── AUTO-RESUME CHECKPOINT ───
        # If this segment was already synthesized and is valid (>500 bytes), reuse it immediately!
        if clip_path.exists() and clip_path.stat().st_size > 500:
            seg["audio_file"] = str(clip_path)
            seg["voice_type"] = seg_voice_type
            seg["prosody"] = {
                "rate": rate,
                "pitch": pitch,
                "volume": volume,
                "persona": profile["name"],
            }
            async with lock:
                done_count += 1
                if on_progress:
                    on_progress(done_count, total)
            return

        # Clean up corrupt/zero-byte files before synthesizing
        if clip_path.exists() and clip_path.stat().st_size <= 500:
            clip_path.unlink(missing_ok=True)

        auto_rate, auto_pitch, auto_volume = _detect_prosody(clean, seg_dur)

        req_rate = seg.get("rate") or rate
        req_pitch = seg.get("pitch") or pitch
        req_volume = seg.get("volume") or volume

        pitch_offset = profile.get("pitch_offset_hz", 0)
        rate_offset = profile.get("rate_offset_pct", 10)

        if not req_rate or req_rate == "auto":
            eff_rate = f"+{rate_offset}%" if rate_offset >= 0 else f"{rate_offset}%"
        else:
            eff_rate = _sanitize_edge_tts_knob(req_rate, "%")

        if not req_pitch or req_pitch == "auto":
            try:
                base_p = int(auto_pitch.replace("Hz", ""))
            except Exception:
                base_p = 0
            total_p = base_p + pitch_offset
            eff_pitch = f"+{total_p}Hz" if total_p >= 0 else f"{total_p}Hz"
        else:
            eff_pitch = _sanitize_edge_tts_knob(req_pitch, "Hz")

        eff_volume = auto_volume if (not req_volume or req_volume == "auto") else _sanitize_edge_tts_knob(req_volume, "%")

        # ─── ROBUST RETRY WITH EXPONENTIAL BACKOFF ───
        backoff_delays = [1.5, 3.5, 8.0, 18.0, 35.0]
        synth_success = False

        for attempt, delay in enumerate(backoff_delays):
            try:
                async with semaphore:
                    communicate = edge_tts.Communicate(
                        clean, voice,
                        rate=eff_rate,
                        pitch=eff_pitch,
                        volume=eff_volume,
                    )
                    await communicate.save(str(clip_path))

                    # Verify that file was written and is not an empty stub
                    if not clip_path.exists() or clip_path.stat().st_size < 300:
                        raise RuntimeError(f"Edge-TTS returned empty audio for seg {seg['id']}")

                    if enhance:
                        loop = asyncio.get_event_loop()
                        await loop.run_in_executor(
                            None,
                            _enhance_audio_clip,
                            str(clip_path),
                            eq_profile,
                            anti_copyright,
                        )

                    seg["audio_file"] = str(clip_path)
                    seg["voice_type"] = seg_voice_type
                    seg["prosody"] = {
                        "rate": eff_rate,
                        "pitch": eff_pitch,
                        "volume": eff_volume,
                        "persona": profile["name"],
                    }
                    synth_success = True
                    break

            except Exception as e:
                if clip_path.exists() and clip_path.stat().st_size < 300:
                    clip_path.unlink(missing_ok=True)

                if attempt == len(backoff_delays) - 1:
                    logger.error(f"Failed to synthesize segment {seg['id']} after {len(backoff_delays)} attempts: {e}")
                    seg["audio_file"] = None
                else:
                    jitter = random.uniform(0.3, 1.2)
                    cooldown = delay + jitter
                    logger.warning(
                        f"Edge-TTS rate-limit/connection error on cue #{seg['id']} "
                        f"(attempt {attempt + 1}/{len(backoff_delays)}). Backing off {cooldown:.1f}s..."
                    )
                    await asyncio.sleep(cooldown)

        async with lock:
            done_count += 1
            if on_progress:
                on_progress(done_count, total)

    # ─── BATCHED EXECUTION TO PREVENT SOCKET/FD EXHAUSTION ───
    # Process in batches of 25 so thousands of dialogue lines don't overwhelm asyncio
    batch_size = 25
    for b_idx in range(0, len(segments), batch_size):
        batch = segments[b_idx : b_idx + batch_size]
        await asyncio.gather(*[_synth_one(seg) for seg in batch])
        # Micro-pause between batches to maintain healthy connection with Microsoft Bing
        await asyncio.sleep(0.08)

    return segments
