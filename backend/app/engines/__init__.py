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


def get_engine(name: str = "free", api_key: str = None) -> BaseTranslationEngine:
    """Retrieve an engine by name, falling back to free engine if not found."""
    import os
    engine = engines.get(name.lower(), engines["free"])
    eff_key = api_key or os.getenv("GEMINI_API_KEY", "")
    if isinstance(engine, GeminiEngine) and eff_key:
        engine.set_api_key(eff_key)
    return engine
