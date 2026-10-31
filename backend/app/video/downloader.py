"""Universal video downloader supporting YouTube, Bilibili, TikTok, Facebook, Twitter/X, Douyin, and web URLs."""

import re
import shutil
import logging
import asyncio
import subprocess
from pathlib import Path
from typing import Dict, Any, Optional, List
import httpx
import yt_dlp

logger = logging.getLogger(__name__)

ROOT_PROJECT_DIR = Path(__file__).resolve().parent.parent.parent.parent
STORAGE_DIR = ROOT_PROJECT_DIR / "storage" / "downloads"
STORAGE_DIR.mkdir(parents=True, exist_ok=True)


def extract_bilibili_bvid(url: str) -> Optional[str]:
    """Extracts Bilibili BV ID from URL (e.g. BV1Foa16yEAh) or standalone BV string."""
    clean = url.strip()
    # Check standalone BV id
    if re.match(r"^BV[a-zA-Z0-9]{10}$", clean):
        return clean
    # Check bilibili domain or shortlink
    if "bilibili.com" in clean or "b23.tv" in clean:
        match = re.search(r"(BV[a-zA-Z0-9]{10})", clean)
        return match.group(1) if match else None
    return None


def detect_video_platform(url: str) -> str:
    """Detects video platform from URL for optimal pipeline routing and user feedback."""
    clean = url.strip().lower()
    if extract_bilibili_bvid(url):
        return "Bilibili"
    if "youtube.com" in clean or "youtu.be" in clean:
        return "YouTube"
    if "tiktok.com" in clean:
        return "TikTok"
    if "douyin.com" in clean:
        return "Douyin"
    if "facebook.com" in clean or "fb.watch" in clean:
        return "Facebook"
    if "instagram.com" in clean:
        return "Instagram"
    if "twitter.com" in clean or "x.com" in clean:
        return "X (Twitter)"
    if "kuaishou.com" in clean:
        return "Kuaishou"
    if "vimeo.com" in clean:
        return "Vimeo"
    if "reddit.com" in clean:
        return "Reddit"
    if any(clean.split("?")[0].endswith(ext) for ext in [".mp4", ".mov", ".m4v", ".webm", ".m3u8", ".mkv"]):
        return "Direct Video"
    return "Web Video"


