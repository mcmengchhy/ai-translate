"""Durable per-card mixing, active-worker updates, and rendered sound levels."""

import asyncio
import copy
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import numpy as np

from backend.app.main import app
from backend.app.api import video_routes
from backend.app.video import dubber, pipeline
from backend.app.video.audio_mix import AudioMixSettings, saved_audio_mix


class AudioMixApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for replacement in (
            patch.dict(pipeline.JOBS, {}, clear=True),
            patch.dict(pipeline._LAST_CHECKPOINT, {}, clear=True),
            patch.object(pipeline, "JOBS_FILE", self.root / "jobs.json"),
            patch.object(pipeline, "RESETTING", False),
            patch.object(pipeline, "cleanup_processing_cache"),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)
        self.job = {"id": "one", "status": "processing", "progress": 74,
                    "pipeline_phase": "translated", "stage": "Generating Khmer voices",
                    "segments": [{"id": 1, "start": 0, "end": 1, "text": "source",
                                  "translation": "សួស្តី", "audio_file": "saved.mp3"}]}
        pipeline.JOBS["one"] = self.job

    async def save(self, **values):
        return await self.client.patch("/api/video/job/one/audio-mix", json=values)

    async def test_save_during_voices_preserves_progress_and_persists_only_this_job(self):
        other = copy.deepcopy(self.job)
        pipeline.JOBS["other"] = other
        segments = copy.deepcopy(self.job["segments"])
        response = await self.save(original_volume=2, sound_effect="ambience")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.job["progress"], 74)
        self.assertEqual(self.job["status"], "processing")
        self.assertEqual(self.job["segments"], segments)
        self.assertNotIn("audio_mix", other)
        saved = json.loads(pipeline.JOBS_FILE.read_text())["one"]
        self.assertEqual(saved["audio_mix"]["original_volume"], 2)
        response = await self.save(sound_effect="both")
        self.assertEqual(response.json()["audio_mix"]["original_volume"], 2)
        summaries = (await self.client.get("/api/video/jobs")).json()["jobs"]
        summary = next(job for job in summaries if job["id"] == "one")
        self.assertEqual(summary["audio_mix"]["sound_effect"], "both")
        self.assertNotIn("segments", summary)
        self.assertFalse(summary["audio_mix_locked"])

    async def test_render_in_progress_rejects_updates_without_mutating_settings(self):
        self.job.update(pipeline_phase="rendering", audio_mix=AudioMixSettings().model_dump())
        before = copy.deepcopy(self.job)
        response = await self.save(original_volume=3)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.job, before)

    async def test_invalid_settings_and_unknown_jobs_are_rejected(self):
        for values in ({"original_volume": -1}, {"original_volume": 101}, {"original_volume": 2.5},
                       {"original_volume": True}, {"effect_volume": 101}, {"sound_effect": "unknown"},
                       {"sound_effect": None}, {"filename": "../../recording.mp3"}):
            with self.subTest(values=values):
                self.assertEqual((await self.save(**values)).status_code, 422)
        self.assertEqual((await self.client.patch("/api/video/job/missing/audio-mix", json={})).status_code, 404)
        self.assertNotIn("audio_mix", self.job)

    async def test_finished_video_is_retained_until_explicit_remix_with_a_new_filename(self):
        self.job.update(status="completed", result={"dubbed_video_path": "old.mp4"})
        response = await self.save(original_volume=2)
        self.assertTrue(response.json()["needs_audio_remix"])
        self.assertEqual(self.job["result"]["dubbed_video_path"], "old.mp4")
        source = self.root / "source.mp4"
        source.touch()
        self.job["video_path"] = str(source)
        renderer = AsyncMock(return_value={"dubbed_video_path": "new.mp4"})
        with patch("backend.app.video.recovery.prepare_dialogue_audio", new=AsyncMock(return_value={"fallback_count": 0})), \
             patch.object(video_routes, "assemble_dubbed_video", renderer):
            response = await self.client.post("/api/video/job/one/resume", json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(renderer.await_args.kwargs["audio_mix"]["original_volume"], 2)
        self.assertIn("_mix_", renderer.await_args.kwargs["output_filename"])
        self.assertFalse(self.job["needs_audio_remix"])

    async def test_changes_while_voices_are_running_reach_the_final_renderer(self):
        self.job["result"] = {"dubbed_video_path": "previous-export.mp4"}
        started, release = asyncio.Event(), asyncio.Event()
        async def voices(*args, **kwargs):
            started.set()
            await release.wait()
            return {"fallback_count": 0}
        renderer = AsyncMock(return_value={})
        with patch("backend.app.video.recovery.prepare_dialogue_audio", new=AsyncMock(side_effect=voices)), \
             patch.object(video_routes, "assemble_dubbed_video", renderer):
            task = asyncio.create_task(video_routes._finish_job_render("one", self.job, "source.mp4", video_routes.JobRerenderRequest()))
            try:
                await asyncio.wait_for(started.wait(), 2)
                self.assertEqual((await self.save(original_volume=2, sound_effect="both")).status_code, 200)
            finally:
                release.set()
                await task
        self.assertEqual(renderer.await_args.kwargs["audio_mix"]["sound_effect"], "both")
        self.assertEqual(renderer.await_args.kwargs["audio_mix"]["original_volume"], 2)
        self.assertIn("_mix_", renderer.await_args.kwargs["output_filename"])

    def test_legacy_levels_and_auto_ducking_are_displayed_truthfully(self):
        for mode, volume, duck in (("replace", 0, False), ("ducking", 10, False),
                                   ("duck_subtle", 5, False), ("smart_sidechain", 15, True)):
            mix = saved_audio_mix({"request_options": {"audio_mode": mode}})
            self.assertEqual((mix["original_volume"], mix["duck_original"]), (volume, duck))


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class AudioMixMediaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source.mp4"
        self.silent_source = self.root / "no-audio.mp4"
        self.voice = self.root / "voice.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=s=96x64:r=10:d=2",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", "-t", "2", str(self.source)], check=True)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(self.source), "-an", "-c:v", "copy", str(self.silent_source)], check=True)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=880:duration=2", str(self.voice)], check=True)

    def pcm(self, media):
        data = subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(media), "-t", "2", "-ar", "48000",
                                        "-ac", "1", "-f", "f32le", "-"])
        return np.frombuffer(data, dtype=np.float32)

    def amplitude(self, samples, frequency):
        # Ignore AAC encoder edges when comparing the independent test tones.
        samples = samples[4800:86400]
        return abs(np.mean(samples * np.exp(-2j * np.pi * frequency * np.arange(len(samples)) / 48000))) * 2

    async def render(self, mix, source=None, voice=True, name="mix"):
        cues = [{"id": 1, "start": 0, "end": 2, "text": "source", "translation": "សួស្តី", "audio_file": str(self.voice)}] if voice else []
        with patch.object(dubber, "DUBBED_DIR", self.root):
            result = await dubber.assemble_dubbed_video(str(source or self.source), cues, name + ".mp4", audio_mix=mix)
        return self.pcm(result["dubbed_video_path"])

    async def test_exact_two_percent_source_and_full_volume_narration(self):
        original = self.pcm(self.source)
        muted = await self.render(AudioMixSettings(original_volume=0).model_dump(), name="muted")
        mixed = await self.render(AudioMixSettings(original_volume=2).model_dump())
        self.assertAlmostEqual(self.amplitude(mixed, 440) / self.amplitude(original, 440), .02, delta=.003)
        self.assertAlmostEqual(self.amplitude(mixed, 880) / self.amplitude(muted, 880), 1, delta=.04)

    async def test_effects_are_audible_with_original_muted_and_without_an_audio_stream(self):
        for effect in ("ambience", "transitions", "both"):
            with self.subTest(effect=effect):
                pcm = await self.render(AudioMixSettings(original_volume=2, sound_effect=effect).model_dump(),
                                        source=self.silent_source, voice=False, name=effect)
                self.assertGreater(float(np.sqrt(np.mean(pcm ** 2))), .0001)

    async def test_smart_ducking_and_generated_effects_can_render_together(self):
        pcm = await self.render(AudioMixSettings(original_volume=15, duck_original=True, sound_effect="both").model_dump())
        self.assertGreater(self.amplitude(pcm, 880), .02)
