import os
import time
import logging
import tempfile
from urllib.parse import urlparse
from collections import defaultdict, deque
from io import BytesIO
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi import Request
from PIL import Image, UnidentifiedImageError
from yt_dlp import YoutubeDL

app = FastAPI(title="Background Remover MVP", version="0.1.0")
logger = logging.getLogger(__name__)

app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "10"))
MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024
RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "20"))
WHITE_THRESHOLD = int(os.getenv("WHITE_THRESHOLD", "245"))
WHITE_SOFTNESS = int(os.getenv("WHITE_SOFTNESS", "20"))
MAX_DIMENSION = int(os.getenv("MAX_DIMENSION", "1800"))
ADVANCED_TOLERANCE = int(os.getenv("ADVANCED_TOLERANCE", "46"))
ADVANCED_SOFTNESS = int(os.getenv("ADVANCED_SOFTNESS", "24"))
ADVANCED_BG_CLUSTERS = int(os.getenv("ADVANCED_BG_CLUSTERS", "8"))
YTMP3_MAX_DURATION_SECONDS = int(os.getenv("YTMP3_MAX_DURATION_SECONDS", "1200"))

# Simple in-memory limiter for MVP usage.
# For multi-instance production, replace with Redis-backed limiting.
_REQUESTS_BY_IP: dict[str, deque[float]] = defaultdict(deque)


def _get_client_ip(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _enforce_rate_limit(client_ip: str) -> None:
    now = time.time()
    window_start = now - 60
    entries = _REQUESTS_BY_IP[client_ip]

    while entries and entries[0] < window_start:
        entries.popleft()

    if len(entries) >= RATE_LIMIT_PER_MINUTE:
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Try again in a minute.",
        )

    entries.append(now)


def _remove_white_background(raw: bytes) -> BytesIO:
    image = Image.open(BytesIO(raw)).convert("RGBA")

    # Resize very large images to keep memory usage predictable on free instances.
    width, height = image.size
    max_side = max(width, height)
    if max_side > MAX_DIMENSION:
        scale = MAX_DIMENSION / max_side
        image = image.resize((int(width * scale), int(height * scale)))

    pixels = image.load()
    w, h = image.size
    threshold = max(0, min(255, WHITE_THRESHOLD))
    softness = max(1, min(80, WHITE_SOFTNESS))

    for y in range(h):
        for x in range(w):
            r, g, b, a = pixels[x, y]
            min_rgb = min(r, g, b)

            if min_rgb >= threshold:
                pixels[x, y] = (r, g, b, 0)
                continue

            if min_rgb >= threshold - softness:
                # Smooth edge alpha for near-white pixels.
                ratio = (threshold - min_rgb) / softness
                new_alpha = int(max(0, min(255, a * ratio)))
                pixels[x, y] = (r, g, b, new_alpha)

    result = BytesIO()
    image.save(result, format="PNG")
    result.seek(0)
    return result


