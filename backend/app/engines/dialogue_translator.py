"""Translate source dialogue directly to Khmer; English is a review reference."""

import asyncio
import hashlib
import json
import logging
import re
from difflib import SequenceMatcher
from contextlib import AsyncExitStack

import httpx

from . import get_engine, resolve_engine
from .free_engine import FreeEngine
from ..khmer import normalize_khmer_unicode, polish_khmer_dialogue
from ..khmer.dialogue_fixer import source_english_phrase, source_phrase_translation
from ..khmer.romanization import is_source_romanization
from ..khmer.native_phrases import native_phrase_translation
from ..khmer.character_names import translation_glossary, missing_character_names, reviewed_reassurance
from ..core.prompts import build_khmer_style_instructions
from ..video.segments import MissingTranslationsError

logger = logging.getLogger(__name__)
FLOW = "speech_subtitles_to_khmer"


class IncompleteTranslationStage(MissingTranslationsError):
    def __init__(self, segments, stage):
        super().__init__(segments)
        self.args = (f"{stage} failed for cue(s): {', '.join(str(cue) for cue in self.cue_ids[:10])}. Retry the saved dialogue to continue.",)


def evidence(seg):
    text = (seg.get("text") or "").strip()
    subtitle = (seg.get("subtitle_text") or "").strip()
    if not subtitle and (seg.get("ocr_detected") or seg.get("source") == "subtitle_file"):
        subtitle = text
    speech = (seg.get("asr_text") or "").strip()
    if not speech and not subtitle:
        speech = text
    return subtitle, speech, subtitle or speech or text


def english_key(seg, source_lang):
    return hashlib.sha256(json.dumps([source_lang, *evidence(seg)], ensure_ascii=False).encode()).hexdigest()


def source_text(seg):
    """Prefer written dialogue, except a matching fragment of longer speech.

    Do not repair uncertain Chinese by guessing from a previous English draft.
    Keep both originals and flag substantial disagreement for human review.
    """
    subtitle, speech, primary = evidence(seg)
    seg.pop("source_warning", None)
    if subtitle and speech:
        written = re.sub(r"[\W_]", "", subtitle)
        spoken = re.sub(r"[\W_]", "", speech)
        matcher = SequenceMatcher(None, written, spoken, autojunk=False)
        shared = sum(block.size for block in matcher.get_matching_blocks())
        if len(spoken) >= 8 and 2 <= len(written) <= len(spoken) * .6 and shared >= len(written) * .75:
            return speech
        if written and spoken and matcher.ratio() < .5:
            seg["source_warning"] = "Speech and subtitles disagree. Review the original dialogue; the subtitle is used for direct translation."
    return primary


def source_key(seg, source_lang, tone):
    return hashlib.sha256(json.dumps([FLOW, source_lang, tone, *evidence(seg)], ensure_ascii=False).encode()).hexdigest()


def current_translation(seg, source_lang, tone):
    return bool((seg.get("translation") or "").strip() and not seg.get("translation_error")
                and seg.get("translation_flow") == FLOW
                and seg.get("translation_source_key") == source_key(seg, source_lang, tone))


def is_english(text):
    return isinstance(text, str) and bool(text.strip()) and not re.search(r"[\u4e00-\u9fff\u1780-\u17ff]", text) and bool(re.search(r"[A-Za-z0-9]", text))


def clean_khmer(text, source):
    text = re.sub(r"(?:4k|超清|漫剧|\b3473\b|\b1080p\b|\b720p\b)", "", text, flags=re.IGNORECASE).strip()
    text = normalize_khmer_unicode(polish_khmer_dialogue(text, source))
    if not any(char.isalnum() for char in text):
        return ""
    if re.search(r"[\u4e00-\u9fff]", source) and not re.search(r"[\u1780-\u17b3]", text) and re.search(r"[A-Za-z]", text):
        if not is_source_romanization(source, text):
            return ""
    return text


def store_english(seg, english, source_lang, subtitle_english="", speech_english=""):
    english = source_english_phrase(evidence(seg)[2]) or english
    if not is_english(english):
        return False
    seg.update(english_intent=english.strip(), english_evidence_key=english_key(seg, source_lang),
               english_subtitle=subtitle_english, english_speech=speech_english)
    return True


