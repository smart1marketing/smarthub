"""Background jobs, run once across all workers.

## The problem this has to solve first

Render runs gunicorn with two workers. A naive `threading.Timer` in each one
means every job runs twice: two nightly backups, two sets of quota alerts,
two attempts to clear the same stuck scans. Worse, it's intermittent — the
duplicates interleave differently each boot, so it looks like flakiness rather
than a design fault.

So exactly one worker holds a **leader lock** and runs jobs; the others idle.
On Postgres that's `pg_try_advisory_lock`, which is held for the life of the
connection and released automatically if that worker dies — a crashed leader
doesn't wedge the schedule. On SQLite (local development) leadership falls
back to a lock file, which is fine for a single process.

## Why not APScheduler

It would work, but it's another dependency and its own persistence model,
and this needs maybe eighty lines. The standing preference on this project is
no new Python dependencies unless unavoidable.

## Job contract

Jobs are plain functions returning a short dict summary. They must be safe to
run late, safe to skip, and safe to run twice — the leader lock makes double
runs unlikely, not impossible (a worker can lose and regain leadership across
a restart mid-job). Anything that must never double-run needs its own guard.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta, timezone

_LOCK_KEY = 918_273_645          # arbitrary, must be stable across deploys
_thread: threading.Thread | None = None
_started = False
_is_leader = False
_lock_conn = None
_state: dict[str, dict] = {}
_state_lock = threading.Lock()

TICK_SECONDS = 60


def _now() -> datetime:
    return datetime.now(timezone.utc)


def enabled() -> bool:
    """Off by default in development, on in production.

    A scheduler that starts during a local test run will happily fire real
    outbound jobs against real client systems. Opt-in is the safe default.
    """
    v = (os.environ.get("HUB_SCHEDULER") or "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return os.environ.get("RENDER") is not None


# ---------------------------------------------------------------------------
# Leadership
# ---------------------------------------------------------------------------

LOCK_STALE_SECONDS = 300


def _lock_path() -> str:
    base = "/var/data" if os.path.isdir("/var/data") else "."
    return os.path.join(base, "hub-scheduler.lock")


def _claim_file_lock() -> bool:
    """Leadership without Postgres, for local development.

    The first version leaked: it wrote a lock file and, if one already
    existed, called os.replace(path, path) — which does nothing. So after any
    restart the file persisted and NO worker ever became leader again. A
    scheduler that silently stops scheduling is the worst kind of broken,
    because everything still looks fine.

    Now the holder heartbeats the file, and a lock older than the stale window
    is taken over. The window is deliberately short: running a job twice is
    recoverable, never running it again is not.
    """
    path = _lock_path()
    mine = str(os.getpid())
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, mine.encode())
        os.close(fd)
        return True
    except FileExistsError:
        pass
    except OSError:
        return False

    try:
        age = time.time() - os.path.getmtime(path)
        with open(path, encoding="utf-8") as fh:
            holder = fh.read().strip()
    except OSError:
        return False

    if holder == mine:
        return True                       # already ours
    if age <= LOCK_STALE_SECONDS:
        return False                      # someone else is alive and leading

    # Stale. Claim it by writing our pid, then re-read to settle any race —
    # last writer wins and everyone else stands down.
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(mine)
        time.sleep(0.05)
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip() == mine
    except OSError:
        return False


def _heartbeat() -> None:
    """Keep the file lock fresh so another worker doesn't steal it."""
    if _is_leader and not _lock_conn:
        try:
            os.utime(_lock_path(), None)
        except OSError:
            pass


def _claim_leadership(app) -> bool:
    """Try to become the one worker that runs jobs."""
    global _lock_conn, _is_leader
    try:
        from hub.extensions import db
        with app.app_context():
            engine = db.engine
            if not engine.dialect.name.startswith("postgres"):
                # Single-process development: a lock file is enough.
                _is_leader = _claim_file_lock()
                return _is_leader

            from sqlalchemy import text
            conn = engine.connect()
            got = conn.execute(text("SELECT pg_try_advisory_lock(:k)"),
                               {"k": _LOCK_KEY}).scalar()
            if got:
                # Hold the connection open: the lock lives as long as it does,
                # and is released by Postgres if this worker dies.
                _lock_conn = conn
                _is_leader = True
            else:
                conn.close()
                _is_leader = False
    except Exception:                                   # noqa: BLE001
        _is_leader = False
    return _is_leader


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

