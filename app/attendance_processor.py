# app/attendance_processor.py
"""
All datetimes stored/compared as naive UTC (DB storage convention).
Timezone conversion happens dynamically based on the Device's timezone_offset.

Production fixes applied
--------------------------
C3: save_punches() uses begin_nested() savepoints.
C4: Day-boundary queries use dynamic UTC ranges based on device offset.
H4: recompute_today() handles active timezones correctly.
"""
from __future__ import annotations

import logging
from datetime import datetime, date, time, timedelta, timezone
from typing import Optional, Sequence

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AttendanceSummary, DailyAttendance, Device, Employee, RawPunchLog
from app.adms_parser import ParsedPunch

logger = logging.getLogger(__name__)


# ── Datetime helpers ──────────────────────────────────────────────────────────

def _to_naive_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _fmt_local(dt: datetime | None, tz_offset: float) -> str | None:
    """Naive-UTC -> Local HH:MM string based on device tz_offset."""
    if dt is None:
        return None
    return (_to_naive_utc(dt) + timedelta(hours=tz_offset)).strftime("%H:%M")


def _local_date_for(dt: datetime, tz_offset: float) -> date:
    """Return the Local calendar date for a naive-UTC datetime."""
    return (_to_naive_utc(dt) + timedelta(hours=tz_offset)).date()


def _today_local(tz_offset: float) -> date:
    return datetime.now(timezone(timedelta(hours=tz_offset))).date()


def _local_day_bounds_utc(work_date: date, tz_offset: float) -> tuple[datetime, datetime]:
    """Convert a Local calendar date into naive-UTC datetime bounds."""
    offset = timedelta(hours=tz_offset)
    day_start = datetime.combine(work_date, time.min) - offset
    day_end = datetime.combine(work_date, time.max) - offset
    return day_start, day_end


# ── Internal Tooling ──────────────────────────────────────────────────────────

async def get_emp_tz(db: AsyncSession, emp_id: str) -> float:
    recent = (await db.execute(
        select(Device.timezone_offset)
        .join(RawPunchLog, RawPunchLog.device_serial == Device.serial_number)
        .where(RawPunchLog.employee_device_id == emp_id)
        .order_by(RawPunchLog.punch_time.desc())
        .limit(1)
    )).scalar_one_or_none()
    return recent if recent is not None else 5.5


# ── Save punches ──────────────────────────────────────────────────────────────

async def save_punches(
    db: AsyncSession,
    device_serial: str,
    punches: list[ParsedPunch],
    source: str = "ADMS",
) -> tuple[int, int]:
    saved = duplicates = 0
    for punch in punches:
        punch_time_utc = _to_naive_utc(punch.punch_time)
        try:
            async with db.begin_nested():
                db.add(RawPunchLog(
                    device_serial=device_serial,
                    employee_device_id=punch.employee_id,
                    punch_time=punch_time_utc,
                    status=punch.status,
                    verify_type=punch.verify_type,
                    raw_payload=punch.raw_line,
                    source=source,
                ))
                await db.flush()
            saved += 1
            # We broadcast after successful db flush
            await broadcast_punch(punch.employee_id, punch_time_utc, punch.status)
        except Exception as exc:
            err = str(exc).upper()
            if "UNIQUE" in err or "INTEGRITY" in err or "DUPLICATE" in err:
                duplicates += 1
            else:
                logger.error("DB error saving punch emp=%s: %s", punch.employee_id, exc)
    return saved, duplicates


# Placeholder for circular import avoidance (WebSockets)
async def broadcast_punch(emp_id: str, punch_time: datetime, status: int):
    try:
        from app.websockets import manager
        st_name = {0:"CHECK_IN", 1:"CHECK_OUT", 2:"BREAK_OUT", 3:"BREAK_IN"}.get(status, f"S{status}")
        await manager.broadcast({
            "type": "NEW_PUNCH",
            "employee_id": emp_id,
            "punch_time_utc": punch_time.isoformat(),
            "status": st_name
        })
    except ImportError:
        pass


