"""Achtergrond-scheduler: periodiek zoeken, downloads volgen, auteurs checken
en de database backuppen.

Draait in dezelfde Python-process als de webserver (BackgroundScheduler).
Werkt dus ook prima als de app via de Windows Taakplanner headless start.
"""
import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler

from . import db, services

log = logging.getLogger("ebookarr.scheduler")

_scheduler: BackgroundScheduler | None = None


def _search_job():
    try:
        services.auto_search_all(trigger="scheduler")
    except Exception:
        log.exception("Onverwachte fout in zoekronde")


def _backup_job():
    try:
        db.backup(keep=7)
    except Exception:
        log.exception("Onverwachte fout bij database-back-up")


def start():
    global _scheduler
    if _scheduler:
        return
    _scheduler = BackgroundScheduler(
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300}
    )
    hours = max(1, services.int_setting("search_interval_hours", 6))
    _scheduler.add_job(_search_job, "interval", hours=hours, id="search")
    _scheduler.add_job(_backup_job, "interval", hours=24, id="backup")

    now = datetime.now(timezone.utc)
    _scheduler.add_job(_search_job, "date", run_date=now + timedelta(seconds=25), id="search_startup")
    _scheduler.add_job(_backup_job, "date", run_date=now + timedelta(seconds=40), id="backup_startup")
    _scheduler.start()
    log.info("Scheduler gestart (zoeken elke %s uur; back-up 1/dag)", hours)


def apply_settings():
    """Herplan de zoekjob als het interval in de instellingen is gewijzigd."""
    if not _scheduler:
        return
    hours = max(1, services.int_setting("search_interval_hours", 6))
    try:
        _scheduler.reschedule_job("search", trigger="interval", hours=hours)
        log.info("Zoekinterval aangepast naar elke %s uur", hours)
    except Exception:
        log.exception("Kon zoekjob niet herplannen")


def shutdown():
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
