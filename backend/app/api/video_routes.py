"""API endpoints for Video Translation and Khmer Voice Dubbing."""

import asyncio
import logging
import shutil
import uuid
import math
import io
import hashlib
import threading
from pathlib import Path
from typing import Optional, List, Dict, Any, Literal
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, BackgroundTasks, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from .models import EngineOptions

from ..video.pipeline import create_job, get_job_status, run_video_dubbing_pipeline, _save_job, cleanup_saved_job_caches
from ..video import pipeline
from ..video.job_identity import media_hash, remember_hash, processing_key
from ..video.cache import cache_in_use
from ..video.tts import synthesize_khmer_clip, synthesize_segments
from ..video.dubber import DUBBED_DIR, assemble_dubbed_video, schedule_non_overlapping_clips, get_media_duration
from ..khmer import normalize_khmer_unicode
from ..video.segments import missing_translations, require_translations, MissingTranslationsError
from ..video.render_options import VideoRenderOptions, LOGO_DIR, resolve_logo
from ..video.checkpoints import ACTIVE_STATUSES, job_steps, mark_step, step_states
from ..core import config

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/video")

ROOT_PROJECT_DIR = Path(__file__).resolve().parent.parent.parent.parent
STORAGE_UPLOADS = ROOT_PROJECT_DIR / "storage" / "uploads"
STORAGE_UPLOADS.mkdir(parents=True, exist_ok=True)
_SUBMISSION_LOCK = threading.RLock()


async def _store_upload(file: UploadFile, directory: Path) -> Path:
    """Store each upload in its own directory and reject client-supplied paths."""
    name = file.filename or ""
    if not name or name in (".", "..") or Path(name).name != name or "\\" in name or "\x00" in name:
        raise HTTPException(status_code=400, detail="Upload filename must be a plain filename")
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / name

    def copy():
        digest = hashlib.sha256()
        with destination.open("xb") as buffer:
            for block in iter(lambda: file.file.read(1024 * 1024), b""):
                digest.update(block)
                buffer.write(block)
        file.content_sha256 = remember_hash(destination, digest.hexdigest())

    await asyncio.to_thread(copy)
    return destination


async def _index_matching_legacy_uploads(digest, size):
    """Hash only same-sized legacy sources; filenames alone are not identities."""
    candidates = [(key, job.get("input")) for key, job in list(pipeline.JOBS.items())
                  if not job.get("input_sha256") and job.get("input")]
    def inspect():
        matches = []
        for key, source in candidates:
            try:
                path = Path(source)
                if path.is_file() and path.stat().st_size == size and media_hash(path) == digest:
                    matches.append(key)
            except OSError:
                continue
        return matches
    for key in await asyncio.to_thread(inspect):
        if key in pipeline.JOBS:
            pipeline.JOBS[key]["input_sha256"] = digest
            _save_job(key)


def _register_submission(source, identity, options, force_new=False, input_sha256=None):
    """The duplicate check and queue reservation are one non-awaiting operation."""
    if pipeline.RESETTING:
        raise HTTPException(status_code=409, detail="Task caches are being cleared. Try again shortly.")
    signature = processing_key(options)
    with _SUBMISSION_LOCK:
        if not force_new:
            matches = [(key, job) for key, job in reversed(list(pipeline.JOBS.items()))
                       if job.get("source_identity") == identity
                       or (input_sha256 and job.get("input_sha256") == input_sha256)
                       or (identity.startswith("url:") and job.get("input", "").strip() == source.strip())]
            if matches:
                key, job = next(((key, job) for key, job in matches
                                 if job.get("status") in {"queued", "processing"}), matches[0])
                raise HTTPException(status_code=409, detail={
                    "message": "This video already has a saved job. Continue it or explicitly start a new run.",
                    "job_id": key, "status": job.get("status"), "progress": job.get("progress", 0),
                    "stage": job.get("stage"), "has_dialogue": bool(job.get("segments")),
                    "options_match": job.get("submission_options_key") == signature,
                })
        job_id = create_job(source)
        job = pipeline.JOBS.get(job_id)
        if job is not None:
            job.update(source_identity=identity, submission_options_key=signature,
                       request_options={key: value for key, value in options.items() if key != "api_key"})
            if identity.startswith("upload:"):
                job["title"] = Path(source).stem
            if input_sha256:
                job["input_sha256"] = input_sha256
            _save_job(job_id)
        return job_id


def _duplicate_submission_response(detail):
    """Older pages understand job_id; updated pages can offer the duplicate choices."""
    return {"job_id": detail["job_id"], "status": detail["status"], "reused": True,
            "existing_job": detail, "message": detail["message"]}


