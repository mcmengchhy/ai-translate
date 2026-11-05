"""API endpoints for Video Translation and Khmer Voice Dubbing."""

import asyncio
import logging
import shutil
import zipfile
from pathlib import Path
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..video.pipeline import create_job, get_job_status, get_all_jobs, run_video_dubbing_pipeline, _save_job, cancel_job
from ..video.tts import synthesize_khmer_clip, VOICE_PROFILES
from ..video.dubber import DUBBED_DIR, assemble_dubbed_video, schedule_non_overlapping_clips, get_media_duration
from ..khmer import normalize_khmer_unicode

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/video")

ROOT_PROJECT_DIR = Path(__file__).resolve().parent.parent.parent.parent
STORAGE_UPLOADS = ROOT_PROJECT_DIR / "storage" / "uploads"
STORAGE_UPLOADS.mkdir(parents=True, exist_ok=True)


class VideoDubUrlRequest(BaseModel):
    url: str = Field(..., description="Bilibili, YouTube, or direct video URL")
    voice_type: str = Field(default="multi", description="'multi', 'male', 'male_deep', 'male_young', 'female', 'female_sweet', 'female_elder'")
    tone: str = Field(default="standard", description="Linguistic tone: standard, casual, formal, polite")
    audio_mode: str = Field(default="replace", description="'replace' (100% remove original sound), 'cinema_3', or 'smart_cinema'")
    remove_origin_sound: bool = Field(default=True, description="Completely remove original video sound so there is zero foreign audio or copyright impact")
    burn_subtitles: bool = Field(default=False, description="Burn Khmer subtitles into video frames")
    mask_hardsubs: bool = Field(default=True, description="Cover Chinese hardsubs with sleek dark banner")
    anti_copyright_video: bool = Field(default=True, description="1.03x smart crop/zoom and subtle color grade to evade Content ID")
    mirror_video: bool = Field(default=False, description="Horizontal mirror flip for video")
    max_duration: Optional[int] = Field(default=0, description="Duration in seconds to dub (e.g. 90 for fast test, 300 for 5m, 0 for full video)")
    api_key: Optional[str] = Field(default=None, description="Optional Gemini API key")
    subtitle_file_path: Optional[str] = Field(default=None, description="Optional path to custom subtitle file")
    voice_rate: str = Field(default="auto", description="Voice speed adjustment e.g. '+0%', '+10%', '-10%'")
    voice_pitch: str = Field(default="auto", description="Voice pitch adjustment e.g. '+0Hz', '+3Hz', '-3Hz'")
    voice_volume: str = Field(default="auto", description="Voice volume adjustment e.g. '+0%', '+10%'")
    split_duration: Optional[int] = Field(default=1800, description="Auto-split output into parts: 1800 for 30m parts, 3600 for 1h, 0 for no split")


class VoicePreviewRequest(BaseModel):
    text: str = Field(default="សួស្តីបងប្អូនទាំងអស់គ្នា សូមស្វាគមន៍មកកាន់វីដេអូបកប្រែជាភាសាខ្មែរ")
    voice_type: str = Field(default="male", description="Key in VOICE_PROFILES: male, male_deep, male_young, female, female_sweet, female_elder")
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


class PolishDialogueRequest(BaseModel):
    api_key: Optional[str] = None
    tone: str = "casual"


class JobResumeRequest(BaseModel):
    voice_type: Optional[str] = None
    tone: Optional[str] = None
    audio_mode: Optional[str] = None
    remove_origin_sound: Optional[bool] = None
    burn_subtitles: Optional[bool] = None
    mask_hardsubs: Optional[bool] = None
    anti_copyright_video: Optional[bool] = None
    mirror_video: Optional[bool] = None
    voice_rate: Optional[str] = None
    voice_pitch: Optional[str] = None
    voice_volume: Optional[str] = None
    split_duration: Optional[int] = None


class JobSplitRequest(BaseModel):
    split_duration: int = Field(default=3600, description="Duration in seconds per part (3600 for 1h, 1800 for 30m, etc.)")
    target: str = Field(default="auto", description="'dubbed', 'source', or 'auto'")


class DirectFileSplitRequest(BaseModel):
    filename_or_path: str
    split_duration: int = Field(default=3600, description="Duration in seconds per part (default 3600 = 1 hour)")


