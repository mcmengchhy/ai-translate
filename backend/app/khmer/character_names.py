"""Reviewed spellings for people, organizations and states from source evidence."""

import re

# These spellings are used only when the source establishes a person by address.
# 秋蝉 on its own can mean an autumn cicada and must retain that literal sense.
REVIEWED_NAMES = ((('秋蝉', '秋蟬'), 'Qiuchan', 'ឈីវឆាន'),)
REVIEWED_STATES = ((('大赵', '大趙'), 'Da Zhao (Great Zhao)', 'តាចាវ'),)
REVIEWED_ORGANIZATIONS = ((('星辰阁', '星辰閣'), 'Xingchen', 'សំណាក់ស៊ីងឆិន'),)
PERSON_SUFFIXES = r'(?:先生|女士|公子|大人|同学|同學|老师|老師|哥|姐)'


def character_glossary(segments):
    text = '\n'.join(str(seg.get('subtitle_text') or seg.get('text') or seg.get('asr_text') or '')
                     for seg in segments)
    glossary = {}
    for variants, latin, khmer in REVIEWED_NAMES:
        if any(re.search(re.escape(name) + r'(?:姐姐|姐|姑娘|小姐)', text) for name in variants):
            for name in variants:
                glossary[name] = {'kind': 'person', 'latin': latin, 'khmer': khmer,
                                  'sister_address': 'បងស្រី' + khmer}
    return glossary


def translation_glossary(segments):
    glossary = character_glossary(segments)
    text = '\n'.join(str(seg.get('subtitle_text') or seg.get('text') or seg.get('asr_text') or '')
                     for seg in segments)
    for variants, latin, khmer in REVIEWED_STATES:
        # A surname or a person's nickname must not establish a country.
        state_context = r'(?:国|國|王朝|皇朝|境内|境內|疆域|朝廷|皇帝|第一[^\s。！？!?\n]{0,12}(?:势力|勢力|杀手|殺手|刺客|宗门|宗門))'
        if any(re.search(re.escape(name) + state_context, text) for name in variants):
            for name in variants:
                glossary[name] = {'kind': 'state', 'latin': latin, 'khmer': khmer,
                                  'note': 'Story state name. Do not infer a modern country or a specific historical dynasty.'}
    for variants, latin, khmer in REVIEWED_ORGANIZATIONS:
        command = r'(?:传信|傳信|传令|傳令|通知|联系|聯繫)(?:给|給|到|去|至)?\s*'
        if any(re.search(command + re.escape(name), text) for name in variants):
            for name in variants:
                glossary[name] = {'kind': 'organization', 'latin': latin, 'khmer': khmer,
                                  'note': 'Reviewed faction name in this story. Use សំណាក់ for the named organization; written 阁/閣 is not the spoken homophone 哥.'}
    return glossary


def missing_character_names(source, translation, glossary):
    compact = re.sub(r'\s+', '', translation)
    missing = []
    for name, entry in (glossary or {}).items():
        if name not in source:
            continue
        if entry['kind'] == 'state' and re.search(re.escape(name) + PERSON_SUFFIXES, source):
            continue
        expected = entry['sister_address'] if entry['kind'] == 'person' and re.search(re.escape(name) + r'姐姐?', source) else entry['khmer']
        if expected not in compact:
            missing.append(name)
    return missing


def reviewed_reassurance(source, next_source, glossary):
    """A named address plus a first-person continuation identifies the speaker.

    Without that continuation, 'Qiuchan is fine' remains a possible reading.
    Questions, longer sentences, and third-person statements are not rewritten.
    """
    compact = re.sub(r'[\s，,。.]', '', source)
    following = re.sub(r'\s+', '', next_source)
    if not following.startswith(('本王只是', '我只是', '我只是在')):
        return ''
    for name, entry in (glossary or {}).items():
        if entry['kind'] != 'person':
            continue
        if re.fullmatch('嗯' + re.escape(name) + r'姐姐?没事', compact):
            return entry['sister_address'] + ' ខ្ញុំមិនអីទេ។'
    return ''