def _apply_segment_updates(job: dict, incoming: List[Dict[str, Any]]):
    """Treat edited times as new anchors, retaining anchors for unchanged cues."""
    previous = {seg.get("id"): seg for seg in job.get("segments", [])}
    updated = []
    for original in incoming:
        seg = dict(original)
        try:
            start, end = float(seg["start"]), float(seg["end"])
            if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise HTTPException(status_code=422, detail="Cue times must be finite, non-negative, and end after start")
        old = previous.get(seg.get("id"))
        if old and seg.get("translation") and seg.get("translation") != old.get("translation"):
            seg.pop("translation_error", None)
            seg.pop("translation_warning", None)
            seg.update(_dirty=True, audio_file=None)
            job["needs_rerender"] = True
        for field, value in (("start", start), ("end", end)):
            seg[field] = value
            if old is None or value != old.get(field):
                seg[f"target_{field}"] = value
            else:
                seg[f"target_{field}"] = old.get(f"target_{field}", value)
        updated.append(seg)
    job["segments"] = sorted(updated, key=lambda seg: seg["target_start"])


class VideoDubUrlRequest(EngineOptions, VideoRenderOptions):
    force_new: bool = False
    url: str = Field(..., description="Bilibili, YouTube, or direct video URL")
    voice_type: str = Field(default="male", description="'male' (Piseth) or 'female' (Sreymom)")
    tone: str = Field(default="casual", description="Linguistic tone: standard, casual, formal, polite")
    audio_mode: str = Field(default="replace", description="'replace' (100% pure voiceover, identical to preview) or 'smart_sidechain' / 'ducking'")
    max_duration: Optional[int] = Field(default=0, description="Duration in seconds to dub (e.g. 90 for fast test, 300 for 5m, 0 for full video)")
    subtitle_file_path: Optional[str] = Field(default=None, description="Optional path to custom subtitle file")
    voice_rate: str = Field(default="auto", description="Voice speed adjustment e.g. '+0%', '+10%', '-10%'")
    voice_pitch: str = Field(default="auto", description="Voice pitch adjustment e.g. '+0Hz', '+3Hz', '-3Hz'")
    voice_volume: str = Field(default="auto", description="Voice volume adjustment e.g. '+0%', '+10%'")


class VoicePreviewRequest(BaseModel):
    text: str = Field(default="សួស្តីបងប្អូនទាំងអស់គ្នា សូមស្វាគមន៍មកកាន់វីដេអូបកប្រែជាភាសាខ្មែរ")
    voice_type: str = Field(default="male")
    rate: str = Field(default="auto", description="Edge-TTS rate e.g. '+10%' or 'auto'")
    pitch: str = Field(default="auto", description="Edge-TTS pitch e.g. '+2Hz' or 'auto'")
    volume: str = Field(default="auto", description="Edge-TTS volume e.g. '+5%' or 'auto'")
    enhance: bool = Field(default=True, description="Apply EQ + loudnorm post-processing")


class SegmentUpdateRequest(BaseModel):
    segments: List[Dict[str, Any]]


class SegmentPreviewRequest(BaseModel):
    segment_id: int
    text: str
    voice_type: str = "male"
    rate: str = "auto"
    pitch: str = "auto"
    volume: str = "auto"
    enhance: bool = True


class PolishDialogueRequest(EngineOptions):
    tone: str = "casual"
    segments: Optional[List[Dict[str, Any]]] = None


class RepairTranslationsRequest(PolishDialogueRequest):
    pass


class JobRerenderRequest(EngineOptions, VideoRenderOptions):
    retranslate: bool = False
    segments: Optional[List[Dict[str, Any]]] = None
    audio_mode: str = "replace"  # "replace" (100% pure voiceover), "mute", "duck_subtle", "ducking", "smart_sidechain"
    bg_volume: Optional[float] = 0.0
    voice_rate: str = "auto"
    voice_pitch: str = "auto"
    voice_volume: str = "auto"


class BatchDubRequest(EngineOptions, VideoRenderOptions):
    filenames: List[str] = Field(min_length=1, max_length=8)
    voice_type: Literal["male", "female"] = "male"
    tone: str = "casual"
    audio_mode: str = "replace"
    voice_rate: str = "auto"
    voice_pitch: str = "auto"
    voice_volume: str = "auto"


@router.post("/batch")
async def start_split_batch(req: BatchDubRequest, background_tasks: BackgroundTasks):
    from ..video.batch import create_batch, start_batch, batch_status
    options = dict(voice_type=req.voice_type, tone=req.tone, audio_mode=req.audio_mode,
                   engine_name=req.engine, model=req.model, base_url=req.base_url,
                   voice_rate=req.voice_rate, voice_pitch=req.voice_pitch, voice_volume=req.voice_volume,
                   burn_subtitles=req.burn_subtitles, render_options=req.render_values(), max_duration=0,
                   include_english_reference=False)
    try:
        batch = await create_batch(req.filenames, options)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))
    if batch.get("status") in {"queued", "processing", "interrupted", "packaging"}:
        background_tasks.add_task(start_batch, batch["id"], req.api_key)
    return batch_status(batch["id"])


@router.get("/batch/{batch_id}")
async def get_split_batch(batch_id: str):
    from ..video.batch import batch_status
    try:
        return batch_status(batch_id)
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail="Batch not found")


@router.get("/batch/{batch_id}/download")
async def download_split_batch(batch_id: str):
    from ..video.batch import read_batch, EXPORT_DIR
    try:
        batch = read_batch(batch_id)
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail="Batch not found")
    if batch.get("status") not in {"completed", "completed_with_errors"} or not batch.get("archive_path"):
        raise HTTPException(status_code=409, detail="The batch download is still being prepared")
    archive = Path(batch["archive_path"]).resolve()
    if not archive.is_relative_to(EXPORT_DIR.resolve()) or not archive.is_file():
        raise HTTPException(status_code=404, detail="Batch download not found")
    return FileResponse(archive, filename=archive.name, media_type="application/zip")


