"""Durable batches of existing split clips, with automatic local exports."""

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
import zipfile
from pathlib import Path

from . import pipeline
from .job_identity import atomic_json_write, media_hash, processing_key
from .resources import resource_slot
from .checkpoints import ACTIVE_STATUSES

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[3]
SPLITS_DIR = ROOT / "storage" / "splits"
BATCH_DIR = ROOT / "storage" / "batches"
EXPORT_DIR = ROOT / "storage" / "exports"
DUBBED_DIR = ROOT / "storage" / "dubbed"
TASKS = {}
_LOCK = threading.RLock()


def read_batch(batch_id):
    if not re.fullmatch(r"[a-f0-9]{12}", batch_id):
        raise ValueError("Invalid batch ID")
    return json.loads((BATCH_DIR / f"{batch_id}.json").read_text(encoding="utf-8"))


def _save_batch(batch):
    batch["updated_at"] = time.time()
    atomic_json_write(BATCH_DIR / f"{batch['id']}.json", batch)


async def create_batch(filenames, options):
    if not 1 <= len(filenames) <= 8 or len(set(filenames)) != len(filenames):
        raise ValueError("Choose one to eight different split clips")
    paths = []
    for filename in filenames:
        path = (SPLITS_DIR / filename).resolve()
        if Path(filename).name != filename or not path.is_relative_to(SPLITS_DIR.resolve()) or path.suffix.lower() != ".mp4" or not path.is_file():
            raise ValueError("Select an existing MP4 from the split clips")
        paths.append(path)
    digests = await asyncio.gather(*(asyncio.to_thread(media_hash, path) for path in paths))
    signature = hashlib.sha256(json.dumps([digests, processing_key(options)]).encode()).hexdigest()
    with _LOCK:
        BATCH_DIR.mkdir(parents=True, exist_ok=True)
        for existing in BATCH_DIR.glob("*.json"):
            try:
                saved = json.loads(existing.read_text(encoding="utf-8"))
                if saved.get("signature") == signature and not any(clip.get("removed") for clip in saved.get("clips", [])):
                    return saved
            except (OSError, ValueError):
                continue
        matches = []
        for path, digest in zip(paths, digests):
            match = next(((key, job) for key, job in reversed(list(pipeline.JOBS.items()))
                          if job.get("input_sha256") == digest), None)
            if match and (match[0] in pipeline.RUNNING_JOBS or match[1].get("status") in {"processing", "queued"}):
                raise ValueError(f"Job {match[0]} is already running. Wait for it before starting this batch.")
            matches.append(match)
        batch_id = uuid.uuid4().hex[:12]
        batch = {"id": batch_id, "signature": signature, "status": "queued", "created_at": time.time(),
                 "options": {key: value for key, value in options.items() if key != "api_key"}, "clips": []}
        for index, (path, digest, match) in enumerate(zip(paths, digests, matches), 1):
            if match:
                job_id, job = match
                atomic_json_write(BATCH_DIR / batch_id / f"before_{job_id}.json", job)
                # Dialogue from the editor already has stable cue IDs and voice references.
                job["pipeline_phase"] = "translated"
                if job.get("status") == "completed":
                    job.update(progress=0, run_id=uuid.uuid4().hex)
                job.update(status="queued", error=None)
            else:
                job_id = pipeline.create_job(str(path))
                job = pipeline.JOBS[job_id]
            job.update(batch_id=batch_id, batch_index=index, input_sha256=digest,
                       stage=f"Queued — batch part {index}/{len(paths)}", title=path.stem,
                       output_filename=f"dubbed_{job_id}_batch_{batch_id}.mp4",
                       request_options=batch["options"], source_identity="upload:" + digest)
            pipeline._save_job(job_id)
            batch["clips"].append({"index": index, "job_id": job_id, "filename": path.name, "source": str(path)})
        _save_batch(batch)
        return batch


def _silent_source(path):
    result = subprocess.run(["ffmpeg", "-hide_banner", "-v", "info", "-i", str(path), "-vn",
                             "-af", "volumedetect", "-f", "null", "-"], capture_output=True, text=True,
                            check=True, timeout=180)
    match = re.search(r"max_volume:\s*(-?[\d.]+) dB", result.stderr)
    return bool(match and float(match.group(1)) <= -90)


