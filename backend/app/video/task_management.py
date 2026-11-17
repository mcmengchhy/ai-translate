"""Remove tasks durably and reset processing data without deleting source media."""

import asyncio
import json
import shutil
import copy
import uuid
from dataclasses import asdict

from . import batch, pipeline
from .cache import purge_processing_cache
from .job_identity import atomic_json_write, clear_media_hash_cache
from .checkpoints import ACTIVE_STATUSES, job_steps, source_segments, step_states
from .resources import resource_slot


async def pause_task(job_id):
    async with resource_slot(f"job_control:{job_id}", 1):
        if pipeline.RESETTING:
            raise RuntimeError("Task caches are being cleared. Try again shortly.")
        job = pipeline.get_job_status(job_id)
        if not job:
            raise KeyError(job_id)
        if job.get("status") == "paused":
            return {"job_id": job_id, "status": "paused"}
        if job.get("status") not in ACTIVE_STATUSES:
            raise RuntimeError("Only a running or queued task can be paused")
        job["step_states"] = step_states(job)
        job["interrupted_stage"] = job.get("stage")
        job.update(pause_requested=True, status="pausing", stage="Pausing — saving completed work")
        pipeline._save_job(job_id)
        task = pipeline.JOB_TASKS.get(job_id)
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if pipeline.get_job_status(job_id) is not job:
            raise KeyError(job_id)
        job.update(status="paused", stage="Paused — resume saved work", error=None)
        pipeline._save_job(job_id)
        return {"job_id": job_id, "status": "paused"}


async def clear_job_step(job_id, step):
    """Invalidate one checkpoint only after all checkpoints depending on it."""
    async with resource_slot(f"job_control:{job_id}", 1):
        if pipeline.RESETTING:
            raise RuntimeError("Task caches are being cleared. Try again shortly.")
        job = pipeline.get_job_status(job_id)
        if not job:
            raise KeyError(job_id)
        if job.get("status") in ACTIVE_STATUSES or job_id in pipeline.RUNNING_JOBS:
            raise RuntimeError("Pause this task before clearing a saved step")
        steps = job_steps(job)
        requested = next((item for item in steps if item["id"] == step), None)
        if requested is None:
            raise ValueError("Unknown processing step")
        latest = next((item for item in reversed(steps) if item["state"] != "pending"), None)
        if latest is None or requested["state"] == "pending":
            raise RuntimeError("This step has no saved work to clear")
        if latest["id"] != step:
            raise RuntimeError(f"Clear step {latest['number']} ({latest['label']}) first")
        states = step_states(job)
        states[step] = "pending"
        job["step_states"] = states
        job.update(result=None, error=None, pause_requested=True, status="paused",
                   stage=f"{requested['label']} cleared — resume to regenerate",
                   run_id=uuid.uuid4().hex)
        # Existing exports remain available on disk; the next render gets a new filename.
        job["output_filename"] = f"dubbed_{job_id}_restart_{job['run_id'][:8]}.mp4"
        for key in ("needs_rerender", "interrupted_stage", "source_only", "source_note"):
            job.pop(key, None)
        if step == "render":
            job.update(progress=85, pipeline_phase="voices_ready")
        elif step == "voices":
            for seg in job.get("segments", []):
                for key in ("audio_file", "audio_source", "prosody", "tts_error", "voice_retry_round",
                            "generation_warning", "review_required", "fallback_subtitle"):
                    seg.pop(key, None)
                seg["_dirty"] = True
            for key in ("fallback_count", "fallback_cue_ids", "generation_warnings"):
                job.pop(key, None)
            job.update(progress=70, pipeline_phase="translated")
        elif step == "translation":
            job["segments"] = copy.deepcopy(job.get("transcript_segments") or source_segments(job.get("segments", [])))
            job.pop("translation_flow", None)
            job.update(progress=50, pipeline_phase="transcribed")
        elif step == "transcription":
            for key in ("transcript_segments", "detected_language", "subtitle_source"):
                job.pop(key, None)
            job.update(segments=[], transcription_revision=uuid.uuid4().hex, progress=25, pipeline_phase="prepared")
        elif step == "prepare":
            job.pop("video_path", None)
            job.pop("pipeline_phase", None)
            job["progress"] = 0
        pipeline._save_job(job_id)
        return {"job_id": job_id, **{key: job.get(key) for key in ("status", "stage", "progress", "run_id")},
                "steps": job_steps(job)}


async def remove_task(job_id):
    async with resource_slot(f"job_control:{job_id}", 1):
        return await _remove_task(job_id)


async def _remove_task(job_id):
    if not pipeline.get_job_status(job_id):
        raise KeyError(job_id)
    # Stop batch recovery before discarding a clip's metadata.
    batch.detach_job(job_id)
    pipeline.DELETED_JOBS.add(job_id)
    task = pipeline.JOB_TASKS.get(job_id)
    if task and task is not asyncio.current_task() and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    with pipeline._WRITE_LOCK:
        saved = json.loads(pipeline.JOBS_FILE.read_text(encoding="utf-8")) if pipeline.JOBS_FILE.exists() else {}
        if not isinstance(saved, dict):
            raise ValueError("Invalid task metadata")
        saved.pop(job_id, None)
        atomic_json_write(pipeline.JOBS_FILE, saved)
        pipeline.JOBS.pop(job_id, None)
        pipeline._LAST_CHECKPOINT.pop(job_id, None)
    return {"job_id": job_id, "status": "removed"}


async def clear_tasks_and_cache():
    if pipeline.RESETTING or pipeline.RUNNING_JOBS or any(not task.done() for task in batch.TASKS.values()) or any(
        job.get("status") in ACTIVE_STATUSES for job in pipeline.JOBS.values()
    ):
        raise RuntimeError("Remove running and queued tasks before clearing all task history and caches.")
    pipeline.RESETTING = True
    try:
        report = await asyncio.to_thread(purge_processing_cache, pipeline.JOBS_FILE.parent)
        if batch.BATCH_DIR.is_symlink():
            raise ValueError("Invalid batch directory")
        if batch.BATCH_DIR.exists():
            await asyncio.to_thread(shutil.rmtree, batch.BATCH_DIR)
        with pipeline._WRITE_LOCK:
            atomic_json_write(pipeline.JOBS_FILE, {})
            pipeline.DELETED_JOBS.update(pipeline.JOBS)
            pipeline.JOBS.clear()
            pipeline._LAST_CHECKPOINT.clear()
        batch.TASKS.clear()
        clear_media_hash_cache()
        return {"status": "cleared", "cache": asdict(report)}
    finally:
        pipeline.RESETTING = False
