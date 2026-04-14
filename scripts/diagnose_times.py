"""Diagnose: why no today data and why reports only show yesterday"""
import asyncio, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from datetime import date, timedelta
from sqlalchemy import select, func, text
from app.database import AsyncSessionLocal
from app.models import RawPunchLog, AttendanceSummary, Employee

IST = timedelta(hours=5, minutes=30)

async def diagnose():
    async with AsyncSessionLocal() as db:
        # 1. What dates have punch records?
        dates = (await db.execute(
            select(
                func.date(RawPunchLog.punch_time).label("d"),
                func.count().label("n")
            ).group_by(text("d")).order_by(text("d"))
        )).all()
        print("=" * 60)
        print("  RAW PUNCH LOGS - by UTC date")
        print("=" * 60)
        for r in dates:
            print(f"  {r.d}  ->  {r.n} punches")
        total = sum(r.n for r in dates)
        print(f"  TOTAL: {total} punches")

        # 2. What dates have attendance summaries?
        print()
        print("=" * 60)
        print("  ATTENDANCE SUMMARY - by work_date")
        print("=" * 60)
        sdates = (await db.execute(
            select(
                AttendanceSummary.work_date,
                func.count().label("n")
            ).group_by(AttendanceSummary.work_date)
            .order_by(AttendanceSummary.work_date)
        )).all()
        for r in sdates:
            print(f"  {r.work_date}  ->  {r.n} employee records")
        if not sdates:
            print("  (EMPTY - no summary data!)")

        # 3. Check if today's punches exist
        print()
        print("=" * 60)
        print("  TODAY's DATA CHECK")
        print("=" * 60)
        today_ist = date(2026, 4, 14)
        # IST day bounds in UTC
        day_start = (
            asyncio.coroutine  # placeholder
        )
        from app.attendance_processor import _ist_day_bounds_utc
        ds, de = _ist_day_bounds_utc(today_ist)
        print(f"  Today IST: {today_ist}")
        print(f"  UTC query range: {ds} to {de}")

        today_count = (await db.execute(
            select(func.count()).select_from(RawPunchLog)
            .where(RawPunchLog.punch_time >= ds, RawPunchLog.punch_time <= de)
        )).scalar()
        print(f"  Punches in range: {today_count}")

        if today_count == 0:
            print()
            print("  ** NO PUNCHES FOR TODAY **")
            print("  Root cause: No data has been pulled from device today.")
            print("  Fix: Pull from device -> POST /api/devices/CQQC232460300/pull")

        # 4. Check total employee count
        emp_count = (await db.execute(
            select(func.count()).select_from(Employee)
        )).scalar()
        print(f"\n  Total employees: {emp_count}")

        # 5. Check min/max punch times
        minp = (await db.execute(select(func.min(RawPunchLog.punch_time)))).scalar()
        maxp = (await db.execute(select(func.max(RawPunchLog.punch_time)))).scalar()
        print(f"  Punch time range (UTC): {minp} to {maxp}")
        if minp:
            print(f"  Punch time range (IST): {minp + IST} to {maxp + IST}")

        print()
        print("=" * 60)
        print("  DIAGNOSIS")
        print("=" * 60)
        print(f"  MySQL only has data from: {dates[0].d if dates else 'NONE'} to {dates[-1].d if dates else 'NONE'}")
        print(f"  That's only {len(dates)} day(s) of data.")
        print()
        print("  The old SQLite had data from Apr 6-9 (663 records).")
        print("  That data was NOT migrated when we switched to MySQL.")
        print()
        print("  To get historical + today's data:")
        print("  1. Pull ALL logs from device (it may store weeks of data)")
        print("  2. Then recompute all attendance")

asyncio.run(diagnose())