def job_clear_stuck_scans(app) -> dict:
    """Resolve or error any scan running longer than the grace window.

    An Insites audit takes one to four minutes. Anything past thirty is
    stalled, and a stalled scan is worse than a failed one because it reads as
    work in progress and nobody investigates it.
    """
    grace = int(os.environ.get("SCANS_STUCK_MINUTES") or 30)
    try:
        from modules.scans.app import Scan, SessionLocal, _try_immediate_fetch
    except Exception as exc:                            # noqa: BLE001
        return {"skipped": f"scans unavailable ({type(exc).__name__})"}

    cutoff = _now() - timedelta(minutes=grace)
    resolved = errored = 0
    db = SessionLocal()
    try:
        for s in db.query(Scan).filter(Scan.status == "running").all():
            created = s.created_at
            if created is not None and created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if created and created > cutoff:
                continue
            if s.insites_report_id:
                # Give Insites one more chance before writing it off.
                try:
                    _try_immediate_fetch(db, s)
                    if s.status == "complete":
                        resolved += 1
                        continue
                except Exception:                       # noqa: BLE001
                    pass
            s.status = "error"
            s.error_message = (
                f"Stopped automatically after {grace} minutes. An Insites "
                f"audit normally takes one to four. Re-run it if you still "
                f"need the report.")
            errored += 1
        db.commit()
    finally:
        db.close()
    return {"resolved": resolved, "errored": errored, "grace_minutes": grace}


def job_rotate_audit_log(app) -> dict:
    """Stop the activity log filling the disk that also holds uploads."""
    from hub import audit
    return {"rotated": bool(audit.rotate(max_mb=64, keep=5))}


def job_quota_warnings(app) -> dict:
    """Record any provider past its monthly warning mark.

    Written to the activity log rather than emailed: the Hub has no mail
    sender, and a warning nobody can deliver is better recorded than lost.
    """
    from hub import audit, quotas
    warns = quotas.warnings()
    for w in warns:
        audit.log("quota", "threshold", provider=w["key"], state=w["state"],
                  used=w["used"], limit=w["limit"], message=w["message"])
    return {"warnings": len(warns),
            "providers": [w["key"] for w in warns]}


def job_refresh_invoice_links(app) -> dict:
    """Cache the public QuickBooks invoice links clients can open without a
    login. Runs three times a day so a newly issued invoice is shareable the
    same working day."""
    try:
        from hub import quickbooks as qb
    except Exception as exc:                            # noqa: BLE001
        return {"skipped": f"quickbooks unavailable ({type(exc).__name__})"}
    fn = getattr(qb, "refresh_public_links", None)
    if not callable(fn):
        return {"skipped": "no refresh_public_links() in hub.quickbooks yet"}
    try:
        return fn()
    except Exception as exc:                            # noqa: BLE001
        return {"error": type(exc).__name__}


def job_refresh_knack_products(app) -> dict:
    """Re-pull IO products from Knack.

    These were served from a hand-made JSON export that nothing refreshed, so
    Client 360 showed whatever was true on the day of the last export. Three
    hours is frequent enough that a product going live today is visible today,
    without hammering the API.
    """
    try:
        from hub import knack_products
    except Exception as exc:                            # noqa: BLE001
        return {"skipped": f"unavailable ({type(exc).__name__})"}
    return knack_products.refresh()


def job_backup_json(app) -> dict:
    """Mirror the durable JSON on the disk into the database.

    Every module that has moved onto ``hub.jsonstore`` already mirrors on each
    write, so on a healthy Hub this sweep finds nothing to do. It is here for
    the two cases where per-write mirroring is not enough:

      * a module still writing its own ``json.dump`` — covered from the next
        sweep rather than from whenever somebody gets round to editing it;
      * a write whose mirror was skipped because the database was asleep and
        the circuit breaker was open — otherwise that file stays stale in the
        backup until the next time a person happens to save it.

    Hourly. The work is proportional to what has *changed* since the last pass,
    not to the size of the disk, because each file's mtime is checked against
    when it was last mirrored.
    """
    from hub import jsonstore
    return jsonstore.sweep()


# name -> (every N minutes, function, human description)
JOBS = {
    "backup_json":       (60, job_backup_json,
                          "Mirror disk JSON into the database backup."),
    "clear_stuck_scans": (15, job_clear_stuck_scans,
                          "Resolve or error scans running past 30 minutes."),
    "rotate_audit_log":  (720, job_rotate_audit_log,
                          "Roll the activity log before it fills the disk."),
    "quota_warnings":    (240, job_quota_warnings,
                          "Record providers past their monthly warning mark."),
    "knack_products":    (180, job_refresh_knack_products,
                          "Re-pull IO products and campaigns from Knack."),
    "invoice_links":     (480, job_refresh_invoice_links,
                          "Refresh public QuickBooks invoice links (3x daily)."),
}


