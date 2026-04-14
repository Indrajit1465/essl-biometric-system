# app/routers/api.py
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Header, Query, Request
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import AttendanceSummary, DailyAttendance, Device, Employee, RawPunchLog
from app.pull_sync import async_pull_and_save, get_device_info, PYZK_AVAILABLE, sync_employees_from_device
from app.attendance_processor import recompute_daily, reprocess_all_pending, recompute_today, _fmt_ist, IST_OFFSET, _ist_day_bounds_utc
from app.schemas import EmployeeCreate, DeviceRegister

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["API"])

IST = timezone(IST_OFFSET)


def _today_ist() -> date:
    return datetime.now(IST).date()


def _fmt_late(minutes: int) -> str:
    """Human-readable late duration: <60 -> '45m', >=60 -> '2h 30m'."""
    if minutes <= 0:
        return "0m"
    if minutes < 60:
        return f"{minutes}m"
    h, m = divmod(minutes, 60)
    return f"{h}h {m}m" if m else f"{h}h"


def _naive_utc_range(s: date, e: date):
    """C4 FIX: Convert IST date range to naive-UTC datetime bounds."""
    start = datetime.combine(s, datetime.min.time()) - IST_OFFSET
    end = datetime.combine(e, datetime.max.time()) - IST_OFFSET
    return start, end


def verify_api_key(x_api_key: str = Header(...)):
    if x_api_key != settings.API_SECRET_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


# ── Rate limiting setup ───────────────────────────────────────────────────────
# L9: Basic in-memory rate limiting
_rate_store: dict[str, list[float]] = {}


def _check_rate_limit(request: Request):
    """L9: Simple per-IP rate limiter."""
    client_ip = request.client.host if request.client else "unknown"
    now = datetime.now().timestamp()
    window = 60.0  # 1 minute window
    limit = settings.RATE_LIMIT_PER_MINUTE

    if client_ip not in _rate_store:
        _rate_store[client_ip] = []

    # Clean old entries
    _rate_store[client_ip] = [t for t in _rate_store[client_ip] if now - t < window]

    if len(_rate_store[client_ip]) >= limit:
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    _rate_store[client_ip].append(now)


# ── Attendance Summary (new clean table) ──────────────────────────────────────

@router.get("/attendance/summary")
async def get_attendance_summary(
    request: Request,
    start_date:  date = Query(..., description="YYYY-MM-DD"),
    end_date:    date = Query(..., description="YYYY-MM-DD"),
    employee_id: Optional[str] = Query(None),
    status:      Optional[str] = Query(None, description="PRESENT/LATE/ABSENT/HALF_DAY"),
    db: AsyncSession = Depends(get_db),
    _=Depends(verify_api_key),
):
    """
    Clean attendance summary -- EMP_ID, EMP_Name, Date, Punch-In, Punch-Out, Is-Late.
    All times in IST HH:MM format.
    """
    _check_rate_limit(request)
    q = (
        select(AttendanceSummary)
        .where(
            AttendanceSummary.work_date >= start_date,
            AttendanceSummary.work_date <= end_date,
        )
        .order_by(AttendanceSummary.work_date, AttendanceSummary.emp_name)
    )
    if employee_id:
        q = q.where(AttendanceSummary.emp_id == employee_id)
    if status:
        q = q.where(AttendanceSummary.status == status.upper())

    rows = (await db.execute(q)).scalars().all()
    return [
        {
            "EMP_ID":     r.emp_id,
            "EMP_Name":   r.emp_name,
            "DATE":       r.work_date.isoformat(),
            "PUNCH_IN":   r.punch_in  or "--:--",
            "PUNCH_OUT":  r.punch_out or "--:--",
            "IS_LATE":    r.is_late,
            "LATE_MIN":   _fmt_late(r.late_minutes),
            "TOTAL_HRS":  r.total_hours,
            "STATUS":     r.status,
        }
        for r in rows
    ]


