import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "attendance.db")

def run_migration():
    if not os.path.exists(DB_PATH):
        print(f"Database not found at {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Step 1: Add timezone_offset to devices
    try:
        cursor.execute("ALTER TABLE devices ADD COLUMN timezone_offset FLOAT DEFAULT 5.5;")
        print("Successfully added timezone_offset to devices table.")
    except sqlite3.OperationalError as e:
        if "duplicate column name" in str(e):
            print("Column timezone_offset already exists in devices table.")
        else:
            print(f"Error adding column: {e}")

    # Step 2: Add ix_punch_time index to raw_punch_logs
    try:
        cursor.execute("CREATE INDEX IF NOT EXISTS ix_punch_time ON raw_punch_logs (punch_time);")
        print("Successfully created ix_punch_time index on raw_punch_logs.")
    except sqlite3.Error as e:
        print(f"Error creating index: {e}")

    conn.commit()
    conn.close()

if __name__ == "__main__":
    run_migration()
