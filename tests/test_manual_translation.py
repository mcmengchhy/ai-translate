"""Manual dialogue exchange preserves cue alignment and never invokes a translator."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from backend.app.main import app
from backend.app.api import video_routes
from backend.app.video import pipeline
from backend.app.video import batch as video_batch
from backend.app.video.manual_translation import (
    apply_translation, dialogue_batches, numbered_text, parse_numbered_translation,
    source_fingerprint, translation_details,
    missing_translation_details, apply_missing_translation,
)


def example_job():
    return {"id": "manual", "title": "Chinese episode", "status": "paused", "progress": 50,
            "detected_language": "zh", "segments": [
                {"id": 7, "start": 1.2, "end": 2.6, "text": "今天下雨了。"},
                {"id": 12, "start": 4.0, "end": 5.8, "text": "记得带伞。"},
            ]}


KHMER = "[7] ថ្ងៃនេះមានភ្លៀងធ្លាក់។\n[12] កុំភ្លេចយកឆ័ត្រទៅផង។"


class ManualExchangeTests(unittest.TestCase):
    def test_missing_board_collects_all_batches_and_updates_only_failed_translations(self):
        job = example_job()
        job['segments'][0]['translation'] = '...'
        job['segments'][1].update(translation='កុំភ្លេចយកឆ័ត្រ',audio_file='saved-voice.mp3',prosody={'rate':'+10%'})
        job['segments'].append({'id':355,'start':900,'end':902,'text':'最后一句。','translation_error':'failed'})
        good = copy.deepcopy(job['segments'][1])
        with patch('backend.app.video.manual_translation.MAX_BATCH_CHARACTERS',20):
            self.assertGreater(len(dialogue_batches(job)),1)
            board = missing_translation_details(job)
            self.assertEqual([row['id'] for row in board['cues']],[7,355])
            self.assertNotIn('[12]',board['source_text'])
            result = apply_missing_translation(job,'[៣៥៥] អរគុណ។ [7] សួស្តី។',board['source_fingerprint'],board['missing_fingerprint'])
        self.assertEqual(result['updated_cue_ids'],[7,355])
        self.assertEqual(result['remaining_cues'],0)
        self.assertEqual(job['segments'][1],good)
        self.assertEqual(job['segments'][2]['start'],900)
        self.assertEqual(job['segments'][0]['translation'],'សួស្តី។')
        self.assertTrue(job['manual_translation'])

    def test_missing_board_rejects_stale_or_invalid_pastes_without_any_partial_changes(self):
        for text in ('[7] សួស្តី','[7] សួស្តី [7] អរគុណ [12] លាហើយ',
                     '[7] ... [12] អរគុណ','[7] 今天下雨了 [12] អរគុណ',
                     '[7] សួស្តី [12] អរគុណ [99] លាហើយ'):
            with self.subTest(text=text):
                job = example_job()
                board = missing_translation_details(job)
                before = copy.deepcopy(job)
                with self.assertRaises(ValueError):
                    apply_missing_translation(job,text,board['source_fingerprint'],board['missing_fingerprint'])
                self.assertEqual(job,before)
        for changed in ('source','translation'):
            job = example_job()
            board = missing_translation_details(job)
            job['segments'][0]['text' if changed=='source' else 'translation'] = 'Changed'
            before = copy.deepcopy(job)
            with self.assertRaises(RuntimeError):
                apply_missing_translation(job,KHMER,board['source_fingerprint'],board['missing_fingerprint'])
            self.assertEqual(job,before)

    def test_punctuation_placeholders_are_missing_text_and_cannot_start_tts(self):
        from backend.app.video.segments import missing_translations
        job = example_job()
        job['segments'][0]['translation'] = '...'
        job['segments'][1]['translation'] = 'កុំភ្លេចយកឆ័ត្រទៅផង។'
        self.assertEqual([cue['id'] for cue in missing_translations(job['segments'])], [7])
        self.assertFalse(translation_details(job)['can_generate'])
        before = copy.deepcopy(job)
        for punctuation in ('...', '…', '---', '។', '?!'):
            with self.subTest(punctuation=punctuation):
                with self.assertRaisesRegex(ValueError, 'only punctuation'):
                    apply_translation(job, 0, f'[7] {punctuation}\n[12] សួស្តី', source_fingerprint(job))
                self.assertEqual(job, before)

    def test_google_paragraphs_with_multiple_inline_markers_keep_each_translation_separate(self):
        parsed = parse_numbered_translation(
            "[886] សួស្តី។ [887] អរគុណ។\n[៨៨៨] កុំភ្លេច\nយកឆ័ត្រ។ 【 ៨៨៩ 】 លាហើយ។"
        )
        self.assertEqual(parsed, {886: "សួស្តី។", 887: "អរគុណ។", 888: "កុំភ្លេច យកឆ័ត្រ។", 889: "លាហើយ។"})

    def test_inline_import_preserves_cue_alignment_and_rejects_duplicates(self):
        job = example_job()
        apply_translation(job, 0, KHMER.replace("\n", " "), source_fingerprint(job))
        self.assertEqual(job["segments"][0]["translation"], "ថ្ងៃនេះមានភ្លៀងធ្លាក់។")
        self.assertEqual(job["segments"][1]["translation"], "កុំភ្លេចយកឆ័ត្រទៅផង។")
        with self.assertRaisesRegex(ValueError, "more than once"):
            parse_numbered_translation("[7] សួស្តី [12] អរគុណ [៧] លាហើយ")

    def test_unicode_numbers_and_wrapped_google_output_keep_cue_numbers(self):
        parsed = parse_numbered_translation("\ufeff[៧]\nថ្ងៃនេះមានភ្លៀងធ្លាក់។\n\n១២. កុំភ្លេច\nយកឆ័ត្រទៅផង។")
        self.assertEqual(set(parsed), {7, 12})
        self.assertEqual(parsed[12], "កុំភ្លេច យកឆ័ត្រទៅផង។")

    def test_incomplete_duplicate_unknown_and_chinese_imports_are_atomic(self):
        for text in ("[7] សួស្តី", KHMER + "\n[12] សួស្តី", KHMER + "\n[50] សួស្តី",
                     "[7] 今天下雨了。\n[12] សួស្តី", "[7]\n[12] សួស្តី", "សួស្តី"):
            with self.subTest(text=text):
                job = example_job()
                before = copy.deepcopy(job)
                with self.assertRaises(ValueError):
                    apply_translation(job, 0, text, source_fingerprint(job))
                self.assertEqual(job, before)

    def test_import_preserves_original_dialogue_timestamps_and_other_batches(self):
        job = example_job()
        job["segments"][0].update(translation="Old", audio_file="old.mp3", translation_error="old error",
                                  target_start=1.3, target_end=2.5, audio_source="original_fallback")
        before = copy.deepcopy(job["segments"])
        result = apply_translation(job, 0, KHMER, source_fingerprint(job))
        for old, new in zip(before, job["segments"]):
            for key in ("id", "start", "end", "text", "target_start", "target_end"):
                self.assertEqual(old.get(key), new.get(key))
            self.assertNotIn("translation_error", new)
            self.assertTrue(new["_dirty"])
            self.assertIsNone(new["audio_file"])
        self.assertEqual(result["remaining_cues"], 0)
        self.assertTrue(result["can_generate"])
        self.assertTrue(job["manual_translation"])
        self.assertEqual(job["step_states"]["voices"], "pending")
        self.assertEqual(job["status"], "awaiting_translation")
        self.assertIn("_manual_", job["output_filename"])

    def test_source_edits_invalidate_previously_exported_batch(self):
        job = example_job()
        fingerprint = source_fingerprint(job)
        job["segments"][0]["start"] = 0.8
        with self.assertRaisesRegex(RuntimeError, "original dialogue changed"):
            apply_translation(job, 0, KHMER, fingerprint)
        self.assertNotIn("translation", job["segments"][0])

    def test_long_transcripts_are_batched_without_splitting_or_reindexing_cues(self):
        job = example_job()
        job["segments"] = [{"id": i * 2, "start": i, "end": i + 1, "text": "这是一个很长的句子。" * 30}
                           for i in range(30)]
        batches = dialogue_batches(job)
        self.assertGreater(len(batches), 1)
        self.assertEqual([seg["id"] for batch in batches for seg in batch], [seg["id"] for seg in job["segments"]])
        self.assertTrue(all(len(numbered_text(batch)) <= 3500 for batch in batches))
        fingerprint = source_fingerprint(job)
        translated = "\n".join(f"[{seg['id']}] សួស្តី" for seg in batches[0])
        apply_translation(job, 0, translated, fingerprint)
        self.assertEqual(source_fingerprint(job), fingerprint)
        self.assertNotIn("translation", batches[1][0])


class ManualWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "storage").mkdir()
        for replacement in (
            patch.dict(pipeline.JOBS, {}, clear=True),
            patch.dict(pipeline._LAST_CHECKPOINT, {}, clear=True),
            patch.object(pipeline, "RUNNING_JOBS", set()),
            patch.object(pipeline, "JOB_TASKS", {}),
            patch.object(pipeline, "JOBS_FILE", self.root / "storage/jobs.json"),
            patch.object(pipeline, "cleanup_saved_job_caches"),
            patch.object(video_routes, "ROOT_PROJECT_DIR", self.root),
            patch.object(video_routes, "STORAGE_UPLOADS", self.root / "storage/uploads"),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)
        self.job = example_job()
        self.source = self.root / "source.mp4"
        self.source.touch()
        self.job.update(input=str(self.source), video_path=str(self.source))
        pipeline.JOBS["manual"] = self.job

    async def test_manual_url_and_upload_submissions_save_and_pass_the_workflow(self):
        with patch.object(video_routes, "run_video_dubbing_pipeline", new=AsyncMock()) as run:
            url = await self.client.post("/api/video/dub-url", json={"url": "https://example.com/manual", "manual_translation": True})
            upload = await self.client.post("/api/video/dub-upload", files={"file": ("manual.mp4", b"unique")},
                                            data={"manual_translation": "true"})
        for response in (url, upload):
            self.assertEqual(response.status_code, 200)
            self.assertTrue(pipeline.JOBS[response.json()["job_id"]]["request_options"]["manual_translation"])
        self.assertTrue(all(call.kwargs["manual_translation"] for call in run.await_args_list))

    async def test_missing_board_api_updates_only_missing_cues_and_persists_them(self):
        self.job['segments'][1].update(translation='កុំភ្លេចយកឆ័ត្រ',audio_file='keep.mp3')
        good = copy.deepcopy(self.job['segments'][1])
        board = (await self.client.get('/api/video/job/manual/missing-translations')).json()
        self.assertEqual([cue['id'] for cue in board['cues']],[7])
        response = await self.client.post('/api/video/job/manual/missing-translations',json={
            'source_fingerprint':board['source_fingerprint'],'missing_fingerprint':board['missing_fingerprint'],'text':'[7] សួស្តី'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['remaining_cues'],0)
        self.assertEqual(self.job['segments'][1],good)
        saved = json.loads(pipeline.JOBS_FILE.read_text())['manual']
        self.assertEqual(saved['segments'][0]['translation'],'សួស្តី')
        self.assertEqual(saved['segments'][1]['audio_file'],'keep.mp3')

    async def test_missing_board_api_rejects_active_jobs_and_changed_missing_sets(self):
        board = (await self.client.get('/api/video/job/manual/missing-translations')).json()
        payload = {'source_fingerprint':board['source_fingerprint'],'missing_fingerprint':board['missing_fingerprint'],'text':KHMER}
        for active in (True,False):
            self.job['status'] = 'processing' if active else 'paused'
            if not active: self.job['segments'][0]['translation'] = 'Someone else fixed this'
            before = copy.deepcopy(self.job)
            response = await self.client.post('/api/video/job/manual/missing-translations',json=payload)
            self.assertEqual(response.status_code,409)
            self.assertEqual(self.job,before)

    async def test_manual_pipeline_waits_after_extraction_without_calling_translation_or_voices(self):
        self.job["segments"] = []
        original = example_job()["segments"]
        with patch.object(pipeline, "transcribe_video_audio", new=AsyncMock(return_value={"segments": original, "language": "zh"})), patch(
            "backend.app.engines.dialogue_translator.translate_dialogue_segments", new=AsyncMock()
        ) as translate, patch.object(pipeline, "synthesize_segments", new=AsyncMock()) as voices:
            await pipeline.run_video_dubbing_pipeline("manual", str(self.source), is_url=False, manual_translation=True)
        translate.assert_not_awaited()
        voices.assert_not_awaited()
        self.assertEqual(self.job["status"], "awaiting_translation")
        self.assertEqual(self.job["step_states"]["transcription"], "complete")
        self.assertNotIn("manual", pipeline.RUNNING_JOBS)
        self.assertEqual(pipeline.mark_interrupted_jobs(), 0)
        self.assertEqual(json.loads(pipeline.JOBS_FILE.read_text())["manual"]["status"], "awaiting_translation")

    async def test_export_and_import_round_trip_preserves_alignment(self):
        export = await self.client.get("/api/video/job/manual/transcript.txt")
        self.assertEqual(export.status_code, 200)
        self.assertIn("[7] 今天下雨了。", export.text)
        self.assertIn("attachment;", export.headers["content-disposition"])
        details = (await self.client.get("/api/video/job/manual/manual-translation")).json()
        result = await self.client.post("/api/video/job/manual/manual-translation", json={
            "batch": 0, "source_fingerprint": details["source_fingerprint"], "text": KHMER,
        })
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.json()["can_generate"])
        self.assertEqual(self.job["segments"][1]["start"], 4.0)
        self.assertEqual(self.job["request_options"]["manual_translation"], True)

    async def test_stale_or_active_import_cannot_modify_the_job(self):
        for active in (False, True):
            with self.subTest(active=active):
                self.job["status"] = "processing" if active else "paused"
                before = copy.deepcopy(self.job)
                result = await self.client.post("/api/video/job/manual/manual-translation", json={
                    "batch": 0, "source_fingerprint": source_fingerprint(self.job) if active else "0" * 64, "text": KHMER,
                })
                self.assertEqual(result.status_code, 409)
                self.assertEqual(self.job, before)

    async def test_manual_mode_requires_idle_job_and_persists(self):
        self.job["status"] = "processing"
        response = await self.client.post("/api/video/job/manual/manual-mode")
        self.assertEqual(response.status_code, 409)
        self.job["status"] = "paused"
        response = await self.client.post("/api/video/job/manual/manual-mode")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["manual_translation"])
        self.assertEqual(self.job["status"], "awaiting_translation")

    async def test_incomplete_manual_job_cannot_resume_or_fall_back_to_a_provider(self):
        self.job.update(manual_translation=True, status="awaiting_translation")
        with patch.object(video_routes, "_rerender_saved_video", new=AsyncMock()) as render:
            response = await self.client.post("/api/video/job/manual/resume", json={"manual_translation": False})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.job["status"], "awaiting_translation")
        render.assert_not_awaited()

    async def test_waiting_jobs_are_not_hidden_by_recent_job_limit(self):
        self.job["status"] = "awaiting_translation"
        data = (await self.client.get("/api/video/jobs?recent_limit=0")).json()
        self.assertEqual([job["id"] for job in data["jobs"]], ["manual"])
        self.assertEqual(data["active_count"], 0)

    async def test_batch_exports_wait_for_manually_translated_clips(self):
        self.job["status"] = "awaiting_translation"
        batch = {"id": "example", "clips": [{"job_id": "manual"}]}
        with patch.object(video_batch, "_save_batch"), patch.object(video_batch, "_package_batch") as package:
            await video_batch._finalize_batch(batch)
        self.assertEqual(batch["status"], "awaiting_translation")
        package.assert_not_called()

    async def test_imported_job_generates_voices_and_video_without_any_translation_request(self):
        apply_translation(self.job, 0, KHMER, source_fingerprint(self.job))

        async def synthesize(segments, output_dir, **options):
            Path(output_dir).mkdir(parents=True, exist_ok=True)
            for seg in segments:
                voice = Path(output_dir) / f"{seg['id']}.mp3"
                voice.write_bytes(b"mock voice audio")
                seg["audio_file"] = str(voice)

        with patch("backend.app.engines.dialogue_translator.translate_dialogue_segments", new=AsyncMock()) as translate, patch.object(
            video_routes, "synthesize_segments", side_effect=synthesize
        ) as voices, patch.object(video_routes, "assemble_dubbed_video", new=AsyncMock(return_value={"download_url": "/video.mp4"})) as render:
            response = await self.client.post("/api/video/job/manual/resume", json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.job["status"], "completed", self.job.get("error"))
        translate.assert_not_awaited()
        voices.assert_awaited_once()
        render.assert_awaited_once()
        self.assertEqual([seg["id"] for seg in render.await_args.kwargs["segments"]], [7, 12])

    async def test_processing_page_is_served_without_starting_a_real_server(self):
        response = await self.client.get("/static/processing.html?job=manual")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Copy original dialogue", response.text)


if __name__ == "__main__":
    unittest.main()
