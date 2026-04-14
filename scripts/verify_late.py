import http.client, json

KEY = "change-me-in-production"
conn = http.client.HTTPConnection("localhost", 8000)
conn.request("GET", "/api/attendance/today", headers={"X-API-Key": KEY})
r = conn.getresponse()
data = json.loads(r.read())
conn.close()

print(f"Today: {len(data)} records\n")
print(f"{'EMP_Name':30} {'IN':>6} {'OUT':>6} {'LATE':>10} {'STATUS':>10}")
print("-" * 70)
for x in data[:10]:
    print(f"{x['EMP_Name'][:30]:30} {x['PUNCH_IN']:>6} {x['PUNCH_OUT']:>6} {x['LATE_MIN']:>10} {x['STATUS']:>10}")
