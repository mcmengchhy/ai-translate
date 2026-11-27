"""Numbered dialogue exchange for human-reviewed translations, without a provider."""

import hashlib
import json
import re
import uuid

from .checkpoints import ACTIVE_STATUSES, mark_step
from .segments import missing_translations, has_spoken_text

MAX_BATCH_CHARACTERS = 3500
_NUMBERED_LINE = re.compile(r"^\s*(?:\[\s*(\d+)\s*\]|【\s*(\d+)\s*】|(\d+)[.)។៖:：、])\s*(.*)$")


def source_rows(job):
    rows = [seg for seg in job.get("segments", []) if (seg.get("text") or "").strip()]
    ids = [seg.get("id") for seg in rows]
    if any(type(key) is not int or key < 0 for key in ids) or len(ids) != len(set(ids)):
        raise ValueError("Dialogue cue numbers must be unique. Re-extract the transcript before exporting.")
    return rows


def source_fingerprint(job):
    source = [{key: seg.get(key) for key in ("id", "start", "end", "text")} for seg in source_rows(job)]
    return hashlib.sha256(json.dumps(source, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def numbered_text(rows, field="text"):
    return "\n".join(f"[{seg['id']}] {' '.join((seg.get(field) or '').split())}" for seg in rows)


def dialogue_batches(job):
    batches, current, size = [], [], 0
    for seg in source_rows(job):
        length = len(numbered_text([seg])) + 1
        if current and size + length > MAX_BATCH_CHARACTERS:
            batches.append(current)
            current, size = [], 0
        current.append(seg)
        size += length
    if current:
        batches.append(current)
    return batches


def batch_rows(job, index):
    batches = dialogue_batches(job)
    if not batches:
        raise ValueError("No extracted dialogue yet. Extract speech and subtitles first.")
    if index < 0 or index >= len(batches):
        raise ValueError("This text batch no longer exists. Reload the processing details.")
    return batches[index]


def translation_details(job, batch=0):
    batches = dialogue_batches(job)
    rows = batch_rows(job, batch) if batches else []
    missing = missing_translations(job.get("segments", []))
    active = job.get("status") in ACTIVE_STATUSES
    return {
        "job_id": job["id"], "title": job.get("title"), "status": job.get("status"),
        "stage": job.get("stage"), "error": job.get("error"),
        "manual_translation": bool(job.get("manual_translation") or job.get("request_options", {}).get("manual_translation")),
        "source_language": job.get("detected_language", "auto"),
        "source_fingerprint": source_fingerprint(job), "batch": batch, "batch_count": len(batches),
        "total_cues": len(source_rows(job)), "remaining_cues": len(missing),
        "source_text": numbered_text(rows), "translation_text": numbered_text(rows, "translation"),
        "cues": [{key: seg.get(key) for key in ("id", "start", "end", "text", "translation")} for seg in rows],
        "can_edit": not active, "can_generate": bool(rows) and not active and not missing,
    }


def missing_translation_details(job):
    rows = missing_translations(source_rows(job))
    fingerprint = source_fingerprint(job)
    state = {"source": fingerprint, "missing": [
        {key: seg.get(key) for key in ("id", "translation", "translation_error")} for seg in rows
    ]}
    missing_fingerprint = hashlib.sha256(json.dumps(state, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return {"job_id": job["id"], "source_fingerprint": fingerprint, "missing_fingerprint": missing_fingerprint,
            "remaining_cues": len(rows), "source_text": numbered_text(rows),
            "cues": [{key: seg.get(key) for key in ("id", "start", "end", "text", "translation")} for seg in rows],
            "can_edit": job.get("status") not in ACTIVE_STATUSES}


def require_idle(job, running_jobs):
    if job.get("status") in ACTIVE_STATUSES or job["id"] in running_jobs:
        raise RuntimeError("Pause this task before changing or importing manual translations.")


def wait_for_manual_translation(job):
    job["manual_translation"] = True
    job.setdefault("request_options", {})["manual_translation"] = True
    missing = missing_translations(job.get("segments", []))
    total = len(source_rows(job))
    job.update(status="awaiting_translation", error=None,
               stage="Waiting for your Khmer translation" if missing else "Translations ready — generate voices and video",
               progress=50 + int(20 * (total - len(missing)) / max(1, total)),
               pipeline_phase="transcribed" if missing else "translated")
    mark_step(job, "translation", "partial" if missing else "complete")


def parse_numbered_translation(text):
    # Google may join multiple cues into one paragraph. Bracketed numbers,
    # rather than line breaks, are the boundaries; never infer a missing ID.
    text = re.sub(r"(?=(?:\[\s*\d+\s*\]|【\s*\d+\s*】))", "\n", text.lstrip("\ufeff"))
    translated, current = {}, None
    for line in text.splitlines():
        if not line.strip():
            continue
        match = _NUMBERED_LINE.match(line)
        if match:
            current = int(next(value for value in match.groups()[:3] if value is not None))
            if current in translated:
                raise ValueError(f"Cue [{current}] appears more than once. Keep each number once.")
            translated[current] = match.group(4).strip()
        elif current is None:
            raise ValueError("Keep a cue number such as [1] before each translated sentence.")
        else:
            translated[current] += " " + line.strip()
    for key, value in translated.items():
        if not value.strip():
            raise ValueError(f"Cue [{key}] has no translation. No changes were saved.")
        if not has_spoken_text(value):
            raise ValueError(f"Cue [{key}] contains only punctuation. Paste Khmer words instead of '...'. No changes were saved.")
        if re.search(r"[\u3400-\u9fff]", value):
            raise ValueError(f"Cue [{key}] still contains Chinese text. Paste the Khmer translation instead.")
    return translated


def _check_cue_numbers(rows, translated):
    expected = {seg["id"] for seg in rows}
    if set(translated) != expected:
        missing, extra = sorted(expected - translated.keys()), sorted(translated.keys() - expected)
        detail = []
        if missing:
            detail.append("missing " + ", ".join(f"[{key}]" for key in missing[:10]))
        if extra:
            detail.append("unexpected " + ", ".join(f"[{key}]" for key in extra[:10]))
        raise ValueError("Cue numbers do not match this batch (" + "; ".join(detail) + "). No changes were saved.")


def _save_translated_rows(job, rows, translated):
    changed = False
    for seg in rows:
        value = translated[seg["id"]].strip()
        if value != seg.get("translation") or seg.get("translation_error") or seg.get("audio_source") == "original_fallback":
            changed = True
            seg.update(translation=value, translation_source="manual", audio_file=None, _dirty=True)
            for key in ("audio_source", "fallback_subtitle", "generation_warning", "review_required", "tts_error"):
                seg.pop(key, None)
        for key in ("translation_error", "translation_warning"):
            seg.pop(key, None)
    if changed:
        job.update(needs_rerender=True, result=None,
                   output_filename=f"dubbed_{job['id']}_manual_{uuid.uuid4().hex[:8]}.mp4")
        mark_step(job, "voices", "pending")
        mark_step(job, "render", "pending")
    wait_for_manual_translation(job)


def apply_translation(job, batch, text, fingerprint):
    if fingerprint != source_fingerprint(job):
        raise RuntimeError("The original dialogue changed. Reload and copy the updated text before importing.")
    rows = batch_rows(job, batch)
    translated = parse_numbered_translation(text)
    _check_cue_numbers(rows, translated)
    _save_translated_rows(job, rows, translated)
    return translation_details(job, batch)


def apply_missing_translation(job, text, fingerprint, missing_fingerprint):
    current = missing_translation_details(job)
    if fingerprint != current["source_fingerprint"] or missing_fingerprint != current["missing_fingerprint"]:
        raise RuntimeError("The missing cues changed. Refresh the board and review your pasted text before updating.")
    rows = missing_translations(source_rows(job))
    if not rows:
        raise RuntimeError("All missing translations have already been repaired. Refresh the board.")
    translated = parse_numbered_translation(text)
    _check_cue_numbers(rows, translated)
    _save_translated_rows(job, rows, translated)
    return {**missing_translation_details(job), "updated_cue_ids": [seg["id"] for seg in rows]}
