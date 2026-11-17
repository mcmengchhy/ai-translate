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
        "- Use precise professional or academic Khmer, with clear sentences rather than needlessly dense phrasing.\n"
        "- Suitable for legal, business contracts, technical documentation, and government notices.\n"
        "- Avoid colloquialisms or slang."
    ),
    "polite": (
        "Tone: Polite & Respectful (គួរសម).\n"
        "- Use appropriate respectful address and polite requests where the source calls for them; avoid adding honorifics mechanically.\n"
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


def build_khmer_style_instructions(tone: str = "standard", dialogue: bool = False) -> str:
    """One Khmer style policy for text translation and scene-based dubbing."""
    register = TONE_INSTRUCTIONS.get(tone, TONE_INSTRUCTIONS["standard"])
    instructions = f"""Native Cambodian Khmer style:
{register}
- Express the intended meaning in Khmer word order; avoid literal foreign syntax and dictionary-by-dictionary phrasing.
- Prefer familiar Cambodian expressions when they preserve the meaning. Keep technical terms precise and keep the requested register.
- Preserve negation, uncertainty, tense/aspect, questions, commands, quantities, and who acts on whom.
- Choose pronouns and forms of address from evidence about the speakers and their relationship. If age, gender or rank is unknown, use neutral phrasing; never invent kinship or intimacy.
- Keep established names, titles, and recurring terms consistent. Royal and religious speech retains the appropriate respectful register even in conversational dialogue. Never replace a foreign royal title with the name of a Cambodian monarch.
- Chinese royal titles: 王爷/王爺 means a prince: use ព្រះអង្គ when addressing him and ព្រះអង្គម្ចាស់ when stating his title. 殿下 is ព្រះអង្គ; 太子/太子殿下 is ព្រះអង្គម្ចាស់រាជទាយាទ. Do not substitute the civil honorific ឯកឧត្តម (or misspelling ឯកឪត្តម) for these royal titles.
- Match the title to the person being addressed. A line can mention both a royal and a civil official; retain each person's rank. Do not change every honorific merely because a royal appears elsewhere in the scene.
- Preserve proper country, state, dynasty, and place names consistently. In a fictional setting, do not invent a modern-country equivalent or a historical identification that the source does not establish. Keep the full state name, including a naming prefix such as 大, rather than translating its syllables into unrelated Khmer words.
- 深思 means careful or deep consideration (គិតគូរម៉ត់ចត់), not merely thinking for a long time. Do not add greetings such as សូមជំរាបថា when the source has none.
- Use Khmer phrase spacing, not a space between every word. Preserve question marks and emotional punctuation; use ។ for ordinary sentence endings without doubling punctuation. Do not append ៕ to every subtitle or chat message.
- Silently reread the Khmer for natural flow and check it against the original before returning the final translation. Do not add explanations or facts absent from the source."""
    if dialogue:
        instructions += """
- Write dialogue an actor could say aloud in the scene. Avoid bureaucratic connectors, excessive nominalizations and repeated pronouns when Khmer naturally omits them.
- Use neighboring cues to resolve references and incomplete sentences. Preserve each cue's own meaning and ID; do not move dialogue between cues or translate read-only context.
- Keep speech concise enough for its timing. Rephrase naturally before shortening; do not remove a negation, action, relationship or plot detail to meet a character budget.
- Do not flatten an angry threat, hesitant reply, joke, or respectful request into the same friendly voice."""
    if tone == "casual":
        instructions += """
- Use spoken particles sparingly when the original emotion warrants them. Do not add បាទ/ចាស, បង/អូន, or សូម to every line.
Examples of conversational phrasing (translate the actual source, not these examples):
Don't worry. → កុំបារម្ភអី។
I didn't mean that. → ខ្ញុំមិនមែនចង់និយាយអ៊ីចឹងទេ។
How is that possible? → ម៉េចអាចទៅរួច?"""
    return instructions.strip()


def build_system_prompt(source_lang: str, target_lang: str, tone: str = "standard", preserve_formatting: bool = True) -> str:
    """
    Constructs an expert translation system prompt for LLMs.
    """
    src_name = LANGUAGE_NAMES.get(source_lang, source_lang)
    tgt_name = LANGUAGE_NAMES.get(target_lang, target_lang)
    tone_instruction = build_khmer_style_instructions(tone) if target_lang == "km" else "Match the source register in the target language."

    prompt = f"""You are a world-class professional translator and linguistic expert specializing in {src_name} and {tgt_name}.
Your mission is to translate text faithfully, preserving exact meaning, nuance, tone, and cultural appropriateness.

Target Language: {tgt_name}
Source Language: {src_name}

{tone_instruction}

Linguistic & Formatting Guidelines:
1. When translating into Khmer:
   - Follow standard Cambodian orthography (Choun Nath dictionary standard).
   - Use natural Khmer punctuation and preserve whether each sentence is a question, exclamation, or statement.
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