@router.post("/batch/{batch_id}/resume")
async def resume_split_batch(batch_id: str, background_tasks: BackgroundTasks):
    from ..video.batch import read_batch, start_batch, batch_status
    try:
        read_batch(batch_id)
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail="Batch not found")
    background_tasks.add_task(start_batch, batch_id)
    return batch_status(batch_id)


@router.post("/dub-url")
async def start_dubbing_from_url(req: VideoDubUrlRequest, background_tasks: BackgroundTasks):
    if not req.url.strip():
        raise HTTPException(status_code=400, detail="Video URL is required")

    worker_options = dict(is_url=True, voice_type=req.voice_type, tone=req.tone,
                          audio_mode=req.audio_mode, burn_subtitles=req.burn_subtitles,
                          render_options=req.render_values(), max_duration=req.max_duration,
                          engine_name=req.engine, model=req.model, base_url=req.base_url,
                          subtitle_file_path=req.subtitle_file_path, voice_rate=req.voice_rate,
                          voice_pitch=req.voice_pitch, voice_volume=req.voice_volume)
    try:
        job_id = _register_submission(req.url.strip(), "url:" + req.url.strip(), worker_options, req.force_new)
    except HTTPException as error:
        if error.status_code == 409 and isinstance(error.detail, dict):
            return _duplicate_submission_response(error.detail)
        raise

    background_tasks.add_task(
        run_video_dubbing_pipeline,
        job_id=job_id,
        video_source=req.url,
        is_url=True,
        voice_type=req.voice_type,
        tone=req.tone,
        audio_mode=req.audio_mode,
        burn_subtitles=req.burn_subtitles,
        render_options=req.render_values(),
        max_duration=req.max_duration,
        api_key=req.api_key,
        engine_name=req.engine,
        model=req.model,
        base_url=req.base_url,
        subtitle_file_path=req.subtitle_file_path,
        voice_rate=req.voice_rate,
        voice_pitch=req.voice_pitch,
        voice_volume=req.voice_volume,
    )

    return {"job_id": job_id, "status": "queued"}


@router.post("/dub-upload")
async def start_dubbing_from_file(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    subtitle_file: Optional[UploadFile] = File(None),
    voice_type: str = Form("male"),
    tone: str = Form("casual"),
    audio_mode: str = Form("replace"),
    burn_subtitles: bool = Form(False),
    add_logo: bool = Form(False),
    logo_id: str = Form("cinekhmer"),
    blur_chinese_subtitles: bool = Form(False),
    blur_x: float = Form(0.10),
    blur_y: float = Form(0.80),
    blur_width: float = Form(0.80),
    blur_height: float = Form(0.14),
    max_duration: Optional[int] = Form(0),
    api_key: Optional[str] = Form(None),
    engine: Literal["auto", "openai", "free"] = Form("auto"),
    model: Optional[str] = Form(None),
    base_url: Optional[str] = Form(None),
    voice_rate: str = Form("auto"),
    voice_pitch: str = Form("auto"),
    voice_volume: str = Form("auto"),
    force_new: bool = Form(False),
):
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file uploaded")

    from pydantic import ValidationError
    try:
        options = EngineOptions(engine=engine, api_key=api_key, model=model, base_url=base_url)
        effects = VideoRenderOptions(add_logo=add_logo, logo_id=logo_id, burn_subtitles=burn_subtitles,
                                     blur_chinese_subtitles=blur_chinese_subtitles, blur_x=blur_x,
                                     blur_y=blur_y, blur_width=blur_width, blur_height=blur_height)
    except ValidationError:
        raise HTTPException(status_code=422, detail="Invalid translation or video effect settings")
    upload_dir = STORAGE_UPLOADS / uuid.uuid4().hex
    dest_file = await _store_upload(file, upload_dir)

    sub_path_str = None
    if subtitle_file and subtitle_file.filename:
        sub_dest = await _store_upload(subtitle_file, upload_dir / "subtitles")
        sub_path_str = str(sub_dest)

    worker_options = dict(is_url=False, voice_type=voice_type, tone=tone,
                          audio_mode=audio_mode, burn_subtitles=burn_subtitles,
                          render_options=effects.render_values(), max_duration=max_duration,
                          engine_name=options.engine, model=options.model, base_url=options.base_url,
                          subtitle_file_path=sub_path_str, voice_rate=voice_rate,
                          voice_pitch=voice_pitch, voice_volume=voice_volume,
                          subtitle_sha256=getattr(subtitle_file, "content_sha256", ""))
    digest = file.content_sha256
    if not force_new:
        await _index_matching_legacy_uploads(digest, dest_file.stat().st_size)
    try:
        job_id = _register_submission(str(dest_file), "upload:" + digest, worker_options, force_new, digest)
    except HTTPException as error:
        # These files belong only to this fresh request, never to the saved job.
        if sub_path_str:
            Path(sub_path_str).unlink(missing_ok=True)
            Path(sub_path_str).parent.rmdir()
        dest_file.unlink(missing_ok=True)
        upload_dir.rmdir()
        if error.status_code == 409 and isinstance(error.detail, dict):
            return _duplicate_submission_response(error.detail)
        raise

    background_tasks.add_task(
        run_video_dubbing_pipeline,
        job_id=job_id,
        video_source=str(dest_file),
        is_url=False,
        voice_type=voice_type,
        tone=tone,
        audio_mode=audio_mode,
        burn_subtitles=burn_subtitles,
        render_options=effects.render_values(),
        max_duration=max_duration,
        api_key=api_key,
        engine_name=options.engine,
        model=options.model,
        base_url=options.base_url,
        subtitle_file_path=sub_path_str,
        voice_rate=voice_rate,
        voice_pitch=voice_pitch,
        voice_volume=voice_volume,
    )

    return {"job_id": job_id, "status": "queued", "filename": file.filename}


