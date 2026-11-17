"""End-to-End Video Translation and Khmer Voice Dubbing Pipeline."""

import asyncio
import logging
import uuid
import os
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, Any, Optional

from .downloader import download_video
from .transcriber import transcribe_video_audio
from .tts import synthesize_segments
from .dubber import assemble_dubbed_video
from .segments import MissingTranslationsError, missing_translations
from .render_options import VideoRenderOptions
from .resources import resource_slot
from .checkpoints import mark_step, source_segments
from ..core import config

import json
import copy
from .cache import cleanup_processing_cache, CacheCleanupReport
logger = logging.getLogger(__name__)

ROOT_PROJECT_DIR = Path(__file__).resolve().parent.parent.parent.parent
JOBS_FILE = ROOT_PROJECT_DIR / "storage" / "jobs.json"
_WRITE_LOCK = threading.RLock()
_LAST_CHECKPOINT = {}
RUNNING_JOBS = set()
JOB_TASKS = {}
DELETED_JOBS = set()
RESETTING = False


def _load_jobs() -> Dict[str, Dict[str, Any]]:
    if JOBS_FILE.exists():
        try:
            return json.loads(JOBS_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error(f"Failed to load jobs from {JOBS_FILE}: {e}")
    return {}


def _save_job(job_id: str):
    try:
        with _WRITE_LOCK:
            if job_id not in JOBS or job_id in DELETED_JOBS:
                return
            JOBS[job_id]["updated_at"] = time.time()
            current_jobs = json.loads(JOBS_FILE.read_text(encoding="utf-8")) if JOBS_FILE.exists() else {}
            if not isinstance(current_jobs, dict):
                raise ValueError("Job metadata must be an object")
            current_jobs[job_id] = JOBS[job_id]
            JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=JOBS_FILE.parent,
                                                 prefix=".jobs-", suffix=".tmp", delete=False) as stream:
                    temporary = Path(stream.name)
                    json.dump(current_jobs, stream, ensure_ascii=False, separators=(",", ":"))
                os.replace(temporary, JOBS_FILE)
            finally:
                if temporary:
                    temporary.unlink(missing_ok=True)
            _LAST_CHECKPOINT[job_id] = time.monotonic()
    except Exception as e:
        logger.error(f"Failed to save job {job_id}: {e}")


# In-memory and persistent job tracker
JOBS: Dict[str, Dict[str, Any]] = _load_jobs()


def checkpoint_job(job_id, progress=None, stage=None, force=False, job=None):
    """Keep live counters current without rewriting all job metadata on every cue."""
    if job_id in DELETED_JOBS:
        raise RuntimeError("Video task was removed")
    job = job if job is not None else JOBS[job_id]
    if progress is not None:
        job["progress"] = max(job.get("progress", 0), min(100, progress))
    if stage is not None:
        job["stage"] = stage
    if force or time.monotonic() - _LAST_CHECKPOINT.get(job_id, 0) >= 3:
        _save_job(job_id)


def mark_interrupted_jobs():
    """Persisted worker states do not represent running tasks after a restart."""
    count = 0
    for job_id, job in JOBS.items():
        if job.get("status") in {"queued", "processing", "running", "rendering", "pausing"} and job_id not in RUNNING_JOBS:
            job["interrupted_stage"] = job.get("stage", "Processing")
            if job.get("pause_requested"):
                job.update(status="paused", stage="Paused — resume saved work", error=None)
            else:
                job.update(status="interrupted", stage="Interrupted — resume saved work", error=None)
            _save_job(job_id)
            count += 1
    return count


def cleanup_saved_job_caches():
    """Use both persisted and live job references; fail closed on unreadable metadata."""
    try:
        disk = json.loads(JOBS_FILE.read_text(encoding="utf-8")) if JOBS_FILE.exists() else {}
        jobs = copy.deepcopy({**{f"disk:{key}": job for key, job in disk.items()},
                              **{f"live:{key}": job for key, job in JOBS.items()}})
        return cleanup_processing_cache(JOBS_FILE.parent, jobs)
    except Exception:
        logger.warning("Skipping cache cleanup because job metadata could not be inspected", exc_info=True)
        return CacheCleanupReport(errors=1)


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
        "created_at": time.time(),
        "run_id": uuid.uuid4().hex,
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


@asynccontextmanager
async def video_job_slot(job_id: str):
    """Share the worker limit across new videos, resumes, and visual renders."""
    if (job_id in RUNNING_JOBS or job_id in DELETED_JOBS
            or JOBS.get(job_id, {}).get("pause_requested")):
        yield False
        return
    RUNNING_JOBS.add(job_id)
    JOB_TASKS[job_id] = asyncio.current_task()
    try:
        job = JOBS.get(job_id)
        if job:
            job.update(status="queued", stage="Queued — waiting for a video worker")
            _save_job(job_id)
        async with resource_slot("video_pipeline", config.VIDEO_MAX_CONCURRENT_JOBS):
            if job:
                job.update(status="processing", stage="Starting video task")
                _save_job(job_id)
            yield True
    except asyncio.CancelledError:
        job = JOBS.get(job_id)
        if job:
            job["interrupted_stage"] = job.get("stage")
            if job.get("pause_requested"):
                job.update(status="paused", stage="Paused — resume saved work")
            else:
                job.update(status="interrupted", stage="Interrupted — resume saved work")
            _save_job(job_id)
        raise
    finally:
        RUNNING_JOBS.discard(job_id)
        JOB_TASKS.pop(job_id, None)