def store_khmer(seg, text, source_lang, tone):
    if seg.get("translation") != text or seg.get("audio_source") == "original_fallback":
        seg.update(_dirty=True, audio_file=None)
        # A generated replacement no longer carries the previous human review.
        seg.pop('translation_review', None)
        seg.pop('translation_review_note', None)
        for field in ("audio_source", "fallback_subtitle", "generation_warning", "review_required"):
            seg.pop(field, None)
    seg.update(translation=text, translation_flow=FLOW,
               translation_source_key=source_key(seg, source_lang, tone),
               translation_source_text=source_text(seg))
    seg.pop("translation_english_key", None)
    seg.pop("translation_error", None)
    if is_source_romanization(evidence(seg)[2], text):
        seg["translation_warning"] = "Name retained in Latin spelling. Review its Khmer pronunciation if needed."
    else:
        seg.pop("translation_warning", None)


def require_translations(segments):
    spoken = [seg for seg in segments if evidence(seg)[2]]
    missing_khmer = [seg for seg in spoken if not (seg.get("translation") or "").strip() or
                     seg.get("translation_flow") != FLOW or seg.get("translation_error")]
    if missing_khmer:
        raise IncompleteTranslationStage(missing_khmer, "Source dialogue to Khmer")


async def translate_dialogue_batch_openai(segments, source_lang, api_key=None, tone="casual", model=None,
                                          base_url=None, context_before=None, context_after=None, name_glossary=None):
    """Translate original speech/subtitles with scene context in one provider call."""
    engine = get_engine("openai")
    key, url, model = engine.request_settings({"api_key": api_key, "base_url": base_url, "model": model})
    local_options = engine.generation_options(url, model)
    scene = [*(context_before or []), *segments, *(context_after or [])]
    name_glossary = name_glossary or translation_glossary(scene)
    reviewed = {}
    for index, cue in enumerate(scene[:-1]):
        following = scene[index + 1]
        if isinstance(cue.get('id'), int) and following.get('id') == cue['id'] + 1:
            value = reviewed_reassurance(evidence(cue)[2], evidence(following)[2], name_glossary)
            if value:
                reviewed[str(cue['id'])] = value
    cues = []
    for seg in segments:
        subtitle, speech, text = evidence(seg)
        if not text:
            continue
        seg["translation_error"] = "Direct Khmer translation pending."
        duration = max(.6, float(seg.get("target_end", seg.get("end", 0))) - float(seg.get("target_start", seg.get("start", 0))))
        cues.append({"id": seg["id"], "subtitle_text": subtitle, "speech_text": speech, "text": source_text(seg),
                     "duration_sec": round(duration, 2), "reading_budget": min(76, max(16, round(duration * 14)))})
        if str(seg['id']) in reviewed:
            cues[-1]['reviewed_translation'] = reviewed[str(seg['id'])]
    if not cues:
        return segments

    prompt = f"""Translate the original {source_lang} movie dialogue DIRECTLY into natural Khmer for subtitles and dubbing.
The original source dialogue is authoritative. Do not translate through an English intermediate.
Use BOTH subtitle_text and speech_text for every cue. OCR and ASR may disagree on homophones.
Use legible written subtitles to identify names, titles and places; use speech and neighboring cues
for sentence intent. Do not discard either source or invent missing plot details.
Preserve negation, who acts on whom, questions, emotion, amounts and relationships.
For example, 阁 is a pavilion and 哥 is a brother; use the written evidence to resolve the homophone.
Translate actions such as 传信 as 'send a message', rather than treating them as a person's name.
Ignore watermarks, channel names and resolution labels. Context is read-only.
Translate idioms by meaning, not literally. Use natural Khmer clauses and phrase spacing.
Preserve foreign names consistently; do not leave ordinary words untranslated.
Use name_glossary spellings exactly for the source names it lists. The kind field distinguishes people, organizations, states and places.
Preserve a state's full name. Do not turn it into an ordinary Khmer word, a person's surname, or an unrelated modern country.
Do not invent a different Khmer word for a listed name, or insert names into cues that do not mention them.
For a person's name followed by 姐/姐姐, use its full sister_address spelling exactly (បងស្រី before the name).
Do not change an older-sister address to អូន, and do not move បងស្រី after the name.
Determine who is fine in 没事 from the spoken exchange: a speaker reassuring someone normally says ខ្ញុំមិនអីទេ។
When 嗯 + a named address + 没事 is followed by the speaker's first-person explanation, address the listener and say ខ្ញុំមិនអីទេ។
Do not force first person when the source actually says someone else is fine.
If a cue includes reviewed_translation, retain that source-reviewed wording exactly.
{build_khmer_style_instructions(tone, dialogue=True)}
Requested tone: {tone}. Keep wording concise for duration_sec and reading_budget without dropping meaning.
You may provide an english_intent as an optional English review reference. Khmer must come from the original dialogue.
Return ONLY a JSON array containing every target id exactly once:
[{{"id":1,"translation":"Khmer dialogue translated directly from the original source","english_intent":"Optional English reference"}}]."""
    def context(items):
        return [{"subtitle_text": evidence(seg)[0], "speech_text": evidence(seg)[1]} for seg in (items or [])]
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    async with httpx.AsyncClient(timeout=180 if local_options else 90) as client:
        async def request(system, body):
            response = await client.post(f"{url}/chat/completions", headers=headers, json={
                "model": model, "temperature": .2,
                **local_options,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": json.dumps(body, ensure_ascii=False)}],
            })
            if response.status_code != 200:
                raise RuntimeError(f"Dialogue API returned {response.status_code}")
            raw = response.json()["choices"][0]["message"]["content"].strip()
            parsed = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", raw))
            if not isinstance(parsed, list):
                raise ValueError("Dialogue response must be a JSON array")
            return {str(item["id"]): item for item in parsed if isinstance(item, dict) and "id" in item}

        body = {"scene_before": context(context_before), "target_cues": cues,
                "scene_after": context(context_after)}
        if name_glossary:
            body["name_glossary"] = name_glossary
        translated = await request(prompt, body)
        invalid = [cue for cue in cues if missing_character_names(
            cue['text'], reviewed.get(str(cue['id'])) or str(translated.get(str(cue['id']), {}).get('translation', '')), name_glossary)]
        if invalid:
            # Retry only affected IDs with the same source and name evidence.
            translated.update(await request(prompt + '\nCorrect the missing or altered character names. Keep every listed name exactly as specified.',
                                             {**body, 'target_cues': invalid}))
        for seg in segments:
            if not evidence(seg)[2]:
                continue
            item = translated.get(str(seg["id"]), {})
            value = reviewed.get(str(seg['id'])) or source_phrase_translation(evidence(seg)[2]) or item.get("translation", "")
            value = clean_khmer(value, evidence(seg)[2]) if isinstance(value, str) else ""
            if missing_character_names(source_text(seg), value, name_glossary):
                seg["translation_error"] = "A source name or place is missing or mistranslated. Retry with the name glossary."
                continue
            if not value:
                seg["translation_error"] = "Direct Khmer translation is missing. Retry this cue."
                continue
            store_khmer(seg, value, source_lang, tone)
            if not store_english(seg, item.get("english_intent", ""), source_lang):
                for field in ("english_intent", "english_evidence_key", "english_subtitle", "english_speech"):
                    seg.pop(field, None)
                seg["english_reference_error"] = "English reference unavailable; Khmer was translated directly from the source."
            else:
                seg.pop("english_reference_error", None)
    require_translations(segments)
    return segments