async def download_bilibili_direct(
    bvid: str,
    output_dir: Path,
    max_clip_seconds: Optional[int] = 90
) -> Dict[str, Any]:
    """
    Downloads Bilibili video directly using Bilibili's official HTML5 API with
    HTTP Range chunking or full stream.
    """
    headers = {"User-Agent": "curl/8.7.1"}
    
    async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(connect=30.0, read=None, write=60.0, pool=30.0)) as client:
        # 1. Fetch metadata (Title, CID, Duration)
        meta_url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
        resp = await client.get(meta_url)
        if resp.status_code != 200:
            raise RuntimeError(f"Bilibili API returned {resp.status_code}")
        
        meta = resp.json()
        if meta.get("code") != 0:
            raise RuntimeError(f"Bilibili API error: {meta.get('message')}")
        
        data = meta["data"]
        cid = data["cid"]
        title = data["title"]
        duration = data.get("duration", 0)
        pic = data.get("pic", "")

        # 2. Fetch HTML5 play stream URL
        play_url = f"https://api.bilibili.com/x/player/playurl?bvid={bvid}&cid={cid}&qn=16&type=mp4&platform=html5"
        play_resp = await client.get(play_url)
        play_data = play_resp.json()
        
        if play_data.get("code") != 0 or not play_data.get("data", {}).get("durl"):
            raise RuntimeError("Could not retrieve Bilibili play stream URL")

        durl = play_data["data"]["durl"][0]
        mp4_stream_url = durl["url"]
        stream_size = durl.get("size", 0)

        # 3. Download video (Range chunk if bounded, or full stream if 0/None)
        cdn_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://www.bilibili.com/",
        }

        chunk_path = output_dir / f"{bvid}_raw.mp4"
        final_video_path = output_dir / f"{bvid}.mp4"

        # If full video already downloaded, reuse it immediately
        if (not max_clip_seconds or max_clip_seconds == 0) and final_video_path.exists():
            from .dubber import get_media_duration
            existing_dur = get_media_duration(str(final_video_path))
            if existing_dur >= duration - 10:
                logger.info(f"Full video already downloaded on disk ({existing_dur:.1f}s). Skipping re-download.")
                return {
                    "id": bvid,
                    "title": title,
                    "duration": existing_dur,
                    "full_duration": duration,
                    "thumbnail": pic,
                    "video_path": str(final_video_path),
                    "sub_path": None,
                    "url": f"https://www.bilibili.com/video/{bvid}",
                    "platform": "Bilibili",
                }

        if max_clip_seconds and max_clip_seconds > 0 and max_clip_seconds < duration:
            # Estimate bytes for duration (~120KB/s)
            clip_bytes = min(stream_size or 500 * 1024 * 1024, int(max_clip_seconds * 140 * 1024) + 4194304)
            cdn_headers["Range"] = f"bytes=0-{clip_bytes}"
            logger.info(f"Downloading Bilibili clip ({max_clip_seconds}s) for {bvid}...")

            cdn_resp = await client.get(mp4_stream_url, headers=cdn_headers)
            if cdn_resp.status_code not in (200, 206):
                raise RuntimeError(f"CDN returned {cdn_resp.status_code}")

            with open(chunk_path, "wb") as f:
                f.write(cdn_resp.content)

            clip_seconds = min(max_clip_seconds, duration)
            cmd_remux = [
                "ffmpeg", "-y",
                "-i", str(chunk_path),
                "-t", str(clip_seconds),
                "-c", "copy",
                str(final_video_path)
            ]
        else:
            # Full Video Download via curl with auto-resume
            logger.info(f"Downloading full Bilibili video {bvid} via curl (Duration: {duration}s / {duration//60} mins)...")
            target_size = stream_size or 0
            for _ in range(25):
                curr_size = chunk_path.stat().st_size if chunk_path.exists() else 0
                if target_size > 0 and curr_size >= target_size:
                    break
                cmd_curl = [
                    "curl", "-L", "-C", "-",
                    "--retry", "5",
                    "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "-H", "Referer: https://www.bilibili.com/",
                    "-o", str(chunk_path),
                    mp4_stream_url
                ]
                subprocess.run(cmd_curl)
                if target_size == 0 and chunk_path.exists() and chunk_path.stat().st_size > 1024*1024:
                    break

            clip_seconds = duration
            cmd_remux = [
                "ffmpeg", "-y",
                "-i", str(chunk_path),
                "-c", "copy",
                str(final_video_path)
            ]

        subprocess.run(cmd_remux, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        return {
            "id": bvid,
            "title": title,
            "duration": clip_seconds,
            "full_duration": duration,
            "thumbnail": pic,
            "video_path": str(final_video_path),
            "sub_path": None,
            "url": f"https://www.bilibili.com/video/{bvid}",
            "platform": "Bilibili",
        }


async def download_direct_media(
    url: str,
    output_path: Path,
    max_clip_seconds: Optional[int] = 0
) -> str:
    """Fallback stream downloader for direct video links (.mp4, .m3u8, .mov, etc.) via FFmpeg."""
    cmd = ["ffmpeg", "-y"]
    if max_clip_seconds and max_clip_seconds > 0:
        cmd.extend(["-t", str(max_clip_seconds)])
    cmd.extend(["-i", url, "-c", "copy", "-movflags", "+faststart", str(output_path)])
    
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None,
        lambda: subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    )
    return str(output_path)


