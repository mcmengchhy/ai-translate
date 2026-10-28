"""Khmer Linguistic and Text Processing Utilities.

Handles:
- Unicode normalization (subscript coeng reordering, diacritic deduplication)
- Khmer numeral conversion (០-៩ <-> 0-9)
- Syllable / boundary segmentation & Zero-Width Space (ZWSP) injection
- Register / Tone prompts (Formal, Polite, Casual, Monarchical)
"""

import re
import unicodedata
from typing import List, Tuple

# Khmer Unicode Ranges
KHMER_START = 0x1780
KHMER_END = 0x17FF

# Character classes
KHMER_CONSONANTS = r"[\u1780-\u17A2]"
KHMER_INDEPENDENT_VOWELS = r"[\u17A3-\u17B3]"
KHMER_DEPENDENT_VOWELS = r"[\u17B6-\u17C5]"
KHMER_COENG = "\u17D2"  # ្
KHMER_DIACRITICS = r"[\u17C6-\u17D1\u17D3]"
KHMER_PUNCTUATION = r"[\u17D4-\u17DA]"
KHMER_NUMERALS = r"[\u17E0-\u17E9]"
ZWSP = "\u200B"

# Khmer to Arabic numerals mapping
KHMER_TO_ARABIC_DIGITS = {
    "០": "0", "១": "1", "២": "2", "៣": "3", "៤": "4",
    "៥": "5", "៦": "6", "៧": "7", "៨": "8", "៩": "9"
}
ARABIC_TO_KHMER_DIGITS = {v: k for k, v in KHMER_TO_ARABIC_DIGITS.items()}


def is_khmer_text(text: str) -> bool:
    """Check if the text contains Khmer characters."""
    return any(KHMER_START <= ord(c) <= KHMER_END for c in text)


def normalize_khmer_unicode(text: str) -> str:
    """
    Normalizes Khmer text to standard canonical order.
    
    Standard Khmer character cluster order:
    Base Consonant -> Coeng + Subscript Consonant(s) -> Dependent Vowel -> Diacritic(s)
    
    Fixes common input issues:
    - Vowel placed before Coeng (e.g., Base + Vowel + Coeng -> Base + Coeng + Vowel)
    - Consecutive redundant diacritics
    - Canonical Unicode decomposition/composition (NFC)
    """
    if not text:
        return ""

    # Unicode standard NFC normalization first
    normalized = unicodedata.normalize("NFC", text)

    # Fix: Dependent Vowel followed by Coeng + Consonant -> swap them
    # e.g., ([\u17B6-\u17C5])(\u17D2[\u1780-\u17A2]) -> \2\1
    pattern_vowel_coeng = re.compile(f"({KHMER_DEPENDENT_VOWELS}+)({KHMER_COENG}{KHMER_CONSONANTS})")
    while pattern_vowel_coeng.search(normalized):
        normalized = pattern_vowel_coeng.sub(r"\2\1", normalized)

    # Fix: Duplicate identical diacritics
    normalized = re.sub(r"([\u17C6-\u17D1\u17D3])\1+", r"\1", normalized)

    # Clean up redundant spaces around Khmer punctuation
    normalized = re.sub(r"\s+([\u17D4\u17D5\u17D6\u17D7])", r"\1", normalized)

    return normalized


def convert_numerals(text: str, to_khmer: bool = True) -> str:
    """
    Convert numerals between Khmer (០-៩) and Western Arabic (0-9).
    """
    if not text:
        return ""
    mapping = ARABIC_TO_KHMER_DIGITS if to_khmer else KHMER_TO_ARABIC_DIGITS
    table = str.maketrans(mapping)
    return text.translate(table)


# Syllable segmentation regex based on Khmer orthographic rules
# Cluster: Base Consonant (+ Coeng Consonant)* (+ Dependent Vowel)? (+ Diacritic)*
_CLUSTER_PATTERN = re.compile(
    rf"({KHMER_CONSONANTS}|{KHMER_INDEPENDENT_VOWELS})"
    rf"({KHMER_COENG}{KHMER_CONSONANTS})*"
    rf"({KHMER_DEPENDENT_VOWELS})*"
    rf"({KHMER_DIACRITICS})*",
    re.UNICODE
)


def segment_khmer_syllables(text: str) -> List[str]:
    """
    Segments Khmer text into syllables / grapheme clusters.
    """
    if not text:
        return []
    
    text = normalize_khmer_unicode(text)
    clusters = []
    last_idx = 0

    for match in _CLUSTER_PATTERN.finditer(text):
        start, end = match.span()
        # capture non-Khmer text or punctuation in-between
        if start > last_idx:
            non_cluster = text[last_idx:start]
            if non_cluster.strip():
                clusters.append(non_cluster)
        clusters.append(match.group(0))
        last_idx = end

    if last_idx < len(text):
        remainder = text[last_idx:]
        if remainder:
            clusters.append(remainder)

    return clusters


def inject_zwsp(text: str) -> str:
    """
    Inserts Zero-Width Spaces (ZWSP) at syllable/word boundaries for
    proper browser text-wrapping and responsive layout display.
    """
    clusters = segment_khmer_syllables(text)
    output = []
    for i, cluster in enumerate(clusters):
        output.append(cluster)
        # Don't add ZWSP if current cluster or next cluster is space/punctuation
        if i < len(clusters) - 1:
            next_cluster = clusters[i + 1]
            if not cluster.isspace() and not next_cluster.isspace() and not re.match(KHMER_PUNCTUATION, next_cluster):
                output.append(ZWSP)
    return "".join(output)


def strip_zwsp(text: str) -> str:
    """Removes all Zero-Width Spaces from text."""
    return text.replace(ZWSP, "")


from .dialogue_fixer import polish_khmer_dialogue, batch_polish_segments
