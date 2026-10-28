"""Khmer Film & Video Dialogue Logic Normalizer.

Detects and corrects common machine-translation blunders from Chinese/English subtitles:
- Literal translation of rhetorical complaints (e.g. "Mom, how old am I?" -> "Mom, I'm an adult now!")
- Mistranslation of '同志' as 'homosexual' in historical/family contexts -> 'comrade/bro/people'
- Awkward pronouns ('អ្នក' everywhere) -> natural conversational Khmer ('បង', 'អូន', 'ខ្ញុំ', 'ម៉ាក់')
- Slang and idiom restoration
"""

import re
from typing import List, Dict, Any

# Common literal translation fixes for movie dialogue
COMMON_DIALOGUE_REPLACEMENTS = [
    # Rhetorical age complaints: "老妈我都多大了" -> "ម៉ាក់ ខ្ញុំធំប៉ុណ្ណឹងហើយ"
    (r"ម៉ាក់\s*តើខ្ញុំអាយុប៉ុន្មាន\??", "ម៉ាក់! ខ្ញុំធំប៉ុណ្ណឹងហើយ!"),
    (r"តើខ្ញុំអាយុប៉ុន្មាន\??", "ខ្ញុំធំប៉ុណ្ណឹងហើយ!"),
    
    # "同志" mistranslated as "homosexual"
    (r"មនុស្សស្រលាញ់ភេទដូចគ្នា", "សមមិត្តបងប្អូន"),
    
    # "是给我干哪儿来了" mistranslated as "Where did it come from for me"
    (r"តើវាមកពីណាសម្រាប់ខ្ញុំ\??", "គាត់នាំខ្ញុំមកដល់កន្លែងណាហើយនេះ?!"),
    
    # "不超位" / "别着急"
    (r"មិនលើសដែនកំណត់", "កុំប្រញាប់ពេក"),
    
    # Robotic pronouns "អ្នក" at beginning of casual speech
    (r"^អ្នក\s+", "បង "),
    (r"បើមិនដូច្នេះទេ\s*អ្នកប្រាកដជា", "បើមិនចឹងទេ បងប្រាកដជា"),
]


def polish_khmer_dialogue(text: str) -> str:
    """Polishes raw machine-translated text into natural conversational Khmer dialogue."""
    if not text:
        return ""
    
    cleaned = text.strip()
    # Strip any accidental Thai characters (range \u0E00-\u0E7F)
    cleaned = re.sub(r"[\u0e00-\u0e7f]+", "", cleaned)
    # Strip parenthetical CJK annotations like (还有)
    cleaned = re.sub(r"\([^\)]*[\u4e00-\u9fff]+[^\)]*\)", "", cleaned)
    # Strip any leftover Chinese characters (range \u4e00-\u9fff)
    cleaned = re.sub(r"[\u4e00-\u9fff]+", "", cleaned)
    for pattern, replacement in COMMON_DIALOGUE_REPLACEMENTS:
        cleaned = re.sub(pattern, replacement, cleaned, flags=re.IGNORECASE)
    
    # Clean up any leftover double spaces or empty parentheses
    cleaned = re.sub(r"\(\s*\)", "", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned.strip()


def batch_polish_segments(segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Applies dialogue logic normalization across a full list of segments."""
    for seg in segments:
        orig = seg.get("translation", "")
        if orig:
            seg["translation"] = polish_khmer_dialogue(orig)
    return segments
