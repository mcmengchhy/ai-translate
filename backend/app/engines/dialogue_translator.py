"""Context-Aware Video Dialogue Translator.

Translates subtitle segments in conversational clusters rather than isolated fragments,
ensuring natural conversational flow, proper pronouns, and logical film dubbing in Khmer.
"""

import json
import logging
import asyncio
import re
from typing import List, Dict, Any, Optional, Tuple
import httpx

from . import get_engine
from ..khmer import normalize_khmer_unicode, polish_khmer_dialogue

logger = logging.getLogger(__name__)


def detect_dialogue_gender(text: str, english_intent: str = "", default: str = "male") -> str:
    """Detects character gender ('male' or 'female') from Chinese and English conversational cues."""
    c_lower = text.strip().lower()
    e_lower = english_intent.strip().lower()

    female_cn_markers = [
        "妈", "娘", "姐", "妹", "婆", "姨", "女", "太太", "夫人", "姑娘",
        "妃", "后", "丫头", "媳妇", "奶奶", "伯母", "婶", "姑", "小姐", "公主",
        "皇后", "她", "女侠", "师妹", "师姐", "女儿", "母后"
    ]
    male_cn_markers = [
        "爸", "爹", "哥", "弟", "爷", "叔", "男", "先生", "少爷", "皇帝",
        "王爷", "他", "儿子", "师傅", "师兄", "师弟", "陛下", "大人", "侍卫",
        "兄弟", "老公", "老汉", "老子"
    ]

    female_en_markers = [
        r"\bshe\b", r"\bher\b", r"\bhers\b", r"\bmom\b", r"\bmother\b",
        r"\bsister\b", r"\bwife\b", r"\bgirl\b", r"\bwoman\b", r"\bdaughter\b",
        r"\blady\b", r"\bqueen\b", r"\bprincess\b", r"\bmadam\b", r"\baunt\b",
        r"\bgrandma\b", r"\bmiss\b", r"\bms\b", r"\bmrs\b"
    ]
    male_en_markers = [
        r"\bhe\b", r"\bhim\b", r"\bhis\b", r"\bdad\b", r"\bfather\b",
        r"\bbrother\b", r"\bhusband\b", r"\bboy\b", r"\bman\b", r"\bson\b",
        r"\bking\b", r"\bprince\b", r"\bmaster\b", r"\buncle\b", r"\bgrandpa\b",
        r"\bmr\b", r"\blord\b"
    ]

    female_score = sum(1 for m in female_cn_markers if m in c_lower)
    female_score += sum(1 for p in female_en_markers if re.search(p, e_lower, re.I))

    male_score = sum(1 for m in male_cn_markers if m in c_lower)
    male_score += sum(1 for p in male_en_markers if re.search(p, e_lower, re.I))

    if female_score > male_score:
        return "female"
    if male_score > female_score:
        return "male"
    return default


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
    preferred_model = model or os.getenv("DEFAULT_GEMINI_MODEL", "gemini-3-flash-preview")

    if not effective_key:
        raise ValueError("Gemini API key is required for AI Dialogue Dubbing.")

    dialogue_payload = [
        {"id": seg["id"], "speaker": seg.get("speaker", "Speaker"), "text": seg.get("text", "")}
        for seg in segments if seg.get("text", "").strip()
    ]
    if not dialogue_payload:
        return segments

    system_prompt = f"""You are an elite cinematic dubbing director specializing in {source_lang}-to-Khmer film localization (អ្នកដឹកនាំការបញ្ចូលសំឡេងភាពយន្តខ្មែរ).

MISSION:
Transform dramatic dialogue into 100% authentic, emotionally resonant spoken Khmer film dubbing (ភាសានិយាយក្នុងភាពយន្ត កម្រិតស្ទូឌីយោ).

CRITICAL 2-STEP TRANSLATION & REASONING PIVOT:
To guarantee 100% logical clarity, accurate idioms, and natural dialogue:
STEP 1 ({source_lang} -> English Meaning & Reasoning):
- Accurately interpret the contextual meaning, intent, sarcasm, emotion, and character relationships into natural English.
- Resolve historical/wuxia/costume drama terms accurately:
  * '爷爷' = Grandpa (លោកតា)
  * '公公' = Father-in-law (លោកពុក / ពុកក្មេក) or Eunuch in imperial palace
  * '婆婆' = Mother-in-law (លោកម៉ែ / ម៉ែក្មេក)
  * '粮食' / '救命粮' = Life-saving food grain / emergency rations (ស្បៀងសង្គ្រោះជីវិត / ស្បៀងអាហារ)
  * '活路' = Way to survive (ផ្លូវរស់)
  * '两' / '白银' = Taels of silver (តម្លឹងប្រាក់)
  * '掌柜' = Boss / Innkeeper (ថៅកែ)
  * '客官' = Respected guest / customer (លោកម្ចាស់ / ភ្ញៀវ)
  * '穿越' = Transmigrated / time-traveled to ancient times (ឆ្លងភព)
  * '无敌' = Invincible (គ្មានគូប្រៀប / ខ្លាំងអស្ចារ្យ)
- NEVER confuse homophones or translate idioms literally (e.g. '老妈我都多大了' -> 'Mom, I am an adult now!', '是你给我50两' -> 'You owe ME 50 taels of silver!').

STEP 2 (English -> Authentic Conversational Khmer Film Dubbing):
- Translate the refined English intent into 100% natural, lively, and emotional spoken Khmer dialogue (ភាសានិយាយភាពយន្ត).
- Use natural Cambodian family/social pronouns (ខ្ញុំ, បង, អូន, ឯង, ម៉ាក់, ប៉ា, លោកពុក, លោកតា, ថៅកែ, លោកម្ចាស់) based on character hierarchy.
- Use natural conversational particles to convey urgency and emotion (ណា, ហ្នឹង, ទេ, ម៉ង, តើ, ម្ល៉េះ, ទៅ, អើយ).
- TIMING & RHYTHMIC FITTING: Keep lines concise, punchy, and natural so voice actors can deliver them smoothly without rushing.

STRICT NEGATIVE SCRIPT CONSTRAINT:
- NEVER use Thai characters (e.g., ร, ้, ่, อ, ง, ื, ฯ) or Vietnamese text. Output MUST BE 100% PURE KHMER SCRIPT (អក្សរខ្មែរ).
- The "translation" field must contain ONLY pure spoken Khmer dialogue for voice actor dubbing. NEVER include original Chinese characters, pinyin, or explanatory parenthetical glosses like '(ផ្ទះសំណាក់)' or '(还有)'.

STEP 3 (Character Voice & Gender Matching):
- Accurately identify the character speaker role and gender for each line based on the scene dialogue, vocatives, and context:
  * "gender": "female" for any female speaker (e.g., mother, daughter, sister, wife, maiden, girl, empress, concubine, grandma).
  * "gender": "male" for any male speaker (e.g., father, son, brother, husband, boy, man, emperor, master, scholar, grandpa).
- "speaker": Short character role name (e.g. "Grandson", "Daughter-in-law", "Son", "Father", "Mother", "Narrator").

OUTPUT FORMAT: Return ONLY a valid JSON array:
[
  {{
    "id": 1,
    "gender": "male" or "female",
    "speaker": "Character Role",
    "english_intent": "Exact English meaning and character intent",
    "translation": "100% natural conversational Khmer film dubbing"
  }}
]
"""

    user_prompt = f"Dialogue scene to translate with 2-step English reasoning:\n{json.dumps(dialogue_payload, ensure_ascii=False, indent=2)}"

    candidate_models = [preferred_model]
    for fallback in ["gemini-3-flash-preview", "gemini-3.1-flash-lite-preview", "gemini-flash-lite-latest"]:
        if fallback not in candidate_models:
            candidate_models.append(fallback)

    last_error = None
    for cur_model in candidate_models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{cur_model}:generateContent?key={effective_key}"
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {
                "temperature": 0.2,
                "responseMimeType": "application/json",
            },
        }

        try:
            async with httpx.AsyncClient(timeout=45.0) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
                    parsed = json.loads(raw_text)
                    trans_map = {}
                    eng_map = {}
                    gender_map = {}
                    speaker_map = {}
                    for item in parsed:
                        if "id" in item:
                            trans_map[item["id"]] = item.get("translation", "")
                            eng_map[item["id"]] = item.get("english_intent", "")
                            g_val = str(item.get("gender", "")).lower().strip()
                            gender_map[item["id"]] = "female" if g_val == "female" else ("male" if g_val == "male" else None)
                            speaker_map[item["id"]] = item.get("speaker", "")

                    for seg in segments:
                        if seg["id"] in trans_map:
                            seg["translation"] = normalize_khmer_unicode(polish_khmer_dialogue(trans_map[seg["id"]]))
                        if seg["id"] in eng_map and eng_map[seg["id"]]:
                            seg["english_intent"] = eng_map[seg["id"]]
                        
                        # Assign character voice type and gender
                        detected_gender = gender_map.get(seg["id"])
                        if not detected_gender:
                            detected_gender = detect_dialogue_gender(seg.get("text", ""), seg.get("english_intent", ""))
                        seg["gender"] = detected_gender
                        seg["voice_type"] = detected_gender

                        if seg["id"] in speaker_map and speaker_map[seg["id"]]:
                            seg["speaker"] = speaker_map[seg["id"]]
                    return segments
                else:
                    last_error = f"Gemini {cur_model} returned {resp.status_code}: {resp.text}"
                    logger.warning(f"Model {cur_model} failed ({resp.status_code}), attempting fallback...")
        except Exception as e:
            last_error = f"Gemini {cur_model} error: {e}"
            logger.warning(f"Model {cur_model} raised exception: {e}, attempting fallback...")

    raise RuntimeError(f"All Gemini models failed: {last_error}")


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
                        seg["translation"] = normalize_khmer_unicode(polish_khmer_dialogue(km_text))
                    if en_text and en_text != "...":
                        seg["english_intent"] = en_text

                    # Rule-based character voice & gender detection
                    detected_gender = detect_dialogue_gender(seg.get("text", ""), seg.get("english_intent", ""))
                    seg["gender"] = detected_gender
                    seg["voice_type"] = detected_gender

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
