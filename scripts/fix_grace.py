"""Update all employees to grace_minutes=0 and recompute"""
import asyncio, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from sqlalchemy import text
from app.database import AsyncSessionLocal

async def fix():
    async with AsyncSessionLocal() as db:
        r = await db.execute(text("UPDATE employees SET grace_minutes=0 WHERE grace_minutes != 0"))
        await db.commit()
        print(f"Updated {r.rowcount} employees to grace_minutes=0")

asyncio.run(fix())
