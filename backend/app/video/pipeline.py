"""End-to-End Video Translation and Khmer Voice Dubbing Pipeline."""

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Dict, Any, Optional

from .downloader import download_video
from .transcriber import transcribe_video_audio
from .tts import synthesize_segments
from .dubber import assemble_dubbed_video
from ..engines import get_engine
from ..khmer import normalize_khmer_unicode

import json
logger = logging.getLogger(__name__)

ROOT_PROJECT_DIR = Path(__file__).resolve().parent.parent.parent.parent
JOBS_FILE = ROOT_PROJECT_DIR / "storage" / "jobs.json"


def _load_jobs() -> Dict[str, Dict[str, Any]]:
    if JOBS_FILE.exists():
        try:
            return json.loads(JOBS_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error(f"Failed to load jobs from {JOBS_FILE}: {e}")
    return {}


def _save_job(job_id: str):
    try:
        current_jobs = _load_jobs()
        if job_id in JOBS:
            current_jobs[job_id] = JOBS[job_id]
            JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
            JOBS_FILE.write_text(json.dumps(current_jobs, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.error(f"Failed to save job {job_id}: {e}")


# In-memory and persistent job tracker
JOBS: Dict[str, Dict[str, Any]] = _load_jobs()


def create_job(url_or_file: str) -> str:
    job_id = str(uuid.uuid4())[:8]
    JOBS[job_id] = {
        "id": job_id,
        "status": "queued",
        "progress": 0,
        "stage": "Queued",
        "title": "Processing video...",
        "input": url_or_file,
        "segments": [],
        "result": None,
        "error": None,
    }
    _save_job(job_id)
    return job_id


def get_job_status(job_id: str) -> Optional[Dict[str, Any]]:
    if job_id in JOBS:
        return JOBS[job_id]
    disk_jobs = _load_jobs()
    if job_id in disk_jobs:
        JOBS[job_id] = disk_jobs[job_id]
        return JOBS[job_id]
    return None


async def run_video_dubbing_pipeline(
    job_id: str,
    video_source: str,  # URL or local file path
    is_url: bool = True,
    voice_type: str = "male",
    tone: str = "standard",
    engine_name: str = "auto",
    audio_mode: str = "replace",
    burn_subtitles: bool = False,
    max_duration: Optional[int] = 90,
    api_key: Optional[str] = None,
    subtitle_file_path: Optional[str] = None,
    voice_rate: str = "auto",
    voice_pitch: str = "auto",
    voice_volume: str = "auto",
):
    job = JOBS.get(job_id)
    if not job:
        return

    try:
        # Step 1: Video Ingestion
        job["status"] = "processing"
        job["stage"] = "Downloading / Preparing video"
        job["progress"] = 10

        if is_url:
            logger.info(f"Downloading video from {video_source} (max {max_duration}s)...")
            dl_info = await download_video(video_source, max_clip_seconds=max_duration)
            video_path = dl_info["video_path"]
            job["title"] = dl_info.get("title", "Video")
            job["platform"] = dl_info.get("platform", "Web Video")
            if not subtitle_file_path and dl_info.get("sub_path"):
                subtitle_file_path = dl_info["sub_path"]
                logger.info(f"Using downloaded subtitle track: {subtitle_file_path}")
        else:
            video_path = video_source
            job["title"] = Path(video_path).stem
            job["platform"] = "Uploaded File"

        job["video_path"] = str(video_path)
        job["progress"] = 25

        # Step 2: Audio Transcription & Subtitle Reading (Hardsubs / Softsubs)
        job["stage"] = "Transcribing speech audio"
        logger.info(f"Transcribing and reading on-screen subtitles for: {video_path}")

        def _on_ocr_progress(done, total):
            job["progress"] = 25 + int(done / total * 23)
            job["stage"] = f"Reading on-screen hardsubs ({done}/{total})"
            _save_job(job_id)

        transcription = await transcribe_video_audio(
            video_path,
            external_subtitle_path=subtitle_file_path,
            on_ocr_progress=_on_ocr_progress,
        )
        segments = transcription["segments"]
        job["detected_language"] = transcription.get("language", "auto")
        job["subtitle_source"] = transcription.get("source", "speech_and_hardsub_ocr")
        job["progress"] = 50

        if not segments:
            raise RuntimeError("No spoken dialogue or speech detected in the video audio track.")

        # Step 3: AI Translation using 2-Step Reasoning Pivot (Chinese -> English Intent -> Khmer Dubbing)
        job["stage"] = "Translating dialogue with 2-step English reasoning"
        logger.info(f"Translating {len(segments)} segments to Khmer with 2-step English reasoning pivot...")

        from ..engines.dialogue_translator import translate_dialogue_batch_gemini, translate_dialogue_clusters_free
        from ..khmer import batch_polish_segments

        import os
        effective_api_key = (api_key or os.getenv("GEMINI_API_KEY", "")).strip()

        if effective_api_key:
            batch_size = 30
            for b in range(0, len(segments), batch_size):
                b_chunk = segments[b : b + batch_size]
                try:
                    await translate_dialogue_batch_gemini(b_chunk, job["detected_language"], effective_api_key, tone=tone)
                except Exception as e:
                    logger.warning(f"Gemini batch failed: {e}. Falling back to cluster translation...")
                    await translate_dialogue_clusters_free(b_chunk, job["detected_language"], tone=tone)
                job["progress"] = 50 + int((min(b + batch_size, len(segments)) / len(segments)) * 20)
                job["stage"] = f"Translating dialogue ({min(b + batch_size, len(segments))}/{len(segments)})"
                _save_job(job_id)
        else:
            def _on_trans_progress(done_chunks, total_chunks):
                job["progress"] = 50 + int(done_chunks / total_chunks * 20)
                job["stage"] = f"Translating dialogue ({done_chunks}/{total_chunks})"
                _save_job(job_id)

            await translate_dialogue_clusters_free(
                segments,
                job["detected_language"],
                tone=tone,
                on_progress=_on_trans_progress,
            )

        batch_polish_segments(segments)

        # Deduplicate any adjacent cues that have identical translations or source text
        deduped = []
        for s in segments:
            if not deduped:
                deduped.append(s)
                continue
            p = deduped[-1]
            t_cur = s.get("translation", "").strip()
            t_prev = p.get("translation", "").strip()
            s_cur = s.get("text", "").strip()
            s_prev = p.get("text", "").strip()
            is_dup = (
                (t_cur and t_prev and t_cur == t_prev) or
                (s_cur and s_prev and s_cur == s_prev)
            ) and (s.get("start", 0) <= p.get("end", 0) + 1.2)
            if is_dup:
                p["end"] = max(p.get("end", 0), s.get("end", 0))
            else:
                deduped.append(s)
        segments = deduped
        for idx, s in enumerate(segments, start=1):
            s["id"] = idx
            s["target_start"] = float(s.get("target_start", s["start"]))
            s["target_end"] = float(s.get("target_end", s["end"]))

        job["segments"] = segments
        job["progress"] = 70
        _save_job(job_id)

        # Step 4: Khmer Speech Synthesis (TTS)
        job["stage"] = "Synthesizing Khmer voiceover"
        logger.info(f"Generating Khmer speech ({voice_type}) for {len(segments)} segments...")
        audio_dir = Path(video_path).parent / f"voice_{job_id}"

        # Synthesize with studio-quality enhancement (enhance=True) matching preview audio exactly
        segments = await synthesize_segments(
            segments, audio_dir,
            voice_type=voice_type,
            rate=voice_rate,
            pitch=voice_pitch,
            volume=voice_volume,
            max_concurrency=4,
            enhance=True,
            on_progress=lambda done, total: (
                job.update({"progress": 70 + int(done / total * 13), "stage": f"Synthesizing voice ({done}/{total})"}),
                _save_job(job_id),
            ),
        )
        job["progress"] = 85

        # Step 5: Audio Mixing & Video Dubbing
        job["stage"] = "Mixing audio and assembling video"
        logger.info("Assembling final dubbed video...")
        dub_result = await assemble_dubbed_video(
            video_path=video_path,
            segments=segments,
            output_filename=f"dubbed_{job_id}.mp4",
            audio_mode=audio_mode,
            burn_subtitles=burn_subtitles,
        )

        job["progress"] = 100
        job["status"] = "completed"
        job["stage"] = "Complete"
        job["result"] = dub_result
        job["segments"] = segments  # Update job with final voice-synchronized timestamps and character voice types
        _save_job(job_id)
        logger.info(f"Job {job_id} successfully completed!")

    except Exception as e:
        logger.error(f"Pipeline error for job {job_id}: {e}", exc_info=True)
        job["status"] = "failed"
        job["stage"] = "Failed"
        job["error"] = str(e)
        _save_job(job_id)
