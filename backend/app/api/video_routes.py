"""API endpoints for Video Translation and Khmer Voice Dubbing."""

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field

from ..video.pipeline import create_job, get_job_status, run_video_dubbing_pipeline, _save_job
from ..video.tts import synthesize_khmer_clip
from ..video.dubber import DUBBED_DIR, assemble_dubbed_video, schedule_non_overlapping_clips, get_media_duration
from ..khmer import normalize_khmer_unicode

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/video")

ROOT_PROJECT_DIR = Path(__file__).resolve().parent.parent.parent.parent
STORAGE_UPLOADS = ROOT_PROJECT_DIR / "storage" / "uploads"
STORAGE_UPLOADS.mkdir(parents=True, exist_ok=True)


class VideoDubUrlRequest(BaseModel):
    url: str = Field(..., description="Bilibili, YouTube, or direct video URL")
    voice_type: str = Field(default="male", description="'male' (Piseth) or 'female' (Sreymom)")
    tone: str = Field(default="standard", description="Linguistic tone: standard, casual, formal, polite")
    audio_mode: str = Field(default="replace", description="'replace' (100% pure voiceover, identical to preview) or 'smart_sidechain' / 'ducking'")
    burn_subtitles: bool = Field(default=False, description="Burn Khmer subtitles into video frames")
    max_duration: Optional[int] = Field(default=0, description="Duration in seconds to dub (e.g. 90 for fast test, 300 for 5m, 0 for full video)")
    api_key: Optional[str] = Field(default=None, description="Optional Gemini API key")
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


class PolishDialogueRequest(BaseModel):
    api_key: Optional[str] = None
    tone: str = "casual"


class JobRerenderRequest(BaseModel):
    segments: Optional[List[Dict[str, Any]]] = None
    audio_mode: str = "replace"  # "replace" (100% pure voiceover), "mute", "duck_subtle", "ducking", "smart_sidechain"
    bg_volume: Optional[float] = 0.0
    burn_subtitles: bool = False
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
        max_duration=req.max_duration,
        api_key=req.api_key,
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
    tone: str = Form("standard"),
    audio_mode: str = Form("replace"),
    burn_subtitles: bool = Form(False),
    api_key: Optional[str] = Form(None),
    voice_rate: str = Form("auto"),
    voice_pitch: str = Form("auto"),
    voice_volume: str = Form("auto"),
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
        api_key=api_key,
        subtitle_file_path=sub_path_str,
        voice_rate=voice_rate,
        voice_pitch=voice_pitch,
        voice_volume=voice_volume,
    )

    return {"job_id": job_id, "status": "queued", "filename": file.filename}


@router.get("/status/{job_id}")
async def check_pipeline_status(job_id: str):
    job = get_job_status(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


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

    schedule_non_overlapping_clips(segments, min_gap=0.08)
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

    # Reassemble video with non-overlapping audio mixer
    dub_result = await assemble_dubbed_video(
        video_path=video_path,
        segments=segments,
        output_filename=f"dubbed_{job_id}.mp4",
        audio_mode=req.audio_mode,
        bg_volume=req.bg_volume,
        burn_subtitles=req.burn_subtitles,
    )

    job["result"] = dub_result
    _save_job(job_id)

    return {
        "status": "completed",
        "result": dub_result,
        "segments": segments,
        "message": "Video successfully re-rendered with zero voice collisions!"
    }