# ---------------------------------------------------------------------------
# Loop
# ---------------------------------------------------------------------------

def _run_job(app, name: str) -> None:
    every, fn, _ = JOBS[name]
    started = time.time()
    try:
        result = fn(app) or {}
        ok, err = True, ""
    except Exception as exc:                            # noqa: BLE001
        result, ok, err = {}, False, f"{type(exc).__name__}: {exc}"
    ms = int((time.time() - started) * 1000)
    with _state_lock:
        _state[name] = {"last_run": _now().isoformat(timespec="seconds"),
                        "ok": ok, "ms": ms, "result": result, "error": err}
    try:
        from hub import audit
        audit.log("scheduler", "job", job=name, ok=ok, ms=ms,
                  result=str(result)[:200], error=err[:200] or None)
    except Exception:                                   # noqa: BLE001
        pass


def _loop(app) -> None:
    # Stagger the first pass so a redeploy doesn't fire everything at once.
    time.sleep(20)
    if not _claim_leadership(app):
        return                                          # another worker leads
    due = {name: 0.0 for name in JOBS}
    while True:
        now = time.time()
        _heartbeat()
        for name, (every, _fn, _desc) in JOBS.items():
            if now >= due[name]:
                _run_job(app, name)
                due[name] = now + every * 60
        time.sleep(TICK_SECONDS)


def start(app) -> bool:
    """Start the scheduler thread. Safe to call more than once."""
    global _thread, _started
    if _started or not enabled():
        return False
    _started = True
    _thread = threading.Thread(target=_loop, args=(app,), daemon=True,
                               name="s1hub-scheduler")
    _thread.start()
    return True


def _leader_exists(app) -> bool:
    """Is some worker holding leadership right now?

    A follower's thread exits on purpose (``_loop`` returns the moment it
    loses the election), so "my thread isn't alive" says nothing about whether
    jobs are being run — and Diagnostics was reporting that as a red **down**
    on whichever worker happened to serve the page. Half the time that is a
    healthy scheduler being called broken, which also means a genuinely dead
    one looks identical and gets ignored.

    Probing the lock is the honest answer. If we cannot take it, someone else
    holds it and the jobs are covered. If we can, nobody is leading — so we
    release it immediately and report the fault.
    """
    if _is_leader:
        return True
    try:
        from hub.extensions import db
        with app.app_context():
            engine = db.engine
            if not engine.dialect.name.startswith("postgres"):
                try:                       # local: a fresh lock file means alive
                    return (time.time() - os.path.getmtime(_lock_path())
                            ) <= LOCK_STALE_SECONDS
                except OSError:
                    return False
            from sqlalchemy import text
            with engine.connect() as conn:
                got = conn.execute(text("SELECT pg_try_advisory_lock(:k)"),
                                   {"k": _LOCK_KEY}).scalar()
                if got:
                    conn.execute(text("SELECT pg_advisory_unlock(:k)"),
                                 {"k": _LOCK_KEY})
                return not got
    except Exception:                                   # noqa: BLE001
        return False


def status(app=None) -> dict:
    """What the scheduler is doing, for Diagnostics."""
    with _state_lock:
        runs = dict(_state)
    alive = bool(_thread and _thread.is_alive())
    if not enabled():
        state = "off"
    elif _is_leader and alive:
        state = "leading"
    elif app is not None and _leader_exists(app):
        state = "standby"          # healthy: the other worker has the jobs
    else:
        state = "down"             # nobody is running jobs — a real fault
    return {
        "enabled": enabled(),
        "running": alive,
        "is_leader": _is_leader,
        "state": state,
        "jobs": [
            {"name": n, "every_minutes": every, "description": desc,
             **runs.get(n, {"last_run": None, "ok": None})}
            for n, (every, _fn, desc) in JOBS.items()
        ],
        "note": {
            "off": "Disabled. Set HUB_SCHEDULER=true to turn the jobs on.",
            "leading": "This worker holds the lock and is running the jobs.",
            "standby": "The other worker holds the lock and is running the "
                       "jobs. Exactly one does, by design — nothing is wrong.",
            "down": "No worker holds the scheduler lock, so no background job "
                    "is running. Redeploy; if it persists, check the database "
                    "connection the lock depends on.",
        }[state],
    }


def run_now(name: str, app) -> dict:
    """Run one job immediately, for the button in Diagnostics."""
    if name not in JOBS:
        return {"error": "No such job."}
    _run_job(app, name)
    with _state_lock:
        return _state.get(name, {})
