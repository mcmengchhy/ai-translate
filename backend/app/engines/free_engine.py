"""Instant Zero-Config Translation Engine (Free / Fallback).

Uses public translation endpoints to provide immediate English <-> Khmer
translations without requiring an API key.
"""

import logging
from contextlib import asynccontextmanager
from email.utils import parsedate_to_datetime
import time
from typing import AsyncGenerator, Dict, Any, Optional
import httpx

from .base import BaseTranslationEngine
from ..khmer import normalize_khmer_unicode
from ..khmer.native_phrases import native_phrase_translation
from ..video.resources import resource_slot, wait_for_service, defer_service

logger = logging.getLogger(__name__)


class FreeEngine(BaseTranslationEngine):
    name: str = "free"
    _client = None

    @asynccontextmanager
    async def session(self):
        """Reuse connections for a whole dialogue job without sharing event loops."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            engine = FreeEngine()
            engine._client = client
            yield engine

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
        if target_lang == "km":
            phrase = native_phrase_translation(text, tone)
            if phrase:
                return phrase

        url = "https://translate.googleapis.com/translate_a/single"
        params = {
            "client": "gtx",
            "sl": "auto" if source_lang == "auto" else source_lang,
            "tl": target_lang,
            "dt": "t",
            "q": text,
        }

        for attempt in range(4):
            await wait_for_service("free_translation")
            async with resource_slot("translation_requests", 6):
                if self._client is not None:
                    resp = await self._client.get(url, params=params)
                else:
                    async with httpx.AsyncClient(timeout=30.0) as client:
                        resp = await client.get(url, params=params)
            if resp.status_code not in {429, 503}:
                break
            try:
                retry_after = float(resp.headers.get("Retry-After", "0"))
            except ValueError:
                try:
                    retry_after = max(0, parsedate_to_datetime(resp.headers["Retry-After"]).timestamp() - time.time())
                except (KeyError, TypeError, ValueError):
                    retry_after = 0
            delay = max(retry_after, 30 * 2 ** attempt)
            defer_service("free_translation", delay)
            logger.warning("Translation service rate limited/unavailable; pausing all jobs for %.0f seconds", delay)
        if resp.status_code != 200:
            raise RuntimeError(f"Translation service returned status {resp.status_code}")
        data = resp.json()
        translated_pieces = []
        if data and isinstance(data, list) and isinstance(data[0], list):
            for segment in data[0]:
                if isinstance(segment, list) and segment and segment[0]:
                    translated_pieces.append(segment[0])
        result = "".join(translated_pieces)
        return normalize_khmer_unicode(result) if target_lang == "km" else result

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
