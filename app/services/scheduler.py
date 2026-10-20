"""Background jobs (runs in-process; the container runs a single app worker)."""
from __future__ import annotations

import threading
import traceback

from apscheduler.schedulers.background import BackgroundScheduler

from .. import config, settings
from ..db import SessionLocal
from ..models import JobRun
from ..util import utcnow

_sched: BackgroundScheduler | None = None
_locks: dict[str, threading.Lock] = {}


def run_job(name: str) -> str:
    """Run a named job now (used by the scheduler and the 'Run now' buttons)."""
    lock = _locks.setdefault(name, threading.Lock())
    if not lock.acquire(blocking=False):
        return f"{name} is already running"
    db = SessionLocal()
    run = JobRun(job=name)
    db.add(run)
    db.commit()
    try:
        msg = JOBS[name](db)
        run.status = "ok"
        run.message = (msg or "")[:4000]
        return msg
    except Exception as exc:
        db.rollback()
        run.status = "error"
        run.message = f"{exc}\n{traceback.format_exc()[-3000:]}"
        return f"{name} failed: {exc}"
    finally:
        run.finished = utcnow()
        db.merge(run)
        db.commit()
        db.close()
        lock.release()


def _sync_listings(db):
    from .sync import account, sync_listings
    acct = account(db)
    return sync_listings(db, acct) if acct else "eBay not connected"


def _sync_orders(db):
    from .sync import account, sync_orders
    acct = account(db)
    return sync_orders(db, acct) if acct else "eBay not connected"


def _sync_finances(db):
    from . import issues
    from .sync import account, sync_finances
    acct = account(db)
    if not acct:
        return "eBay not connected"
    msg = sync_finances(db, acct)
    n = issues.alert_new(db)
    return msg + (f", {n} new issues" if n else "")


def _full(db):
    from . import issues
    from .sync import run_full_sync
    msg = run_full_sync(db)
    issues.alert_new(db)
    return msg


def _relist(db):
    from .automations import run_relist
    return run_relist(db)


def _promote(db):
    from .automations import run_promote
    return run_promote(db)


def _backup(db):
    from .data_io import run_backup
    return run_backup(db)


JOBS = {
    "sync_listings": _sync_listings,
    "sync_orders": _sync_orders,
    "sync_finances": _sync_finances,
    "full_sync": _full,
    "relist": _relist,
    "promote": _promote,
    "backup": _backup,
}


def start() -> None:
    global _sched
    if _sched is None:
        _sched = BackgroundScheduler(timezone=config.TIMEZONE, job_defaults={"coalesce": True, "max_instances": 1,
                                                                             "misfire_grace_time": 600})
        _sched.start()
    reschedule()


def reschedule() -> None:
    if _sched is None:
        return
    _sched.remove_all_jobs()
    if settings.get("SYNC_ENABLED"):
        _sched.add_job(run_job, "interval", args=["sync_orders"], id="sync_orders",
                       minutes=max(int(settings.get("SYNC_ORDERS_MINUTES") or 15), 5))
        _sched.add_job(run_job, "interval", args=["sync_listings"], id="sync_listings",
                       minutes=max(int(settings.get("SYNC_LISTINGS_MINUTES") or 30), 10))
        _sched.add_job(run_job, "interval", args=["sync_finances"], id="sync_finances",
                       minutes=max(int(settings.get("SYNC_FINANCES_MINUTES") or 60), 15))
    _sched.add_job(run_job, "interval", args=["relist"], id="relist", minutes=20)
    _sched.add_job(run_job, "interval", args=["promote"], id="promote", hours=6)
    if settings.get("BACKUP_ENABLED"):
        _sched.add_job(run_job, "cron", args=["backup"], id="backup",
                       hour=int(settings.get("BACKUP_HOUR")) % 24, minute=17)


def next_runs() -> dict[str, str]:
    if _sched is None:
        return {}
    return {j.id: (j.next_run_time.strftime("%Y-%m-%d %H:%M") if j.next_run_time else "—") for j in _sched.get_jobs()}


def run_async(name: str) -> None:
    threading.Thread(target=run_job, args=(name,), daemon=True).start()


def shutdown() -> None:
    global _sched
    if _sched is not None and _sched.running:
        _sched.shutdown(wait=False)
    _sched = None
