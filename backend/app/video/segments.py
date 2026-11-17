"""Dialogue completeness checks shared by translation, speech, and rendering."""

from pathlib import Path


def valid_audio(segment):
    try:
        path = Path(segment.get("audio_file") or "")
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


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
        (not (seg.get("translation") or "").strip() or seg.get("translation_error"))
    ]


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