# ── Core daily computation ────────────────────────────────────────────────────

def _compute_daily(emp: Employee, work_date: date, punches: list[RawPunchLog], tz_offset: float) -> dict:
    """
    Compute attendance fields from a list of punches for one employee on one day.
    """
    result = dict(
        first_in=None, last_out=None, total_minutes=None,
        status="ABSENT", is_late=False, late_minutes=0,
        overtime_minutes=0, punch_count=len(punches),
    )

    if not punches:
        return result

    srt = sorted(punches, key=lambda p: p.punch_time)
    first_in = _to_naive_utc(srt[0].punch_time)

    # MODIFICATION: Auto-checkout missing punch policy
    if len(srt) >= 2:
        last_out = _to_naive_utc(srt[-1].punch_time)
    else:
        last_out = None

    result["first_in"]    = first_in
    result["last_out"]    = last_out
    result["punch_count"] = len(punches)

    if last_out is not None:
        result["total_minutes"] = int((last_out - first_in).total_seconds() / 60)

    # Shift boundaries — converted to naive UTC for comparison using the dynamic tz_offset
    offset = timedelta(hours=tz_offset)
    h, m = map(int, (emp.shift_start or "09:00").split(":"))
    shift_start_utc = datetime.combine(work_date, time(h, m)) - offset
    h, m = map(int, (emp.shift_end or "18:00").split(":"))
    shift_end_utc = datetime.combine(work_date, time(h, m)) - offset
    
    grace_mnts = emp.grace_minutes if emp.grace_minutes is not None else 0
    grace_utc  = shift_start_utc + timedelta(minutes=grace_mnts)
    shift_min  = int((shift_end_utc - shift_start_utc).total_seconds() / 60)

    # Status detection
    total = result["total_minutes"]
    if total is not None:
        result["status"] = "PRESENT" if total >= shift_min * 0.5 else "HALF_DAY"
    else:
        # User requested to flag single punches as MISSING_OUT instead of PRESENT
        # However, if it's the current day, they might just be currently in the office.
        today = _today_local(tz_offset)
        if work_date < today:
            result["status"] = "MISSING_OUT"
        else:
            result["status"] = "PRESENT"

    # Late detection: Ignore seconds so 09:00:59 counts as 09:00
    first_in_trunc = first_in.replace(second=0, microsecond=0)
    if first_in_trunc > grace_utc:
        result["is_late"]      = True
        result["late_minutes"] = int((first_in_trunc - shift_start_utc).total_seconds() / 60)
        if result["status"] == "PRESENT":
            result["status"] = "LATE"

    # Overtime
    if last_out is not None and last_out > shift_end_utc:
        result["overtime_minutes"] = int((last_out - shift_end_utc).total_seconds() / 60)

    return result


# ── Recompute one employee+day ────────────────────────────────────────────────

