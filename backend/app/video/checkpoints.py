"""Ordered processing checkpoints, including migration of existing saved jobs."""

STEPS = (
    ("prepare", "Prepare video"),
    ("transcription", "Transcription"),
    ("translation", "Khmer translation"),
    ("voices", "Khmer voices"),
    ("render", "Final video"),
)
ACTIVE_STATUSES = {"queued", "processing", "running", "rendering", "pausing"}


def step_states(job):
    saved = job.get("step_states")
    if isinstance(saved, dict):
        return {key: saved.get(key, "pending") for key, _ in STEPS}
    states = {key: "pending" for key, _ in STEPS}
    phase = job.get("pipeline_phase")
    segments = [seg for seg in job.get("segments", []) if isinstance(seg, dict)]
    stage = (job.get("interrupted_stage") or job.get("stage") or "").lower()
    if job.get("video_path"):
        states["prepare"] = "complete"
    if segments or phase in {"transcribed", "translated", "voices_ready", "rendering", "completed"}:
        states["transcription"] = "complete"
    elif "transcrib" in stage or "hardsub" in stage or "subtitle worker" in stage:
        states["transcription"] = "partial"
    if any(seg.get("translation") or seg.get("translation_error") for seg in segments) or "→ khmer" in stage:
        states["translation"] = "partial"
    if phase in {"translated", "voices_ready", "rendering", "completed"}:
        states["translation"] = "complete"
    if any(seg.get("audio_file") or seg.get("tts_error") for seg in segments) or "voice" in stage:
        states["voices"] = "partial"
    if phase in {"voices_ready", "rendering", "completed"}:
        states["voices"] = "complete"
    if phase == "rendering" or "assembling" in stage or "rendering video" in stage:
        states["render"] = "partial"
    if job.get("result") or phase == "completed":
        states["render"] = "complete"
    # An existing later checkpoint implies that its prerequisites were reached.
    highest = max((i for i, (key, _) in enumerate(STEPS) if states[key] != "pending"), default=-1)
    for key, _ in STEPS[:highest]:
        if states[key] == "pending":
            states[key] = "complete"
    return states


def mark_step(job, step, state):
    states = step_states(job)
    states[step] = state
    job["step_states"] = states


def job_steps(job):
    states = step_states(job)
    latest = next((key for key, _ in reversed(STEPS) if states[key] != "pending"), None)
    idle = job.get("status") not in ACTIVE_STATUSES
    return [{"id": key, "number": index, "label": label, "state": states[key],
             "can_clear": idle and key == latest}
            for index, (key, label) in enumerate(STEPS, 1)]


def source_segments(segments):
    """Retain original dialogue and timing while discarding generated outputs."""
    exact = {"audio_file", "audio_source", "prosody", "_dirty", "fallback_subtitle", "review_required",
             "generation_warning", "target_start", "target_end", "original_start", "original_end"}
    prefixes = ("translation", "english_", "tts_", "voice_retry")
    return [{key: value for key, value in seg.items() if key not in exact and not key.startswith(prefixes)}
            for seg in segments if isinstance(seg, dict)]
