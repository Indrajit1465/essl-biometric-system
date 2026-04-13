import sqlite3

# Connect to the database
conn = sqlite3.connect('attendance.db')
cursor = conn.cursor()

print("Clearing old tables...")
cursor.execute("DROP TABLE IF EXISTS attendance_summary;")
cursor.execute("DROP TABLE IF EXISTS daily_attendance;")

print("Resetting punch logs...")
cursor.execute("UPDATE raw_punch_logs SET is_processed = 0;")

# Save and close
conn.commit()
conn.close()

print("Database successfully reset!")