def _collect_border_clusters(
    pixels, width: int, height: int, max_clusters: int
) -> list[tuple[int, int, int]]:
    bucket_size = 24
    clusters: dict[tuple[int, int, int], list[int]] = {}
    step = max(1, max(width, height) // 400)

    def add_sample(x: int, y: int) -> None:
        r, g, b, _ = pixels[x, y]
        key = (r // bucket_size, g // bucket_size, b // bucket_size)
        if key not in clusters:
            clusters[key] = [0, 0, 0, 0]
        bucket = clusters[key]
        bucket[0] += 1
        bucket[1] += r
        bucket[2] += g
        bucket[3] += b

    for x in range(0, width, step):
        add_sample(x, 0)
        add_sample(x, height - 1)
    for y in range(0, height, step):
        add_sample(0, y)
        add_sample(width - 1, y)

    sorted_clusters = sorted(clusters.values(), key=lambda item: item[0], reverse=True)
    selected = sorted_clusters[: max(1, min(16, max_clusters))]

    if not selected:
        return [(255, 255, 255)]

    return [
        (entry[1] // entry[0], entry[2] // entry[0], entry[3] // entry[0])
        for entry in selected
    ]


def _min_distance_sq(color: tuple[int, int, int, int], refs: list[tuple[int, int, int]]) -> int:
    r, g, b, _ = color
    best = 255 * 255 * 3
    for rr, gg, bb in refs:
        dr = r - rr
        dg = g - gg
        db = b - bb
        dist = dr * dr + dg * dg + db * db
        if dist < best:
            best = dist
    return best


def _remove_background_advanced(raw: bytes) -> BytesIO:
    image = Image.open(BytesIO(raw)).convert("RGBA")

    # Resize very large images to keep memory usage predictable on free instances.
    width, height = image.size
    max_side = max(width, height)
    if max_side > MAX_DIMENSION:
        scale = MAX_DIMENSION / max_side
        image = image.resize((int(width * scale), int(height * scale)))

    pixels = image.load()
    w, h = image.size
    refs = _collect_border_clusters(pixels, w, h, ADVANCED_BG_CLUSTERS)

    tolerance = max(12, min(140, ADVANCED_TOLERANCE))
    softness = max(4, min(80, ADVANCED_SOFTNESS))
    tol_sq = tolerance * tolerance
    seed_tol_sq = (tolerance + 18) * (tolerance + 18)
    soft_sq = (tolerance + softness) * (tolerance + softness)

    visited = bytearray(w * h)
    queue: deque[int] = deque()

    def push_if_background(x: int, y: int, threshold_sq: int) -> None:
        idx = y * w + x
        if visited[idx]:
            return
        if _min_distance_sq(pixels[x, y], refs) <= threshold_sq:
            visited[idx] = 1
            queue.append(idx)

    for x in range(w):
        push_if_background(x, 0, seed_tol_sq)
        push_if_background(x, h - 1, seed_tol_sq)
    for y in range(h):
        push_if_background(0, y, seed_tol_sq)
        push_if_background(w - 1, y, seed_tol_sq)

    while queue:
        idx = queue.popleft()
        x = idx % w
        y = idx // w

        if x > 0:
            push_if_background(x - 1, y, tol_sq)
        if x + 1 < w:
            push_if_background(x + 1, y, tol_sq)
        if y > 0:
            push_if_background(x, y - 1, tol_sq)
        if y + 1 < h:
            push_if_background(x, y + 1, tol_sq)

    for y in range(h):
        for x in range(w):
            idx = y * w + x
            r, g, b, a = pixels[x, y]

            if visited[idx]:
                pixels[x, y] = (r, g, b, 0)
                continue

            # Soft transition only on pixels adjacent to detected background.
            is_adjacent_to_bg = False
            if x > 0 and visited[idx - 1]:
                is_adjacent_to_bg = True
            elif x + 1 < w and visited[idx + 1]:
                is_adjacent_to_bg = True
            elif y > 0 and visited[idx - w]:
                is_adjacent_to_bg = True
            elif y + 1 < h and visited[idx + w]:
                is_adjacent_to_bg = True

            if not is_adjacent_to_bg:
                continue

            dist_sq = _min_distance_sq((r, g, b, a), refs)
            if dist_sq <= soft_sq:
                if soft_sq == tol_sq:
                    alpha_scale = 0.0
                else:
                    alpha_scale = (dist_sq - tol_sq) / (soft_sq - tol_sq)
                alpha_scale = max(0.0, min(1.0, alpha_scale))
                pixels[x, y] = (r, g, b, int(a * alpha_scale))

    result = BytesIO()
    image.save(result, format="PNG")
    result.seek(0)
    return result


@app.get("/", response_class=HTMLResponse)
async def home(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("home.html", {"request": request})


@app.get("/tools/remove-bg", response_class=HTMLResponse)
async def remove_bg_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/tools/youtube-mp3", response_class=HTMLResponse)
async def youtube_mp3_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("youtube_mp3.html", {"request": request})


@app.get("/privacy", response_class=HTMLResponse)
async def privacy(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("privacy.html", {"request": request})


@app.get("/terms", response_class=HTMLResponse)
async def terms(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("terms.html", {"request": request})


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/api/youtube-to-mp3")
async def youtube_to_mp3(background_tasks: BackgroundTasks, url: str = Form(...)) -> FileResponse:
    parsed = urlparse(url.strip())
    host = (parsed.netloc or "").lower()
    if not host:
        raise HTTPException(status_code=400, detail="Invalid URL")

    if "youtube.com" not in host and "youtu.be" not in host:
        raise HTTPException(status_code=400, detail="Only YouTube links are accepted")

    tmp_dir = Path(tempfile.mkdtemp(prefix="ytmp3_"))
    outtmpl = str(tmp_dir / "%(id)s.%(ext)s")

    try:
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": outtmpl,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ],
        }

        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

        duration = int(info.get("duration") or 0)
        if duration and duration > YTMP3_MAX_DURATION_SECONDS:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Video too long. Max duration is {YTMP3_MAX_DURATION_SECONDS // 60} minutes "
                    "for this server tier."
                ),
            )

        mp3_candidates = sorted(tmp_dir.glob("*.mp3"))
        if not mp3_candidates:
            raise HTTPException(status_code=500, detail="Could not create MP3 output")

        mp3_path = mp3_candidates[0]
        base_name = (info.get("title") or "audio").strip()[:80]
        safe_name = "".join(ch if ch.isalnum() or ch in (" ", "-", "_") else "_" for ch in base_name)
        if not safe_name:
            safe_name = "audio"

        def _cleanup_dir() -> None:
            for item in tmp_dir.glob("*"):
                try:
                    item.unlink(missing_ok=True)
                except OSError:
                    pass
            try:
                tmp_dir.rmdir()
            except OSError:
                pass

        background_tasks.add_task(_cleanup_dir)
        return FileResponse(
            path=str(mp3_path),
            media_type="audio/mpeg",
            filename=f"{safe_name}.mp3",
            background=background_tasks,
        )
    except HTTPException:
        for item in tmp_dir.glob("*"):
            item.unlink(missing_ok=True)
        tmp_dir.rmdir()
        raise
    except Exception as exc:
        logger.exception("YouTube to MP3 failed")
        for item in tmp_dir.glob("*"):
            item.unlink(missing_ok=True)
        tmp_dir.rmdir()
        raise HTTPException(status_code=500, detail="Could not process this video") from exc


@app.post("/api/remove-bg")
async def remove_background(
    request: Request,
    file: UploadFile = File(...),
    mode: str = Form("simple"),
) -> StreamingResponse:
    _enforce_rate_limit(_get_client_ip(request))

    mode = (mode or "simple").strip().lower()
    if mode not in {"simple", "advanced"}:
        raise HTTPException(status_code=400, detail="Invalid mode")

    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Only image files are allowed")

    raw = await file.read()
    if len(raw) == 0:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(raw) > MAX_FILE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File too large. Max size is {MAX_FILE_MB}MB",
        )

    try:
        # Validate the input as an image before processing.
        Image.open(BytesIO(raw)).verify()
    except (UnidentifiedImageError, OSError) as exc:
        raise HTTPException(status_code=400, detail="Invalid image file") from exc

    try:
        if mode == "advanced":
            result = _remove_background_advanced(raw)
        else:
            result = _remove_white_background(raw)
    except Exception as exc:
        logger.exception("Background removal failed")
        raise HTTPException(status_code=500, detail="Background removal failed") from exc

    safe_name = (file.filename or "image").rsplit(".", 1)[0]
    headers = {
        "Content-Disposition": f'attachment; filename="{safe_name}_transparent.png"'
    }
    return StreamingResponse(result, media_type="image/png", headers=headers)
