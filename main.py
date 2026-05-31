# -*- coding: utf-8 -*-
import re
import os
import json
import logging
import uuid
import subprocess
import shutil
import requests
import uvicorn
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional, List
import yt_dlp

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

app = FastAPI(title="Social Media Video Downloader API")

@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    if isinstance(exc, HTTPException):
        raise exc
    logger.error(f"Unhandled error on {request.url.path}", exc_info=True)
    from fastapi.responses import JSONResponse
    detail = str(exc)[:300]
    if "UnicodeEncodeError" in detail or "latin-1" in detail:
        detail = "Download failed due to special characters in the filename. This has been fixed, please try again."
    elif "timeout" in detail.lower() or "timed out" in detail.lower():
        detail = "Request timed out. Try again later."
    return JSONResponse(status_code=500, content={"detail": detail})

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

YOUTUBE_PATTERN = r"(youtube\.com|youtu\.be)"

def detect_platform(url: str) -> str:
    url_lower = url.lower()
    if "instagram.com" in url_lower: return "instagram"
    if "facebook.com" in url_lower or "fb.com" in url_lower or "fb.watch" in url_lower: return "facebook"
    if "tiktok.com" in url_lower: return "tiktok"
    if "x.com" in url_lower or "twitter.com" in url_lower: return "twitter"
    if "pinterest.com" in url_lower or "pin.it" in url_lower: return "pinterest"
    if "linkedin.com" in url_lower: return "linkedin"
    if "snapchat.com" in url_lower or "snapchat" in url_lower: return "snapchat"
    if "reddit.com" in url_lower or "redd.it" in url_lower: return "reddit"
    if "vimeo.com" in url_lower: return "vimeo"
    if "dailymotion.com" in url_lower or "dai.ly" in url_lower: return "dailymotion"
    if "twitch.tv" in url_lower or "twitch" in url_lower: return "twitch"
    if "rumble.com" in url_lower: return "rumble"
    if "bilibili.com" in url_lower or "b23.tv" in url_lower: return "bilibili"
    if "vk.com" in url_lower or "vk.ru" in url_lower: return "vk"
    if "tumblr.com" in url_lower: return "tumblr"
    if "imgur.com" in url_lower: return "imgur"
    if "odysee.com" in url_lower: return "odysee"
    if "d.tube" in url_lower: return "dtube"
    return "unknown"

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://www.pinterest.com/",
}

def resolve_pinterest_url(short_url: str) -> str:
    if "pin.it" not in short_url.lower():
        return short_url
    try:
        resp = requests.head(short_url, headers=BROWSER_HEADERS, allow_redirects=True, timeout=10)
        return resp.url
    except Exception:
        return short_url

def get_pinterest_direct_link(url: str) -> Optional[dict]:
    final_url = resolve_pinterest_url(url)
    try:
        resp = requests.get(final_url, headers=BROWSER_HEADERS, timeout=15)
        if resp.status_code != 200:
            return None
        matches = re.findall(r'https://v1\.pinimg\.com/videos/[^"\' ]+?\.mp4', resp.text)
        if not matches:
            matches = re.findall(r'https://v[0-9]+\.pinimg\.com/videos/[^"\' ]+?\.mp4', resp.text)
        if matches:
            seen = set()
            unique = []
            for m in matches:
                clean = m.split("?")[0]
                if clean not in seen:
                    seen.add(clean)
                    unique.append(clean)
            unique = unique[:3]
            thumb = None
            thumb_match = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', resp.text)
            if not thumb_match:
                thumb_match = re.search(r"<meta[^>]+property='og:image'[^>]+content='([^']+)'", resp.text)
            if not thumb_match:
                thumb_match = re.search(r'content="([^"]+)"[^>]+property="og:image"', resp.text)
            if thumb_match:
                thumb = thumb_match.group(1)
            logger.info(f"Pinterest fallback: {len(unique)} videos, thumbnail={'yes' if thumb else 'no'}")
            fmts = []
            for i, link in enumerate(unique):
                label = "Best Quality" if i == 0 else f"Video {i + 1}"
                fmts.append({
                    "format_id": f"pinterest_{i}",
                    "resolution": label,
                    "ext": "mp4",
                    "url": link,
                    "filesize": None,
                    "has_audio": True,
                    "format_note": "Video+Audio",
                    "needs_merge": False,
                })
            return {
                "title": "Pinterest Video",
                "thumbnail": thumb,
                "duration": None,
                "formats": fmts,
            }
        return None
    except Exception as e:
        logger.warning(f"Pinterest HTML scrape failed: {e}")
        return None