@router.get("/attendance/today")
async def get_today_attendance(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _=Depends(verify_api_key),
):
    """
    Returns today's attendance. Automatically recomputes for any employee
    who punched in today but doesn't have a summary entry yet.
    """
    _check_rate_limit(request)
    # Auto-process any punches that arrived since last recompute
    await recompute_today(db)

    today = _today_ist()
    rows = (await db.execute(
        select(AttendanceSummary)
        .where(AttendanceSummary.work_date == today)
        .order_by(AttendanceSummary.emp_name)
    )).scalars().all()
    return [
        {
            "EMP_ID":    r.emp_id,
            "EMP_Name":  r.emp_name,
            "DATE":      r.work_date.isoformat(),
            "PUNCH_IN":  r.punch_in  or "--:--",
            "PUNCH_OUT": r.punch_out or "--:--",
            "IS_LATE":   r.is_late,
            "LATE_MIN":  _fmt_late(r.late_minutes),
            "TOTAL_HRS": r.total_hours,
            "STATUS":    r.status,
        }
        for r in rows
    ]


@router.get("/attendance/report")
async def get_attendance_report(
    request: Request,
    start_date:  date = Query(...),
    end_date:    date = Query(...),
    employee_id: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
    _=Depends(verify_api_key),
):
    """Full attendance report -- delegates to summary table."""
    return await get_attendance_summary(request, start_date, end_date, employee_id, None, db, None)


@router.get("/attendance/raw/{employee_id}")
async def get_raw_punches(
    request: Request,
    employee_id: str,
    start_date:  date = Query(...),
    end_date:    date = Query(...),
    db: AsyncSession = Depends(get_db),
    _=Depends(verify_api_key),
):
    """Raw punch log for one employee -- times in IST HH:MM."""
    _check_rate_limit(request)
    # C4 FIX: Use IST-adjusted UTC range
    day_start, day_end = _naive_utc_range(start_date, end_date)
    punches = (await db.execute(
        select(RawPunchLog)
        .where(
            RawPunchLog.employee_device_id == employee_id,
            RawPunchLog.punch_time >= day_start,
            RawPunchLog.punch_time <= day_end,
        )
        .order_by(RawPunchLog.punch_time)
    )).scalars().all()

    STATUS = {0:"CHECK_IN", 1:"CHECK_OUT", 2:"BREAK_OUT", 3:"BREAK_IN", 4:"OT_IN", 5:"OT_OUT"}
    VERIFY = {1:"FINGERPRINT", 3:"PASSWORD", 11:"FACE", 200:"RFID", 255:"CARD/OTHER"}
    return [
        {
            "date":        (p.punch_time + IST_OFFSET).strftime("%Y-%m-%d") if p.punch_time else None,
            "time_ist":    _fmt_ist(p.punch_time),
            "status":      STATUS.get(p.status, str(p.status)),
            "verify_type": VERIFY.get(p.verify_type, str(p.verify_type)),
            "device":      p.device_serial,
            "source":      p.source,
        }
        for p in punches
    ]


@router.post("/attendance/recompute")
async def trigger_recompute(
    request: Request,
    employee_id: Optional[str] = Query(None),
    work_date:   Optional[date] = Query(None),
    db: AsyncSession = Depends(get_db),
    _=Depends(verify_api_key),
):
    _check_rate_limit(request)
    if employee_id and work_date:
        try:
            rec = await recompute_daily(db, employee_id, work_date)
            await db.commit()
            return {"recomputed": 1, "status": rec.status if rec else "employee_not_found"}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))
    try:
        count = await reprocess_all_pending(db)
        return {"recomputed": count, "message": f"Processed {count} employee-day pairs"}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── Dashboard stats ────────────────────────────────────────────────────────────

