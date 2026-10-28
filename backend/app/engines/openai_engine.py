"""OpenAI / Ollama / Groq Compatible Translation Engine."""

import json
import logging
from typing import AsyncGenerator, Dict, Any, Optional
import httpx

from .base import BaseTranslationEngine
from ..core.prompts import build_system_prompt, build_translation_user_prompt
from ..khmer import normalize_khmer_unicode

logger = logging.getLogger(__name__)


class OpenAIEngine(BaseTranslationEngine):
    name: str = "openai"

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-4o-mini",
    ):
        self.api_key = api_key or ""
        self.base_url = base_url.rstrip("/")
        self.model = model

    def is_configured(self) -> bool:
        return bool(self.api_key or "localhost" in self.base_url or "127.0.0.1" in self.base_url)

    async def translate(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
        tone: str = "standard",
        options: Optional[Dict[str, Any]] = None,
    ) -> str:
        options = options or {}
        key = options.get("api_key") or self.api_key
        base_url = (options.get("base_url") or self.base_url).rstrip("/")
        model = options.get("model") or self.model

        system_prompt = build_system_prompt(source_lang, target_lang, tone=tone)
        user_prompt = build_translation_user_prompt(text, context=options.get("context"))

        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
        }

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(f"{base_url}/chat/completions", headers=headers, json=payload)
            if resp.status_code != 200:
                raise RuntimeError(f"API Error ({resp.status_code}): {resp.text}")

            data = resp.json()
            choices = data.get("choices", [])
            if not choices:
                return ""
            translated = choices[0].get("message", {}).get("content", "").strip()

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
        key = options.get("api_key") or self.api_key
        base_url = (options.get("base_url") or self.base_url).rstrip("/")
        model = options.get("model") or self.model

        system_prompt = build_system_prompt(source_lang, target_lang, tone=tone)
        user_prompt = build_translation_user_prompt(text, context=options.get("context"))

        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
            "stream": True,
        }

        async with httpx.AsyncClient(timeout=90.0) as client:
            async with client.stream("POST", f"{base_url}/chat/completions", headers=headers, json=payload) as response:
                if response.status_code != 200:
                    err = await response.aread()
                    raise RuntimeError(f"Stream API Error ({response.status_code}): {err.decode()}")

                async for line in response.aiter_lines():
                    if not line:
                        continue
                    if line.startswith("data: "):
                        data_str = line[6:].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data_str)
                            delta = chunk.get("choices", [{}])[0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                yield content
                        except json.JSONDecodeError:
                            continue
