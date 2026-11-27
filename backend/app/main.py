"""Main FastAPI application entrypoint."""

import logging
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse

from .api.routes import router as api_router
from .api.video_routes import router as video_router
from .api.media_export_routes import router as media_export_router
from .core import config
from .video.pipeline import cleanup_saved_job_caches, mark_interrupted_jobs
from .video.batch import restore_batches, stop_batches
from .video import pipeline
from .video import media_exports

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("ai_translator")
logging.getLogger("httpx").setLevel(logging.WARNING)

@asynccontextmanager
async def lifespan(app):
    await asyncio.to_thread(mark_interrupted_jobs)
    await asyncio.to_thread(media_exports.mark_interrupted_exports)
    await asyncio.to_thread(cleanup_saved_job_caches)
    await restore_batches()
    try:
        yield
    finally:
        await media_exports.stop_exports()
        await stop_batches()


app = FastAPI(
    lifespan=lifespan,
    title="Khmer AI Translator & Video Dubbing API",
    description="Next-generation intelligent translation and video voice-dubbing system specialized in Khmer language nuances.",
    version="2.0.0",
)


@app.middleware("http")
async def clear_processing_cache_before_submission(request, call_next):
    if pipeline.RESETTING and request.method in {"POST", "PUT", "DELETE"} and request.url.path.startswith("/api/video/"):
        return JSONResponse(status_code=409, content={"detail": "Task caches are being cleared. Try again shortly."})
    if request.method == "POST" and request.url.path.rstrip("/") in {
        "/api/video/dub-upload", "/api/video/dub-url", "/api/video/split-video",
    }:
        # Run before multipart parsing spools a large upload to disk.
        await asyncio.to_thread(cleanup_saved_job_caches)
    return await call_next(request)

# Enable CORS for local development and web clients
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include API Routers
app.include_router(api_router)
app.include_router(video_router)
app.include_router(media_export_router)

# Mount frontend static directory & media directories
ROOT_DIR = Path(__file__).resolve().parent.parent.parent
FRONTEND_DIR = ROOT_DIR / "frontend"
DUBBED_DIR = ROOT_DIR / "storage" / "dubbed"
DUBBED_DIR.mkdir(parents=True, exist_ok=True)
SPLITS_DIR = ROOT_DIR / "storage" / "splits"
SPLITS_DIR.mkdir(parents=True, exist_ok=True)

if FRONTEND_DIR.exists():
    app.mount("/static/dubbed", StaticFiles(directory=str(DUBBED_DIR)), name="dubbed_media")
    app.mount("/static/splits", StaticFiles(directory=str(SPLITS_DIR)), name="splits_media")
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

    @app.get("/")
    async def serve_index():
        return FileResponse(str(FRONTEND_DIR / "index.html"), headers={"Cache-Control": "no-cache"})


@app.get("/health")
async def health_check():
    return {"status": "ok", "app": "Khmer AI Translator"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.app.main:app", host=config.HOST, port=config.PORT, reload=True)
