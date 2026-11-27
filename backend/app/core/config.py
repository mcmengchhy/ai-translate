"""Application configuration settings."""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env file from project root if present
ROOT_DIR = Path(__file__).resolve().parent.parent.parent.parent
load_dotenv(ROOT_DIR / ".env")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
DEFAULT_ENGINE = os.getenv("DEFAULT_ENGINE", "auto")
PORT = int(os.getenv("PORT", "8000"))
HOST = os.getenv("HOST", "0.0.0.0")
VIDEO_MAX_CONCURRENT_JOBS = max(1, int(os.getenv("VIDEO_MAX_CONCURRENT_JOBS", "1")))
TTS_MAX_CONCURRENT_REQUESTS = max(1, int(os.getenv("TTS_MAX_CONCURRENT_REQUESTS", "4")))
TTS_REQUEST_INTERVAL = max(0.0, float(os.getenv("TTS_REQUEST_INTERVAL", "0.2")))
