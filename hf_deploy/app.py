# -*- coding: utf-8 -*-
import re
import os
import logging
import uuid
import shutil
import requests
import uvicorn
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
import yt_dlp

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

app = FastAPI(title="Video Downloader API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    if isinstance(exc, HTTPException):
        raise exc
    logger.error(f"Unhandled error on {request.url.path}", exc_info=True)
    from fastapi.responses import JSONResponse
    detail = str(exc)[:300]
    return JSONResponse(status_code=500, content={"detail": detail})

# ─── Constants ────────────────────────────────────────────────────────────────

YOUTUBE_PATTERN = r"(youtube\.com|youtu\.be)"

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://www.pinterest.com/",
}

# FFmpeg is installed via Dockerfile
FFMPEG_BIN = shutil.which("ffmpeg")
FFMPEG_DIR = os.path.dirname(FFMPEG_BIN) if FFMPEG_BIN else None
FFMPEG_AVAILABLE = FFMPEG_BIN is not None
logger.info(f"FFmpeg available: {FFMPEG_AVAILABLE}, path: {FFMPEG_BIN}")

# ─── Helpers ──────────────────────────────────────────────────────────────────

def detect_platform(url: str) -> str:
    u = url.lower()
    if "instagram.com" in u: return "instagram"
    if "facebook.com" in u or "fb.com" in u or "fb.watch" in u: return "facebook"
    if "tiktok.com" in u: return "tiktok"
    if "x.com" in u or "twitter.com" in u: return "twitter"
    if "pinterest.com" in u or "pin.it" in u: return "pinterest"
    if "linkedin.com" in u: return "linkedin"
    if "snapchat.com" in u: return "snapchat"
    if "reddit.com" in u or "redd.it" in u: return "reddit"
    if "vimeo.com" in u: return "vimeo"
    if "dailymotion.com" in u or "dai.ly" in u: return "dailymotion"
    if "twitch.tv" in u: return "twitch"
    if "rumble.com" in u: return "rumble"
    if "bilibili.com" in u or "b23.tv" in u: return "bilibili"
    if "vk.com" in u: return "vk"
    if "tumblr.com" in u: return "tumblr"
    if "imgur.com" in u: return "imgur"
    if "odysee.com" in u: return "odysee"
    return "unknown"

def resolve_pinterest_url(short_url: str) -> str:
    if "pin.it" not in short_url.lower():
        return short_url
    try:
        resp = requests.head(short_url, headers=BROWSER_HEADERS, allow_redirects=True, timeout=10)
        return resp.url
    except Exception:
        return short_url

def get_pinterest_direct_link(url: str):
    final_url = resolve_pinterest_url(url)
    try:
        resp = requests.get(final_url, headers=BROWSER_HEADERS, timeout=15)
        if resp.status_code != 200:
            return None
        matches = re.findall(r'https://v[0-9]*\.pinimg\.com/videos/[^"\' ]+?\.mp4', resp.text)
        if not matches:
            return None
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
        if thumb_match:
            thumb = thumb_match.group(1)
        fmts = []
        for i, link in enumerate(unique):
            fmts.append({
                "format_id": f"pinterest_{i}",
                "resolution": "Best Quality" if i == 0 else f"Video {i+1}",
                "ext": "mp4",
                "url": link,
                "filesize": None,
                "has_audio": True,
                "format_note": "Video+Audio",
                "needs_merge": False,
            })
        return {"title": "Pinterest Video", "thumbnail": thumb, "duration": None, "formats": fmts}
    except Exception as e:
        logger.warning(f"Pinterest scrape failed: {e}")
        return None

def has_audio_track(fmt: dict) -> bool:
    return fmt.get("acodec", "none") not in ("none", None)

def has_video_track(fmt: dict) -> bool:
    return fmt.get("vcodec", "none") not in ("none", None)

def extract_resolution(fmt: dict) -> str:
    height = fmt.get("height")
    if height:
        return f"{height}p"
    if fmt.get("vcodec") == "none":
        return "Audio Only"
    return "Unknown"

