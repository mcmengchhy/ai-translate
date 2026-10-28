"""Base interface for translation engines."""

from abc import ABC, abstractmethod
from typing import AsyncGenerator, Dict, Any, Optional


class BaseTranslationEngine(ABC):
    """Abstract Base Class for translation backends."""

    name: str = "base"

    @abstractmethod
    async def translate(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
        tone: str = "standard",
        options: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Translate text from source_lang to target_lang."""
        pass

    @abstractmethod
    async def translate_stream(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
        tone: str = "standard",
        options: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[str, None]:
        """Stream translation chunks for live UI rendering."""
        pass

    @abstractmethod
    def is_configured(self) -> bool:
        """Returns True if the engine is ready with valid credentials/setup."""
        pass
