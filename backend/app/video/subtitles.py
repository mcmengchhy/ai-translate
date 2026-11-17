"""Readable subtitle layout without splitting Khmer combining clusters."""

import os
import re
import unicodedata
from pathlib import Path


def graphemes(text):
    clusters = []
    join_next = False
    for char in text:
        combining = unicodedata.category(char).startswith("M") or char in "\u200d\u200c"
        if clusters and (combining or join_next):
            clusters[-1] += char
        else:
            clusters.append(char)
        join_next = char in "\u17d2\u200d"
    return clusters


def wrap_subtitle(text, line_width=38):
    """Balance two lines at a phrase/word boundary; keep every source character."""
    text = re.sub(r"\s+", " ", text.replace("\u200b", "")).strip()
    clusters = graphemes(text)
    if len(clusters) <= line_width:
        return text
    midpoint = len(clusters) / 2
    candidates = [i + 1 for i, cluster in enumerate(clusters[:-1]) if cluster.isspace() or cluster in "។!?៖,;។"]
    candidates = [i for i in candidates if len(clusters) * 0.30 <= i <= len(clusters) * 0.70]
    if len(clusters) <= line_width * 2:
        candidates = [i for i in candidates if len(clusters) - line_width <= i <= line_width]
    split = min(candidates, key=lambda i: abs(i - midpoint)) if candidates else round(midpoint)
    return "".join(clusters[:split]).strip() + "\n" + "".join(clusters[split:]).strip()