async def run_video_dubbing_pipeline(job_id: str, video_source: str, **options):
    """A duplicate background request cannot run the same pipeline twice."""
    async with video_job_slot(job_id) as acquired:
        if acquired:
            return await _run_video_dubbing_pipeline(job_id, video_source, **options)


async def _run_video_dubbing_pipeline(
    job_id: str,
    video_source: str,  # URL or local file path
    is_url: bool = True,
    voice_type: str = "male",
    tone: str = "casual",
    engine_name: str = "auto",
    audio_mode: str = "replace",
    burn_subtitles: bool = False,
    max_duration: Optional[int] = 0,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    subtitle_file_path: Optional[str] = None,
    voice_rate: str = "auto",
    voice_pitch: str = "auto",
    voice_volume: str = "auto",
    render_options: Optional[dict] = None,
    include_english_reference: bool = True,
):
    job = JOBS.get(job_id)
    if not job:
        return

    try:
        effects = VideoRenderOptions(**(render_options or {}))
        if burn_subtitles:
            effects.burn_subtitles = True
        job["render_options"] = effects.render_values()
        job["voice_type"] = voice_type
        job["voice_settings"] = {"voice_rate": voice_rate, "voice_pitch": voice_pitch, "voice_volume": voice_volume}
        # Step 1: Video Ingestion
        mark_step(job, "prepare", "partial")
        job["status"] = "processing"
        checkpoint_job(job_id, 10, "Downloading / Preparing video", force=True)

        existing_video = job.get("video_path")
        if existing_video and Path(existing_video).is_file():
            video_path = existing_video
        elif is_url:
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

            # ⚡ Support Quick Test (e.g. 90s / 1:30 min) on uploaded local files as well
            if max_duration and max_duration > 0:
                from .dubber import get_media_duration
                total_local_dur = await asyncio.to_thread(get_media_duration, str(video_source))
                if total_local_dur > (max_duration + 5):
                    clipped_file = Path(video_source).parent / f"test_{max_duration}s_{Path(video_source).name}"
                    logger.info(f"⚡ 1:30 Quick Test: Clipping local video ({total_local_dur:.1f}s) to first {max_duration}s for fast test...")
                    import subprocess
                    clip_cmd = [
                        "ffmpeg", "-y",
                        "-ss", "00:00:00",
                        "-i", str(video_source),
                        "-t", str(max_duration),
                        "-c", "copy",
                        "-avoid_negative_ts", "make_zero",
                        "-movflags", "+faststart",
                        str(clipped_file),
                    ]
                    try:
                        await asyncio.to_thread(subprocess.run, clip_cmd, check=True,
                                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        if clipped_file.exists() and clipped_file.stat().st_size > 0:
                            video_path = str(clipped_file)
                            logger.info(f"⚡ Clipped test video generated: {clipped_file.name}")
                    except Exception as e:
                        logger.warning(f"Could not stream-copy clip local video: {e}. Processing full file.")

        job["video_path"] = str(video_path)
        mark_step(job, "prepare", "complete")
        if subtitle_file_path:
            job.setdefault("request_options", {})["subtitle_file_path"] = subtitle_file_path
        checkpoint_job(job_id, 25)

        # Step 2: Audio Transcription & Subtitle Reading (Hardsubs / Softsubs)
        mark_step(job, "transcription", "partial")
        checkpoint_job(job_id, 25, "Transcribing speech audio", force=True)

        def _on_speech_progress(done_seconds, total_seconds):
            checkpoint_job(job_id, 25 + int(done_seconds / max(1, total_seconds) * 10),
                           f"Transcribing speech ({int(done_seconds)}/{int(total_seconds)} seconds)")

        def _on_ocr_progress(done, total):
            checkpoint_job(job_id, 35 + int(done / max(1, total) * 13),
                           f"Reading on-screen hardsubs ({done}/{total})")

        if job.get("segments"):
            transcription = {"segments": job["segments"], "language": job.get("detected_language", "auto"),
                             "source": job.get("subtitle_source", "saved_dialogue")}
        else:
            checkpoint_job(job_id, stage="Waiting for speech and subtitle worker", force=True)
            async with resource_slot("transcription", 1):
                checkpoint_job(job_id, stage="Transcribing speech audio", force=True)
                cache_options = {"cache_revision": job["transcription_revision"]} if job.get("transcription_revision") else {}
                transcription = await transcribe_video_audio(
                    video_path, external_subtitle_path=subtitle_file_path,
                    on_ocr_progress=_on_ocr_progress, on_speech_progress=_on_speech_progress,
                    **cache_options,
                )
        segments = transcription["segments"]
        job["detected_language"] = transcription.get("language", "auto")
        job["subtitle_source"] = transcription.get("source", "speech_and_hardsub_ocr")
        checkpoint_job(job_id, 50)

        if not segments:
            raise RuntimeError("No spoken dialogue or speech detected in the video audio track.")

        # Retain partial translations for recovery if the provider cannot finish.
        job["segments"] = segments
        job.setdefault("transcript_segments", source_segments(segments))
        mark_step(job, "transcription", "complete")
        job.setdefault("pipeline_phase", "transcribed")
        _save_job(job_id)

        # Step 3: Translate original dialogue directly; English is a review reference.
        mark_step(job, "translation", "partial")
        job["stage"] = "Original dialogue → Khmer"
        job["translation_flow"] = "speech_subtitles_to_khmer"
        logger.info(f"Translating {len(segments)} segments to Khmer with scene context...")

        from ..engines.dialogue_translator import translate_dialogue_segments
        from ..khmer import batch_polish_segments

        def _on_trans_progress(done_chunks, total_chunks):
            checkpoint_job(job_id, 50 + int(done_chunks / max(1, total_chunks) * 20),
                           f"Original dialogue → Khmer ({done_chunks}/{total_chunks} scenes)")

        try:
            await translate_dialogue_segments(
                segments, job["detected_language"], engine_name=engine_name,
                api_key=api_key, model=model, base_url=base_url, tone=tone,
                on_progress=_on_trans_progress,
                include_english=include_english_reference,
            )
        except MissingTranslationsError as error:
            logger.warning("Keeping completed translations and recovering only failed cues: %s", error)
        batch_polish_segments(segments)

        if job.get("pipeline_phase") not in {"translated", "voices_ready", "rendering", "completed"}:
            # Deduplicate any adjacent cues that have identical translations or source text
            deduped = []
            for s in segments:
                if not deduped:
                    deduped.append(s)
                    continue
                p = deduped[-1]
                t_cur = (s.get("translation") or "").strip()
                t_prev = (p.get("translation") or "").strip()
                s_cur = s.get("text", "").strip()
                s_prev = p.get("text", "").strip()
                is_dup = (
                    (t_cur and t_prev and t_cur == t_prev) or
                    (s_cur and s_prev and s_cur == s_prev)
                ) and bool(t_cur and t_prev) and (s.get("start", 0) <= p.get("end", 0) + 1.2)
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
        job["pipeline_phase"] = "translated"
        mark_step(job, "translation", "partial" if missing_translations(segments) else "complete")
        checkpoint_job(job_id, 70, force=True)

        # Step 4: Khmer Speech Synthesis (TTS)
        mark_step(job, "voices", "partial")
        job["stage"] = "Synthesizing Khmer voiceover"
        logger.info(f"Generating Khmer speech ({voice_type}) for {len(segments)} segments...")
        audio_dir = Path(video_path).parent / f"voice_{job_id}"

        from .recovery import prepare_dialogue_audio

        def audio_progress(stage, done, total):
            checkpoint_job(job_id, 70 + int(done / max(1, total) * 15),
                           f"{stage} ({done}/{total} voices ready)", force=done == total)

        recovery = await prepare_dialogue_audio(
            segments, video_path, audio_dir, source_lang=job["detected_language"],
            engine_name=engine_name, api_key=api_key, model=model, base_url=base_url, tone=tone,
            voice_type=voice_type, rate=voice_rate, pitch=voice_pitch, volume=voice_volume,
            on_progress=audio_progress, synthesize=synthesize_segments,
        )
        job.update(recovery)
        if not missing_translations(segments):
            mark_step(job, "translation", "complete")
        mark_step(job, "voices", "complete")
        mark_step(job, "render", "partial")
        job["pipeline_phase"] = "rendering"
        checkpoint_job(job_id, 85, "Mixing audio and assembling video", force=True)

        # Step 5: Audio Mixing & Video Dubbing
        job["stage"] = "Mixing audio and assembling video"
        logger.info("Assembling final dubbed video...")
        dub_result = await assemble_dubbed_video(
            video_path=video_path,
            segments=segments,
            output_filename=job.get("output_filename") or f"dubbed_{job_id}.mp4",
            audio_mode=audio_mode,
            burn_subtitles=burn_subtitles,
            render_options=effects.render_values(),
        )

        job["progress"] = 100
        job["status"] = "completed"
        job["pipeline_phase"] = "completed"
        job["error"] = None
        job["stage"] = "Complete" if not recovery["fallback_count"] else f"Complete — {recovery['fallback_count']} cues need review"
        dub_result.update(recovery)
        job["result"] = dub_result
        mark_step(job, "render", "complete")
        _save_job(job_id)
        logger.info(f"Job {job_id} successfully completed!")

    except Exception as e:
        logger.error(f"Pipeline error for job {job_id}: {e}", exc_info=True)
        job["status"] = "failed"
        job["stage"] = "Failed"
        job["error"] = str(e)
        _save_job(job_id)
    finally:
        await asyncio.to_thread(cleanup_saved_job_caches)
