# app/scheduler.py
"""
Background scheduler for:
  1. AUTO-PULL: Periodically pull attendance from all active devices (every 10 min)
  2. HEALTH: Monitor device connectivity (every 5 min)

This is the PERMANENT fix for missing daily data. Without auto-pull,
data only appears when someone manually calls POST /api/devices/{sn}/pull.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models import Device

logger = logging.getLogger(__name__)

_OFFLINE_THRESHOLD_SECONDS = 600  # 10 minutes


# ── Auto-pull job ─────────────────────────────────────────────────────────────

async def auto_pull_all_devices():
    """
    PERMANENT FIX: Automatically pull attendance logs from ALL active devices
    every 10 minutes. This ensures data flows in without manual intervention.
    After pulling, recomputes today's attendance so the dashboard stays current.
    """
    try:
        from app.pull_sync import async_pull_and_save, PYZK_AVAILABLE
        from app.attendance_processor import recompute_today

        if not PYZK_AVAILABLE:
            logger.warning("[AUTO-PULL] pyzk not installed - skipping")
            return

        async with AsyncSessionLocal() as db:
            devices = (await db.execute(
                select(Device).where(Device.is_active.is_(True))
            )).scalars().all()

            if not devices:
                logger.debug("[AUTO-PULL] No active devices")
                return

            total_saved = 0
            for dev in devices:
                if not dev.ip_address:
                    logger.warning("[AUTO-PULL] Device %s has no IP, skipping", dev.serial_number)
                    continue

                try:
                    result = await async_pull_and_save(
                        db, dev.ip_address, dev.serial_number, dev.port or 4370
                    )
                    saved = result.get("saved", 0)
                    total_saved += saved
                    if saved > 0:
                        logger.info(
                            "[AUTO-PULL] Device %s: fetched=%d saved=%d dupes=%d",
                            dev.serial_number, result.get("fetched", 0),
                            saved, result.get("duplicates", 0)
                        )
                except Exception as exc:
                    logger.error("[AUTO-PULL] Device %s failed: %s", dev.serial_number, exc)

            # Recompute today's attendance if we got new data
            if total_saved > 0:
                try:
                    count = await recompute_today(db)
                    logger.info("[AUTO-PULL] Recomputed %d employees after pulling %d new punches",
                                count, total_saved)
                except Exception as exc:
                    logger.error("[AUTO-PULL] Recompute failed: %s", exc)
            else:
                logger.debug("[AUTO-PULL] No new punches from any device")

    except Exception as exc:
        logger.error("[AUTO-PULL] Auto-pull job failed: %s", exc)


# ── Health check job ──────────────────────────────────────────────────────────

async def check_device_health():
    """Check all active devices and log warnings for offline ones."""
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

                last_seen = d.last_seen_at
                if last_seen.tzinfo is None:
                    last_seen = last_seen.replace(tzinfo=timezone.utc)

                elapsed = (now - last_seen).total_seconds()
                if elapsed > _OFFLINE_THRESHOLD_SECONDS:
                    logger.critical(
                        "[HEALTH] DEVICE OFFLINE: %s (%s) -- last seen %d seconds ago. IP=%s",
                        d.serial_number, d.name, int(elapsed), d.ip_address
                    )
                else:
                    logger.debug(
                        "[HEALTH] Device %s OK -- last seen %d seconds ago",
                        d.serial_number, int(elapsed)
                    )
    except Exception as exc:
        logger.error("[HEALTH] Device health check failed: %s", exc)


# ── Missing Checkout Sweep ────────────────────────────────────────────────────

async def sweep_missing_checkouts():
    """
    Runs at 3:00 AM. Looks at yesterday's attendance.
    Triggers recompute for yesterday.
    Any single-punch records will automatically convert from 'PRESENT' to 'MISSING_OUT',
    as per the newly implemented policy.
    """
    try:
        from app.attendance_processor import recompute_daily, _today_local, _local_day_bounds_utc
        from app.models import Device, RawPunchLog
        async with AsyncSessionLocal() as db:
            devices = (await db.execute(select(Device.serial_number, Device.timezone_offset))).all()
            count = 0
            for dev_sn, tz_offset in devices:
                yesterday = _today_local(tz_offset) - timedelta(days=1)
                
                day_start, day_end = _local_day_bounds_utc(yesterday, tz_offset)
                
                emps = (await db.execute(
                    select(RawPunchLog.employee_device_id)
                    .where(
                        RawPunchLog.device_serial == dev_sn,
                        RawPunchLog.punch_time >= day_start,
                        RawPunchLog.punch_time <= day_end
                    ).distinct()
                )).scalars().all()
                
                for emp_id in emps:
                    rec = await recompute_daily(db, emp_id, yesterday)
                    if rec and rec.status == "MISSING_OUT":
                        count += 1
            await db.commit()
            logger.info("[SWEEP] Swept yesterday's checkouts. Flagged %d as MISSING_OUT.", count)
    except Exception as exc:
        logger.error("[SWEEP] Sweep failed: %s", exc)


# ── Scheduler setup ──────────────────────────────────────────────────────────

def setup_scheduler():
    """
    Create and start the APScheduler instance with two jobs:
      1. auto_pull_all_devices - every 10 minutes (data ingestion)
      2. check_device_health   - every 5 minutes  (monitoring)
    """
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.interval import IntervalTrigger

        scheduler = AsyncIOScheduler()

        # JOB 1: Auto-pull attendance from devices every 10 minutes
        scheduler.add_job(
            auto_pull_all_devices,
            trigger=IntervalTrigger(minutes=10),
            id="auto_pull_devices",
            name="Auto-Pull Attendance",
            replace_existing=True,
        )

        # JOB 2: Device health monitoring every 5 minutes
        scheduler.add_job(
            check_device_health,
            trigger=IntervalTrigger(minutes=5),
            id="device_health_check",
            name="Device Health Monitor",
            replace_existing=True,
        )

        from apscheduler.triggers.cron import CronTrigger
        # JOB 3: Sweep missing checkouts at 3:00 AM daily
        scheduler.add_job(
            sweep_missing_checkouts,
            trigger=CronTrigger(hour=3, minute=0),
            id="sweep_missing_checkouts",
            name="Sweep Missing Checkouts",
            replace_existing=True,
        )

        scheduler.start()
        logger.info("[SCHEDULER] Auto-pull started (every 10 min)")
        logger.info("[SCHEDULER] Health monitor started (every 5 min)")
        logger.info("[SCHEDULER] Missing checkout sweep started (03:00 AM daily)")

        # Run first pull immediately (30 sec delay to let server fully start)
        scheduler.add_job(
            auto_pull_all_devices,
            trigger="date",
            run_date=datetime.now(timezone.utc) + timedelta(seconds=30),
            id="initial_pull",
            name="Initial Pull on Startup",
            replace_existing=True,
        )
        logger.info("[SCHEDULER] Initial pull scheduled in 30 seconds")

        return scheduler
    except ImportError:
        logger.warning("[SCHEDULER] apscheduler not installed -- schedulers disabled")
        return None
    except Exception as exc:
        logger.error("[SCHEDULER] Failed to start scheduler: %s", exc)
        return None
