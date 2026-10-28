"""Google Gemini AI Translation Engine."""

import json
import logging
from typing import AsyncGenerator, Dict, Any, Optional
import httpx

from .base import BaseTranslationEngine
from ..core.prompts import build_system_prompt, build_translation_user_prompt
from ..khmer import normalize_khmer_unicode

logger = logging.getLogger(__name__)

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"


class GeminiEngine(BaseTranslationEngine):
    name: str = "gemini"

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        import os
        self.api_key = (api_key or os.getenv("GEMINI_API_KEY", "")).strip()
        self.model = model or os.getenv("DEFAULT_GEMINI_MODEL", "gemini-flash-lite-latest")

    def set_api_key(self, api_key: str):
        self.api_key = api_key.strip()

    def is_configured(self) -> bool:
        return bool(self.api_key)

    async def translate(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
        tone: str = "standard",
        options: Optional[Dict[str, Any]] = None,
    ) -> str:
        options = options or {}
        effective_key = options.get("api_key") or self.api_key
        effective_model = options.get("model") or self.model

        if not effective_key:
            raise ValueError("Gemini API key is required. Please set it in settings or .env.")

        system_prompt = build_system_prompt(source_lang, target_lang, tone=tone)
        user_prompt = build_translation_user_prompt(text, context=options.get("context"))

        url = f"{GEMINI_BASE_URL}/{effective_model}:generateContent?key={effective_key}"
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {
                "temperature": 0.2,
                "topP": 0.95,
                "maxOutputTokens": 4096,
            },
        }

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code != 200:
                logger.error(f"Gemini API error: {resp.status_code} - {resp.text}")
                try:
                    err_json = resp.json()
                    err_msg = err_json.get("error", {}).get("message", resp.text)
                except Exception:
                    err_msg = resp.text
                raise RuntimeError(f"Gemini API Error ({resp.status_code}): {err_msg}")

            data = resp.json()
            candidates = data.get("candidates", [])
            if not candidates:
                return ""
            parts = candidates[0].get("content", {}).get("parts", [])
            translated = "".join(part.get("text", "") for part in parts).strip()

            if target_lang == "km":
                translated = normalize_khmer_unicode(translated)

            return translated

    async def translate_stream(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
        tone: str = "standard",
        options: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[str, None]:
        options = options or {}
        effective_key = options.get("api_key") or self.api_key
        effective_model = options.get("model") or self.model

        if not effective_key:
            raise ValueError("Gemini API key is required. Please configure it in Settings.")

        system_prompt = build_system_prompt(source_lang, target_lang, tone=tone)
        user_prompt = build_translation_user_prompt(text, context=options.get("context"))

        url = f"{GEMINI_BASE_URL}/{effective_model}:streamGenerateContent?alt=sse&key={effective_key}"
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {
                "temperature": 0.2,
                "topP": 0.95,
                "maxOutputTokens": 4096,
            },
        }

        async with httpx.AsyncClient(timeout=90.0) as client:
            async with client.stream("POST", url, json=payload) as response:
                if response.status_code != 200:
                    err_body = await response.aread()
                    try:
                        err_json = json.loads(err_body.decode())
                        err_msg = err_json.get("error", {}).get("message", err_body.decode())
                    except Exception:
                        err_msg = err_body.decode()
                    raise RuntimeError(f"Gemini Stream Error ({response.status_code}): {err_msg}")

                async for line in response.aiter_lines():
                    if not line:
                        continue
                    if line.startswith("data: "):
                        json_str = line[6:].strip()
                        if json_str == "[DONE]":
                            break
                        try:
                            chunk_data = json.loads(json_str)
                            candidates = chunk_data.get("candidates", [])
                            if candidates:
                                parts = candidates[0].get("content", {}).get("parts", [])
                                for part in parts:
                                    part_text = part.get("text", "")
                                    if part_text:
                                        yield part_text
                        except json.JSONDecodeError:
                            continue
