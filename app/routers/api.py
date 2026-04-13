# app/routers/api.py
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Header, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import AttendanceSummary, DailyAttendance, Device, Employee, RawPunchLog
from app.pull_sync import async_pull_and_save, get_device_info, PYZK_AVAILABLE, sync_employees_from_device
from app.attendance_processor import recompute_daily, reprocess_all_pending, recompute_today, _fmt_ist, IST_OFFSET

router = APIRouter(prefix="/api", tags=["API"])

IST = timezone(IST_OFFSET)


def _today_ist() -> date:
    return datetime.now(IST).date()


def _naive_utc_range(s: date, e: date):
    return datetime.combine(s, datetime.min.time()), datetime.combine(e, datetime.max.time())


def verify_api_key(x_api_key: str = Header(...)):
    if x_api_key != settings.API_SECRET_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


# ── Attendance Summary (new clean table) ──────────────────────────────────────

@router.get("/attendance/summary")
async def get_attendance_summary(
    start_date:  date = Query(..., description="YYYY-MM-DD"),
    end_date:    date = Query(..., description="YYYY-MM-DD"),
    employee_id: Optional[str] = Query(None),
    status:      Optional[str] = Query(None, description="PRESENT/LATE/ABSENT/HALF_DAY"),
    db: AsyncSession = Depends(get_db),
    _=Depends(verify_api_key),
):
    """
    Clean attendance summary — EMP_ID, EMP_Name, Date, Punch-In, Punch-Out, Is-Late.
    All times in IST HH:MM format.
    """
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
            "LATE_MIN":   r.late_minutes,
            "TOTAL_HRS":  r.total_hours,
            "STATUS":     r.status,
        }
        for r in rows
    ]


@router.get("/attendance/today")
async def get_today_attendance(db: AsyncSession = Depends(get_db), _=Depends(verify_api_key)):
    """
    Returns today's attendance. Automatically recomputes for any employee
    who punched in today but doesn't have a summary entry yet — fixes the
    "only 39 of 60+ employees showing" problem.
    """
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
            "LATE_MIN":  r.late_minutes,
            "TOTAL_HRS": r.total_hours,
            "STATUS":    r.status,
        }
        for r in rows
    ]


@router.get("/attendance/report")
async def get_attendance_report(
    start_date:  date = Query(...),
    end_date:    date = Query(...),
    employee_id: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
    _=Depends(verify_api_key),
):
    """Full attendance report — delegates to summary table."""
    return await get_attendance_summary(start_date, end_date, employee_id, None, db, None)


@router.get("/attendance/raw/{employee_id}")
async def get_raw_punches(
    employee_id: str,
    start_date:  date = Query(...),
    end_date:    date = Query(...),
    db: AsyncSession = Depends(get_db),
    _=Depends(verify_api_key),
):
    """Raw punch log for one employee — times in IST HH:MM."""
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
            "date":        (_fmt_ist(p.punch_time) and
                           (p.punch_time + IST_OFFSET).strftime("%Y-%m-%d") if p.punch_time else None),
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
    employee_id: Optional[str] = Query(None),
    work_date:   Optional[date] = Query(None),
    db: AsyncSession = Depends(get_db),
    _=Depends(verify_api_key),
):
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
async def dashboard_summary(db: AsyncSession = Depends(get_db), _=Depends(verify_api_key)):
    today = _today_ist()
    sc = (await db.execute(
        select(AttendanceSummary.status, func.count().label("n"))
        .where(AttendanceSummary.work_date == today)
        .group_by(AttendanceSummary.status)
    )).all()
    by_status = {r.status: r.n for r in sc}
    emp_n = (await db.execute(select(func.count()).select_from(Employee).where(Employee.is_active==True))).scalar()
    dev_n = (await db.execute(select(func.count()).select_from(Device).where(Device.is_active==True))).scalar()
    pending = (await db.execute(select(func.count()).select_from(RawPunchLog).where(RawPunchLog.is_processed==False))).scalar()
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
    days: int = Query(7, ge=1, le=30),
    db: AsyncSession = Depends(get_db),
    _=Depends(verify_api_key),
):
    """Last N days attendance counts — for the line chart."""
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
async def list_devices(db: AsyncSession = Depends(get_db), _=Depends(verify_api_key)):
    devs = (await db.execute(select(Device).where(Device.is_active==True))).scalars().all()
    return [{"serial": d.serial_number, "name": d.name, "location": d.location,
             "ip": d.ip_address, "last_seen": d.last_seen_at.isoformat() if d.last_seen_at else None}
            for d in devs]


