import os
import pymysql

# Derive DB connection parameters from environment or hardcode based on .env
DB_HOST = "localhost"
DB_PORT = 3306
DB_USER = "att_user"
DB_PASS = "root"
DB_NAME = "attendance_db"

def run_migration():
    try:
        conn = pymysql.connect(
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=DB_PASS,
            database=DB_NAME,
            cursorclass=pymysql.cursors.DictCursor
        )
        cursor = conn.cursor()

        # Step 1: Add timezone_offset to devices
        try:
            cursor.execute("ALTER TABLE devices ADD COLUMN timezone_offset FLOAT DEFAULT 5.5;")
            print("Successfully added timezone_offset to devices table.")
        except pymysql.err.OperationalError as e:
            if "Duplicate column name" in str(e):
                print("Column timezone_offset already exists in devices table.")
            else:
                print(f"Error adding column: {e}")

        # Step 2: Add ix_punch_time index to raw_punch_logs
        try:
            cursor.execute("CREATE INDEX ix_punch_time ON raw_punch_logs (punch_time);")
            print("Successfully created ix_punch_time index on raw_punch_logs.")
        except pymysql.err.OperationalError as e:
            if "Duplicate key name" in str(e):
                print("Index ix_punch_time already exists on raw_punch_logs.")
            else:
                print(f"Error creating index: {e}")

        conn.commit()
        conn.close()
        print("MySQL Migration Complete.")
    except Exception as exc:
        print(f"Failed to connect or migrate MySQL: {exc}")

if __name__ == "__main__":
    run_migration()
