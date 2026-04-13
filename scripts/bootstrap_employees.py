#!/usr/bin/env python3
# scripts/bootstrap_employees.py
"""
One-time Employee Bootstrap
============================
Reads every distinct employee_device_id already in raw_punch_logs
and creates a placeholder Employee row for any that don't have one yet.

Run this ONCE after the device has sent its first real punches.
Then edit the generated employees in the DB (or via POST /api/employees)
to fill in real names, departments, and shift times.

Usage:
    python scripts/bootstrap_employees.py

    # Or with a name mapping file (CSV: device_id,name,employee_code,department):
    python scripts/bootstrap_employees.py --csv staff.csv
"""
import asyncio
import argparse
import csv
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from sqlalchemy import select, func
from app.database import create_tables, AsyncSessionLocal
from app.models import Employee, RawPunchLog


async def bootstrap(name_map: dict[str, dict] = None):
    await create_tables()

    async with AsyncSessionLocal() as db:
        # Find all distinct device IDs that have punches
        result = await db.execute(
            select(
                RawPunchLog.employee_device_id,
                func.count().label("punch_count"),
                func.min(RawPunchLog.punch_time).label("first_seen"),
                func.max(RawPunchLog.punch_time).label("last_seen"),
            )
            .group_by(RawPunchLog.employee_device_id)
            .order_by(RawPunchLog.employee_device_id)
        )
        device_ids = result.all()

        if not device_ids:
            print("\nNo raw punches found in the database yet.")
            print("Make sure the device has sent at least one punch first.")
            return

        print(f"\nFound {len(device_ids)} distinct device IDs in raw_punch_logs:\n")
        print(f"{'Device ID':<12} {'Punches':>8}  {'First seen':<22}  {'Last seen'}")
        print("-" * 70)
        for row in device_ids:
            print(
                f"{row.employee_device_id:<12} {row.punch_count:>8}  "
                f"{str(row.first_seen):<22}  {row.last_seen}"
            )

        print()
        created = 0
        skipped = 0

        for row in device_ids:
            dev_id = row.employee_device_id

            # Check if employee already exists
            existing = await db.execute(
                select(Employee).where(Employee.device_user_id == dev_id)
            )
            if existing.scalar_one_or_none():
                print(f"  SKIP  device_id={dev_id!r} — employee already registered")
                skipped += 1
                continue

            # Use name map if provided, otherwise create placeholder
            info = (name_map or {}).get(dev_id, {})
            emp = Employee(
                device_user_id=dev_id,
                name=info.get("name") or f"Employee {dev_id}",
                employee_code=info.get("employee_code") or f"EMP{dev_id.zfill(4)}",
                department=info.get("department") or "Unassigned",
                shift_start=info.get("shift_start") or "09:00",
                shift_end=info.get("shift_end") or "18:00",
                grace_minutes=int(info.get("grace_minutes") or 15),
            )
            db.add(emp)
            print(f"  CREATE  device_id={dev_id!r}  name={emp.name!r}")
            created += 1

        await db.commit()

    print(f"\nDone: {created} employees created, {skipped} already existed.")
    print()
    if created:
        print("NEXT STEP: Reprocess all historical punches into daily_attendance:")
        print()
        print('  curl -X POST "http://10.0.3.51:8000/api/attendance/recompute" \\')
        print('    -H "X-API-Key: your-key"')
        print()
        print("Or update employee names first:")
        print("  Edit the database directly, or use POST /api/employees to re-register")
        print("  with correct names before reprocessing.")


def load_csv(path: str) -> dict[str, dict]:
    name_map = {}
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            dev_id = row.get("device_id", "").strip()
            if dev_id:
                name_map[dev_id] = {
                    "name":          row.get("name", "").strip(),
                    "employee_code": row.get("employee_code", "").strip(),
                    "department":    row.get("department", "").strip(),
                    "shift_start":   row.get("shift_start", "09:00").strip(),
                    "shift_end":     row.get("shift_end", "18:00").strip(),
                    "grace_minutes": row.get("grace_minutes", "15").strip(),
                }
    return name_map


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bootstrap employees from device punch logs")
    parser.add_argument("--csv", help="Optional CSV file: device_id,name,employee_code,department,shift_start,shift_end")
    args = parser.parse_args()

    name_map = load_csv(args.csv) if args.csv else {}
    asyncio.run(bootstrap(name_map))