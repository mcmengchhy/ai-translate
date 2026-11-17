"""Validated rendering preferences shared by upload, URL, and studio jobs."""

import re
from pathlib import Path
from pydantic import BaseModel, Field, field_validator, model_validator

ROOT_DIR = Path(__file__).resolve().parents[3]
LOGO_DIR = ROOT_DIR / "storage" / "logos"
BUILTIN_LOGOS = {
    "cinekhmer": ROOT_DIR / "youtube-concept" / "logo_cinekhmer_ai.png",
    "khmerdub": ROOT_DIR / "youtube-concept" / "logo_khmerdub_ai.png",
}


def resolve_logo(logo_id: str) -> Path:
    if logo_id in BUILTIN_LOGOS:
        return BUILTIN_LOGOS[logo_id]
    if re.fullmatch(r"custom_[a-f0-9]{32}", logo_id):
        for extension in ("png", "jpg", "webp"):
            candidate = LOGO_DIR / f"{logo_id}.{extension}"
            if candidate.is_file():
                return candidate
    raise ValueError("Logo not found. Select a built-in logo or upload your logo again.")


class VideoRenderOptions(BaseModel):
    add_logo: bool = False
    logo_id: str = "cinekhmer"
    blur_chinese_subtitles: bool = False
    blur_x: float = Field(default=0.10, ge=0, lt=1, allow_inf_nan=False)
    blur_y: float = Field(default=0.80, ge=0, lt=1, allow_inf_nan=False)
    blur_width: float = Field(default=0.80, gt=0, le=1, allow_inf_nan=False)
    blur_height: float = Field(default=0.14, gt=0, le=1, allow_inf_nan=False)
    burn_subtitles: bool = False

    @field_validator("logo_id")
    @classmethod
    def validate_logo_id(cls, value):
        if value not in BUILTIN_LOGOS and not re.fullmatch(r"custom_[a-f0-9]{32}", value):
            raise ValueError("Invalid logo identifier")
        return value

    @model_validator(mode="after")
    def validate_region(self):
        if self.blur_x + self.blur_width > 1.000001 or self.blur_y + self.blur_height > 1.000001:
            raise ValueError("The subtitle blur area must fit inside the video frame")
        if self.add_logo:
            resolve_logo(self.logo_id)
        return self

    def render_values(self):
        return {name: getattr(self, name) for name in VideoRenderOptions.model_fields}
