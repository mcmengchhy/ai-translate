"""Regression checks for omitted batch lines, retries, and saved-job recovery."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from backend.app.main import app
from backend.app.api import video_routes
from backend.app.engines.dialogue_translator import translate_dialogue_clusters_free
from backend.app.video import pipeline
from backend.app.video.segments import MissingTranslationsError
from backend.app.video.tts import synthesize_segments
from backend.app.khmer.romanization import is_source_romanization


def cues():
    return [
        {"id": 1404, "start": 1, "end": 2, "text": "书籍乃是圣道载体"},
        {"id": 1405, "start": 2, "end": 3, "text": "交给百姓"},
        {"id": 1406, "start": 3, "end": 4, "text": "只会糟践圣贤文章"},
    ]


class FreeBatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_romanized_character_names_are_preserved_and_flagged_for_review(self):
        segments = [{"id": 36, "text": "钰儿"}, {"id": 135, "text": "赵钰"}]
        translate = AsyncMock(side_effect=lambda text, *args, **kwargs: {"钰儿": "Yuer", "赵钰": "Zhao Yu"}[text])
        with patch("backend.app.engines.dialogue_translator.get_engine", return_value=SimpleNamespace(translate=translate)):
            await translate_dialogue_clusters_free(segments, "zh")
        self.assertEqual([seg["translation"] for seg in segments], ["Yuer", "Zhao Yu"])
        self.assertTrue(all(seg.get("translation_warning") for seg in segments))
        self.assertEqual(translate.await_count, 4)

    async def test_known_source_phrase_does_not_need_an_english_pivot(self):
        segments = [{"id": 518, "text": "双关语"}]
        translate = AsyncMock(side_effect=AssertionError("Known source phrase needs no provider call"))
        with patch("backend.app.engines.dialogue_translator.get_engine", return_value=SimpleNamespace(translate=translate)):
            await translate_dialogue_clusters_free(segments, "zh")
        self.assertEqual(segments[0]["translation"], "ពាក្យមានន័យពីរ")
        self.assertNotIn("translation_warning", segments[0])
        translate.assert_not_awaited()

    async def test_known_idiom_has_a_khmer_fallback_without_changing_negation(self):
        from backend.app.khmer.dialogue_fixer import source_phrase_translation
        self.assertEqual(source_phrase_translation("语双关"), "ពាក្យមានន័យពីរ")
        self.assertEqual(source_phrase_translation("不是一语双关"), "")
        segments = [{"id": 518, "text": "语双关"}]
        translate = AsyncMock(return_value="1. pun")
        with patch("backend.app.engines.dialogue_translator.get_engine", return_value=SimpleNamespace(translate=translate)):
            await translate_dialogue_clusters_free(segments, "zh")
        self.assertEqual(segments[0]["translation"], "ពាក្យមានន័យពីរ")

    async def test_romanization_must_match_the_source_and_does_not_accept_english_prose(self):
        for source, name in (("赵钰", "Zhao Yu"), ("钰儿", "Yuer"), ("协林家牧云", "Xie Linjiamuyun"), ("小生李无悔", "Xiaosheng Li Wuhui")):
            self.assertTrue(is_source_romanization(source, name), (source, name))
        self.assertFalse(is_source_romanization("语双关", "pun"))
        self.assertFalse(is_source_romanization("书籍乃是圣道载体", "Books carry sacred teachings"))
        self.assertFalse(is_source_romanization("赵钰", "Another Name"))

    async def test_per_cue_failure_retries_only_the_missing_cue(self):
        segments = cues()
        results = {segments[0]["text"]: "សៀវភៅផ្ទុកនូវការបង្រៀន", segments[1]["text"]: "ប្រគល់ឱ្យប្រជារាស្ត្រ",
                   segments[2]["text"]: "វានឹងបំផ្លាញអត្ថបទ"}
        requests = []
        failed_once = False
        async def translate(text, source_lang, target_lang, tone="casual"):
            nonlocal failed_once
            requests.append((text, source_lang, target_lang))
            self.assertEqual(source_lang, "zh")
            if target_lang == "en":
                return "Optional reference"
            if text == segments[1]["text"] and not failed_once:
                failed_once = True
                raise RuntimeError("temporary failure")
            return results[text]
        with patch("backend.app.engines.dialogue_translator.get_engine", return_value=SimpleNamespace(translate=translate)), patch("backend.app.engines.dialogue_translator.asyncio.sleep", new=AsyncMock()):
            await translate_dialogue_clusters_free(segments, "zh")
        self.assertEqual([seg["translation"] for seg in segments], list(results.values()))
        self.assertEqual(sum(target == "km" for _, _, target in requests), 4)
        self.assertEqual(sum(text == segments[0]["text"] and target == "km" for text, _, target in requests), 1)

    async def test_ambiguous_unnumbered_result_does_not_shift_translations(self):
        segments = [{"id": idx, "text": text} for idx, text in enumerate(("one", "two", "three"), 1)]
        translations = {"one": "មួយ", "two": "ពីរ", "three": "បី"}

        async def translate(text, source_lang, target_lang, tone="casual"):
            return "មួយ\nបី" if "\n" in text else translations[text]

        with patch("backend.app.engines.dialogue_translator.get_engine", return_value=SimpleNamespace(translate=translate)):
            await translate_dialogue_clusters_free(segments, "en")
        self.assertEqual([seg["translation"] for seg in segments], ["មួយ", "ពីរ", "បី"])

    async def test_numbered_source_dialogue_is_preserved_without_adding_batch_numbers(self):
        segments = [{"id": 1, "text": "1. one"}, {"id": 2, "text": "2. two"}]
        requests = []
        async def translate(text, *args, **kwargs):
            requests.append(text)
            return {"1. one": "១. មួយ", "2. two": "២. ពីរ"}[text]
        with patch("backend.app.engines.dialogue_translator.get_engine", return_value=SimpleNamespace(translate=translate)):
            await translate_dialogue_clusters_free(segments, "en")
        self.assertEqual([seg["translation"] for seg in segments], ["១. មួយ", "២. ពីរ"])
        self.assertEqual(requests, ["1. one", "2. two"])

    async def test_transient_per_cue_failures_are_retried(self):
        segments = [{"id": 1, "text": "Hello"}]
        translate = AsyncMock(side_effect=[RuntimeError("batch unavailable"), RuntimeError("rate limit"), "សួស្តី"])
        with patch("backend.app.engines.dialogue_translator.get_engine", return_value=SimpleNamespace(translate=translate)), patch("backend.app.engines.dialogue_translator.asyncio.sleep", new=AsyncMock()):
            await translate_dialogue_clusters_free(segments, "en")
        self.assertEqual(segments[0]["translation"], "សួស្តី")
        self.assertNotIn("translation_error", segments[0])
        self.assertEqual(translate.await_count, 3)

    async def test_exhausted_retries_raise_and_record_the_failed_cue(self):
        segments = [{"id": 1404, "text": "Hello"}]
        translate = AsyncMock(side_effect=RuntimeError("service unavailable"))
        with patch("backend.app.engines.dialogue_translator.get_engine", return_value=SimpleNamespace(translate=translate)), patch("backend.app.engines.dialogue_translator.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(MissingTranslationsError) as failure:
                await translate_dialogue_clusters_free(segments, "en")
        self.assertEqual(failure.exception.cue_ids, [1404])
        self.assertIn("translation_error", segments[0])
        self.assertEqual(translate.await_count, 3)

    async def test_free_translation_uses_chinese_even_when_english_reference_is_wrong(self):
        segments = cues()
        translations = dict(zip([seg["text"] for seg in segments], ["សៀវភៅ", "ប្រជារាស្ត្រ", "អត្ថបទ"]))
        async def translate(text, source_lang, target_lang, tone="casual"):
            self.assertEqual(source_lang, "zh")
            return "Incorrect English reference" if target_lang == "en" else translations[text]
        with patch("backend.app.engines.dialogue_translator.get_engine", return_value=SimpleNamespace(translate=translate)):
            await translate_dialogue_clusters_free(segments, "zh")
        self.assertEqual([seg["translation"] for seg in segments], ["សៀវភៅ", "ប្រជារាស្ត្រ", "អត្ថបទ"])

    async def test_cleaned_empty_output_is_retried(self):
        segments = [{"id": 1, "text": "Hello"}]
        translate = AsyncMock(side_effect=["4K超清", "សួស្តី"])
        with patch("backend.app.engines.dialogue_translator.get_engine", return_value=SimpleNamespace(translate=translate)):
            await translate_dialogue_clusters_free(segments, "en")
        self.assertEqual(segments[0]["translation"], "សួស្តី")


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        delay = patch("backend.app.video.recovery.asyncio.sleep", new=AsyncMock())
        delay.start()
        self.addCleanup(delay.stop)
        cleanup = patch("backend.app.video.pipeline.cleanup_processing_cache")
        cleanup.start()
        self.addCleanup(cleanup.stop)

    async def test_resume_generates_only_missing_audio_and_finishes_the_saved_job(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, cached = root / "video.mp4", root / "cached.mp3"
            source.touch(); cached.write_bytes(b"voice")
            segments = [{"id": 1, "start": 0, "end": 1, "text": "one", "translation": "មួយ", "audio_file": str(cached)},
                        {"id": 2, "start": 1, "end": 2, "text": "two", "translation": "ពីរ"}]
            job = {"status": "failed", "video_path": str(source), "segments": segments}

            async def generate(pending, *args, **kwargs):
                self.assertEqual([seg["id"] for seg in pending], [2])
                pending[0]["audio_file"] = str(cached)
                return pending

            speech = AsyncMock(side_effect=generate)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                with patch.object(video_routes, "get_job_status", return_value=job), patch.object(video_routes, "_save_job"), patch.object(video_routes, "ROOT_PROJECT_DIR", root), patch.object(video_routes, "synthesize_segments", speech), patch.object(video_routes, "assemble_dubbed_video", new=AsyncMock(return_value={"download_url": "video.mp4"})):
                    response = await client.post("/api/video/job/test/resume", json={})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["status"], "queued")
            self.assertEqual(job["status"], "completed")
            speech.assert_awaited_once()

    async def test_unchanged_explicit_voice_settings_reuse_cached_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, audio = root / "video.mp4", root / "cached.mp3"
            source.touch(); audio.write_bytes(b"voice")
            job = {"status": "completed", "video_path": str(source), "segments": [{"id": 1, "start": 0, "end": 1, "translation": "មួយ", "audio_file": str(audio), "prosody": {"rate": "+10%", "pitch": "+0Hz", "volume": "+5%"}}]}
            speech = AsyncMock()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                with patch.object(video_routes, "get_job_status", return_value=job), patch.object(video_routes, "_save_job"), patch.object(video_routes, "synthesize_segments", speech), patch.object(video_routes, "assemble_dubbed_video", new=AsyncMock(return_value={})):
                    response = await client.post("/api/video/job/test/rerender", json={"voice_rate": "+10%", "voice_pitch": "+0Hz", "voice_volume": "+5%"})
            self.assertEqual(response.status_code, 200)
            speech.assert_not_awaited()

    async def test_failed_voice_generation_never_produces_a_completed_video(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "video.mp4"
            source.touch()
            job = {"video_path": str(source), "segments": [{"id": 1, "start": 0, "end": 1, "translation": "មួយ"}]}
            renderer = AsyncMock()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                with patch.object(video_routes, "get_job_status", return_value=job), patch.object(video_routes, "_save_job"), patch.object(video_routes, "synthesize_segments", new=AsyncMock()), patch.object(video_routes, "assemble_dubbed_video", renderer):
                    response = await client.post("/api/video/job/test/rerender", json={})
            self.assertEqual(response.status_code, 502)
            self.assertEqual(job["status"], "failed")
            renderer.assert_not_awaited()

    async def test_repair_only_changes_missing_cues_and_invalidates_their_audio(self):
        existing = {**cues()[0], "translation": "ការបកប្រែរបស់ខ្ញុំ", "audio_file": "keep.mp3"}
        job = {"segments": [existing, cues()[1]], "detected_language": "zh"}

        async def fill(segments, *args, **kwargs):
            self.assertEqual([seg["id"] for seg in segments], [1405])
            segments[0]["translation"] = "ប្រគល់ឱ្យប្រជារាស្ត្រ"

        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        with patch.object(video_routes, "get_job_status", return_value=job), patch.object(video_routes, "_save_job") as save, patch("backend.app.engines.dialogue_translator.translate_dialogue_segments", new=AsyncMock(side_effect=fill)):
            async with client:
                response = await client.post("/api/video/job/test/translate-missing", json={"engine": "free"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["repaired_count"], 1)
        self.assertEqual(response.json()["remaining_count"], 0)
        self.assertEqual(job["segments"][0], existing)
        self.assertIsNone(job["segments"][1]["audio_file"])
        self.assertTrue(job["segments"][1]["_dirty"])
        self.assertTrue(job["needs_rerender"])
        save.assert_called_once_with("test")

    async def test_partial_repair_is_saved_and_reported(self):
        job = {"segments": cues()[:2]}

        async def partial(segments, *args, **kwargs):
            segments[0]["translation"] = "សៀវភៅ"
            segments[1]["translation_error"] = "Retry translation"
            raise MissingTranslationsError([segments[1]])

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            with patch.object(video_routes, "get_job_status", return_value=job), patch.object(video_routes, "_save_job") as save, patch("backend.app.engines.dialogue_translator.translate_dialogue_segments", new=AsyncMock(side_effect=partial)):
                response = await client.post("/api/video/job/test/translate-missing", json={"engine": "free"})
        self.assertEqual(response.json()["status"], "partial")
        self.assertEqual((response.json()["repaired_count"], response.json()["remaining_count"]), (1, 1))
        save.assert_called_once()

    async def test_tts_never_speaks_untranslated_source_as_khmer(self):
        with tempfile.TemporaryDirectory() as directory, patch("backend.app.video.tts.edge_tts.Communicate") as speech:
            with self.assertRaises(MissingTranslationsError):
                await synthesize_segments(cues(), Path(directory))
            speech.assert_not_called()

    async def test_pipeline_completes_other_voices_and_flags_original_speech_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "movie.mp4"
            source.touch()
            audio = root / "speech.mp3"
            audio.write_bytes(b"voice")
            job = {"id": "test"}
            segments = cues()

            async def partial(items, *args, **kwargs):
                for seg in items:
                    if seg["id"] == 1404:
                        seg["translation"] = "សៀវភៅ"
                    else:
                        seg["translation_error"] = "provider unavailable"
                raise MissingTranslationsError([seg for seg in items if seg.get("translation_error")])

            async def generate(items, *args, **kwargs):
                for seg in items:
                    seg["audio_file"] = str(audio)

            speech = AsyncMock(side_effect=generate)
            with patch.dict(pipeline.JOBS, {"test": job}), patch.object(pipeline, "_save_job"), patch.object(pipeline, "transcribe_video_audio", new=AsyncMock(return_value={"segments": segments, "language": "zh"})), patch("backend.app.engines.dialogue_translator.translate_dialogue_segments", new=AsyncMock(side_effect=partial)), patch("backend.app.video.recovery.extract_original_speech", return_value=str(audio)), patch.object(pipeline, "synthesize_segments", speech), patch.object(pipeline, "assemble_dubbed_video", new=AsyncMock(return_value={})):
                await pipeline.run_video_dubbing_pipeline("test", str(source), is_url=False)
            self.assertEqual(job["status"], "completed")
            self.assertEqual(job["fallback_count"], 2)
            self.assertEqual(job["segments"][0]["translation"], "សៀវភៅ")
            speech.assert_awaited_once()
            self.assertEqual(len(speech.await_args.args[0]), 1)

    async def test_rerender_reports_failure_when_neither_translation_nor_original_audio_is_usable(self):
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "video.mp4"
            video.touch()
            job = {"video_path": str(video), "segments": cues()}
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                with patch.object(video_routes, "get_job_status", return_value=job), patch.object(video_routes, "_save_job"), patch("backend.app.engines.dialogue_translator.translate_dialogue_segments", new=AsyncMock(side_effect=MissingTranslationsError(cues()))):
                    response = await client.post("/api/video/job/test/rerender", json={})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(job["status"], "failed")
