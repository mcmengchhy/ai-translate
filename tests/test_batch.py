"""Batch admission, worker overlap, restart recovery, and automatic ZIP exports."""

import asyncio
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from backend.app.video import batch, pipeline
from backend.app.video.job_identity import media_hash
from backend.app.video.resources import resource_slot
from scripts import watch_batch


class BatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ("splits", "batches", "exports", "dubbed"):
            (self.root / name).mkdir()
        for replacement in (
            patch.dict(pipeline.JOBS, {}, clear=True), patch.object(pipeline, "JOBS_FILE", self.root / "jobs.json"),
            patch.object(pipeline, "RUNNING_JOBS", set()), patch.dict(batch.TASKS, {}, clear=True),
            patch.object(batch, "SPLITS_DIR", self.root / "splits"), patch.object(batch, "BATCH_DIR", self.root / "batches"),
            patch.object(batch, "EXPORT_DIR", self.root / "exports"), patch.object(batch, "DUBBED_DIR", self.root / "dubbed"),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)
        self.options = {"engine_name": "free", "voice_type": "male", "include_english_reference": False}

    def clips(self, count=8):
        result = []
        for index in range(1, count + 1):
            name = f"part{index:02d}.mp4"
            (batch.SPLITS_DIR / name).write_bytes(f"source {index}".encode())
            result.append(name)
        return result

    async def test_identical_batch_request_returns_existing_jobs(self):
        names = self.clips()
        first = await batch.create_batch(names, self.options)
        second = await batch.create_batch(names, self.options)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(pipeline.JOBS), 8)

    async def test_existing_dialogue_and_previous_export_are_preserved(self):
        name = self.clips(1)[0]
        source = batch.SPLITS_DIR / name
        previous = self.root / "old.mp4"
        previous.write_bytes(b"original rendered video")
        pipeline.JOBS["saved"] = {"id": "saved", "input_sha256": media_hash(source), "status": "completed",
                                  "progress": 100, "segments": [{"id": 99, "text": "你好", "audio_file": "old.mp3"}],
                                  "result": {"dubbed_video_path": str(previous)}}
        created = await batch.create_batch([name], self.options)
        self.assertEqual(created["clips"][0]["job_id"], "saved")
        self.assertEqual(pipeline.JOBS["saved"]["segments"][0]["id"], 99)
        self.assertTrue(previous.exists())
        self.assertTrue((batch.BATCH_DIR / created["id"] / "before_saved.json").exists())
        self.assertNotEqual(pipeline.JOBS["saved"]["output_filename"], previous.name)

    async def test_invalid_paths_and_busy_jobs_are_rejected_before_admission(self):
        with self.assertRaises(ValueError):
            await batch.create_batch(["../outside.mp4"], self.options)
        name = self.clips(1)[0]
        pipeline.JOBS["busy"] = {"status": "processing", "input_sha256": media_hash(batch.SPLITS_DIR / name)}
        with self.assertRaisesRegex(ValueError, "already running"):
            await batch.create_batch([name], self.options)
        self.assertEqual(len(pipeline.JOBS), 1)

    async def test_interrupted_job_keeps_its_progress_when_added_to_a_batch(self):
        name = self.clips(1)[0]
        pipeline.JOBS["saved"] = {"id": "saved", "input_sha256": media_hash(batch.SPLITS_DIR / name),
                                  "status": "interrupted", "progress": 61, "segments": [{"id": 1, "text": "你好"}]}
        created = await batch.create_batch([name], self.options)
        self.assertEqual(created["clips"][0]["job_id"], "saved")
        self.assertEqual(pipeline.JOBS["saved"]["progress"], 61)

    async def test_all_parts_are_packaged_and_silent_sources_are_explicitly_labelled(self):
        created = await batch.create_batch(self.clips(), self.options)
        calls = []
        async def render(job_id, source, **kwargs):
            calls.append(job_id)
            job = pipeline.JOBS[job_id]
            output = batch.DUBBED_DIR / job["output_filename"]
            output.write_bytes(b"dubbed video")
            subtitle = output.with_suffix(".srt")
            subtitle.write_text("Khmer subtitles")
            job.update(status="completed", progress=100, result={"dubbed_video_path": str(output), "srt_path": str(subtitle)})
        with patch.object(batch, "_silent_source", side_effect=lambda path: Path(path).name in {"part07.mp4", "part08.mp4"}), patch.object(pipeline, "_run_video_dubbing_pipeline", new=AsyncMock(side_effect=render)):
            await batch.start_batch(created["id"])
            await batch.TASKS[created["id"]]
        finished = batch.batch_status(created["id"])
        self.assertEqual(finished["status"], "completed")
        self.assertEqual(len(calls), 6)
        self.assertTrue(finished["clips"][6]["source_only"])
        archive = Path(finished["archive_path"])
        self.assertTrue(archive.exists())
        with zipfile.ZipFile(archive) as bundle:
            self.assertEqual(len([name for name in bundle.namelist() if name.endswith(".mp4")]), 8)
            report = json.loads(bundle.read("processing_report.json"))
            self.assertTrue(report[7]["source_only"])
            self.assertEqual(bundle.read("Part07.mp4"), b"source 7")

    async def test_batch_queues_extra_clips_behind_three_workers(self):
        created = await batch.create_batch(self.clips(5), self.options)
        for job in pipeline.JOBS.values():
            job["segments"] = [{"id": 1, "text": "saved dialogue"}]
        started, release = asyncio.Event(), asyncio.Event()
        running = peak = count = 0

        async def render(job_id, source, **kwargs):
            nonlocal running, peak, count
            running += 1
            count += 1
            peak = max(peak, running)
            if count == 3:
                started.set()
            try:
                await release.wait()
                pipeline.JOBS[job_id].update(status="completed", progress=100)
            finally:
                running -= 1

        with patch.object(pipeline.config, "VIDEO_MAX_CONCURRENT_JOBS", 3), patch.object(
            pipeline, "_run_video_dubbing_pipeline", new=AsyncMock(side_effect=render)
        ), patch.object(batch, "_package_batch", return_value="archive.zip"):
            task = asyncio.create_task(batch._run_batch(created["id"]))
            try:
                await asyncio.wait_for(started.wait(), 3)
                self.assertEqual(sum(job["status"] == "processing" for job in pipeline.JOBS.values()), 3)
                self.assertEqual(sum(job["status"] == "queued" for job in pipeline.JOBS.values()), 2)
            finally:
                release.set()
                await asyncio.wait_for(task, 3)
        self.assertEqual(peak, 3)
        self.assertEqual(count, 5)
        self.assertFalse(pipeline.RUNNING_JOBS)
        self.assertEqual(batch.read_batch(created["id"])["status"], "completed")

    async def test_stage_limits_apply_across_jobs(self):
        active = maximum = 0
        async def worker():
            nonlocal active, maximum
            async with resource_slot("test-batch-stage", 2):
                active += 1
                maximum = max(maximum, active)
                await asyncio.sleep(.001)
                active -= 1
        await asyncio.gather(*(worker() for _ in range(8)))
        self.assertEqual(maximum, 2)

    async def test_interrupted_batch_restarts_without_redoing_completed_clips(self):
        created = await batch.create_batch(self.clips(2), self.options)
        created["status"] = "interrupted"
        batch._save_batch(created)
        first = pipeline.JOBS[created["clips"][0]["job_id"]]
        first["status"] = "completed"
        with patch.object(batch, "_run_batch", new=AsyncMock()) as run:
            await batch.restore_batches()
            await batch.TASKS[created["id"]]
        run.assert_awaited_once_with(created["id"], None)
        self.assertEqual(first["status"], "completed")

    async def test_completed_archive_is_verified_and_saved_to_downloads_idempotently(self):
        project = self.root / "project"
        exports = project / "storage/exports/test"
        exports.mkdir(parents=True)
        video = exports / "Part01.mp4"
        video.write_bytes(b"final video")
        archive = exports / "finished.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.write(video, video.name)
        snapshot = {"id": "test", "status": "completed", "archive_path": str(archive), "clips": [{}]}
        downloads = self.root / "downloads"
        with patch.object(watch_batch, "ROOT", project):
            first = watch_batch.save_downloads(snapshot, downloads)
            watch_batch.save_downloads(snapshot, downloads)
        self.assertEqual(first["video_count"], 1)
        self.assertTrue(first["archive_verified"])
        self.assertEqual((downloads / "Part01.mp4").read_bytes(), b"final video")
        self.assertTrue((downloads / "finished.zip").is_file())
        self.assertTrue((downloads / "download_receipt.json").is_file())
