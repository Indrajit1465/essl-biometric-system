#!/usr/bin/env python3
# reset.py
"""
M3 FIX: Database reset script using async SQLAlchemy instead of raw sqlite3.
Works with both SQLite and MySQL.

WARNING: This will DROP computed tables and mark all punches for reprocessing.
Do NOT run while the server is running.

Usage:
    python reset.py
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from sqlalchemy import text
from app.database import create_tables, AsyncSessionLocal, engine
from app.models import Base, DailyAttendance, AttendanceSummary


async def reset():
    print("\n[1] Dropping computed tables (daily_attendance, attendance_summary)...")
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: DailyAttendance.__table__.drop(sync_conn, checkfirst=True))
        await conn.run_sync(lambda sync_conn: AttendanceSummary.__table__.drop(sync_conn, checkfirst=True))
    print("    Done.")

    print("\n[2] Recreating tables...")
    await create_tables()
    print("    Tables recreated.")

    print("\n[3] Resetting punch logs (is_processed = 0)...")
    async with AsyncSessionLocal() as db:
        await db.execute(text("UPDATE raw_punch_logs SET is_processed = 0"))
        await db.commit()
    print("    All punch logs marked for reprocessing.")

    print("\n" + "=" * 50)
    print("  Database successfully reset!")
    print("=" * 50)
    print("\nNext steps:")
    print("  1. Start the server: python main.py")
    print("  2. Reprocess all punches: POST /api/attendance/recompute")
    print("     or: curl -X POST 'http://localhost:8000/api/attendance/recompute' -H 'X-API-Key: YOUR_KEY'")


if __name__ == "__main__":
    asyncio.run(reset())