@router.get("/dashboard/summary")
async def dashboard_summary(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _=Depends(verify_api_key),
):
    _check_rate_limit(request)
    today = _today_ist()
    sc = (await db.execute(
        select(AttendanceSummary.status, func.count().label("n"))
        .where(AttendanceSummary.work_date == today)
        .group_by(AttendanceSummary.status)
    )).all()
    by_status = {r.status: r.n for r in sc}
    # L10 FIX: Use .is_(True) for SQLAlchemy boolean comparisons
    emp_n = (await db.execute(select(func.count()).select_from(Employee).where(Employee.is_active.is_(True)))).scalar()
    dev_n = (await db.execute(select(func.count()).select_from(Device).where(Device.is_active.is_(True)))).scalar()
    pending = (await db.execute(select(func.count()).select_from(RawPunchLog).where(RawPunchLog.is_processed.is_(False)))).scalar()
    return {
        "date": today.isoformat(),
        "total_employees": emp_n,
        "total_devices":   dev_n,
        "today": {
            "present":  by_status.get("PRESENT", 0),
            "late":     by_status.get("LATE", 0),
            "half_day": by_status.get("HALF_DAY", 0),
            "absent":   max(0, emp_n - sum(by_status.values())),
        },
        "pending_punches": pending,
    }


@router.get("/dashboard/trend")
async def dashboard_trend(
    request: Request,
    days: int = Query(7, ge=1, le=30),
    db: AsyncSession = Depends(get_db),
    _=Depends(verify_api_key),
):
    """Last N days attendance counts -- for the line chart."""
    _check_rate_limit(request)
    today = _today_ist()
    start = today - timedelta(days=days - 1)
    rows = (await db.execute(
        select(
            AttendanceSummary.work_date,
            AttendanceSummary.status,
            func.count().label("n"),
        )
        .where(AttendanceSummary.work_date >= start)
        .group_by(AttendanceSummary.work_date, AttendanceSummary.status)
        .order_by(AttendanceSummary.work_date)
    )).all()

    by_date: dict = {}
    for r in rows:
        d = r.work_date.isoformat() if hasattr(r.work_date, "isoformat") else str(r.work_date)
        if d not in by_date:
            by_date[d] = {"date": d, "present": 0, "late": 0, "absent": 0, "half_day": 0}
        key = r.status.lower() if r.status.lower() in ("present","late","absent","half_day") else "absent"
        by_date[d][key] = r.n
    return list(by_date.values())


# ── Devices ────────────────────────────────────────────────────────────────────

@router.get("/devices")
async def list_devices(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _=Depends(verify_api_key),
):
    _check_rate_limit(request)
    devs = (await db.execute(select(Device).where(Device.is_active.is_(True)))).scalars().all()
    return [{"serial": d.serial_number, "name": d.name, "location": d.location,
             "ip": d.ip_address, "last_seen": d.last_seen_at.isoformat() if d.last_seen_at else None}
            for d in devs]


@router.post("/devices")
async def register_device(
    request: Request,
    payload: DeviceRegister,
    db: AsyncSession = Depends(get_db),
    _=Depends(verify_api_key),
):
    """H7 FIX: Uses Pydantic schema instead of raw dict."""
    _check_rate_limit(request)
    serial = payload.serial_number
    dev = (await db.execute(select(Device).where(Device.serial_number == serial))).scalar_one_or_none()
    if dev:
        dev.name = payload.name or dev.name
        dev.ip_address = payload.ip_address or dev.ip_address
        dev.port = payload.port
        dev.is_active = True
        action = "updated"
    else:
        dev = Device(
            serial_number=serial,
            name=payload.name or f"eSSL-{serial[:8]}",
            location=payload.location or "",
            ip_address=payload.ip_address or "",
            port=payload.port,
            protocol="ADMS",
            is_active=True,
        )
        db.add(dev)
        action = "created"
    await db.flush()
    await db.commit()
    return {"action": action, "serial_number": dev.serial_number, "ip_address": dev.ip_address}


@router.post("/devices/{serial}/sync-employees")
async def sync_employees(
    request: Request,
    serial: str,
    db: AsyncSession = Depends(get_db),
    _=Depends(verify_api_key),
):
    _check_rate_limit(request)
    if not PYZK_AVAILABLE:
        raise HTTPException(501, "pyzk not installed")
    dev = (await db.execute(select(Device).where(Device.serial_number == serial))).scalar_one_or_none()
    if not dev or not dev.ip_address:
        raise HTTPException(404, "Device not found or no IP")
    try:
        stats = await sync_employees_from_device(db, dev.ip_address, dev.port or 4370)
        await db.commit()
        return {"device": serial, **stats}
    except Exception as exc:
        logger.error("sync_employees failed for %s: %s", serial, exc)
        raise HTTPException(502, str(exc))


