"""API routes for Bilibili authentication and high-speed download configuration."""

import logging
from typing import Optional
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..video.bilibili_auth import (
    get_login_status,
    generate_qr_code,
    poll_qr_code,
    import_manual_cookies,
    import_browser_cookies,
    clear_bilibili_cookies,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/bilibili", tags=["Bilibili Auth"])


class CookieImportRequest(BaseModel):
    cookies: str = Field(..., description="SESSDATA token, raw cookie string, or Netscape cookies.txt content")


class BrowserImportRequest(BaseModel):
    browser: str = Field(default="chrome", description="Browser name: chrome, safari, edge, firefox, brave")


@router.get("/status")
async def bilibili_status(refresh: bool = Query(default=False)):
    """Checks whether Bilibili account is authenticated and returns user profile."""
    try:
        status = await get_login_status(refresh=refresh)
        return status
    except Exception as e:
        logger.error(f"Error checking Bilibili status: {e}")
        return {"logged_in": False, "has_cookies": False, "error": str(e)}


@router.get("/qrcode/generate")
async def bilibili_qrcode_generate():
    """Generates official Bilibili QR code for mobile login scan."""
    try:
        return await generate_qr_code()
    except Exception as e:
        logger.error(f"Failed to generate Bilibili QR code: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/qrcode/poll")
async def bilibili_qrcode_poll(qrcode_key: str = Query(..., description="Bilibili QR code session key")):
    """Polls status of the mobile QR code scan."""
    try:
        result = await poll_qr_code(qrcode_key)
        return result
    except Exception as e:
        logger.error(f"Failed to poll Bilibili QR code: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/cookies")
async def bilibili_import_cookies(req: CookieImportRequest):
    """Manually imports SESSDATA or cookie string and validates against Bilibili."""
    try:
        result = await import_manual_cookies(req.cookies)
        return result
    except Exception as e:
        logger.warning(f"Failed to validate manual cookies: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/browser-import")
async def bilibili_browser_import(req: BrowserImportRequest):
    """Imports Bilibili session cookies from local browser."""
    try:
        result = await import_browser_cookies(req.browser)
        return result
    except Exception as e:
        logger.warning(f"Browser cookie import failed for {req.browser}: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/logout")
async def bilibili_logout():
    """Removes stored Bilibili cookies."""
    clear_bilibili_cookies()
    return {"status": "ok", "message": "Logged out successfully"}

