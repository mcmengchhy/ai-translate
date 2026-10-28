"""Pydantic request and response models."""

from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field


class TranslateRequest(BaseModel):
    text: str = Field(..., description="Text to be translated")
    source_lang: str = Field(default="auto", description="Source language code (e.g. 'en', 'km', 'auto')")
    target_lang: str = Field(default="km", description="Target language code (e.g. 'km', 'en')")
    engine: str = Field(default="auto", description="Translation engine: 'auto', 'gemini', 'openai', or 'free'")
    tone: str = Field(default="standard", description="Translation tone: standard, formal, polite, casual, literary")
    inject_zwsp: bool = Field(default=False, description="Insert Zero-Width Space between Khmer words/syllables")
    convert_numbers: Optional[str] = Field(default=None, description="Numeral conversion: None, 'to_khmer', or 'to_arabic'")
    api_key: Optional[str] = Field(default=None, description="Optional per-request API key for Gemini / OpenAI")
    model: Optional[str] = Field(default=None, description="Optional custom model identifier")
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
