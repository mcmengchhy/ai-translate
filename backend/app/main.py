"""Main FastAPI application entrypoint."""

import logging
from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from .api.routes import router as api_router
from .api.video_routes import router as video_router
from .core import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("ai_translator")

app = FastAPI(
    title="Khmer AI Translator & Video Dubbing API",
    description="Next-generation intelligent translation and video voice-dubbing system specialized in Khmer language nuances.",
    version="2.0.0",
)

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
        return FileResponse(str(FRONTEND_DIR / "index.html"))


@app.get("/health")
async def health_check():
    return {"status": "ok", "app": "Khmer AI Translator"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.app.main:app", host=config.HOST, port=config.PORT, reload=True)
