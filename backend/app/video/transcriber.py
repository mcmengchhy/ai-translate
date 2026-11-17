"""Audio extractor and speech-to-text transcriber using faster-whisper and direct FFmpeg PCM pipe."""

import asyncio
import logging
import subprocess
import hashlib
import json
import math
import copy
from pathlib import Path
from typing import List, Dict, Any, Optional
import numpy as np

from .job_identity import media_hash, atomic_json_write

TRANSCRIPT_CACHE_DIR = Path(__file__).resolve().parents[3] / "storage" / "transcripts"

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


def fuse_speech_and_subtitles(speech_segments, subtitle_segments):
    """Keep subtitle wording and aligned spoken wording; retain uncovered speech."""
    combined = []
    covered_speech = set()
    for subtitle in subtitle_segments:
        cue = dict(subtitle)
        matching = []
        for index, speech in enumerate(speech_segments):
            overlap = min(float(cue["end"]), float(speech["end"])) - max(float(cue["start"]), float(speech["start"]))
            if overlap > 0:
                matching.append(speech.get("asr_text") or speech.get("text", ""))
                if overlap >= min(0.2, (float(speech["end"]) - float(speech["start"])) * 0.5):
                    covered_speech.add(index)
        cue["subtitle_text"] = cue.get("text", "")
        cue["asr_text"] = " ".join(text.strip() for text in matching if text.strip())
        combined.append(cue)
    combined.extend(dict(speech) for index, speech in enumerate(speech_segments) if index not in covered_speech)
    combined.sort(key=lambda cue: float(cue["start"]))
    for index, cue in enumerate(combined, start=1):
        cue["id"] = index
    return combined


def _read_transcript_cache(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("segments"), list):
            return None
        if not isinstance(data.get("language"), str) or not math.isfinite(float(data["duration"])) or float(data["duration"]) < 0:
            return None
        for cue in data["segments"]:
            if not isinstance(cue.get("text"), str) or not all(
                math.isfinite(float(cue[field])) for field in ("start", "end")
            ) or float(cue["end"]) < float(cue["start"]):
                return None
        return data
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


async def transcribe_video_audio(
    video_path: str,
    language: Optional[str] = None,
    external_subtitle_path: Optional[str] = None,
    on_ocr_progress=None,
    on_speech_progress=None,
    cache_revision=None,
) -> Dict[str, Any]:
    """Reuse content-addressed speech/OCR checkpoints and report both stages.

    Complete speech is checkpointed before OCR starts. A restart during OCR can
    therefore reuse Whisper's work; identical uploads reuse the final transcript.
    """
    def _sync_transcribe():
        cache_path = speech_path = None
        try:
            if Path(video_path).is_file():
                subtitle_digest = media_hash(external_subtitle_path) if external_subtitle_path and Path(external_subtitle_path).is_file() else ""
                digest = media_hash(video_path)
                revision = [cache_revision] if cache_revision else []
                key = hashlib.sha256(json.dumps(["speech-ocr-v1-base", digest, subtitle_digest, language, *revision]).encode()).hexdigest()
                speech_key = hashlib.sha256(json.dumps(["speech-v1-base", digest, language, *revision]).encode()).hexdigest()
                cache_path = TRANSCRIPT_CACHE_DIR / f"{key}.json"
                speech_path = TRANSCRIPT_CACHE_DIR / f"{speech_key}.speech.json"
                cached = _read_transcript_cache(cache_path)
                if cached is not None:
                    cached["cache_hit"] = True
                    if on_speech_progress:
                        on_speech_progress(cached.get("duration", 0), cached.get("duration", 0))
                    if on_ocr_progress:
                        on_ocr_progress(len(cached["segments"]), len(cached["segments"]))
                    return cached
        except OSError:
            logger.warning("Transcription cache unavailable; processing the source", exc_info=True)

        subtitle_segments = []
        subtitle_source = None
        if external_subtitle_path and Path(external_subtitle_path).exists():
            subtitle_segments = parse_srt_or_vtt(external_subtitle_path)
            if subtitle_segments:
                subtitle_source = "external_file"
        if not subtitle_segments:
            subtitle_segments = extract_embedded_soft_subtitles(video_path) or []
            if subtitle_segments:
                subtitle_source = "container_softsub"

        speech = _read_transcript_cache(speech_path) if speech_path else None
        if speech is None:
            try:
                audio_array, temp_pcm = extract_pcm_from_video(video_path)
            except subprocess.CalledProcessError:
                if not subtitle_segments:
                    raise
                probe = subprocess.run([
                    "ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
                    "stream=index", "-of", "csv=p=0", str(video_path),
                ], capture_output=True, text=True, check=True)
                if probe.stdout.strip():
                    raise
                return {
                    "language": language or "auto", "language_probability": 0,
                    "duration": max(float(seg["end"]) for seg in subtitle_segments),
                    "segments": fuse_speech_and_subtitles([], subtitle_segments),
                    "source": f"{subtitle_source}_without_audio",
                }
            try:
                duration = len(audio_array) / 16000.0
                model = get_whisper_model("base")
                logger.info("Transcribing %.1fs audio with faster-whisper...", duration)
                segments_gen, info = model.transcribe(
                    audio_array, language=language if language and language != "auto" else None,
                    beam_size=5, vad_filter=False, condition_on_previous_text=False, no_speech_threshold=.6,
                )
                segments_list = []
                for index, segment in enumerate(segments_gen, 1):
                    text = segment.text.strip()
                    if text:
                        segments_list.append({"id": index, "start": round(segment.start, 2),
                                              "end": round(segment.end, 2), "text": text, "asr_text": text})
                    if on_speech_progress:
                        on_speech_progress(min(duration, segment.end), duration)
                speech = {"language": info.language, "language_probability": round(info.language_probability, 2),
                          "duration": round(duration, 2), "segments": segments_list}
                if speech_path:
                    atomic_json_write(speech_path, speech)
            finally:
                del audio_array
                temp_pcm.unlink(missing_ok=True)
        elif on_speech_progress:
            on_speech_progress(speech["duration"], speech["duration"])

        segments_list = copy.deepcopy(speech["segments"])
        if subtitle_segments:
            segments_list = fuse_speech_and_subtitles(segments_list, subtitle_segments)
        elif segments_list:
            segments_list = enhance_segments_with_hardsubs(video_path, segments_list, progress_callback=on_ocr_progress)
        for segment in segments_list:
            if segment.get("ocr_detected"):
                segment["subtitle_text"] = segment.get("text", "")
        result = {**speech, "segments": segments_list,
                  "source": f"speech_and_{subtitle_source}" if subtitle_source else "speech_and_hardsub_ocr"}
        if cache_path:
            atomic_json_write(cache_path, result)
        return result

    from .cache import cache_in_use
    from .resources import run_blocking
    with cache_in_use(Path(video_path).parent):
        return await run_blocking(_sync_transcribe)
