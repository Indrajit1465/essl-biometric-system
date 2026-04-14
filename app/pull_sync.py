# app/pull_sync.py
"""
pyzk Pull Integration -- attendance logs AND employee names from device

Fixes applied:
  H6: Uses asyncio.to_thread() instead of deprecated get_event_loop()
  M6: Retry logic with exponential backoff for device connections
  M8: Warns on truncated employee names
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, date, timezone, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from zk import ZK, const as zk_const
    PYZK_AVAILABLE = True
except ImportError:
    PYZK_AVAILABLE = False
    logger.warning("pyzk not installed. Run: pip install pyzk")

from app.config import settings
from app.attendance_processor import save_punches
from app.adms_parser import ParsedPunch

IST = timezone(timedelta(hours=5, minutes=30))

# M8: Threshold for detecting truncated names from device
_NAME_TRUNCATION_THRESHOLD = 24


@dataclass
class DeviceInfo:
    serial_number: str
    firmware: str
    platform: str
    user_count: int
    attendance_count: int


def _make_zk(ip: str, port: int = 4370, password: int = 0) -> "ZK":
    if not PYZK_AVAILABLE:
        raise RuntimeError("pyzk not installed. Run: pip install pyzk")
    return ZK(ip, port=port, timeout=settings.PULL_DEVICE_TIMEOUT,
               password=password, force_udp=False, ommit_ping=False)


def _with_retry(func, *args, max_retries: int = 3, **kwargs):
    """
    M6: Execute a function with exponential backoff retry on failure.
    Used for all device TCP connections to handle transient network issues.
    """
    last_exc = None
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            last_exc = e
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                logger.warning(
                    "Device connection failed (attempt %d/%d), retrying in %ds: %s",
                    attempt + 1, max_retries, wait, e
                )
                time.sleep(wait)
            else:
                logger.error(
                    "Device connection failed after %d attempts: %s",
                    max_retries, e
                )
    raise last_exc


# ── Pull employee list from device ────────────────────────────────────────────

def pull_users_from_device(ip: str, port: int = 4370) -> list[dict]:
    """
    Fetch all enrolled users from the device.
    Returns list of dicts with uid, user_id, name, privilege.
    """
    def _do_pull():
        zk = _make_zk(ip, port)
        conn = None
        users = []
        try:
            conn = zk.connect()
            conn.disable_device()
            raw_users = conn.get_users()
            logger.info("Fetched %d users from device", len(raw_users))
            for u in raw_users:
                name = u.name.strip() if u.name else ""

                # M8: Warn if name appears truncated
                if name and len(name) >= _NAME_TRUNCATION_THRESHOLD:
                    logger.warning(
                        "Employee name may be truncated (len=%d): id=%s name=%r",
                        len(name), u.user_id, name
                    )

                users.append({
                    "uid":      str(u.uid),
                    "user_id":  str(u.user_id),
                    "name":     name,
                    "privilege": getattr(u, "privilege", 0),
                })
        finally:
            if conn:
                try:
                    conn.enable_device()
                    conn.disconnect()
                except Exception:
                    pass
        return users

    return _with_retry(_do_pull)


async def sync_employees_from_device(db, ip: str, port: int = 4370) -> dict:
    """
    Pull users from device and upsert into employees table.
    Preserves existing shift/department settings.
    Returns summary dict.
    """
    from sqlalchemy import select
    from app.models import Employee

    # H6 FIX: Use asyncio.to_thread() instead of deprecated get_event_loop()
    users = await asyncio.to_thread(pull_users_from_device, ip, port)

    created = updated = skipped = 0
    for u in users:
        device_id = u["user_id"]
        name      = u["name"] or f"Employee {device_id}"

        result = await db.execute(
            select(Employee).where(Employee.device_user_id == device_id)
        )
        emp: Employee | None = result.scalar_one_or_none()

        if emp is None:
            emp = Employee(
                device_user_id=device_id,
                name=name,
                employee_code=f"EMP{str(device_id).zfill(4)}",
                department="Unassigned",
                shift_start="09:00",
                shift_end="18:00",
                grace_minutes=15,
                is_active=True,
            )
            db.add(emp)
            created += 1
            logger.info("Created employee: id=%s name=%r", device_id, name)
        else:
            # Only update name if device has a real name and current is placeholder
            if name and (not emp.name or emp.name.startswith("Employee ")):
                emp.name = name
                updated += 1
            else:
                skipped += 1

    await db.flush()
    return {"total_on_device": len(users), "created": created,
            "updated": updated, "skipped": skipped}


# ── Pull attendance logs ───────────────────────────────────────────────────────

def pull_attendance_logs(ip: str, port: int = 4370,
                         since_date: Optional[date] = None) -> list[ParsedPunch]:
    def _do_pull():
        zk = _make_zk(ip, port)
        conn = None
        try:
            logger.info("Connecting to device at %s:%d...", ip, port)
            conn = zk.connect()
            conn.disable_device()
            raw = conn.get_attendance()
            logger.info("Fetched %d raw records from device", len(raw))

            punches = []
            for r in raw:
                naive_dt: datetime = r.timestamp
                # Device clock is IST -- convert to UTC for storage
                ist_dt = naive_dt.replace(tzinfo=IST)
                punch_time = (ist_dt.astimezone(timezone.utc)).replace(tzinfo=None)  # naive UTC
                if since_date and punch_time.date() < since_date:
                    continue
                punches.append(ParsedPunch(
                    uid=str(r.uid),
                    employee_id=str(r.user_id),
                    punch_time=punch_time,
                    status=r.status,
                    verify_type=r.punch,
                    raw_line=f"PULL:{r.uid}\t{r.user_id}\t{r.timestamp}\t{r.status}\t{r.punch}",
                ))
            logger.info("Pull complete: %d records (after filter)", len(punches))
            return punches
        finally:
            if conn:
                try:
                    conn.enable_device()
                    conn.disconnect()
                except Exception:
                    pass

    # M6: Retry on failure
    return _with_retry(_do_pull)


def get_device_info(ip: str, port: int = 4370) -> DeviceInfo:
    def _do_info():
        zk = _make_zk(ip, port)
        conn = None
        try:
            conn = zk.connect()
            conn.disable_device()
            return DeviceInfo(
                serial_number=conn.get_serialnumber(),
                firmware=conn.get_firmware_version(),
                platform=conn.get_platform(),
                user_count=len(conn.get_users()),
                attendance_count=len(conn.get_attendance()),
            )
        finally:
            if conn:
                try:
                    conn.enable_device()
                    conn.disconnect()
                except Exception:
                    pass

    return _with_retry(_do_info)


async def async_pull_and_save(db, ip: str, device_serial: str,
                               port: int = 4370,
                               since_date: Optional[date] = None) -> dict:
    # H6 FIX: Use asyncio.to_thread() instead of deprecated get_event_loop()
    punches = await asyncio.to_thread(pull_attendance_logs, ip, port, since_date)
    saved, dupes = await save_punches(db, device_serial, punches, source="PULL")
    await db.commit()
    return {"fetched": len(punches), "saved": saved, "duplicates": dupes}