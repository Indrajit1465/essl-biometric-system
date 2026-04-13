#!/usr/bin/env python3
# scripts/fix_and_reset.py
"""
One-shot fix script for your current situation:

  1. Removes test employees (device_user_id 101, 205)
  2. Removes the ghost "UNKNOWN" device
  3. Shows all real device IDs from raw_punch_logs
  4. Creates real employee placeholders for every unique device ID
  5. Reprocesses ALL raw punches into daily_attendance

Run once while server is STOPPED:
    python scripts/fix_and_reset.py
"""
import asyncio
import sys
import os
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from sqlalchemy import select, delete, func, text
from app.database import create_tables, AsyncSessionLocal
from app.models import Employee, Device, RawPunchLog, DailyAttendance
from app.attendance_processor import recompute_daily


async def main():
    await create_tables()

    async with AsyncSessionLocal() as db:

        # ── Step 1: Remove test employees ─────────────────────────────────────
        print("\n[1] Removing test employees (101, 205)...")
        test_ids = ["101", "205"]
        for tid in test_ids:
            emp = await db.execute(
                select(Employee).where(Employee.device_user_id == tid)
            )
            emp = emp.scalar_one_or_none()
            if emp:
                await db.delete(emp)
                print(f"    Deleted test employee: device_user_id={tid!r} name={emp.name!r}")
            else:
                print(f"    Not found (already clean): {tid}")

        # ── Step 2: Remove ghost UNKNOWN device ────────────────────────────────
        print("\n[2] Removing ghost UNKNOWN device...")
        ghost = await db.execute(
            select(Device).where(Device.serial_number == "UNKNOWN")
        )
        ghost = ghost.scalar_one_or_none()
        if ghost:
            await db.delete(ghost)
            print(f"    Deleted UNKNOWN device (was ip={ghost.ip_address})")
        else:
            print("    No UNKNOWN device found (already clean)")

        await db.flush()

        # ── Step 3: Show all real device IDs in raw_punch_logs ────────────────
        print("\n[3] Real employee device IDs found in raw_punch_logs:")
        rows = await db.execute(
            select(
                RawPunchLog.employee_device_id,
                func.count().label("punches"),
                func.min(RawPunchLog.punch_time).label("first"),
                func.max(RawPunchLog.punch_time).label("last"),
            )
            .group_by(RawPunchLog.employee_device_id)
            .order_by(RawPunchLog.employee_device_id)
        )
        device_ids = rows.all()

        if not device_ids:
            print("    No punch records found! Pull from device first.")
            await db.rollback()
            return

        print(f"\n    {'Device ID':<12}  {'Punches':>8}  {'First punch':<22}  Last punch")
        print("    " + "-" * 70)
        for r in device_ids:
            print(f"    {r.employee_device_id:<12}  {r.punches:>8}  {str(r.first):<22}  {r.last}")

        # ── Step 4: Create real employee rows ──────────────────────────────────
        print("\n[4] Creating employee records for real device IDs...")
        created = 0
        for r in device_ids:
            dev_id = r.employee_device_id
            existing = await db.execute(
                select(Employee).where(Employee.device_user_id == dev_id)
            )
            if existing.scalar_one_or_none():
                print(f"    SKIP  device_id={dev_id!r} — already exists")
                continue

            emp = Employee(
                device_user_id=dev_id,
                name=f"Employee {dev_id}",
                employee_code=f"EMP{str(dev_id).zfill(4)}",
                department="Unassigned",
                shift_start="09:00",
                shift_end="18:00",
                grace_minutes=15,
            )
            db.add(emp)
            print(f"    CREATED  device_id={dev_id!r}  → name='Employee {dev_id}'")
            created += 1

        await db.flush()

        # ── Step 5: Mark all punches as unprocessed so recompute picks them up ─
        print("\n[5] Marking all raw punches for reprocessing...")
        await db.execute(
            text("UPDATE raw_punch_logs SET is_processed = 0")
        )
        await db.flush()

        # Clear old daily_attendance rows so we start fresh
        await db.execute(text("DELETE FROM daily_attendance"))
        await db.flush()
        print("    Cleared old daily_attendance table")

        await db.commit()

    # ── Step 6: Reprocess all punches ─────────────────────────────────────────
    print("\n[6] Reprocessing all punches into daily_attendance...")
    processed = 0
    skipped = 0

    async with AsyncSessionLocal() as db:
        rows = await db.execute(
            select(
                RawPunchLog.employee_device_id,
                func.date(RawPunchLog.punch_time).label("work_date"),
            )
            .distinct()
            .order_by(RawPunchLog.employee_device_id)
        )
        pairs = rows.all()

        for row in pairs:
            work_date = row.work_date
            if isinstance(work_date, str):
                work_date = date.fromisoformat(work_date)
            try:
                record = await recompute_daily(db, row.employee_device_id, work_date)
                if record:
                    processed += 1
                else:
                    skipped += 1
            except Exception as e:
                print(f"    ERROR emp={row.employee_device_id} date={work_date}: {e}")
                skipped += 1

        await db.commit()

    print(f"\n    Processed: {processed}  Skipped (no employee): {skipped}")

    # ── Summary ────────────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("  DONE — your database is now clean.")
    print("=" * 60)
    print(f"\n  Employees created : {created}")
    print(f"  Daily records     : {processed}")
    print()
    print("  NEXT: Update employee names (they are 'Employee 1' etc.)")
    print("  Use POST /api/employees in Swagger to re-register each")
    print("  with the correct name, or edit the DB directly.")
    print()
    print("  Then start the server:")
    print("    python main.py")
    print()
    print("  And check today's attendance:")
    print("    GET /api/attendance/today")


if __name__ == "__main__":
    asyncio.run(main())