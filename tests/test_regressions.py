"""Regression checks using temporary files and mocked provider HTTP responses."""

import asyncio
import copy
import json
import logging
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException

from backend.app.main import app
from backend.app.api import video_routes
from backend.app.engines import engines, resolve_engine
from backend.app.engines.openai_engine import OpenAIEngine
from backend.app.engines.dialogue_translator import FLOW, translate_dialogue_segments
from backend.app.video import dubber, pipeline
from backend.app.video.splitter import split_video_by_minutes
from backend.app.video.subtitle_reader import parse_srt_or_vtt

logging.getLogger("httpx").setLevel(logging.WARNING)


@contextmanager
def provider_response(handler):
    client_type = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    with patch.object(httpx, "AsyncClient", side_effect=lambda **kwargs: client_type(transport=transport, **kwargs)):
        yield


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cleanup_patch = patch("backend.app.video.pipeline.cleanup_processing_cache")
        self.cleanup_patch.start()
        self.addCleanup(self.cleanup_patch.stop)
        self.temp = tempfile.TemporaryDirectory(prefix="translator-tests-")
        self.root = Path(self.temp.name)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        self.provider = patch.dict(engines, {"openai": OpenAIEngine()})
        self.provider.start()

    async def asyncTearDown(self):
        self.provider.stop()
        await self.client.aclose()
        self.temp.cleanup()

    async def test_smoke_and_supported_engines(self):
        for url in ("/", "/health", "/api/status", "/api/languages", "/static/js/app.js"):
            with self.subTest(url=url):
                self.assertEqual((await self.client.get(url)).status_code, 200)
        status = (await self.client.get("/api/status")).json()
        self.assertEqual(set(status["engines"]), {"openai", "free"})
        response = await self.client.post("/api/tools", json={"text": "២០២៦", "action": "to_arabic_num"})
        self.assertEqual(response.json()["result"], "2026")

    async def test_text_provider_receives_key_model_and_custom_url(self):
        captured = []

        def handler(request):
            captured.append(request)
            return httpx.Response(200, json={"choices": [{"message": {"content": "សួស្តី"}}]})

        with provider_response(handler):
            response = await self.client.post("/api/translate", json={
                "text": "Hello", "engine": "auto", "api_key": "test-key",
                "model": "local-model", "base_url": "http://localhost:11434/v1/",
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["engine_used"], "openai")
        self.assertEqual(str(captured[0].url), "http://localhost:11434/v1/chat/completions")
        self.assertEqual(captured[0].headers["Authorization"], "Bearer test-key")
        self.assertEqual(json.loads(captured[0].content)["model"], "local-model")

    async def test_stream_uses_custom_provider_settings(self):
        captured = []

        def handler(request):
            captured.append(request)
            return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"Hello"}}]}\n\ndata: [DONE]\n\n')

        with provider_response(handler):
            response = await self.client.post("/api/translate/stream", json={
                "text": "Hello", "engine": "openai", "model": "local-model",
                "base_url": "http://localhost:11434/v1", "api_key": "test-key",
            })
        self.assertEqual(response.status_code, 200)
        self.assertIn('"chunk": "Hello"', response.text)
        self.assertEqual(str(captured[0].url), "http://localhost:11434/v1/chat/completions")
        self.assertTrue(json.loads(captured[0].content)["stream"])

    async def test_invalid_provider_settings_are_rejected(self):
        for options in ({"engine": "unsupported"}, {"base_url": "file:///tmp/test"}, {"base_url": "https://user:pass@example.com/v1"}):
            response = await self.client.post("/api/translate", json={"text": "Hello", **options})
            self.assertEqual(response.status_code, 422)

    async def test_uploads_reject_paths_on_both_endpoints(self):
        for endpoint in ("dub-upload", "split-video"):
            for filename in ("../escape.mp4", str(self.root / "escape.mp4"), "..\\escape.mp4"):
                with self.subTest(endpoint=endpoint, filename=filename), patch.object(video_routes, "STORAGE_UPLOADS", self.root / "uploads"):
                    response = await self.client.post(f"/api/video/{endpoint}", files={"file": (filename, b"video")})
                    self.assertEqual(response.status_code, 400)
        self.assertFalse((self.root / "escape.mp4").exists())

    async def test_duplicate_upload_names_are_isolated_and_options_forwarded(self):
        worker = AsyncMock()
        with patch.object(video_routes, "STORAGE_UPLOADS", self.root / "uploads"), patch.object(video_routes, "create_job", return_value="test"), patch.object(video_routes, "run_video_dubbing_pipeline", worker):
            for content in (b"first", b"second"):
                response = await self.client.post("/api/video/dub-upload", files={"file": ("movie.mp4", content)}, data={
                    "engine": "openai", "api_key": "test-key", "model": "local-model", "base_url": "http://localhost:11434/v1",
                })
                self.assertEqual(response.status_code, 200)
        paths = [Path(call.kwargs["video_source"]) for call in worker.await_args_list]
        self.assertNotEqual(paths[0], paths[1])
        self.assertEqual([path.read_bytes() for path in paths], [b"first", b"second"])
        self.assertEqual(worker.await_args.kwargs["engine_name"], "openai")
        self.assertEqual(worker.await_args.kwargs["model"], "local-model")
        self.assertEqual(worker.await_args.kwargs["base_url"], "http://localhost:11434/v1")

    async def test_subtitle_upload_rejects_traversal(self):
        with patch.object(video_routes, "STORAGE_UPLOADS", self.root / "uploads"):
            response = await self.client.post("/api/video/dub-upload", files={
                "file": ("movie.mp4", b"video"), "subtitle_file": ("../escape.srt", b"subtitle"),
            })
        self.assertEqual(response.status_code, 400)

    async def test_splitter_cannot_read_files_outside_storage(self):
        outside = self.root / "outside.mp4"
        outside.write_bytes(b"private")
        with patch.object(video_routes, "STORAGE_UPLOADS", self.root / "uploads"), patch.object(video_routes, "DUBBED_DIR", self.root / "dubbed"):
            response = await self.client.post("/api/video/split-video", data={"existing_filename": str(outside)})
        self.assertEqual(response.status_code, 404)

    async def test_url_dubbing_and_polish_forward_provider_settings(self):
        settings = {"engine": "openai", "api_key": "test-key", "base_url": "http://localhost:11434/v1", "model": "local-model"}
        worker = AsyncMock()
        with patch.object(video_routes, "create_job", return_value="test"), patch.object(video_routes, "run_video_dubbing_pipeline", worker):
            response = await self.client.post("/api/video/dub-url", json={"url": "https://example.com/movie.mp4", **settings})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(worker.await_args.kwargs["model"], settings["model"])
        self.assertEqual(worker.await_args.kwargs["engine_name"], settings["engine"])
        job = {"segments": [{"id": 1, "text": "Hello"}], "detected_language": "en"}
        translator = AsyncMock()
        with patch.object(video_routes, "get_job_status", return_value=job), patch.object(video_routes, "_save_job"), patch("backend.app.engines.dialogue_translator.translate_dialogue_segments", translator):
            response = await self.client.post("/api/video/job/test/ai-polish-dialogue", json=settings)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(translator.await_args.kwargs["base_url"], settings["base_url"])
        self.assertEqual(translator.await_args.kwargs["api_key"], settings["api_key"])

    async def test_rerender_preserves_edited_anchors_and_can_remove_all_cues(self):
        source = self.root / "source.mp4"
        source.write_bytes(b"video")
        audio = self.root / "cue.mp3"
        audio.write_bytes(b"audio")
        cue = {"id": 1, "start": 1, "end": 2, "target_start": 1, "target_end": 2, "audio_file": str(audio)}
        edited = {**cue, "start": 5, "end": 6}
        job = {"video_path": str(source), "segments": [cue], "status": "failed", "error": "Translation incomplete"}
        renderer = AsyncMock(return_value={"download_url": "/static/dubbed/test.mp4"})
        with patch.object(video_routes, "get_job_status", return_value=job), patch.object(video_routes, "_save_job"), patch.object(video_routes, "ROOT_PROJECT_DIR", self.root), patch.object(video_routes, "assemble_dubbed_video", renderer):
            response = await self.client.put("/api/video/job/test/segments", json={"segments": [edited]})
            self.assertEqual(response.status_code, 200)
            response = await self.client.post("/api/video/job/test/rerender", json={"segments": [edited]})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["segments"][0]["target_start"], 5)
            self.assertEqual(job["status"], "completed")
            self.assertIsNone(job["error"])
            response = await self.client.post("/api/video/job/test/rerender", json={"segments": []})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(renderer.await_args.kwargs["segments"], [])

    async def test_pipeline_translates_with_selected_provider(self):
        job = {"id": "test"}
        transcription = {"language": "en", "segments": [{"id": 1, "text": "Hello", "start": 1, "end": 2}]}
        captured = []

        def handler(request):
            captured.append(request)
            content = json.dumps([{"id": 1, "translation": "សួស្តី", "english_intent": "Hello"}])
            return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

        async def generate_voice(segments, *args, **kwargs):
            audio = self.root / "voice.mp3"
            audio.write_bytes(b"voice")
            for segment in segments:
                segment["audio_file"] = str(audio)
            return segments

        with patch.dict(pipeline.JOBS, {"test": job}), patch.object(pipeline, "_save_job"), patch.object(pipeline, "transcribe_video_audio", AsyncMock(return_value=transcription)), patch.object(pipeline, "synthesize_segments", AsyncMock(side_effect=generate_voice)), patch.object(pipeline, "assemble_dubbed_video", AsyncMock(return_value={"download_url": "test.mp4"})), provider_response(handler):
            await pipeline.run_video_dubbing_pipeline(
                "test", str(self.root / "movie.mp4"), is_url=False,
                engine_name="openai", model="local-model", base_url="http://localhost:11434/v1",
            )
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["segments"][0]["translation"], "សួស្តី")
        self.assertEqual(json.loads(captured[0].content)["model"], "local-model")