def _link_or_copy(source, destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if os.path.samefile(source, destination):
            return
        destination.unlink()
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def _preserve_silent_clip(job_id, source):
    job = pipeline.JOBS[job_id]
    output = DUBBED_DIR / job["output_filename"]
    _link_or_copy(source, output)
    subtitle = output.with_suffix(".srt")
    subtitle.write_text("", encoding="utf-8")
    note = "No audible speech in the source. Original clip preserved; no Khmer dubbing was generated."
    job.update(status="completed", progress=100, stage="Silent source — original clip preserved", error=None,
               source_only=True, source_note=note, pipeline_phase="completed", segments=[],
               video_path=str(source), result={"dubbed_video_path": str(output), "srt_path": str(subtitle),
               "download_url": f"/static/dubbed/{output.name}", "source_only": True})
    pipeline._save_job(job_id)


def _package_batch(batch):
    destination = EXPORT_DIR / batch["id"]
    destination.mkdir(parents=True, exist_ok=True)
    report = []
    for clip in batch["clips"]:
        job = pipeline.JOBS.get(clip["job_id"], {})
        result = job.get("result") or {}
        row = {"part": clip["index"], "job_id": clip["job_id"], "status": "removed" if clip.get("removed") else job.get("status"),
               "error": job.get("error"), "source_only": job.get("source_only", False),
               "note": job.get("source_note"), "fallback_count": job.get("fallback_count", 0),
               "source_warnings": sum(bool(seg.get("source_warning")) for seg in job.get("segments", []))}
        if job.get("status") == "completed":
            for field, suffix in (("dubbed_video_path", ".mp4"), ("srt_path", ".srt")):
                path = result.get(field)
                if path and Path(path).is_file():
                    _link_or_copy(path, destination / f"Part{clip['index']:02d}{suffix}")
        report.append(row)
    atomic_json_write(destination / "processing_report.json", report)
    (destination / "README.txt").write_text(
        "Chinese to Khmer batch export. Machine translation has not been manually reviewed.\n"
        "See processing_report.json for source disagreements, original-speech fallbacks, and failures.\n"
        "Silent source clips are preserved unchanged and explicitly marked source_only.\n", encoding="utf-8")
    archive = destination / f"KhmerDub_{batch['id']}.zip"
    temporary = archive.with_suffix(".zip.partial")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as bundle:
        for path in sorted(destination.iterdir()):
            if path.is_file() and path not in (archive, temporary):
                bundle.write(path, path.name)
    os.replace(temporary, archive)
    return str(archive)


async def _run_batch(batch_id, api_key=None):
    batch = read_batch(batch_id)
    batch.update(status="processing", error=None)
    _save_batch(batch)
    options = batch["options"]

    async def clip_worker(clip):
        job_id = clip["job_id"]
        job = pipeline.JOBS.get(job_id)
        if clip.get("removed") or not job or job_id in pipeline.DELETED_JOBS:
            clip["removed"] = True
            return
        if job.get("status") in {"completed", "paused"} or job.get("pause_requested"):
            return
        try:
            async with pipeline.video_job_slot(job_id) as acquired:
                if not acquired:
                    if job_id in pipeline.DELETED_JOBS:
                        clip["removed"] = True
                    return
                job.update(status="processing", stage="Waiting for source audio check")
                pipeline._save_job(job_id)
                if not job.get("segments"):
                    async with resource_slot("media_probe", 1):
                        silent = await asyncio.to_thread(_silent_source, clip["source"])
                    if silent:
                        await asyncio.to_thread(_preserve_silent_clip, job_id, clip["source"])
                        return
                for attempt in range(2):
                    await pipeline._run_video_dubbing_pipeline(job_id, clip["source"], is_url=False,
                                                             api_key=api_key, **options)
                    if job.get("status") != "failed":
                        break
                    if attempt == 0:
                        job.update(stage="Retrying saved checkpoint after failure")
                        pipeline._save_job(job_id)
                        await asyncio.sleep(15)
        except asyncio.CancelledError:
            if job_id in pipeline.DELETED_JOBS:
                clip["removed"] = True
                return
            if job.get("pause_requested"):
                job.update(status="paused", stage="Paused — resume saved work")
                pipeline._save_job(job_id)
                return
            job.update(status="interrupted", stage="Batch interrupted — saved work will resume")
            pipeline._save_job(job_id)
            raise
        except Exception as error:
            logger.exception("Batch part %s failed", clip["index"])
            job.update(status="failed", stage="Failed", error=str(error))
            pipeline._save_job(job_id)

    try:
        await asyncio.gather(*(clip_worker(clip) for clip in batch["clips"]))
        # A paused clip can be resumed through the job API while its siblings run.
        while any(not clip.get("removed") and pipeline.JOBS.get(clip["job_id"], {}).get("status") in ACTIVE_STATUSES
                  for clip in batch["clips"]):
            await asyncio.sleep(.2)
        await _finalize_batch(batch)
    except asyncio.CancelledError:
        batch["status"] = "interrupted"
        _save_batch(batch)
        raise
    except Exception as error:
        logger.exception("Batch %s failed", batch_id)
        batch.update(status="failed", error=str(error))
        _save_batch(batch)


async def _finalize_batch(batch):
    if any(not clip.get("removed") and pipeline.JOBS.get(clip["job_id"], {}).get("status") == "paused"
           for clip in batch["clips"]):
        batch["status"] = "paused"
        _save_batch(batch)
        return
    batch["status"] = "packaging"
    _save_batch(batch)
    batch["archive_path"] = await asyncio.to_thread(_package_batch, batch)
    batch["download_url"] = f"/api/video/batch/{batch['id']}/download"
    batch["status"] = "completed" if all(c.get("removed") or pipeline.JOBS.get(c["job_id"], {}).get("status") == "completed"
                                        for c in batch["clips"]) else "completed_with_errors"
    _save_batch(batch)


async def finalize_resumed_job(job):
    batch_id = job.get("batch_id")
    task = TASKS.get(batch_id)
    if not batch_id or (task and not task.done()):
        return
    try:
        batch = read_batch(batch_id)
        if any(pipeline.JOBS.get(clip["job_id"], {}).get("status") in ACTIVE_STATUSES for clip in batch["clips"]):
            return
        await _finalize_batch(batch)
    except (OSError, ValueError):
        logger.exception("Could not finalize resumed batch %s", batch_id)


async def start_batch(batch_id, api_key=None):
    if batch_id in TASKS and not TASKS[batch_id].done():
        return
    TASKS[batch_id] = asyncio.create_task(_run_batch(batch_id, api_key), name=f"video-batch-{batch_id}")


async def restore_batches():
    if not BATCH_DIR.exists():
        return
    for path in BATCH_DIR.glob("*.json"):
        try:
            batch = read_batch(path.stem)
            if batch.get("status") in {"queued", "processing", "interrupted", "packaging"}:
                await start_batch(batch["id"])
        except (OSError, ValueError):
            logger.exception("Could not restore batch %s", path.name)


async def stop_batches():
    active = [task for task in TASKS.values() if not task.done()]
    for task in active:
        task.cancel()
    await asyncio.gather(*active, return_exceptions=True)


def batch_status(batch_id):
    batch = read_batch(batch_id)
    return {**batch, "clips": [{**clip, **{field: pipeline.JOBS.get(clip["job_id"], {}).get(field)
            for field in ("status", "progress", "stage", "error", "source_only", "source_note", "fallback_count")}}
            for clip in batch["clips"]]}


def detach_job(job_id):
    """Keep a removed clip out of durable batch recovery."""
    if not BATCH_DIR.exists():
        return
    with _LOCK:
        for path in BATCH_DIR.glob("*.json"):
            batch = read_batch(path.stem)
            changed = False
            for clip in batch.get("clips", []):
                if clip["job_id"] == job_id:
                    clip["removed"] = True
                    changed = True
            if changed:
                _save_batch(batch)
