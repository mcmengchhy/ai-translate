"""Exercise real narration bundles and replacement renders without changing saved jobs."""

import asyncio
import copy
import io
import json
import shutil
import subprocess
import tempfile
import unittest
import wave
import zipfile
from pathlib import Path
from unittest.mock import patch

import httpx
import numpy as np

from backend.app.main import app
from backend.app.video import media_exports, pipeline


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class MediaExportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="khmer-exports-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for replacement in (
            patch.dict(pipeline.JOBS, {}, clear=True),
            patch.dict(media_exports.TASKS, {}, clear=True),
            patch.object(pipeline, "JOBS_FILE", self.root / "jobs.json"),
            patch.object(pipeline, "RESETTING", False),
            patch.object(media_exports, "EXPORT_DIR", self.root / "exports"),
            patch.object(media_exports, "_video_encoding_args", return_value=["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)
        self.addAsyncCleanup(media_exports.stop_exports)
        self.voice = self.root / "voice.wav"
        self.tone(self.voice, 440, .4)
        self.source = self.root / "source.mp4"
        self.video(self.source, "red", 2.4, 1700)
        self.replacement = self.root / "new.mp4"
        self.video(self.replacement, "blue", .4, 2000)
        self.job = {"id": "one", "title": "../Episode <1>", "status": "completed", "video_path": str(self.source),
                    "result": {"dubbed_video_path": "keep-original-export.mp4", "duration": 2.4},
                    "segments": [{"id": 1, "text": "source dialogue", "translation": "សួស្តី",
                                  "start": .3, "end": 1, "audio_file": str(self.voice), "audio_source": "tts"}]}
        pipeline.JOBS["one"] = self.job

    def tone(self, path, frequency, duration):
        rate = 48000
        samples = (np.sin(2 * np.pi * frequency * np.arange(round(duration * rate)) / rate) * 8000).astype('<i2')
        with wave.open(str(path), "wb") as stream:
            stream.setparams((1, 2, rate, 0, "NONE", "not compressed"))
            stream.writeframes(samples.tobytes())

    def video(self, path, color, duration, frequency):
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                        f"color={color}:s=160x120:r=10:d={duration}", "-f", "lavfi", "-i",
                        f"sine=frequency={frequency}:duration={duration}", "-c:v", "libx264", "-preset", "ultrafast",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)], check=True)

    async def finish(self, response):
        self.assertEqual(response.status_code, 202, response.text)
        export_id = response.json()["id"]
        task = media_exports.TASKS.get(export_id)
        if task:
            await asyncio.wait_for(asyncio.shield(task), 30)
        state = (await self.client.get(f"/api/video/exports/{export_id}")).json()
        self.assertEqual(state["status"], "completed", state)
        return export_id, state

    def audio(self, path):
        raw = subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1",
                                       "-ar", "48000", "-f", "f32le", "pipe:1"])
        return np.frombuffer(raw, dtype=np.float32)

    def assert_khmer_only(self, samples):
        # The saved voice is 440 Hz; source and replacement audio are 1700 and 2000 Hz.
        active = samples[round(.35 * 48000):round(.65 * 48000)]
        spectrum = abs(np.fft.rfft(active))
        frequencies = np.fft.rfftfreq(len(active), 1 / 48000)
        amplitude = lambda hz: spectrum[np.argmin(abs(frequencies - hz))]
        self.assertGreater(amplitude(440), 100)
        self.assertLess(amplitude(1700), amplitude(440) * .01)
        self.assertLess(amplitude(2000), amplitude(440) * .01)
        self.assertLess(np.abs(samples[:round(.2 * 48000)]).max(), .003)

    async def test_batch_zip_contains_each_pure_voice_and_timed_subtitles_without_mutating_jobs(self):
        pipeline.JOBS["two"] = {**copy.deepcopy(self.job), "id": "two", "title": "Episode 2"}
        before = copy.deepcopy(pipeline.JOBS)
        export_id, state = await self.finish(await self.client.post("/api/video/exports", json={"job_ids": ["one", "two", "one"]}))
        response = await self.client.get(f"/api/video/exports/{export_id}/download")
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        self.assertEqual(set(archive.namelist()), {"001/khmer.wav", "001/khmer.srt", "002/khmer.wav", "002/khmer.srt", "manifest.json"})
        self.assertIn("00:00:00,300 -->", archive.read("001/khmer.srt").decode())
        self.assertIn("សួស្តី", archive.read("001/khmer.srt").decode())
        self.assertNotIn(str(self.root), archive.read("manifest.json").decode())
        self.assert_khmer_only(self.audio(media_exports.export_directory(export_id) / "0" / "khmer.wav"))
        self.assertEqual(pipeline.JOBS, before)
        self.assertEqual(len(state["jobs"]), 2)
        self.assertAlmostEqual(state["jobs"][0]["duration"], 2.4, places=2)
        summaries = (await self.client.get("/api/video/export-jobs")).json()["jobs"]
        self.assertTrue(all(job["export_ready"] for job in summaries))
        self.assertNotIn("segments", summaries[0])

    async def test_unready_batches_fail_before_any_export_is_created(self):
        original = copy.deepcopy(self.job)
        for changes in ({"audio_source": "original_fallback"}, {"_dirty": True}, {"tts_error": "failed"},
                        {"audio_file": str(self.root / "missing.wav")}, {"translation": ""}):
            with self.subTest(changes=changes):
                self.job["segments"] = [{**original["segments"][0], **changes}]
                response = await self.client.post("/api/video/exports", json={"job_ids": ["one"]})
                self.assertEqual(response.status_code, 409, response.text)
        self.job.update(original)
        self.job["status"] = "processing"
        self.assertEqual((await self.client.post("/api/video/exports", json={"job_ids": ["one"]})).status_code, 409)
        self.assertFalse(media_exports.EXPORT_DIR.exists())
        self.assertEqual((await self.client.post("/api/video/exports", json={"job_ids": ["absent"]})).status_code, 404)
        for ids in ([], ["one"] * 101):
            self.assertEqual((await self.client.post("/api/video/exports", json={"job_ids": ids})).status_code, 422)

    async def test_loop_and_freeze_replacements_keep_narration_duration_and_discard_both_other_soundtracks(self):
        before = copy.deepcopy(self.job)
        for ending in ("loop", "freeze"):
            with self.subTest(ending=ending):
                response = await self.client.post("/api/video/job/one/replacement-video",
                    files={"file": ("new.mp4", self.replacement.read_bytes(), "video/mp4")},
                    data={"ending": ending, "burn_subtitles": "false"})
                export_id, _ = await self.finish(response)
                result = media_exports.export_directory(export_id) / "0" / "replacement.mp4"
                self.assertAlmostEqual(media_exports.get_media_duration(str(result)), 2.4, delta=.12)
                self.assert_khmer_only(self.audio(result))
                raw = subprocess.check_output(["ffmpeg", "-v", "error", "-ss", "2.1", "-i", str(result),
                                               "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"])
                frame = np.frombuffer(raw, dtype=np.uint8).reshape(120, 160, 3)
                self.assertGreater(frame[:, :, 2].mean(), 240)
                self.assertLess(frame[:, :, 0].mean(), 10)
                download = await self.client.get(f"/api/video/exports/{export_id}/files/0/video")
                self.assertEqual(download.status_code, 200)
        self.assertEqual(self.job, before)

    async def test_burned_khmer_captions_are_visible(self):
        response = await self.client.post("/api/video/job/one/replacement-video",
            files={"file": ("new.mp4", self.replacement.read_bytes())}, data={"burn_subtitles": "true"})
        export_id, _ = await self.finish(response)
        video = media_exports.export_directory(export_id) / "0" / "replacement.mp4"
        raw = subprocess.check_output(["ffmpeg", "-v", "error", "-ss", "0.5", "-i", str(video), "-frames:v", "1",
                                       "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"])
        frame = np.frombuffer(raw, dtype=np.uint8).reshape(120, 160, 3)
        self.assertGreater(np.count_nonzero(frame[75:, :, :].min(axis=2) > 160), 10)

    async def test_bad_upload_and_download_paths_are_reported_without_harming_jobs(self):
        before = copy.deepcopy(self.job)
        response = await self.client.post("/api/video/job/one/replacement-video", files={"file": ("bad.mp4", b"not a video")})
        self.assertEqual(response.status_code, 202)
        export_id = response.json()["id"]
        await media_exports.TASKS[export_id]
        state = media_exports.read_export(export_id)
        self.assertEqual(state["status"], "failed")
        self.assertEqual((await self.client.get(f"/api/video/exports/{export_id}/download")).status_code, 409)
        self.assertEqual((await self.client.get("/api/video/exports/not-an-id")).status_code, 404)
        response = await self.client.post("/api/video/job/one/replacement-video", files={"file": ("../bad.mp4", b"invalid")})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.job, before)

    async def test_exports_survive_reload_and_stale_workers_are_marked_interrupted(self):
        export_id, _ = await self.finish(await self.client.post("/api/video/exports", json={"job_ids": ["one"]}))
        pending = media_exports.create_export([media_exports.snapshot_job("one")])
        media_exports.mark_interrupted_exports()
        self.assertEqual(media_exports.read_export(pending["id"])["status"], "interrupted")
        self.assertEqual(media_exports.read_export(export_id)["status"], "completed")
        states = (await self.client.get("/api/video/exports")).json()["exports"]
        self.assertEqual(len(states), 2)
        self.assertEqual((await self.client.get(f"/api/video/exports/{export_id}/files/-1/audio")).status_code, 404)
        self.assertEqual((await self.client.get(f"/api/video/exports/{export_id}/files/0/secret")).status_code, 422)

    async def test_longer_replacement_is_trimmed_and_silent_video_is_supported(self):
        silent = self.root / "silent.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(self.source), "-an", "-c:v", "copy", str(silent)], check=True)
        self.job["video_path"] = None
        self.job["result"]["duration"] = 1.5
        export_id, _ = await self.finish(await self.client.post("/api/video/job/one/replacement-video",
            files={"file": ("silent.mp4", silent.read_bytes())}, data={"burn_subtitles": "false"}))
        result = media_exports.export_directory(export_id) / "0" / "replacement.mp4"
        self.assertAlmostEqual(media_exports.get_media_duration(str(result)), 1.5, delta=.12)

    async def test_streaming_track_keeps_more_than_fifty_clips(self):
        clips = [{"path": str(self.voice), "start": index * .5, "duration": .4, "seg_id": index} for index in range(55)]
        path = self.root / "many.wav"
        duration = await asyncio.to_thread(media_exports._write_narration, clips, 28, path)
        self.assertEqual(duration, 28)
        samples = self.audio(path)
        self.assertGreater(np.abs(samples[round(27.05 * 48000):round(27.3 * 48000)]).mean(), .1)

    async def test_queued_export_detects_regenerated_voices_instead_of_mismatching_subtitles(self):
        snapshot = media_exports.snapshot_job("one")
        self.tone(self.voice, 900, .6)
        state = media_exports.create_export([snapshot])
        await media_exports.run_export(state, [snapshot])
        self.assertEqual(media_exports.read_export(state["id"])["status"], "failed")
        self.assertIn("voices changed", media_exports.read_export(state["id"])["error"])


if __name__ == "__main__":
    unittest.main()
