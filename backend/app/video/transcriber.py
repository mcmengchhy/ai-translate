"""Audio extractor and speech-to-text transcriber using faster-whisper and direct FFmpeg PCM pipe."""

import asyncio
import logging
import subprocess
from pathlib import Path
from typing import List, Dict, Any, Optional
import numpy as np

logger = logging.getLogger(__name__)

# Cached model instance
_WHISPER_MODEL = None


def get_whisper_model(model_size: str = "base"):
    global _WHISPER_MODEL
    if _WHISPER_MODEL is None:
        from faster_whisper import WhisperModel
        logger.info(f"Loading faster-whisper model ({model_size})...")
        _WHISPER_MODEL = WhisperModel(model_size, device="cpu", compute_type="int8")
    return _WHISPER_MODEL


def extract_pcm_from_video(video_path: str):
    """
    Extracts raw 16kHz mono float32 PCM directly via FFmpeg to a disk-backed memmap.
    Bypasses PyAV container errors and uses virtually 0 MB RAM even for 10h+ videos.
    """
    temp_pcm = Path(video_path).parent / f"{Path(video_path).stem}_16k.pcm"
    cmd = [
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-vn",
        "-f", "f32le",
        "-ac", "1",
        "-ar", "16000",
        str(temp_pcm)
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    audio_memmap = np.memmap(str(temp_pcm), dtype=np.float32, mode="r")
    return audio_memmap, temp_pcm


from .subtitle_reader import (
    extract_embedded_soft_subtitles,
    parse_srt_or_vtt,
    enhance_segments_with_hardsubs,
)


async def transcribe_video_audio(
    video_path: str,
    language: Optional[str] = None,
    external_subtitle_path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Extracts speech timestamps and dialogue content from video.
    
    1. Checks for user-provided external subtitle file (.srt, .vtt).
    2. Probes video container for embedded soft subtitle streams.
    3. Runs Faster-Whisper to detect speech activity and timing.
    4. Runs on-screen hardsub OCR (RapidOCR) on video frames to replace
       noisy audio homophones with exact on-screen characters.
    """
    def _sync_transcribe():
        # 1. External subtitle file if provided
        if external_subtitle_path and Path(external_subtitle_path).exists():
            parsed = parse_srt_or_vtt(external_subtitle_path)
            if parsed:
                logger.info(f"Using {len(parsed)} cues from external subtitle file: {external_subtitle_path}")
                return {
                    "language": language or "zh",
                    "language_probability": 1.0,
                    "segments": parsed,
                    "source": "external_file",
                }

        # 2. Embedded soft subtitles if available in container
        soft_subs = extract_embedded_soft_subtitles(video_path)
        if soft_subs:
            logger.info(f"Using {len(soft_subs)} cues from container soft subtitles.")
            return {
                "language": language or "zh",
                "language_probability": 1.0,
                "segments": soft_subs,
                "source": "container_softsub",
            }

        # 3. Audio speech recognition via Faster-Whisper
        audio_array, temp_pcm = extract_pcm_from_video(video_path)
        try:
            duration = len(audio_array) / 16000.0

            model = get_whisper_model("base")
            logger.info(f"Transcribing {duration:.1f}s ({duration/3600:.1f}h) audio with faster-whisper...")

            segments_gen, info = model.transcribe(
                audio_array,
                language=language if language and language != "auto" else None,
                beam_size=5,
                vad_filter=True,
                vad_parameters=dict(min_silence_duration_ms=400),
            )

            segments_list = []
            for i, segment in enumerate(segments_gen):
                text = segment.text.strip()
                if text:
                    segments_list.append({
                        "id": i + 1,
                        "start": round(segment.start, 2),
                        "end": round(segment.end, 2),
                        "text": text,
                    })

            # 4. Enhance speech segments with on-screen burned-in subtitle OCR (Hardsubs)
            if segments_list:
                logger.info(f"Checking for on-screen burned-in subtitles (Hardsubs) across {len(segments_list)} segments...")
                segments_list = enhance_segments_with_hardsubs(video_path, segments_list)

            return {
                "language": info.language,
                "language_probability": round(info.language_probability, 2),
                "duration": round(duration, 2),
                "segments": segments_list,
                "source": "speech_and_hardsub_ocr",
            }
        finally:
            del audio_array
            if temp_pcm.exists():
                try:
                    temp_pcm.unlink()
                except Exception:
                    pass

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_transcribe)