async def translate_dialogue_segments(segments, source_lang, engine_name="auto", api_key=None, model=None,
                                      base_url=None, tone="casual", on_progress=None, force=False, include_english=True):
    engine = resolve_engine(engine_name, api_key, base_url)
    names = translation_glossary(segments)
    if engine.name == "free":
        return await translate_dialogue_clusters_free(segments, source_lang, tone=tone, on_progress=on_progress,
                                                     force=force, include_english=include_english, name_glossary=names)
    provider = get_engine("openai")
    _, url, selected_model = provider.request_settings({"api_key": api_key, "base_url": base_url, "model": model})
    size = 8 if provider.generation_options(url, selected_model) else 30
    total = (len(segments) + size - 1) // size
    for number, start in enumerate(range(0, len(segments), size), 1):
        chunk = [seg for seg in segments[start:start + size]
                 if force or not current_translation(seg, source_lang, tone)]
        if not chunk:
            if on_progress:
                on_progress(number, total)
            continue
        for seg in chunk:
            if evidence(seg)[2]:
                seg["translation_error"] = "Direct Khmer translation pending."
        try:
            await translate_dialogue_batch_openai(chunk, source_lang, api_key, tone, model, base_url,
                                                  context_before=segments[max(0, start-3):start],
                                                  context_after=segments[start+size:start+size+3],
                                                  **({'name_glossary': names} if names else {}))
        except Exception as error:
            logger.warning("Direct provider translation failed (%s); retrying missing cues through the free direct engine", error)
            try:
                pending = [seg for seg in chunk if not current_translation(seg, source_lang, tone)]
                await translate_dialogue_clusters_free(pending, source_lang, tone=tone, include_english=include_english,
                                                      name_glossary=names)
            except MissingTranslationsError:
                # Continue later batches; the recovery queue isolates these cues.
                pass
        if on_progress:
            on_progress(number, total)
    require_translations(segments)
    return segments


