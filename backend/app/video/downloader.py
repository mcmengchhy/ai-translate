"""Video downloader supporting Bilibili direct API extraction, YouTube, and web URLs."""

import re
import logging
import asyncio
import subprocess
from pathlib import Path
from typing import Dict, Any, Optional
import httpx
import yt_dlp

logger = logging.getLogger(__name__)

ROOT_PROJECT_DIR = Path(__file__).resolve().parent.parent.parent.parent
STORAGE_DIR = ROOT_PROJECT_DIR / "storage" / "downloads"
STORAGE_DIR.mkdir(parents=True, exist_ok=True)


def extract_bilibili_bvid(url: str) -> Optional[str]:
    """Extracts Bilibili BV ID from URL (e.g. BV1Foa16yEAh)."""
    match = re.search(r"(BV[a-zA-Z0-9]+)", url)
    return match.group(1) if match else None


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
        }


async def download_video(
    url: str,
    output_dir: Optional[Path] = None,
    max_clip_seconds: Optional[int] = 90
) -> Dict[str, Any]:
    """
    Downloads video from Bilibili (via direct API) or YouTube/other platforms (via yt-dlp).
    """
    dest_dir = output_dir or STORAGE_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)

    bvid = extract_bilibili_bvid(url)
    if bvid:
        try:
            logger.info(f"Detected Bilibili BV ID: {bvid}. Using direct API (clip: {max_clip_seconds}s)...")
            return await download_bilibili_direct(bvid, dest_dir, max_clip_seconds=max_clip_seconds)
        except Exception as e:
            logger.warning(f"Direct Bilibili download failed: {e}. Falling back to yt-dlp...")

    # Fallback to yt-dlp for YouTube, Vimeo, direct mp4, etc.
    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': str(dest_dir / '%(id)s.%(ext)s'),
        'writesubtitles': True,
        'writeautomaticsub': True,
        'subtitleslangs': ['zh', 'en'],
        'postprocessors': [{
            'key': 'FFmpegVideoConvertor',
            'preferedformat': 'mp4',
        }],
        'quiet': False,
    }

    def _sync_download():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            video_id = info.get('id', 'video')
            title = info.get('title', 'video')
            duration = info.get('duration', 0)
            thumbnail = info.get('thumbnail', '')

            video_path = dest_dir / f"{video_id}.mp4"
            if not video_path.exists():
                for candidate in dest_dir.glob(f"{video_id}.*"):
                    if candidate.suffix in ['.mp4', '.mkv', '.webm']:
                        video_path = candidate
                        break

            return {
                "id": video_id,
                "title": title,
                "duration": duration,
                "thumbnail": thumbnail,
                "video_path": str(video_path),
                "sub_path": None,
                "url": url,
            }

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_download)