class SubtitleTests(unittest.TestCase):
    def parse(self, text):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "subtitle.srt"
            path.write_text(text, encoding="utf-8")
            return parse_srt_or_vtt(str(path))

    def test_srt_numbers_do_not_enter_dialogue(self):
        cues = self.parse("\ufeff1\r\n00:00:01,000 --> 00:00:02,000\r\nHello\r\n\r\n2\r\n00:00:03,000 --> 00:00:04,000\r\nWorld\r\n")
        self.assertEqual([cue["text"] for cue in cues], ["Hello", "World"])

    def test_vtt_short_timestamps_and_settings(self):
        cues = self.parse("WEBVTT\n\nNOTE Ignore this metadata\n\ncue-id\n00:01.250 --> 00:02.500 align:start\nHello\nworld\n")
        self.assertEqual(len(cues), 1)
        self.assertEqual((cues[0]["start"], cues[0]["end"], cues[0]["text"]), (1.25, 2.5, "Hello world"))

    def test_numeric_dialogue_is_preserved(self):
        cues = self.parse("1\n00:00:01,000 --> 00:00:02,000\n123\n\n2\n00:00:03,000 --> 00:00:04,000\n456\n")
        self.assertEqual([cue["text"] for cue in cues], ["123", "456"])


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_qwen_text_and_stream_skip_thinking(self):
        captured = []

        def handler(request):
            captured.append(request)
            if json.loads(request.content).get("stream"):
                return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"សួស្តី"}}]}\n\ndata: [DONE]\n\n')
            return httpx.Response(200, json={"choices": [{"message": {"content": "សួស្តី"}}]})

        engine = OpenAIEngine(base_url="http://localhost:11434/v1", model="qwen3.5:9b")
        with provider_response(handler):
            self.assertEqual(await engine.translate("Hello", "en", "km"), "សួស្តី")
            self.assertEqual("".join([part async for part in engine.translate_stream("Hello", "en", "km")]), "សួស្តី")
        self.assertEqual(len(captured), 2)
        for request in captured:
            self.assertEqual(json.loads(request.content)["reasoning_effort"], "none")
            self.assertEqual(request.extensions["timeout"]["read"], 180)

    async def test_local_qwen_dialogue_uses_small_batches(self):
        cues = [{"id": i, "text": f"Bring letter {i}", "start": i, "end": i + 1} for i in range(1, 10)]
        captured = []

        def handler(request):
            captured.append(request)
            targets = json.loads(json.loads(request.content)["messages"][-1]["content"])["target_cues"]
            content = json.dumps([{"id": cue["id"], "translation": "យកសំបុត្រមក។"} for cue in targets])
            return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

        provider = OpenAIEngine(base_url="http://localhost:11434/v1", model="qwen3.5:9b")
        with patch.dict(engines, {"openai": provider}), provider_response(handler):
            await translate_dialogue_segments(cues, "en", engine_name="openai")
        payloads = [json.loads(request.content) for request in captured]
        self.assertEqual([len(json.loads(payload["messages"][-1]["content"])["target_cues"]) for payload in payloads], [8, 1])
        self.assertTrue(all(payload["reasoning_effort"] == "none" for payload in payloads))
        self.assertTrue(all(cue.get("translation") and not cue.get("translation_error") for cue in cues))

    def test_local_qwen_options_are_scoped_to_ollama(self):
        for base_url, model in (("https://example.com/v1", "qwen3.5:9b"),
                                ("http://localhost:8080/v1", "qwen3.5:9b"),
                                ("http://localhost:11434/v1", "other-model")):
            with self.subTest(base_url=base_url, model=model):
                self.assertEqual(OpenAIEngine.generation_options(base_url, model), {})

    async def test_local_provider_needs_no_key_and_keeps_cloud_key_private(self):
        captured = []

        def handler(request):
            captured.append(request)
            return httpx.Response(200, json={"choices": [{"message": {"content": "Hello"}}]})

        engine = OpenAIEngine(api_key="server-private-key")
        with provider_response(handler):
            await engine.translate("Hello", "en", "en", options={"base_url": "http://localhost:11434/v1", "model": "local-model"})
        self.assertNotIn("Authorization", captured[0].headers)

    async def test_dialogue_uses_openai_compatible_api(self):
        cues = [{"id": 1, "text": "Hello", "start": 1, "end": 2}]
        captured = []

        def handler(request):
            captured.append(request)
            content = json.dumps([{"id": "1", "translation": "សួស្តី", "english_intent": "Hello"}])
            return httpx.Response(200, json={"choices": [{"message": {"content": f"```json\n{content}\n```"}}]})

        with patch.dict(engines, {"openai": OpenAIEngine()}), provider_response(handler):
            await translate_dialogue_segments(cues, "en", base_url="http://localhost:11434/v1", model="local-model")
        self.assertEqual(cues[0]["translation"], "សួស្តី")
        self.assertEqual(cues[0]["english_intent"], "Hello")
        self.assertEqual(json.loads(captured[0].content)["model"], "local-model")
        self.assertEqual(str(captured[0].url), "http://localhost:11434/v1/chat/completions")

    async def test_incomplete_dialogue_response_uses_free_fallback(self):
        cues = [{"id": 1, "text": "Hello", "start": 1, "end": 2}]
        async def fill_translation(segments, *args, **kwargs):
            segments[0].update(translation="សួស្តី", english_intent="Hello",
                               translation_flow=FLOW)
            segments[0].pop("translation_error", None)
            return segments
        fallback = AsyncMock(side_effect=fill_translation)
        with provider_response(lambda request: httpx.Response(200, json={"choices": [{"message": {"content": "[]"}}]})), patch("backend.app.engines.dialogue_translator.translate_dialogue_clusters_free", fallback):
            await translate_dialogue_segments(cues, "en", engine_name="openai")
        fallback.assert_awaited_once()

    async def test_auto_selection_and_explicit_free(self):
        with patch.dict(engines, {"openai": OpenAIEngine(base_url="http://localhost:11434/v1")}), patch("backend.app.core.config.DEFAULT_ENGINE", "auto"):
            self.assertEqual(resolve_engine().name, "openai")
            self.assertEqual(resolve_engine("free", "test-key").name, "free")
        with patch("backend.app.core.config.DEFAULT_ENGINE", "free"):
            self.assertEqual(resolve_engine("auto", base_url="http://localhost:11434/v1").name, "openai")


