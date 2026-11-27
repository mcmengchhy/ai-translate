"""Export saved narration in batches and reuse it with replacement visuals."""

import asyncio
from typing import Literal

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .video_routes import _store_upload
from ..video import media_exports, pipeline
from ..video.clip_edits import ClipEditOptions, ColorFilter, launch_clip_edit
from ..video.checkpoints import ACTIVE_STATUSES
from ..video.segments import translation_summary, voice_summary

router = APIRouter(prefix="/api/video")


class NarrationExportRequest(BaseModel):
    job_ids: list[str] = Field(min_length=1, max_length=100)


@router.get("/export-jobs")
async def export_jobs():
    def summaries():
        result = []
        for job in list(pipeline.JOBS.values()):
            voices = voice_summary(job.get("segments") or [])
            translations = translation_summary(job.get("segments") or [])
            result.append({"id": job["id"], "title": job.get("title") or job["id"],
                           "status": job.get("status"), **voices, **translations,
                           "export_ready": bool(voices["voice_cues"] and not voices["missing_voices"] and
                                                not translations["missing_cues"] and
                                                job.get("status") not in ACTIVE_STATUSES and
                                                job["id"] not in pipeline.RUNNING_JOBS)})
        return sorted(result, key=lambda item: (item["title"].casefold(), item["id"]))
    return {"jobs": await asyncio.to_thread(summaries)}


@router.get("/exports")
async def export_history(kind: Literal["bundle", "replacement", "clip-edit"] | None = None):
    def recent():
        states = []
        for path in media_exports.EXPORT_DIR.glob("*/state.json"):
            try:
                state = media_exports.read_export(path.parent.name)
                if kind is None or state.get("kind") == kind:
                    states.append(state)
            except (OSError, ValueError):
                continue
        return sorted(states, key=lambda state: state.get("created_at", 0), reverse=True)[:30]
    return {"exports": await asyncio.to_thread(recent)}


def _snapshot(job_id):
    try:
        return media_exports.snapshot_job(job_id)
    except KeyError:
        raise HTTPException(404, f"Job {job_id} not found")
    except (ValueError, RuntimeError, OSError) as error:
        raise HTTPException(409, str(error))


@router.post("/exports", status_code=202)
async def export_narration(request: NarrationExportRequest):
    if pipeline.RESETTING:
        raise HTTPException(409, "Task caches are being cleared. Try again shortly.")
    snapshots = [_snapshot(job_id) for job_id in dict.fromkeys(request.job_ids)]
    state = media_exports.create_export(snapshots)
    media_exports.launch_export(state, snapshots)
    return state


@router.post("/job/{job_id}/replacement-video", status_code=202)
async def replacement_video(job_id: str, file: UploadFile = File(...),
                            ending: Literal["loop", "freeze"] = Form("loop"),
                            burn_subtitles: bool = Form(True)):
    if pipeline.RESETTING:
        raise HTTPException(409, "Task caches are being cleared. Try again shortly.")
    _snapshot(job_id)
    state = media_exports.create_export([], kind="replacement", ending=ending, burn_subtitles=burn_subtitles)
    try:
        destination = await _store_upload(file, media_exports.export_directory(state["id"]) / "upload")
        # The original job may have changed while a large file was uploading.
        snapshot = _snapshot(job_id)
        state["jobs"] = [{"id": snapshot["id"], "title": snapshot["title"]}]
        media_exports.save_export(state)
        media_exports.launch_export(state, [snapshot], destination)
    except BaseException:
        state.update(status="failed", stage="Replacement upload failed; try again")
        media_exports.save_export(state)
        raise
    finally:
        await file.close()
    return state


@router.post("/clip-edits", status_code=202)
async def edit_uploaded_clip(
    file: UploadFile = File(...),
    flip: Literal["none", "horizontal", "vertical", "both"] = Form("horizontal"),
    speed: float = Form(1, ge=.25, le=4),
    color_filter: ColorFilter = Form("normal"),
    sound_effect: Literal["none", "ambience", "transitions", "both"] = Form("none"),
    original_volume: int = Form(100, ge=0, le=100),
    effect_volume: int = Form(8, ge=0, le=100),
):
    if pipeline.RESETTING:
        raise HTTPException(409, "Task caches are being cleared. Try again shortly.")
    options = ClipEditOptions(flip=flip, speed=speed, color_filter=color_filter, sound_effect=sound_effect,
                              original_volume=original_volume, effect_volume=effect_volume)
    state = media_exports.create_export([{"id": "clip", "title": file.filename or "Uploaded clip"}],
                                        kind="clip-edit", edit_options=options.model_dump())
    try:
        destination = await _store_upload(file, media_exports.export_directory(state["id"]) / "upload")
        launch_clip_edit(state, destination)
    except BaseException:
        state.update(status="failed", stage="Clip upload failed; try again")
        media_exports.save_export(state)
        raise
    finally:
        await file.close()
    return state


def _state(export_id):
    try:
        return media_exports.read_export(export_id)
    except FileNotFoundError:
        raise HTTPException(404, "Export not found")


@router.get("/exports/{export_id}")
async def export_status(export_id: str):
    return _state(export_id)


@router.get("/exports/{export_id}/download")
async def download_export(export_id: str):
    state = _state(export_id)
    if state["status"] != "completed":
        raise HTTPException(409, "The export is not ready")
    path = media_exports.export_directory(export_id) / "khmer-bundle.zip"
    if not path.is_file():
        raise HTTPException(404, "Export archive not found")
    prefix = "clip" if state.get("kind") == "clip-edit" else "khmer"
    return FileResponse(path, filename=f"{prefix}-{export_id[:8]}.zip", media_type="application/zip")


@router.get("/exports/{export_id}/files/{index}/{kind}")
async def download_export_file(export_id: str, index: int, kind: Literal["audio", "subtitles", "video"]):
    state = _state(export_id)
    if state["status"] != "completed":
        raise HTTPException(409, "The export is not ready")
    if not 0 <= index < len(state["jobs"]):
        raise HTTPException(404, "Export item not found")
    name, media_type = {"audio": ("khmer.wav", "audio/wav"),
                        "subtitles": ("khmer.srt", "application/x-subrip"),
                        "video": ("replacement.mp4", "video/mp4")}[kind]
    if kind == "video" and state.get("kind") == "clip-edit":
        name = "edited.mp4"
    path = media_exports.export_directory(export_id) / str(index) / name
    if not path.is_file():
        raise HTTPException(404, "Export file not found")
    return FileResponse(path, filename=f"{index + 1:03d}-{name}", media_type=media_type)
