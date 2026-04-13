# app/attendance_processor.py
"""
All datetimes stored/compared as naive UTC (SQLite limitation).
IST conversion happens only in _fmt_ist() for display.

Bug fixes in this version
--------------------------
1. Single punch: last_out is now None (not same as first_in).
   The same punch was being used for both IN and OUT — fixed.
2. recompute_today(): processes ALL of today's raw punches that have
   no attendance_summary entry yet. Called by /api/attendance/today
   before querying, so new punches are always included.
"""
from __future__ import annotations

import logging
from datetime import datetime, date, time, timedelta, timezone
from typing import Optional, Sequence

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AttendanceSummary, DailyAttendance, Employee, RawPunchLog
from app.adms_parser import ParsedPunch

logger = logging.getLogger(__name__)

IST_OFFSET = timedelta(hours=5, minutes=30)


# ── Datetime helpers ──────────────────────────────────────────────────────────

def _to_naive_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _fmt_ist(dt: datetime | None) -> str | None:
    """Naive-UTC → IST HH:MM string.  e.g. 03:36 UTC → 09:06 IST"""
    if dt is None:
        return None
    return (_to_naive_utc(dt) + IST_OFFSET).strftime("%H:%M")


def _ist_date_for(dt: datetime) -> date:
    """Return the IST calendar date for a naive-UTC datetime."""
    return (_to_naive_utc(dt) + IST_OFFSET).date()


def _today_ist() -> date:
    return datetime.now(timezone(IST_OFFSET)).date()


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
        except Exception as exc:
            await db.rollback()
            err = str(exc).upper()
            if "UNIQUE" in err or "INTEGRITY" in err or "DUPLICATE" in err:
                duplicates += 1
            else:
                logger.error("DB error saving punch emp=%s: %s", punch.employee_id, exc)
    return saved, duplicates


# ── Core daily computation ────────────────────────────────────────────────────