def get_format_note(fmt: dict) -> str:
    v = has_video_track(fmt)
    a = has_audio_track(fmt)
    if v and a: return "Video+Audio"
    if v: return "Video Only"
    if a: return "Audio Only"
    return "Unknown"

def get_format_priority(fmt) -> int:
    height_match = re.search(r"(\d+)", fmt["resolution"])
    height = int(height_match.group(1)) if height_match else 0
    if not fmt["has_audio"]:
        return height - 50000
    if fmt["resolution"] == "Audio Only":
        return height - 100000
    return height

def platform_error(url: str, message: str) -> str:
    examples = {
        "instagram": "Example: https://www.instagram.com/reel/ABC123/",
        "facebook": "Example: https://www.facebook.com/watch/?v=123",
        "tiktok": "Example: https://www.tiktok.com/@user/video/123",
        "twitter": "Example: https://x.com/user/status/123",
        "pinterest": "Example: https://www.pinterest.com/pin/123/",
        "reddit": "Example: https://www.reddit.com/r/.../comments/...",
        "vimeo": "Example: https://vimeo.com/123456789",
    }
    plat = detect_platform(url)
    example = examples.get(plat, "Make sure the video URL is correct and publicly accessible.")
    return f"{message}\n\n{example}"

# ─── Models ───────────────────────────────────────────────────────────────────

class ExtractRequest(BaseModel):
    url: str

class DownloadRequest(BaseModel):
    url: str
    format_id: str = "bestvideo+bestaudio/best"

# ─── Routes ───────────────────────────────────────────────────────────────────

@app.get("/status")
async def status():
    return {"status": "ok", "ffmpeg": FFMPEG_AVAILABLE}

@app.post("/extract")
async def extract_video(request: ExtractRequest):
    url = request.url.strip()

    if re.search(YOUTUBE_PATTERN, url, re.IGNORECASE):
        raise HTTPException(status_code=400, detail="YouTube URLs are not supported.")

    plat = detect_platform(url)
    if plat == "pinterest":
        url = resolve_pinterest_url(url)

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
    }
    if plat == "pinterest":
        ydl_opts["http_headers"] = BROWSER_HEADERS

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            logger.info(f"Extracting: {url[:80]}")
            info = ydl.extract_info(url, download=False)

            if not info:
                raise HTTPException(status_code=400, detail="Could not extract video info.")

            formats_raw = info.get("formats", [info])
            seen = set()
            result_formats = []
            has_any_combined = False

            for fmt in formats_raw:
                if not has_video_track(fmt) and not has_audio_track(fmt):
                    continue

                resolution = extract_resolution(fmt)
                has_audio = has_audio_track(fmt)
                note = get_format_note(fmt)
                ext = fmt.get("ext", "mp4")
                direct_url = fmt.get("url", "")

                if not direct_url:
                    continue

                # Determine if this format needs server-side merging
                # Combined (video+audio) and audio-only: phone downloads directly
                # Video-only: needs server merge with best audio
                needs_merge = has_video_track(fmt) and not has_audio

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

                result_formats.append({
                    "format_id": fmt.get("format_id", ""),
                    "resolution": resolution,
                    "ext": ext,
                    # Send actual URL for direct download, "backend_merge" only for video-only
                    "url": "backend_merge" if needs_merge else direct_url,
                    "filesize": fmt.get("filesize") or fmt.get("filesize_approx"),
                    "has_audio": has_audio,
                    "format_note": note,
                    "needs_merge": needs_merge,
                })

            # Add best merged option if only video-only streams exist
            has_video_only = any(f["format_note"] == "Video Only" for f in result_formats)
            if has_video_only and FFMPEG_AVAILABLE and not has_any_combined:
                result_formats.insert(0, {
                    "format_id": "merged_best",
                    "resolution": "Best Quality",
                    "ext": "mp4",
                    "url": "backend_merge",
                    "filesize": None,
                    "has_audio": True,
                    "format_note": "Video+Audio",
                    "needs_merge": True,  # This one genuinely needs server merge
                })

            result_formats.sort(key=get_format_priority, reverse=True)

            return {
                "title": info.get("title", "Unknown Title"),
                "thumbnail": info.get("thumbnail"),
                "duration": info.get("duration"),
                "formats": result_formats,
            }

    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        logger.error(f"DownloadError: {msg[:200]}")

        # Pinterest fallback
        if plat == "pinterest":
            fallback = get_pinterest_direct_link(url)
            if fallback:
                return fallback

        msg_lower = msg.lower()
        if "private" in msg_lower:
            detail = "This video is private."
        elif "sign in" in msg_lower or "login" in msg_lower:
            detail = "This video requires login."
        elif "404" in msg:
            detail = platform_error(url, "Video not found.")
        elif "403" in msg:
            detail = platform_error(url, "This video is restricted.")
        elif "unsupported" in msg_lower:
            detail = platform_error(url, "This URL is not supported.")
        else:
            detail = platform_error(url, "Could not fetch this video. Check the link.")
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
        logger.warning(f"Cleanup failed: {e}")