@router.post("/devices")
async def register_device(payload: dict, db: AsyncSession = Depends(get_db), _=Depends(verify_api_key)):
    serial = payload.get("serial_number", "").strip()
    if not serial:
        raise HTTPException(400, "serial_number required")
    dev = (await db.execute(select(Device).where(Device.serial_number==serial))).scalar_one_or_none()
    if dev:
        dev.name=payload.get("name",dev.name); dev.ip_address=payload.get("ip_address",dev.ip_address)
        dev.port=int(payload.get("port",dev.port or 4370)); dev.is_active=True; action="updated"
    else:
        dev = Device(serial_number=serial, name=payload.get("name",f"eSSL-{serial[:8]}"),
                     location=payload.get("location",""), ip_address=payload.get("ip_address",""),
                     port=int(payload.get("port",4370)), protocol="ADMS", is_active=True)
        db.add(dev); action="created"
    await db.flush(); await db.commit()
    return {"action":action,"serial_number":dev.serial_number,"ip_address":dev.ip_address}


@router.post("/devices/{serial}/sync-employees")
async def sync_employees(serial: str, db: AsyncSession=Depends(get_db), _=Depends(verify_api_key)):
    if not PYZK_AVAILABLE:
        raise HTTPException(501, "pyzk not installed")
    dev = (await db.execute(select(Device).where(Device.serial_number==serial))).scalar_one_or_none()
    if not dev or not dev.ip_address:
        raise HTTPException(404, "Device not found or no IP")
    try:
        stats = await sync_employees_from_device(db, dev.ip_address, dev.port or 4370)
        await db.commit()
        return {"device": serial, **stats}
    except Exception as exc:
        raise HTTPException(502, str(exc))


@router.post("/devices/{serial}/pull")
async def pull_device(
    serial: str,
    since_date: Optional[date] = Query(None),
    db: AsyncSession = Depends(get_db),
    _=Depends(verify_api_key),
):
    if not PYZK_AVAILABLE:
        raise HTTPException(501, "pyzk not installed")
    dev = (await db.execute(select(Device).where(Device.serial_number==serial))).scalar_one_or_none()
    if not dev or not dev.ip_address:
        raise HTTPException(404, "Device not found or no IP")
    try:
        stats = await async_pull_and_save(db=db, ip=dev.ip_address, device_serial=serial,
                                           port=dev.port or 4370, since_date=since_date)
        return {"device": serial, **stats}
    except Exception as exc:
        raise HTTPException(502, str(exc))


@router.get("/devices/{serial}/info")
async def device_info(serial: str, db: AsyncSession=Depends(get_db), _=Depends(verify_api_key)):
    if not PYZK_AVAILABLE:
        raise HTTPException(501, "pyzk not installed")
    dev = (await db.execute(select(Device).where(Device.serial_number==serial))).scalar_one_or_none()
    if not dev or not dev.ip_address:
        raise HTTPException(404, "Device not found")
    import asyncio
    try:
        info = await asyncio.get_event_loop().run_in_executor(
            None, lambda: get_device_info(dev.ip_address, dev.port or 4370))
        return {"serial_number":info.serial_number,"firmware":info.firmware,
                "enrolled_users":info.user_count,"stored_logs":info.attendance_count}
    except Exception as exc:
        raise HTTPException(502, str(exc))


# ── Employees ──────────────────────────────────────────────────────────────────

@router.get("/employees")
async def list_employees(db: AsyncSession=Depends(get_db), _=Depends(verify_api_key)):
    emps = (await db.execute(select(Employee).where(Employee.is_active==True).order_by(Employee.name))).scalars().all()
    return [{"id":e.id,"device_user_id":e.device_user_id,"employee_code":e.employee_code,
             "name":e.name,"department":e.department,"shift_start":e.shift_start,"shift_end":e.shift_end}
            for e in emps]


@router.post("/employees")
async def create_or_update_employee(payload: dict, db: AsyncSession=Depends(get_db), _=Depends(verify_api_key)):
    did = payload.get("device_user_id","").strip()
    if not did:
        raise HTTPException(400, "device_user_id required")
    emp = (await db.execute(select(Employee).where(Employee.device_user_id==did))).scalar_one_or_none()
    if emp:
        emp.name=payload.get("name",emp.name); emp.department=payload.get("department",emp.department)
        emp.shift_start=payload.get("shift_start",emp.shift_start)
        emp.shift_end=payload.get("shift_end",emp.shift_end)
        emp.grace_minutes=int(payload.get("grace_minutes",emp.grace_minutes))
        action="updated"
    else:
        emp=Employee(device_user_id=did,name=payload.get("name",f"Employee {did}"),
                     employee_code=payload.get("employee_code"),department=payload.get("department"),
                     shift_start=payload.get("shift_start","09:00"),shift_end=payload.get("shift_end","18:00"),
                     grace_minutes=int(payload.get("grace_minutes",15)))
        db.add(emp); action="created"
    await db.flush(); await db.commit()
    return {"action":action,"id":emp.id,"device_user_id":emp.device_user_id,"name":emp.name}