async def download_video(
    url: str,
    output_dir: Optional[Path] = None,
    max_clip_seconds: Optional[int] = 90
) -> Dict[str, Any]:
    """
    Downloads video from YouTube, Bilibili, TikTok, Facebook, Twitter/X, Douyin, or direct video URL.
    Supports partial duration clipping (max_clip_seconds) and subtitle auto-extraction.
    """
    dest_dir = output_dir or STORAGE_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)

    platform = detect_video_platform(url)
    logger.info(f"Target URL detected platform: '{platform}' (URL: {url}, Clip: {max_clip_seconds}s)")

    # 1. Bilibili Direct API Optimization
    bvid = extract_bilibili_bvid(url)
    if bvid:
        try:
            logger.info(f"Detected Bilibili BV ID: {bvid}. Using direct fast API...")
            res = await download_bilibili_direct(bvid, dest_dir, max_clip_seconds=max_clip_seconds)
            res["platform"] = "Bilibili"
            return res
        except Exception as e:
            logger.warning(f"Direct Bilibili download failed: {e}. Falling back to yt-dlp...")

    # 2. Universal Downloader (YouTube, TikTok, Facebook, Twitter, Douyin, Vimeo, Reddit, etc.)
    from .dubber import get_media_duration

    # Look for Node.js runtime for YouTube signature deciphering
    node_bin = shutil.which("node") or "/opt/homebrew/bin/node"
    has_node = bool(node_bin and Path(node_bin).exists())

    ydl_opts: Dict[str, Any] = {
        'format': 'bestvideo*+bestaudio/best',
        'merge_output_format': 'mp4',
        'outtmpl': str(dest_dir / '%(id)s.%(ext)s'),
        'writesubtitles': True,
        'writeautomaticsub': False,  # Avoid YouTube auto-sub HTTP 429 rate limits
        'subtitleslangs': ['zh.*', 'en.*', 'km.*'],
        'subtitlesformat': 'srt/vtt/best',
        'nocheckcertificate': True,
        'ignoreerrors': True,
        'quiet': False,
    }

    if has_node:
        ydl_opts['js_runtimes'] = {'node': {}}

    # Fast duration range download if requested (> 0)
    if max_clip_seconds and max_clip_seconds > 0:
        ydl_opts['download_ranges'] = yt_dlp.utils.download_range_func(None, [(0, max_clip_seconds)])
        ydl_opts['force_keyframes_at_cuts'] = True

    def _sync_download():
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                return info
        except Exception as err:
            # If download_ranges caused an issue on certain platforms, retry without ranges and trim with ffmpeg
            if 'download_ranges' in ydl_opts:
                logger.warning(f"Range download failed ({err}). Retrying full download with ffmpeg trimming...")
                ydl_opts.pop('download_ranges', None)
                ydl_opts.pop('force_keyframes_at_cuts', None)
                with yt_dlp.YoutubeDL(ydl_opts) as ydl_retry:
                    return ydl_retry.extract_info(url, download=True)
            raise

    loop = asyncio.get_event_loop()
    try:
        info = await loop.run_in_executor(None, _sync_download)
    except Exception as e:
        logger.warning(f"yt-dlp download failed: {e}")
        # 3. Direct video fallback (.mp4, .mov, .m3u8, direct CDN links)
        if any(url.lower().split("?")[0].endswith(ext) for ext in [".mp4", ".mov", ".m4v", ".webm", ".m3u8", ".mkv"]):
            logger.info("Attempting direct media download via FFmpeg...")
            import hashlib
            url_hash = hashlib.md5(url.encode()).hexdigest()[:10]
            fallback_path = dest_dir / f"direct_{url_hash}.mp4"
            await download_direct_media(url, fallback_path, max_clip_seconds=max_clip_seconds)
            actual_dur = get_media_duration(str(fallback_path))
            return {
                "id": url_hash,
                "title": f"Web Video ({url_hash})",
                "duration": actual_dur,
                "full_duration": actual_dur,
                "thumbnail": "",
                "video_path": str(fallback_path),
                "sub_path": None,
                "url": url,
                "platform": "Direct Video",
            }
        raise RuntimeError(f"Failed to download video from {platform} ({url}): {e}")

    video_id = info.get('id', 'video')
    title = info.get('title', 'video')
    full_duration = info.get('duration', 0)
    thumbnail = info.get('thumbnail', '')

    # Locate downloaded video file
    video_path = dest_dir / f"{video_id}.mp4"
    if not video_path.exists():
        for candidate in dest_dir.glob(f"{video_id}.*"):
            if candidate.suffix in ['.mp4', '.mkv', '.webm', '.mov']:
                video_path = candidate
                break

    if not video_path.exists():
        raise RuntimeError(f"Downloaded video file not found for {video_id}")

    # Measure exact duration
    actual_dur = get_media_duration(str(video_path))

    # Strict duration enforcement: if requested max_clip_seconds and actual is longer, trim with ffmpeg
    if max_clip_seconds and max_clip_seconds > 0 and actual_dur > max_clip_seconds + 2:
        logger.info(f"Trimming {platform} video {video_id} to requested {max_clip_seconds}s...")
        trimmed_path = dest_dir / f"{video_id}_trimmed.mp4"
        cmd_trim = [
            "ffmpeg", "-y", "-ss", "0",
            "-i", str(video_path),
            "-t", str(max_clip_seconds),
            "-c", "copy",
            str(trimmed_path)
        ]
        subprocess.run(cmd_trim, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        trimmed_path.replace(video_path)
        actual_dur = get_media_duration(str(video_path))

    # Detect any downloaded subtitles (.srt, .vtt)
    sub_path = None
    sub_candidates = sorted(
        list(dest_dir.glob(f"{video_id}*.srt")) + list(dest_dir.glob(f"{video_id}*.vtt")),
        key=lambda p: (
            0 if any(k in p.name.lower() for k in ['zh', 'chinese', 'cmn', 'hans', 'hant']) else
            1 if any(k in p.name.lower() for k in ['en', 'english']) else
            2
        )
    )
    if sub_candidates:
        sub_path = str(sub_candidates[0])
        logger.info(f"Found downloaded subtitle track for {video_id}: {sub_path}")

    return {
        "id": video_id,
        "title": title,
        "duration": actual_dur,
        "full_duration": full_duration,
        "thumbnail": thumbnail,
        "video_path": str(video_path),
        "sub_path": sub_path,
        "url": url,
        "platform": platform,
    }

