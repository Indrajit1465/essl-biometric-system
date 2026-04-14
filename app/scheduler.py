# app/scheduler.py
"""
H5: Background scheduler for device health monitoring.
M7: Uses the apscheduler dependency that was previously unused.

Runs periodic checks:
  - Device heartbeat monitoring (every 5 minutes)
  - Alerts when devices go offline (last_seen_at > 10 minutes ago)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models import Device

logger = logging.getLogger(__name__)

# Threshold: consider device offline if not seen for this many seconds
_OFFLINE_THRESHOLD_SECONDS = 600  # 10 minutes


async def check_device_health():
    """
    H5: Check all active devices and log warnings for any that haven't
    sent a heartbeat within the threshold period.
    """
    try:
        async with AsyncSessionLocal() as db:
            devices = (await db.execute(
                select(Device).where(Device.is_active.is_(True))
            )).scalars().all()

            now = datetime.now(timezone.utc)
            for d in devices:
                if d.last_seen_at is None:
                    logger.warning(
                        "[HEALTH] Device %s (%s) has NEVER connected. IP=%s",
                        d.serial_number, d.name, d.ip_address
                    )
                    continue

                # Handle naive datetimes (stored as UTC)
                last_seen = d.last_seen_at
                if last_seen.tzinfo is None:
                    last_seen = last_seen.replace(tzinfo=timezone.utc)

                elapsed = (now - last_seen).total_seconds()
                if elapsed > _OFFLINE_THRESHOLD_SECONDS:
                    logger.critical(
                        "[HEALTH] DEVICE OFFLINE: %s (%s) -- last seen %s (%d seconds ago). IP=%s",
                        d.serial_number, d.name, d.last_seen_at,
                        int(elapsed), d.ip_address
                    )
                else:
                    logger.debug(
                        "[HEALTH] Device %s OK -- last seen %d seconds ago",
                        d.serial_number, int(elapsed)
                    )
    except Exception as exc:
        logger.error("[HEALTH] Device health check failed: %s", exc)


def setup_scheduler():
    """
    Create and start the APScheduler instance.
    Called from main.py lifespan.
    Returns the scheduler instance so it can be shut down gracefully.
    """
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.interval import IntervalTrigger

        scheduler = AsyncIOScheduler()
        scheduler.add_job(
            check_device_health,
            trigger=IntervalTrigger(minutes=5),
            id="device_health_check",
            name="Device Health Monitor",
            replace_existing=True,
        )
        scheduler.start()
        logger.info("[SCHEDULER] Device health monitoring started (every 5 min)")
        return scheduler
    except ImportError:
        logger.warning("[SCHEDULER] apscheduler not installed -- device health monitoring disabled")
        return None
    except Exception as exc:
        logger.error("[SCHEDULER] Failed to start scheduler: %s", exc)
        return None
