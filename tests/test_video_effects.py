import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import numpy as np
from PIL import Image
from pydantic import ValidationError

from backend.app.main import app
from backend.app.api import video_routes
from backend.app.khmer import polish_khmer_dialogue
from backend.app.video import dubber, render_options
from backend.app.video.render_options import VideoRenderOptions
from backend.app.video.subtitles import graphemes, readable_cues, wrap_subtitle


class SubtitleQualityTests(unittest.TestCase):
    def test_cleanup_preserves_questions_relationships_and_meaning(self):
        for text in ("តើខ្ញុំអាយុប៉ុន្មាន?", "មិនលើសដែនកំណត់", "អ្នក ជាគ្រូរបស់ខ្ញុំ", "មនុស្សស្រលាញ់ភេទដូចគ្នា"):
            self.assertEqual(polish_khmer_dialogue(text), text)

    def test_literary_terms_are_corrected_only_with_source_evidence(self):
        literal = "សៀវភៅគឺជាអ្នកដឹកជញ្ជូននៃផ្លូវបរិសុទ្ធ"
        self.assertEqual(polish_khmer_dialogue(literal), literal)
        self.assertEqual(polish_khmer_dialogue(literal, "书籍乃是圣道载体"), "សៀវភៅគឺជាមធ្យោបាយផ្សព្វផ្សាយគោលធម៌")
        self.assertEqual(polish_khmer_dialogue("Sages បានសរសេរអត្ថបទ", "圣贤写下文章"), "អ្នកប្រាជ្ញ បានសរសេរអត្ថបទ")

    def test_line_wrapping_keeps_khmer_clusters_intact(self):
        text = "ខ្មែរ" * 30
        wrapped = wrap_subtitle(text)
        self.assertEqual(wrapped.replace("\n", ""), text)
        self.assertEqual(len(wrapped.splitlines()), 2)
        for line in wrapped.splitlines():
            self.assertNotEqual(line[0], "្")
        self.assertIn("ខ្មែ", graphemes(text))

    def test_reading_time_uses_pauses_without_overlapping_next_cue(self):
        segments = [{"start": 1, "end": 1.4, "translation": "សួស្តី"}, {"start": 2, "end": 3, "translation": "អរគុណ"}]
        result = readable_cues(segments, 3.2)
        self.assertGreater(result[0]["end"], 1.4)
        self.assertLess(result[0]["end"], 2)
        self.assertLessEqual(result[-1]["end"], 3.2)
        self.assertEqual(segments[0]["end"], 1.4)

    def test_long_subtitles_use_readable_pages_without_losing_text(self):
        text = "ខ្មែរ" * 100
        result = readable_cues([{"start": 0, "end": 8, "translation": text}], 8)
        self.assertGreater(len(result), 1)
        self.assertEqual("".join(cue["text"].replace("\n", "") for cue in result), text)
        for cue in result:
            self.assertLessEqual(max(len(graphemes(line)) for line in cue["text"].splitlines()), 38)
            self.assertLessEqual(cue["end"], 8.00001)

    def test_blur_region_and_logo_identifiers_are_validated(self):
        for values in ({"blur_y": .9, "blur_height": .3}, {"blur_x": -.1}, {"logo_id": "../../secret"}, {"blur_width": float("nan")}):
            with self.assertRaises(ValidationError):
                VideoRenderOptions(**values)


class EffectsApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cleanup_patch = patch("backend.app.video.pipeline.cleanup_processing_cache")
        self.cleanup_patch.start()
        self.addCleanup(self.cleanup_patch.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.temp.cleanup()

    async def test_custom_logo_upload_is_verified_and_uses_an_opaque_name(self):
        data = io.BytesIO()
        Image.new("RGBA", (80, 60), "red").save(data, format="PNG")
        with patch.object(video_routes, "LOGO_DIR", self.root), patch.object(render_options, "LOGO_DIR", self.root):
            response = await self.client.post("/api/video/logo", files={"file": ("../logo.png", data.getvalue(), "image/png")})
            self.assertEqual(response.status_code, 200)
            identifier = response.json()["logo_id"]
            self.assertRegex(identifier, r"^custom_[a-f0-9]{32}$")
            self.assertEqual(len(list(self.root.glob("*.png"))), 1)
            preview = await self.client.get(response.json()["preview_url"])
            self.assertEqual(preview.status_code, 200)
            self.assertEqual(preview.content, data.getvalue())
            self.assertTrue(VideoRenderOptions(add_logo=True, logo_id=identifier).add_logo)

    async def test_non_image_logo_is_rejected(self):
        response = await self.client.post("/api/video/logo", files={"file": ("logo.png", b"not an image", "image/png")})
        self.assertEqual(response.status_code, 400)

    async def test_effects_are_forwarded_for_url_and_upload(self):
        effects = {"add_logo": True, "logo_id": "khmerdub", "blur_chinese_subtitles": True, "burn_subtitles": True}
        worker = AsyncMock()
        with patch.object(video_routes, "create_job", return_value="test"), patch.object(video_routes, "run_video_dubbing_pipeline", worker), patch.object(video_routes, "STORAGE_UPLOADS", self.root):
            response = await self.client.post("/api/video/dub-url", json={"url": "https://example.com/video.mp4", **effects})
            self.assertEqual(response.status_code, 200)
            response = await self.client.post("/api/video/dub-upload", files={"file": ("video.mp4", b"video")}, data={key: str(value).lower() for key, value in effects.items()})
            self.assertEqual(response.status_code, 200)
        for call in worker.await_args_list:
            for key, value in effects.items():
                self.assertEqual(call.kwargs["render_options"][key], value)

    async def test_rerender_persists_effects(self):
        source = self.root / "video.mp4"
        source.touch()
        job = {"video_path": str(source), "segments": []}
        renderer = AsyncMock(return_value={})
        with patch.object(video_routes, "get_job_status", return_value=job), patch.object(video_routes, "assemble_dubbed_video", renderer), patch.object(video_routes, "_save_job"), patch.object(video_routes, "ROOT_PROJECT_DIR", self.root):
            response = await self.client.post("/api/video/job/test/rerender", json={"add_logo": True, "blur_chinese_subtitles": True, "burn_subtitles": True})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(job["render_options"]["add_logo"])
        self.assertTrue(renderer.await_args.kwargs["render_options"]["burn_subtitles"])

    async def test_visual_update_uses_the_completed_audio_and_a_new_output_file(self):
        source, completed = self.root / "source.mp4", self.root / "completed.mp4"
        source.touch(); completed.touch()
        job = {"status": "completed", "video_path": str(source), "segments": [],
               "result": {"dubbed_video_path": str(completed)}}
        renderer = AsyncMock(return_value={"dubbed_video_path": str(self.root / "visual.mp4")})
        with patch.object(video_routes, "get_job_status", return_value=job), patch.object(video_routes, "_save_job"), patch.object(video_routes, "assemble_dubbed_video", renderer):
            response = await self.client.post("/api/video/job/test/visual-update", json={"blur_chinese_subtitles": True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(renderer.await_args.kwargs["prepared_audio_path"], str(completed))
        self.assertIn("_visual_", renderer.await_args.kwargs["output_filename"])
        self.assertTrue(job["render_options"]["blur_chinese_subtitles"])
        self.assertEqual(job["status"], "completed")

    async def test_visual_update_rejects_unrendered_dialogue_edits(self):
        job = {"status": "completed", "needs_rerender": True, "segments": []}
        with patch.object(video_routes, "get_job_status", return_value=job):
            response = await self.client.post("/api/video/job/test/visual-update", json={"blur_chinese_subtitles": True})
        self.assertEqual(response.status_code, 409)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class EffectsMediaTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="video-effects-tests-")
        cls.root = Path(cls.temp.name)
        cls.width, cls.height = 160, 120
        grid = np.indices((cls.height, cls.width)).sum(axis=0) % 2 * 255
        Image.fromarray(np.repeat(grid[:, :, None], 3, axis=2).astype(np.uint8)).save(cls.root / "grid.png")
        cls.source = cls.root / "grid.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-loop", "1", "-i", str(cls.root / "grid.png"), "-t", "2", "-r", "10", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(cls.source)], check=True)
        cls.black = cls.root / "black.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=black:s=160x120:r=10:d=2", "-c:v", "libx264", str(cls.black)], check=True)
        cls.logo = cls.root / "white.png"
        Image.new("RGBA", (80, 80), "white").save(cls.logo)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def frame(self, filename, when="1"):
        output = subprocess.check_output(["ffmpeg", "-v", "error", "-ss", when, "-i", str(filename), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"])
        return np.frombuffer(output, dtype=np.uint8).reshape(self.height, self.width, 3)

    async def test_logo_is_exactly_40_pixels_in_the_top_right(self):
        with patch.object(dubber, "DUBBED_DIR", self.root), patch.object(dubber, "resolve_logo", return_value=self.logo):
            result = await dubber.assemble_dubbed_video(str(self.black), [], "logo.mp4", render_options={"add_logo": True})
        image = self.frame(result["dubbed_video_path"])
        self.assertGreater(image[12:52, 108:148].mean(), 245)
        self.assertLess(image[12:52, 104:107].mean(), 8)
        self.assertLess(image[8:11, 108:148].mean(), 8)

    async def test_blur_reduces_detail_only_in_the_requested_area(self):
        with patch.object(dubber, "DUBBED_DIR", self.root):
            result = await dubber.assemble_dubbed_video(str(self.source), [], "blur.mp4", render_options={"blur_chinese_subtitles": True})
        original = self.frame(self.source).astype(float)
        blurred = self.frame(result["dubbed_video_path"]).astype(float)
        self.assertLess(blurred[98:110, 20:140].var(), original[98:110, 20:140].var() / 4)
        self.assertLess(np.abs(original[10:70, 10:90] - blurred[10:70, 10:90]).mean(), 8)

    async def test_burned_khmer_captions_are_visible_only_during_their_cue(self):
        audio = self.root / "caption_voice.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1.1", str(audio)], check=True)
        segments = [{"id": 1, "start": .4, "end": 1.5, "text": "Hello", "translation": "សួស្តី", "audio_file": str(audio)}]
        with patch.object(dubber, "DUBBED_DIR", self.root), patch.object(dubber, "resolve_logo", return_value=self.logo):
            result = await dubber.assemble_dubbed_video(str(self.black), segments, "captions.mp4", render_options={"add_logo": True, "blur_chinese_subtitles": True, "burn_subtitles": True})
        before = self.frame(result["dubbed_video_path"], "0.1")
        during = self.frame(result["dubbed_video_path"], "1")
        self.assertLess(before[75:].max(), 10)
        self.assertGreater(np.count_nonzero(during[75:] > 150), 30)
        self.assertGreater(during[12:52, 108:148].mean(), 245)
        self.assertTrue(result["render_options"]["burn_subtitles"])

    async def test_visual_update_copies_audio_exactly_without_rebuilding_the_timeline(self):
        with patch.object(dubber, "DUBBED_DIR", self.root):
            initial = await dubber.assemble_dubbed_video(str(self.source), [], "finished.mp4")
            with patch.object(dubber, "schedule_non_overlapping_clips", side_effect=AssertionError("Audio must be retained")), patch.object(dubber, "build_master_audio_track", side_effect=AssertionError("Audio must be retained")):
                updated = await dubber.assemble_dubbed_video(
                    str(self.source), [], "visual-update.mp4", prepared_audio_path=initial["dubbed_video_path"],
                    render_options={"blur_chinese_subtitles": True},
                )
        def audio(path):
            return subprocess.check_output(["ffmpeg", "-v", "error", "-i", path, "-map", "0:a:0", "-c", "copy", "-f", "adts", "-"])
        self.assertEqual(audio(initial["dubbed_video_path"]), audio(updated["dubbed_video_path"]))
        self.assertLess(self.frame(updated["dubbed_video_path"])[98:110, 20:140].var(), self.frame(self.source)[98:110, 20:140].var() / 4)
