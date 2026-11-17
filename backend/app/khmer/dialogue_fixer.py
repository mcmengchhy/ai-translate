"""Conservative Khmer dialogue cleanup that preserves meaning and relationships.

Contextual rewriting belongs to the translator. Text cleanup must never turn a
question into a statement or guess whether a speaker is family, royalty, or a peer.
"""

import re
from difflib import SequenceMatcher
from typing import List, Dict, Any

# Exact source matches only: do not apply these to negated or longer sentences.
SOURCE_PHRASE_GLOSSARY = {
    "双关语": "ពាក្យមានន័យពីរ",
    "一语双关": "ពាក្យមួយបង្កប់ន័យពីរ",
    "语双关": "ពាក្យមានន័យពីរ",
}
SOURCE_ENGLISH_GLOSSARY = {
    "双关语": "A phrase with a double meaning.",
    "一语双关": "One phrase with a double meaning.",
    "语双关": "A phrase with a double meaning.",
}

ROYAL_ADDRESSES = {
    "王爷": ("Prince", "ព្រះអង្គម្ចាស់"),
    "太子殿下": ("Crown Prince", "ព្រះអង្គម្ចាស់រាជទាយាទ"),
    "殿下": ("Your Highness", "ព្រះអង្គម្ចាស់"),
    "陛下": ("Your Majesty", "ព្រះករុណា"),
}

# Reviewed complete sentences only; do not replace civil honorifics globally.
ROYAL_SOURCE_PHRASES = {
    "王爷深思也是应当": "ព្រះអង្គគិតគូរម៉ត់ចត់បែបនេះ ក៏សមគួរដែរ។",
    "王爺深思也是應當": "ព្រះអង្គគិតគូរម៉ត់ចត់បែបនេះ ក៏សមគួរដែរ។",
}


def royal_source_phrase(source_text):
    # A question, negation, or longer line must retain its own meaning.
    key = (source_text or "").strip().rstrip("。.")
    return ROYAL_SOURCE_PHRASES.get(key, "")


def source_english_phrase(source_text):
    """Constrain only complete, unambiguous stock phrases; never longer dialogue."""
    source = re.sub(r"[\s，,。.!！]", "", source_text or "")
    if royal_source_phrase(source_text):
        return "It is appropriate for Your Highness to consider matters carefully."
    titles = "|".join(ROYAL_ADDRESSES)
    match = re.fullmatch(rf"谢({titles})谢({titles})", source)
    if match:
        first, second = (ROYAL_ADDRESSES[title][0] for title in match.groups())
        return f"Thank you, {first}; thank you, {second}."
    return SOURCE_ENGLISH_GLOSSARY.get(source, "")


def reconcile_english_evidence(subtitle_english, speech_english, subtitle_text, speech_text):
    """Use written evidence to resolve an ASR homophone without losing spoken intent."""
    source = (subtitle_text or speech_text or "").strip().rstrip("。！？!?")
    phrase = source_english_phrase(source)
    if phrase:
        return phrase
    subtitle_text = (subtitle_text or "").strip()
    speech_text = (speech_text or "").strip()
    written = re.sub(r"[\W_]", "", subtitle_text)
    spoken = re.sub(r"[\W_]", "", speech_text)
    # OCR sometimes captures only a few characters of a longer spoken line.
    # Prefer the complete speech when most of that fragment is also heard.
    # Keep subtitles as primary evidence when lengths are comparable.
    if len(spoken) >= 8 and 2 <= len(written) <= len(spoken) * 0.6:
        shared = sum(block.size for block in SequenceMatcher(None, written, spoken, autojunk=False).get_matching_blocks())
        if shared >= len(written) * 0.75 and speech_english and not re.search(r"[\u4e00-\u9fff\u1780-\u17ff]", speech_english):
            return speech_english.strip()
    # 阁 (pavilion) and 哥 (brother) sound alike. The audio supplies the
    # sentence's action; the printed subtitle identifies the destination.
    if subtitle_text.endswith("阁") and speech_text.endswith("哥") and subtitle_text[:-1] == speech_text[:-1]:
        if re.search(r"\bpavilion\b", subtitle_english or "", flags=re.IGNORECASE):
            corrected = re.sub(r"\bBrother\s+([^.!?]+)", r"\1 Pavilion", speech_english or "", flags=re.IGNORECASE)
            if corrected and corrected != speech_english:
                return corrected.strip()
    primary = (subtitle_english or "").strip()
    alternative = (speech_english or "").strip()
    if primary and not re.search(r"[\u4e00-\u9fff\u1780-\u17ff]", primary):
        return primary
    return alternative if alternative and not re.search(r"[\u4e00-\u9fff\u1780-\u17ff]", alternative) else ""