async def recompute_daily(
    db: AsyncSession,
    employee_device_id: str,
    work_date: date,
) -> Optional[DailyAttendance]:
    emp = (await db.execute(
        select(Employee).where(Employee.device_user_id == employee_device_id)
    )).scalar_one_or_none()

    if emp is None:
        return None

    tz_offset = await get_emp_tz(db, employee_device_id)
    day_start, day_end = _local_day_bounds_utc(work_date, tz_offset)

    punches: Sequence[RawPunchLog] = (await db.execute(
        select(RawPunchLog)
        .where(
            RawPunchLog.employee_device_id == employee_device_id,
            RawPunchLog.punch_time >= day_start,
            RawPunchLog.punch_time <= day_end,
        )
        .order_by(RawPunchLog.punch_time)
    )).scalars().all()

    daily = _compute_daily(emp, work_date, list(punches), tz_offset)

    # Upsert daily_attendance
    rec = (await db.execute(
        select(DailyAttendance).where(
            DailyAttendance.employee_id == emp.id,
            DailyAttendance.work_date == work_date,
        )
    )).scalar_one_or_none()

    if rec is None:
        rec = DailyAttendance(employee_id=emp.id, work_date=work_date)
        db.add(rec)

    rec.first_in         = daily["first_in"]
    rec.last_out         = daily["last_out"]
    rec.total_minutes    = daily["total_minutes"]
    rec.status           = daily["status"]
    rec.is_late          = daily["is_late"]
    rec.late_minutes     = daily["late_minutes"]
    rec.overtime_minutes = daily["overtime_minutes"]
    rec.punch_count      = daily["punch_count"]

    # Upsert attendance_summary
    summ = (await db.execute(
        select(AttendanceSummary).where(
            AttendanceSummary.emp_id == employee_device_id,
            AttendanceSummary.work_date == work_date,
        )
    )).scalar_one_or_none()

    if summ is None:
        summ = AttendanceSummary(emp_id=employee_device_id, work_date=work_date)
        db.add(summ)

    summ.employee_id  = emp.id
    summ.emp_name     = emp.name
    summ.punch_in     = _fmt_local(daily["first_in"], tz_offset)
    summ.punch_out    = _fmt_local(daily["last_out"], tz_offset)
    summ.is_late      = daily["is_late"]
    summ.late_minutes = daily["late_minutes"]
    summ.total_hours  = round(daily["total_minutes"] / 60, 2) if daily["total_minutes"] else None
    summ.status       = daily["status"]

    for p in punches:
        p.is_processed = True

    await db.flush()
    return rec


# ── Auto-refresh today ────────────────────────────────────────────────────────

async def recompute_today(db: AsyncSession) -> int:
    """
    Called automatically by GET /api/attendance/today.
    It determines the local "today" for every active device, finds punches for those bounds,
    and updates the summaries.
    """
    devices = (await db.execute(select(Device.serial_number, Device.timezone_offset))).all()
    count = 0
    to_process_pairs = set()

    for dev_sn, tz_offset in devices:
        today = _today_local(tz_offset)
        day_start, day_end = _local_day_bounds_utc(today, tz_offset)

        all_today = (await db.execute(
            select(RawPunchLog.employee_device_id)
            .where(
                RawPunchLog.device_serial == dev_sn,
                RawPunchLog.punch_time >= day_start,
                RawPunchLog.punch_time <= day_end,
            )
            .distinct()
        )).scalars().all()

        if not all_today:
            continue

        have_summary = set((await db.execute(
            select(AttendanceSummary.emp_id)
            .where(AttendanceSummary.work_date == today)
        )).scalars().all())

        have_unprocessed = set((await db.execute(
            select(RawPunchLog.employee_device_id)
            .where(
                RawPunchLog.device_serial == dev_sn,
                RawPunchLog.punch_time >= day_start,
                RawPunchLog.punch_time <= day_end,
                RawPunchLog.is_processed.is_(False),
            )
            .distinct()
        )).scalars().all())

        for emp_id in all_today:
            if emp_id not in have_summary or emp_id in have_unprocessed:
                to_process_pairs.add((emp_id, today))

    for emp_id, w_date in to_process_pairs:
        try:
            rec = await recompute_daily(db, emp_id, w_date)
            if rec:
                count += 1
        except Exception as exc:
            logger.error("recompute_today failed emp=%s: %s", emp_id, exc)

    if count:
        await db.commit()
    return count


# ── Full batch reprocess ──────────────────────────────────────────────────────

async def reprocess_all_pending(db: AsyncSession) -> int:
    all_punches = (await db.execute(
        select(RawPunchLog.employee_device_id, RawPunchLog.punch_time, Device.timezone_offset)
        .join(Device, RawPunchLog.device_serial == Device.serial_number)
    )).all()

    if not all_punches:
        return 0

    pairs: set[tuple[str, date]] = set()
    for emp_id, pt, tz in all_punches:
        if pt is not None:
            l_date = _local_date_for(pt, tz)
            pairs.add((emp_id, l_date))

    count = 0
    for emp_id, wd in pairs:
        try:
            rec = await recompute_daily(db, emp_id, wd)
            if rec:
                count += 1
        except Exception as exc:
            pass

    await db.commit()
    return count