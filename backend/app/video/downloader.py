"""Universal video downloader supporting YouTube, Bilibili, TikTok, Facebook, Twitter/X, Douyin, and web URLs."""

import re
import time
import shutil
import logging
import asyncio
import subprocess
from pathlib import Path
from typing import Dict, Any, Optional, List
import httpx
import yt_dlp

from .bilibili_auth import get_bilibili_cookie_header, get_bilibili_cookie_file

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
    max_clip_seconds: Optional[int] = 90,
    on_progress: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Downloads Bilibili video directly using Bilibili's official HTML5 API with
    HTTP Range chunking or full stream.
    """
    cookie_str = get_bilibili_cookie_header()
    req_headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Referer": "https://www.bilibili.com/",
    }
    if cookie_str:
        req_headers["Cookie"] = cookie_str
        logger.info(f"Authenticating Bilibili direct download for {bvid} with user session cookies.")
    else:
        logger.info(f"Downloading Bilibili {bvid} as guest (login recommended for faster speeds & 1080P).")
    
    async with httpx.AsyncClient(headers=req_headers, timeout=httpx.Timeout(connect=30.0, read=None, write=60.0, pool=30.0)) as client:
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
        # For authenticated users, try 1080P/720P qualities first, falling back to 360P gracefully
        play_data = {}
        target_qns = [80, 64, 32, 16] if cookie_str else [16]
        for qn_try in target_qns:
            play_url = f"https://api.bilibili.com/x/player/playurl?bvid={bvid}&cid={cid}&qn={qn_try}&type=mp4&platform=html5&high_quality=1"
            try:
                play_resp = await client.get(play_url)
                if play_resp.status_code == 200:
                    pj = play_resp.json()
                    if pj.get("code") == 0 and pj.get("data", {}).get("durl"):
                        play_data = pj
                        logger.info(f"Retrieved Bilibili stream at quality qn={qn_try} for {bvid}")
                        break
            except Exception as ex:
                logger.warning(f"Failed playurl check qn={qn_try}: {ex}")
                continue

        if not play_data or not play_data.get("data", {}).get("durl"):
            raise RuntimeError(f"Could not retrieve direct Bilibili MP4 play stream URL for {bvid}. Delegating to yt-dlp...")

        durl = play_data["data"]["durl"][0]
        mp4_stream_url = durl["url"]
        stream_size = durl.get("size", 0)

        # 3. Stream direct to MP4 using FFmpeg (fast, losslessly copies audio/video with valid MP4 container)
        # 3. Stream direct to MP4
        is_clip = bool(max_clip_seconds and max_clip_seconds > 0 and max_clip_seconds < duration)
        if is_clip:
            final_video_path = output_dir / f"{bvid}_clip{max_clip_seconds}.mp4"
        else:
            final_video_path = output_dir / f"{bvid}.mp4"

        # If file already downloaded and complete on disk, reuse it immediately
        if final_video_path.exists():
            from .dubber import get_media_duration
            existing_dur = get_media_duration(str(final_video_path))
            target_check = max_clip_seconds if is_clip else duration
            if existing_dur >= target_check * 0.90:
                logger.info(f"Video already fully downloaded on disk ({existing_dur:.1f}s). Skipping re-download.")
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
            else:
                logger.warning(f"Existing file on disk is incomplete ({existing_dur:.1f}s / {target_check}s). Removing and re-downloading...")
                final_video_path.unlink(missing_ok=True)

        if is_clip:
            # Fast clip streaming via FFmpeg (-t max_clip_seconds)
            headers_str = (
                "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36\r\n"
                "Referer: https://www.bilibili.com/\r\n"
            )
            if cookie_str:
                headers_str += f"Cookie: {cookie_str}\r\n"

            clip_seconds = max_clip_seconds
            logger.info(f"Streaming Bilibili clip ({clip_seconds}s) for {bvid} directly via FFmpeg...")
            cmd = [
                "ffmpeg", "-y",
                "-reconnect", "1",
                "-reconnect_at_eof", "1",
                "-reconnect_streamed", "1",
                "-reconnect_delay_max", "5",
                "-headers", headers_str,
                "-i", mp4_stream_url,
                "-t", str(clip_seconds),
                "-c", "copy", "-movflags", "+faststart", "-progress", "pipe:1", "-nostats",
                str(final_video_path)
            ]

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL
            )

            target_dur = clip_seconds
            last_progress_time = 0
            current_us = 0
            speed_str = ""
            total_size_bytes = 0

            while True:
                try:
                    line = await process.stdout.readline()
                    if not line:
                        break
                except (OSError, asyncio.CancelledError):
                    break
                line_str = line.decode("utf-8", errors="ignore").strip()
                if "=" in line_str:
                    k, v = line_str.split("=", 1)
                    k, v = k.strip(), v.strip()
                    if k in ("out_time_us", "out_time_ms"):
                        try:
                            current_us = float(v)
                        except ValueError:
                            pass
                    elif k == "speed":
                        speed_str = v
                    elif k == "total_size":
                        try:
                            total_size_bytes = int(v)
                        except ValueError:
                            pass
                    elif k == "progress" and v == "continue":
                        cur_sec = current_us / 1000000.0 if current_us > 1000 else current_us
                        now = time.time()
                        if on_progress and (now - last_progress_time >= 0.4):
                            last_progress_time = now
                            pct = min(99.0, max(1.0, (cur_sec / max(1.0, target_dur)) * 100))
                            speed_disp = speed_str if speed_str and speed_str != "N/A" else "Streaming"
                            speed_mult = 1.0
                            if "x" in speed_disp:
                                try:
                                    speed_mult = float(speed_disp.replace("x", ""))
                                except ValueError:
                                    speed_mult = 1.0
                            eta_val = max(0, (target_dur - cur_sec) / max(0.1, speed_mult))
                            eta_disp = f"{int(eta_val)}s" if eta_val > 0 else "< 1s"
                            if total_size_bytes > 0:
                                mb_downloaded = total_size_bytes / (1024 * 1024)
                                detail_disp = f"{mb_downloaded:.1f} MB ({cur_sec:.0f}s / {target_dur:.0f}s)"
                            else:
                                detail_disp = f"{cur_sec:.0f}s / {target_dur:.0f}s"
                            on_progress(pct, speed_disp, eta_disp, detail_disp)

            await process.wait()
            if process.returncode != 0:
                raise RuntimeError(f"FFmpeg download failed with returncode {process.returncode}")

            if on_progress:
                on_progress(100.0, "Ready", "0s", f"{target_dur:.0f}s / {target_dur:.0f}s")

            from .dubber import get_media_duration
            actual_dur = get_media_duration(str(final_video_path))
            return {
                "id": bvid,
                "title": title,
                "duration": actual_dur,
                "full_duration": duration,
                "thumbnail": pic,
                "video_path": str(final_video_path),
                "sub_path": None,
                "url": f"https://www.bilibili.com/video/{bvid}",
                "platform": "Bilibili",
            }

        else:
            # Full Video: download via fast, resilient HTTP Range Chunking
            # Bypasses single-socket 20-minute CDN timeouts, downloads at 7-12 MB/s, and supports resuming
            part_path = output_dir / f"{bvid}.mp4.part"
            chunk_size = 10 * 1024 * 1024  # 10MB chunk
            total_size = stream_size or durl.get("size", 0)

            if not total_size or total_size == 0:
                try:
                    probe_headers = {"Referer": "https://www.bilibili.com/", "User-Agent": req_headers["User-Agent"], "Range": "bytes=0-0"}
                    if cookie_str:
                        probe_headers["Cookie"] = cookie_str
                    probe_resp = await client.get(mp4_stream_url, headers=probe_headers)
                    cr = probe_resp.headers.get("content-range", "")
                    if "/" in cr:
                        total_size = int(cr.split("/")[-1])
                except Exception as ex:
                    logger.warning(f"Failed to probe file size: {ex}")

            downloaded = part_path.stat().st_size if part_path.exists() else 0
            if total_size and downloaded >= total_size:
                downloaded = 0  # Re-download if old part was corrupt

            size_mb = f"{total_size/(1024*1024):.1f} MB" if total_size else "unknown size"
            logger.info(f"Downloading full Bilibili video {bvid} ({size_mb}, {duration}s) via HTTP Range Chunks...")
            t_start = time.time()
            last_progress_time = 0

            with open(part_path, "ab" if downloaded > 0 else "wb") as f:
                while True:
                    if total_size and downloaded >= total_size:
                        break
                    chunk_end = (downloaded + chunk_size - 1)
                    if total_size:
                        chunk_end = min(total_size - 1, chunk_end)

                    chunk_headers = {
                        "Referer": "https://www.bilibili.com/",
                        "User-Agent": req_headers["User-Agent"],
                        "Range": f"bytes={downloaded}-{chunk_end}",
                    }
                    if cookie_str:
                        chunk_headers["Cookie"] = cookie_str

                    chunk_data = None
                    for attempt in range(5):
                        try:
                            resp = await client.get(mp4_stream_url, headers=chunk_headers)
                            if resp.status_code in (200, 206):
                                chunk_data = resp.content
                                break
                            elif resp.status_code == 416:  # Range Not Satisfiable -> reached EOF
                                break
                            await asyncio.sleep(1)
                        except Exception as ex:
                            if attempt == 4:
                                raise RuntimeError(f"Chunk download failed after 5 retries at offset {downloaded}: {ex}")
                            await asyncio.sleep(1)

                    if not chunk_data:
                        break

                    f.write(chunk_data)
                    f.flush()
                    downloaded += len(chunk_data)

                    now = time.time()
                    if on_progress and (now - last_progress_time >= 0.4):
                        last_progress_time = now
                        pct = (downloaded / max(1, total_size) * 100) if total_size else 50.0
                        elapsed = max(0.1, now - t_start)
                        speed_mb = (downloaded / (1024 * 1024)) / elapsed
                        speed_disp = f"{speed_mb:.1f} MB/s" if speed_mb > 0 else ""
                        eta_val = max(0, (total_size - downloaded) / (1024 * 1024) / max(0.1, speed_mb)) if total_size else 0
                        eta_disp = f"{int(eta_val)}s" if eta_val > 0 else "< 1s"
                        detail_disp = f"{downloaded/(1024*1024):.1f} MB / {total_size/(1024*1024):.1f} MB" if total_size else f"{downloaded/(1024*1024):.1f} MB"
                        on_progress(min(99.0, max(1.0, pct)), speed_disp, eta_disp, detail_disp)

            part_path.replace(final_video_path)

            if on_progress:
                on_progress(100.0, "Ready", "0s", f"{total_size/(1024*1024):.1f} MB Complete")

            from .dubber import get_media_duration
            actual_dur = get_media_duration(str(final_video_path))
            return {
                "id": bvid,
                "title": title,
                "duration": actual_dur or duration,
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
    max_clip_seconds: Optional[int] = 90,
    on_progress: Optional[Any] = None,
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
        clean_url = f"https://www.bilibili.com/video/{bvid}"
        try:
            logger.info(f"Detected Bilibili BV ID: {bvid}. Using direct fast API...")
            res = await download_bilibili_direct(bvid, dest_dir, max_clip_seconds=max_clip_seconds, on_progress=on_progress)
            res["platform"] = "Bilibili"
            return res
        except Exception as e:
            logger.warning(f"Direct Bilibili download failed: {e}. Falling back to yt-dlp...")
            url = clean_url

    # 2. Universal Downloader (YouTube, TikTok, Facebook, Twitter, Douyin, Vimeo, Reddit, etc.)
    from .dubber import get_media_duration

    # Look for Node.js runtime for YouTube signature deciphering
    node_bin = shutil.which("node") or "/opt/homebrew/bin/node"
    has_node = bool(node_bin and Path(node_bin).exists())

    bilibili_cookie_file = get_bilibili_cookie_file()
    is_bilibili = (platform == "Bilibili") or bool(extract_bilibili_bvid(url))

    # Format selection: allow 1080P if authenticated Bilibili cookies exist or 720P default
    target_format = (
        'bestvideo[height<=1080][vcodec^=avc]+bestaudio[acodec^=mp4a]/bestvideo[height<=1080]+bestaudio/bestvideo*+bestaudio/best'
        if (is_bilibili and bilibili_cookie_file)
        else 'bestvideo[height<=720][vcodec^=avc]+bestaudio[acodec^=mp4a]/bestvideo[height<=720]+bestaudio/bestvideo*+bestaudio/best'
    )

    ydl_opts: Dict[str, Any] = {
        'format': target_format,
        'merge_output_format': 'mp4',
        'outtmpl': str(dest_dir / '%(id)s.%(ext)s'),
        'writesubtitles': True,
        'writeautomaticsub': False,  # Avoid YouTube auto-sub HTTP 429 rate limits
        'subtitleslangs': ['zh.*', 'en.*', 'km.*'],
        'subtitlesformat': 'srt/vtt/best',
        'nocheckcertificate': True,
        'ignoreerrors': False,
        'quiet': True,              # Prevent [Errno 5] Input/output error on detached stdout/terminal
        'no_warnings': True,
        'noplaylist': True,         # Single video/part only
        'concurrent_fragment_downloads': 4, # Stable concurrent fragment download
        'continuedl': True,  # Resume partially downloaded files (.part) seamlessly
        'nopart': False,
        'retries': 30,
        'fragment_retries': 30,
        'buffersize': 1048576,
    }

    if is_bilibili and bilibili_cookie_file:
        ydl_opts['cookiefile'] = str(bilibili_cookie_file)
        logger.info(f"Using authenticated Bilibili cookies from {bilibili_cookie_file}")

    if has_node:
        ydl_opts['js_runtimes'] = {'node': {}}

    if on_progress:
        last_yt_hook = 0
        def _yt_hook(d):
            nonlocal last_yt_hook
            if d.get("status") == "downloading":
                now = time.time()
                if now - last_yt_hook < 0.4:
                    return
                last_yt_hook = now
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                downloaded = d.get("downloaded_bytes") or 0
                speed = d.get("speed") or 0
                eta = d.get("eta") or 0
                pct = (downloaded / total * 100) if total > 0 else 50.0
                speed_mb = (speed / (1024 * 1024)) if speed else 0
                speed_str = f"{speed_mb:.1f} MB/s" if speed_mb > 0 else ""
                eta_str = f"{int(eta)}s" if eta else ""
                detail_str = f"{downloaded/(1024*1024):.1f} MB / {total/(1024*1024):.1f} MB" if total > 0 else f"{downloaded/(1024*1024):.1f} MB"
                on_progress(pct, speed_str, eta_str, detail_str)
            elif d.get("status") == "finished":
                on_progress(100.0, "Complete", "0s", "Download finished")

        ydl_opts["progress_hooks"] = [_yt_hook]

    # Fast duration range download if requested (> 0)
    if max_clip_seconds and max_clip_seconds > 0:
        ydl_opts['download_ranges'] = yt_dlp.utils.download_range_func(None, [(0, max_clip_seconds)])
        ydl_opts['force_keyframes_at_cuts'] = True

    def _sync_download():
        max_attempts = 4
        for attempt in range(max_attempts):
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    extracted = ydl.extract_info(url, download=True)
                    if not extracted:
                        raise RuntimeError(f"Could not extract video metadata from {platform} ({url})")
                    return extracted
            except Exception as err:
                err_str = str(err)
                is_expired_or_403 = any(kw in err_str for kw in ["403", "Forbidden", "expired", "HTTP Error 403"])
                if attempt < max_attempts - 1 and is_expired_or_403:
                    logger.warning(
                        f"Stream token expired or HTTP 403 on attempt {attempt + 1}/{max_attempts} ({err}). "
                        f"Re-extracting fresh signed stream URL to resume partial download..."
                    )
                    time.sleep(2)
                    continue

                # If download_ranges caused an issue on certain platforms, retry without ranges and trim with ffmpeg
                if 'download_ranges' in ydl_opts:
                    logger.warning(f"Range download failed ({err}). Retrying full download with ffmpeg trimming...")
                    ydl_opts.pop('download_ranges', None)
                    ydl_opts.pop('force_keyframes_at_cuts', None)
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl_retry:
                        extracted_retry = ydl_retry.extract_info(url, download=True)
                        if not extracted_retry:
                            raise RuntimeError(f"Retry could not extract video metadata from {platform} ({url})")
                        return extracted_retry
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

    if not info:
        raise RuntimeError(f"Failed to download video from {platform} ({url}). No video metadata received.")

    video_id = info.get('id', 'video')
    title = info.get('title', 'video')
    full_duration = info.get('duration', 0)
    thumbnail = info.get('thumbnail', '')

    # If info is a playlist (multi-part Bilibili video), take the first entry
    if 'entries' in info and info['entries']:
        entries = [e for e in info['entries'] if e]
        if entries:
            entry = entries[0]
            video_id = entry.get('id', video_id)
            title = entry.get('title', title)
            full_duration = entry.get('duration', full_duration)
            thumbnail = entry.get('thumbnail', thumbnail)

    # Locate downloaded video file
    video_path = dest_dir / f"{video_id}.mp4"
    if not video_path.exists():
        valid_exts = ['.mp4', '.mkv', '.webm', '.mov', '.m4v']
        candidates = []

        # Check requested_downloads from yt-dlp info
        if 'requested_downloads' in info:
            for rd in info['requested_downloads']:
                fp = rd.get('filepath') or rd.get('_filename')
                if fp and Path(fp).exists():
                    candidates.append(Path(fp))

        # Check single file path
        if info.get('_filename') and Path(info['_filename']).exists():
            candidates.append(Path(info['_filename']))

        # Match files containing video_id or BV ID (e.g. BV1m7Yr6mEYF_p1.mp4)
        if not candidates:
            bvid_match = extract_bilibili_bvid(url) or video_id
            candidates = [
                p for p in dest_dir.iterdir()
                if p.is_file() and p.suffix.lower() in valid_exts and (video_id in p.name or bvid_match in p.name)
            ]

        if candidates:
            # Sort: prioritize part 1 (_p1) or most recently modified
            candidates.sort(key=lambda p: (0 if "_p1" in p.name.lower() else 1, -p.stat().st_mtime))
            video_path = candidates[0]

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

