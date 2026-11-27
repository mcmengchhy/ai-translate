"""Duplicate submissions, durable recovery, and reusable media checkpoints."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

from backend.app.main import app
from backend.app.api import video_routes
from backend.app.video import pipeline, transcriber


class SubmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "storage"
        self.root.mkdir()
        for replacement in (
            patch.dict(pipeline.JOBS, {}, clear=True),
            patch.dict(pipeline._LAST_CHECKPOINT, {}, clear=True),
            patch.object(pipeline, "RUNNING_JOBS", set()),
            patch.object(pipeline, "JOBS_FILE", self.root / "jobs.json"),
            patch.object(video_routes, "STORAGE_UPLOADS", self.root / "uploads"),
            patch.object(pipeline, "cleanup_processing_cache"),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)

    async def test_concurrent_url_submissions_queue_only_one_worker_and_never_save_api_keys(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def worker(**kwargs):
            started.set()
            await release.wait()
        payload = {"url": "https://example.com/video.mp4", "api_key": "private-test-credential"}
        with patch.object(video_routes, "run_video_dubbing_pipeline", new=AsyncMock(side_effect=worker)) as run:
            first = asyncio.create_task(self.client.post("/api/video/dub-url", json=payload))
            try:
                await asyncio.wait_for(started.wait(), 2)
                duplicate = await self.client.post("/api/video/dub-url", json=payload)
            finally:
                release.set()
                accepted = await first
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(duplicate.status_code, 200)
        self.assertTrue(duplicate.json()["reused"])
        self.assertEqual(duplicate.json()["job_id"], accepted.json()["job_id"])
        self.assertTrue(duplicate.json()["existing_job"]["options_match"])
        self.assertEqual(len(pipeline.JOBS), 1)
        run.assert_awaited_once()
        self.assertNotIn("private-test-credential", pipeline.JOBS_FILE.read_text())

    async def test_identical_upload_with_another_name_reuses_job_and_discards_only_the_new_copy(self):
        worker = AsyncMock()
        with patch.object(video_routes, "run_video_dubbing_pipeline", worker):
            first = await self.client.post("/api/video/dub-upload", files={"file": ("first.mp4", b"same media")})
            duplicate = await self.client.post("/api/video/dub-upload", files={"file": ("renamed.mp4", b"same media")})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(duplicate.status_code, 200)
        self.assertTrue(duplicate.json()["reused"])
        self.assertEqual(duplicate.json()["job_id"], first.json()["job_id"])
        self.assertEqual(len(list((self.root / "uploads").rglob("*.mp4"))), 1)
        worker.assert_awaited_once()

    async def test_same_filename_with_different_content_is_not_a_duplicate_and_new_run_is_explicit(self):
        with patch.object(video_routes, "run_video_dubbing_pipeline", new=AsyncMock()) as worker:
            first = await self.client.post("/api/video/dub-upload", files={"file": ("video.mp4", b"one")})
            changed = await self.client.post("/api/video/dub-upload", files={"file": ("video.mp4", b"two")})
            forced = await self.client.post("/api/video/dub-upload", files={"file": ("video.mp4", b"one")}, data={"force_new": "true"})
        self.assertEqual([first.status_code, changed.status_code, forced.status_code], [200, 200, 200])
        self.assertEqual(len(pipeline.JOBS), 3)
        self.assertEqual(worker.await_count, 3)

    async def test_legacy_job_is_identified_from_file_content(self):
        source = self.root / "old.mp4"
        source.write_bytes(b"legacy media")
        pipeline.JOBS["legacy"] = {"id": "legacy", "input": str(source), "status": "completed", "progress": 100}
        with patch.object(video_routes, "run_video_dubbing_pipeline", new=AsyncMock()) as worker:
            duplicate = await self.client.post("/api/video/dub-upload", files={"file": ("new.mp4", b"legacy media")})
        self.assertEqual(duplicate.status_code, 200)
        self.assertTrue(duplicate.json()["reused"])
        self.assertEqual(duplicate.json()["job_id"], "legacy")
        worker.assert_not_awaited()
        self.assertTrue(source.exists())

    async def test_startup_marks_interrupted_work_without_losing_progress_or_voices(self):
        cue = {"id": 1, "translation": "សួស្តី", "audio_file": "saved.mp3"}
        pipeline.JOBS["saved"] = {"id": "saved", "status": "processing", "progress": 74,
                                  "stage": "Generating voices", "segments": [cue]}
        self.assertEqual(pipeline.mark_interrupted_jobs(), 1)
        job = json.loads(pipeline.JOBS_FILE.read_text())["saved"]
        self.assertEqual(job["status"], "interrupted")
        self.assertEqual(job["progress"], 74)
        self.assertEqual(job["segments"], [cue])
        self.assertEqual(pipeline.mark_interrupted_jobs(), 0)

    async def test_resume_before_dialogue_exists_uses_the_same_job_and_original_options(self):
        source = self.root / "source.mp4"
        source.touch()
        pipeline.JOBS["saved"] = {"id": "saved", "input": str(source), "status": "interrupted", "progress": 35,
                                  "segments": [], "request_options": {"is_url": False, "max_duration": 90,
                                  "subtitle_sha256": "not-a-worker-argument", "engine_name": "free"}}
        with patch.object(video_routes, "run_video_dubbing_pipeline", new=AsyncMock()) as worker:
            response = await self.client.post("/api/video/job/saved/resume", json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(worker.await_args.kwargs["job_id"], "saved")
        self.assertEqual(worker.await_args.kwargs["max_duration"], 90)
        self.assertEqual(worker.await_args.kwargs["video_source"], str(source))
        self.assertNotIn("subtitle_sha256", worker.await_args.kwargs)
        self.assertEqual(len(pipeline.JOBS), 1)
        self.assertEqual(pipeline.JOBS["saved"]["progress"], 35)

    async def test_summary_poll_does_not_resend_dialogue(self):
        pipeline.JOBS["saved"] = {"id": "saved", "status": "processing", "progress": 60,
                                  "segments": [{"text": "large dialogue"}] * 2000}
        response = await self.client.get("/api/video/status/saved?summary=true")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("segments", response.json())
        self.assertEqual(response.json()["segments_count"], 2000)
        self.assertLess(len(response.content), 500)

    async def test_seven_uploads_run_three_pipelines_and_queue_the_rest_without_blocking_status(self):
        release, three_started = asyncio.Event(), asyncio.Event()
        running = peak = started = 0

        async def worker(job_id, video_source, **options):
            nonlocal running, peak, started
            running += 1
            started += 1
            peak = max(peak, running)
            pipeline.JOBS[job_id].update(status="processing", segments=[{"text": "large private dialogue"}])
            if started == 3:
                three_started.set()
            try:
                await release.wait()
                pipeline.JOBS[job_id].update(status="completed", progress=100)
            finally:
                running -= 1

        with patch.object(pipeline.config, "VIDEO_MAX_CONCURRENT_JOBS", 3), patch.object(
            pipeline, "_run_video_dubbing_pipeline", new=AsyncMock(side_effect=worker)
        ) as run:
            requests = [asyncio.create_task(self.client.post("/api/video/dub-upload",
                        files={"file": (f"video{index}.mp4", f"unique media {index}".encode())}))
                        for index in range(7)]
            try:
                await asyncio.wait_for(three_started.wait(), 5)
                for _ in range(100):
                    if len(pipeline.JOBS) == 7:
                        break
                    await asyncio.sleep(.01)
                response = await asyncio.wait_for(self.client.get("/api/video/jobs?recent_limit=0"), 2)
                snapshot = response.json()
                self.assertEqual(snapshot["active_count"], 7)
                self.assertEqual(snapshot["processing_count"], 3)
                self.assertEqual(snapshot["queued_count"], 4)
                self.assertEqual(snapshot["max_concurrent_jobs"], 3)
                self.assertEqual(len(snapshot["jobs"]), 7)
                self.assertNotIn("large private dialogue", response.text)
                self.assertNotIn("input", snapshot["jobs"][0])
                queued_id = next(job["id"] for job in snapshot["jobs"] if job["status"] == "queued")
                await pipeline.run_video_dubbing_pipeline(queued_id, "duplicate.mp4")
                self.assertEqual(run.await_count, 3)
            finally:
                release.set()
                responses = await asyncio.wait_for(asyncio.gather(*requests), 5)
        self.assertTrue(all(response.status_code == 200 for response in responses))
        self.assertEqual(peak, 3)
        self.assertEqual(started, 7)
        self.assertEqual(len({response.json()["job_id"] for response in responses}), 7)
        self.assertFalse(pipeline.RUNNING_JOBS)

    async def test_new_resumed_direct_and_visual_tasks_share_three_workers(self):
        release, three_started = asyncio.Event(), asyncio.Event()
        running = peak = started = 0
        for name in ("new", "resume", "direct", "visual"):
            source = self.root / f"{name}.mp4"
            source.touch()
            pipeline.JOBS[name] = {"id": name, "status": "interrupted", "progress": 61,
                "input": str(source), "video_path": str(source), "segments": [{"id": 1, "text": name}],
                "result": {"dubbed_video_path": str(source)}}
        pipeline.JOBS["new"]["status"] = "queued"
        pipeline.JOBS["visual"]["status"] = "completed"

        async def work(job_id):
            nonlocal running, peak, started
            running += 1
            started += 1
            peak = max(peak, running)
            if started == 3:
                three_started.set()
            try:
                await release.wait()
                pipeline.JOBS[job_id].update(status="completed", progress=100)
                return {"status": "completed"}
            finally:
                running -= 1

        async def new_worker(job_id, *args, **kwargs):
            return await work(job_id)

        async def saved_worker(job_id, req):
            return await work(job_id)

        async def visual_worker(**kwargs):
            return await work("visual")

        with patch.object(pipeline.config, "VIDEO_MAX_CONCURRENT_JOBS", 3), patch.object(
            pipeline, "_run_video_dubbing_pipeline", new=AsyncMock(side_effect=new_worker)
        ), patch.object(video_routes, "_rerender_saved_video", new=AsyncMock(side_effect=saved_worker)), patch.object(
            video_routes, "assemble_dubbed_video", new=AsyncMock(side_effect=visual_worker)
        ):
            tasks = [
                asyncio.create_task(pipeline.run_video_dubbing_pipeline("new", "new.mp4")),
                asyncio.create_task(self.client.post("/api/video/job/resume/resume", json={})),
                asyncio.create_task(self.client.post("/api/video/job/direct/rerender", json={})),
                asyncio.create_task(self.client.post("/api/video/job/visual/visual-update", json={})),
            ]
            try:
                await asyncio.wait_for(three_started.wait(), 5)
                for _ in range(100):
                    if len(pipeline.RUNNING_JOBS) == 4:
                        break
                    await asyncio.sleep(.01)
                snapshot = (await self.client.get("/api/video/jobs?recent_limit=0")).json()
                self.assertEqual(snapshot["processing_count"], 3)
                self.assertEqual(snapshot["queued_count"], 1)
                self.assertEqual(len(snapshot["jobs"]), 4)
                queued = next(job for job in snapshot["jobs"] if job["status"] == "queued")
                duplicate = await self.client.post(f"/api/video/job/{queued['id']}/visual-update", json={})
                self.assertEqual(duplicate.status_code, 409)
            finally:
                release.set()
                results = await asyncio.wait_for(asyncio.gather(*tasks), 5)
        self.assertTrue(all(response.status_code == 200 for response in results[1:]))
        self.assertEqual(peak, 3)
        self.assertEqual(started, 4)
        self.assertTrue(all(job["status"] == "completed" for job in pipeline.JOBS.values()))
        self.assertFalse(pipeline.RUNNING_JOBS)

    async def test_cancelling_a_queued_video_preserves_progress_and_releases_its_reservation(self):
        started, release = asyncio.Event(), asyncio.Event()
        for key in ("first", "waiting"):
            pipeline.JOBS[key] = {"id": key, "status": "queued", "progress": 61}

        async def worker(*args, **kwargs):
            started.set()
            await release.wait()

        with patch.object(pipeline.config, "VIDEO_MAX_CONCURRENT_JOBS", 1), patch.object(
            pipeline, "_run_video_dubbing_pipeline", new=AsyncMock(side_effect=worker)
        ) as run:
            first = asyncio.create_task(pipeline.run_video_dubbing_pipeline("first", "source.mp4"))
            await asyncio.wait_for(started.wait(), 2)
            waiting = asyncio.create_task(pipeline.run_video_dubbing_pipeline("waiting", "other.mp4"))
            try:
                await asyncio.sleep(0)
                self.assertEqual(pipeline.JOBS["waiting"]["status"], "queued")
                waiting.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await waiting
                self.assertEqual(pipeline.JOBS["waiting"]["status"], "interrupted")
                self.assertEqual(pipeline.JOBS["waiting"]["progress"], 61)
                self.assertNotIn("waiting", pipeline.RUNNING_JOBS)
                run.assert_awaited_once()
            finally:
                release.set()
                await first

    async def test_job_list_includes_all_active_jobs_and_only_requested_recent_jobs(self):
        for index, status in enumerate(["queued"] * 5 + ["completed"] * 5):
            pipeline.JOBS[str(index)] = {"id": str(index), "status": status, "created_at": index,
                                         "request_options": {"api_key": "secret"}, "segments": ["dialogue"]}
        response = await self.client.get("/api/video/jobs?recent_limit=2")
        self.assertEqual(len(response.json()["jobs"]), 7)
        self.assertEqual(response.json()["active_count"], 5)
        self.assertNotIn("secret", response.text)
        self.assertNotIn("dialogue", response.text)
        self.assertEqual([job["id"] for job in response.json()["jobs"]][-2:], ["9", "8"])

    async def test_job_list_downloads_only_link_to_existing_outputs(self):
        video = self.root / "ready.mp4"
        video.touch()
        pipeline.JOBS["ready"] = {"id": "ready", "status": "completed", "result": {
            "dubbed_video_path": str(video), "srt_path": str(self.root / "removed.srt")}}
        response = await self.client.get("/api/video/jobs")
        job = response.json()["jobs"][0]
        self.assertEqual(job["download_url"], "/static/dubbed/ready.mp4")
        self.assertNotIn("srt_url", job)

    async def test_job_cards_report_saved_cues_and_translation_errors_without_dialogue(self):
        pipeline.JOBS["cues"] = {"id": "cues", "status": "awaiting_translation", "segments": [
            {"id": 1, "text": "private-source-one", "translation": "សួស្តី"},
            {"id": 2, "text": "private-source-two", "translation": ""},
            {"id": 3, "text": "private-source-three", "translation": "អរគុណ", "translation_error": "provider failed"},
            {"id": 4, "text": "  ", "translation": ""},
            {"id": 5, "text": "private-source-five", "translation": "  "},
        ]}
        pipeline.JOBS["empty"] = {"id": "empty", "status": "queued", "segments": []}
        response = await self.client.get("/api/video/jobs")
        jobs = {job["id"]: job for job in response.json()["jobs"]}
        self.assertEqual(jobs["cues"]["total_cues"], 4)
        self.assertEqual(jobs["cues"]["translated_cues"], 1)
        self.assertEqual(jobs["cues"]["missing_cues"], 3)
        self.assertEqual(jobs["cues"]["missing_cue_ids"], [2, 3, 5])
        self.assertEqual(jobs["empty"]["total_cues"], 0)
        self.assertEqual(jobs["empty"]["missing_cue_ids"], [])
        self.assertNotIn("private-source", response.text)
        self.assertNotIn("provider failed", response.text)
        pipeline.JOBS["cues"]["segments"][1].update(translation="សួស្តី")
        response = await self.client.get("/api/video/jobs")
        updated = next(job for job in response.json()["jobs"] if job["id"] == "cues")
        self.assertEqual(updated["translated_cues"], 2)
        self.assertEqual(updated["missing_cue_ids"], [3, 5])

    async def test_missing_cue_samples_stay_compact_for_long_videos(self):
        pipeline.JOBS["long"] = {"id": "long", "status": "awaiting_translation", "segments": [
            {"id": index, "text": "source", "translation": ""} for index in range(1, 501)
        ]}
        response = await self.client.get("/api/video/jobs")
        job = response.json()["jobs"][0]
        self.assertEqual(job["missing_cues"], 500)
        self.assertEqual(job["missing_cue_ids"], list(range(1, 11)))

    async def test_invalid_rerender_request_preserves_completed_job_status(self):
        source = self.root / "source.mp4"
        source.touch()
        pipeline.JOBS["saved"] = {"id": "saved", "status": "completed", "progress": 100,
                                  "video_path": str(source), "segments": []}
        response = await self.client.post("/api/video/job/saved/rerender", json={
            "segments": [{"id": 1, "start": 2, "end": 1, "text": "invalid time"}],
        })
        self.assertEqual(response.status_code, 422)
        self.assertEqual(pipeline.JOBS["saved"]["status"], "completed")
        self.assertEqual(pipeline.JOBS["saved"]["progress"], 100)
        self.assertNotIn("saved", pipeline.RUNNING_JOBS)

    async def test_stale_editor_snapshot_keeps_newer_generated_voice_metadata(self):
        clip = self.root / "new-voice.mp3"
        clip.write_bytes(b"saved Khmer voice")
        current = {"id": 1, "start": 1, "end": 2, "translation": "សួស្តី", "audio_file": str(clip),
                   "audio_source": "khmer_tts", "prosody": {"rate": "+10%"}}
        job = {"segments": [current], "status": "paused"}
        old_browser = {"id": 1, "start": 1, "end": 2, "translation": "សួស្តី", "voice_type": "male",
                       "audio_file": None, "_dirty": True, "tts_error": "outdated failure"}
        video_routes._apply_segment_updates(job, [old_browser])
        updated = job["segments"][0]
        self.assertEqual(updated["audio_file"], str(clip))
        self.assertEqual(updated["audio_source"], "khmer_tts")
        self.assertEqual(updated["prosody"], {"rate": "+10%"})
        self.assertNotIn("_dirty", updated)
        self.assertNotIn("tts_error", updated)
        self.assertNotIn("needs_rerender", job)

    async def test_stale_browser_cannot_reattach_a_cleared_voice_checkpoint(self):
        current = {"id": 1, "start": 1, "end": 2, "translation": "សួស្តី", "_dirty": True}
        job = {"segments": [current]}
        video_routes._apply_segment_updates(job, [{**current, "audio_file": "old-but-present.mp3", "_dirty": False,
                                                 "audio_source": "khmer_tts", "prosody": {"rate": "+10%"}}])
        self.assertNotIn("audio_file", job["segments"][0])
        self.assertNotIn("audio_source", job["segments"][0])
        self.assertTrue(job["segments"][0]["_dirty"])

    async def test_only_changed_text_or_voice_invalidates_its_cached_clip(self):
        for change in ({"translation": "អរគុណ"}, {"voice_type": "female"}, {"rate": "+20%"}):
            with self.subTest(change=change):
                first = {"id": 1, "start": 1, "end": 2, "translation": "សួស្តី", "audio_file": "one.mp3", "audio_source": "khmer_tts"}
                second = {"id": 2, "start": 3, "end": 4, "translation": "ពីរ", "audio_file": "two.mp3"}
                job = {"segments": [first, second]}
                video_routes._apply_segment_updates(job, [{**first, **change}, dict(second)])
                self.assertIsNone(job["segments"][0]["audio_file"])
                self.assertTrue(job["segments"][0]["_dirty"])
                self.assertEqual(job["segments"][1]["audio_file"], "two.mp3")
                self.assertNotIn("_dirty", job["segments"][1])

    async def test_active_job_rejects_editor_writes_without_discarding_live_voices(self):
        segments = [{"id": 1, "start": 1, "end": 2, "translation": "សួស្តី", "audio_file": "keep.mp3"}]
        pipeline.JOBS["saved"] = {"id": "saved", "status": "processing", "segments": segments}
        response = await self.client.put("/api/video/job/saved/segments", json={"segments": []})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(pipeline.JOBS["saved"]["segments"], segments)

    async def test_repeated_resume_requests_launch_one_worker(self):
        pipeline.JOBS["saved"] = {"id": "saved", "status": "interrupted", "progress": 74,
                                  "segments": [{"id": 1, "text": "你好", "translation": "សួស្តី"}]}
        started, release = asyncio.Event(), asyncio.Event()
        async def blocked(*args):
            started.set()
            await release.wait()
        with patch.object(video_routes, "_rerender_saved_video", new=AsyncMock(side_effect=blocked)) as worker:
            first = asyncio.create_task(self.client.post("/api/video/job/saved/resume", json={}))
            try:
                await asyncio.wait_for(started.wait(), 2)
                duplicate = await self.client.post("/api/video/job/saved/resume", json={})
            finally:
                release.set()
                accepted = await first
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(duplicate.status_code, 409)
        worker.assert_awaited_once()

    async def test_checkpoint_throttling_keeps_live_progress_and_prevents_backward_progress(self):
        pipeline.JOBS["saved"] = {"progress": 60}
        pipeline._LAST_CHECKPOINT["saved"] = 100
        with patch.object(pipeline.time, "monotonic", return_value=101), patch.object(pipeline, "_save_job") as save:
            for count in range(100):
                pipeline.checkpoint_job("saved", 25, f"Reading cue {count}")
            save.assert_not_called()
            self.assertEqual(pipeline.JOBS["saved"]["stage"], "Reading cue 99")
            self.assertEqual(pipeline.JOBS["saved"]["progress"], 60)
            pipeline.checkpoint_job("saved", 70, force=True)
            save.assert_called_once_with("saved")

    async def test_failed_atomic_write_keeps_the_previous_valid_job_file(self):
        pipeline.JOBS_FILE.write_text('{"saved":{"progress":60}}')
        pipeline.JOBS["saved"] = {"progress": 70}
        with patch.object(pipeline.os, "replace", side_effect=OSError("interrupted write")):
            pipeline._save_job("saved")
        self.assertEqual(json.loads(pipeline.JOBS_FILE.read_text())["saved"]["progress"], 60)
        self.assertEqual(list(self.root.glob(".jobs-*.tmp")), [])

    async def test_a_second_worker_cannot_run_the_same_job_and_cancellation_is_checkpointed(self):
        pipeline.JOBS["saved"] = {"id": "saved", "status": "processing", "progress": 61}
        started, release = asyncio.Event(), asyncio.Event()
        async def blocked(*args, **kwargs):
            started.set()
            await release.wait()
        with patch.object(pipeline, "_run_video_dubbing_pipeline", new=AsyncMock(side_effect=blocked)) as worker:
            first = asyncio.create_task(pipeline.run_video_dubbing_pipeline("saved", "source.mp4"))
            await asyncio.wait_for(started.wait(), 2)
            await pipeline.run_video_dubbing_pipeline("saved", "source.mp4")
            worker.assert_awaited_once()
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
        self.assertEqual(pipeline.JOBS["saved"]["status"], "interrupted")
        self.assertEqual(pipeline.JOBS["saved"]["progress"], 61)
        self.assertNotIn("saved", pipeline.RUNNING_JOBS)

    async def test_resuming_saved_dialogue_does_not_repeat_transcription_or_cached_voices(self):
        source = self.root / "source.mp4"
        source.touch()
        voice = self.root / "saved.mp3"
        voice.write_bytes(b"saved audio")
        pipeline.JOBS["saved"] = {"id": "saved", "status": "interrupted", "progress": 75,
                                  "video_path": str(source), "pipeline_phase": "translated", "detected_language": "zh",
                                  "segments": [{"id": 99, "start": 0, "end": 1, "text": "你好",
                                                "translation": "សួស្តី", "audio_file": str(voice)}]}
        with patch.object(pipeline, "transcribe_video_audio", new=AsyncMock()) as transcribe, patch.object(pipeline, "download_video", new=AsyncMock()) as download, patch.object(pipeline, "synthesize_segments", new=AsyncMock()) as synthesize, patch("backend.app.engines.dialogue_translator.translate_dialogue_segments", new=AsyncMock()), patch.object(pipeline, "assemble_dubbed_video", new=AsyncMock(return_value={"download_url": "saved.mp4"})):
            await pipeline.run_video_dubbing_pipeline("saved", str(source), is_url=False)
        transcribe.assert_not_awaited()
        download.assert_not_awaited()
        synthesize.assert_not_awaited()
        self.assertEqual(pipeline.JOBS["saved"]["segments"][0]["id"], 99)
        self.assertEqual(pipeline.JOBS["saved"]["status"], "completed")


class TranscriptCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source.mp4"
        self.source.write_bytes(b"same video bytes")
        self.pcm = self.root / "source.pcm"
        self.pcm.touch()
        self.model = SimpleNamespace(transcribe=Mock(return_value=(
            [SimpleNamespace(start=0, end=1, text="你好")],
            SimpleNamespace(language="zh", language_probability=1),
        )))
        for replacement in (
            patch.object(transcriber, "TRANSCRIPT_CACHE_DIR", self.root / "cache"),
            patch.object(transcriber, "extract_pcm_from_video", return_value=([0] * 16000, self.pcm)),
            patch.object(transcriber, "get_whisper_model", return_value=self.model),
            patch.object(transcriber, "extract_embedded_soft_subtitles", return_value=[]),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)

    async def test_same_content_reuses_transcription_and_ocr_even_after_renaming(self):
        progress = Mock()
        with patch.object(transcriber, "enhance_segments_with_hardsubs", side_effect=lambda path, segments, **kwargs: segments) as ocr:
            first = await transcriber.transcribe_video_audio(str(self.source), on_speech_progress=progress)
            renamed = self.root / "renamed.mp4"
            renamed.write_bytes(self.source.read_bytes())
            second = await transcriber.transcribe_video_audio(str(renamed))
        self.assertEqual(first["segments"], second["segments"])
        self.assertTrue(second["cache_hit"])
        self.model.transcribe.assert_called_once()
        ocr.assert_called_once()
        progress.assert_called_with(1, 1)

    async def test_cleared_transcription_gets_a_new_cache_without_invalidating_other_jobs(self):
        with patch.object(transcriber, "enhance_segments_with_hardsubs", side_effect=lambda path, segments, **kwargs: segments) as ocr:
            first = await transcriber.transcribe_video_audio(str(self.source))
            rebuilt = await transcriber.transcribe_video_audio(str(self.source), cache_revision="restart-one")
            resumed = await transcriber.transcribe_video_audio(str(self.source), cache_revision="restart-one")
            other_job = await transcriber.transcribe_video_audio(str(self.source))
        self.assertEqual(self.model.transcribe.call_count, 2)
        self.assertEqual(ocr.call_count, 2)
        self.assertEqual(first["segments"], rebuilt["segments"])
        self.assertTrue(resumed["cache_hit"])
        self.assertTrue(other_job["cache_hit"])

    async def test_restart_during_ocr_reuses_saved_speech(self):
        with patch.object(transcriber, "enhance_segments_with_hardsubs", side_effect=RuntimeError("interrupted OCR")):
            with self.assertRaisesRegex(RuntimeError, "interrupted OCR"):
                await transcriber.transcribe_video_audio(str(self.source))
        with patch.object(transcriber, "enhance_segments_with_hardsubs", side_effect=lambda path, segments, **kwargs: segments):
            result = await transcriber.transcribe_video_audio(str(self.source))
        self.assertEqual(result["segments"][0]["text"], "你好")
        self.model.transcribe.assert_called_once()

    async def test_changed_subtitles_refresh_fusion_but_reuse_speech(self):
        subtitle = self.root / "subs.srt"
        subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\n字幕一\n")
        first = await transcriber.transcribe_video_audio(str(self.source), external_subtitle_path=str(subtitle))
        subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\n字幕二\n")
        second = await transcriber.transcribe_video_audio(str(self.source), external_subtitle_path=str(subtitle))
        self.assertEqual(first["segments"][0]["subtitle_text"], "字幕一")
        self.assertEqual(second["segments"][0]["subtitle_text"], "字幕二")
        self.model.transcribe.assert_called_once()