PLATFORM_EXAMPLES = {
    "instagram": "Example: https://www.instagram.com/reel/ABC123/",
    "facebook": "Example: https://www.facebook.com/watch/?v=123",
    "tiktok": "Example: https://www.tiktok.com/@user/video/123",
    "twitter": "Example: https://x.com/user/status/123",
    "pinterest": "Example: https://www.pinterest.com/pin/123/",
    "linkedin": "Example: https://www.linkedin.com/posts/...",
    "snapchat": "Example: https://www.snapchat.com/spotlight/...",
    "reddit": "Example: https://www.reddit.com/r/.../comments/...",
    "vimeo": "Example: https://vimeo.com/123456789",
    "dailymotion": "Example: https://www.dailymotion.com/video/...",
    "twitch": "Example: https://www.twitch.tv/videos/123456789",
    "rumble": "Example: https://rumble.com/...",
    "bilibili": "Example: https://www.bilibili.com/video/...",
    "vk": "Example: https://vk.com/video...",
    "tumblr": "Example: https://tumblr.com/post/...",
    "imgur": "Example: https://imgur.com/gallery/...",
    "odysee": "Example: https://odysee.com/@user/video",
    "dtube": "Example: https://d.tube/v/...",
}

def platform_error(url: str, message: str) -> str:
    plat = detect_platform(url)
    example = PLATFORM_EXAMPLES.get(plat, "Make sure the video URL is correct and publicly accessible.")
    return f"{message}\n\n{example}"

FFMPEG_BIN = os.environ.get("FFMPEG_PATH") or shutil.which("ffmpeg")
if not FFMPEG_BIN:
    for p in [
        r"C:\ffmpeg\ffmpeg-8.1.1-essentials_build\bin\ffmpeg.exe",
        r"C:\ffmpeg\ffmpeg-8.1.1-full_build\bin\ffmpeg.exe",
    ]:
        if os.path.exists(p):
            FFMPEG_BIN = p
            break
FFMPEG_DIR = os.path.dirname(FFMPEG_BIN) if FFMPEG_BIN and os.path.isfile(FFMPEG_BIN) else None
FFMPEG_AVAILABLE = FFMPEG_DIR is not None

logger.info(f"FFmpeg available: {FFMPEG_AVAILABLE} bin={FFMPEG_BIN} dir={FFMPEG_DIR}")

class ExtractRequest(BaseModel):
    url: str

class DownloadRequest(BaseModel):
    url: str
    format_id: str = "bestvideo+bestaudio/best"

class FormatResponse(BaseModel):
    format_id: str = ""
    resolution: str = ""
    ext: str = ""
    url: str = ""
    filesize: Optional[int] = None
    format_note: str = ""
    video_ext: str = ""
    audio_ext: str = ""
    acodec: str = ""
    vcodec: str = ""
    tbr: float = 0.0
    fps: float = 0.0
    height: int = 0
    width: int = 0
    thumbnail: Optional[str] = None
    duration: Optional[float] = None
    formats: List['FormatResponse'] = []
    has_audio: bool = True
    needs_merge: bool = False

class ExtractResponse(BaseModel):
    title: str = ""
    thumbnail: Optional[str] = None
    duration: Optional[float] = None
    formats: List[FormatResponse] = []

def extract_resolution(format_info: dict) -> str:
    height = format_info.get("height")
    if height:
        return f"{height}p"
    if format_info.get("vcodec") == "none":
        return "Audio Only"
    return "Unknown"

def has_audio_track(format_info: dict) -> bool:
    acodec = format_info.get("acodec", "none")
    return acodec not in ("none", None)

def has_video_track(format_info: dict) -> bool:
    vcodec = format_info.get("vcodec", "none")
    return vcodec not in ("none", None)

def get_format_note(format_info: dict) -> str:
    v = has_video_track(format_info)
    a = has_audio_track(format_info)
    if v and a:
        return "Video+Audio"
    if v and not a:
        return "Video Only"
    if not v and a:
        return "Audio Only"
    return "Unknown"

def get_format_priority(fmt: FormatResponse) -> int:
    height_match = re.search(r"(\d+)", fmt.resolution)
    height = int(height_match.group(1)) if height_match else 0
    if not fmt.has_audio:
        return height - 50000
    if fmt.resolution == "Audio Only":
        return height - 100000
    return height

