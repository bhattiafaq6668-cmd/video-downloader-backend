---
title: Video Downloader API
emoji: 🎬
colorFrom: purple
colorTo: blue
sdk: docker
pinned: false
---

# Video Downloader API

FastAPI backend for social media video downloading using yt-dlp.

## Endpoints

- `POST /extract` — Extract video formats from URL
- `POST /download` — Download and merge video+audio
- `GET /status` — Health check
