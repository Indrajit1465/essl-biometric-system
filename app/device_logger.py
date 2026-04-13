# app/device_logger.py
"""
Raw Device Request Logger - Windows-safe (ASCII only)
"""
from __future__ import annotations
import logging
import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("device_logger")

LOG_DEVICE_REQUESTS = os.getenv("LOG_DEVICE_REQUESTS", "true").lower() == "true"

_SKIP_PATHS = {"/docs", "/openapi.json", "/redoc", "/health", "/favicon.ico"}


class DeviceRequestLoggerMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        if not LOG_DEVICE_REQUESTS or request.url.path in _SKIP_PATHS:
            return await call_next(request)

        raw_body = await request.body()
        body_text = raw_body.decode("utf-8", errors="replace").strip()

        sn = request.query_params.get("SN", "")
        ua = request.headers.get("user-agent", "").lower()
        is_browser = "mozilla" in ua or "chrome" in ua or "safari" in ua

        is_device_request = (
            request.url.path.startswith("/iclock")
            or bool(sn)
            or (request.url.path == "/" and not is_browser)
        )

        if is_device_request:
            sep = "=" * 52
            logger.info(
                "\n%s\n  DEVICE REQUEST  %s %s\n%s\n"
                "  Client : %s\n"
                "  SN     : %s\n"
                "  Params : %s\n"
                "  Body   : %s\n"
                "  %s",
                sep,
                request.method,
                request.url.path,
                sep,
                f"{request.client.host}:{request.client.port}" if request.client else "unknown",
                sn or "(not in query params - check body)",
                str(request.query_params) or "(none)",
                body_text[:300] if body_text else "(EMPTY - handshake only)",
                "-" * 52,
            )

            if not sn and body_text:
                for part in body_text.split("&"):
                    if part.strip().upper().startswith("SN="):
                        logger.warning("  SN found in BODY: %s", part.strip())

        async def receive():
            return {"type": "http.request", "body": raw_body, "more_body": False}

        request._receive = receive
        return await call_next(request)