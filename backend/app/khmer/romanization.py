"""Recognize source-derived romanization without accepting untranslated English prose."""

import re
import unicodedata
from functools import lru_cache


def _latin_key(value):
    value = unicodedata.normalize("NFKD", value.lower().replace("v", "u"))
    return re.sub(r"[^a-z]", "", value)


@lru_cache(maxsize=4096)
def is_source_romanization(source, translated):
    """A short romanized name must match the actual Chinese source pronunciation."""
    characters = re.findall(r"[\u4e00-\u9fff]", source)
    if not characters or len(characters) > 12 or re.search(r"[\u1780-\u17ff]", translated):
        return False
    if not re.fullmatch(r"[A-Za-z\u00c0-\u024f\s'’.,\-]+", translated.strip()):
        return False
    from pypinyin import lazy_pinyin

    expected = _latin_key(" ".join(lazy_pinyin(source)))
    return bool(expected) and expected == _latin_key(translated)
