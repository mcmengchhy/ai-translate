"""Failed cues recover independently; completed exports never silently omit speech."""
import asyncio
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend.app.engines.dialogue_translator import FLOW, translate_dialogue_segments
from backend.app.video.recovery import prepare_dialogue_audio, extract_original_speech
from backend.app.video.segments import MissingTranslationsError, MissingVoicesError, require_renderable_segments
from backend.app.video.subtitles import readable_cues
from backend.app.video.tts import synthesize_segments


class CueRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        delay = patch('backend.app.video.recovery.asyncio.sleep', new=AsyncMock())
        delay.start()
        self.addCleanup(delay.stop)
        for replacement in (
            patch('backend.app.video.tts.wait_for_service', new=AsyncMock()),
            patch('backend.app.video.tts.defer_service'),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)

    def audio(self, name):
        path = self.root / name
        path.write_bytes(b'voice')
        return str(path)

    def cue(self, index=1, translated=True):
        return {'id': index, 'start': index, 'end': index+1, 'text': 'dialogue',
                **({'translation': 'សួស្តី'} if translated else {'translation_error': 'failed', 'english_intent': 'Hello'})}

    async def test_placeholder_text_is_stopped_before_contacting_the_voice_provider(self):
        cues = [self.cue(1), {**self.cue(2), 'translation':'...'}]
        communicate = SimpleNamespace(save=AsyncMock())
        with patch('backend.app.video.tts.edge_tts.Communicate', return_value=communicate):
            with self.assertRaises(MissingTranslationsError):
                await synthesize_segments(cues, self.root, enhance=False)
        communicate.save.assert_not_awaited()

    def test_voice_summary_distinguishes_valid_files_and_retry_states(self):
        from backend.app.video.segments import voice_summary
        cues = [self.cue(i) for i in range(1, 6)]
        cues[0]['audio_file'] = self.audio('ready.mp3')
        cues[1]['audio_file'] = str(self.root/'deleted.mp3')
        cues[2].update(audio_file=self.audio('source.mp3'), audio_source='original_fallback')
        cues[3].update(audio_file=self.audio('dirty.mp3'), _dirty=True)
        cues[4].update(audio_file=self.audio('failed.mp3'), tts_error='failed')
        result = voice_summary(cues)
        self.assertEqual(result, {'voice_cues':5,'ready_voices':1,'missing_voices':4,'missing_voice_ids':[2,3,4,5]})

    async def test_only_failed_voice_is_retried_and_cached_voice_is_kept(self):
        cues = [self.cue(i) for i in (1, 2, 3)]
        cues[0]['audio_file'] = self.audio('cached.mp3')
        calls = []
        concurrency = []
        async def generate(items, *args, **kwargs):
            calls.append([seg['id'] for seg in items])
            concurrency.append(kwargs['max_concurrency'])
            for seg in items:
                if seg['id'] == 2 or len(calls) == 2:
                    seg['audio_file'] = self.audio(f"{seg['id']}.mp3")
        report = await prepare_dialogue_audio(cues, 'source.mp4', self.root, synthesize=generate)
        self.assertEqual(calls, [[2, 3], [3]])
        self.assertEqual(concurrency, [4, 1])
        self.assertEqual(cues[0]['audio_file'], str(self.root/'cached.mp3'))
        self.assertEqual(report['fallback_count'], 0)

    async def test_saved_voice_failures_retry_serially_from_the_first_pass(self):
        good, failed = self.cue(1), self.cue(2)
        good['audio_file'] = self.audio('cached.mp3')
        failed['tts_error'] = 'No audio was received'
        async def generate(items, *args, **kwargs):
            self.assertEqual([seg['id'] for seg in items], [2])
            self.assertEqual(kwargs['max_concurrency'], 1)
            items[0]['audio_file'] = self.audio('recovered.mp3')
        await prepare_dialogue_audio([good,failed], 'source.mp4', self.root, synthesize=generate, allow_original_fallback=False)
        self.assertEqual(good['audio_file'],str(self.root/'cached.mp3'))
        self.assertNotIn('tts_error',failed)

    async def test_failed_voice_pass_cools_down_before_serial_recovery(self):
        cue = self.cue()
        attempts = 0
        async def generate(items, *args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 2:
                items[0]['audio_file'] = self.audio('retry.mp3')
        with patch('backend.app.video.recovery.asyncio.sleep',new=AsyncMock()) as delay:
            await prepare_dialogue_audio([cue], 'source.mp4', self.root, synthesize=generate, allow_original_fallback=False)
        delay.assert_awaited_once_with(15)

    async def test_existing_original_speech_is_retried_as_khmer_without_rebuilding_good_clips(self):
        good, failed = self.cue(), self.cue(2)
        good['audio_file'] = self.audio('good.mp3')
        failed.update(audio_file=self.audio('original.mp3'), audio_source='original_fallback',
                      tts_error='service unavailable', generation_warning='old warning', review_required=True)
        async def generate(items, *args, **kwargs):
            self.assertEqual([seg['id'] for seg in items], [2])
            items[0]['audio_file'] = self.audio('repaired.mp3')
        voice = AsyncMock(side_effect=generate)
        report = await prepare_dialogue_audio([good, failed], 'source.mp4', self.root,
                                              synthesize=voice, allow_original_fallback=False)
        voice.assert_awaited_once()
        self.assertEqual(good['audio_file'], str(self.root/'good.mp3'))
        self.assertEqual(failed['audio_source'], 'khmer_tts')
        self.assertNotIn('generation_warning', failed)
        self.assertNotIn('tts_error', failed)
        self.assertEqual(report['fallback_count'], 0)

    async def test_strict_voice_failure_keeps_successes_and_never_extracts_original_speech(self):
        good, failed = self.cue(), self.cue(2)
        good['audio_file'] = self.audio('good.mp3')
        with patch('backend.app.video.recovery.extract_original_speech') as original:
            with self.assertRaises(MissingVoicesError) as error:
                await prepare_dialogue_audio([good, failed], 'source.mp4', self.root,
                                             synthesize=AsyncMock(), allow_original_fallback=False)
        self.assertEqual(error.exception.cue_ids, [2])
        self.assertEqual(good['audio_file'], str(self.root/'good.mp3'))
        original.assert_not_called()
        self.assertIn('original speech will not be substituted', failed['generation_warning'])

    async def test_healthy_voices_progress_while_failed_translation_retries(self):
        cues = [self.cue(), self.cue(2, translated=False)]
        healthy_done = asyncio.Event()
        translated_ids = []
        async def translate(items, *args, **kwargs):
            translated_ids.extend(seg['id'] for seg in items)
            await asyncio.wait_for(healthy_done.wait(), 1)
            items[0]['translation'] = 'ពីរ'
            items[0].pop('translation_error')
        async def generate(items, *args, **kwargs):
            for seg in items:
                seg['audio_file'] = self.audio(f"{seg['id']}.mp3")
                if seg['id'] == 1:
                    healthy_done.set()
        report = await prepare_dialogue_audio(cues, 'source.mp4', self.root, synthesize=generate, translate=translate)
        self.assertEqual(translated_ids, [2])
        self.assertEqual(report['fallback_count'], 0)

    async def test_persistent_translation_failure_exports_explicit_original_audio_and_english_caption(self):
        cue = self.cue(translated=False)
        translate = AsyncMock(side_effect=MissingTranslationsError([cue]))
        synthesize = AsyncMock()
        with patch('backend.app.video.recovery.extract_original_speech', return_value=self.audio('source.mp3')):
            report = await prepare_dialogue_audio([cue], 'source.mp4', self.root, synthesize=synthesize, translate=translate)
        self.assertEqual(translate.await_count, 2)
        synthesize.assert_not_awaited()
        self.assertEqual(report['fallback_cue_ids'], [1])
        self.assertEqual(cue['audio_source'], 'original_fallback')
        self.assertTrue(cue['review_required'])
        self.assertEqual(readable_cues([cue])[0]['text'], 'Hello')
        require_renderable_segments([cue])

    async def test_translation_repair_discards_old_source_fallback_and_clears_review_warning(self):
        cue = self.cue(translated=False)
        cue.update(audio_file=self.audio('old.mp3'), audio_source='original_fallback', generation_warning='old', review_required=True)
        async def translate(items, *args, **kwargs):
            items[0]['translation'] = 'សួស្តី'
            items[0].pop('translation_error')
        async def generate(items, *args, **kwargs):
            self.assertIsNone(items[0]['audio_file'])
            items[0]['audio_file'] = self.audio('new.mp3')
        report = await prepare_dialogue_audio([cue], 'source.mp4', self.root, translate=translate, synthesize=generate)
        self.assertEqual(report['fallback_count'], 0)
        self.assertEqual(cue['audio_source'], 'khmer_tts')
        self.assertNotIn('generation_warning', cue)
        self.assertNotIn('review_required', cue)

    async def test_persistent_tts_failure_uses_source_and_keeps_valid_khmer_subtitle(self):
        cue = self.cue()
        voice = AsyncMock()
        with patch('backend.app.video.recovery.extract_original_speech', return_value=self.audio('source.mp3')):
            report = await prepare_dialogue_audio([cue], 'source.mp4', self.root, synthesize=voice)
        self.assertEqual(voice.await_count, 2)
        self.assertEqual(report['fallback_count'], 1)
        self.assertEqual(readable_cues([cue])[0]['text'], 'សួស្តី')

    async def test_missing_original_audio_remains_an_honest_failure(self):
        cue = self.cue()
        with patch('backend.app.video.recovery.extract_original_speech', side_effect=RuntimeError('no source audio')):
            with self.assertRaisesRegex(RuntimeError, 'No generated or original speech'):
                await prepare_dialogue_audio([cue], 'source.mp4', self.root, synthesize=AsyncMock())
        self.assertIn('no source audio', cue['tts_error'])

    async def test_translation_recovery_has_a_time_limit_and_still_finishes_other_cues(self):
        cue = self.cue(translated=False)
        forever = asyncio.Event()
        async def blocked(*args, **kwargs):
            await forever.wait()
        with patch('backend.app.video.recovery.extract_original_speech', return_value=self.audio('source.mp3')):
            report = await prepare_dialogue_audio([cue], 'source.mp4', self.root, synthesize=AsyncMock(), translate=blocked, translation_recovery_timeout=.02)
        self.assertEqual(report['fallback_count'], 1)

    async def test_openai_batch_failure_does_not_skip_later_batches(self):
        cues = [dict(self.cue(i), text='Hello') for i in range(61)]
        for cue in cues:
            cue.pop('translation')
        seen = []
        async def batch(items, *args, **kwargs):
            seen.append(items[0]['id'])
            if items[0]['id'] == 0:
                raise RuntimeError('first batch failed')
            for cue in items:
                cue.update(english_intent='Hello', translation='សួស្តី', translation_flow=FLOW)
        with patch('backend.app.engines.dialogue_translator.resolve_engine', return_value=SimpleNamespace(name='openai')), patch('backend.app.engines.openai_engine.OpenAIEngine.generation_options', return_value={}), patch('backend.app.engines.dialogue_translator.translate_dialogue_batch_openai', new=AsyncMock(side_effect=batch)), patch('backend.app.engines.dialogue_translator.translate_dialogue_clusters_free', new=AsyncMock(side_effect=MissingTranslationsError(cues[:30]))):
            with self.assertRaises(MissingTranslationsError):
                await translate_dialogue_segments(cues, 'en', engine_name='openai')
        self.assertEqual(seen, [0, 30, 60])
        self.assertEqual(cues[-1]['translation'], 'សួស្តី')

    async def test_empty_tts_audio_is_retried_and_reported(self):
        cue = self.cue()
        async def save(path):
            Path(path).touch()
        communicate = SimpleNamespace(save=save)
        with patch('backend.app.video.tts.edge_tts.Communicate', return_value=communicate):
            await synthesize_segments([cue], self.root, enhance=False)
        self.assertIsNone(cue['audio_file'])
        self.assertIn('empty audio', cue['tts_error'])

    async def test_voice_downloads_continue_while_two_audio_enhancements_are_busy(self):
        third_download, release_enhancement = asyncio.Event(), asyncio.Event()
        downloaded = []

        async def save(path):
            Path(path).write_bytes(b'voice')
            downloaded.append(path)
            if len(downloaded) >= 3:
                third_download.set()

        async def enhance(function, path):
            await release_enhancement.wait()
            return path

        communicate = SimpleNamespace(save=save)
        with patch('backend.app.video.tts.edge_tts.Communicate', return_value=communicate), patch(
            'backend.app.video.resources.run_blocking', side_effect=enhance
        ) as enhanced:
            task = asyncio.create_task(synthesize_segments([self.cue(i) for i in range(4)], self.root))
            try:
                await asyncio.wait_for(third_download.wait(), 1)
                self.assertFalse(task.done())
            finally:
                release_enhancement.set()
                await task
        self.assertEqual(len(downloaded), 4)
        self.assertEqual(enhanced.await_count, 4)

    async def test_outage_circuit_bounds_requests_instead_of_retrying_whole_queue(self):
        cues = [self.cue(i) for i in range(100)]
        communicate = SimpleNamespace(save=AsyncMock(side_effect=RuntimeError('service offline')))
        with patch('backend.app.video.tts.edge_tts.Communicate', return_value=communicate):
            await synthesize_segments(cues, self.root, enhance=False)
        self.assertLessEqual(communicate.save.await_count, 12)
        self.assertTrue(all(cue.get('tts_error') for cue in cues))

    async def test_four_download_slots_are_shared_across_video_jobs(self):
        from backend.app.video.tts import config
        four_started, release = asyncio.Event(), asyncio.Event()
        active = peak = 0

        async def save(path):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 4:
                four_started.set()
            try:
                await release.wait()
                Path(path).write_bytes(b'voice')
            finally:
                active -= 1

        with patch.object(config, 'TTS_MAX_CONCURRENT_REQUESTS', 4), patch.object(config, 'TTS_REQUEST_INTERVAL', .2), patch(
            'backend.app.video.tts.edge_tts.Communicate', return_value=SimpleNamespace(save=save)
        ):
            tasks = [asyncio.create_task(synthesize_segments([self.cue(i) for i in range(4)], self.root / name, enhance=False))
                     for name in ('video_a', 'video_b')]
            try:
                await asyncio.wait_for(four_started.wait(), 1)
                self.assertEqual(active, 4)
            finally:
                release.set()
                await asyncio.gather(*tasks)
        self.assertEqual(peak, 4)

    async def test_real_original_audio_fallback_has_correct_duration(self):
        source = self.root/'source.wav'
        output = self.root/'clip.mp3'
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=3', str(source)], check=True)
        await asyncio.to_thread(extract_original_speech, source, {'start': 1, 'end': 2}, output)
        duration = float(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', str(output)]))
        self.assertAlmostEqual(duration, 1, delta=.1)

    async def test_persistent_failure_still_produces_a_real_video_with_audio_and_subtitles(self):
        from backend.app.video import dubber
        source = self.root / 'source.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'color=black:s=160x120:r=10:d=3', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=3', '-c:v', 'libx264', '-c:a', 'aac', '-shortest', str(source)], check=True)
        cue = self.cue(translated=False)
        report = await prepare_dialogue_audio([cue], source, self.root / 'voices', translate=AsyncMock(side_effect=MissingTranslationsError([cue])), synthesize=AsyncMock())
        with patch.object(dubber, 'DUBBED_DIR', self.root):
            result = await dubber.assemble_dubbed_video(str(source), [cue], 'finished.mp4')
        streams = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries', 'stream=codec_type', '-of', 'json', result['dubbed_video_path']]))['streams']
        self.assertEqual({stream['codec_type'] for stream in streams}, {'video', 'audio'})
        self.assertIn('Hello', Path(result['srt_path']).read_text())
        self.assertEqual(report['fallback_count'], 1)
