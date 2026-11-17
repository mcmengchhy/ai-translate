"""Royal gratitude preserves two addressees and never invents a monarch."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend.app.engines.dialogue_translator import translate_dialogue_clusters_free, store_english
from backend.app.khmer.dialogue_fixer import (
    source_english_phrase, english_phrase_translation, source_phrase_translation,
    polish_khmer_dialogue, batch_polish_segments,
)
from backend.app.core.prompts import build_khmer_style_instructions
from backend.app.api.video_routes import _apply_segment_updates


class RoyalDialogueTests(unittest.IsolatedAsyncioTestCase):
    def test_prince_consideration_uses_royal_address_and_careful_thought(self):
        expected = 'ព្រះអង្គគិតគូរម៉ត់ចត់បែបនេះ ក៏សមគួរដែរ។'
        for source in ('王爷深思也是应当', '王爷深思也是应当。', '王爺深思也是應當'):
            self.assertEqual(source_phrase_translation(source), expected)
            self.assertEqual(polish_khmer_dialogue('ឯកឧត្តមគិតយូរៗក៏ត្រូវហើយ។', source), expected)
        self.assertIn('consider matters carefully', source_english_phrase('王爷深思也是应当'))

    def test_reviewed_royal_line_never_overrides_a_question_negation_or_other_people(self):
        for source in ('王爷深思也是应当吗', '王爷深思也是应当？', '王爷深思也是应当!',
                       '不是王爷深思也是应当', '王爷深思也是应当但是不能等', '大人深思也是应当',
                       '王爷说大人应该深思', '深思也是应当'):
            self.assertEqual(source_phrase_translation(source), '')
            self.assertEqual(polish_khmer_dialogue('ឯកឧត្តម', source), 'ឯកឧត្តម')

    def test_royal_wording_correction_invalidates_the_old_voice(self):
        cue = {'id':4, 'text':'王爷深思也是应当', 'translation':'ឯកឧត្តមគិតយូរ។',
               'audio_file':'old.mp3', 'audio_source':'khmer_tts'}
        batch_polish_segments([cue])
        self.assertTrue(cue['_dirty'])
        self.assertIsNone(cue['audio_file'])
        self.assertNotIn('audio_source', cue)
        self.assertIn('ព្រះអង្គ', cue['translation'])

    def test_all_khmer_tones_keep_royal_and_civil_titles_distinct(self):
        for tone in ('standard', 'casual', 'polite', 'formal', 'literary'):
            prompt = build_khmer_style_instructions(tone, dialogue=True)
            self.assertIn('王爷/王爺', prompt)
            self.assertIn('ព្រះអង្គម្ចាស់', prompt)
            self.assertIn('Do not substitute the civil honorific', prompt)

    async def test_cached_wrong_english_and_khmer_are_corrected_without_another_provider_guess(self):
        cue = {"id": 1, "start": 0, "end": 2.21, "text": "谢王爷谢太子殿下", "asr_text": "殿下 殿下 殿下", "ocr_detected": True}
        store_english(cue, "Thank you, Your Majesty, Your Highness, the Crown Prince", "zh",
                      "Thank you, Your Majesty, Your Highness, the Crown Prince", "Your Highness Your Highness Your Highness")
        cue["translation"] = "ព្រះបរមនាថ នរោត្តម សីហមុនី"
        translate = AsyncMock(side_effect=AssertionError("Stock phrase must not be guessed again"))
        with patch("backend.app.engines.dialogue_translator.get_engine", return_value=SimpleNamespace(translate=translate)):
            await translate_dialogue_clusters_free([cue], "zh")
        self.assertEqual(cue["english_intent"], "Thank you, Prince; thank you, Crown Prince.")
        self.assertEqual(cue["translation"], "សូមអរព្រះគុណ ព្រះអង្គម្ចាស់។ សូមអរព្រះគុណ ព្រះអង្គម្ចាស់រាជទាយាទ។")
        self.assertNotIn("នរោត្តម", cue["translation"])
        translate.assert_not_awaited()

    async def test_titles_do_not_rewrite_questions_negation_or_unrelated_speech(self):
        for source in ("不是谢王爷谢太子殿下", "谢王爷谢太子殿下吗", "谢王爷谢太子殿下之后", "你谢王爷谢太子殿下"):
            self.assertEqual(source_english_phrase(source), "")
        for english in ("Do not thank the Prince", "Thank you, Prince; thank you, Crown Prince. Leave now."):
            self.assertEqual(english_phrase_translation(english), "")
        self.assertEqual(source_english_phrase("谢陛下，谢太子殿下！"), "Thank you, Your Majesty; thank you, Crown Prince.")

    async def test_saving_corrected_translation_invalidates_old_voice_and_export(self):
        old = {"id": 1, "start": 0, "end": 2, "translation": "old", "audio_file": "old.mp3"}
        job = {"segments": [old]}
        _apply_segment_updates(job, [{**old, "translation": "corrected"}])
        self.assertIsNone(job["segments"][0]["audio_file"])
        self.assertTrue(job["segments"][0]["_dirty"])
        self.assertTrue(job["needs_rerender"])