class MergeEpisodesRequest(BaseModel):
    episode_indices: List[int] = Field(default=[], description="List of episode numbers to merge in sequence (e.g. [1, 2, 3])")
    output_name: Optional[str] = Field(default=None, description="Optional custom name for merged video")


class JobRerenderRequest(BaseModel):
    segments: Optional[List[Dict[str, Any]]] = None
    audio_mode: str = "replace"  # "replace" (100% remove original sound), "cinema_3", "smart_cinema"
    remove_origin_sound: bool = True
    bg_volume: Optional[float] = 0.0
    burn_subtitles: bool = False
    mask_hardsubs: bool = True
    anti_copyright_video: bool = True
    mirror_video: bool = False
    voice_rate: str = "auto"
    voice_pitch: str = "auto"
    voice_volume: str = "auto"


@router.post("/dub-url")
async def start_dubbing_from_url(req: VideoDubUrlRequest, background_tasks: BackgroundTasks):
    if not req.url.strip():
        raise HTTPException(status_code=400, detail="Video URL is required")

    job_id = create_job(req.url)

    background_tasks.add_task(
        run_video_dubbing_pipeline,
        job_id=job_id,
        video_source=req.url,
        is_url=True,
        voice_type=req.voice_type,
        tone=req.tone,
        audio_mode=req.audio_mode,
        burn_subtitles=req.burn_subtitles,
        mask_hardsubs=req.mask_hardsubs,
        anti_copyright_video=req.anti_copyright_video,
        mirror_video=req.mirror_video,
        remove_origin_sound=req.remove_origin_sound,
        max_duration=req.max_duration,
        api_key=req.api_key,
        subtitle_file_path=req.subtitle_file_path,
        voice_rate=req.voice_rate,
        voice_pitch=req.voice_pitch,
        voice_volume=req.voice_volume,
        split_duration=req.split_duration,
    )

    return {"job_id": job_id, "status": "queued"}


