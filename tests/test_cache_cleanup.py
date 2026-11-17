"""Deletion boundaries, concurrency, and cleanup before multipart body parsing."""

import asyncio
import json
import os
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from backend.app import main
from backend.app.api import video_routes
from backend.app.video import pipeline, dubber
from backend.app.video.cache import cache_in_use, cleanup_processing_cache


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "storage"
        self.root.mkdir()
        self.jobs = {"done": {"id": "done", "status": "completed"}}

    def file(self, relative, old=False):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"preserve or regenerate")
        if old:
            os.utime(path, (time.time()-7200,)*2)
        return path

    def test_only_allowlisted_intermediates_are_deleted(self):
        disposable = [self.file("dubbed/temp_dubbed_done/"+name) for name in
                      ("master_khmer_voice.wav", "fitted_lip_1_1.45.wav", "silence.wav")]
        keep = [self.file(path) for path in (
            "uploads/source.mp4", "downloads/voice_done/seg_1_male.mp3", "dubbed/dubbed_done.mp4",
            "dubbed/dubbed_done.srt", "jobs.json", "splits/part01.mp4",
            "dubbed/temp_dubbed_done/custom.wav", "dubbed/temp_dubbed_done/notes.txt")]
        size = sum(path.stat().st_size for path in disposable)
        report = cleanup_processing_cache(self.root, self.jobs)
        self.assertEqual(report.deleted_files, 3)
        self.assertEqual(report.freed_bytes, size)
        self.assertTrue(all(not path.exists() for path in disposable))
        self.assertTrue(all(path.exists() for path in keep))

    def test_active_jobs_and_leased_render_folders_are_protected(self):
        active = self.file("dubbed/temp_dubbed_active/master_khmer_voice.wav", old=True)
        overlap = self.file("dubbed/temp_done/fitted_lip_1_1.45.wav", old=True)
        self.jobs["active"] = {"id": "active", "status": "processing"}
        with cache_in_use(overlap.parent):
            report = cleanup_processing_cache(self.root, self.jobs)
            self.assertEqual(report.skipped_active, 2)
            self.assertTrue(overlap.exists())
        cleanup_processing_cache(self.root, self.jobs)
        self.assertFalse(overlap.exists())
        self.assertTrue(active.exists())

    def test_finished_batch_intermediates_are_reclaimed_without_waiting_for_orphan_age(self):
        cache = self.file("dubbed/temp_dubbed_done_batch_123456abcdef/master_khmer_voice.wav")
        report = cleanup_processing_cache(self.root, self.jobs)
        self.assertEqual(report.deleted_files, 1)
        self.assertFalse(cache.exists())

    def test_referenced_audio_and_nested_episode_artifacts_are_preserved(self):
        voice = self.file("dubbed/temp_dubbed_done/fitted_lip_1_1.45.wav")
        self.jobs["done"]["episodes"] = [{"segments": [{"audio_file": str(voice)}]}]
        report = cleanup_processing_cache(self.root, self.jobs)
        self.assertEqual(report.skipped_referenced, 1)
        self.assertTrue(voice.exists())

    def test_unknown_caches_and_pcm_have_a_grace_period_and_active_sources_are_protected(self):
        fresh = self.file("dubbed/temp_unknown/master_khmer_voice.wav")
        stale = self.file("dubbed/temp_old/master_khmer_voice.wav", old=True)
        pcm = self.file("uploads/orphan/source_16k.pcm", old=True)
        active_pcm = self.file("uploads/live/source_16k.pcm", old=True)
        self.jobs["active"] = {"id": "active", "status": "queued", "input": str(active_pcm.parent / "source.mp4")}
        report = cleanup_processing_cache(self.root, self.jobs)
        self.assertEqual(report.deleted_files, 2)
        self.assertTrue(fresh.exists())
        self.assertFalse(stale.exists())
        self.assertFalse(pcm.exists())
        self.assertTrue(active_pcm.exists())

    def test_symlink_files_and_directories_cannot_escape_storage(self):
        outside = self.root.parent / "outside"
        outside.mkdir()
        target = outside / "master_khmer_voice.wav"
        target.write_bytes(b"never delete")
        link = self.root / "dubbed/temp_dubbed_done/master_khmer_voice.wav"
        link.parent.mkdir(parents=True)
        link.symlink_to(target)
        (link.parent.parent / "temp_old").symlink_to(outside, target_is_directory=True)
        (self.root / "uploads").symlink_to(outside, target_is_directory=True)
        self.assertEqual(cleanup_processing_cache(self.root, self.jobs).deleted_files, 0)
        self.assertTrue(target.exists())
        self.assertTrue(link.is_symlink())

    def test_concurrent_cleanups_delete_each_file_only_once(self):
        for index in range(20):
            self.file(f"dubbed/temp_dubbed_done/fitted_lip_{index}_1.45.wav")
        with ThreadPoolExecutor(max_workers=2) as executor:
            reports = list(executor.map(lambda _: cleanup_processing_cache(self.root, self.jobs), range(2)))
        self.assertEqual(sum(report.deleted_files for report in reports), 20)
        self.assertEqual(sum(report.errors for report in reports), 0)

    def test_unlink_failure_does_not_abort_remaining_cleanup(self):
        locked = self.file("dubbed/temp_dubbed_done/master_khmer_voice.wav")
        other = self.file("dubbed/temp_dubbed_done/silence.wav")
        unlink = Path.unlink
        def remove(path, *args, **kwargs):
            if path == locked:
                raise PermissionError("busy")
            return unlink(path, *args, **kwargs)
        with patch.object(Path, "unlink", remove):
            report = cleanup_processing_cache(self.root, self.jobs)
        self.assertEqual((report.errors, report.deleted_files), (1, 1))
        self.assertTrue(locked.exists())
        self.assertFalse(other.exists())

    def test_corrupt_job_metadata_stops_deletion(self):
        cache = self.file("dubbed/temp_dubbed_done/master_khmer_voice.wav", old=True)
        jobs_file = self.file("jobs.json")
        with patch.object(pipeline, "JOBS_FILE", jobs_file), patch.dict(pipeline.JOBS, self.jobs, clear=True):
            report = pipeline.cleanup_saved_job_caches()
        self.assertEqual(report.errors, 1)
        self.assertTrue(cache.exists())

    def test_persisted_active_job_overrides_stale_completed_memory(self):
        cache = self.file("dubbed/temp_dubbed_done/master_khmer_voice.wav", old=True)
        jobs_file = self.root / "jobs.json"
        jobs_file.write_text(json.dumps({"done": {"id": "done", "status": "processing"}}))
        with patch.object(pipeline, "JOBS_FILE", jobs_file), patch.dict(pipeline.JOBS, self.jobs, clear=True):
            report = pipeline.cleanup_saved_job_caches()
        self.assertEqual(report.deleted_files, 0)
        self.assertTrue(cache.exists())


class CleanupIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_render_worker_holds_lease_until_media_processing_returns(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "dubbed/temp_dubbed_test/master_khmer_voice.wav"
            jobs = {"test": {"id": "test", "status": "completed"}}
            def render(*args):
                cache.parent.mkdir(parents=True)
                cache.write_bytes(b"in use by ffmpeg")
                report = cleanup_processing_cache(root, jobs)
                self.assertEqual(report.skipped_active, 1)
                self.assertTrue(cache.exists())
                return {}
            with patch.object(dubber, "DUBBED_DIR", root / "dubbed"), patch.object(dubber, "_assemble_dubbed_video", side_effect=render):
                await dubber.assemble_dubbed_video("source.mp4", [], output_filename="dubbed_test.mp4")
            self.assertEqual(cleanup_processing_cache(root, jobs).deleted_files, 1)

    async def test_cleanup_happens_before_upload_body_is_consumed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "dubbed/temp_dubbed_old/master_khmer_voice.wav"
            cache.parent.mkdir(parents=True)
            cache.write_bytes(b"temporary")
            events = []
            def cleanup():
                events.append("cleanup")
                return cleanup_processing_cache(root, {"old": {"id": "old", "status": "completed"}})
            class UploadStream(httpx.AsyncByteStream):
                async def __aiter__(self):
                    events.append("body")
                    self_test.assertFalse(cache.exists())
                    yield b'--clip\r\nContent-Disposition: form-data; name="file"; filename="movie.mp4"\r\nContent-Type: video/mp4\r\n\r\nvideo\r\n--clip--\r\n'
            self_test = self
            worker = AsyncMock()
            with patch.object(main, "cleanup_saved_job_caches", side_effect=cleanup), patch.object(video_routes, "STORAGE_UPLOADS", root / "uploads"), patch.object(video_routes, "create_job", return_value="new"), patch.object(video_routes, "run_video_dubbing_pipeline", worker):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
                    response = await client.post("/api/video/dub-upload", headers={"Content-Type": "multipart/form-data; boundary=clip"}, content=UploadStream())
            self.assertEqual(response.status_code, 200)
            self.assertEqual(events, ["cleanup", "body"])
            worker.assert_awaited_once()
            self.assertEqual(Path(worker.await_args.kwargs["video_source"]).read_bytes(), b"video")

    async def test_startup_runs_cleanup(self):
        with patch.object(main, "cleanup_saved_job_caches") as cleanup, patch.object(main, "mark_interrupted_jobs") as recover, patch.object(main, "restore_batches", new=AsyncMock()), patch.object(main, "stop_batches", new=AsyncMock()):
            async with main.lifespan(main.app):
                cleanup.assert_called_once()
                recover.assert_called_once()

    async def test_completed_rerender_clears_intermediates_preserving_export(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "uploads/source.mp4"
            source.parent.mkdir(parents=True)
            source.touch()
            cache = root / "dubbed/temp_dubbed_test/master_khmer_voice.wav"
            export = root / "dubbed/dubbed_test.mp4"
            job = {"id": "test", "status": "failed", "video_path": str(source), "segments": []}
            async def render(**kwargs):
                cache.parent.mkdir(parents=True)
                cache.write_bytes(b"temporary")
                export.write_bytes(b"finished")
                return {"dubbed_video_path": str(export)}
            with patch.object(video_routes, "get_job_status", return_value=job), patch.object(video_routes, "_save_job"), patch.object(video_routes, "assemble_dubbed_video", new=AsyncMock(side_effect=render)), patch.object(video_routes, "cleanup_saved_job_caches", side_effect=lambda: cleanup_processing_cache(root, {"test": job})):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
                    response = await client.post("/api/video/job/test/rerender", json={})
            self.assertEqual(response.status_code, 200)
            self.assertFalse(cache.exists())
            self.assertTrue(export.exists())
            self.assertTrue(source.exists())
