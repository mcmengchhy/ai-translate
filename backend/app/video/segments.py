"""Dialogue completeness checks shared by translation, speech, and rendering."""

from pathlib import Path


def has_spoken_text(text):
    """Punctuation and placeholder ellipses cannot be synthesized as dialogue."""
    return any(character.isalnum() for character in (text or ""))


def valid_audio(segment):
    try:
        path = Path(segment.get("audio_file") or "")
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


class MissingVoicesError(RuntimeError):
    def __init__(self, segments):
        self.cue_ids = [seg.get("id") for seg in segments]
        super().__init__(
            f"Khmer voice is missing for {len(segments)} cue(s): " +
            ", ".join(str(key) for key in self.cue_ids[:10]) +
            ". Saved Khmer voices and translations are kept. Retry failed Khmer voices before rendering; original speech will not be substituted."
        )


def require_renderable_segments(segments):
    unresolved = [seg for seg in missing_translations(segments)
                  if seg.get("audio_source") != "original_fallback" or not valid_audio(seg)]
    if unresolved:
        raise MissingTranslationsError(unresolved)
    silent = [seg for seg in segments if (seg.get("text") or seg.get("translation") or "").strip() and not valid_audio(seg)]
    if silent:
        raise RuntimeError("No generated or original speech is available for cue(s): " +
                           ", ".join(str(seg.get("id")) for seg in silent[:10]))


def missing_translations(segments):
    return [
        seg for seg in segments
        if (seg.get("text") or "").strip() and
        (not has_spoken_text(seg.get("translation")) or seg.get("translation_error"))
    ]


def translation_summary(segments):
    """Compact saved-cue counts for cards without transmitting dialogue."""
    cues = [seg for seg in segments if isinstance(seg, dict) and (seg.get("text") or "").strip()]
    missing = missing_translations(cues)
    return {"total_cues": len(cues), "translated_cues": len(cues) - len(missing),
            "missing_cues": len(missing),
            "missing_cue_ids": [seg["id"] for seg in missing if seg.get("id") is not None][:10]}


def voice_summary(segments):
    cues = [seg for seg in segments if isinstance(seg, dict) and (seg.get("text") or seg.get("translation") or "").strip()]
    missing = [seg for seg in cues if not valid_audio(seg) or seg.get("tts_error") or
               seg.get("audio_source") == "original_fallback" or seg.get("_dirty")]
    return {"voice_cues": len(cues), "ready_voices": len(cues) - len(missing), "missing_voices": len(missing),
            "missing_voice_ids": [seg["id"] for seg in missing if seg.get("id") is not None][:10]}


class MissingTranslationsError(RuntimeError):
    def __init__(self, segments):
        self.cue_ids = [seg.get("id") for seg in segments]
        sample = ", ".join(str(cue_id) for cue_id in self.cue_ids[:10])
        super().__init__(
            f"Khmer translation is missing for {len(segments)} cue(s): {sample}. "
            "Use Translate Missing to retry before rendering."
        )


def require_translations(segments):
    missing = missing_translations(segments)
    if missing:
        raise MissingTranslationsError(missing)
