"""End-to-End Video Translation and Khmer Voice Dubbing Pipeline."""

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Dict, Any, Optional

from .downloader import download_video
from .transcriber import transcribe_video_audio
from .tts import synthesize_segments
from .dubber import assemble_dubbed_video, get_media_duration, generate_srt_file, generate_vtt_file
from ..engines import get_engine
from ..khmer import normalize_khmer_unicode

import json
import time

logger = logging.getLogger(__name__)

ROOT_PROJECT_DIR = Path(__file__).resolve().parent.parent.parent.parent
JOBS_FILE = ROOT_PROJECT_DIR / "storage" / "jobs.json"
DUBBED_DIR = ROOT_PROJECT_DIR / "storage" / "dubbed"
DUBBED_DIR.mkdir(parents=True, exist_ok=True)
_LAST_SAVE_TIME = 0.0
_PIPELINE_SEMAPHORE = asyncio.Semaphore(1)


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


def _save_job_throttled(job_id: str, force: bool = False, min_interval: float = 3.0):
    """
    Prevents thrashing the disk when processing thousands of segments (e.g. 12,000 cues).
    Only serializes the 8MB+ jobs.json every 3 seconds or on critical milestones.
    """
    global _LAST_SAVE_TIME
    now = time.time()
    if force or (now - _LAST_SAVE_TIME >= min_interval):
        _LAST_SAVE_TIME = now
        _save_job(job_id)


# In-memory and persistent job tracker
JOBS: Dict[str, Dict[str, Any]] = _load_jobs()


def create_job(url_or_file: str, params: Optional[Dict[str, Any]] = None) -> str:
    job_id = str(uuid.uuid4())[:8]
    JOBS[job_id] = {
        "id": job_id,
        "status": "queued",
        "progress": 0,
        "stage": "Queued",
        "title": "Processing video...",
        "input": url_or_file,
        "params": params or {},
        "segments": [],
        "result": None,
        "error": None,
    }
    _save_job_throttled(job_id, force=True)
    return job_id


def get_job_status(job_id: str) -> Optional[Dict[str, Any]]:
    job = JOBS.get(job_id)
    if not job:
        disk_jobs = _load_jobs()
        if job_id in disk_jobs:
            JOBS[job_id] = disk_jobs[job_id]
            job = JOBS[job_id]

    if job:
        if job.get("is_episodic") and job.get("episodes"):
            total_eps = len(job["episodes"])
            completed_eps = sum(1 for ep in job["episodes"] if ep.get("status") == "completed")
            job["completed_episodes_count"] = completed_eps
            job["detail"] = f"{completed_eps} of {total_eps} episodes ready to download"
            if completed_eps == total_eps and total_eps > 0:
                job["status"] = "completed"
                job["progress"] = 100
                job["stage"] = f"All {total_eps} episodes complete!"

        # Dynamic check for live synthesized clips count to reflect accurate real-time progress
        vpath = job.get("video_path")
        if vpath:
            audio_dir = Path(vpath).parent / f"voice_{job_id}"
            if audio_dir.exists():
                segs = job.get("segments", [])
                total = len(segs)
                if total > 0:
                    clip_count = len(list(audio_dir.glob("*.mp3")))
                    if clip_count > 0 and job.get("status") in ("processing", "queued"):
                        job["detail"] = f"{clip_count}/{total} clips synthesized"
                        if "Synthesizing" in str(job.get("stage", "")) or "Waiting in queue" in str(job.get("stage", "")):
                            job["stage"] = f"Synthesizing voice ({clip_count}/{total})"
                            job["status"] = "processing"
                            job["progress"] = max(job.get("progress", 70), min(85, 70 + int((clip_count / max(1, total)) * 15)))

    return job


def get_all_jobs() -> list:
    disk_jobs = _load_jobs()
    # Merge disk jobs without replacing in-flight active jobs in memory
    for jid, dj in disk_jobs.items():
        if jid not in JOBS or JOBS[jid].get("status") not in ("processing", "queued"):
            JOBS[jid] = dj
    return list(JOBS.values())


# Tracking active background tasks per job_id to prevent duplicates
_ACTIVE_JOB_TASKS = set()
_CANCELLED_JOBS = set()
_ACTIVE_TASKS: Dict[str, asyncio.Task] = {}