@router.post("/devices/{serial}/pull")
async def pull_device(
    request: Request,
    serial: str,
    since_date: Optional[date] = Query(None),
    db: AsyncSession = Depends(get_db),
    _=Depends(verify_api_key),
):
    _check_rate_limit(request)
    if not PYZK_AVAILABLE:
        raise HTTPException(501, "pyzk not installed")
    dev = (await db.execute(select(Device).where(Device.serial_number == serial))).scalar_one_or_none()
    if not dev or not dev.ip_address:
        raise HTTPException(404, "Device not found or no IP")
    try:
        stats = await async_pull_and_save(db=db, ip=dev.ip_address, device_serial=serial,
                                           port=dev.port or 4370, since_date=since_date)
        return {"device": serial, **stats}
    except Exception as exc:
        logger.error("pull_device failed for %s: %s", serial, exc)
        raise HTTPException(502, str(exc))


@router.get("/devices/{serial}/info")
async def device_info(
    request: Request,
    serial: str,
    db: AsyncSession = Depends(get_db),
    _=Depends(verify_api_key),
):
    _check_rate_limit(request)
    if not PYZK_AVAILABLE:
        raise HTTPException(501, "pyzk not installed")
    dev = (await db.execute(select(Device).where(Device.serial_number == serial))).scalar_one_or_none()
    if not dev or not dev.ip_address:
        raise HTTPException(404, "Device not found")
    try:
        # H6 FIX: Use asyncio.to_thread instead of deprecated get_event_loop()
        info = await asyncio.to_thread(get_device_info, dev.ip_address, dev.port or 4370)
        return {"serial_number": info.serial_number, "firmware": info.firmware,
                "enrolled_users": info.user_count, "stored_logs": info.attendance_count}
    except Exception as exc:
        logger.error("device_info failed for %s: %s", serial, exc)
        raise HTTPException(502, str(exc))


# ── Employees ──────────────────────────────────────────────────────────────────

@router.get("/employees")
async def list_employees(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _=Depends(verify_api_key),
):
    _check_rate_limit(request)
    emps = (await db.execute(
        select(Employee).where(Employee.is_active.is_(True)).order_by(Employee.name)
    )).scalars().all()
    return [{"id": e.id, "device_user_id": e.device_user_id, "employee_code": e.employee_code,
             "name": e.name, "department": e.department, "shift_start": e.shift_start, "shift_end": e.shift_end}
            for e in emps]


@router.post("/employees")
async def create_or_update_employee(
    request: Request,
    payload: EmployeeCreate,
    db: AsyncSession = Depends(get_db),
    _=Depends(verify_api_key),
):
    """H7 FIX: Uses Pydantic schema instead of raw dict."""
    _check_rate_limit(request)
    did = payload.device_user_id.strip()
    emp = (await db.execute(select(Employee).where(Employee.device_user_id == did))).scalar_one_or_none()
    if emp:
        emp.name = payload.name or emp.name
        emp.department = payload.department or emp.department
        emp.shift_start = payload.shift_start
        emp.shift_end = payload.shift_end
        emp.grace_minutes = payload.grace_minutes
        if payload.employee_code:
            emp.employee_code = payload.employee_code
        action = "updated"
    else:
        emp = Employee(
            device_user_id=did,
            name=payload.name or f"Employee {did}",
            employee_code=payload.employee_code,
            department=payload.department,
            shift_start=payload.shift_start,
            shift_end=payload.shift_end,
            grace_minutes=payload.grace_minutes,
        )
        db.add(emp)
        action = "created"
    await db.flush()
    await db.commit()
    return {"action": action, "id": emp.id, "device_user_id": emp.device_user_id, "name": emp.name}