@app.post("/extract", response_model=ExtractResponse)
async def extract_video(request: ExtractRequest):
    url = request.url.strip()

    if re.search(YOUTUBE_PATTERN, url, re.IGNORECASE):
        raise HTTPException(
            status_code=400,
            detail="YouTube URLs are not supported. Please use links from Instagram, Facebook, TikTok, Twitter, etc."
        )

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
    }

    plat = detect_platform(url)
    if plat == "pinterest":
        url = resolve_pinterest_url(url)
        ydl_opts["http_headers"] = BROWSER_HEADERS

    cookies_file = "cookies.txt"
    if os.path.exists(cookies_file):
        ydl_opts["cookiefile"] = cookies_file

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            logger.info(f"Extracting info for: {url}")
            info = ydl.extract_info(url, download=False)

            if not info:
                raise HTTPException(status_code=400, detail="Could not extract video info. Check the URL.")

            formats = info.get("formats", [info])
            seen = set()
            unique_formats = []
            has_any_combined = False

            for fmt in formats:
                ext = fmt.get("ext", "unknown")
                format_id = fmt.get("format_id", "")
                video_ext = fmt.get("video_ext", "none")
                audio_ext = fmt.get("audio_ext", "none")

                if not has_video_track(fmt) and not has_audio_track(fmt):
                    continue

                resolution = extract_resolution(fmt)
                has_audio = has_audio_track(fmt)
                note = get_format_note(fmt)

                if has_video_track(fmt) and has_audio:
                    dedup_key = f"combined_{resolution}_{ext}"
                    has_any_combined = True
                elif has_video_track(fmt):
                    dedup_key = f"video_{resolution}_{ext}"
                else:
                    dedup_key = f"audio_{ext}"

                if dedup_key in seen:
                    continue
                seen.add(dedup_key)

                download_url = fmt.get("url")
                if not download_url:
                    continue

                unique_formats.append(FormatResponse(
                    format_id=format_id,
                    resolution=resolution,
                    ext=ext,
                    url="backend_merge",
                    filesize=fmt.get("filesize") or fmt.get("filesize_approx"),
                    has_audio=has_audio,
                    format_note=note,
                    needs_merge=True,
                ))

            has_video_only = any(f.format_note == "Video Only" for f in unique_formats)
            has_audio_only = any(f.format_note == "Audio Only" for f in unique_formats)
            logger.info(f"Extract stats: combined={has_any_combined}, video_only={has_video_only}, audio_only={has_audio_only}, ffmpeg={FFMPEG_AVAILABLE}")

            if has_video_only and FFMPEG_AVAILABLE and not has_any_combined:
                unique_formats.insert(0, FormatResponse(
                    format_id="merged_best",
                    resolution="Best Quality",
                    ext="mp4",
                    url="backend_merge",
                    filesize=None,
                    has_audio=True,
                    format_note="Video+Audio",
                    needs_merge=True,
                ))
                logger.info("Added Best Quality (Video+Audio) merged option")

            unique_formats.sort(key=get_format_priority, reverse=True)

            return ExtractResponse(
                title=info.get("title", "Unknown Title"),
                thumbnail=info.get("thumbnail"),
                duration=info.get("duration"),
                formats=unique_formats,
            )

    except yt_dlp.utils.DownloadError as e:
        logger.error(f"DownloadError: {e}")
        msg = str(e)
        plat = detect_platform(url)
        if plat == "pinterest":
            logger.info("yt-dlp failed for Pinterest, trying HTML scrape fallback...")
            fallback = get_pinterest_direct_link(url)
            if fallback:
                fallback_fmts = []
                for f in fallback["formats"]:
                    fallback_fmts.append(FormatResponse(
                        format_id=f["format_id"],
                        resolution=f["resolution"],
                        ext=f["ext"],
                        url=f["url"],
                        filesize=f["filesize"],
                        has_audio=f["has_audio"],
                        format_note=f["format_note"],
                        needs_merge=f["needs_merge"],
                    ))
                return ExtractResponse(
                    title=fallback["title"],
                    thumbnail=fallback["thumbnail"],
                    duration=fallback["duration"],
                    formats=fallback_fmts,
                )
        msg_lower = msg.lower()
        if "Could not copy" in msg or "cookie" in msg_lower:
            detail = "Browser cookies can't be accessed. Close all browser windows and try again, or open Pinterest in Edge browser."
        elif "522" in msg:
            detail = "Server is not responding. Please try again later."
        elif "410" in msg:
            detail = "This video has been removed or is no longer available."
        elif "404" in msg:
            detail = platform_error(url, "Video not found. Check the link.")
        elif "Private video" in msg or "private" in msg_lower:
            detail = "This video is private. Only public videos work."
        elif "Sign in" in msg or "login" in msg_lower:
            detail = "This video requires login. Try a different one."
        elif "Unsupported" in msg or "not supported" in msg_lower:
            detail = platform_error(url, "This URL type is not supported.")
        elif "Invalid URL" in msg or "not a valid" in msg_lower:
            detail = platform_error(url, "Invalid link. Paste the full video URL.")
        elif "403" in msg:
            detail = platform_error(url, "This video is private or restricted. Try a public video.")
        else:
            detail = platform_error(url, "Could not fetch this video. Make sure the link is correct.")
        raise HTTPException(status_code=400, detail=detail)
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)[:200]}")

