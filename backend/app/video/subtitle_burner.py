"""Khmer Subtitle Rendering & Chinese Hardsub Concealment Engine.

Renders cinema-grade Khmer Unicode subtitles using Pillow with native Khmer fonts,
and applies FFmpeg masking/video filters to completely hide Chinese hardsubs and
mitigate automated copyright fingerprinting.
"""

import json
import logging
import re
import subprocess
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

# Preferred Khmer fonts ordered by visual clarity & typography
KHMER_FONT_CANDIDATES = [
    "/Library/Fonts/Battambang-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Khmer Sangam MN.ttf",
    "/System/Library/Fonts/Khmer Sangam MN.ttf",
    "/Library/Fonts/NotoSansKhmer-Regular.ttf",
]


def get_preferred_khmer_font(size: int = 42) -> ImageFont.FreeTypeFont:
    """Finds an installed Khmer font on the operating system."""
    for cand in KHMER_FONT_CANDIDATES:
        p = Path(cand)
        if p.exists():
            try:
                return ImageFont.truetype(str(p), size)
            except Exception:
                continue
    return ImageFont.load_default()


def get_video_dimensions(video_path: str) -> Tuple[int, int]:
    """Uses ffprobe to obtain exact video width and height."""
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=p=0:s=x",
        str(video_path)
    ]
    try:
        out = subprocess.check_output(cmd).decode().strip()
        parts = out.split("x")
        if len(parts) == 2:
            return int(parts[0]), int(parts[1])
    except Exception as e:
        logger.warning(f"Failed to probe video dimensions for {video_path}: {e}")
    return 1920, 1080


def wrap_khmer_text(text: str, max_chars_per_line: int = 50) -> List[str]:
    """
    Wraps Khmer text lines intelligently so dialogue fits the subtitle box.
    """
    clean = text.strip()
    if len(clean) <= max_chars_per_line:
        return [clean]

    # Try splitting on spaces first
    words = clean.split(" ")
    if len(words) > 1:
        lines = []
        cur_line = []
        cur_len = 0
        for w in words:
            if cur_len + len(w) + 1 > max_chars_per_line and cur_line:
                lines.append(" ".join(cur_line))
                cur_line = [w]
                cur_len = len(w)
            else:
                cur_line.append(w)
                cur_len += len(w) + 1
        if cur_line:
            lines.append(" ".join(cur_line))
        return lines

    # Khmer doesn't always use spaces, split at roughly mid-point
    mid = len(clean) // 2
    return [clean[:mid], clean[mid:]]


def render_khmer_subtitle_frame(
    text: str,
    width: int,
    height: int,
    output_path: Path,
    mask_box_y: int,
    mask_box_h: int,
    font_size: Optional[int] = None,
) -> Path:
    """
    Renders Khmer text onto a transparent RGBA image centered in the mask box.
    """
    if font_size is None:
        # Scale font proportionally with video height (e.g. 40px for 1080p, 26px for 720p)
        font_size = max(24, int(height * 0.038))

    font = get_preferred_khmer_font(font_size)
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    lines = wrap_khmer_text(text, max_chars_per_line=int(width / (font_size * 0.65)))
    
    # Calculate total height of text block
    line_bboxes = [draw.textbbox((0, 0), line, font=font) for line in lines]
    line_heights = [bb[3] - bb[1] for bb in line_bboxes]
    line_widths = [bb[2] - bb[0] for bb in line_bboxes]
    
    spacing = int(font_size * 0.25)
    total_text_h = sum(line_heights) + spacing * (len(lines) - 1)

    # Center vertically inside the mask box
    y_start = mask_box_y + max(4, (mask_box_h - total_text_h) // 2)

    cur_y = y_start
    for i, line in enumerate(lines):
        lw = line_widths[i]
        x_pos = (width - lw) // 2

        # 2px dark outline for cinema contrast
        for dx in [-2, -1, 0, 1, 2]:
            for dy in [-2, -1, 0, 1, 2]:
                if dx != 0 or dy != 0:
                    draw.text((x_pos + dx, cur_y + dy), line, font=font, fill=(0, 0, 0, 255))

        # Golden yellow fill (#FFDE59) - high visibility
        draw.text((x_pos, cur_y), line, font=font, fill=(255, 222, 89, 255))
        cur_y += line_heights[i] + spacing

    img.save(str(output_path), "PNG")
    return output_path


def build_subtitle_overlay_concat(
    segments: List[Dict[str, Any]],
    video_width: int,
    video_height: int,
    total_duration: float,
    temp_dir: Path,
) -> Path:
    """
    Generates a full-timeline subtitle overlay concat demuxer playlist.
    Returns the path to 'subtitles_concat.txt'.
    """
    sub_img_dir = temp_dir / "sub_frames"
    sub_img_dir.mkdir(parents=True, exist_ok=True)

    # Create master blank image
    blank_png = sub_img_dir / "blank.png"
    if not blank_png.exists():
        blank_img = Image.new("RGBA", (video_width, video_height), (0, 0, 0, 0))
        blank_img.save(str(blank_png), "PNG")

    # Hardsub mask dimensions (bottom 16% of frame)
    mask_box_y = int(video_height * 0.84)
    mask_box_h = int(video_height * 0.16)

    # Sort segments by start timestamp
    sorted_segs = sorted(
        [s for s in segments if (s.get("translation") or s.get("text", "")).strip()],
        key=lambda x: float(x.get("start", 0))
    )

    playlist_lines: List[str] = []
    current_time = 0.0

    for seg in sorted_segs:
        start_t = max(current_time, float(seg.get("start", 0.0)))
        end_t = max(start_t + 0.3, float(seg.get("end", start_t + 1.0)))
        txt = (seg.get("translation") or seg.get("text", "")).strip()
        if not txt:
            continue

        # Fill gap with blank frame
        if start_t > current_time + 0.02:
            gap = start_t - current_time
            playlist_lines.append(f"file '{blank_png}'")
            playlist_lines.append(f"duration {gap:.3f}")
            current_time = start_t

        # Render subtitle PNG
        sub_png = sub_img_dir / f"sub_{seg.get('id', current_time):04d}.png"
        render_khmer_subtitle_frame(
            text=txt,
            width=video_width,
            height=video_height,
            output_path=sub_png,
            mask_box_y=mask_box_y,
            mask_box_h=mask_box_h,
        )

        cue_dur = end_t - start_t
        playlist_lines.append(f"file '{sub_png}'")
        playlist_lines.append(f"duration {cue_dur:.3f}")
        current_time = end_t

    # Fill any remaining time to video end
    if current_time < total_duration:
        playlist_lines.append(f"file '{blank_png}'")
        playlist_lines.append(f"duration {total_duration - current_time:.3f}")

    # FFmpeg concat demuxer quirk: repeat last entry without duration
    playlist_lines.append(f"file '{blank_png}'\n")

    concat_file = temp_dir / "subtitles_concat.txt"
    concat_file.write_text("\n".join(playlist_lines), encoding="utf-8")
    return concat_file

