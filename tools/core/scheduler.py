"""Scheduler — reminders, recurring tasks, cron jobs. The boss's time is money."""

import asyncio
import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from goodman.config import TIMEZONE, TELEGRAM_ALLOWED_USER_ID
from goodman import db, soul

logger = logging.getLogger(__name__)

CHECK_INTERVAL = 30  # seconds between checking for due jobs


class Scheduler:
    """Manages scheduled tasks — one-shot reminders, daily routines, recurring jobs."""

    def __init__(self, send_callback):
        """
        Args:
            send_callback: async callable(text: str) to send messages to user.
        """
        self.send = send_callback
        self.running = False
        self._task = None

    def start(self):
        if self.running:
            return
        self.running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("Scheduler started — checking every %d seconds", CHECK_INTERVAL)

    def stop(self):
        self.running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("Scheduler stopped")

    async def _loop(self):
        await asyncio.sleep(30)  # Let bot initialize
        while self.running:
            try:
                await self._check_due_jobs()
                await asyncio.sleep(CHECK_INTERVAL)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Scheduler error: %s", e)
                await asyncio.sleep(60)

    async def _check_due_jobs(self):
        """Check for and execute any due jobs."""
        now = datetime.now(ZoneInfo(TIMEZONE))
        now_str = now.isoformat()
        jobs = await db.get_due_jobs(now_str)
        for job in jobs:
            try:
                await self._execute_job(job, now)
            except Exception as e:
                logger.error("Failed to execute job %d '%s': %s", job["id"], job["name"], e)

    async def _execute_job(self, job: dict, now: datetime):
        """Execute a single scheduled job."""
        payload = job["payload"]
        name = job["name"]

        # Dynamic placeholder substitution
        if "{days_to_deadline}" in payload:
            from datetime import date
            deadline = date(2026, 4, 30)
            days_left = (deadline - now.date()).days
            payload = payload.replace("{days_to_deadline}", str(days_left))

        # Check for chained context from a parent job
        chained_ctx = await db.get_chained_context(job["id"])
        if chained_ctx:
            payload = f"{chained_ctx}\n\n---\n\n{payload}"

        # Send the reminder/task to user
        await self.send(payload)

        # Save output for potential downstream chaining
        await db.save_job_output(job["id"], payload)

        soul.append_daily_log(f"[SCHEDULER] Fired: {name}")
        logger.info("Scheduler fired job %d: %s", job["id"], name)

        # Update job state
        schedule_type = job["schedule_type"]
        if schedule_type == "once":
            await db.delete_job(job["id"])
        else:
            next_run = self._calculate_next_run(job, now)
            if next_run:
                await db.update_job_run(job["id"], now.isoformat(), next_run.isoformat())
            else:
                await db.delete_job(job["id"])

    def _calculate_next_run(self, job: dict, now: datetime) -> datetime | None:
        """Calculate the next run time for a recurring job."""
        schedule_type = job["schedule_type"]
        data = json.loads(job["schedule_data"])

        if schedule_type == "every":
            minutes = data.get("minutes", 60)
            return now + timedelta(minutes=minutes)

        elif schedule_type == "daily":
            hour = data.get("hour", 9)
            minute = data.get("minute", 0)
            next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if next_run <= now:
                next_run += timedelta(days=1)
            return next_run

        elif schedule_type == "weekly":
            hour = data.get("hour", 9)
            minute = data.get("minute", 0)
            days = data.get("days", [0])  # 0=Monday, 6=Sunday

            # Find next matching day
            for offset in range(1, 8):
                candidate = now + timedelta(days=offset)
                if candidate.weekday() in days:
                    return candidate.replace(hour=hour, minute=minute, second=0, microsecond=0)
            return None

        return None

    async def add_job(self, name: str, schedule_type: str, schedule_data: dict, payload: str) -> int:
        """Add a new scheduled job. Returns the job ID."""
        now = datetime.now(ZoneInfo(TIMEZONE))

        if schedule_type == "once":
            # schedule_data should have "datetime" as ISO string
            next_run = schedule_data.get("datetime", now.isoformat())
        elif schedule_type == "every":
            minutes = schedule_data.get("minutes", 60)
            next_run = (now + timedelta(minutes=minutes)).isoformat()
        elif schedule_type == "daily":
            hour = schedule_data.get("hour", 9)
            minute = schedule_data.get("minute", 0)
            next_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if next_dt <= now:
                next_dt += timedelta(days=1)
            next_run = next_dt.isoformat()
        elif schedule_type == "weekly":
            hour = schedule_data.get("hour", 9)
            minute = schedule_data.get("minute", 0)
            days = schedule_data.get("days", [0])
            next_dt = None
            for offset in range(1, 8):
                candidate = now + timedelta(days=offset)
                if candidate.weekday() in days:
                    next_dt = candidate.replace(hour=hour, minute=minute, second=0, microsecond=0)
                    break
            next_run = next_dt.isoformat() if next_dt else now.isoformat()
        else:
            next_run = now.isoformat()

        job_id = await db.create_job(
            name=name,
            schedule_type=schedule_type,
            schedule_data=json.dumps(schedule_data),
            payload=payload,
            next_run_at=next_run,
        )
        logger.info("Created job %d: %s (%s, next: %s)", job_id, name, schedule_type, next_run)
        return job_id

    async def list_jobs(self) -> list[dict]:
        """List all active scheduled jobs."""
        return await db.list_jobs()

    async def remove_job(self, job_id: int):
        """Remove a scheduled job."""
        await db.delete_job(job_id)
        logger.info("Removed job %d", job_id)