@app.post("/download")
async def download_video(request: DownloadRequest, background_tasks: BackgroundTasks):
    url = request.url.strip()
    logger.info(f"/download called: {url[:60]} format={request.format_id}")

    if re.search(YOUTUBE_PATTERN, url, re.IGNORECASE):
        raise HTTPException(status_code=400, detail="YouTube URLs not supported.")

    if not FFMPEG_AVAILABLE:
        raise HTTPException(status_code=400, detail="FFmpeg not available on server.")

    temp_id = str(uuid.uuid4())[:8]
    output_template = f"/tmp/temp_{temp_id}_%(id)s.%(ext)s"

    format_spec = request.format_id
    if format_spec == "merged_best":
        format_spec = "bestvideo+bestaudio/best"

    ydl_opts = {
        "format": format_spec,
        "merge_output_format": "mp4",
        "outtmpl": output_template,
        "quiet": True,
        "no_warnings": True,
        "windowsfilenames": False,
    }
    if FFMPEG_DIR:
        ydl_opts["ffmpeg_location"] = FFMPEG_DIR

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            logger.info(f"Downloading + merging: {url[:60]}")
            info = ydl.extract_info(url, download=True)
            merged_file = ydl.prepare_filename(info)

            # Try alternate extensions if file not found
            if not os.path.exists(merged_file):
                base = merged_file.rsplit(".", 1)[0]
                for ext in [".mp4", ".mkv", ".webm"]:
                    candidate = f"{base}{ext}"
                    if os.path.exists(candidate):
                        merged_file = candidate
                        break

            # Last resort: scan /tmp for matching temp file
            if not os.path.exists(merged_file):
                candidates = [
                    f"/tmp/{f}" for f in os.listdir("/tmp")
                    if f.startswith(f"temp_{temp_id}")
                ]
                if candidates:
                    merged_file = candidates[0]

            if not os.path.exists(merged_file):
                raise HTTPException(status_code=500, detail="Merged file not found after download.")

            title = info.get("title", "video")
            safe_title = re.sub(r'[\\/:*?"<>|]', '_', title)[:80]
            safe_title = safe_title.encode('ascii', 'replace').decode('ascii')
            download_name = f"{safe_title}.mp4"

            background_tasks.add_task(cleanup_file, merged_file)

            return FileResponse(
                path=merged_file,
                media_type="video/mp4",
                filename=download_name,
                headers={"Content-Disposition": f'attachment; filename="{download_name}"'},
            )

    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        logger.error(f"DownloadError: {msg[:200]}")
        if "403" in msg:
            detail = "This video is restricted."
        elif "private" in msg.lower():
            detail = "This video is private."
        else:
            detail = f"Download failed: {msg[:200]}"
        raise HTTPException(status_code=400, detail=detail)

    except Exception as e:
        logger.error(f"Download error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error: {str(e)[:200]}")


if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=7860, reload=False)
