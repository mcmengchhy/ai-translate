"""Khmer Text-to-Speech (TTS) synthesizer using Edge-TTS Neural Voices."""

import asyncio
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional
import edge_tts

from ..khmer import normalize_khmer_unicode

logger = logging.getLogger(__name__)

KHMER_VOICES = {
    "male": "km-KH-PisethNeural",
    "female": "km-KH-SreymomNeural",
}


async def synthesize_khmer_clip(text: str, output_path: str, voice_type: str = "male", rate: str = "+0%") -> str:
    """
    Synthesizes a single text string into an MP3 audio file.
    """
    voice = KHMER_VOICES.get(voice_type.lower(), KHMER_VOICES["male"])
    clean_text = normalize_khmer_unicode(text.strip())

    if not clean_text:
        return ""

    communicate = edge_tts.Communicate(clean_text, voice, rate=rate)
    await communicate.save(output_path)
    return output_path


async def synthesize_segments(
    segments: List[Dict[str, Any]],
    output_dir: Path,
    voice_type: str = "male",
    max_concurrency: int = 8,
) -> List[Dict[str, Any]]:
    """
    Synthesizes speech for a list of timestamped segments in parallel.
    Uses an asyncio Semaphore to synthesize up to max_concurrency clips simultaneously.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    voice = KHMER_VOICES.get(voice_type.lower(), KHMER_VOICES["male"])
    semaphore = asyncio.Semaphore(max_concurrency)

    async def _synth_one(seg: Dict[str, Any]):
        text = seg.get("translation") or seg.get("text", "")
        if not text.strip():
            seg["audio_file"] = None
            return

        clip_path = output_dir / f"seg_{seg['id']}.mp3"
        async with semaphore:
            try:
                communicate = edge_tts.Communicate(normalize_khmer_unicode(text), voice)
                await communicate.save(str(clip_path))
                seg["audio_file"] = str(clip_path)
            except Exception as e:
                logger.error(f"Error synthesizing segment {seg['id']}: {e}")
                seg["audio_file"] = None

    tasks = [_synth_one(seg) for seg in segments]
    await asyncio.gather(*tasks)
    return segments
