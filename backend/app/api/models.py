"""Pydantic request and response models."""

from typing import Optional, Dict, Literal
from urllib.parse import urlsplit
from pydantic import BaseModel, Field, field_validator


class EngineOptions(BaseModel):
    engine: Literal["auto", "openai", "free"] = "auto"
    api_key: Optional[str] = Field(default=None, description="Optional OpenAI-compatible API key")
    model: Optional[str] = Field(default=None, description="Model identifier; defaults to the server's OPENAI_MODEL")
    base_url: Optional[str] = Field(default=None, description="OpenAI-compatible API URL, including /v1")

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value):
        if not value or not value.strip():
            return None
        value = value.strip().rstrip("/")
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError("Base URL must be an HTTP(S) URL without credentials, query, or fragment")
        return value


class TranslateRequest(EngineOptions):
    text: str = Field(..., description="Text to be translated")
    source_lang: str = Field(default="auto", description="Source language code (e.g. 'en', 'km', 'auto')")
    target_lang: str = Field(default="km", description="Target language code (e.g. 'km', 'en')")
    tone: str = Field(default="standard", description="Translation tone: standard, formal, polite, casual, literary")
    inject_zwsp: bool = Field(default=False, description="Insert Zero-Width Space between Khmer words/syllables")
    convert_numbers: Optional[str] = Field(default=None, description="Numeral conversion: None, 'to_khmer', or 'to_arabic'")
    context: Optional[str] = Field(default=None, description="Additional context or domain knowledge")


class TranslateResponse(BaseModel):
    translated_text: str
    source_lang: str
    target_lang: str
    engine_used: str
    tone: str
    characters_count: int


class KhmerToolsRequest(BaseModel):
    text: str = Field(..., description="Khmer text to process")
    action: str = Field(..., description="Action: 'normalize', 'to_khmer_num', 'to_arabic_num', 'add_zwsp', 'strip_zwsp'")


class KhmerToolsResponse(BaseModel):
    result: str
    action: str


class LanguageInfo(BaseModel):
    code: str
    name: str
    native_name: str


class SystemStatusResponse(BaseModel):
    status: str
    engines: Dict[str, bool]
    default_engine: str
