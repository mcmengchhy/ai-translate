"""Context-Aware Video Dialogue Translator.

Translates subtitle segments in conversational clusters rather than isolated fragments,
ensuring natural conversational flow, proper pronouns, and logical film dubbing in Khmer.
"""

import json
import logging
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
        {"id": seg["id"], "speaker": seg.get("speaker", "Speaker"), "text": seg.get("text", "")}
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

STEP 2 (English -> Authentic Conversational Khmer Film Dubbing):
- Translate the refined English intent into 100% natural, lively, and emotional Khmer dialogue (ភាសានិយាយក្នុងភាពយន្តខ្មែរ).
- Use natural Cambodian pronouns (ខ្ញុំ, បង, អូន, ឯង, ម៉ាក់, ប៉ា, ថៅកែ, លោក) based on character hierarchy.
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

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{effective_model}:generateContent?key={effective_key}"
    payload = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json",
        },
    }

    async with httpx.AsyncClient(timeout=45.0) as client:
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
                    seg["translation"] = normalize_khmer_unicode(polish_khmer_dialogue(trans_map[seg["id"]]))
                if seg["id"] in eng_map and eng_map[seg["id"]]:
                    seg["english_intent"] = eng_map[seg["id"]]
            return segments
        else:
            raise RuntimeError(f"Gemini API returned {resp.status_code}: {resp.text}")


async def translate_dialogue_clusters_free(
    segments: List[Dict[str, Any]],
    source_lang: str,
    tone: str = "casual",
    cluster_size: int = 4,
) -> List[Dict[str, Any]]:
    """
    Groups contiguous short speech fragments into context clusters before sending to
    the free translation endpoint, then splits and normalizes Khmer phrasing.
    """
    free_engine = get_engine("free")

    for i in range(0, len(segments), cluster_size):
        chunk = segments[i : i + cluster_size]
        texts = [s.get("text", "").strip() for s in chunk]
        
        # If segments are very short phrases, join with newline delimiter to preserve context
        joined_query = " \n ".join(texts)
        if joined_query.strip():
            try:
                # 2-step English pivot for superior reasoning: zh -> en -> km
                if source_lang and source_lang.lower() not in ("en", "english"):
                    eng_joined = await free_engine.translate(
                        text=joined_query,
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
                    eng_joined = joined_query
                    translated_joined = await free_engine.translate(
                        text=joined_query,
                        source_lang=source_lang,
                        target_lang="km",
                        tone=tone,
                    )

                pieces = [p.strip() for p in translated_joined.split("\n") if p.strip()]
                eng_pieces = [e.strip() for e in eng_joined.split("\n") if e.strip()]

                if len(pieces) == len(chunk):
                    for idx, s in enumerate(chunk):
                        s["translation"] = normalize_khmer_unicode(polish_khmer_dialogue(pieces[idx]))
                        if idx < len(eng_pieces):
                            s["english_intent"] = eng_pieces[idx]
                else:
                    for s in chunk:
                        t = s.get("text", "").strip()
                        if t:
                            eng = await free_engine.translate(t, source_lang, "en", tone=tone)
                            res = await free_engine.translate(eng, "en", "km", tone=tone)
                            s["english_intent"] = eng
                            s["translation"] = normalize_khmer_unicode(polish_khmer_dialogue(res))
            except Exception as e:
                logger.warning(f"Cluster translate failed: {e}. Translating individually...")
                for s in chunk:
                    t = s.get("text", "").strip()
                    if t:
                        try:
                            eng = await free_engine.translate(t, source_lang, "en", tone=tone)
                            res = await free_engine.translate(eng, "en", "km", tone=tone)
                            s["english_intent"] = eng
                            s["translation"] = normalize_khmer_unicode(polish_khmer_dialogue(res))
                        except Exception:
                            res = await free_engine.translate(t, source_lang, "km", tone=tone)
                            s["translation"] = normalize_khmer_unicode(polish_khmer_dialogue(res))

    return segments
