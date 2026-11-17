"""Native phrasing must retain meaning and respect the requested register."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from backend.app.core.prompts import build_system_prompt
from backend.app.engines.dialogue_translator import translate_dialogue_clusters_free
from backend.app.engines.free_engine import FreeEngine
from backend.app.khmer.native_phrases import native_phrase_translation


class NativeKhmerTests(unittest.IsolatedAsyncioTestCase):
    def test_complete_phrases_preserve_negation_and_question_intent(self):
        self.assertEqual(native_phrase_translation('我不是这个意思。'), 'ខ្ញុំមិនមែនចង់និយាយអ៊ីចឹងទេ។')
        self.assertEqual(native_phrase_translation("I didn't mean that."), 'ខ្ញុំមិនមែនចង់និយាយអ៊ីចឹងទេ។')
        self.assertEqual(native_phrase_translation('怎么可能？'), 'ម៉េចអាចទៅរួច?')
        self.assertEqual(native_phrase_translation('你没事吧'), 'អ្នកមិនអីទេឬ?')
        self.assertEqual(native_phrase_translation('我不明白'), 'ខ្ញុំមិនយល់ទេ។')

    def test_glossary_never_rewrites_a_fragment_or_removes_emotional_punctuation(self):
        for source in ('我不是这个意思吗', '别担心吗', "Don't worry?", "Don't worry!", "Don't worry...",
                       "Don't worry. Leave now.", '别担心，但不要出去', "Don't worry\nLeave now"):
            self.assertEqual(native_phrase_translation(source), '', source)

    def test_register_changes_only_when_requested(self):
        self.assertEqual(native_phrase_translation('别担心', 'casual'), 'កុំបារម្ភអី។')
        self.assertEqual(native_phrase_translation('别担心', 'standard'), 'កុំបារម្ភ។')
        for tone in ('polite', 'formal', 'literary'):
            self.assertEqual(native_phrase_translation('别担心', tone), '')
        self.assertNotIn('កុំបារម្ភអី', build_system_prompt('en', 'km', tone='formal'))

    async def test_free_engine_uses_native_standalone_phrases_without_network_requests(self):
        with patch.object(httpx, 'AsyncClient', side_effect=AssertionError('No provider request needed')):
            result = await FreeEngine().translate("Don't worry.", 'en', 'km', tone='casual')
        self.assertEqual(result, 'កុំបារម្ភអី។')

    async def test_native_cue_retranslation_invalidates_a_stale_voice(self):
        cue = {'id': 1, 'text': '我不是这个意思', 'translation': 'old', 'audio_file': 'old.mp3'}
        translate = AsyncMock(side_effect=AssertionError('A complete native phrase needs no provider'))
        with patch('backend.app.engines.dialogue_translator.get_engine', return_value=SimpleNamespace(translate=translate)):
            await translate_dialogue_clusters_free([cue], 'zh', include_english=False)
        self.assertEqual(cue['translation'], 'ខ្ញុំមិនមែនចង់និយាយអ៊ីចឹងទេ។')
        self.assertIsNone(cue['audio_file'])
        self.assertTrue(cue['_dirty'])
        translate.assert_not_awaited()