@router.get("/jobs")
async def list_video_jobs(recent_limit: int = Query(30, ge=0, le=100)):
    """All active videos plus recent saved jobs, without large dialogue or credentials."""
    active_statuses = ACTIVE_STATUSES
    jobs = sorted(list(pipeline.JOBS.values()), key=lambda job: job.get("created_at", 0), reverse=True)
    active = [job for job in jobs if job.get("status") in active_statuses]
    paused = [job for job in jobs if job.get("status") == "paused"]
    recent = [job for job in jobs if job.get("status") not in active_statuses | {"paused"}][:recent_limit]
    summaries = []
    for job in active + paused + recent:
        summary = {field: job.get(field) for field in (
            "id", "title", "status", "progress", "stage", "error", "created_at",
        )}
        summary["steps"] = job_steps(job)
        result = job.get("result") or {}
        if job.get("status") == "completed":
            for field, path_field in (("download_url", "dubbed_video_path"), ("srt_url", "srt_path")):
                path = result.get(path_field)
                if path and Path(path).is_file():
                    summary[field] = f"/static/dubbed/{Path(path).name}"
        summaries.append(summary)
    return {"jobs": summaries, "active_count": len(active),
            "processing_count": sum(job.get("status") != "queued" for job in active),
            "queued_count": sum(job.get("status") == "queued" for job in active),
            "max_concurrent_jobs": config.VIDEO_MAX_CONCURRENT_JOBS}


@router.get("/status/{job_id}")
async def check_pipeline_status(job_id: str, summary: bool = False):
    job = get_job_status(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if summary:
        fields = ("id", "status", "progress", "stage", "title", "run_id", "error", "fallback_count", "pipeline_phase")
        return {**{field: job.get(field) for field in fields}, "segments_count": len(job.get("segments", [])),
                "step_states": step_states(job)}
    return {**job, "steps": job_steps(job)}


@router.post("/job/{job_id}/pause")
async def pause_video_task(job_id: str):
    from ..video.task_management import pause_task
    try:
        return await pause_task(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Task not found")
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error))


@router.delete("/job/{job_id}/steps/{step}")
async def clear_video_step(job_id: str, step: Literal["prepare", "transcription", "translation", "voices", "render"]):
    from ..video.task_management import clear_job_step
    try:
        return await clear_job_step(job_id, step)
    except KeyError:
        raise HTTPException(status_code=404, detail="Task not found")
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error))


@router.delete("/job/{job_id}")
async def remove_video_task(job_id: str):
    from ..video.task_management import remove_task
    try:
        return await remove_task(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Task not found")


@router.delete("/jobs")
async def clear_video_tasks_and_cache():
    from ..video.task_management import clear_tasks_and_cache
    try:
        return await clear_tasks_and_cache()
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error))


@router.post("/preview-voice")
async def preview_khmer_voice(req: VoicePreviewRequest):
    out_file = DUBBED_DIR / f"preview_{req.voice_type}.mp3"
    try:
        await synthesize_khmer_clip(
            req.text, str(out_file),
            voice_type=req.voice_type,
            rate=req.rate,
            pitch=req.pitch,
            volume=req.volume,
            enhance=req.enhance,
        )
        return {"audio_url": f"/static/dubbed/{out_file.name}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Voice synthesis error: {e}")


# =========================================================================
# CAPCUT-STYLE STUDIO ENDPOINTS: Segment Inspector, Audio Preview, Overlap Fix, Re-render
# =========================================================================