def cancel_job(job_id: str) -> bool:
    """Cancels an active or queued job immediately and marks it cancelled."""
    _CANCELLED_JOBS.add(job_id)
    job = JOBS.get(job_id)
    if not job:
        disk_jobs = _load_jobs()
        if job_id in disk_jobs:
            JOBS[job_id] = disk_jobs[job_id]
            job = JOBS[job_id]

    if job:
        job["status"] = "cancelled"
        job["stage"] = "Cancelled by user"
        job["error"] = "Process cancelled by user"
        job["detail"] = "The dubbing process was cancelled by the user."
        if job.get("episodes"):
            for ep in job["episodes"]:
                if ep.get("status") in ("processing", "queued"):
                    ep["status"] = "cancelled"
                    ep["stage"] = "Cancelled by user"
        _save_job(job_id)

    task = _ACTIVE_TASKS.get(job_id)
    if task and not task.done():
        logger.info(f"Cancelling active asyncio task for job {job_id}")
        task.cancel()
        return True
    return False


def is_job_cancelled(job_id: str) -> bool:
    """Checks if a job has been requested to cancel."""
    if job_id in _CANCELLED_JOBS:
        return True
    job = JOBS.get(job_id)
    if job and job.get("status") == "cancelled":
        return True
    return False


async def _dub_single_episode(
    job_id: str,
    job: Dict[str, Any],
    ep_data: Dict[str, Any],
    voice_type: str = "multi",
    tone: str = "standard",
    engine_name: str = "auto",
    audio_mode: str = "replace",
    burn_subtitles: bool = False,
    mask_hardsubs: bool = True,
    anti_copyright_video: bool = True,
    mirror_video: bool = False,
    remove_origin_sound: bool = True,
    api_key: Optional[str] = None,
    voice_rate: str = "auto",
    voice_pitch: str = "auto",
    voice_volume: str = "auto",
):
    ep_idx = ep_data["index"]
    ep_src = ep_data["source_video_path"]
    total_eps = len(job.get("episodes", []))

    if is_job_cancelled(job_id):
        raise asyncio.CancelledError(f"Job {job_id} cancelled by user")

    base_prog = int(((ep_idx - 1) / max(1, total_eps)) * 100)
    step_prog = int((1.0 / max(1, total_eps)) * 100)

    # 1. Transcribe Episode Audio
    ep_data["stage"] = "Transcribing speech..."
    job["stage"] = f"Episode {ep_idx}/{total_eps}: Transcribing speech..."
    _save_job_throttled(job_id)

    def _on_stt_progress(pct, speed, eta, detail):
        if is_job_cancelled(job_id):
            raise asyncio.CancelledError(f"Job {job_id} cancelled by user")
        ep_data["progress"] = int(pct * 0.35)
        job["progress"] = base_prog + int((pct / 100.0) * (step_prog * 0.35))
        job["stage"] = f"Episode {ep_idx}/{total_eps}: Transcribing speech ({pct:.0f}%)"
        _save_job_throttled(job_id)

    transcription = await transcribe_video_audio(
        ep_src,
        on_stt_progress=_on_stt_progress,
    )
    if is_job_cancelled(job_id):
        raise asyncio.CancelledError(f"Job {job_id} cancelled by user")
    segments = transcription.get("segments", [])
    ep_data["cues_count"] = len(segments)

    if not segments:
        logger.warning(f"No speech detected in Episode {ep_idx}. Assembling video directly.")
        out_filename = f"dubbed_{job_id}_ep{ep_idx:02d}.mp4"
        dub_res = await assemble_dubbed_video(
            video_path=ep_src,
            segments=[],
            output_filename=out_filename,
            audio_mode=audio_mode,
            burn_subtitles=burn_subtitles,
            mask_hardsubs=mask_hardsubs,
            anti_copyright_video=anti_copyright_video,
            mirror_video=mirror_video,
            remove_origin_sound=remove_origin_sound,
        )
        ep_data["dubbed_video_path"] = dub_res.get("dubbed_video_path")
        ep_data["download_url"] = f"/static/dubbed/{out_filename}"
        ep_data["status"] = "completed"
        ep_data["progress"] = 100
        ep_data["stage"] = "Ready to download"
        return

    # 2. Translate Episode Segments (Chinese -> English Pivot -> Khmer Dubbing)
    if is_job_cancelled(job_id):
        raise asyncio.CancelledError(f"Job {job_id} cancelled by user")
    ep_data["stage"] = f"Translating {len(segments)} dialogue cues..."
    job["stage"] = f"Episode {ep_idx}/{total_eps}: Translating dialogue..."
    _save_job_throttled(job_id)

    from ..engines.dialogue_translator import translate_dialogue_batch_gemini, translate_dialogue_clusters_free
    from ..khmer import batch_polish_segments
    import os

    effective_api_key = (api_key or os.getenv("GEMINI_API_KEY", "")).strip()
    detected_lang = transcription.get("language", "zh")

    if effective_api_key:
        batch_size = 30
        for b in range(0, len(segments), batch_size):
            if is_job_cancelled(job_id):
                raise asyncio.CancelledError(f"Job {job_id} cancelled by user")
            b_chunk = segments[b : b + batch_size]
            try:
                await translate_dialogue_batch_gemini(b_chunk, detected_lang, effective_api_key, tone=tone)
            except Exception as e:
                logger.warning(f"Episode {ep_idx} Gemini batch failed: {e}. Falling back to cluster translation...")
                await translate_dialogue_clusters_free(b_chunk, detected_lang, tone=tone)
            done_cues = min(b + batch_size, len(segments))
            ep_data["progress"] = 35 + int((done_cues / max(1, len(segments))) * 25)
            job["progress"] = base_prog + int(step_prog * (0.35 + (done_cues / max(1, len(segments))) * 0.25))
            job["stage"] = f"Episode {ep_idx}/{total_eps}: Translating ({done_cues}/{len(segments)})"
            _save_job_throttled(job_id)
    else:
        def _on_trans_progress(done_chunks, total_chunks):
            if is_job_cancelled(job_id):
                raise asyncio.CancelledError(f"Job {job_id} cancelled by user")
            pct = done_chunks / max(1, total_chunks)
            ep_data["progress"] = 35 + int(pct * 25)
            job["progress"] = base_prog + int(step_prog * (0.35 + pct * 0.25))
            job["stage"] = f"Episode {ep_idx}/{total_eps}: Translating ({int(pct*100)}%)"
            _save_job_throttled(job_id)

        await translate_dialogue_clusters_free(
            segments,
            detected_lang,
            tone=tone,
            on_progress=_on_trans_progress,
        )

    batch_polish_segments(segments)

    # Deduplicate adjacent duplicate lines and assign target timestamps
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

    # 3. Synthesize Khmer Voiceover (Edge-TTS)
    if is_job_cancelled(job_id):
        raise asyncio.CancelledError(f"Job {job_id} cancelled by user")
    ep_data["stage"] = f"Synthesizing voice ({len(segments)} cues)..."
    job["stage"] = f"Episode {ep_idx}/{total_eps}: Synthesizing voice..."
    _save_job_throttled(job_id)

    audio_dir = Path(ep_src).parent / f"voice_{job_id}_ep{ep_idx:02d}"

    def _on_tts_progress(done, total):
        if is_job_cancelled(job_id):
            raise asyncio.CancelledError(f"Job {job_id} cancelled by user")
        pct = done / max(1, total)
        ep_data["progress"] = 60 + int(pct * 25)
        job["progress"] = base_prog + int(step_prog * (0.60 + pct * 0.25))
        job["stage"] = f"Episode {ep_idx}/{total_eps}: Synthesizing voice ({done}/{total})"
        _save_job_throttled(job_id)

    segments = await synthesize_segments(
        segments,
        audio_dir,
        voice_type=voice_type,
        rate=voice_rate,
        pitch=voice_pitch,
        volume=voice_volume,
        max_concurrency=4,
        enhance=True,
        anti_copyright=True,
        on_progress=_on_tts_progress,
    )

    # 4. Assemble Dubbed Video for Episode
    if is_job_cancelled(job_id):
        raise asyncio.CancelledError(f"Job {job_id} cancelled by user")
    ep_data["stage"] = "Mixing audio & assembling video..."
    job["stage"] = f"Episode {ep_idx}/{total_eps}: Assembling video..."
    _save_job_throttled(job_id)

    out_filename = f"dubbed_{job_id}_ep{ep_idx:02d}.mp4"
    dub_res = await assemble_dubbed_video(
        video_path=ep_src,
        segments=segments,
        output_filename=out_filename,
        audio_mode=audio_mode,
        burn_subtitles=burn_subtitles,
        mask_hardsubs=mask_hardsubs,
        anti_copyright_video=anti_copyright_video,
        mirror_video=mirror_video,
        remove_origin_sound=remove_origin_sound,
    )

    # Generate subtitles
    srt_filename = f"dubbed_{job_id}_ep{ep_idx:02d}_kh.srt"
    vtt_filename = f"dubbed_{job_id}_ep{ep_idx:02d}_kh.vtt"
    srt_path = DUBBED_DIR / srt_filename
    vtt_path = DUBBED_DIR / vtt_filename

    generate_srt_file(segments, srt_path)
    generate_vtt_file(segments, vtt_path)

    # Mark Episode Complete!
    ep_data["status"] = "completed"
    ep_data["progress"] = 100
    ep_data["stage"] = "Ready to download"
    ep_data["dubbed_video_path"] = dub_res.get("dubbed_video_path")
    ep_data["download_url"] = f"/static/dubbed/{out_filename}"
    ep_data["srt_url"] = f"/static/dubbed/{srt_filename}"
    ep_data["vtt_url"] = f"/static/dubbed/{vtt_filename}"
    ep_data["segments"] = segments

    out_p = Path(dub_res.get("dubbed_video_path", ""))
    if out_p.exists():
        ep_data["size_mb"] = round(out_p.stat().st_size / (1024 * 1024), 1)

    logger.info(f"✅ Episode {ep_idx} successfully dubbed: {out_filename} ({ep_data.get('size_mb', 0)} MB)")