class TimingTests(unittest.TestCase):
    def test_edited_times_replace_anchors_and_unchanged_cues_keep_them(self):
        job = {"segments": [{"id": 1, "start": 1.0, "end": 2.2, "target_start": 1.0, "target_end": 2.0}]}
        video_routes._apply_segment_updates(job, copy.deepcopy(job["segments"]))
        self.assertEqual(job["segments"][0]["target_end"], 2.0)
        edited = [{**job["segments"][0], "start": 5.0, "end": 6.0}]
        video_routes._apply_segment_updates(job, edited)
        self.assertEqual((job["segments"][0]["target_start"], job["segments"][0]["target_end"]), (5.0, 6.0))

    def test_invalid_cue_times_are_rejected(self):
        for start, end in ((-1, 2), (2, 1), (1, float("inf"))):
            with self.assertRaises(HTTPException):
                video_routes._apply_segment_updates({"segments": []}, [{"start": start, "end": end}])


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg and FFprobe are required")
class MediaTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="translator-media-tests-")
        cls.root = Path(cls.temp.name)
        cls.video = cls.root / "source.mp4"
        cls.tone = cls.root / "tone.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=96x64:r=10:d=22", "-f", "lavfi", "-i", "sine=frequency=440:duration=22", "-c:v", "libx264", "-g", "10", "-pix_fmt", "yuv420p", "-c:a", "aac", "-t", "22", str(cls.video)], check=True)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=880:duration=0.5", str(cls.tone)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    async def test_split_includes_short_tail_and_archive_matches(self):
        result = await asyncio.to_thread(split_video_by_minutes, str(self.video), 1 / 6, self.root / "split")
        self.assertEqual(result["total_parts"], 2)
        self.assertEqual(result["parts"][-1]["end_seconds"], 22.0)
        self.assertGreaterEqual(result["parts"][-1]["duration_seconds"], 12.0)
        with zipfile.ZipFile(result["zip_path"]) as archive:
            self.assertEqual(set(archive.namelist()), {part["filename"] for part in result["parts"]})

    async def test_edited_timing_reaches_rendered_subtitles(self):
        cue = {"id": 1, "start": 1.0, "end": 2.0, "target_start": 1.0, "target_end": 2.0, "audio_file": str(self.tone), "translation": "សួស្តី"}
        job = {"segments": [cue]}
        video_routes._apply_segment_updates(job, [{**cue, "start": 5, "end": 6}])
        with patch.object(dubber, "DUBBED_DIR", self.root):
            result = await dubber.assemble_dubbed_video(str(self.video), job["segments"], "edited.mp4")
        self.assertTrue(Path(result["dubbed_video_path"]).exists())
        self.assertIn("00:00:05,000 -->", Path(result["srt_path"]).read_text())
        self.assertEqual(job["segments"][0]["start"], 5)

    async def test_smart_ducking_keeps_background_and_zero_volume_mutes_it(self):
        import numpy as np
        for volume, name in ((-1, "smart"), (0, "mute")):
            with patch.object(dubber, "DUBBED_DIR", self.root):
                result = await dubber.assemble_dubbed_video(str(self.video), [], f"{name}.mp4", bg_volume=volume)
            pcm = subprocess.check_output(["ffmpeg", "-v", "error", "-i", result["dubbed_video_path"], "-t", "1", "-f", "f32le", "-ac", "1", "-"])
            rms = float(np.sqrt(np.mean(np.frombuffer(pcm, dtype=np.float32) ** 2)))
            if volume < 0:
                self.assertGreater(rms, 0.001)
            else:
                self.assertLess(rms, 0.0001)


if __name__ == "__main__":
    unittest.main()
