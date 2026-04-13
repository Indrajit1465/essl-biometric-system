# main.py
import logging, sys
from contextlib import asynccontextmanager
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import os

from app.config import settings
from app.database import create_tables
from app.device_logger import DeviceRequestLoggerMiddleware
from app.routers.adms import router as adms_router
from app.routers.api  import router as api_router

_fmt = "%(asctime)s [%(levelname)s] %(name)s - %(message)s"
_sh  = logging.StreamHandler(sys.stdout)
_sh.setFormatter(logging.Formatter(_fmt))
_fh  = logging.FileHandler("attendance.log", encoding="utf-8")
_fh.setFormatter(logging.Formatter(_fmt))
logging.basicConfig(level=logging.INFO, handlers=[_sh, _fh])

if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Attendance Management System")
    await create_tables()
    logger.info("Database ready | ADMS → http://%s:%d/iclock/cdata", settings.HOST, settings.PORT)
    logger.info("Dashboard      → http://%s:%d/dashboard", settings.HOST, settings.PORT)
    logger.info("API Docs       → http://%s:%d/docs", settings.HOST, settings.PORT)
    yield
    logger.info("Shutdown")


app = FastAPI(title="Attendance System", version="1.0.0", lifespan=lifespan)

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
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
    return {"status": "ok"}

if __name__ == "__main__":
    uvicorn.run("main:app", host=settings.HOST, port=settings.PORT, reload=False, log_level="info")