async def run_video_dubbing_pipeline(
    job_id: str,
    video_source: str,  # URL or local file path
    is_url: bool = True,
    voice_type: str = "multi",
    tone: str = "standard",
    engine_name: str = "auto",
    audio_mode: str = "replace",
    burn_subtitles: bool = False,
    mask_hardsubs: bool = True,
    anti_copyright_video: bool = True,
    mirror_video: bool = False,
    remove_origin_sound: bool = True,
    max_duration: Optional[int] = 0,
    api_key: Optional[str] = None,
    subtitle_file_path: Optional[str] = None,
    voice_rate: str = "auto",
    voice_pitch: str = "auto",
    voice_volume: str = "auto",
    split_duration: Optional[int] = 0,
):
    global _ACTIVE_JOB_TASKS
    if job_id in _ACTIVE_JOB_TASKS:
        logger.warning(f"Job {job_id} already has an active pipeline running. Skipping duplicate invocation.")
        return
    _ACTIVE_JOB_TASKS.add(job_id)

    cur_task = asyncio.current_task()
    if cur_task:
        _ACTIVE_TASKS[job_id] = cur_task

    if is_job_cancelled(job_id):
        _ACTIVE_JOB_TASKS.discard(job_id)
        _ACTIVE_TASKS.pop(job_id, None)
        return

    job = JOBS.get(job_id)
    if not job:
        _ACTIVE_JOB_TASKS.discard(job_id)
        _ACTIVE_TASKS.pop(job_id, None)
        return

    # Preserve job parameters for resume capability
    job["params"] = {
        "voice_type": voice_type,
        "tone": tone,
        "engine_name": engine_name,
        "audio_mode": audio_mode,
        "burn_subtitles": burn_subtitles,
        "mask_hardsubs": mask_hardsubs,
        "anti_copyright_video": anti_copyright_video,
        "mirror_video": mirror_video,
        "remove_origin_sound": remove_origin_sound,
        "max_duration": max_duration,
        "api_key": api_key,
        "subtitle_file_path": subtitle_file_path,
        "voice_rate": voice_rate,
        "voice_pitch": voice_pitch,
        "voice_volume": voice_volume,
        "split_duration": split_duration,
        "is_url": is_url,
    }

    if job.get("status") == "failed" and "Cancelled duplicate" in str(job.get("error", "")):
        _ACTIVE_JOB_TASKS.discard(job_id)
        _ACTIVE_TASKS.pop(job_id, None)
        return

    semaphore_acquired = False
    try:
        if _PIPELINE_SEMAPHORE.locked():
            job["status"] = "queued"
            job["stage"] = "Waiting in queue for active job to finish..."
            _save_job_throttled(job_id, force=True)

        await _PIPELINE_SEMAPHORE.acquire()
        semaphore_acquired = True

        if is_job_cancelled(job_id):
            raise asyncio.CancelledError(f"Job {job_id} cancelled by user while queued")
        if job.get("status") == "completed":
            logger.info(f"Job {job_id} is already completed. Skipping redundant execution.")
            return

        # Step 1: Video Ingestion
        job["status"] = "processing"
        job["error"] = None

        existing_vpath = job.get("video_path")
        if existing_vpath and Path(existing_vpath).exists() and Path(existing_vpath).stat().st_size > 1024 * 1024:
            logger.info(f"Checkpoint Resume: Reusing existing video file at {existing_vpath}")
            video_path = existing_vpath
            job["progress"] = max(job.get("progress", 0), 25)
            job["stage"] = "Video ready (cached)"
            job["detail"] = f"Using cached video ({Path(video_path).name})"
            _save_job_throttled(job_id, force=True)
        else:
            job["stage"] = "Connecting & downloading video..."
            job["progress"] = 5
            job["speed"] = ""
            job["eta"] = ""
            job["detail"] = ""
            _save_job_throttled(job_id, force=True)

            if is_url:
                def _on_download_progress(pct, speed, eta, detail):
                    if is_job_cancelled(job_id):
                        raise asyncio.CancelledError(f"Job {job_id} cancelled by user")
                    job["progress"] = 5 + int(pct * 0.20)
                    job["stage"] = f"Downloading video ({pct:.0f}%)"
                    job["speed"] = speed
                    job["eta"] = eta
                    job["detail"] = detail
                    _save_job_throttled(job_id)

                logger.info(f"Downloading video from {video_source} (max {max_duration}s)...")
                dl_info = await download_video(
                    video_source,
                    max_clip_seconds=max_duration,
                    on_progress=_on_download_progress,
                )
                if is_job_cancelled(job_id):
                    raise asyncio.CancelledError(f"Job {job_id} cancelled by user")
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
            job["speed"] = ""
            job["eta"] = ""
            job["detail"] = "Video ready"
            _save_job_throttled(job_id, force=True)

        if is_job_cancelled(job_id):
            raise asyncio.CancelledError(f"Job {job_id} cancelled by user")

        video_dur = get_media_duration(str(video_path))
        job["duration"] = video_dur

        effective_split_dur = int(split_duration) if split_duration is not None else 1800

        # Check if video should be sliced and processed episodically (e.g. 30m by 30m)
        if effective_split_dur > 0 and video_dur > effective_split_dur * 1.15:
            from .splitter import slice_source_video_into_episodes
            logger.info(f"Episodic Mode Activated: Slicing video ({video_dur:.1f}s) into {effective_split_dur}s episodes upfront...")
            job["is_episodic"] = True
            job["split_duration"] = effective_split_dur
            job["stage"] = f"Pre-slicing video into {int(effective_split_dur//60)}-minute episodes..."
            _save_job_throttled(job_id, force=True)

            episodes_dir = Path(video_path).parent / f"episodes_{job_id}"
            raw_episodes = slice_source_video_into_episodes(
                video_path=video_path,
                episode_duration=float(effective_split_dur),
                output_dir=episodes_dir,
                prefix=f"ep_{job_id}",
            )

            job["total_episodes"] = len(raw_episodes)
            existing_eps = {ep["index"]: ep for ep in job.get("episodes", [])}
            episodes_list = []
            for ep in raw_episodes:
                idx = ep["index"]
                if idx in existing_eps and existing_eps[idx].get("status") == "completed":
                    episodes_list.append(existing_eps[idx])
                else:
                    episodes_list.append({
                        "index": idx,
                        "title": ep["title"],
                        "start": ep["start"],
                        "end": ep["end"],
                        "duration": ep["duration"],
                        "source_video_path": str(ep["file"]),
                        "status": "queued",
                        "progress": 0,
                        "stage": "Waiting in queue...",
                        "dubbed_video_path": None,
                        "download_url": None,
                        "srt_url": None,
                        "vtt_url": None,
                        "cues_count": 0,
                    })

            job["episodes"] = episodes_list
            _save_job_throttled(job_id, force=True)

            for ep_idx, ep_data in enumerate(job["episodes"], 1):
                if is_job_cancelled(job_id):
                    raise asyncio.CancelledError(f"Job {job_id} cancelled by user")

                if ep_data.get("status") == "completed" and ep_data.get("dubbed_video_path") and Path(ep_data["dubbed_video_path"]).exists():
                    logger.info(f"Episode {ep_idx} already finished. Skipping.")
                    continue

                ep_data["status"] = "processing"
                job["current_episode"] = ep_idx
                job["stage"] = f"Episode {ep_idx}/{len(job['episodes'])}: {ep_data['title']}"
                _save_job_throttled(job_id, force=True)

                await _dub_single_episode(
                    job_id=job_id,
                    job=job,
                    ep_data=ep_data,
                    voice_type=voice_type,
                    tone=tone,
                    engine_name=engine_name,
                    audio_mode=audio_mode,
                    burn_subtitles=burn_subtitles,
                    mask_hardsubs=mask_hardsubs,
                    anti_copyright_video=anti_copyright_video,
                    mirror_video=mirror_video,
                    remove_origin_sound=remove_origin_sound,
                    api_key=api_key,
                    voice_rate=voice_rate,
                    voice_pitch=voice_pitch,
                    voice_volume=voice_volume,
                )

                _save_job_throttled(job_id, force=True)

            job["status"] = "completed"
            job["stage"] = f"All {len(job['episodes'])} episodes completed!"
            job["progress"] = 100
            job["detail"] = f"All {len(job['episodes'])} episodes ready to watch & download"
            _save_job_throttled(job_id, force=True)
            return

        # Step 2: Audio Transcription & Subtitle Reading (Hardsubs / Softsubs)
        if is_job_cancelled(job_id):
            raise asyncio.CancelledError(f"Job {job_id} cancelled by user")
        existing_segments = job.get("segments", [])
        if existing_segments and len(existing_segments) > 0:
            logger.info(f"Checkpoint Resume: Reusing {len(existing_segments)} existing transcribed segments...")
            segments = existing_segments
            job["progress"] = max(job.get("progress", 0), 50)
            job["stage"] = f"Using {len(segments)} transcribed dialogue cues (cached)"
            job["detail"] = f"{len(segments)} dialogue cues loaded from checkpoint"
            _save_job_throttled(job_id, force=True)
        else:
            job["stage"] = "Transcribing speech audio..."
            _save_job_throttled(job_id, force=True)

            def _on_stt_progress(pct, speed, eta, detail):
                if is_job_cancelled(job_id):
                    raise asyncio.CancelledError(f"Job {job_id} cancelled by user")
                job["progress"] = 25 + int((pct / 100.0) * 12)
                job["stage"] = f"Transcribing speech ({pct:.0f}%)"
                job["speed"] = speed
                job["eta"] = eta
                job["detail"] = detail
                _save_job_throttled(job_id)

            def _on_ocr_progress(done, total, speed="", eta="", detail=""):
                if is_job_cancelled(job_id):
                    raise asyncio.CancelledError(f"Job {job_id} cancelled by user")
                pct = done / max(1, total)
                job["progress"] = 37 + int(pct * 13)
                job["stage"] = f"Reading on-screen subtitles ({done}/{total})"
                job["speed"] = speed
                job["eta"] = eta
                job["detail"] = detail or f"{done}/{total} cues scanned"
                _save_job_throttled(job_id)

            transcription = await transcribe_video_audio(
                video_path,
                external_subtitle_path=subtitle_file_path,
                on_stt_progress=_on_stt_progress,
                on_ocr_progress=_on_ocr_progress,
            )
            if is_job_cancelled(job_id):
                raise asyncio.CancelledError(f"Job {job_id} cancelled by user")
            segments = transcription["segments"]
            job["detected_language"] = transcription.get("language", "auto")
            job["subtitle_source"] = transcription.get("source", "speech_and_hardsub_ocr")
            job["segments"] = segments
            job["progress"] = 50
            job["speed"] = ""
            job["eta"] = ""
            job["detail"] = f"Found {len(segments)} dialogue cues"
            _save_job_throttled(job_id, force=True)

        if not segments:
            raise RuntimeError("No spoken dialogue or speech detected in the video audio track.")

        # Step 3: AI Translation using 2-Step Reasoning Pivot (Chinese -> English Intent -> Khmer Dubbing)
        if is_job_cancelled(job_id):
            raise asyncio.CancelledError(f"Job {job_id} cancelled by user")
        untranslated = [s for s in segments if not s.get("translation") or not str(s.get("translation")).strip()]

        if not untranslated:
            logger.info(f"Checkpoint Resume: All {len(segments)} segments already translated! Skipping Step 3.")
            job["progress"] = max(job.get("progress", 0), 70)
            job["stage"] = f"All {len(segments)} dialogue cues translated (cached)"
            job["detail"] = f"{len(segments)}/{len(segments)} lines translated"
            _save_job_throttled(job_id, force=True)
        else:
            job["stage"] = f"Translating dialogue ({len(segments) - len(untranslated)}/{len(segments)} already done)"
            logger.info(f"Translating {len(untranslated)} remaining segments (out of {len(segments)} total) to Khmer...")

            from ..engines.dialogue_translator import translate_dialogue_batch_gemini, translate_dialogue_clusters_free
            from ..khmer import batch_polish_segments

            import os
            effective_api_key = (api_key or os.getenv("GEMINI_API_KEY", "")).strip()
            detected_lang = job.get("detected_language", "zh")

            if effective_api_key:
                batch_size = 30
                for b in range(0, len(untranslated), batch_size):
                    if is_job_cancelled(job_id):
                        raise asyncio.CancelledError(f"Job {job_id} cancelled by user")
                    b_chunk = untranslated[b : b + batch_size]
                    try:
                        await translate_dialogue_batch_gemini(b_chunk, detected_lang, effective_api_key, tone=tone)
                    except Exception as e:
                        logger.warning(f"Gemini batch failed: {e}. Falling back to cluster translation...")
                        await translate_dialogue_clusters_free(b_chunk, detected_lang, tone=tone)
                    done_cues = (len(segments) - len(untranslated)) + min(b + batch_size, len(untranslated))
                    job["progress"] = 50 + int((done_cues / max(1, len(segments))) * 20)
                    job["stage"] = f"Translating dialogue ({done_cues}/{len(segments)})"
                    job["speed"] = "Gemini 3 Flash"
                    job["detail"] = f"{done_cues}/{len(segments)} lines translated"
                    _save_job_throttled(job_id)
            else:
                def _on_trans_progress(done_chunks, total_chunks):
                    if is_job_cancelled(job_id):
                        raise asyncio.CancelledError(f"Job {job_id} cancelled by user")
                    translated_so_far = (len(segments) - len(untranslated)) + int((done_chunks / max(1, total_chunks)) * len(untranslated))
                    job["progress"] = 50 + int((translated_so_far / max(1, len(segments))) * 20)
                    job["stage"] = f"Translating dialogue ({translated_so_far}/{len(segments)})"
                    _save_job_throttled(job_id)

                await translate_dialogue_clusters_free(
                    untranslated,
                    detected_lang,
                    tone=tone,
                    on_progress=_on_trans_progress,
                )

            batch_polish_segments(untranslated)

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
            _save_job_throttled(job_id, force=True)

        # Step 4: Khmer Speech Synthesis (TTS)
        if is_job_cancelled(job_id):
            raise asyncio.CancelledError(f"Job {job_id} cancelled by user")
        job["stage"] = "Synthesizing Khmer voiceover..."
        job["speed"] = "Edge-TTS Neural"
        job["detail"] = f"Synthesizing {len(segments)} audio segments"
        _save_job_throttled(job_id, force=True)
        logger.info(f"Generating Khmer speech ({voice_type}) for {len(segments)} segments...")
        audio_dir = Path(video_path).parent / f"voice_{job_id}"

        import time
        t_tts_start = time.time()

        def _on_tts_progress(done, total):
            if is_job_cancelled(job_id):
                raise asyncio.CancelledError(f"Job {job_id} cancelled by user")
            elapsed = max(0.1, time.time() - t_tts_start)
            tts_speed = done / elapsed
            eta_val = max(0, (total - done) / max(0.1, tts_speed))
            up_data = {
                "status": "processing",
                "progress": 70 + int((done / max(1, total)) * 15),
                "stage": f"Synthesizing voice ({done}/{total})",
                "speed": f"{tts_speed:.1f} clips/s",
                "eta": f"{int(eta_val)}s" if eta_val > 0 else "< 1s",
                "detail": f"{done}/{total} clips synthesized"
            }
            job.update(up_data)
            if job_id in JOBS:
                JOBS[job_id].update(up_data)
            _save_job_throttled(job_id)

        # Synthesize with studio-quality enhancement & anti-copyright acoustic protection
        segments = await synthesize_segments(
            segments, audio_dir,
            voice_type=voice_type,
            rate=voice_rate,
            pitch=voice_pitch,
            volume=voice_volume,
            max_concurrency=4,
            enhance=True,
            anti_copyright=True,
            on_progress=_on_tts_progress,
        )
        if is_job_cancelled(job_id):
            raise asyncio.CancelledError(f"Job {job_id} cancelled by user")
        job["progress"] = 85
        job["speed"] = ""
        job["eta"] = ""
        job["detail"] = "All audio clips ready"
        _save_job_throttled(job_id, force=True)

        # Step 5: Audio Mixing & Video Dubbing
        if is_job_cancelled(job_id):
            raise asyncio.CancelledError(f"Job {job_id} cancelled by user")
        stage_desc = "Stripping original sound & assembling video..." if remove_origin_sound else "Mixing audio & assembling video..."
        job["stage"] = stage_desc
        job["speed"] = "Streaming Audio Engine"
        job["eta"] = "Assembling..."
        job["detail"] = "Applying pure voiceover shield & subtitle mask"
        _save_job_throttled(job_id, force=True)
        logger.info("Assembling final dubbed video...")
        dub_result = await assemble_dubbed_video(
            video_path=video_path,
            segments=segments,
            output_filename=f"dubbed_{job_id}.mp4",
            audio_mode=audio_mode,
            burn_subtitles=burn_subtitles,
            mask_hardsubs=mask_hardsubs,
            anti_copyright_video=anti_copyright_video,
            mirror_video=mirror_video,
            remove_origin_sound=remove_origin_sound,
        )

        # Optional Episode Slicing (e.g. 1 hour / 3600s chunks)
        if split_duration and split_duration > 0 and dub_result.get("dubbed_video_path"):
            try:
                job["stage"] = f"Splitting dubbed video into {int(split_duration//60)}m parts..."
                _save_job_throttled(job_id, force=True)
                from .splitter import split_video_into_parts
                split_res = split_video_into_parts(
                    video_path=dub_result["dubbed_video_path"],
                    split_duration=float(split_duration),
                    prefix=f"dubbed_{job_id}",
                    segments=segments,
                )
                dub_result["parts"] = split_res.get("parts", [])
                job["parts"] = split_res.get("parts", [])
                logger.info(f"Job {job_id} successfully split into {len(dub_result['parts'])} parts!")
            except Exception as split_err:
                logger.error(f"Failed to split video for job {job_id}: {split_err}")

        job["progress"] = 100
        job["status"] = "completed"
        job["stage"] = "Complete"
        job["result"] = dub_result
        job["segments"] = segments  # Update job with final voice-synchronized timestamps and character voice types
        _save_job_throttled(job_id, force=True)
        logger.info(f"Job {job_id} successfully completed!")

    except asyncio.CancelledError:
        logger.info(f"🛑 Job {job_id} cancelled by user.")
        job["status"] = "cancelled"
        job["stage"] = "Cancelled by user"
        job["error"] = "Process cancelled by user"
        job["detail"] = "Dubbing process was cancelled by the user."
        if job.get("episodes"):
            for ep in job["episodes"]:
                if ep.get("status") in ("processing", "queued"):
                    ep["status"] = "cancelled"
                    ep["stage"] = "Cancelled by user"
        _save_job_throttled(job_id, force=True)
    except Exception as e:
        logger.error(f"Pipeline error for job {job_id}: {e}", exc_info=True)
        job["status"] = "failed"
        job["stage"] = "Failed"
        job["error"] = str(e)
        _save_job_throttled(job_id, force=True)
    finally:
        _ACTIVE_JOB_TASKS.discard(job_id)
        _ACTIVE_TASKS.pop(job_id, None)
        _CANCELLED_JOBS.discard(job_id)
        if semaphore_acquired:
            try:
                _PIPELINE_SEMAPHORE.release()
            except RuntimeError:
                pass
