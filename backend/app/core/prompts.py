"""Khmer Translation Prompt Engineering & System Instructions.

Contains tailored instructions for LLM engines to produce accurate,
fluent, and culturally natural Khmer translations with correct orthography,
registers, and formatting preservation.
"""

from typing import Optional

TONE_INSTRUCTIONS = {
    "standard": (
        "Tone: Standard Contemporary Khmer (ទូទៅ).\n"
        "- Use natural, standard modern Khmer vocabulary.\n"
        "- Accurate grammar and smooth sentence flow.\n"
        "- Suitable for general articles, web content, and daily reading."
    ),
    "formal": (
        "Tone: Formal / Official (ផ្លូវការ).\n"
        "- Use professional, bureaucratic, or academic Khmer terms.\n"
        "- Suitable for legal, business contracts, technical documentation, and government notices.\n"
        "- Avoid colloquialisms or slang."
    ),
    "polite": (
        "Tone: Polite & Respectful (គួរសម).\n"
        "- Use polite prefixes and respectful particles (e.g., សូម, ជម្រាបសួរ, ផ្ដល់កិត្តិយស).\n"
        "- Honorific verbs and respectful pronouns suitable for customer support, hospitality, or elder communication."
    ),
    "casual": (
        "Tone: Casual & Conversational (សាមញ្ញ / សន្ទនា).\n"
        "- Use spoken everyday Khmer as people naturally speak in Phnom Penh/Cambodia.\n"
        "- Keep it friendly, authentic, and modern.\n"
        "- Ideal for chat messages, social media, subtitles, and friendly dialogue."
    ),
    "literary": (
        "Tone: Literary & Poetic (អក្សរសិល្ប៍).\n"
        "- Rich aesthetic vocabulary, traditional Khmer idioms, and graceful phrasing.\n"
        "- Ideal for stories, novels, poetry, and creative narratives."
    )
}

LANGUAGE_NAMES = {
    "km": "Khmer (ភាសាខ្មែរ)",
    "en": "English",
    "zh": "Chinese (Simplified - 中文)",
    "fr": "French (Français)",
    "th": "Thai (ไทย)",
    "vi": "Vietnamese (Tiếng Việt)",
    "ja": "Japanese (日本語)",
    "ko": "Korean (한국어)",
    "de": "German (Deutsch)",
    "es": "Spanish (Español)"
}


def build_system_prompt(source_lang: str, target_lang: str, tone: str = "standard", preserve_formatting: bool = True) -> str:
    """
    Constructs an expert translation system prompt for LLMs.
    """
    src_name = LANGUAGE_NAMES.get(source_lang, source_lang)
    tgt_name = LANGUAGE_NAMES.get(target_lang, target_lang)
    tone_instruction = TONE_INSTRUCTIONS.get(tone, TONE_INSTRUCTIONS["standard"])

    prompt = f"""You are a world-class professional translator and linguistic expert specializing in {src_name} and {tgt_name}.
Your mission is to translate text faithfully, preserving exact meaning, nuance, tone, and cultural appropriateness.

Target Language: {tgt_name}
Source Language: {src_name}

{tone_instruction}

Linguistic & Formatting Guidelines:
1. When translating into Khmer:
   - Follow standard Cambodian orthography (Choun Nath dictionary standard).
   - Use proper Khmer punctuation: use Khan (។) at the end of sentences, and Bariyoosan (៕) at the end of paragraphs/texts.
   - Do NOT insert artificial English-style spaces between every Khmer word. Only use spaces where natural clauses or phrase boundaries exist in Khmer.
   - Maintain the proper stacking of Khmer subscript consonants (ជើងអក្សរ).
   - For modern technical or scientific terms without standard Khmer equivalents, provide the transliteration or standard loan word, and optionally keep the original in parentheses if it aids clarity.
2. Structure & Output:
   - Output ONLY the translated text.
   - Do NOT include conversational filler like "Here is the translation:" or "Translated text:".
   - Do NOT explain why you chose certain words unless explicitly asked.
   - Preserve all original formatting, markdown, line breaks, code blocks, bullet points, numbers, and XML/SRT subtitle tags.
"""
    return prompt.strip()


def build_translation_user_prompt(text: str, context: Optional[str] = None) -> str:
    """Creates the user turn prompt for translation."""
    if context:
        return f"Context:\n{context}\n\nText to translate:\n{text}"
    return f"Text to translate:\n{text}"
