"""Task deletion, cancellation, restart recovery, and explicit cache reset."""

import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from backend.app.main import app
from backend.app.video import batch, pipeline
from backend.app.video.cache import cache_in_use, purge_processing_cache
from backend.app.video.resources import run_blocking
from backend.app.video.task_management import clear_tasks_and_cache, remove_task, pause_task, clear_job_step
from backend.app.video.checkpoints import job_steps


class TaskManagementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / 'storage'
        self.root.mkdir()
        for replacement in (
            patch.dict(pipeline.JOBS, {}, clear=True),
            patch.dict(pipeline.JOB_TASKS, {}, clear=True),
            patch.dict(pipeline._LAST_CHECKPOINT, {}, clear=True),
            patch.object(pipeline, 'RUNNING_JOBS', set()),
            patch.object(pipeline, 'DELETED_JOBS', set()),
            patch.object(pipeline, 'RESETTING', False),
            patch.object(pipeline, 'JOBS_FILE', self.root / 'jobs.json'),
            patch.dict(batch.TASKS, {}, clear=True),
            patch.object(batch, 'BATCH_DIR', self.root / 'batches'),
            patch.object(batch, 'SPLITS_DIR', self.root / 'splits'),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test')
        self.addAsyncCleanup(self.client.aclose)

    def file(self, name):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'source or disposable cache')
        return path

    def job(self, name, status='completed'):
        pipeline.JOBS[name] = {'id': name, 'status': status, 'input': str(self.root / f'{name}.mp4'), 'progress': 61}
        pipeline._save_job(name)

    async def test_pause_retains_completed_work_releases_worker_and_survives_restart(self):
        self.job('live', 'queued')
        voice = self.file('downloads/voice_live/seg_1.mp3')
        pipeline.JOBS['live']['segments'] = [{'id': 1, 'text': 'source', 'translation': 'saved', 'audio_file': str(voice)}]
        started = asyncio.Event()
        async def work(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()
        with patch.object(pipeline, '_run_video_dubbing_pipeline', new=AsyncMock(side_effect=work)):
            task = asyncio.create_task(pipeline.run_video_dubbing_pipeline('live', 'source.mp4'))
            await asyncio.wait_for(started.wait(), 2)
            response = await self.client.post('/api/video/job/live/pause')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(task.cancelled())
        self.assertFalse(pipeline.RUNNING_JOBS)
        self.assertFalse(pipeline.JOB_TASKS)
        self.assertEqual(pipeline.JOBS['live']['status'], 'paused')
        self.assertEqual(pipeline.JOBS['live']['segments'][0]['translation'], 'saved')
        self.assertTrue(voice.exists())
        pipeline.mark_interrupted_jobs()
        self.assertEqual(json.loads(pipeline.JOBS_FILE.read_text())['live']['status'], 'paused')
        self.assertEqual((await self.client.post('/api/video/job/live/pause')).status_code, 200)

    async def test_pause_before_background_registration_prevents_a_late_worker(self):
        self.job('queued', 'queued')
        await pause_task('queued')
        with patch.object(pipeline, '_run_video_dubbing_pipeline', new=AsyncMock()) as work:
            await pipeline.run_video_dubbing_pipeline('queued', 'source.mp4')
        work.assert_not_awaited()
        self.assertEqual(pipeline.JOBS['queued']['status'], 'paused')

    async def test_polishing_counts_as_running_and_can_be_paused_without_losing_dialogue(self):
        self.job('polish', 'completed')
        pipeline.JOBS['polish']['segments'] = [{'id':1,'text':'source','start':0,'end':1,'translation':'saved'}]
        started = asyncio.Event()
        async def work(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()
        with patch('backend.app.engines.dialogue_translator.translate_dialogue_segments', new=AsyncMock(side_effect=work)):
            request = asyncio.create_task(self.client.post('/api/video/job/polish/ai-polish-dialogue', json={}))
            await asyncio.wait_for(started.wait(), 2)
            jobs = (await self.client.get('/api/video/jobs')).json()
            self.assertEqual(jobs['active_count'], 1)
            self.assertEqual(jobs['jobs'][0]['status'], 'processing')
            self.assertIn('Polishing', jobs['jobs'][0]['stage'])
            self.assertEqual((await self.client.post('/api/video/job/polish/ai-polish-dialogue', json={})).status_code, 409)
            await pause_task('polish')
            await asyncio.gather(request, return_exceptions=True)
        self.assertEqual(pipeline.JOBS['polish']['status'], 'paused')
        self.assertEqual(pipeline.JOBS['polish']['segments'][0]['translation'], 'saved')

    async def test_pause_waits_for_native_worker_before_allowing_checkpoint_clear(self):
        self.job('native', 'queued')
        pipeline.JOBS['native']['video_path'] = str(self.file('uploads/native.mp4'))
        started, release = threading.Event(), threading.Event()
        def native():
            started.set()
            release.wait(3)
        async def work(*args, **kwargs):
            await run_blocking(native)
        with patch.object(pipeline, '_run_video_dubbing_pipeline', new=AsyncMock(side_effect=work)):
            worker = asyncio.create_task(pipeline.run_video_dubbing_pipeline('native', 'source.mp4'))
            await asyncio.to_thread(started.wait, 2)
            pausing = asyncio.create_task(pause_task('native'))
            await asyncio.sleep(.02)
            try:
                self.assertFalse(pausing.done())
                self.assertEqual(pipeline.JOBS['native']['status'], 'pausing')
                self.assertIn('native', pipeline.RUNNING_JOBS)
            finally:
                release.set()
                await asyncio.wait_for(pausing, 2)
                await asyncio.gather(worker, return_exceptions=True)
        self.assertEqual(pipeline.JOBS['native']['status'], 'paused')
        self.assertTrue(next(step for step in job_steps(pipeline.JOBS['native']) if step['id'] == 'prepare')['can_clear'])

    async def test_clear_steps_requires_reverse_order_and_preserves_source_and_exports(self):
        self.job('saved')
        source = self.file('uploads/source.mp4')
        voice = self.file('downloads/voice_saved/seg_1.mp3')
        export = self.file('dubbed/finished.mp4')
        original = {'id': 1, 'text': 'original source', 'start': 1, 'end': 2}
        job = pipeline.JOBS['saved']
        job.update(video_path=str(source), pipeline_phase='completed', transcript_segments=[original],
                   segments=[{**original, 'translation': 'saved Khmer', 'audio_file': str(voice)}],
                   result={'dubbed_video_path': str(export)})
        rejected = await self.client.delete('/api/video/job/saved/steps/translation')
        self.assertEqual(rejected.status_code, 409)
        self.assertIn('step 5', rejected.json()['detail'])
        for step, progress in [('render', 85), ('voices', 70), ('translation', 50)]:
            response = await self.client.delete(f'/api/video/job/saved/steps/{step}')
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['progress'], progress)
        self.assertEqual(job['segments'], [original])
        self.assertEqual(job['status'], 'paused')
        self.assertTrue(source.exists())
        self.assertTrue(export.exists())
        self.assertNotEqual(job['output_filename'], export.name)
        self.assertEqual((await self.client.delete('/api/video/job/saved/steps/translation')).status_code, 409)
        await clear_job_step('saved', 'transcription')
        self.assertEqual(job['segments'], [])
        self.assertTrue(job['transcription_revision'])
        self.assertNotIn('transcript_segments', job)
        await clear_job_step('saved', 'prepare')
        self.assertNotIn('video_path', job)
        self.assertTrue(source.exists())
        self.assertTrue(all(step['state'] == 'pending' for step in job_steps(job)))

    async def test_running_task_cannot_clear_steps_or_pause_a_finished_task(self):
        self.job('active', 'processing')
        self.assertEqual((await self.client.delete('/api/video/job/active/steps/prepare')).status_code, 409)
        self.job('finished')
        self.assertEqual((await self.client.post('/api/video/job/finished/pause')).status_code, 409)
        self.assertEqual((await self.client.post('/api/video/job/missing/pause')).status_code, 404)
        self.assertEqual((await self.client.delete('/api/video/job/missing/steps/render')).status_code, 404)

    async def test_paused_jobs_remain_visible_without_using_a_worker(self):
        self.job('paused', 'paused')
        response = await self.client.get('/api/video/jobs?recent_limit=0')
        self.assertEqual([job['id'] for job in response.json()['jobs']], ['paused'])
        self.assertEqual(response.json()['active_count'], 0)

    async def test_resume_cleared_translation_reuses_source_transcript(self):
        from backend.app.api import video_routes
        self.job('resume', 'paused')
        source = self.file('uploads/resume.mp4')
        job = pipeline.JOBS['resume']
        job.update(video_path=str(source), pipeline_phase='transcribed',
                   segments=[{'id': 1, 'text': 'source words', 'start': 0, 'end': 1, 'translation': 'old'}])
        await clear_job_step('resume', 'translation')
        async def translate(segments, *args, **kwargs):
            self.assertNotIn('translation', segments[0])
            for cue in segments:
                cue['translation'] = 'new Khmer'
        with patch('backend.app.engines.dialogue_translator.translate_dialogue_segments', new=AsyncMock(side_effect=translate)) as translator, patch(
            'backend.app.video.recovery.prepare_dialogue_audio', new=AsyncMock(return_value={'fallback_count': 0})
        ), patch.object(video_routes, 'assemble_dubbed_video', new=AsyncMock(return_value={'dubbed_video_path': 'new.mp4'})), patch.object(
            pipeline, 'transcribe_video_audio', new=AsyncMock()
        ) as transcribe, patch.object(video_routes, 'cleanup_saved_job_caches'):
            response = await self.client.post('/api/video/job/resume/resume', json={
                'engine': 'openai', 'base_url': 'http://localhost:11434/v1', 'model': 'qwen3.5:9b',
            })
        self.assertEqual(response.status_code, 200)
        translator.assert_awaited_once()
        transcribe.assert_not_awaited()
        self.assertEqual(job['segments'][0]['translation'], 'new Khmer')
        self.assertEqual(job['status'], 'completed')
        self.assertFalse(job.get('pause_requested'))

    async def test_concurrent_duplicate_step_clears_only_clear_once(self):
        self.job('saved')
        pipeline.JOBS['saved'].update(video_path='source.mp4', pipeline_phase='transcribed',
                                      segments=[{'id': 1, 'text': 'source', 'translation': 'old'}])
        responses = await asyncio.gather(*(self.client.delete('/api/video/job/saved/steps/translation') for _ in range(2)))
        self.assertEqual(sorted(response.status_code for response in responses), [200, 409])
        self.assertTrue(next(step for step in job_steps(pipeline.JOBS['saved']) if step['id'] == 'transcription')['can_clear'])

    async def test_pausing_one_batch_clip_does_not_stop_siblings_or_auto_resume_it(self):
        for name in ('one.mp4', 'two.mp4'):
            self.file('splits/' + name)
        created = await batch.create_batch(['one.mp4', 'two.mp4'], {'engine_name': 'free'})
        for job in pipeline.JOBS.values():
            job['segments'] = [{'id': 1, 'text': 'saved'}]
        started, release = asyncio.Event(), asyncio.Event()
        seen = []
        async def work(job_id, *args, **kwargs):
            seen.append(job_id)
            if len(seen) == 2:
                started.set()
            await release.wait()
            pipeline.JOBS[job_id]['status'] = 'completed'
        with patch.object(pipeline.config, 'VIDEO_MAX_CONCURRENT_JOBS', 2), patch.object(
            pipeline, '_run_video_dubbing_pipeline', new=AsyncMock(side_effect=work)
        ), patch.object(
            batch, '_package_batch', return_value='archive.zip'
        ) as package:
            running = asyncio.create_task(batch._run_batch(created['id']))
            await asyncio.wait_for(started.wait(), 2)
            paused_id = created['clips'][0]['job_id']
            await pause_task(paused_id)
            self.assertFalse(running.done())
            self.assertEqual(len(pipeline.RUNNING_JOBS), 1)
            release.set()
            await asyncio.wait_for(running, 2)
            self.assertEqual(batch.read_batch(created['id'])['status'], 'paused')
            package.assert_not_called()
            await batch._run_batch(created['id'])
            self.assertEqual(seen.count(paused_id), 1)
            pipeline.JOBS[paused_id].update(status='completed', pause_requested=False)
            await batch.finalize_resumed_job(pipeline.JOBS[paused_id])
            package.assert_called_once()
            self.assertEqual(batch.read_batch(created['id'])['status'], 'completed')

    async def test_remove_is_durable_and_keeps_other_tasks_sources_and_exports(self):
        source, export = self.file('uploads/source.mp4'), self.file('dubbed/final.mp4')
        self.job('remove')
        self.job('keep')
        response = await self.client.delete('/api/video/job/remove')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(pipeline.JOBS), {'keep'})
        self.assertEqual(set(json.loads(pipeline.JOBS_FILE.read_text())), {'keep'})
        self.assertTrue(source.exists())
        self.assertTrue(export.exists())
        self.assertIsNone(pipeline.get_job_status('remove'))
        pipeline._save_job('remove')
        self.assertNotIn('remove', json.loads(pipeline.JOBS_FILE.read_text()))
        self.assertEqual((await self.client.delete('/api/video/job/remove')).status_code, 404)

    async def test_remove_cancels_one_live_task_and_releases_its_slot(self):
        self.job('live', 'queued')
        started = asyncio.Event()
        async def worker(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()
        with patch.object(pipeline, '_run_video_dubbing_pipeline', new=AsyncMock(side_effect=worker)):
            task = asyncio.create_task(pipeline.run_video_dubbing_pipeline('live', 'source.mp4'))
            await asyncio.wait_for(started.wait(), 2)
            response = await self.client.delete('/api/video/job/live')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(task.cancelled())
        self.assertFalse(pipeline.RUNNING_JOBS)
        self.assertFalse(pipeline.JOB_TASKS)
        self.assertNotIn('live', pipeline.JOBS)

    async def test_queued_removal_does_not_cancel_another_running_task(self):
        self.job('first', 'queued')
        self.job('waiting', 'queued')
        started, release = asyncio.Event(), asyncio.Event()
        async def worker(*args, **kwargs):
            started.set()
            await release.wait()
        with patch.object(pipeline.config, 'VIDEO_MAX_CONCURRENT_JOBS', 1), patch.object(
            pipeline, '_run_video_dubbing_pipeline', new=AsyncMock(side_effect=worker)
        ) as run:
            first = asyncio.create_task(pipeline.run_video_dubbing_pipeline('first', 'one.mp4'))
            await asyncio.wait_for(started.wait(), 2)
            waiting = asyncio.create_task(pipeline.run_video_dubbing_pipeline('waiting', 'two.mp4'))
            await asyncio.sleep(0)
            try:
                await remove_task('waiting')
                self.assertTrue(waiting.cancelled())
                self.assertFalse(first.done())
                run.assert_awaited_once()
                self.assertEqual((await self.client.delete('/api/video/jobs')).status_code, 409)
            finally:
                release.set()
                await first

    async def test_removed_batch_clip_stays_removed_while_other_clips_finish(self):
        names = ['one.mp4', 'two.mp4', 'three.mp4']
        for name in names:
            self.file('splits/' + name)
        created = await batch.create_batch(names, {'engine_name': 'free'})
        for job in pipeline.JOBS.values():
            job['segments'] = [{'id': 1, 'text': 'saved'}]
        started, release = asyncio.Event(), asyncio.Event()
        count = 0
        async def worker(job_id, *args, **kwargs):
            nonlocal count
            count += 1
            if count == 3:
                started.set()
            await release.wait()
            pipeline.JOBS[job_id]['status'] = 'completed'
        with patch.object(pipeline.config, 'VIDEO_MAX_CONCURRENT_JOBS', 3), patch.object(
            pipeline, '_run_video_dubbing_pipeline', new=AsyncMock(side_effect=worker)
        ), patch.object(batch, '_package_batch', return_value='archive.zip'):
            running = asyncio.create_task(batch._run_batch(created['id']))
            await asyncio.wait_for(started.wait(), 2)
            removed = created['clips'][0]['job_id']
            try:
                await remove_task(removed)
                self.assertFalse(running.done())
                self.assertEqual(len(pipeline.RUNNING_JOBS), 2)
            finally:
                release.set()
                await asyncio.wait_for(running, 2)
        saved = batch.read_batch(created['id'])
        self.assertTrue(saved['clips'][0]['removed'])
        self.assertEqual(saved['status'], 'completed')
        self.assertNotIn(removed, pipeline.JOBS)

    async def test_clear_removes_task_caches_and_batches_but_retains_media_and_settings(self):
        self.job('old')
        disposable = [self.file(name) for name in (
            'transcripts/source.json', 'downloads/voice_old/seg_1.mp3',
            'dubbed/temp_old/master_khmer_voice.wav', 'dubbed/preview_old.mp3', 'uploads/source_16k.pcm',
        )]
        retained = [self.file(name) for name in (
            'uploads/source.mp4', 'uploads/source.srt', 'dubbed/final.mp4', 'dubbed/final.srt',
            'splits/part01.mp4', 'exports/finished.zip', 'logos/custom.png',
        )]
        self.file('batches/123456abcdef.json')
        response = await self.client.delete('/api/video/jobs')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['cache']['deleted_files'], len(disposable))
        self.assertTrue(all(not path.exists() for path in disposable))
        self.assertTrue(all(path.exists() for path in retained))
        self.assertEqual(pipeline.JOBS, {})
        self.assertEqual(json.loads(pipeline.JOBS_FILE.read_text()), {})
        self.assertFalse(batch.BATCH_DIR.exists())
        self.assertFalse(pipeline.RESETTING)

    async def test_in_use_cache_prevents_reset_without_discarding_saved_tasks(self):
        self.job('keep')
        cache = self.file('transcripts/cached.json')
        with cache_in_use(cache.parent):
            response = await self.client.delete('/api/video/jobs')
        self.assertEqual(response.status_code, 409)
        self.assertIn('keep', pipeline.JOBS)
        self.assertTrue(cache.exists())
        self.assertFalse(pipeline.RESETTING)

    async def test_purge_does_not_follow_symlinks(self):
        outside = self.root.parent / 'outside'
        outside.mkdir()
        protected = outside / 'transcript.json'
        protected.write_text('keep')
        (self.root / 'transcripts').symlink_to(outside, target_is_directory=True)
        report = purge_processing_cache(self.root)
        self.assertEqual(report.deleted_files, 0)
        self.assertTrue(protected.exists())

    async def test_cancellation_waits_for_native_worker_before_releasing_leases(self):
        started, release = threading.Event(), threading.Event()
        def native_work():
            started.set()
            release.wait(3)
        task = asyncio.create_task(run_blocking(native_work))
        await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(.02)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
