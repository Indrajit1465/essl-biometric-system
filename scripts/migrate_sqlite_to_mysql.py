"""Migrate historical punch data from old SQLite to MySQL"""
import asyncio, sys, os, sqlite3
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from datetime import datetime
from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models import RawPunchLog

SQLITE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "attendance.db")

async def migrate():
    # 1. Read from SQLite
    print("Reading from SQLite...")
    conn = sqlite3.connect(SQLITE_PATH)
    cursor = conn.execute(
        "SELECT device_serial, employee_device_id, punch_time, status, "
        "verify_type, raw_payload, source FROM raw_punch_logs ORDER BY punch_time"
    )
    rows = cursor.fetchall()
    conn.close()
    print(f"  Found {len(rows)} records in SQLite")

    # 2. Insert into MySQL (skip duplicates)
    print("Migrating to MySQL...")
    saved = dupes = errors = 0

    async with AsyncSessionLocal() as db:
        for row in rows:
            device_serial, emp_id, punch_time_str, status, verify, raw, source = row
            try:
                # Parse punch_time
                pt = datetime.fromisoformat(punch_time_str.replace(".000000", ""))
            except Exception:
                try:
                    pt = datetime.strptime(punch_time_str[:19], "%Y-%m-%d %H:%M:%S")
                except Exception:
                    errors += 1
                    continue

            try:
                async with db.begin_nested():
                    db.add(RawPunchLog(
                        device_serial=device_serial,
                        employee_device_id=str(emp_id),
                        punch_time=pt,
                        status=status or 0,
                        verify_type=verify or 255,
                        raw_payload=raw,
                        source=source or "PULL",
                    ))
                    await db.flush()
                saved += 1
            except Exception as exc:
                err = str(exc).upper()
                if "UNIQUE" in err or "DUPLICATE" in err or "INTEGRITY" in err:
                    dupes += 1
                else:
                    errors += 1
                    if errors <= 3:
                        print(f"  Error: {exc}")

        await db.commit()

    print(f"  Saved: {saved}, Duplicates: {dupes}, Errors: {errors}")

    # 3. Recompute
    print("\nRecomputing all attendance...")
    import http.client, json
    conn = http.client.HTTPConnection("localhost", 8000, timeout=120)
    conn.request("POST", "/api/attendance/recompute",
                 headers={"X-API-Key": "change-me-in-production", "Content-Type": "application/json"})
    r = conn.getresponse()
    data = json.loads(r.read())
    conn.close()
    print(f"  {data}")

    # 4. Verify date range
    print("\nVerifying date coverage...")
    conn = http.client.HTTPConnection("localhost", 8000, timeout=30)
    conn.request("GET", "/api/attendance/summary?start_date=2026-04-01&end_date=2026-04-14",
                 headers={"X-API-Key": "change-me-in-production"})
    r = conn.getresponse()
    data = json.loads(r.read())
    conn.close()

    dates = {}
    for rec in data:
        d = rec["DATE"]
        dates[d] = dates.get(d, 0) + 1
    print(f"  Date coverage:")
    for d in sorted(dates.keys()):
        print(f"    {d}: {dates[d]} employee records")
    print(f"  Total: {len(data)} records across {len(dates)} days")

asyncio.run(migrate())