def _compute_daily(emp: Employee, work_date: date, punches: list[RawPunchLog]) -> dict:
    """
    Compute attendance fields from a list of punches for one employee on one day.

    RULE: First punch = PUNCH-IN. Last punch = PUNCH-OUT.
          If only 1 punch exists → punch_out is None (unknown departure).
          If 0 punches → ABSENT.
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

    # ── FIX 1: single punch → punch_out is None, NOT the same as punch_in ──
    # Previously srt[-1] on a 1-element list returns the same element as srt[0],
    # causing IN == OUT. Now last_out is only set when there are ≥ 2 punches.
    if len(srt) >= 2:
        last_out = _to_naive_utc(srt[-1].punch_time)
    else:
        last_out = None   # Still clocked in — departure unknown

    result["first_in"]    = first_in
    result["last_out"]    = last_out
    result["punch_count"] = len(punches)

    # Total working minutes (only computable when we have both in and out)
    if last_out is not None:
        result["total_minutes"] = int((last_out - first_in).total_seconds() / 60)

    # Shift boundaries — stored as IST strings, convert to naive UTC for comparison
    h, m = map(int, (emp.shift_start or "09:00").split(":"))
    shift_start_utc = datetime.combine(work_date, time(h, m)) - IST_OFFSET
    h, m = map(int, (emp.shift_end or "18:00").split(":"))
    shift_end_utc = datetime.combine(work_date, time(h, m)) - IST_OFFSET
    grace_utc     = shift_start_utc + timedelta(minutes=emp.grace_minutes or 15)
    shift_min     = int((shift_end_utc - shift_start_utc).total_seconds() / 60)

    # Status — if no checkout yet, treat single punch as PRESENT (employee is in)
    total = result["total_minutes"]
    if total is not None:
        result["status"] = "PRESENT" if total >= shift_min * 0.5 else "HALF_DAY"
    else:
        result["status"] = "PRESENT"   # Single punch: assume still present

    # Late detection
    if first_in > grace_utc:
        result["is_late"]      = True
        result["late_minutes"] = int((first_in - shift_start_utc).total_seconds() / 60)
        if result["status"] == "PRESENT":
            result["status"] = "LATE"

    # Overtime (only when we have a checkout)
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
        logger.warning("No Employee for device_user_id=%s — sync employees first", employee_device_id)
        return None

    # Naive UTC day boundaries (punches stored as naive UTC)
    day_start = datetime.combine(work_date, time.min)
    day_end   = datetime.combine(work_date, time.max)

    punches: Sequence[RawPunchLog] = (await db.execute(
        select(RawPunchLog)
        .where(
            RawPunchLog.employee_device_id == employee_device_id,
            RawPunchLog.punch_time >= day_start,
            RawPunchLog.punch_time <= day_end,
        )
        .order_by(RawPunchLog.punch_time)
    )).scalars().all()

    daily = _compute_daily(emp, work_date, list(punches))

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

    # Upsert attendance_summary (human-readable IST, no joins needed for reports)
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
    summ.punch_in     = _fmt_ist(daily["first_in"])
    summ.punch_out    = _fmt_ist(daily["last_out"])   # None → "--:--" in API layer
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
    FIX 2: Find every (employee, date) pair for TODAY that either:
      a) has no attendance_summary row yet, OR
      b) has raw punches added since the last summary update (is_processed=False)

    Called automatically by GET /api/attendance/today so the response
    always includes employees who punched in after the last manual recompute.

    Returns count of (employee, date) pairs updated.
    """
    today = _today_ist()

    # Day boundaries in naive UTC
    day_start = datetime.combine(today, time.min)
    day_end   = datetime.combine(today, time.max)

    # All distinct employee IDs that have ANY punch today
    all_today = (await db.execute(
        select(RawPunchLog.employee_device_id)
        .where(
            RawPunchLog.punch_time >= day_start,
            RawPunchLog.punch_time <= day_end,
        )
        .distinct()
    )).scalars().all()

    if not all_today:
        return 0

    # Which ones already have a current summary?
    have_summary = set((await db.execute(
        select(AttendanceSummary.emp_id)
        .where(AttendanceSummary.work_date == today)
    )).scalars().all())

    # Which ones have unprocessed punches today?
    have_unprocessed = set((await db.execute(
        select(RawPunchLog.employee_device_id)
        .where(
            RawPunchLog.punch_time >= day_start,
            RawPunchLog.punch_time <= day_end,
            RawPunchLog.is_processed == False,   # noqa: E712
        )
        .distinct()
    )).scalars().all())

    # Process: missing from summary OR has new punches
    to_process = set(all_today) - (have_summary - have_unprocessed)

    if not to_process:
        logger.debug("recompute_today: all %d employees already up to date", len(have_summary))
        return 0

    logger.info(
        "recompute_today: %d employees need update (%d missing summary, %d have new punches)",
        len(to_process),
        len(set(all_today) - have_summary),
        len(have_unprocessed & set(all_today)),
    )

    count = 0
    for emp_id in to_process:
        try:
            rec = await recompute_daily(db, emp_id, today)
            if rec:
                count += 1
        except Exception as exc:
            logger.error("recompute_today failed emp=%s: %s", emp_id, exc)

    if count:
        await db.commit()
        logger.info("recompute_today: updated %d employees", count)

    return count


# ── Full batch reprocess ──────────────────────────────────────────────────────

async def reprocess_all_pending(db: AsyncSession) -> int:
    """
    Recompute attendance_summary for every (employee, date) pair in raw_punch_logs.
    Use after: syncing employees, pulling historical data, or fixing bad records.
    """
    rows = (await db.execute(
        select(
            RawPunchLog.employee_device_id,
            func.date(RawPunchLog.punch_time).label("work_date"),
        ).distinct()
    )).all()

    if not rows:
        logger.info("reprocess_all_pending: no records found")
        return 0

    logger.info("reprocess_all_pending: %d employee-date pairs to process", len(rows))
    count = errors = 0

    for row in rows:
        wd = row.work_date
        if isinstance(wd, str):
            try:
                wd = date.fromisoformat(wd)
            except ValueError:
                continue
        try:
            rec = await recompute_daily(db, row.employee_device_id, wd)
            if rec:
                count += 1
        except Exception as exc:
            errors += 1
            logger.error("recompute_daily failed emp=%s date=%s: %s",
                         row.employee_device_id, wd, exc)

    await db.commit()
    logger.info("reprocess_all_pending: %d ok, %d errors", count, errors)
    return count