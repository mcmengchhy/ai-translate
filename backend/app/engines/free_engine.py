"""Instant Zero-Config Translation Engine (Free / Fallback).

Uses public translation endpoints to provide immediate English <-> Khmer
translations without requiring an API key.
"""

import logging
from typing import AsyncGenerator, Dict, Any, Optional
import httpx

from .base import BaseTranslationEngine
from ..khmer import normalize_khmer_unicode

logger = logging.getLogger(__name__)


class FreeEngine(BaseTranslationEngine):
    name: str = "free"

    def is_configured(self) -> bool:
        return True

    async def translate(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
        tone: str = "standard",
        options: Optional[Dict[str, Any]] = None,
    ) -> str:
        if not text.strip():
            return ""

        url = "https://translate.googleapis.com/translate_a/single"
        params = {
            "client": "gtx",
            "sl": "auto" if source_lang == "auto" else source_lang,
            "tl": target_lang,
            "dt": "t",
            "q": text,
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url, params=params)
            if resp.status_code != 200:
                raise RuntimeError(f"Translation service returned status {resp.status_code}")

            data = resp.json()
            # The structure is [[["translated chunk", "source chunk", ...]]]
            translated_pieces = []
            if data and isinstance(data, list) and len(data) > 0 and isinstance(data[0], list):
                for segment in data[0]:
                    if isinstance(segment, list) and len(segment) > 0 and segment[0]:
                        translated_pieces.append(segment[0])

            result = "".join(translated_pieces)
            if target_lang == "km":
                result = normalize_khmer_unicode(result)

            return result

    async def translate_stream(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
        tone: str = "standard",
        options: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[str, None]:
        # Free endpoint provides complete output; simulate fast streaming chunking
        full_translation = await self.translate(text, source_lang, target_lang, tone, options)
        chunk_size = 8
        for i in range(0, len(full_translation), chunk_size):
            yield full_translation[i : i + chunk_size]
