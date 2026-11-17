"""Named addresses retain the person's spelling and reject unrelated Khmer words."""

import json
import unittest
from unittest.mock import patch

import httpx

from backend.app.engines.dialogue_translator import translate_dialogue_batch_openai, IncompleteTranslationStage, store_khmer
from backend.app.khmer.character_names import character_glossary, translation_glossary, missing_character_names, reviewed_reassurance


class CharacterNameTests(unittest.IsolatedAsyncioTestCase):
    def test_xingchen_faction_keeps_the_reviewed_organization_name(self):
        glossary=translation_glossary([{'subtitle_text':'传信星辰阁','asr_text':'传信星辰哥'}])
        self.assertEqual(glossary['星辰阁']['kind'],'organization')
        self.assertEqual(glossary['星辰閣']['khmer'],'សំណាក់ស៊ីងឆិន')
        self.assertEqual(missing_character_names('传信星辰阁','ផ្ញើសារទៅឯកាគន្លឹះ',glossary),['星辰阁'])
        self.assertEqual(missing_character_names('传信星辰阁','ផ្ញើសារទៅសំណាក់ស៊ីងឆិន។',glossary),[])
        self.assertEqual(translation_glossary([{'text':'楼阁很高'}]),{})
        self.assertEqual(translation_glossary([{'text':'传信星辰哥'}]),{})

    def test_generated_replacement_does_not_keep_a_stale_source_review_label(self):
        cue={'id':10,'text':'传信星辰阁','translation':'reviewed wording',
             'translation_review':'direct_chinese_source_review','translation_review_note':'old review','audio_file':'old.mp3'}
        store_khmer(cue,'new generated wording','zh','casual')
        self.assertNotIn('translation_review',cue)
        self.assertNotIn('translation_review_note',cue)
        self.assertIsNone(cue['audio_file'])

    async def test_xingchen_glossary_uses_the_written_name_and_rejects_a_dropped_name(self):
        captured=[]
        cue={'id':10,'text':'传信星辰阁','subtitle_text':'传信星辰阁','asr_text':'传信星辰哥','start':20.8,'end':21.69}
        def handler(request):
            body=json.loads(json.loads(request.content)['messages'][1]['content'])
            captured.append(body)
            value='ផ្ញើសារទៅឯកាគន្លឹះ' if len(captured)==1 else 'ផ្ញើសារទៅសំណាក់ស៊ីងឆិន។'
            return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps([{'id':10,'translation':value}])}}]})
        original=httpx.AsyncClient
        with patch.object(httpx,'AsyncClient',side_effect=lambda **kwargs:original(transport=httpx.MockTransport(handler),**kwargs)):
            await translate_dialogue_batch_openai([cue],'zh')
        self.assertEqual(len(captured),2)
        self.assertEqual(captured[0]['name_glossary']['星辰阁']['kind'],'organization')
        self.assertEqual(cue['translation'],'ផ្ញើសារទៅសំណាក់ស៊ីងឆិន។')

    def test_state_glossary_preserves_da_zhao_without_inventing_a_modern_country(self):
        glossary = translation_glossary([{'text':'目标是成为大赵第一杀手势力'}])
        self.assertEqual(glossary['大赵']['kind'], 'state')
        self.assertEqual(glossary['大赵']['khmer'], 'តាចាវ')
        self.assertEqual(glossary['大趙']['khmer'], 'តាចាវ')
        self.assertEqual(missing_character_names('大赵第一杀手势力','ក្រុមឃាតករលេខមួយនៅប្រទេសដាច់',glossary), ['大赵'])
        self.assertEqual(missing_character_names('大赵第一杀手势力','ក្រុមឃាតករលេខមួយនៅនគរតាចាវ',glossary), [])

    def test_a_surname_or_nickname_is_not_automatically_a_country(self):
        self.assertEqual(translation_glossary([{'text':'大赵先生来了'}]), {})
        self.assertEqual(translation_glossary([{'text':'赵先生来了'}]), {})
        glossary = translation_glossary([{'text':'大赵国内'}, {'text':'大赵先生来了'}])
        self.assertEqual(missing_character_names('大赵先生来了','លោកចាវមកហើយ',glossary), [])
        self.assertEqual(missing_character_names('这个国家很大','ប្រទេសនេះធំ',glossary), [])

    async def test_missing_state_name_is_retried_without_rewriting_another_country(self):
        captured = []
        cue = {'id':17,'text':'目标是成为大赵第一杀手势力','start':32.64,'end':35.01}
        def handler(request):
            body = json.loads(json.loads(request.content)['messages'][1]['content'])
            captured.append(body)
            value = 'ក្រុមឃាតករលេខមួយនៅប្រទេសដាច់' if len(captured)==1 else 'គោលដៅគឺក្លាយជាក្រុមឃាតករលេខមួយនៅនគរតាចាវ។'
            return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps([{'id':17,'translation':value}])}}]})
        original = httpx.AsyncClient
        with patch.object(httpx,'AsyncClient',side_effect=lambda **kwargs:original(transport=httpx.MockTransport(handler),**kwargs)):
            await translate_dialogue_batch_openai([cue],'zh')
        self.assertEqual(len(captured),2)
        self.assertEqual(captured[0]['name_glossary']['大赵']['kind'],'state')
        self.assertIn('តាចាវ',cue['translation'])
    def test_sister_address_establishes_a_name_but_literal_cicadas_do_not(self):
        names = character_glossary([{'subtitle_text': '嗯秋蝉姐没事', 'asr_text': '秋专杰 没事'}])
        self.assertEqual(names['秋蝉']['khmer'], 'ឈីវឆាន')
        self.assertEqual(names['秋蝉']['sister_address'], 'បងស្រីឈីវឆាន')
        self.assertEqual(character_glossary([{'text': '秋蝉在树上叫'}]), {})
        self.assertEqual(character_glossary([{'text': '别担心姐姐'}]), {})
        self.assertEqual(character_glossary([{'subtitle_text': '普通话', 'asr_text': '秋蝉姐'}]), {})

    def test_validation_only_checks_names_present_in_the_source(self):
        glossary = character_glossary([{'text': '秋蝉姐'}])
        self.assertEqual(missing_character_names('秋蝉姐没事', 'ឱក្សាន្តស្រី គ្មានអ្វីខុសទេ។', glossary), ['秋蝉'])
        self.assertEqual(missing_character_names('秋蝉姐没事', 'បងស្រីឈីវឆាន ខ្ញុំមិនអីទេ។', glossary), [])
        self.assertEqual(missing_character_names('秋蝉姐没事', 'អូនឈីវឆាន មិនអីទេ។', glossary), ['秋蝉'])
        self.assertEqual(missing_character_names('秋蝉姐没事', 'ឈីវឆានបងស្រី មិនអីទេ។', glossary), ['秋蝉'])
        self.assertEqual(missing_character_names('没事', 'ខ្ញុំមិនអីទេ។', glossary), [])

    def test_reassurance_uses_first_person_only_with_the_following_speakers_explanation(self):
        glossary = character_glossary([{'text':'嗯秋蝉姐没事'}])
        expected = 'បងស្រីឈីវឆាន ខ្ញុំមិនអីទេ។'
        self.assertEqual(reviewed_reassurance('嗯秋蝉姐没事','本王只是在盘算后续各项安排',glossary), expected)
        for source, following in (('秋蝉姐没事','本王只是想知道'), ('嗯秋蝉姐没事？','本王只是想知道'),
                                  ('嗯秋蝉姐没事','她只是想知道'), ('嗯秋蝉姐没事','')):
            self.assertEqual(reviewed_reassurance(source, following, glossary), '')

    async def test_reviewed_scene_reassurance_prevents_wrong_you_are_fine_output(self):
        cue = {'id':1,'text':'嗯秋蝉姐没事','subtitle_text':'嗯秋蝉姐没事','start':0,'end':1.64}
        def handler(request):
            body = json.loads(json.loads(request.content)['messages'][1]['content'])
            self.assertEqual(body['target_cues'][0]['reviewed_translation'], 'បងស្រីឈីវឆាន ខ្ញុំមិនអីទេ។')
            return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps([{'id':1,'translation':'បងស្រីឈីវឆាន អ្នកមិនអីទេ។'}])}}]})
        original = httpx.AsyncClient
        with patch.object(httpx,'AsyncClient',side_effect=lambda **kwargs:original(transport=httpx.MockTransport(handler),**kwargs)):
            await translate_dialogue_batch_openai([cue],'zh',context_after=[{'id':2,'text':'本王只是在盘算后续各项安排'}])
        self.assertEqual(cue['translation'], 'បងស្រីឈីវឆាន ខ្ញុំមិនអីទេ។')

    async def test_altered_name_is_retried_with_the_same_source_glossary(self):
        captured = []
        cue = {'id': 1, 'text': '嗯秋蝉姐没事', 'subtitle_text': '嗯秋蝉姐没事',
               'asr_text': '秋专杰 没事', 'start': 0, 'end': 1.64}
        def handler(request):
            body = json.loads(request.content)
            captured.append(body)
            value = 'ឱក្សាន្តស្រី គ្មានអ្វីខុសទេ។' if len(captured) == 1 else 'បងស្រីឈីវឆាន ខ្ញុំមិនអីទេ។'
            return httpx.Response(200, json={'choices':[{'message':{'content':json.dumps([{'id':1,'translation':value}])}}]})
        original = httpx.AsyncClient
        with patch.object(httpx, 'AsyncClient', side_effect=lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs)):
            await translate_dialogue_batch_openai([cue], 'zh', context_after=[{'text': '本王只是在盘算后续各项安排'}])
        self.assertEqual(len(captured), 2)
        body = json.loads(captured[0]['messages'][1]['content'])
        self.assertEqual(body['name_glossary']['秋蝉']['khmer'], 'ឈីវឆាន')
        self.assertEqual(body['target_cues'][0]['speech_text'], '秋专杰 没事')
        self.assertEqual(cue['translation'], 'បងស្រីឈីវឆាន ខ្ញុំមិនអីទេ។')

    async def test_failed_name_retry_remains_flagged_for_review(self):
        cue = {'id': 1, 'text': '秋蝉姐自明日起', 'start': 0, 'end': 2}
        def handler(request):
            return httpx.Response(200, json={'choices':[{'message':{'content':json.dumps([{'id':1,'translation':'ចាប់ពីថ្ងៃស្អែក ឱក្សាន្តស្រី'}])}}]})
        original = httpx.AsyncClient
        with patch.object(httpx, 'AsyncClient', side_effect=lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs)):
            with self.assertRaises(IncompleteTranslationStage):
                await translate_dialogue_batch_openai([cue], 'zh')
        self.assertIn('source name or place', cue['translation_error'])
        self.assertNotIn('translation', cue)
