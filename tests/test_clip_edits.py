"""Real uploads verify flipping, color, synchronized speed, and generated effects."""

import asyncio
import copy
import io
import json
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import httpx
import numpy as np

from backend.app.main import app
from backend.app.video import clip_edits, media_exports, pipeline


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class ClipEditTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.media = tempfile.TemporaryDirectory(prefix="clip-editor-media-")
        cls.source = Path(cls.media.name) / "quadrants.mp4"
        pattern = ("color=red:s=160x120:r=20:d=1.2,"
                   "drawbox=x=80:y=0:w=80:h=60:color=green:t=fill,"
                   "drawbox=x=0:y=60:w=80:h=60:color=blue:t=fill,"
                   "drawbox=x=80:y=60:w=80:h=60:color=gray:t=fill")
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", pattern,
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=1.2", "-c:v", "libx264",
                        "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(cls.source)], check=True)
        cls.silent = Path(cls.media.name) / "silent.mp4"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(cls.source), "-an", "-c:v", "copy", str(cls.silent)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.media.cleanup()

    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="clip-editor-tests-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for replacement in (
            patch.object(media_exports, "EXPORT_DIR", self.root / "exports"),
            patch.dict(media_exports.TASKS, {}, clear=True),
            patch.dict(pipeline.JOBS, {"keep": {"id": "keep", "status": "completed"}}, clear=True),
            patch.object(pipeline, "RESETTING", False),
            patch.object(clip_edits, "_video_encoding_args", return_value=["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)
        self.addAsyncCleanup(media_exports.stop_exports)

    async def render(self, source=None, **options):
        source = source or self.source
        response = await self.client.post("/api/video/clip-edits",
            files={"file": (source.name, source.read_bytes(), "video/mp4")}, data=options)
        self.assertEqual(response.status_code, 202, response.text)
        export_id = response.json()["id"]
        task = media_exports.TASKS.get(export_id)
        if task:
            await asyncio.wait_for(asyncio.shield(task), 30)
        state = media_exports.read_export(export_id)
        self.assertEqual(state["status"], "completed", state)
        return state, media_exports.export_directory(export_id) / "0" / "edited.mp4"

    def frame(self, source):
        raw = subprocess.check_output(["ffmpeg", "-v", "error", "-ss", "0.2", "-i", str(source),
                                       "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"])
        return np.frombuffer(raw, dtype=np.uint8).reshape(120, 160, 3).astype(float)

    def samples(self, source):
        raw = subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(source), "-vn", "-ac", "1",
                                       "-ar", "48000", "-f", "f32le", "pipe:1"])
        return np.frombuffer(raw, dtype=np.float32)

    async def test_all_flip_directions_change_the_pixels_and_leave_saved_jobs_intact(self):
        original = self.frame(self.source)
        before = copy.deepcopy(pipeline.JOBS)
        expected = {"none": original, "horizontal": original[:, ::-1], "vertical": original[::-1], "both": original[::-1, ::-1]}
        for flip, wanted in expected.items():
            with self.subTest(flip=flip):
                state, output = await self.render(flip=flip)
                self.assertLess(np.abs(self.frame(output) - wanted).mean(), 4)
                self.assertEqual(state["edit_options"]["flip"], flip)
        self.assertEqual(pipeline.JOBS, before)

    async def test_speed_changes_video_and_audio_duration_without_changing_voice_pitch(self):
        for speed in (.25, .5, 1.25, 2, 4):
            with self.subTest(speed=speed):
                state, output = await self.render(speed=str(speed), flip="none")
                info = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(output)]))
                video = next(stream for stream in info["streams"] if stream["codec_type"] == "video")
                audio = next(stream for stream in info["streams"] if stream["codec_type"] == "audio")
                self.assertAlmostEqual(float(video["duration"]), 1.2 / speed, delta=.08)
                self.assertAlmostEqual(float(audio["duration"]), 1.2 / speed, delta=.05)
                samples = self.samples(output)
                middle = samples[round(.05 * 48000):round((1.2 / speed - .04) * 48000)]
                frequencies = np.fft.rfftfreq(len(middle), 1 / 48000)
                peak = frequencies[np.argmax(abs(np.fft.rfft(middle))) ]
                self.assertAlmostEqual(peak, 440, delta=10)
                self.assertAlmostEqual(state["jobs"][0]["duration"], 1.2 / speed)

    async def test_color_filters_render_and_grayscale_and_sepia_have_expected_channels(self):
        frames = {}
        for color in clip_edits.COLOR_FILTERS:
            with self.subTest(color=color):
                _, output = await self.render(flip="none", color_filter=color)
                frames[color] = self.frame(output)
        gray = frames["grayscale"]
        self.assertLess(np.abs(gray[:, :, 0] - gray[:, :, 2]).mean(), 2)
        sepia = frames["sepia"][75:100, 100:140].mean(axis=(0, 1))
        self.assertGreater(sepia[0], sepia[1])
        self.assertGreater(sepia[1], sepia[2])
        for color in ("vivid", "warm", "cool"):
            self.assertGreater(np.abs(frames[color] - frames["normal"]).mean(), 1)
        for color in ("cinematic", "vintage", "soft", "high_contrast", "muted",
                      "bright", "dark", "golden_hour", "teal_orange", "fade"):
            with self.subTest(rendered_color=color):
                self.assertGreater(np.abs(frames[color] - frames["normal"]).mean(), 1)
        middle = np.s_[75:100, 100:140]
        self.assertGreater(frames["bright"][middle].mean(), frames["normal"][middle].mean())
        self.assertLess(frames["dark"][middle].mean(), frames["normal"][middle].mean())
        golden = frames["golden_hour"][middle].mean(axis=(0, 1))
        self.assertGreater(golden[0], golden[2])

    async def test_muted_and_silent_inputs_work_with_and_without_every_sound_effect(self):
        _, muted = await self.render(original_volume="0")
        self.assertLess(np.abs(self.samples(muted)).max(), .0001)
        _, silent = await self.render(source=self.silent, sound_effect="none")
        self.assertLess(np.abs(self.samples(silent)).max(), .0001)
        for effect in ("ambience", "transitions", "both"):
            with self.subTest(effect=effect):
                _, output = await self.render(source=self.silent, sound_effect=effect, effect_volume="60")
                self.assertGreater(np.abs(self.samples(output)).mean(), .0001)

    async def test_downloads_and_filtered_history_survive_page_reload(self):
        state, output = await self.render(color_filter="cool", speed="1.25")
        media_exports.create_export([], kind="bundle")
        history = (await self.client.get("/api/video/exports?kind=clip-edit")).json()["exports"]
        self.assertEqual([item["id"] for item in history], [state["id"]])
        base = f"/api/video/exports/{state['id']}"
        response = await self.client.get(f"{base}/files/0/video", headers={"Range": "bytes=0-99"})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(len(response.content), 100)
        response = await self.client.get(f"{base}/download")
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        self.assertEqual(set(archive.namelist()), {"edited.mp4", "manifest.json"})
        self.assertEqual(json.loads(archive.read("manifest.json"))["edit_options"]["color_filter"], "cool")
        self.assertEqual((await self.client.get(f"{base}/files/0/audio")).status_code, 404)
        self.assertTrue(output.is_file())

    async def test_invalid_settings_and_filenames_are_rejected(self):
        for settings in ({"speed": "nan"}, {"speed": "inf"}, {"speed": "0"}, {"speed": "5"},
                         {"flip": "hflip;arbitrary"}, {"color_filter": "movie=/etc/passwd"},
                         {"sound_effect": "unknown"}, {"effect_volume": "101"}, {"original_volume": "-1"}):
            with self.subTest(settings=settings):
                response = await self.client.post("/api/video/clip-edits", files={"file": ("clip.mp4", b"invalid")}, data=settings)
                self.assertEqual(response.status_code, 422, response.text)
        self.assertFalse(media_exports.EXPORT_DIR.exists())
        response = await self.client.post("/api/video/clip-edits", files={"file": ("../clip.mp4", b"invalid")})
        self.assertEqual(response.status_code, 400)

    async def test_unplayable_upload_reports_failure_without_a_partial_download(self):
        response = await self.client.post("/api/video/clip-edits", files={"file": ("bad.mp4", b"invalid")})
        self.assertEqual(response.status_code, 202)
        export_id = response.json()["id"]
        await media_exports.TASKS[export_id]
        state = media_exports.read_export(export_id)
        self.assertEqual(state["status"], "failed")
        self.assertIn("playable video", state["error"])
        self.assertEqual((await self.client.get(f"/api/video/exports/{export_id}/files/0/video")).status_code, 409)


if __name__ == "__main__":
    unittest.main()
