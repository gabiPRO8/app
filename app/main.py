import os
import time
import logging
from collections import defaultdict, deque
from io import BytesIO

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi import Request
from PIL import Image, UnidentifiedImageError

app = FastAPI(title="Background Remover MVP", version="0.1.0")
logger = logging.getLogger(__name__)

app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "10"))
MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024
RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "20"))

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


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/privacy", response_class=HTMLResponse)
async def privacy(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("privacy.html", {"request": request})


@app.get("/terms", response_class=HTMLResponse)
async def terms(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("terms.html", {"request": request})


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/api/remove-bg")
async def remove_background(request: Request, file: UploadFile = File(...)) -> StreamingResponse:
    _enforce_rate_limit(_get_client_ip(request))

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
        from rembg import remove

        out_bytes = remove(raw)
        png = Image.open(BytesIO(out_bytes)).convert("RGBA")
        result = BytesIO()
        png.save(result, format="PNG")
        result.seek(0)
    except Exception as exc:
        logger.exception("Background removal failed")
        raise HTTPException(status_code=500, detail="Background removal failed") from exc

    safe_name = (file.filename or "image").rsplit(".", 1)[0]
    headers = {
        "Content-Disposition": f'attachment; filename="{safe_name}_transparent.png"'
    }
    return StreamingResponse(result, media_type="image/png", headers=headers)
