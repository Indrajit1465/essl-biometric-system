# app/routers/adms.py
"""
ADMS Push Receiver -- eSSL F18 (No Custom Path) Edition
=======================================================

eSSL F18 firmware (confirmed: no path field on device screen)
hardcodes these paths. Server handles ALL variants:

  GET  /iclock/cdata          <- standard ADMS handshake
  POST /iclock/cdata          <- standard ADMS push
  GET  /iclock/gateway.fcgi   <- older eSSL firmware variant
  POST /iclock/gateway.fcgi   <- older eSSL firmware variant
  GET  /                      <- F18 root-only firmware (no path field)
  POST /                      <- F18 root-only firmware (no path field)
  POST /iclock/devicecmd      <- command acknowledgements

All paths share the same two core handlers (_handle_get / _handle_post)
so there is zero logic duplication.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.config import settings
from app.database import get_db
from app.models import Device
from app.adms_parser import build_handshake_response, parse_adms_body
from app.attendance_processor import recompute_daily, save_punches

logger = logging.getLogger(__name__)
router = APIRouter(tags=["ADMS Device"])

# Known eSSL device user-agent fragments
_DEVICE_UA_FRAGMENTS = {"zk", "iface", "bw", "essl", "iclock"}


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _get_or_create_device(db: AsyncSession, serial: str, client_ip: str = "") -> Device:
    result = await db.execute(select(Device).where(Device.serial_number == serial))
    device: Device | None = result.scalar_one_or_none()

    if device is None:
        device = Device(serial_number=serial, name=f"eSSL-{serial[:8]}", ip_address=client_ip)
        db.add(device)
        logger.info("Auto-registered new device SN=%s ip=%s", serial, client_ip)

    # FIX: Do NOT overwrite ip_address from ADMS push requests.
    # The ADMS source IP (client_ip) may differ from the device's direct IP
    # (behind NAT, proxy, or localhost testing). The device's pull IP should
    # only be set manually via POST /api/devices.
    # We just update last_seen_at to track connectivity.

    device.last_seen_at = datetime.now(timezone.utc)
    await db.flush()
    return device


def _extract_sn(request: Request, body_text: str = "") -> str:
    """
    Extract serial number from query params (standard) or body (some F18 firmware).
    L7 FIX: Only parse key=value pairs from the first line of the body,
    not from tab-separated attendance data.
    """
    sn = request.query_params.get("SN", "").strip()
    if not sn and body_text:
        # Only check the first line for SN= (metadata line, not data)
        first_line = body_text.strip().split("\n", 1)[0]
        for part in first_line.split("&"):
            if part.strip().upper().startswith("SN="):
                sn = part.strip()[3:].strip()
                break
    return sn or "UNKNOWN"


def _is_allowed(serial: str) -> bool:
    if not settings.ALLOWED_DEVICE_SERIALS:
        return True
    return serial in settings.ALLOWED_DEVICE_SERIALS


def _looks_like_device(request: Request) -> bool:
    """
    M1 FIX: More robust device detection using known UA fragments and SN param.
    """
    # If SN param is present, it's definitely a device
    if request.query_params.get("SN"):
        return True

    ua = request.headers.get("user-agent", "").lower()

    # Check known eSSL device user-agent fragments
    for frag in _DEVICE_UA_FRAGMENTS:
        if frag in ua:
            return True

    # Fallback: very short UA and no browser indicators
    if len(ua) < 30 and "mozilla" not in ua and "chrome" not in ua:
        return True

    return False


# ── Core GET handler (handshake) ──────────────────────────────────────────────

async def _handle_get(request: Request, db: AsyncSession) -> Response:
    serial    = _extract_sn(request)
    client_ip = request.client.host if request.client else ""

    logger.info("[HANDSHAKE] SN=%s  path=%s  ip=%s", serial, request.url.path, client_ip)

    if not _is_allowed(serial):
        logger.warning("[HANDSHAKE] REJECTED -- SN=%s not in allowlist", serial)
        return Response(status_code=403, content="Unauthorized")

    await _get_or_create_device(db, serial, client_ip)
    config = build_handshake_response(
        device_serial=serial,
        trans_interval=settings.ADMS_TRANS_INTERVAL,
        device_tz=settings.DEVICE_TIMEZONE,
    )
    logger.debug("[HANDSHAKE] Config sent to SN=%s:\n%s", serial, config)

    # M9 FIX: Single commit at end of handler instead of scattered commits
    await db.commit()
    return Response(content=config, media_type="text/plain")


# ── Core POST handler (data push) ─────────────────────────────────────────────

async def _handle_post(request: Request, db: AsyncSession) -> Response:
    raw_bytes  = await request.body()
    body_text  = raw_bytes.decode("utf-8", errors="replace")
    params     = dict(request.query_params)
    serial     = _extract_sn(request, body_text)
    table      = params.get("table", "").strip()
    client_ip  = request.client.host if request.client else ""

    logger.info(
        "[PUSH] SN=%s  table=%s  path=%s  body=%d bytes",
        serial, table or "(none)", request.url.path, len(raw_bytes)
    )

    if not _is_allowed(serial):
        logger.warning("[PUSH] REJECTED -- SN=%s not in allowlist", serial)
        return Response(status_code=403, content="Unauthorized")

    await _get_or_create_device(db, serial, client_ip)

    # Empty body = keepalive heartbeat, not a data push
    if not body_text.strip():
        logger.debug("[PUSH] Empty body from SN=%s -- heartbeat only", serial)
        await db.commit()
        return Response(content="OK: 0", media_type="text/plain")

    # Non-attendance tables (OPERLOG, USERLOG, ATTPHOTO) — acknowledge and skip
    if table and table != "ATTLOG":
        logger.debug("[PUSH] table=%s from SN=%s -- not attendance, skipping", table, serial)
        await db.commit()
        return Response(content="OK: 0", media_type="text/plain")

    # If table param is missing, sniff body for tab-separated attendance lines
    # (Some F18 firmware omits table= when posting to root /)
    if not table:
        tab_lines = [l.strip() for l in body_text.splitlines() if "\t" in l]
        if tab_lines:
            logger.info(
                "[PUSH] SN=%s: table param absent but found %d tab-separated lines "
                "-> treating as ATTLOG",
                serial, len(tab_lines)
            )
            params["table"] = "ATTLOG"
        else:
            logger.warning(
                "[PUSH] SN=%s: no table param and no tab-separated lines.\n"
                "Raw body (first 200 chars): %s",
                serial, body_text[:200]
            )
            await db.commit()
            return Response(content="OK: 0", media_type="text/plain")

    # Parse the ATTLOG payload
    payload = parse_adms_body(
        raw_body=body_text,
        query_params={**params, "SN": serial},
        device_tz_offset=settings.DEVICE_TIMEZONE,
    )

    for err in payload.parse_errors:
        logger.warning("[PUSH] Parse error SN=%s: %s", serial, err)

    if not payload.punches:
        logger.warning(
            "[PUSH] SN=%s: ATTLOG received but 0 punches parsed.\n"
            "Body preview:\n%s",
            serial, body_text[:400]
        )
        await db.commit()
        return Response(content="OK: 0", media_type="text/plain")

    # Save with deduplication (C3: uses savepoints now)
    saved, dupes = await save_punches(db, serial, payload.punches, source="ADMS")
    logger.info(
        "[PUSH] SN=%s: received=%d  saved=%d  duplicates=%d",
        serial, len(payload.punches), saved, dupes
    )

    # Recompute daily attendance for every affected (employee, date) pair
    affected = {(p.employee_id, p.punch_time.date()) for p in payload.punches}
    for emp_id, work_date in affected:
        try:
            record = await recompute_daily(db, emp_id, work_date)
            if record:
                logger.info(
                    "[DAILY] emp=%s  date=%s  status=%s  total_min=%s",
                    emp_id, work_date, record.status, record.total_minutes
                )
            else:
                logger.warning(
                    "[DAILY] SKIPPED emp_device_id='%s' -- no Employee row found.\n"
                    "  Fix: POST /api/employees with device_user_id='%s'",
                    emp_id, emp_id
                )
        except Exception as exc:
            logger.error("[DAILY] Failed emp=%s date=%s: %s", emp_id, work_date, exc)

    # M9 FIX: Single commit at end of handler
    await db.commit()
    return Response(content=f"OK: {saved}", media_type="text/plain")


# ── Route registrations ───────────────────────────────────────────────────────

@router.get("/iclock/cdata")
async def iclock_get(r: Request, db: AsyncSession = Depends(get_db)):
    return await _handle_get(r, db)

@router.post("/iclock/cdata")
async def iclock_post(r: Request, db: AsyncSession = Depends(get_db)):
    return await _handle_post(r, db)

@router.get("/iclock/gateway.fcgi")
async def gateway_get(r: Request, db: AsyncSession = Depends(get_db)):
    return await _handle_get(r, db)

@router.post("/iclock/gateway.fcgi")
async def gateway_post(r: Request, db: AsyncSession = Depends(get_db)):
    return await _handle_post(r, db)

@router.post("/iclock/devicecmd")
async def devicecmd(r: Request):
    body = await r.body()
    logger.debug("[DEVICECMD] %s", body.decode("utf-8", errors="replace")[:200])
    return Response(content="OK", media_type="text/plain")

@router.get("/")
async def root_get(r: Request, db: AsyncSession = Depends(get_db)):
    if _looks_like_device(r):
        return await _handle_get(r, db)
    return Response(content="Attendance System OK", media_type="text/plain")

@router.post("/")
async def root_post(r: Request, db: AsyncSession = Depends(get_db)):
    return await _handle_post(r, db)