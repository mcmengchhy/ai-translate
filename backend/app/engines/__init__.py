"""Translation engines registry and factory."""

from typing import Dict
from .base import BaseTranslationEngine
from .openai_engine import OpenAIEngine
from .free_engine import FreeEngine
from ..core import config

engines: Dict[str, BaseTranslationEngine] = {
    "free": FreeEngine(),
    "openai": OpenAIEngine(
        api_key=config.OPENAI_API_KEY,
        base_url=config.OPENAI_BASE_URL,
        model=config.OPENAI_MODEL,
    ),
}


def get_engine(name: str = "free") -> BaseTranslationEngine:
    """Retrieve a supported translation engine."""
    return engines[name.lower()]


def resolve_engine(req_engine: str = "auto", api_key: str | None = None, base_url: str | None = None):
    """Use request settings first, then server configuration, then the free engine."""
    if req_engine != "auto":
        return get_engine(req_engine)
    if api_key or base_url:
        return engines["openai"]
    if config.DEFAULT_ENGINE in engines:
        return engines[config.DEFAULT_ENGINE]
    if engines["openai"].is_configured():
        return engines["openai"]
    return engines["free"]