def cleanup_file(path: str):
    try:
        if os.path.exists(path):
            os.remove(path)
            logger.info(f"Cleaned up: {path}")
    except Exception as e:
        logger.warning(f"Cleanup failed for {path}: {e}")

@app.post("/download")
async def download_video(request: DownloadRequest, background_tasks: BackgroundTasks):
    url = request.url.strip()
    logger.info(f"▶️ /download called url={url[:60]}.. format={request.format_id} ffmpeg={FFMPEG_AVAILABLE}")

    if re.search(YOUTUBE_PATTERN, url, re.IGNORECASE):
        raise HTTPException(status_code=400, detail="YouTube URLs not supported")

    if not FFMPEG_AVAILABLE:
        raise HTTPException(status_code=400, detail="FFmpeg not installed")

    temp_id = str(uuid.uuid4())[:8]
    output_template = f"temp_{temp_id}_%(id)s.%(ext)s"

    format_spec = request.format_id
    if format_spec == "merged_best":
        format_spec = "bestvideo+bestaudio/best"

    ydl_opts = {
        "format": format_spec,
        "merge_output_format": "mp4",
        "outtmpl": output_template,
        "quiet": True,
        "no_warnings": True,
        "windowsfilenames": True,
    }
    if os.path.exists("cookies.txt"):
        ydl_opts["cookiefile"] = "cookies.txt"
    if FFMPEG_DIR:
        ydl_opts["ffmpeg_location"] = FFMPEG_DIR

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            logger.info(f"⏳ Downloading + merging for: {url}")
            info = ydl.extract_info(url, download=True)
            merged_file = ydl.prepare_filename(info)

            if not os.path.exists(merged_file):
                base = merged_file.rsplit(".", 1)[0]
                for ext in [".mp4", ".mkv", ".webm"]:
                    candidate = f"{base}{ext}"
                    if os.path.exists(candidate):
                        merged_file = candidate
                        break

            if not os.path.exists(merged_file):
                candidates = [f for f in os.listdir(".") if f.startswith(f"temp_{temp_id}")]
                if candidates:
                    merged_file = candidates[0]

            logger.info(f"Merged file: {merged_file}")

            if not os.path.exists(merged_file):
                raise HTTPException(status_code=500, detail="Failed to create merged file")

            title = info.get("title", "video")
            safe_title = re.sub(r'[\\/:*?"<>|]', '_', title)[:100]
            safe_title = safe_title.encode('ascii', 'replace').decode('ascii')
            download_name = f"{safe_title}.mp4"

            background_tasks.add_task(cleanup_file, merged_file)

            return FileResponse(
                path=merged_file,
                media_type="video/mp4",
                filename=download_name,
                headers={
                    "Content-Disposition": f"attachment; filename=\"{download_name}\""
                },
            )

    except yt_dlp.utils.DownloadError as e:
        logger.error(f"DownloadError: {e}")
        msg = str(e)
        if "Could not copy" in msg or "cookie" in msg.lower():
            detail = "Browser cookies can't be accessed. Close all browser windows and try again."
        elif "403" in msg:
            detail = "This video is private or restricted."
        elif "522" in msg:
            detail = "Server not responding. Try again later."
        else:
            detail = f"Download failed: {msg[:200]}"
        raise HTTPException(status_code=400, detail=detail)
    except Exception as e:
        logger.error(f"Download error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error: {str(e)[:200]}")

def cleanup_temp_files(temp_id: str):
    for f in os.listdir("."):
        if f.startswith(f"temp_{temp_id}"):
            try:
                os.remove(f)
                logger.info(f"Cleaned up: {f}")
            except Exception as e:
                logger.warning(f"Cleanup failed for {f}: {e}")

@app.get("/status")
async def status():
    return {"status": "ok", "ffmpeg": FFMPEG_AVAILABLE}

@app.get("/install-ffmpeg")
async def install_ffmpeg_info():
    return {
        "ffmpeg_installed": FFMPEG_AVAILABLE,
        "message": "FFmpeg is required for merging video+audio. Install it from https://ffmpeg.org/download.html",
        "windows_install": "Run: winget install ffmpeg  OR  download from https://www.gyan.dev/ffmpeg/builds/"
    }

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
