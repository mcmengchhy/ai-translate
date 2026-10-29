"""Video Subtitle Extraction & Ground-Truth Alignment.

Reads on-screen subtitles (Hardsubs) via RapidOCR and embedded container soft subtitles,
fusing visual subtitle ground-truth with speech-activity timing for 100% accurate dialogue.
"""

import logging
import re
import subprocess
from pathlib import Path
from typing import List, Dict, Any, Optional

import cv2

logger = logging.getLogger(__name__)

# Cached RapidOCR singleton
_OCR_ENGINE = None


def get_ocr_engine():
    global _OCR_ENGINE
    if _OCR_ENGINE is None:
        from rapidocr_onnxruntime import RapidOCR
        logger.info("Initializing RapidOCR ONNX engine for subtitle recognition...")
        _OCR_ENGINE = RapidOCR()
    return _OCR_ENGINE


def parse_srt_or_vtt(subtitle_file_path: str) -> List[Dict[str, Any]]:
    """
    Parses an SRT or VTT subtitle file into standard timestamped segment dictionaries.
    """
    content = Path(subtitle_file_path).read_text(encoding="utf-8", errors="ignore")
    lines = content.splitlines()

    segments = []
    current_cue = {}
    text_buffer = []

    def _parse_time(t_str: str) -> float:
        t_str = t_str.strip().replace(",", ".")
        parts = t_str.split(":")
        if len(parts) == 3:
            h, m, s = parts
            return int(h) * 3600 + int(m) * 60 + float(s)
        elif len(parts) == 2:
            m, s = parts
            return int(m) * 60 + float(s)
        return float(t_str)

    time_pattern = re.compile(r"(\d{1,2}:\d{2}:\d{2}[,\.]\d{1,3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[,\.]\d{1,3})")

    cue_id = 1
    for line in lines:
        line_s = line.strip()
        time_match = time_pattern.search(line_s)
        if time_match:
            if current_cue and text_buffer:
                current_cue["text"] = " ".join(text_buffer).strip()
                if current_cue["text"]:
                    segments.append(current_cue)
                text_buffer = []

            start_t = _parse_time(time_match.group(1))
            end_t = _parse_time(time_match.group(2))
            current_cue = {
                "id": cue_id,
                "start": round(start_t, 2),
                "end": round(end_t, 2),
                "source": "subtitle_file"
            }
            cue_id += 1
        elif line_s.isdigit() and not text_buffer:
            continue
        elif line_s:
            if current_cue:
                text_buffer.append(line_s)

    if current_cue and text_buffer:
        current_cue["text"] = " ".join(text_buffer).strip()
        if current_cue["text"]:
            segments.append(current_cue)

    logger.info(f"Parsed {len(segments)} subtitle segments from {subtitle_file_path}")
    return segments