@router.get("/job/{job_id}/segments")
async def get_job_segments(job_id: str):
    job = get_job_status(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {
        "job_id": job_id,
        "title": job.get("title", "Video Dubbing"),
        "segments": job.get("segments", []),
        "result": job.get("result"),
    }


@router.put("/job/{job_id}/segments")
async def update_job_segments(job_id: str, req: SegmentUpdateRequest):
    job = get_job_status(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    _apply_segment_updates(job, req.segments)
    _save_job(job_id)
    return {"status": "ok", "count": len(req.segments)}


@router.post("/job/{job_id}/preview-segment")
async def preview_single_segment_audio(job_id: str, req: SegmentPreviewRequest):
    """Synthesize a single dialogue cue with Edge-TTS and return its audio stream."""
    job = get_job_status(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    clean_text = normalize_khmer_unicode(req.text.strip())
    if not clean_text:
        raise HTTPException(status_code=400, detail="Khmer text cannot be empty")

    preview_name = f"preview_{job_id}_seg_{req.segment_id}_{req.voice_type}.mp3"
    out_file = DUBBED_DIR / preview_name

    try:
        await synthesize_khmer_clip(
            clean_text, str(out_file),
            voice_type=req.voice_type,
            rate=req.rate,
            pitch=req.pitch,
            volume=req.volume,
            enhance=req.enhance,
        )
        dur = get_media_duration(str(out_file))

        # Also find and update the segment's audio_file in the job
        for seg in job.get("segments", []):
            if seg.get("id") == req.segment_id:
                seg["audio_file"] = str(out_file)
                seg["voice_type"] = req.voice_type
                seg["translation"] = clean_text
                break
        _save_job(job_id)

        return {
            "status": "ok",
            "audio_url": f"/static/dubbed/{preview_name}",
            "duration": dur,
        }
    except Exception as e:
        logger.error(f"Error previewing segment: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/job/{job_id}/auto-fix-overlaps")
async def auto_fix_job_overlaps(job_id: str):
    """Automatically adjusts timestamps so no two speech cues overlap in time."""
    job = get_job_status(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    segments = job.get("segments", [])
    if not segments:
        return {"status": "ok", "segments": []}

    temp_dir = ROOT_PROJECT_DIR / "storage" / "dubbed" / f"temp_{job_id}"
    with cache_in_use(temp_dir):
        await asyncio.to_thread(schedule_non_overlapping_clips, segments, temp_dir=temp_dir, max_speed=1.45)
    _save_job(job_id)

    return {
        "status": "ok",
        "message": "All speech cue overlaps automatically resolved!",
        "segments": segments,
    }


@router.post("/job/{job_id}/ai-polish-dialogue")
async def ai_polish_job_dialogue(job_id: str, req: PolishDialogueRequest):
    """Translate saved originals directly, retaining completed cues on failure."""
    job = get_job_status(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if pipeline.RESETTING or job.get("status") in ACTIVE_STATUSES or job_id in pipeline.RUNNING_JOBS:
        raise HTTPException(status_code=409, detail="This job is already running or task caches are being cleared")
    previous_status, previous_progress = job.get('status', 'completed'), job.get('progress', 0)
    was_paused = job.pop('pause_requested', False)
    async with pipeline.video_job_slot(job_id) as acquired:
        if not acquired:
            raise HTTPException(status_code=409, detail="This job is already running")
        try:
            result = await _polish_saved_dialogue(job_id, job, req)
        except asyncio.CancelledError:
            # The worker context persists paused/interrupted state and saved cues.
            raise
        except Exception:
            job.update(status='failed', stage='Polishing failed — saved dialogue retained')
            _save_job(job_id)
            raise
        else:
            job.update(status=previous_status, progress=previous_progress,
                       stage='Khmer polished — re-render to update voice and video')
            if was_paused:
                job['pause_requested'] = True
            _save_job(job_id)
            return result


async def _polish_saved_dialogue(job_id, job, req):

    if req.segments is not None:
        _apply_segment_updates(job, req.segments)
    segments = job.get("segments", [])
    if not segments:
        return {"status": "ok", "segments": []}

    from ..engines.dialogue_translator import translate_dialogue_segments, FLOW
    from ..khmer import batch_polish_segments

    source_lang = job.get("detected_language", "zh")
    error = None
    job.update(progress=50, stage='Polishing Khmer from original dialogue', run_id=uuid.uuid4().hex)
    mark_step(job, 'translation', 'partial')
    _save_job(job_id)

    def progress(done, total):
        pipeline.checkpoint_job(job_id, 50 + int(done / max(1, total) * 20),
                                f'Polishing Khmer ({done}/{total} scenes)', job=job)

    try:
        await translate_dialogue_segments(
            segments, source_lang, engine_name=req.engine, api_key=req.api_key,
            model=req.model, base_url=req.base_url, tone=req.tone, force=True, on_progress=progress,
        )
        batch_polish_segments(segments)
    except MissingTranslationsError as failure:
        error = str(failure)
    finally:
        job["translation_flow"] = FLOW
        job["needs_rerender"] = True
        mark_step(job, 'translation', 'partial' if missing_translations(segments) else 'complete')
        _save_job(job_id)

    return {
        "status": "partial" if error else "ok",
        "message": error or "Dialogue translated directly from the source. Review the Khmer before re-rendering.",
        "segments": segments,
    }


@router.post("/job/{job_id}/rerender")
async def rerender_dubbed_video(job_id: str, req: JobRerenderRequest):
    """Reserve a worker for direct API calls as well as background resumes."""
    job = get_job_status(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.get("status") in ACTIVE_STATUSES or job_id in pipeline.RUNNING_JOBS:
        raise HTTPException(status_code=409, detail="This job is already running")
    previous_state = {field: job.get(field) for field in ("status", "stage", "progress", "run_id", "error")}
    if job.get("status") == "completed":
        job.update(progress=0, run_id=uuid.uuid4().hex)
    try:
        async with pipeline.video_job_slot(job_id) as acquired:
            if not acquired:
                raise HTTPException(status_code=409, detail="This job is already running")
            return await _rerender_saved_video(job_id, req)
    except asyncio.CancelledError:
        if job.get("pause_requested"):
            job.update(status="paused", stage="Paused — resume saved work")
        else:
            job.update(status="interrupted", stage="Interrupted — resume saved work")
        _save_job(job_id)
        raise
    except Exception as error:
        if not isinstance(error, HTTPException) or error.status_code >= 500:
            job.update(status="failed", stage="Failed", error=str(getattr(error, "detail", error)))
            _save_job(job_id)
        else:
            job.update(previous_state)
            _save_job(job_id)
        raise


async def _rerender_saved_video(job_id: str, req: JobRerenderRequest):
    """Render edited dialogue, audio balance, branding, blur, and Khmer captions."""
    job = get_job_status(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    video_path = job.get("video_path")
    if not video_path or not Path(video_path).exists():
        # Fallback to storage/downloads
        candidate = ROOT_PROJECT_DIR / "storage" / "downloads" / f"{job.get('title', '')}.mp4"
        if candidate.exists():
            video_path = str(candidate)
        else:
            # Check BV1Foa16yEAh.mp4
            candidate2 = ROOT_PROJECT_DIR / "storage" / "downloads" / "BV1Foa16yEAh.mp4"
            if candidate2.exists():
                video_path = str(candidate2)
            else:
                raise HTTPException(status_code=400, detail="Original source video not found on disk to re-render")

    if req.segments is not None:
        _apply_segment_updates(job, req.segments)

    if job.get("status") == "completed":
        job.update(progress=0, run_id=uuid.uuid4().hex)
    segments = job["segments"]
    pending = segments if req.retranslate else missing_translations(segments)
    if pending:
        from ..engines.dialogue_translator import translate_dialogue_segments, FLOW
        previous = {seg["id"]: seg.get("translation") for seg in segments}
        job.update(status="processing", stage="Original dialogue → Khmer", progress=max(job.get("progress", 0), 50), error=None)
        mark_step(job, "translation", "partial")
        _save_job(job_id)

        def translation_progress(done, total):
            pipeline.checkpoint_job(job_id, 50 + int(done / max(1, total) * 20),
                                    f"Original dialogue → Khmer ({done}/{total} scenes)", job=job)

        try:
            await translate_dialogue_segments(
                pending, job.get("detected_language", "auto"), engine_name=req.engine,
                api_key=req.api_key, model=req.model, base_url=req.base_url,
                on_progress=translation_progress, force=req.retranslate,
            )
            job["translation_flow"] = FLOW
            mark_step(job, "translation", "complete")
        except MissingTranslationsError as error:
            logger.warning("Retrying failed cues without restarting successful dialogue: %s", error)
        except Exception as error:
            job.update(status="failed", stage="Translation failed", error=str(error))
            raise HTTPException(status_code=502, detail=str(error))
        finally:
            for seg in segments:
                if seg.get("translation") != previous.get(seg["id"]):
                    seg.update(_dirty=True, audio_file=None)
            _save_job(job_id)
    try:
        return await _finish_job_render(job_id, job, video_path, req)
    except Exception as error:
        job.update(status="failed", stage="Failed", error=str(error))
        _save_job(job_id)
        raise HTTPException(status_code=502, detail=f"Video render failed: {error}")


async def _finish_job_render(job_id, job, video_path, req):
    """Resume voice synthesis in parallel, retaining matching cached voice clips."""
    segments = job["segments"]
    from ..video.recovery import prepare_dialogue_audio
    job.update(status="processing", stage="Preparing voice clips and recovering failed cues",
               progress=max(job.get("progress", 0), 70), error=None)
    mark_step(job, "voices", "partial")
    _save_job(job_id)

    def progress(stage, done, total):
        pipeline.checkpoint_job(job_id, 70 + int(done / max(1, total) * 15),
                                f"{stage} ({done}/{total} voices ready)", force=done == total, job=job)

    recovery = await prepare_dialogue_audio(
        segments, video_path, ROOT_PROJECT_DIR / "storage" / "downloads" / f"voice_{job_id}",
        source_lang=job.get("detected_language", "auto"), engine_name=req.engine,
        api_key=req.api_key, model=req.model, base_url=req.base_url,
        voice_type=job.get("voice_type", "male"), rate=req.voice_rate,
        pitch=req.voice_pitch, volume=req.voice_volume, on_progress=progress,
        synthesize=synthesize_segments,
    )
    job.update(recovery)
    if not missing_translations(segments):
        mark_step(job, "translation", "complete")
    mark_step(job, "voices", "complete")
    mark_step(job, "render", "partial")

    job.update(stage="Mixing audio and rendering video", progress=max(job.get("progress", 0), 85), pipeline_phase="rendering")
    _save_job(job_id)
    dub_result = await assemble_dubbed_video(
        video_path=video_path, segments=segments, output_filename=job.get("output_filename") or f"dubbed_{job_id}.mp4",
        audio_mode=req.audio_mode, bg_volume=req.bg_volume,
        burn_subtitles=req.burn_subtitles, render_options=req.render_values(),
    )
    dub_result.update(recovery)
    job["result"] = dub_result
    job["render_options"] = req.render_values()
    job["needs_rerender"] = False
    stage = "Complete" if not recovery["fallback_count"] else f"Complete — {recovery['fallback_count']} cues need review"
    job.update(status="completed", progress=100, stage=stage, error=None, pipeline_phase="completed")
    mark_step(job, "render", "complete")
    _save_job(job_id)
    await asyncio.to_thread(cleanup_saved_job_caches)
    return {"status": "completed", "result": dub_result, "segments": segments,
            "message": "Video rendered successfully." if not recovery["fallback_count"] else "Video rendered with original-speech fallbacks; review the flagged cues."}


@router.post("/job/{job_id}/resume")
async def resume_saved_job(job_id: str, req: JobRerenderRequest, background_tasks: BackgroundTasks):
    if pipeline.RESETTING:
        raise HTTPException(status_code=409, detail="Task caches are being cleared. Try again shortly.")
    job = get_job_status(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.get("status") in ACTIVE_STATUSES or job_id in pipeline.RUNNING_JOBS:
        raise HTTPException(status_code=409, detail="This job is already running")
    saved = job.get("request_options") or {}
    values = {"audio_mode": saved.get("audio_mode", "replace"), "engine": saved.get("engine_name", "auto"),
              "model": saved.get("model"), "base_url": saved.get("base_url"),
              **(job.get("render_options") or {}), **job.get("voice_settings", {}),
              **req.model_dump(exclude_unset=True)}
    resume_request = JobRerenderRequest(**values)

    async def resume():
        try:
            if not job.get("segments") and req.segments is None:
                worker_options = {key: value for key, value in saved.items() if key in {
                    "is_url", "voice_type", "tone", "audio_mode", "burn_subtitles", "render_options",
                    "max_duration", "model", "base_url", "subtitle_file_path", "voice_rate", "voice_pitch", "voice_volume",
                    "include_english_reference",
                }}
                worker_options.setdefault("is_url", str(job.get("input", "")).startswith(("http://", "https://")))
                worker_options.update(engine_name=resume_request.engine, api_key=resume_request.api_key,
                                      model=resume_request.model, base_url=resume_request.base_url)
                await run_video_dubbing_pipeline(job_id=job_id, video_source=job.get("input", ""), **worker_options)
            else:
                async with pipeline.video_job_slot(job_id) as acquired:
                    if acquired:
                        await _rerender_saved_video(job_id, resume_request)
        except asyncio.CancelledError:
            if job.get("pause_requested"):
                job.update(status="paused", stage="Paused — resume saved work")
            else:
                job.update(status="interrupted", stage="Interrupted — resume saved work")
            _save_job(job_id)
            raise
        except Exception as error:
            detail = error.detail if isinstance(error, HTTPException) else str(error)
            job.update(status="failed", stage="Failed", error=str(detail))
            _save_job(job_id)
        finally:
            if job.get("batch_id"):
                from ..video.batch import finalize_resumed_job
                await finalize_resumed_job(job)

    if job.get("status") == "completed":
        job.update(progress=0, run_id=uuid.uuid4().hex)
    job.pop("pause_requested", None)
    job.update(status="queued", stage="Queued — waiting to resume saved video", error=None)
    _save_job(job_id)
    background_tasks.add_task(resume)
    return {"job_id": job_id, "status": "queued"}


@router.post("/job/{job_id}/visual-update")
async def update_job_visuals(job_id: str, req: VideoRenderOptions, background_tasks: BackgroundTasks):
    """Apply branding, blur, and captions while copying the completed audio track."""
    job = get_job_status(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.get("status") in ACTIVE_STATUSES or job_id in pipeline.RUNNING_JOBS:
        raise HTTPException(status_code=409, detail="This job is already running")
    if job.get("needs_rerender") or any(seg.get("_dirty") for seg in job.get("segments", [])):
        raise HTTPException(status_code=409, detail="Re-render edited dialogue before applying visual-only changes")
    source = job.get("video_path")
    completed = (job.get("result") or {}).get("dubbed_video_path")
    if not source or not completed or not Path(source).is_file() or not Path(completed).is_file():
        raise HTTPException(status_code=400, detail="A completed video and the original source are required")

    async def render():
        try:
            async with pipeline.video_job_slot(job_id) as acquired:
                if not acquired:
                    return
                job.update(stage="Applying visual effects to the completed video", progress=85)
                _save_job(job_id)
                result = await assemble_dubbed_video(
                    video_path=source, segments=job.get("segments", []),
                    output_filename=f"dubbed_{job_id}_visual_{uuid.uuid4().hex[:8]}.mp4",
                    render_options=req.render_values(), prepared_audio_path=completed,
                )
                job.update(result=result, render_options=req.render_values(), status="completed",
                           stage="Complete", progress=100, error=None)
        except Exception as error:
            job.update(status="failed", stage="Failed", error=str(error))
        _save_job(job_id)
        await asyncio.to_thread(cleanup_saved_job_caches)

    job.update(status="queued", stage="Queued — waiting to apply visual effects", progress=0,
               error=None, render_options=req.render_values(), run_id=uuid.uuid4().hex)
    _save_job(job_id)
    background_tasks.add_task(render)
    return {"job_id": job_id, "status": "queued"}


@router.post("/logo")
async def upload_video_logo(file: UploadFile = File(...)):
    """Accept a small verified raster logo and return an opaque identifier."""
    from PIL import Image, UnidentifiedImageError

    content = await file.read(10 * 1024 * 1024 + 1)
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Logo must be smaller than 10 MB")
    try:
        with Image.open(io.BytesIO(content)) as logo:
            extension = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp"}.get(logo.format)
            if not extension or logo.width * logo.height > 16_000_000:
                raise ValueError
            logo.verify()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise HTTPException(status_code=400, detail="Upload a valid PNG, JPEG, or WebP logo under 16 megapixels")
    identifier = f"custom_{uuid.uuid4().hex}"
    LOGO_DIR.mkdir(parents=True, exist_ok=True)
    path = LOGO_DIR / f"{identifier}.{extension}"
    await asyncio.to_thread(path.write_bytes, content)
    return {"logo_id": identifier, "preview_url": f"/api/video/logo/{identifier}", "size": "40×40"}


@router.get("/logo/{logo_id}")
async def preview_video_logo(logo_id: str):
    try:
        return FileResponse(resolve_logo(logo_id))
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error))


@router.post("/job/{job_id}/translate-missing")
async def repair_missing_job_translations(job_id: str, req: RepairTranslationsRequest):
    """Fill gaps without replacing existing translations or rerunning transcription."""
    from ..engines.dialogue_translator import translate_dialogue_segments

    job = get_job_status(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if req.segments is not None:
        _apply_segment_updates(job, req.segments)
    pending = missing_translations(job.get("segments", []))
    error = None
    try:
        if pending:
            await translate_dialogue_segments(
                pending, job.get("detected_language", "auto"), engine_name=req.engine,
                api_key=req.api_key, model=req.model, base_url=req.base_url, tone=req.tone,
            )
    except MissingTranslationsError as failure:
        error = str(failure)
    finally:
        repaired = [seg for seg in pending if (seg.get("translation") or "").strip() and not seg.get("translation_error")]
        for seg in repaired:
            seg.pop("translation_error", None)
            seg["_dirty"] = True
            seg["audio_file"] = None
        if repaired:
            job["needs_rerender"] = True
        _save_job(job_id)

    remaining = missing_translations(job.get("segments", []))
    return {
        "status": "partial" if remaining else "ok",
        "repaired_count": len(repaired),
        "remaining_count": len(remaining),
        "error": error,
        "segments": job.get("segments", []),
        "needs_rerender": job.get("needs_rerender", False),
    }


# =========================================================================
# VIDEO SPLITTER & CLIP CUTTER ENDPOINTS
# =========================================================================

@router.post("/split-video")
async def split_video_endpoint(
    file: Optional[UploadFile] = File(None),
    existing_filename: Optional[str] = Form(None),
    clip_minutes: float = Form(30.0),
):
    """
    Splits a multi-hour video into clips of exact duration (in minutes),
    and packages them into a single downloadable .zip archive.
    """
    from ..video.splitter import split_video_by_minutes, SPLITS_DIR

    target_video_path: Optional[Path] = None

    if file and file.filename:
        target_video_path = await _store_upload(file, STORAGE_UPLOADS / uuid.uuid4().hex)
    elif existing_filename:
        for root in (STORAGE_UPLOADS, DUBBED_DIR):
            candidate = (root / existing_filename).resolve()
            if candidate.is_relative_to(root.resolve()) and candidate.is_file():
                target_video_path = candidate
                break
        if target_video_path is None:
            raise HTTPException(status_code=404, detail=f"File not found: {existing_filename}")
    else:
        raise HTTPException(status_code=400, detail="No video file or existing filename provided")

    if not target_video_path or not target_video_path.exists():
        raise HTTPException(status_code=400, detail="Valid video file could not be resolved")

    if clip_minutes <= 0:
        clip_minutes = 30.0

    try:
        # Run splitting in worker thread to prevent event loop blocking
        result = await asyncio.to_thread(
            split_video_by_minutes,
            video_path=str(target_video_path),
            clip_minutes=float(clip_minutes),
            output_dir=SPLITS_DIR,
            prefix=f"{target_video_path.stem}_{uuid.uuid4().hex[:8]}",
        )
        return result
    except Exception as e:
        logger.error(f"Error splitting video {target_video_path}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/download-split-zip")
async def download_split_zip(zip_name: str = Query(...)):
    """Downloads a generated .zip containing split video parts."""
    from ..video.splitter import SPLITS_DIR

    safe_name = Path(zip_name).name
    zip_path = SPLITS_DIR / safe_name
    if not zip_path.exists():
        zip_path_alt = DUBBED_DIR / safe_name
        if zip_path_alt.exists():
            zip_path = zip_path_alt
        else:
            raise HTTPException(status_code=404, detail="ZIP archive not found")

    return FileResponse(
        path=str(zip_path),
        filename=zip_path.name,
        media_type="application/zip",
    )

