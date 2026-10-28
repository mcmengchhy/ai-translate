"""Translation engines registry and factory."""

from typing import Dict
from .base import BaseTranslationEngine
from .gemini_engine import GeminiEngine
from .openai_engine import OpenAIEngine
from .free_engine import FreeEngine

engines: Dict[str, BaseTranslationEngine] = {
    "free": FreeEngine(),
    "gemini": GeminiEngine(),
    "openai": OpenAIEngine(),
}


def get_engine(name: str = "free") -> BaseTranslationEngine:
    """Retrieve an engine by name, falling back to free engine if not found."""
    import os
    engine = engines.get(name.lower(), engines["free"])
    if isinstance(engine, GeminiEngine) and not engine.api_key:
        engine.set_api_key(os.getenv("GEMINI_API_KEY", ""))
    return engine
