"""Context-Aware Video Dialogue Translator.

Translates subtitle segments in conversational clusters rather than isolated fragments,
ensuring natural conversational flow, proper pronouns, and logical film dubbing in Khmer.
"""

import json
import logging
import re
import asyncio
from typing import List, Dict, Any, Optional
import httpx

from . import get_engine
from ..khmer import normalize_khmer_unicode, polish_khmer_dialogue

logger = logging.getLogger(__name__)


async def translate_dialogue_batch_gemini(
    segments: List[Dict[str, Any]],
    source_lang: str,
    api_key: Optional[str] = None,
    tone: str = "casual",
    model: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Translates an entire dialogue scene using a 2-Step Reasoning Pivot:
    Step 1: Chinese -> English Contextual Meaning & Intent (resolves wuxia terms, sarcasm, slang, relationships)
    Step 2: English -> Authentic Conversational Khmer Film Dubbing (natural prose, proper pronouns, emotional particles)
    """
    import os
    effective_key = (api_key or os.getenv("GEMINI_API_KEY", "")).strip()
    effective_model = model or os.getenv("DEFAULT_GEMINI_MODEL", "gemini-flash-lite-latest")

    if not effective_key:
        raise ValueError("Gemini API key is required for AI Dialogue Dubbing.")

    dialogue_payload = [
        {
            "id": seg["id"],
            "speaker": seg.get("speaker", "Speaker"),
            "text": seg.get("text", ""),
            "duration_sec": round(max(0.6, float(seg.get("end", 0)) - float(seg.get("start", 0))), 1)
        }
        for seg in segments if seg.get("text", "").strip()
    ]
    if not dialogue_payload:
        return segments

    system_prompt = f"""You are a master cinematic translation & voice dubbing director specializing in {source_lang}-to-Khmer film localization (អ្នកដឹកនាំការបញ្ចូលសំឡេងភាពយន្តខ្មែរ).

CRITICAL 2-STEP TRANSLATION & REASONING PIVOT:
To guarantee 100% logical clarity, accurate idioms, and natural dialogue:
STEP 1 ({source_lang} -> English Meaning & Reasoning):
- Accurately interpret the contextual meaning, intent, sarcasm, emotion, and character relationships into natural English.
- Resolve historical/wuxia/cultural terms correctly (e.g. '两' = taels of silver [តម្លឹង], '客官' = guest/customer, '掌柜' = innkeeper/boss, '元宝' = silver/gold ingot, '穿越' = transmigrate/time-travel to another world, '无敌' = invincible/unbeatable).
- NEVER confuse homophones or translate idioms literally (e.g. '老妈我都多大了' -> 'Mom, I am an adult now!', '是你给我50两' -> 'You owe ME 50 taels of silver!').
- STRICT FILTER: If the input text contains video watermarks, channel badges, or resolution labels (e.g. '4K', '超清', '漫剧', '3473', '全集', '关注', 'bilibili'), COMPLETELY IGNORE them. Translate ONLY the actual spoken dialogue!

STEP 2 (English -> Authentic Conversational Khmer Film Dubbing):
- Translate the refined English intent into 100% natural, lively, and emotional Khmer dialogue (ភាសានិយាយក្នុងភាពយន្តខ្មែរ).
- LIP-SYNC & BREVITY: The Khmer translation must be concise and punchy to match the character's speaking duration (see duration_sec). Keep short lines brief and colloquial so speaking duration matches mouth movement without rushing!
- Use natural Cambodian pronouns (ខ្ញុំ, បង, អូន, ឯង, ម៉ាក់, ប៉ា, ថៅកែ, លោក, ទូលបង្គំ, ព្រះអង្គ) based on character hierarchy.
- Use natural spoken particles (ណា, ហ្នឹង, ទេ, ម៉ង, តើ, ម្ល៉េះ).
- NEVER output Thai (e.g., นี่คือ, ตำลึง), Vietnamese, or robotic transliterations.
- The "translation" field must contain ONLY pure spoken Khmer dialogue for voice actor dubbing. NEVER include original Chinese characters, pinyin, or explanatory parenthetical glosses like '(ផ្ទះសំណាក់)' or '(还有)'.

OUTPUT FORMAT: Return ONLY a valid JSON array:
[
  {{
    "id": 1,
    "english_intent": "Exact English meaning and character intent",
    "translation": "100% natural conversational Khmer film dubbing"
  }}
]
"""

    user_prompt = f"Dialogue scene to translate with 2-step English reasoning:\n{json.dumps(dialogue_payload, ensure_ascii=False, indent=2)}"

    candidate_models = []
    for m in [effective_model, "gemini-flash-latest", "gemini-3.1-flash-lite", "gemini-flash-lite-latest", "gemini-2.5-flash-lite", "gemini-2.5-flash"]:
        if m and m not in candidate_models:
            candidate_models.append(m)

    payload = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json",
        },
    }

    last_err = None
    async with httpx.AsyncClient(timeout=45.0) as client:
        for cur_model in candidate_models:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{cur_model}:generateContent?key={effective_key}"
            try:
                resp = await client.post(url, json=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
                    parsed = json.loads(raw_text)
                    trans_map = {}
                    eng_map = {}
                    for item in parsed:
                        if "id" in item:
                            trans_map[item["id"]] = item.get("translation", "")
                            eng_map[item["id"]] = item.get("english_intent", "")

                    for seg in segments:
                        if seg["id"] in trans_map:
                            t_val = trans_map[seg["id"]]
                            # Clean any leftover watermark remnants
                            t_val = re.sub(r'(?:4k|超清|漫剧|\b3473\b|\b1080p\b|\b720p\b|រឿងកំប្លែង\s*4k|4k\s*រឿងកំប្លែង|ច្បាស់បំផុត\s*3473|4k\s*ultra)', '', t_val, flags=re.IGNORECASE).strip()
                            seg["translation"] = normalize_khmer_unicode(polish_khmer_dialogue(t_val))
                        if seg["id"] in eng_map and eng_map[seg["id"]]:
                            seg["english_intent"] = eng_map[seg["id"]]
                    return segments
                else:
                    logger.warning(f"Model {cur_model} returned {resp.status_code}. Trying next model...")
                    last_err = f"Gemini API returned {resp.status_code}: {resp.text}"
            except Exception as ex:
                logger.warning(f"Model {cur_model} failed: {ex}. Trying next model...")
                last_err = str(ex)

        raise RuntimeError(f"All Gemini models failed. Last error: {last_err}")


async def translate_dialogue_clusters_free(
    segments: List[Dict[str, Any]],
    source_lang: str,
    tone: str = "casual",
    cluster_size: int = 8,
    max_concurrency: int = 6,
    on_progress=None,
) -> List[Dict[str, Any]]:
    """
    Groups contiguous dialogue lines into numbered clusters before translating via
    the free endpoint in parallel, cutting translation time from 20+ mins down to ~1 min.
    """
    import re
    free_engine = get_engine("free")
    pattern = re.compile(r"^\s*(\d+)[\.\:\)]\s*(.*)$")

    chunks = [segments[i : i + cluster_size] for i in range(0, len(segments), cluster_size)]
    if not chunks:
        return segments

    semaphore = asyncio.Semaphore(max_concurrency)
    done_count = 0
    lock = asyncio.Lock()

    def parse_numbered_output(text: str, count: int) -> List[str]:
        output = [""] * count
        for raw_line in text.splitlines():
            line_s = raw_line.strip()
            m = pattern.match(line_s)
            if m:
                idx = int(m.group(1))
                if 1 <= idx <= count:
                    output[idx - 1] = m.group(2).strip()
        # Fallback if numbering was stripped
        if all(not x for x in output):
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            for idx in range(min(count, len(lines))):
                output[idx] = lines[idx]
        return output

    async def _translate_one_chunk(chunk: List[Dict[str, Any]]):
        nonlocal done_count
        texts = [s.get("text", "").strip() or "..." for s in chunk]
        numbered_query = "\n".join(f"{idx}. {t}" for idx, t in enumerate(texts, start=1))

        async with semaphore:
            try:
                if source_lang and source_lang.lower() not in ("en", "english"):
                    eng_joined = await free_engine.translate(
                        text=numbered_query,
                        source_lang=source_lang,
                        target_lang="en",
                        tone=tone,
                    )
                    translated_joined = await free_engine.translate(
                        text=eng_joined,
                        source_lang="en",
                        target_lang="km",
                        tone=tone,
                    )
                else:
                    eng_joined = numbered_query
                    translated_joined = await free_engine.translate(
                        text=numbered_query,
                        source_lang=source_lang,
                        target_lang="km",
                        tone=tone,
                    )

                km_pieces = parse_numbered_output(translated_joined, len(chunk))
                en_pieces = parse_numbered_output(eng_joined, len(chunk))

                for idx, seg in enumerate(chunk):
                    km_text = km_pieces[idx] if idx < len(km_pieces) and km_pieces[idx] else ""
                    en_text = en_pieces[idx] if idx < len(en_pieces) and en_pieces[idx] else ""

                    if km_text and km_text != "...":
                        # Clean any stray watermark words
                        km_text = re.sub(r'(?:4k|超清|漫剧|\b3473\b|\b1080p\b|\b720p\b|រឿងកំប្លែង\s*4k|4k\s*រឿងកំប្លែង|ច្បាស់បំផុត\s*3473|4k\s*ultra)', '', km_text, flags=re.IGNORECASE).strip()
                        seg["translation"] = normalize_khmer_unicode(polish_khmer_dialogue(km_text))
                    if en_text and en_text != "...":
                        seg["english_intent"] = en_text

            except Exception as e:
                logger.warning(f"Chunk translate failed ({e}). Falling back for chunk...")
                for s in chunk:
                    t = s.get("text", "").strip()
                    if t:
                        try:
                            eng = await free_engine.translate(t, source_lang, "en", tone=tone)
                            res = await free_engine.translate(eng, "en", "km", tone=tone)
                            s["english_intent"] = eng
                            s["translation"] = normalize_khmer_unicode(polish_khmer_dialogue(res))
                        except Exception:
                            pass

        async with lock:
            done_count += 1
            if on_progress:
                on_progress(done_count, len(chunks))

    tasks = [_translate_one_chunk(c) for c in chunks]
    await asyncio.gather(*tasks)
    return segments
