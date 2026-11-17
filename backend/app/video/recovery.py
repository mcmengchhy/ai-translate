"""Cue-scoped retries with an explicit original-speech fallback for final exports."""

import asyncio
import logging
import subprocess
from pathlib import Path

from .segments import missing_translations, valid_audio, require_renderable_segments

logger = logging.getLogger(__name__)


def extract_original_speech(video_path, segment, output_path):
    start = max(0, float(segment.get("target_start", segment.get("start", 0))))
    end = float(segment.get("target_end", segment.get("end", start)))
    if end <= start:
        raise ValueError("Original dialogue has no usable time range")
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-ss", str(start), "-i", str(video_path),
        "-t", str(end-start), "-map", "0:a:0", "-vn", "-c:a", "libmp3lame", "-b:a", "128k",
        str(output_path),
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=45)
    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise RuntimeError("Original speech extraction produced no audio")
    return str(output_path)


async def prepare_dialogue_audio(
    segments, video_path, output_dir, source_lang="auto", engine_name="auto", api_key=None,
    model=None, base_url=None, tone="casual", voice_type="male", rate="auto", pitch="auto",
    volume="auto", on_progress=None, synthesize=None, translate=None, retry_rounds=2,
    translation_recovery_timeout=180,
):
    """Keep successful work, repair failures while other voices run, then fill remaining audio.

    A persistent provider failure uses the corresponding original speech, never
    an invented Khmer line or silence. Those cues remain explicitly reviewable.
    """
    if synthesize is None:
        from .tts import synthesize_segments
        synthesize = synthesize_segments
    if translate is None:
        from ..engines.dialogue_translator import translate_dialogue_segments
        translate = translate_dialogue_segments
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    spoken = [seg for seg in segments if (seg.get("text") or seg.get("translation") or "").strip()]
    missing = missing_translations(spoken)
    for seg in missing:
        if seg.get("audio_source") != "original_fallback":
            seg["audio_file"] = None
    missing_ids = {id(seg) for seg in missing}
    healthy = [seg for seg in spoken if id(seg) not in missing_ids]
    repair_slots = asyncio.Semaphore(2)

    def progress(stage):
        if on_progress:
            on_progress(stage, sum(valid_audio(seg) for seg in spoken), len(spoken))

    async def repair(seg):
        previous = seg.get("translation")
        for attempt in range(retry_rounds):
            seg["translation_retry_round"] = attempt + 1
            progress(f"Retrying translation for cue {seg['id']} ({attempt+1}/{retry_rounds})")
            try:
                async with repair_slots:
                    await asyncio.wait_for(translate(
                        [seg], source_lang, engine_name=engine_name, api_key=api_key,
                        model=model, base_url=base_url, tone=tone,
                    ), timeout=90)
            except Exception as error:
                logger.warning("Cue %s translation recovery %s failed: %s", seg.get("id"), attempt+1, error)
            if not missing_translations([seg]):
                if seg.get("translation") != previous or seg.get("audio_source") == "original_fallback":
                    seg.update(audio_file=None, _dirty=True)
                    for key in ("audio_source", "fallback_subtitle", "generation_warning", "review_required"):
                        seg.pop(key, None)
                progress(f"Recovered translation for cue {seg['id']}")
                return
            if attempt + 1 < retry_rounds:
                await asyncio.sleep(2 * (attempt+1))

    async def voices(group):
        pending = []
        for seg in group:
            requested = {"rate": seg.get("rate") or rate, "pitch": seg.get("pitch") or pitch,
                         "volume": seg.get("volume") or volume}
            changed = any(value != "auto" and value != seg.get("prosody", {}).get(knob)
                          for knob, value in requested.items())
            if not valid_audio(seg) or seg.get("_dirty") or changed:
                seg["audio_file"] = None
                pending.append(seg)
        for attempt in range(retry_rounds):
            if not pending:
                return
            progress(f"{'Generating' if attempt == 0 else 'Retrying'} {len(pending)} voice clips")
            for seg in pending:
                seg["voice_retry_round"] = attempt+1
            try:
                await synthesize(
                    pending, output_dir, voice_type=voice_type, rate=rate, pitch=pitch, volume=volume,
                    max_concurrency=4, enhance=True,
                    on_progress=lambda done, total: progress("Generating Khmer voices"),
                )
            except Exception as error:
                logger.warning("Voice recovery pass %s failed: %s", attempt+1, error)
            for seg in pending:
                if valid_audio(seg):
                    seg.pop("_dirty", None)
                    seg.pop("tts_error", None)
                    seg["audio_source"] = "khmer_tts"
                    for key in ("generation_warning", "review_required", "fallback_subtitle"):
                        seg.pop(key, None)
            pending = [seg for seg in pending if not valid_audio(seg)]
            progress(f"Voice pass {attempt+1} saved")
            if pending and attempt + 1 < retry_rounds:
                await asyncio.sleep(2 * (attempt+1))

    # Successful cues keep advancing while failed cues retry independently.
    async def repair_queue():
        try:
            async with asyncio.timeout(translation_recovery_timeout):
                await asyncio.gather(*(repair(seg) for seg in missing))
        except TimeoutError:
            logger.warning("Translation recovery time limit reached; preserving original speech for unresolved cues")
            progress("Translation retry limit reached; preparing original speech for unresolved cues")

    await asyncio.gather(voices(healthy), repair_queue())
    repaired = [seg for seg in missing if not missing_translations([seg])]
    await voices(repaired)

    fallback_slots = asyncio.Semaphore(2)
    async def original_fallback(seg):
        if valid_audio(seg) and seg.get("audio_source") == "original_fallback":
            return
        reason = seg.get("translation_error") or seg.get("tts_error") or "Khmer voice service did not produce audio"
        progress(f"Preserving original speech for cue {seg['id']}")
        try:
            async with fallback_slots:
                audio_file = await asyncio.to_thread(
                    extract_original_speech, video_path, seg, output_dir / f"source_fallback_{seg['id']}.mp3",
                )
            seg.update(audio_file=audio_file, audio_source="original_fallback", review_required=True,
                       fallback_subtitle=seg.get("english_intent") or seg.get("text") or "",
                       generation_warning=f"Original speech used after Khmer generation retries failed. Review this cue. {reason}")
            seg.pop("_dirty", None)
        except Exception as error:
            seg["tts_error"] = f"Could not recover original speech: {error}"
            logger.error("Original speech fallback failed for cue %s: %s", seg.get("id"), error)
        progress(f"Saved audio for cue {seg['id']}")

    await asyncio.gather(*(original_fallback(seg) for seg in spoken if not valid_audio(seg)))
    require_renderable_segments(segments)
    fallbacks = [seg for seg in spoken if seg.get("audio_source") == "original_fallback"]
    return {"fallback_count": len(fallbacks), "fallback_cue_ids": [seg["id"] for seg in fallbacks],
            "generation_warnings": [{"cue_id": seg["id"], "message": seg["generation_warning"]} for seg in fallbacks]}