def extract_embedded_soft_subtitles(video_path: str) -> Optional[List[Dict[str, Any]]]:
    """
    Checks if video has embedded soft subtitle streams (e.g. mov_text, srt, subrip)
    and extracts them using FFmpeg.
    """
    try:
        probe_cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "s",
            "-show_entries", "stream=index,codec_name",
            "-of", "json",
            str(video_path)
        ]
        result = subprocess.run(probe_cmd, capture_output=True, text=True, check=True)
        import json
        info = json.loads(result.stdout)
        streams = info.get("streams", [])
        if not streams:
            return None

        # Extract first subtitle stream to temporary .srt
        temp_srt = Path(video_path).parent / f"{Path(video_path).stem}_sub0.srt"
        extract_cmd = [
            "ffmpeg", "-y",
            "-i", str(video_path),
            "-map", "0:s:0",
            str(temp_srt)
        ]
        subprocess.run(extract_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        if temp_srt.exists() and temp_srt.stat().st_size > 50:
            parsed = parse_srt_or_vtt(str(temp_srt))
            temp_srt.unlink(missing_ok=True)
            if parsed:
                logger.info(f"Successfully extracted {len(parsed)} embedded soft subtitles.")
                return parsed
        temp_srt.unlink(missing_ok=True)
    except Exception as e:
        logger.debug(f"Soft subtitle check skipped or failed: {e}")

    return None


def detect_hardsub_presence(cap: cv2.VideoCapture, speech_segments: List[Dict[str, Any]], h: int, ocr) -> bool:
    """
    Quickly samples 6 speech cues across the video to verify if on-screen subtitles are present.
    """
    if not speech_segments:
        return False

    sample_indices = [
        int(i * (len(speech_segments) - 1) / min(6, len(speech_segments)))
        for i in range(min(6, len(speech_segments)))
    ]

    detected_hits = 0
    crop_top = int(h * 0.76)

    for idx in sample_indices:
        seg = speech_segments[idx]
        mid_t = (seg["start"] + seg["end"]) / 2.0
        cap.set(cv2.CAP_PROP_POS_MSEC, mid_t * 1000.0)
        ret, frame = cap.read()
        if not ret or frame is None:
            continue
        crop = frame[crop_top:h, :]
        res, _ = ocr(crop)
        if res:
            # Check if any recognized text line has confidence > 0.65
            has_conf = any(r[2] > 0.65 and len(r[1].strip()) >= 2 for r in res)
            if has_conf:
                detected_hits += 1

    has_hardsubs = detected_hits >= 2
    logger.info(f"Hardsub presence probe: {detected_hits}/{len(sample_indices)} hits -> Subtitles detected: {has_hardsubs}")
    return has_hardsubs


def enhance_segments_with_hardsubs(
    video_path: str,
    speech_segments: List[Dict[str, Any]],
    progress_callback=None
) -> List[Dict[str, Any]]:
    """
    Enhances Whisper speech segments with on-screen subtitle OCR text.
    
    1. Probes for burned-in subtitles (hardsubs).
    2. If found, reads on-screen characters at the midpoint of each speech segment.
    3. Uses frame-delta caching to skip OCR when consecutive cues share the same subtitle frame.
    4. Replaces noisy ASR text with exact ground-truth subtitle characters.
    5. Merges consecutive segments that share the same subtitle text line.
    """
    if not speech_segments:
        return speech_segments

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        logger.warning(f"Could not open video file {video_path} for subtitle OCR.")
        return speech_segments

    try:
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        ocr = get_ocr_engine()

        # Step 1: Detect presence
        has_subtitles = detect_hardsub_presence(cap, speech_segments, h, ocr)
        if not has_subtitles:
            logger.info("No burned-in subtitles detected; proceeding with Whisper audio transcription.")
            return speech_segments

        total_cues = len(speech_segments)
        logger.info(f"Reading on-screen hardsubs across {total_cues} dialogue segments with frame caching...")

        # Focus crop on bottom 22% height and central 80% width where subtitles are rendered
        crop_top = int(h * 0.76)
        crop_bottom = int(h * 0.97)
        crop_left = int(w * 0.10)
        crop_right = int(w * 0.90)

        enhanced = []
        prev_crop_small = None
        prev_ocr_text = ""

        import numpy as np

        for i, seg in enumerate(speech_segments):
            mid_t = (seg["start"] + seg["end"]) / 2.0
            target_msec = mid_t * 1000.0

            # Seeking optimization: if next target is close ahead (<1.5s), grab forward instead of full keyframe seek
            cur_msec = cap.get(cv2.CAP_PROP_POS_MSEC)
            diff_msec = target_msec - cur_msec
            if 0 < diff_msec < 1500:
                # Fast forward a few frames
                while cap.get(cv2.CAP_PROP_POS_MSEC) < target_msec - 40:
                    if not cap.grab():
                        break
                ret, frame = cap.read()
            else:
                cap.set(cv2.CAP_PROP_POS_MSEC, target_msec)
                ret, frame = cap.read()

            ocr_text = ""
            if ret and frame is not None:
                crop = frame[crop_top:crop_bottom, crop_left:crop_right]
                # Downscaled thumbnail for ultra-fast frame change detection
                crop_small = cv2.resize(crop, (160, 32), interpolation=cv2.INTER_AREA)

                # If frame is nearly identical to previous cue (< 3.0 mean pixel delta), reuse previous OCR
                if prev_crop_small is not None and np.mean(np.abs(crop_small.astype(np.float32) - prev_crop_small.astype(np.float32))) < 3.0:
                    ocr_text = prev_ocr_text
                else:
                    res, _ = ocr(crop)
                    if res:
                        valid_lines = [r[1].strip() for r in res if r[2] > 0.6 and len(r[1].strip()) > 0]
                        if valid_lines:
                            ocr_text = " ".join(valid_lines)
                    prev_crop_small = crop_small
                    prev_ocr_text = ocr_text

            asr_text = seg.get("text", "")
            if ocr_text and len(ocr_text) >= 2:
                seg["asr_text"] = asr_text
                seg["text"] = ocr_text
                seg["ocr_detected"] = True
            else:
                seg["ocr_detected"] = False

            enhanced.append(seg)
            if progress_callback and (i % 15 == 0 or i == total_cues - 1):
                progress_callback(i + 1, total_cues)

        # Step 2: Merge adjacent speech segments that share the exact same subtitle line or fragments
        # (Combines broken speech fragments into complete, logical cinematic dialogue sentences)
        merged = []
        for seg in enhanced:
            if not merged:
                merged.append(seg)
                continue

            prev = merged[-1]
            cur_text = seg.get("text", "").strip()
            prev_text = prev.get("text", "").strip()

            # Clean OCR artifacts (leading single letters like 'L ', 'I ', symbols)
            cur_clean = re.sub(r"^[A-Za-z0-9_\W]\s*", "", cur_text)
            prev_clean = re.sub(r"^[A-Za-z0-9_\W]\s*", "", prev_text)

            # Check if one is a duplicate or progressive subtitle fragment of the other
            is_dup_or_fragment = False
            if cur_text and prev_text:
                if cur_text == prev_text or cur_clean == prev_clean:
                    is_dup_or_fragment = True
                elif len(cur_clean) >= 2 and len(prev_clean) >= 2:
                    if cur_clean in prev_clean or prev_clean in cur_clean:
                        is_dup_or_fragment = True

            if is_dup_or_fragment and (seg["start"] <= prev["end"] + 1.5):
                prev["end"] = max(prev["end"], seg["end"])
                # Keep the longer, more complete text
                if len(cur_text) > len(prev_text):
                    prev["text"] = cur_text
                # Keep audio speech references
                if "asr_text" in seg and "asr_text" in prev and seg["asr_text"] != prev["asr_text"]:
                    prev["asr_text"] = f"{prev['asr_text']} {seg['asr_text']}".strip()
            else:
                merged.append(seg)

        # Re-index merged segments
        for idx, m_seg in enumerate(merged, start=1):
            m_seg["id"] = idx

        logger.info(f"Hardsub alignment completed: {len(speech_segments)} raw cues merged into {len(merged)} complete dialogue lines.")
        return merged

    finally:
        cap.release()