def english_phrase_translation(english_text):
    # Royal forms of address must not expand into a real monarch's name.
    addresses = {english.lower(): khmer for english, khmer in ROYAL_ADDRESSES.values()}
    titles = "|".join(re.escape(title) for title in addresses)
    match = re.fullmatch(rf"thank you, ({titles}); thank you, ({titles})\.?", english_text.strip(), re.IGNORECASE)
    if match:
        first, second = (addresses[title.lower()] for title in match.groups())
        return f"សូមអរព្រះគុណ {first}។ សូមអរព្រះគុណ {second}។"
    key = english_text.strip().rstrip(".!?").lower()
    return {
        "a phrase with a double meaning": "ពាក្យមានន័យពីរ",
        "one phrase with a double meaning": "ពាក្យមួយបង្កប់ន័យពីរ",
    }.get(key, "")


def source_phrase_translation(source_text: str) -> str:
    royal = royal_source_phrase(source_text)
    if royal:
        return royal
    phrase = SOURCE_PHRASE_GLOSSARY.get(source_text.strip().rstrip("。！？!?"), "")
    return phrase or english_phrase_translation(source_english_phrase(source_text))

def polish_khmer_dialogue(text: str, source_text: str = "") -> str:
    """Polishes raw machine-translated text into natural conversational Khmer dialogue."""
    if not text:
        return ""
    reviewed = royal_source_phrase(source_text)
    if reviewed:
        return reviewed
    
    cleaned = text.strip()
    # Strip any accidental Thai characters (range \u0E00-\u0E7F)
    cleaned = re.sub(r"[\u0e00-\u0e7f]+", "", cleaned)
    # Strip parenthetical CJK annotations like (还有)
    cleaned = re.sub(r"\([^\)]*[\u4e00-\u9fff]+[^\)]*\)", "", cleaned)
    # Strip any leftover Chinese characters (range \u4e00-\u9fff)
    cleaned = re.sub(r"[\u4e00-\u9fff]+", "", cleaned)
    # These literary senses are established by the source, not guessed from Khmer.
    if "圣贤" in source_text:
        cleaned = re.sub(r"\bsages?\b", "អ្នកប្រាជ្ញ", cleaned, flags=re.IGNORECASE)
        cleaned = cleaned.replace("ពួកបរិសុទ្ធ", "អ្នកប្រាជ្ញ")
    if all(term in source_text for term in ("书籍", "圣道", "载体")):
        cleaned = cleaned.replace("អ្នកដឹកជញ្ជូននៃ", "មធ្យោបាយផ្សព្វផ្សាយ")
        cleaned = cleaned.replace("អ្នកដឹកជញ្ជូន", "មធ្យោបាយផ្សព្វផ្សាយ")
        cleaned = cleaned.replace("ផ្លូវបរិសុទ្ធ", "គោលធម៌")
    
    # Clean up any leftover double spaces or empty parentheses
    cleaned = re.sub(r"\(\s*\)", "", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned.strip()


def batch_polish_segments(segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Applies dialogue logic normalization across a full list of segments."""
    for seg in segments:
        orig = seg.get("translation", "")
        if orig:
            seg["translation"] = polish_khmer_dialogue(orig, seg.get("text") or "")
            if seg["translation"] != orig:
                seg.update(audio_file=None, _dirty=True)
                for key in ("audio_source", "fallback_subtitle", "generation_warning", "review_required"):
                    seg.pop(key, None)
    return segments
