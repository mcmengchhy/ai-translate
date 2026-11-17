"""API route definitions for translation, streaming, and Khmer NLP tools."""

import json
import logging
from typing import List
from fastapi import APIRouter, HTTPException
from sse_starlette.sse import EventSourceResponse

from .models import (
    TranslateRequest,
    TranslateResponse,
    KhmerToolsRequest,
    KhmerToolsResponse,
    LanguageInfo,
    SystemStatusResponse,
)
from ..engines import engines, resolve_engine
from ..khmer import (
    normalize_khmer_unicode,
    convert_numerals,
    inject_zwsp,
    strip_zwsp,
    is_khmer_text,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

SUPPORTED_LANGUAGES = [
    LanguageInfo(code="km", name="Khmer", native_name="ភាសាខ្មែរ"),
    LanguageInfo(code="en", name="English", native_name="English"),
    LanguageInfo(code="zh", name="Chinese (Simplified)", native_name="中文"),
    LanguageInfo(code="fr", name="French", native_name="Français"),
    LanguageInfo(code="th", name="Thai", native_name="ไทย"),
    LanguageInfo(code="vi", name="Vietnamese", native_name="Tiếng Việt"),
    LanguageInfo(code="ja", name="Japanese", native_name="日本語"),
    LanguageInfo(code="ko", name="Korean", native_name="한국어"),
    LanguageInfo(code="de", name="German", native_name="Deutsch"),
    LanguageInfo(code="es", name="Spanish", native_name="Español"),
]


def post_process_translation(text: str, target_lang: str, req: TranslateRequest) -> str:
    """Apply Khmer linguistic enhancements."""
    if target_lang == "km" or is_khmer_text(text):
        text = normalize_khmer_unicode(text)
        if req.convert_numbers == "to_khmer":
            text = convert_numerals(text, to_khmer=True)
        elif req.convert_numbers == "to_arabic":
            text = convert_numerals(text, to_khmer=False)

        if req.inject_zwsp:
            text = inject_zwsp(text)

    elif req.convert_numbers == "to_arabic":
        text = convert_numerals(text, to_khmer=False)

    return text


@router.post("/translate", response_model=TranslateResponse)
async def translate_text(req: TranslateRequest):
    if not req.text.strip():
        return TranslateResponse(
            translated_text="",
            source_lang=req.source_lang,
            target_lang=req.target_lang,
            engine_used="none",
            tone=req.tone,
            characters_count=0,
        )

    engine = resolve_engine(req.engine, req.api_key, req.base_url)
    options = {
        "api_key": req.api_key,
        "model": req.model,
        "base_url": req.base_url,
        "context": req.context,
    }

    try:
        translated = await engine.translate(
            text=req.text,
            source_lang=req.source_lang,
            target_lang=req.target_lang,
            tone=req.tone,
            options=options,
        )
        final_text = post_process_translation(translated, req.target_lang, req)
        return TranslateResponse(
            translated_text=final_text,
            source_lang=req.source_lang,
            target_lang=req.target_lang,
            engine_used=engine.name,
            tone=req.tone,
            characters_count=len(final_text),
        )
    except Exception as e:
        logger.error(f"Translation failed with {engine.name}: {e}")
        # Graceful fallback to free engine if AI engine fails and user didn't explicitly forbid it
        if engine.name != "free":
            try:
                fallback_translated = await engines["free"].translate(
                    text=req.text,
                    source_lang=req.source_lang,
                    target_lang=req.target_lang,
                    tone=req.tone,
                    options=options,
                )
                final_text = post_process_translation(fallback_translated, req.target_lang, req)
                return TranslateResponse(
                    translated_text=final_text,
                    source_lang=req.source_lang,
                    target_lang=req.target_lang,
                    engine_used="free (fallback)",
                    tone=req.tone,
                    characters_count=len(final_text),
                )
            except Exception as fb_err:
                raise HTTPException(status_code=500, detail=f"Translation error: {str(e)} | Fallback error: {str(fb_err)}")
        raise HTTPException(status_code=500, detail=f"Translation error: {str(e)}")


@router.post("/translate/stream")
async def translate_text_stream(req: TranslateRequest):
    if not req.text.strip():
        async def empty_stream():
            yield json.dumps({"chunk": "", "done": True})
        return EventSourceResponse(empty_stream())

    engine = resolve_engine(req.engine, req.api_key, req.base_url)
    options = {
        "api_key": req.api_key,
        "model": req.model,
        "base_url": req.base_url,
        "context": req.context,
    }

    async def event_generator():
        try:
            async for chunk in engine.translate_stream(
                text=req.text,
                source_lang=req.source_lang,
                target_lang=req.target_lang,
                tone=req.tone,
                options=options,
            ):
                yield json.dumps({"chunk": chunk, "done": False, "engine": engine.name})
            yield json.dumps({"chunk": "", "done": True, "engine": engine.name})
        except Exception as e:
            logger.error(f"Stream error: {e}")
            yield json.dumps({"error": str(e), "done": True})

    return EventSourceResponse(event_generator())


@router.post("/tools", response_model=KhmerToolsResponse)
async def process_khmer_tool(req: KhmerToolsRequest):
    text = req.text
    action = req.action

    if action == "normalize":
        res = normalize_khmer_unicode(text)
    elif action == "to_khmer_num":
        res = convert_numerals(text, to_khmer=True)
    elif action == "to_arabic_num":
        res = convert_numerals(text, to_khmer=False)
    elif action == "add_zwsp":
        res = inject_zwsp(text)
    elif action == "strip_zwsp":
        res = strip_zwsp(text)
    else:
        raise HTTPException(status_code=400, detail=f"Unknown action '{action}'")

    return KhmerToolsResponse(result=res, action=action)


@router.get("/languages", response_model=List[LanguageInfo])
async def list_languages():
    return SUPPORTED_LANGUAGES


@router.get("/status", response_model=SystemStatusResponse)
async def system_status():
    return SystemStatusResponse(
        status="healthy",
        engines={
            "openai": engines["openai"].is_configured(),
            "free": True,
        },
        default_engine=resolve_engine().name,
    )