def subtitle_pages(text, line_width=38):
    clusters = graphemes(re.sub(r"\s+", " ", text.replace("\u200b", "")).strip())
    result = []
    limit = line_width * 2
    while len(clusters) > limit:
        boundaries = [i + 1 for i, char in enumerate(clusters[:limit]) if char.isspace() or char in "។!?៖,;"]
        split = next((i for i in reversed(boundaries) if i >= limit // 2), limit)
        result.append(wrap_subtitle("".join(clusters[:split]).strip(), line_width))
        clusters = clusters[split:]
    if clusters:
        result.append(wrap_subtitle("".join(clusters).strip(), line_width))
    return result


def readable_cues(segments, video_duration=None):
    """Use brief pauses for reading time while respecting the next spoken cue."""
    result = []
    for index, segment in enumerate(segments):
        text = (segment.get("translation") if not segment.get("translation_error") else "") or ""
        if not text and segment.get("audio_source") == "original_fallback":
            text = segment.get("fallback_subtitle") or segment.get("english_intent") or segment.get("text") or ""
        text = text.strip()
        if not text:
            continue
        start = max(0, float(segment.get("start", 0)))
        speech_end = max(start + 0.04, float(segment.get("end", start + 1)))
        reading_time = min(6.0, max(1.2, len(graphemes(text)) / 14.0))
        end = max(speech_end, start + reading_time)
        # Keep captions near their dialogue and never cross the next cue.
        end = min(end, speech_end + 1.0)
        if index + 1 < len(segments):
            end = min(end, max(start + 0.04, float(segments[index + 1].get("start", end)) - 0.03))
        if video_duration:
            end = min(end, video_duration)
        if end > start:
            pages = subtitle_pages(text)
            weights = [len(graphemes(page.replace("\n", ""))) for page in pages]
            total = max(1, sum(weights))
            page_start = start
            for page, weight in zip(pages, weights):
                page_end = page_start + (end - start) * weight / total
                result.append({"start": page_start, "end": min(end, page_end), "text": page})
                page_start = page_end
    return result


def subtitle_font():
    candidates = [
        os.getenv("SUBTITLE_FONT_PATH", ""),
        str(Path(__file__).resolve().parents[3] / "frontend/assets/fonts/NotoSansKhmer.ttf"),
        "/Library/Fonts/NotoSansKhmerUI-Regular.ttf",
        "/System/Library/Fonts/Supplemental/Khmer Sangam MN.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansKhmer-Regular.ttf",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise RuntimeError("A Khmer font is required for burned subtitles. Set SUBTITLE_FONT_PATH to a Khmer TTF font.")


def _shape_line(text, font_path, size):
    """Shape Khmer through HarfBuzz and rasterize its positioned glyphs through FreeType."""
    import freetype
    import uharfbuzz as hb
    from PIL import Image, ImageChops, ImageFilter

    face = freetype.Face(font_path)
    face.set_char_size(size * 64)
    hb_font = hb.Font(hb.Face(Path(font_path).read_bytes()))
    hb_font.scale = (size * 64, size * 64)
    hb.ot_font_set_funcs(hb_font)
    buffer = hb.Buffer()
    buffer.add_str(text)
    buffer.guess_segment_properties()
    hb.shape(hb_font, buffer)
    pen_x = 0.0
    glyphs = []
    ascent = face.size.ascender / 64
    descent = -face.size.descender / 64
    for info, position in zip(buffer.glyph_infos, buffer.glyph_positions):
        face.load_glyph(info.codepoint, freetype.FT_LOAD_RENDER | freetype.FT_LOAD_TARGET_NORMAL)
        bitmap = face.glyph.bitmap
        if bitmap.width and bitmap.rows:
            glyph = Image.frombytes("L", (bitmap.width, bitmap.rows), bytes(bitmap.buffer), "raw", "L", abs(bitmap.pitch))
            glyphs.append((glyph, round(pen_x + position.x_offset / 64 + face.glyph.bitmap_left),
                           round(ascent - position.y_offset / 64 - face.glyph.bitmap_top)))
        pen_x += position.x_advance / 64
    padding = max(3, size // 10)
    mask = Image.new("L", (max(1, round(pen_x)) + padding * 2, max(1, round(ascent + descent)) + padding * 2))
    for glyph, x, y in glyphs:
        # Glyphs can overlap within a correctly shaped Khmer syllable.
        layer = Image.new("L", mask.size)
        layer.paste(glyph, (x + padding, y + padding))
        mask = ImageChops.lighter(mask, layer)
    stroke = max(1, size // 18)
    outline = mask.filter(ImageFilter.MaxFilter(stroke * 2 + 1))
    image = Image.new("RGBA", mask.size, "black")
    image.putalpha(outline)
    white = Image.new("RGBA", mask.size, "white")
    white.putalpha(mask)
    return Image.alpha_composite(image, white)


def render_caption_track(segments, width, height, video_duration, directory):
    """Rasterize shaped Khmer captions into a timed transparent PNG track.

    This also works with FFmpeg builds that do not include libass/subtitles.
    """
    from PIL import Image
    directory.mkdir(parents=True, exist_ok=True)
    band_height = max(48, int(height * 0.20))
    font_size = max(12, int(height * 0.045))
    blank = directory / "blank.png"
    Image.new("RGBA", (width, band_height)).save(blank)
    events = []
    cursor = 0.0
    font_path = subtitle_font()
    for index, cue in enumerate(readable_cues(segments, video_duration)):
        start, end = cue["start"], cue["end"]
        if start > cursor:
            events.append((blank, start - cursor))
        image = Image.new("RGBA", (width, band_height))
        size = font_size
        while True:
            lines = [_shape_line(line, font_path, size) for line in cue["text"].splitlines()]
            text_height = sum(line.height for line in lines) + 4 * (len(lines) - 1)
            if size <= 10 or (max(line.width for line in lines) <= width * 0.94 and text_height <= band_height * 0.94):
                break
            size -= 1
        y = (band_height - text_height) // 2
        for line in lines:
            image.alpha_composite(line, ((width - line.width) // 2, y))
            y += line.height + 4
        filename = directory / f"cue_{index:05d}.png"
        image.save(filename)
        events.append((filename, end - start))
        cursor = end
    if cursor < video_duration:
        events.append((blank, video_duration - cursor))
    playlist = directory / "captions.ffconcat"
    # Relative, generated filenames avoid quoting arbitrary uploaded paths in the demuxer.
    lines = ["ffconcat version 1.0"]
    for filename, duration in events:
        lines.extend([f"file '{filename.name}'", f"duration {max(0.04, duration):.6f}"])
    lines.append("file 'blank.png'")
    playlist.write_text("\n".join(lines) + "\n")
    return playlist
