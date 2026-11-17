"""Verify direct translation, legacy pivot migration, and independent English references."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from backend.app.engines.dialogue_translator import (
    FLOW, translate_dialogue_batch_openai, translate_dialogue_clusters_free,
)
from backend.app.video.segments import MissingTranslationsError
from backend.app.video.transcriber import fuse_speech_and_subtitles, transcribe_video_audio
from backend.app.api import video_routes
from backend.app.main import app
from backend.app.engines.free_engine import FreeEngine


class DirectTranslationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        cleanup = patch("backend.app.video.pipeline.cleanup_processing_cache")
        cleanup.start()
        self.addCleanup(cleanup.stop)

    async def test_free_translates_original_chinese_without_english_or_numbering(self):
        source = "这也是居然穿越到了一个同靈同性的铺皮身上"
        cue = {"id": 3, "text": source, "english_intent": "same spirit and sex paupi",
               "translation": "old draft", "translation_flow": "speech_subtitles_to_english_to_khmer",
               "audio_file": "old.mp3"}
        translate = AsyncMock(side_effect=["ខ្ញុំបានឆ្លងមកកាន់រាងកាយមនុស្សម្នាក់។", "An English reference"])
        with patch("backend.app.engines.dialogue_translator.get_engine", return_value=SimpleNamespace(translate=translate)):
            await translate_dialogue_clusters_free([cue], "zh")
        self.assertEqual(translate.await_args_list[0].args, (source, "zh", "km"))
        self.assertEqual(translate.await_args_list[1].args, (source, "zh", "en"))
        self.assertEqual(cue["translation_flow"], FLOW)
        self.assertIsNone(cue["audio_file"])
        self.assertTrue(cue["_dirty"])
        self.assertNotIn("translation_error", cue)
        self.assertNotIn("translation_english_key", cue)
        translate.reset_mock()
        with patch("backend.app.engines.dialogue_translator.get_engine", return_value=SimpleNamespace(translate=translate)):
            await translate_dialogue_clusters_free([cue], "zh")
        translate.assert_not_awaited()

    async def test_missing_english_does_not_block_direct_khmer_or_preserve_stale_reference(self):
        cue = {"id": 1, "text": "你好", "english_intent": "stale reference"}
        translate = AsyncMock(side_effect=["សួស្តី", RuntimeError("English unavailable")])
        with patch("backend.app.engines.dialogue_translator.get_engine", return_value=SimpleNamespace(translate=translate)):
            await translate_dialogue_clusters_free([cue], "zh")
        self.assertEqual(cue["translation"], "សួស្តី")
        self.assertNotIn("translation_error", cue)
        self.assertNotIn("english_intent", cue)
        self.assertIn("english_reference_error", cue)

    async def test_batch_can_omit_english_reference_requests(self):
        cue = {"id": 1, "text": "你好", "english_intent": "old reference"}
        translate = AsyncMock(return_value="សួស្តី")
        with patch("backend.app.engines.dialogue_translator.get_engine", return_value=SimpleNamespace(translate=translate)):
            await translate_dialogue_clusters_free([cue], "zh", include_english=False)
        translate.assert_awaited_once_with("你好", "zh", "km", tone="casual")
        self.assertNotIn("english_intent", cue)
        self.assertEqual(cue["translation"], "សួស្តី")

    async def test_free_dialogue_job_reuses_one_http_client_for_all_cues_and_references(self):
        requests = []
        def handler(request):
            requests.append(request)
            result = "Hello" if request.url.params["tl"] == "en" else "សួស្តី"
            return httpx.Response(200, json=[[[result]]])
        original = httpx.AsyncClient
        with patch("backend.app.engines.dialogue_translator.get_engine", return_value=FreeEngine()), patch.object(httpx, "AsyncClient", side_effect=lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs)) as create_client:
            await translate_dialogue_clusters_free([{"id": 1, "text": "你好"}, {"id": 2, "text": "再见"}], "zh")
        self.assertEqual(len(requests), 4)
        self.assertEqual(create_client.call_count, 1)

    async def test_rate_limit_sets_a_shared_cooldown_and_retries(self):
        requests = []
        def handler(request):
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(429, headers={"Retry-After": "90"})
            return httpx.Response(200, json=[[["សួស្តី"]]])
        original = httpx.AsyncClient
        with patch.object(httpx, "AsyncClient", side_effect=lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs)), patch("backend.app.engines.free_engine.wait_for_service", new=AsyncMock()) as pace, patch("backend.app.engines.free_engine.defer_service") as cooldown:
            async with FreeEngine().session() as engine:
                translated = await engine.translate("你好", "zh", "km")
        self.assertEqual(translated, "សួស្តី")
        self.assertEqual(len(requests), 2)
        self.assertEqual(pace.await_count, 2)
        cooldown.assert_called_once_with("free_translation", 90)

    async def test_failed_direct_translation_does_not_accept_old_pivot_draft(self):
        cue = {"id": 1, "text": "你好", "translation": "សួស្តី", "english_intent": "Hello"}
        translate = AsyncMock(return_value="你好")
        with patch("backend.app.engines.dialogue_translator.get_engine", return_value=SimpleNamespace(translate=translate)), patch("backend.app.engines.dialogue_translator.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(MissingTranslationsError):
                await translate_dialogue_clusters_free([cue], "zh")
        self.assertEqual(translate.await_count, 3)
        self.assertTrue(all(call.args[1:3] == ("zh", "km") for call in translate.await_args_list))
        self.assertIn("translation_error", cue)
        self.assertEqual(cue["translation"], "សួស្តី")

    async def test_fragmentary_ocr_uses_complete_chinese_and_unrelated_evidence_is_flagged(self):
        cues = [{"id": 1, "text": "话一 出口 日", "asr_text": "话一旦出口 便再无反回之地", "ocr_detected": True},
                {"id": 2, "text": "等等", "asr_text": "立即动身前往皇宫", "ocr_detected": True}]
        translate = AsyncMock(side_effect=lambda text, source, target, **kwargs: "សួស្តី" if target == "km" else "Reference")
        with patch("backend.app.engines.dialogue_translator.get_engine", return_value=SimpleNamespace(translate=translate)):
            await translate_dialogue_clusters_free(cues, "zh")
        self.assertEqual(translate.await_args_list[0].args, (cues[0]["asr_text"], "zh", "km"))
        self.assertNotIn("source_warning", cues[0])
        self.assertIn("disagree", cues[1]["source_warning"])
        self.assertEqual(cues[1]["translation_source_text"], "等等")

    async def test_checkpoint_changes_with_source_or_tone_and_force_preserves_unchanged_voice(self):
        cue = {"id": 1, "text": "你好"}
        translate = AsyncMock(side_effect=lambda text, source, target, **kwargs: "សួស្តី" if target == "km" else "Hello")
        with patch("backend.app.engines.dialogue_translator.get_engine", return_value=SimpleNamespace(translate=translate)):
            await translate_dialogue_clusters_free([cue], "zh")
            first_key = cue["translation_source_key"]
            cue.update(audio_file="cached.mp3")
            cue.pop("_dirty")
            await translate_dialogue_clusters_free([cue], "zh", force=True)
            self.assertEqual(cue["audio_file"], "cached.mp3")
            self.assertNotIn("_dirty", cue)
            cue["text"] = "您好"
            await translate_dialogue_clusters_free([cue], "zh")
            self.assertNotEqual(first_key, cue["translation_source_key"])
            second_key = cue["translation_source_key"]
            await translate_dialogue_clusters_free([cue], "zh", tone="formal")
            self.assertNotEqual(second_key, cue["translation_source_key"])
        self.assertEqual(translate.await_count, 8)

    async def test_openai_uses_original_sources_and_context_in_one_request(self):
        captured = []
        def handler(request):
            body = json.loads(request.content)
            captured.append(body)
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps([{"id": 1, "translation": "ផ្ញើសារទៅសំណាក់ស៊ីងឆិន។"}])}}]})
        original = httpx.AsyncClient
        cue = {"id": 1, "text": "传信星辰阁", "asr_text": "传信星辰哥", "ocr_detected": True,
               "english_intent": "Stale wrong interpretation"}
        with patch.object(httpx, "AsyncClient", side_effect=lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs)):
            await translate_dialogue_batch_openai([cue], "zh", api_key="test", base_url="http://example.test/v1",
                                                 context_before=[{"text": "等等"}])
        self.assertEqual(len(captured), 1)
        self.assertIn("Native Cambodian Khmer style", captured[0]["messages"][0]["content"])
        self.assertIn("កុំបារម្ភអី", captured[0]["messages"][0]["content"])
        body = json.loads(captured[0]["messages"][1]["content"])
        self.assertEqual(body["target_cues"][0]["subtitle_text"], "传信星辰阁")
        self.assertEqual(body["target_cues"][0]["speech_text"], "传信星辰哥")
        self.assertEqual(body["scene_before"][0]["speech_text"], "等等")
        self.assertNotIn("Stale wrong interpretation", captured[0]["messages"][1]["content"])
        self.assertNotIn("english_intent", cue)
        self.assertEqual(cue["translation_flow"], FLOW)
        self.assertEqual(cue["translation"], "ផ្ញើសារទៅសំណាក់ស៊ីងឆិន។")

    async def test_subtitle_track_still_runs_speech_recognition(self):
        with tempfile.TemporaryDirectory() as directory:
            subtitle = Path(directory) / "subs.srt"
            subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\n字幕\n")
            pcm = Path(directory) / "audio.pcm"
            pcm.touch()
            model = SimpleNamespace(transcribe=lambda *args, **kwargs: ([SimpleNamespace(start=0, end=1, text="语音")], SimpleNamespace(language="zh", language_probability=1)))
            with patch("backend.app.video.transcriber.extract_pcm_from_video", return_value=([0]*16000, pcm)), patch("backend.app.video.transcriber.get_whisper_model", return_value=model):
                result = await transcribe_video_audio("movie.mp4", external_subtitle_path=str(subtitle))
            self.assertEqual(result["segments"][0]["subtitle_text"], "字幕")
            self.assertEqual(result["segments"][0]["asr_text"], "语音")
            self.assertFalse(pcm.exists())

    async def test_subtitles_without_audio_remain_usable_without_invented_speech(self):
        with tempfile.TemporaryDirectory() as directory:
            subtitle = Path(directory) / "subs.srt"
            subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\n字幕\n")
            with patch("backend.app.video.transcriber.extract_pcm_from_video", side_effect=subprocess.CalledProcessError(1, "ffmpeg")), patch("backend.app.video.transcriber.subprocess.run", return_value=SimpleNamespace(stdout="")):
                result = await transcribe_video_audio("movie.mp4", external_subtitle_path=str(subtitle))
            self.assertEqual(result["segments"][0]["asr_text"], "")
            self.assertIn("without_audio", result["source"])

    async def test_saved_job_resume_translates_before_synthesizing(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.touch()
            cue = {"id": 1, "start": 0, "end": 1, "text": "你好", "translation_error": "failed"}
            job = {"status": "failed", "video_path": str(source), "segments": [cue], "detected_language": "zh"}
            order = []
            async def translate(segments, *args, **kwargs):
                order.append("translate")
                segments[0].update(english_intent="Hello", translation="សួស្តី", translation_flow=FLOW)
                segments[0].pop("translation_error")
            async def render(*args):
                order.append("render")
                self.assertEqual(cue["english_intent"], "Hello")
                return {"status": "completed"}
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                with patch.object(video_routes, "get_job_status", return_value=job), patch.object(video_routes, "_save_job"), patch.object(video_routes, "_finish_job_render", new=AsyncMock(side_effect=render)), patch("backend.app.engines.dialogue_translator.translate_dialogue_segments", new=AsyncMock(side_effect=translate)):
                    response = await client.post("/api/video/job/test/resume", json={"retranslate": True, "engine": "free"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(order, ["translate", "render"])
            self.assertEqual(job["translation_flow"], FLOW)
            self.assertTrue(cue["_dirty"])

    async def test_polishing_saves_partial_results_and_forces_retranslation(self):
        cues = [{"id": 1, "text": "你好", "translation": "old"},
                {"id": 2, "text": "等等", "translation": "old"}]
        job = {"segments": cues, "detected_language": "zh"}
        async def partial(segments, *args, **kwargs):
            self.assertTrue(kwargs["force"])
            segments[0].update(translation="សួស្តី", translation_flow=FLOW, audio_file=None, _dirty=True)
            segments[1]["translation_error"] = "Direct translation unavailable"
            raise MissingTranslationsError([segments[1]])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            with patch.object(video_routes, "get_job_status", return_value=job), patch.object(video_routes, "_save_job") as save, patch("backend.app.engines.dialogue_translator.translate_dialogue_segments", new=AsyncMock(side_effect=partial)):
                response = await client.post("/api/video/job/test/ai-polish-dialogue", json={"engine": "free"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "partial")
        self.assertEqual(response.json()["segments"][0]["translation"], "សួស្តី")
        self.assertIn("translation_error", response.json()["segments"][1])
        self.assertTrue(job["needs_rerender"])
        save.assert_called_with("test")
        self.assertTrue(all(call.args == ("test",) for call in save.call_args_list))


class FusionTests(unittest.TestCase):
    def test_fragmentary_ocr_uses_complete_spoken_meaning(self):
        from backend.app.khmer.dialogue_fixer import reconcile_english_evidence
        english = reconcile_english_evidence("Word 1 Exit Day", "Once the words are spoken, there is no turning back.", "话一 出口 日", "话一旦出口 便再无反回之地")
        self.assertEqual(english, "Once the words are spoken, there is no turning back.")
        # Different dialogue does not count as evidence for the fragment.
        self.assertEqual(reconcile_english_evidence("Wait", "Go to the palace immediately", "等等", "立即动身前往皇宫"), "Wait")

    def test_subtitle_timing_retains_spoken_evidence_and_uncovered_speech(self):
        speech = [{"start": 0, "end": 1, "text": "heard", "asr_text": "heard"}, {"start": 4, "end": 5, "text": "offscreen", "asr_text": "offscreen"}]
        subtitles = [{"start": 0, "end": 1.5, "text": "written"}]
        result = fuse_speech_and_subtitles(speech, subtitles)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["text"], "written")
        self.assertEqual(result[0]["asr_text"], "heard")
        self.assertEqual(result[0]["end"], 1.5)
        self.assertEqual(result[1]["text"], "offscreen")
        self.assertEqual([cue["id"] for cue in result], [1, 2])
