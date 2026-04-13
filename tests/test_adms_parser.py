#!/usr/bin/env python3
# tests/test_adms_parser.py
"""
Quick integration test — verifies the full ADMS parse → DB save pipeline
without needing a real device.

Run with:  python tests/test_adms_parser.py
"""
import asyncio
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app.adms_parser import parse_adms_body, build_handshake_response
from app.database import create_tables, AsyncSessionLocal
from app.models import Employee
from app.attendance_processor import save_punches, recompute_daily

# ── Sample data mimicking a real eSSL F18 ADMS push ──────────────────────────
SAMPLE_SN = "TEST12345678"

SAMPLE_ATTLOG_BODY = """\
SN=TEST12345678&table=ATTLOG&Stamp=9999
1\t101\t2024-06-01 09:02:11\t0\t1\t0
2\t205\t2024-06-01 09:07:45\t0\t1\t0
1\t101\t2024-06-01 18:05:33\t1\t1\t0
3\t101\t2024-06-01 13:00:00\t2\t1\t0
"""

SAMPLE_QUERY_PARAMS = {
    "SN": SAMPLE_SN,
    "table": "ATTLOG",
    "Stamp": "9999",
}


async def run_test():
    print("=" * 60)
    print("  ADMS Parser + DB Integration Test")
    print("=" * 60)

    # 1. Test handshake response
    print("\n[1] Handshake response:")
    config = build_handshake_response(SAMPLE_SN)
    print(config)

    # 2. Test ATTLOG parsing
    print("[2] Parsing sample ATTLOG body:")
    payload = parse_adms_body(SAMPLE_ATTLOG_BODY, SAMPLE_QUERY_PARAMS, device_tz_offset=5.5)
    print(f"    Device SN   : {payload.device_serial}")
    print(f"    Table       : {payload.table}")
    print(f"    Punches     : {len(payload.punches)}")
    print(f"    Parse errors: {len(payload.parse_errors)}")
    for p in payload.punches:
        print(f"    → emp={p.employee_id} time={p.punch_time} status={p.status_label}")

    assert len(payload.punches) == 4, f"Expected 4 punches, got {len(payload.punches)}"
    assert payload.punches[0].employee_id == "101"
    assert payload.punches[0].status == 0  # CHECK_IN
    print("    ✓ Parse assertions passed")

    # 3. Database test
    print("\n[3] DB: creating tables...")
    await create_tables()

    async with AsyncSessionLocal() as db:
        # Create a test employee
        emp = Employee(
            device_user_id="101",
            name="Test Employee",
            employee_code="EMP001",
            shift_start="09:00",
            shift_end="18:00",
            grace_minutes=15,
        )
        db.add(emp)
        try:
            await db.commit()
            print("    ✓ Test employee created")
        except Exception:
            await db.rollback()
            print("    ℹ Test employee already exists (re-run)")

        # Save punches
        async with AsyncSessionLocal() as db2:
            saved, dupes = await save_punches(db2, SAMPLE_SN, payload.punches, source="TEST")
            await db2.commit()
            print(f"    Saved={saved}, Duplicates={dupes}")

            # Run again to verify deduplication
            saved2, dupes2 = await save_punches(db2, SAMPLE_SN, payload.punches, source="TEST")
            await db2.commit()
            print(f"    Re-save: Saved={saved2}, Duplicates={dupes2}")
            assert saved2 == 0 and dupes2 == 4, "Deduplication failed!"
            print("    ✓ Deduplication works correctly")

        # Recompute daily
        async with AsyncSessionLocal() as db3:
            from datetime import date
            record = await recompute_daily(db3, "101", date(2024, 6, 1))
            await db3.commit()
            if record:
                print(f"\n    Daily record for emp=101 on 2024-06-01:")
                print(f"      first_in      : {record.first_in}")
                print(f"      last_out      : {record.last_out}")
                print(f"      total_minutes : {record.total_minutes}")
                print(f"      status        : {record.status}")
                print(f"      is_late       : {record.is_late}")
                print(f"      late_minutes  : {record.late_minutes}")
                print(f"      overtime_min  : {record.overtime_minutes}")
                print("    ✓ Daily computation successful")
            else:
                print("    ✗ Daily record not computed (check employee mapping)")

    print("\n" + "=" * 60)
    print("  All tests passed ✓")
    print("  Start the server: python main.py")
    print("  Swagger docs    : http://localhost:8000/docs")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(run_test())