@router.post("/dub-upload")
async def start_dubbing_from_file(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    subtitle_file: Optional[UploadFile] = File(None),
    voice_type: str = Form("multi"),
    tone: str = Form("standard"),
    audio_mode: str = Form("replace"),
    remove_origin_sound: bool = Form(True),
    burn_subtitles: bool = Form(False),
    mask_hardsubs: bool = Form(True),
    anti_copyright_video: bool = Form(True),
    mirror_video: bool = Form(False),
    api_key: Optional[str] = Form(None),
    voice_rate: str = Form("auto"),
    voice_pitch: str = Form("auto"),
    voice_volume: str = Form("auto"),
    split_duration: int = Form(1800),
):
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file uploaded")

    dest_file = STORAGE_UPLOADS / file.filename
    with open(dest_file, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    sub_path_str = None
    if subtitle_file and subtitle_file.filename:
        sub_dest = STORAGE_UPLOADS / f"sub_{file.filename}_{subtitle_file.filename}"
        with open(sub_dest, "wb") as sub_buf:
            shutil.copyfileobj(subtitle_file.file, sub_buf)
        sub_path_str = str(sub_dest)

    job_id = create_job(str(dest_file))

    background_tasks.add_task(
        run_video_dubbing_pipeline,
        job_id=job_id,
        video_source=str(dest_file),
        is_url=False,
        voice_type=voice_type,
        tone=tone,
        audio_mode=audio_mode,
        burn_subtitles=burn_subtitles,
        mask_hardsubs=mask_hardsubs,
        anti_copyright_video=anti_copyright_video,
        mirror_video=mirror_video,
        remove_origin_sound=remove_origin_sound,
        api_key=api_key,
        subtitle_file_path=sub_path_str,
        voice_rate=voice_rate,
        voice_pitch=voice_pitch,
        voice_volume=voice_volume,
        split_duration=split_duration,
    )

    return {"job_id": job_id, "status": "queued", "filename": file.filename}


@router.get("/status/{job_id}")
async def check_pipeline_status(job_id: str):
    job = get_job_status(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.get("/jobs")
async def list_recent_jobs():
    """List all recent jobs for the dashboard and resume picker."""
    jobs = get_all_jobs()
    summary = []
    for j in reversed(jobs):
        summary.append({
            "id": j.get("id"),
            "status": j.get("status"),
            "title": j.get("title", "Video"),
            "progress": j.get("progress", 0),
            "stage": j.get("stage", ""),
            "segments_count": len(j.get("segments", [])),
            "has_result": bool(j.get("result")),
            "error": j.get("error"),
        })
    return summary[:20]


@router.post("/job/{job_id}/resume")
async def resume_job_endpoint(
    job_id: str,
    background_tasks: BackgroundTasks,
    req: Optional[JobResumeRequest] = None,
):
    """Resume a stopped, failed, or partially-completed video pipeline from its latest checkpoint."""
    job = get_job_status(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    from backend.app.video.pipeline import _ACTIVE_JOB_TASKS, _CANCELLED_JOBS
    if job_id in _ACTIVE_JOB_TASKS:
        return {
            "job_id": job_id,
            "status": "processing",
            "message": "Job is already actively running and generating voiceover",
            "progress": job.get("progress", 0),
        }

    # Clear previous cancellation flag
    _CANCELLED_JOBS.discard(job_id)

    # If episodic, reset any cancelled or unfinished episodes to queued
    if job.get("episodes"):
        for ep in job["episodes"]:
            if ep.get("status") in ("cancelled", "processing"):
                ep["status"] = "queued"
                ep["stage"] = "Waiting in queue..."

    stored_params = job.get("params", {})
    req_dict = req.dict(exclude_unset=True) if req else {}

    def _param(key, default):
        if req and req_dict.get(key) is not None:
            return req_dict[key]
        if key in stored_params and stored_params[key] is not None:
            return stored_params[key]
        return default

    voice_type = _param("voice_type", "multi")
    tone = _param("tone", "standard")
    engine_name = _param("engine_name", "auto")
    audio_mode = _param("audio_mode", "replace")
    remove_origin_sound = _param("remove_origin_sound", True)
    burn_subtitles = _param("burn_subtitles", False)
    mask_hardsubs = _param("mask_hardsubs", True)
    anti_copyright_video = _param("anti_copyright_video", True)
    mirror_video = _param("mirror_video", False)
    max_duration = _param("max_duration", 0)
    api_key = _param("api_key", None)
    subtitle_file_path = _param("subtitle_file_path", None)
    voice_rate = _param("voice_rate", "auto")
    voice_pitch = _param("voice_pitch", "auto")
    voice_volume = _param("voice_volume", "auto")
    split_duration = int(_param("split_duration", 0) or 0)
    is_url = bool(stored_params.get("is_url", True))

    video_source = job.get("video_path") or job.get("input") or ""
    if not video_source:
        raise HTTPException(status_code=400, detail="Job has no video source to resume")

    if Path(video_source).exists():
        is_url = False

    job["status"] = "processing"
    job["error"] = None
    job["stage"] = "Resuming from checkpoint..."
    _save_job(job_id)

    background_tasks.add_task(
        run_video_dubbing_pipeline,
        job_id=job_id,
        video_source=video_source,
        is_url=is_url,
        voice_type=voice_type,
        tone=tone,
        engine_name=engine_name,
        audio_mode=audio_mode,
        burn_subtitles=burn_subtitles,
        mask_hardsubs=mask_hardsubs,
        anti_copyright_video=anti_copyright_video,
        mirror_video=mirror_video,
        remove_origin_sound=remove_origin_sound,
        max_duration=max_duration,
        api_key=api_key,
        subtitle_file_path=subtitle_file_path,
        voice_rate=voice_rate,
        voice_pitch=voice_pitch,
        voice_volume=voice_volume,
        split_duration=split_duration,
    )

    return {
        "status": "resumed",
        "job_id": job_id,
        "message": f"Job {job_id} resumed from checkpoint!",
    }


@router.post("/job/{job_id}/cancel")
async def cancel_job_endpoint(job_id: str):
    """Aborts an active or queued dubbing job immediately and releases semaphore."""
    job = get_job_status(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    was_active = cancel_job(job_id)
    return {
        "status": "cancelled",
        "job_id": job_id,
        "message": "Dubbing process was successfully cancelled.",
        "was_active": was_active,
    }


@router.post("/job/{job_id}/split")
async def split_job_video_endpoint(job_id: str, req: Optional[JobSplitRequest] = None):
    """
    Splits a job's video (dubbed or source) into 1-hour parts (or custom duration)
    with matched Khmer subtitle files (.srt & .vtt) for each part.
    """
    job = get_job_status(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    split_dur = req.split_duration if req and req.split_duration > 0 else 3600
    target_mode = req.target if req else "auto"

    video_path = None
    if target_mode in ("dubbed", "auto"):
        dubbed_p = job.get("result", {}).get("dubbed_video_path")
        if dubbed_p and Path(dubbed_p).exists():
            video_path = dubbed_p

    if not video_path:
        # Use source video
        src_p = job.get("video_path") or job.get("input")
        if src_p and Path(src_p).exists():
            video_path = src_p

    if not video_path or not Path(video_path).exists():
        raise HTTPException(status_code=400, detail="No valid video file found on disk to split")

    from ..video.splitter import split_video_into_parts
    try:
        segments = job.get("segments", [])
        is_dubbed = "dubbed" in Path(video_path).name
        prefix = f"dubbed_{job_id}" if is_dubbed else f"split_{job_id}"

        split_result = split_video_into_parts(
            video_path=video_path,
            split_duration=float(split_dur),
            prefix=prefix,
            segments=segments,
        )

        job["parts"] = split_result.get("parts", [])
        if "result" in job and isinstance(job["result"], dict):
            job["result"]["parts"] = split_result.get("parts", [])
        _save_job(job_id)

        return {
            "status": "completed",
            "job_id": job_id,
            "total_parts": split_result["total_parts"],
            "split_duration": split_result["split_duration"],
            "parts": split_result["parts"],
            "message": f"Successfully split video into {split_result['total_parts']} parts ({int(split_dur//60)}m each)!",
        }
    except Exception as e:
        logger.error(f"Error splitting video for job {job_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/job/{job_id}/episodes")
async def get_job_episodes_endpoint(job_id: str):
    """Returns the list of 30-minute sliced episodes and their live completion statuses."""
    job = get_job_status(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    episodes = job.get("episodes", [])
    completed_count = sum(1 for ep in episodes if ep.get("status") == "completed")

    return {
        "job_id": job_id,
        "is_episodic": bool(job.get("is_episodic")),
        "total_episodes": len(episodes),
        "completed_count": completed_count,
        "split_duration": job.get("split_duration", 1800),
        "episodes": episodes,
    }


@router.get("/job/{job_id}/download-all-zip")
async def download_all_episodes_zip(job_id: str):
    """Packages all finished episodes (MP4s + SRTs) into a single ZIP file for one-click download."""
    job = get_job_status(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    files_to_zip = []
    # 1. Check episodic outputs
    if job.get("is_episodic") and job.get("episodes"):
        for ep in job["episodes"]:
            if ep.get("status") == "completed" and ep.get("dubbed_video_path"):
                v_p = Path(ep["dubbed_video_path"])
                if v_p.exists():
                    files_to_zip.append(v_p)
                srt_p = v_p.with_name(v_p.stem + "_kh.srt")
                if not srt_p.exists():
                    srt_p = v_p.with_suffix(".srt")
                if srt_p.exists():
                    files_to_zip.append(srt_p)
    # 2. Check parts from split
    elif job.get("parts"):
        for p in job["parts"]:
            v_p = Path(p.get("video_path", ""))
            if v_p.exists():
                files_to_zip.append(v_p)
            if p.get("srt_url"):
                srt_p = DUBBED_DIR / Path(p["srt_url"]).name
                if srt_p.exists():
                    files_to_zip.append(srt_p)
    # 3. Single video result
    elif job.get("result", {}).get("dubbed_video_path"):
        v_p = Path(job["result"]["dubbed_video_path"])
        if v_p.exists():
            files_to_zip.append(v_p)
        srt_p = v_p.with_name(v_p.stem + "_kh.srt")
        if not srt_p.exists():
            srt_p = v_p.with_suffix(".srt")
        if srt_p.exists():
            files_to_zip.append(srt_p)

    if not files_to_zip:
        raise HTTPException(status_code=400, detail="No completed video episodes found to package yet")

    zip_filename = f"Dubbed_{job_id}_Episodes.zip"
    zip_path = DUBBED_DIR / zip_filename

    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for f in files_to_zip:
            zf.write(f, arcname=f.name)

    return FileResponse(
        str(zip_path),
        media_type="application/zip",
        filename=zip_filename,
    )


@router.post("/job/{job_id}/merge-episodes")
async def merge_selected_episodes_endpoint(job_id: str, req: MergeEpisodesRequest):
    """
    Merges selected completed 30-minute episode clips into a single continuous video clip
    with concatenated Khmer subtitles.
    """
    job = get_job_status(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    from ..video.splitter import merge_episodes_into_single_video

    episodes = job.get("episodes", [])
    if not episodes:
        raise HTTPException(status_code=400, detail="This job does not have episodic parts to merge")

    # Filter requested episodes
    if req.episode_indices:
        selected_eps = [ep for ep in episodes if ep.get("index") in req.episode_indices]
        selected_eps.sort(key=lambda e: e.get("index", 0))
    else:
        selected_eps = [ep for ep in episodes if ep.get("status") == "completed"]

    completed_eps = [ep for ep in selected_eps if ep.get("status") == "completed" and ep.get("dubbed_video_path") and Path(ep["dubbed_video_path"]).exists()]

    if not completed_eps:
        raise HTTPException(status_code=400, detail="None of the selected episodes are completed yet or their files were not found")

    file_paths = [ep["dubbed_video_path"] for ep in completed_eps]
    segments_per_ep = [ep.get("segments", []) for ep in completed_eps]

    indices = [ep.get("index", 1) for ep in completed_eps]
    min_idx, max_idx = min(indices), max(indices)
    label = f"ep{min_idx:02d}_to_ep{max_idx:02d}" if min_idx != max_idx else f"ep{min_idx:02d}"
    if req.output_name:
        clean_name = "".join(c for c in req.output_name if c.isalnum() or c in ("-", "_"))
        if clean_name:
            label = clean_name

    merged_filename = f"dubbed_{job_id}_merged_{label}.mp4"
    output_path = DUBBED_DIR / merged_filename

    try:
        merge_res = merge_episodes_into_single_video(
            episode_files=file_paths,
            output_path=str(output_path),
            segments_per_episode=segments_per_ep,
        )
        return merge_res
    except Exception as e:
        logger.error(f"Failed to merge episodes for job {job_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/split-file")
async def split_standalone_file_endpoint(req: DirectFileSplitRequest):
    """
    Directly splits any local video file (e.g. In storage/uploads or storage/dubbed)
    into 1-hour parts without needing a full dubbing pipeline.
    """
    candidate = Path(req.filename_or_path)
    if not candidate.exists():
        # Check storage/uploads or storage/dubbed
        cand1 = STORAGE_UPLOADS / req.filename_or_path
        cand2 = DUBBED_DIR / req.filename_or_path
        if cand1.exists():
            candidate = cand1
        elif cand2.exists():
            candidate = cand2
        else:
            raise HTTPException(status_code=404, detail=f"File not found: {req.filename_or_path}")

    from ..video.splitter import split_video_into_parts
    try:
        split_dur = req.split_duration if req.split_duration > 0 else 3600
        split_result = split_video_into_parts(
            video_path=str(candidate),
            split_duration=float(split_dur),
            prefix=f"part_{candidate.stem}",
        )
        return {
            "status": "completed",
            "file": candidate.name,
            "total_parts": split_result["total_parts"],
            "split_duration": split_result["split_duration"],
            "parts": split_result["parts"],
        }
    except Exception as e:
        logger.error(f"Error splitting direct file {candidate}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/voice-profiles")
async def list_voice_profiles():
    """Returns available Khmer character voice personas."""
    return [
        {
            "id": k,
            "name": v["name"],
            "label": v["label"],
            "gender": v["gender"],
        }
        for k, v in VOICE_PROFILES.items()
    ]


@router.post("/preview-voice")
async def preview_khmer_voice(req: VoicePreviewRequest):
    v_type = req.voice_type if req.voice_type in VOICE_PROFILES else "male"
    out_file = DUBBED_DIR / f"preview_{v_type}.mp3"
    try:
        await synthesize_khmer_clip(
            req.text, str(out_file),
            voice_type=v_type,
            rate=req.rate,
            pitch=req.pitch,
            volume=req.volume,
            enhance=req.enhance,
        )
        return {"audio_url": f"/static/dubbed/{out_file.name}", "voice_type": v_type}
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

    job["segments"] = req.segments
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
    schedule_non_overlapping_clips(segments, temp_dir=temp_dir, max_speed=1.45)
    _save_job(job_id)

    return {
        "status": "ok",
        "message": "All speech cue overlaps automatically resolved!",
        "segments": segments,
    }


@router.post("/job/{job_id}/ai-polish-dialogue")
async def ai_polish_job_dialogue(job_id: str, req: PolishDialogueRequest):
    """Re-translates dialogue using contextual film-dubbing logic."""
    job = get_job_status(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    segments = job.get("segments", [])
    if not segments:
        return {"status": "ok", "segments": []}

    from ..engines.dialogue_translator import translate_dialogue_batch_gemini, translate_dialogue_clusters_free
    from ..khmer import batch_polish_segments

    import os
    source_lang = job.get("detected_language", "zh")
    effective_api_key = (req.api_key or os.getenv("GEMINI_API_KEY", "")).strip()
    if effective_api_key:
        batch_size = 15
        for b in range(0, len(segments), batch_size):
            b_chunk = segments[b : b + batch_size]
            try:
                await translate_dialogue_batch_gemini(b_chunk, source_lang, effective_api_key, tone=req.tone)
            except Exception as e:
                logger.warning(f"Gemini polish batch failed: {e}. Falling back to free clustered polish...")
                await translate_dialogue_clusters_free(b_chunk, source_lang, tone=req.tone)
    else:
        await translate_dialogue_clusters_free(segments, source_lang, tone=req.tone)

    batch_polish_segments(segments)
    for seg in segments:
        seg["_dirty"] = True

    _save_job(job_id)

    return {
        "status": "ok",
        "message": "Dialogue successfully polished with natural conversational logic!",
        "segments": segments,
    }


@router.post("/job/{job_id}/rerender")
async def rerender_dubbed_video(job_id: str, req: JobRerenderRequest):
    """Re-renders the video timeline with edited translations, voices, or audio modes in seconds."""
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

    if req.segments:
        job["segments"] = req.segments

    segments = job["segments"]

    # Re-synthesize any segment that needs audio
    audio_dir = ROOT_PROJECT_DIR / "storage" / "downloads" / f"voice_{job_id}"
    audio_dir.mkdir(parents=True, exist_ok=True)

    force_resynth = (req.voice_rate != "auto" or req.voice_pitch != "auto" or req.voice_volume != "auto")

    for seg in segments:
        audio_file = seg.get("audio_file")
        v_type = seg.get("voice_type", "male")
        text = normalize_khmer_unicode(seg.get("translation", ""))

        if not audio_file or not Path(audio_file).exists() or seg.get("_dirty") or force_resynth:
            seg_file = audio_dir / f"seg_{seg['id']}_{v_type}.mp3"
            if text:
                seg_dur = float(seg.get("end", 0)) - float(seg.get("start", 0))
                seg_rate = seg.get("rate") or req.voice_rate
                seg_pitch = seg.get("pitch") or req.voice_pitch
                seg_volume = seg.get("volume") or req.voice_volume
                await synthesize_khmer_clip(
                    text, str(seg_file),
                    voice_type=v_type,
                    rate=seg_rate,
                    pitch=seg_pitch,
                    volume=seg_volume,
                    seg_duration=seg_dur if seg_dur > 0 else None,
                    enhance=True,
                )
                seg["audio_file"] = str(seg_file)
                seg.pop("_dirty", None)

    # Reassemble video with non-overlapping audio mixer & video anti-copyright mask
    dub_result = await assemble_dubbed_video(
        video_path=video_path,
        segments=segments,
        output_filename=f"dubbed_{job_id}.mp4",
        audio_mode=req.audio_mode,
        bg_volume=req.bg_volume,
        burn_subtitles=req.burn_subtitles,
        mask_hardsubs=req.mask_hardsubs,
        anti_copyright_video=req.anti_copyright_video,
        mirror_video=req.mirror_video,
        remove_origin_sound=req.remove_origin_sound,
    )

    job["result"] = dub_result
    job["segments"] = segments
    _save_job(job_id)

    return {
        "status": "completed",
        "result": dub_result,
        "segments": segments,
        "message": "Video successfully re-rendered with zero voice collisions!"
    }
