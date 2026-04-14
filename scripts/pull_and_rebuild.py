"""Pull ALL historical data from device and recompute everything"""
import asyncio, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import http.client, json

KEY = "change-me-in-production"

def api(method, path, timeout=30):
    conn = http.client.HTTPConnection("localhost", 8000, timeout=timeout)
    conn.request(method, path, headers={"X-API-Key": KEY, "Content-Type": "application/json"})
    r = conn.getresponse()
    data = json.loads(r.read())
    conn.close()
    return r.status, data

print("=" * 60)
print("  Step 1: Check current state")
print("=" * 60)
s, d = api("GET", "/api/dashboard/summary")
print(f"  Date: {d['date']}")
print(f"  Employees: {d['total_employees']}")
print(f"  Pending punches: {d['pending_punches']}")
print(f"  Today: {d['today']}")

print()
print("=" * 60)
print("  Step 2: Pull ALL attendance from device")
print("=" * 60)
s, d = api("POST", "/api/devices/CQQC232460300/pull", timeout=60)
print(f"  Status: {s}")
print(f"  Result: {d}")

print()
print("=" * 60)
print("  Step 3: Recompute all attendance")
print("=" * 60)
s, d = api("POST", "/api/attendance/recompute", timeout=120)
print(f"  Status: {s}")
print(f"  Result: {d}")

print()
print("=" * 60)
print("  Step 4: Check updated state")
print("=" * 60)
s, d = api("GET", "/api/dashboard/summary")
print(f"  Date: {d['date']}")
print(f"  Today: {d['today']}")
print(f"  Pending: {d['pending_punches']}")

# Check date range
s, d = api("GET", "/api/attendance/summary?start_date=2026-04-01&end_date=2026-04-14")
dates = set()
for r in d:
    dates.add(r["DATE"])
print(f"\n  Summary data spans: {sorted(dates)}")
print(f"  Total records: {len(d)}")

print()
print("=" * 60)
print("  DONE")
print("=" * 60)
