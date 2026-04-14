# main.py
"""
Attendance Management System — FastAPI Application Entry Point

Production fixes applied:
  H1: CORS restricted to configured origins
  L1: UTF-8 encoding fix for Windows console
  L2: Log rotation (10MB max, 5 backups)
  L12: Health endpoint checks DB connectivity
  H5/M7: Background scheduler for device health monitoring
"""
import logging
import sys
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import os

from app.config import settings
from app.database import create_tables, check_db_health
from app.device_logger import DeviceRequestLoggerMiddleware
from app.routers.adms import router as adms_router
from app.routers.api  import router as api_router

# ── Logging setup ─────────────────────────────────────────────────────────────
# L1 FIX: Use ASCII-safe format, avoid unicode arrows
_fmt = "%(asctime)s [%(levelname)s] %(name)s - %(message)s"

_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(logging.Formatter(_fmt))

# L2 FIX: RotatingFileHandler instead of plain FileHandler
_fh = RotatingFileHandler(
    "attendance.log",
    maxBytes=10 * 1024 * 1024,   # 10 MB per file
    backupCount=5,
    encoding="utf-8",
)
_fh.setFormatter(logging.Formatter(_fmt))

logging.basicConfig(level=logging.INFO, handlers=[_sh, _fh])

# L1 FIX: Force UTF-8 stdout on Windows
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

logger = logging.getLogger(__name__)

# Scheduler reference for graceful shutdown
_scheduler = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _scheduler

    logger.info("Starting Attendance Management System")
    await create_tables()
    logger.info("Database ready | ADMS -> http://%s:%d/iclock/cdata", settings.HOST, settings.PORT)
    logger.info("Dashboard      -> http://%s:%d/dashboard", settings.HOST, settings.PORT)
    logger.info("API Docs       -> http://%s:%d/docs", settings.HOST, settings.PORT)

    # H5/M7: Start background scheduler for device health monitoring
    from app.scheduler import setup_scheduler
    _scheduler = setup_scheduler()

    yield

    # Graceful shutdown
    if _scheduler:
        _scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")
    logger.info("Shutdown complete")


app = FastAPI(title="Attendance System", version="2.0.0", lifespan=lifespan)

# H1 FIX: CORS restricted to configured origins instead of allow_origins=["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["X-API-Key", "Content-Type"],
    allow_credentials=False,
)
app.add_middleware(DeviceRequestLoggerMiddleware)

app.include_router(adms_router)
app.include_router(api_router)

# Serve the frontend dashboard
FRONTEND = os.path.join(os.path.dirname(__file__), "frontend")
if os.path.isdir(FRONTEND):
    app.mount("/static", StaticFiles(directory=FRONTEND), name="static")

@app.get("/dashboard", include_in_schema=False)
async def dashboard():
    html = os.path.join(FRONTEND, "dashboard.html")
    return FileResponse(html) if os.path.exists(html) else {"error": "frontend not found"}

@app.get("/health", tags=["System"])
async def health():
    """L12 FIX: Health endpoint checks actual DB connectivity."""
    db_ok = await check_db_health()
    if db_ok:
        return {"status": "ok", "database": "connected"}
    return {"status": "degraded", "database": "unreachable"}

if __name__ == "__main__":
    uvicorn.run("main:app", host=settings.HOST, port=settings.PORT, reload=False, log_level="info")