async def translate_dialogue_clusters_free(segments, source_lang, tone="casual", cluster_size=8,
                                           max_concurrency=3, on_progress=None, force=False, include_english=True,
                                           name_glossary=None):
    """Translate each original cue directly, independently of optional English.

    Unnumbered requests match standalone translation and cannot shuffle adjacent
    cue outputs. Completed source checkpoints are reused during recovery.
    """
    engine = get_engine("free")
    name_glossary = name_glossary or translation_glossary(segments)
    semaphore = asyncio.Semaphore(max_concurrency)
    chunks = [segments[start:start+cluster_size] for start in range(0, len(segments), cluster_size)]
    completed = 0
    following_by_identity = {id(cue): following for cue, following in zip(segments, segments[1:])}

    async def translate_cue(seg):
        original = source_text(seg)
        if not original or (not force and current_translation(seg, source_lang, tone)):
            return
        seg["translation_error"] = "Direct Khmer translation pending."
        following = following_by_identity.get(id(seg), {})
        reviewed_value = reviewed_reassurance(original, evidence(following)[2], name_glossary) if (
            isinstance(seg.get('id'), int) and following.get('id') == seg['id'] + 1) else ''
        value = reviewed_value or source_phrase_translation(original) or native_phrase_translation(original, tone)
        if not value:
            for attempt in range(3):
                try:
                    result = await engine.translate(original, source_lang or "auto", "km", tone=tone)
                    value = clean_khmer(result, original)
                    if missing_character_names(original, value, name_glossary):
                        value = ''
                    if value:
                        break
                except Exception as error:
                    logger.warning("Direct Khmer translation for cue %s failed: %s", seg.get("id"), error)
                if attempt < 2:
                    await asyncio.sleep(.5 * (attempt + 1))
        if not value:
            seg["translation_error"] = "Direct source-to-Khmer translation failed after three attempts. Retry this cue."
            return
        store_khmer(seg, value, source_lang, tone)

        # This reference is never fed into Khmer translation and cannot block it.
        for field in ("english_intent", "english_evidence_key", "english_subtitle", "english_speech"):
            seg.pop(field, None)
        if not include_english:
            seg.pop("english_reference_error", None)
            return
        reference = source_english_phrase(original)
        try:
            if not reference:
                reference = original if (source_lang or "auto").lower() in ("en", "english") else await engine.translate(
                    original, source_lang or "auto", "en", tone=tone,
                )
            if store_english(seg, reference, source_lang):
                seg.pop("english_reference_error", None)
            else:
                seg["english_reference_error"] = "English reference unavailable; Khmer was translated directly from the source."
        except Exception:
            seg["english_reference_error"] = "English reference unavailable; Khmer was translated directly from the source."

    async def translate_chunk(chunk):
        nonlocal completed
        async with semaphore:
            for seg in chunk:
                await translate_cue(seg)
        completed += 1
        if on_progress:
            on_progress(completed, len(chunks))

    async with AsyncExitStack() as stack:
        if isinstance(engine, FreeEngine):
            engine = await stack.enter_async_context(engine.session())
        await asyncio.gather(*(translate_chunk(chunk) for chunk in chunks))
    require_translations(segments)
    return segments
