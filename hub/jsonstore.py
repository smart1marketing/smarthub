"""Durable JSON — the disk stays the fast path, the database holds the copy.

## The problem

Roughly fifty places in this codebase persist state by writing a JSON file to
the Render disk. Every one of them is the same four functions, copy-pasted:

    def data_dir():                 "/var/data" if it exists else <repo>/data
    def _read(path, fallback):      open, json.load, except -> fallback
    def _write(path, data):         write .tmp, os.replace
    _lock = threading.Lock()

That works, and the atomic write is right. What it is not is **backed up**.
Render's managed Postgres is backed up; the 5 GB disk mounted at /var/data is
not part of those backups, and it does not survive being recreated — a plan
change, a region move, a corrupted volume, or detaching the disk to resize it
all end with an empty /var/data. The database comes back. The disk does not.

For a cache that is a non-event: `hub/knack_products.py` re-pulls from Knack
and is whole again. For anything where the JSON file is the **only copy** it is
data loss with no recovery path — every SEO client's setup answers and approved
schemas, every Fan Radio project, both OAuth token files, the house client
registry. Nobody would notice until they went looking for it.

## What this does

One shared store, so the fix lands once rather than fifty times:

* **Writes go to disk first, exactly as before** — same atomic .tmp + replace,
  same paths, same speed. Nothing about how a module reads its data changes.
* **Then the payload is mirrored into `hub_json_blobs`**, keyed by the file's
  path relative to the data root. That table lives in the database, so it is
  inside the same backup and the same point-in-time restore as everything else.
* **A read that misses on disk falls back to the database**, rewrites the file,
  and returns it. A recreated disk therefore refills itself as it is used,
  with no restore step and nothing for anyone to remember to run.

## Why its own engine and not hub.extensions.db

`db.session` needs an application context belonging to the app that `db` was
`init_app`-ed against. Half the callers here are dispatcher-mounted modules
with their *own* Flask app, and some are scheduler threads with no app at all —
both would raise "The current Flask app is not registered with this
'SQLAlchemy' instance", which is the trap `hub/extensions.py` already documents.
A plain Core engine against the same `database_url()` has no such requirement
and reaches the same table.

## Failure is never the caller's problem

A mirror failure is logged and swallowed: the disk write already succeeded, so
the module is no worse off than it was before this file existed. A database
that is down or asleep would otherwise add its timeout to *every* save, so
consecutive failures open a circuit breaker and mirroring goes quiet for a
minute rather than making the whole Hub feel broken.

## Durable or cache — say which

`write_json(..., durable=False)` opts a file out of mirroring. That is the
right call for anything rebuildable, and it is deliberately a *stated* argument
rather than a default: "this one does not need backing up" should be a decision
someone wrote down, not something a reader has to infer. `status()` reports
both sets, so the answer to "what would we lose?" is a page rather than a
guess.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone

# Mirroring is best-effort by design, so SQLAlchemy missing must not stop the
# disk half of this module from working — that half is what modules depend on.
try:
    from sqlalchemy import (Column, DateTime, Integer, MetaData, String, Table,
                            Text, create_engine, delete, select)
    _SA_ERROR = ""
except Exception as exc:                                # noqa: BLE001
    Column = DateTime = Integer = MetaData = String = Table = Text = None
    create_engine = delete = select = None
    _SA_ERROR = f"{type(exc).__name__}: {exc}"


_lock = threading.RLock()

# A blob is state, not a file store — 8 MB is far beyond anything here (the
# largest today is a 5000-row archive at well under 1 MB) and small enough that
# a runaway file is reported rather than quietly pushed into every backup.
MAX_MIRROR_BYTES = 8 * 1024 * 1024

# How long to stop trying after consecutive failures, so a sleeping database
# costs one timeout rather than one per save.
BREAKER_SECONDS = 60
BREAKER_AFTER = 3

_fail_count = 0
_breaker_until = 0.0
_last_error = ""

_engine = None
_table = None
_ready = False
_init_error = ""
_init_done = False
_init_retry_at = 0.0

# A failed connection is retried after this long rather than never. Render's
# Postgres can be asleep when the first write of a boot lands, and caching that
# first failure for the life of the worker would mean a Hub that came up while
# the database was waking never mirrors anything again — backups silently off
# until the next deploy, which is the failure mode this module exists to end.
INIT_RETRY_SECONDS = 120

# Files written with durable=False. Recorded so status() can list what is
# deliberately not backed up next to what is.
_declared_caches: set[str] = set()
_mirrored: dict[str, float] = {}


# --------------------------------------------------------------------- paths
def data_root() -> str:
    """The one place that decides where persistent files live.

    Every module had its own copy of this expression. They all agreed, which
    is luck rather than design: the moment one of them disagreed, its files
    would land somewhere the backup sweep never looks.
    """
    base = os.environ.get("HUB_DATA_DIR", "").strip()
    if not base:
        base = "/var/data" if os.path.isdir("/var/data") else os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    try:
        os.makedirs(base, exist_ok=True)
    except OSError:
        pass
    return base


def data_dir(*parts: str) -> str:
    """A subdirectory of the data root, created if needed.

    ``data_dir("fan_radio", "projects")`` replaces the six-line ``data_dir()``
    each module carried.
    """
    path = os.path.join(data_root(), *[str(p) for p in parts if p])
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass
    return path


def key_for(path: str) -> str:
    """Stable database key for a file path.

    Relative to the data root, so the key survives the root itself moving —
    /var/data in production and <repo>/data locally produce the same key, which
    is what lets a production blob be restored into a development checkout.
    """
    try:
        rel = os.path.relpath(os.path.abspath(path), data_root())
    except (OSError, ValueError):
        rel = path
    if rel.startswith(".."):
        # Outside the root (an explicit override path). Key on the absolute
        # location instead of inventing a relative one that collides.
        rel = "abs:" + os.path.abspath(path)
    return rel.replace(os.sep, "/")


# ------------------------------------------------------------------ database
def _init() -> bool:
    """Open the engine and make sure the table exists. Called once, lazily.

    Lazily because import time is boot time: a database still waking up must
    not delay the workers coming online, and the first *write* is a much better
    moment to find out than the first import.
    """
    global _engine, _table, _ready, _init_error, _init_done, _init_retry_at
    with _lock:
        if _init_done:
            if _ready or time.time() < _init_retry_at:
                return _ready
            _init_done = False          # cooldown elapsed — try again below
        _init_done = True
        _init_retry_at = time.time() + INIT_RETRY_SECONDS
        if create_engine is None:
            _init_error = f"SQLAlchemy unavailable ({_SA_ERROR})"
            return False
        try:
            from .extensions import database_url
            url = database_url()
        except Exception as exc:                        # noqa: BLE001
            _init_error = f"no database url ({type(exc).__name__}: {exc})"
            return False
        try:
            _engine = create_engine(
                url, pool_pre_ping=True, pool_recycle=280, pool_size=3,
                max_overflow=2, future=True)
            meta = MetaData()
            _table = Table(
                "hub_json_blobs", meta,
                Column("key", String(500), primary_key=True),
                Column("payload", Text, nullable=False),
                Column("bytes", Integer, nullable=False, default=0),
                Column("updated_at", DateTime, nullable=False),
            )
            meta.create_all(_engine)
            _ready = True
            return True
        except Exception as exc:                        # noqa: BLE001
            # Two workers creating the same table at once is the benign race
            # hub/extensions.py documents; the table exists either way.
            from .extensions import _is_benign_ddl_race
            if _is_benign_ddl_race(exc):
                _ready = True
                return True
            _init_error = f"{type(exc).__name__}: {exc}"
            _engine = None
            return False


def _breaker_open() -> bool:
    return time.time() < _breaker_until


def _note_failure(exc: Exception) -> None:
    global _fail_count, _breaker_until, _last_error
    with _lock:
        _fail_count += 1
        _last_error = f"{type(exc).__name__}: {exc}"
        if _fail_count >= BREAKER_AFTER:
            _breaker_until = time.time() + BREAKER_SECONDS
            _fail_count = 0


def _note_success() -> None:
    global _fail_count, _breaker_until, _last_error
    with _lock:
        _fail_count = 0
        _breaker_until = 0.0
        _last_error = ""


def available() -> bool:
    """Can the mirror be used right now?"""
    return _init() and not _breaker_open()


# ----------------------------------------------------------------- mirroring
def _upsert(key: str, text: str) -> bool:
    if not _init() or _breaker_open():
        return False
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        with _engine.begin() as cx:
            # UPDATE-then-INSERT rather than a dialect-specific ON CONFLICT:
            # this runs on both Postgres and the SQLite fallback, and the two
            # gunicorn workers only ever race to write the *same* value for the
            # same key, so the loser of an insert race is a no-op either way.
            res = cx.execute(_table.update().where(_table.c.key == key).values(
                payload=text, bytes=len(text), updated_at=now))
            if not res.rowcount:
                try:
                    cx.execute(_table.insert().values(
                        key=key, payload=text, bytes=len(text), updated_at=now))
                except Exception:                       # noqa: BLE001
                    cx.execute(_table.update().where(
                        _table.c.key == key).values(
                            payload=text, bytes=len(text), updated_at=now))
        _note_success()
        with _lock:
            _mirrored[key] = time.time()
            _declared_caches.discard(key)
        return True
    except Exception as exc:                            # noqa: BLE001
        _note_failure(exc)
        return False


def _fetch(key: str) -> str | None:
    if not _init() or _breaker_open():
        return None
    try:
        with _engine.connect() as cx:
            row = cx.execute(select(_table.c.payload).where(
                _table.c.key == key)).first()
        _note_success()
        return row[0] if row else None
    except Exception as exc:                            # noqa: BLE001
        _note_failure(exc)
        return None


def _forget(key: str) -> bool:
    if not _init() or _breaker_open():
        return False
    try:
        with _engine.begin() as cx:
            cx.execute(delete(_table).where(_table.c.key == key))
        _note_success()
        with _lock:
            _mirrored.pop(key, None)
        return True
    except Exception as exc:                            # noqa: BLE001
        _note_failure(exc)
        return False


# -------------------------------------------------------------------- public
def read_json(path: str, default=None, *, restore: bool = True):
    """Read a JSON file, falling back to the mirrored copy.

    The disk is tried first and answers almost every call. Only when the file
    is missing or unreadable — a fresh disk, or a write torn by a crash — does
    this reach for the database, and when it finds a copy it puts the file back
    before returning, so the next read is a plain disk read again.

    A missing file with no mirrored copy returns ``default``. That is the same
    answer the hand-rolled ``_read`` gave, so a caller that never had a backup
    behaves exactly as it did.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        pass

    if not restore:
        return default

    text = _fetch(key_for(path))
    if text is None:
        return default
    try:
        data = json.loads(text)
    except ValueError:
        return default

    # Put it back. Failing to do so is not an error — the value is in hand and
    # the caller gets it either way; the next read simply pays for the fetch.
    try:
        _atomic_write(path, text)
    except OSError:
        pass
    return data


def _atomic_write(path: str, text: str) -> None:
    """Write via a temp file in the same directory, then rename.

    The rename is atomic on POSIX, so a reader never sees a half-written file
    and a crash mid-write leaves the previous version intact rather than a
    truncated one. Every module that hand-rolled this got it right; it is
    preserved here so none of them lose it by moving over.
    """
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def write_json(path: str, data, *, durable: bool = True, indent=None) -> bool:
    """Write a JSON file and, unless it is a cache, mirror it to the database.

    Returns True when the disk write succeeded — which is the only part the
    caller depends on. Whether the mirror landed is reported by ``status()``,
    not by raising here: a database problem must never turn a working save into
    a failed one.

    Pass ``durable=False`` for anything rebuildable from its source. It stays
    on disk exactly as now and is listed as a known, intentional gap.
    """
    text = json.dumps(data, indent=indent, ensure_ascii=False, default=str)
    key = key_for(path)
    with _lock:
        _atomic_write(path, text)

    if not durable:
        with _lock:
            _declared_caches.add(key)
        return True

    size = len(text.encode("utf-8"))
    if size > MAX_MIRROR_BYTES:
        # Reported rather than silently truncated. A backup that quietly holds
        # part of a file is worse than one that says it holds none of it.
        # Both numbers in KB and stated outright: "over 0 MB" is what integer
        # division produced for any cap below a megabyte, and the size that
        # actually breached the cap is the one you need in order to act on it.
        with _lock:
            global _last_error
            _last_error = (f"{key} is {size // 1024} KB, over the "
                           f"{MAX_MIRROR_BYTES // 1024} KB mirror limit, and "
                           f"was NOT backed up")
        return True

    _upsert(key, text)
    return True


def delete_json(path: str) -> bool:
    """Remove a file and its mirrored copy.

    Both halves, always. Deleting only the file would leave the blob behind to
    be restored by the next ``read_json`` — the deletion would appear to work
    and then undo itself, which is a far more confusing bug than never having
    had a backup.
    """
    ok = False
    try:
        os.remove(path)
        ok = True
    except OSError:
        pass
    _forget(key_for(path))
    return ok


def mirror_file(path: str) -> bool:
    """Mirror a JSON file that some other code path wrote.

    The sweep uses this to cover writers that have not moved onto ``write_json``
    yet, so a module still doing its own ``json.dump`` is backed up from the
    next sweep rather than from the day someone gets round to editing it.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        json.loads(text)                    # never mirror an unparseable file
    except (OSError, ValueError):
        return False
    if len(text.encode("utf-8")) > MAX_MIRROR_BYTES:
        return False
    return _upsert(key_for(path), text)


# ------------------------------------------------------- disaster / recovery
GENERATION_FILE = ".hub-jsonstore-generation"
_GENERATION_KEY = GENERATION_FILE


def _generation_on_disk() -> str:
    try:
        with open(os.path.join(data_root(), GENERATION_FILE), encoding="utf-8") as fh:
            return json.load(fh).get("id", "")
    except (OSError, ValueError):
        return ""


def stamp_generation() -> str:
    """Record that this disk has been initialised, on disk and in the database.

    The pair is how a *recreated* disk is told apart from a merely empty one.
    A blob in the database with no matching marker on disk means the database
    has seen a disk before and this is not that disk — which is precisely the
    disaster this module exists for, and the only condition under which a bulk
    restore is the right thing to do.
    """
    import secrets
    gen = _generation_on_disk()
    if gen:
        return gen
    gen = secrets.token_hex(8)
    payload = {"id": gen, "at": datetime.now(timezone.utc).isoformat(
        timespec="seconds")}
    try:
        _atomic_write(os.path.join(data_root(), GENERATION_FILE),
                      json.dumps(payload))
    except OSError:
        return ""
    _upsert(_GENERATION_KEY, json.dumps(payload))
    return gen


def disk_is_fresh() -> bool:
    """True when the database holds blobs that this disk has never seen."""
    if _generation_on_disk():
        return False
    return _fetch(_GENERATION_KEY) is not None


def restore_all(limit: int = 20000) -> dict:
    """Write every mirrored blob back to disk.

    Only for a genuinely fresh disk. Restoring over a live one would resurrect
    anything deleted outside ``delete_json`` since the mirror was taken, so the
    caller has to have established that first — ``maybe_restore()`` does.
    """
    if not _init():
        return {"restored": 0, "skipped": 0, "error": _init_error or "unavailable"}
    restored = failed = skipped = 0
    try:
        with _engine.connect() as cx:
            rows = cx.execute(select(_table.c.key, _table.c.payload)
                              .limit(limit)).all()
    except Exception as exc:                            # noqa: BLE001
        _note_failure(exc)
        return {"restored": 0, "skipped": 0, "error": f"{type(exc).__name__}"}

    root = data_root()
    for key, payload in rows:
        if key.startswith("abs:"):
            path = key[4:]
        else:
            path = os.path.join(root, *key.split("/"))
        if os.path.exists(path):
            skipped += 1
            continue
        try:
            _atomic_write(path, payload)
            restored += 1
        except OSError:
            failed += 1
    return {"restored": restored, "skipped": skipped, "failed": failed,
            "total": len(rows)}


def maybe_restore() -> dict:
    """Restore the disk from the database if — and only if — the disk is new.

    Called once at boot. On every ordinary boot this is two cheap queries and
    a no-op. On the boot after a disk is recreated it is the whole recovery.
    """
    try:
        if not _init():
            return {"ran": False, "reason": _init_error or "no database"}
        if not disk_is_fresh():
            stamp_generation()
            return {"ran": False, "reason": "disk already initialised"}
        out = restore_all()
        stamp_generation()
        out["ran"] = True
        try:
            from . import audit
            audit.log("jsonstore", "restore", **{
                k: v for k, v in out.items() if k != "ran"})
        except Exception:                               # noqa: BLE001
            pass
        return out
    except Exception as exc:                            # noqa: BLE001
        return {"ran": False, "reason": f"{type(exc).__name__}: {exc}"}


# ----------------------------------------------------------------- the sweep
# Directories under the data root holding files that are rebuildable from their
# source. Sweeping them would put megabytes of Knack and provider responses
# into the backup to no purpose, and would make "what would we lose?" unreadable.
SWEEP_SKIP_DIRS = {"assets", "cache", "caches", "tmp", "knack-cache",
                   "ad_builder", "sessions"}
SWEEP_SKIP_FILES = {"knack_products.json", "knack_products_cache.json"}


def sweep(limit: int = 4000) -> dict:
    """Mirror every durable JSON file under the data root.

    Belt and braces for the migration: modules that still write their own JSON
    are covered from the next sweep, and a file whose mirror failed while the
    breaker was open is picked up on the next pass rather than staying stale
    until someone saves it again.
    """
    if not _init():
        return {"mirrored": 0, "error": _init_error or "unavailable"}
    root = data_root()
    seen = mirrored = skipped = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in SWEEP_SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            if not name.endswith(".json") or name.endswith(".tmp"):
                continue
            if name in SWEEP_SKIP_FILES:
                skipped += 1
                continue
            path = os.path.join(dirpath, name)
            key = key_for(path)
            if key in _declared_caches:
                skipped += 1
                continue
            seen += 1
            if seen > limit:
                break
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            # Already mirrored since the file last changed.
            if _mirrored.get(key, 0) >= mtime:
                continue
            if mirror_file(path):
                mirrored += 1
        if seen > limit:
            break
    return {"scanned": seen, "mirrored": mirrored, "skipped": skipped,
            "capped": seen > limit}


# ---------------------------------------------------------------- reporting
def mirror_is_on_the_same_disk() -> bool:
    """Is the "backup" sitting on the very disk it is supposed to survive?

    With DATABASE_URL unset, ``extensions.database_url()`` falls back to a
    SQLite file on /var/data — sensible for local development, and worthless
    as a backup, because the mirror is then destroyed by exactly the event it
    exists to protect against. Everything still reports healthy: writes
    succeed, reads succeed, the blob count climbs. That is the most dangerous
    shape a backup can have, so it is detected and said out loud rather than
    left to be discovered after a disk is recreated.
    """
    if not _engine:
        return False
    try:
        if not _engine.dialect.name.startswith("sqlite"):
            return False                # Postgres — a separate service
        db_path = (_engine.url.database or "").strip()
        if not db_path or db_path == ":memory:":
            return False
        root = os.path.abspath(data_root())
        return os.path.abspath(db_path).startswith(root + os.sep)
    except Exception:                                   # noqa: BLE001
        return False


def status() -> dict:
    """What is backed up, what is not, and why — for /diagnostics.

    Deliberately reports "unknown" rather than zero when the database cannot be
    reached. A backup panel showing a confident 0 blobs during an outage would
    read as "nothing is backed up", which is the wrong answer stated well.
    """
    ready = _init()
    out = {
        "ready": ready,
        "error": _init_error or _last_error,
        "breaker_open": _breaker_open(),
        "root": data_root(),
        "declared_caches": sorted(_declared_caches),
        "same_disk": ready and mirror_is_on_the_same_disk(),
        "blobs": None,
        "bytes": None,
        "newest": None,
    }
    if not ready or _breaker_open():
        return out
    try:
        from sqlalchemy import func
        with _engine.connect() as cx:
            row = cx.execute(select(
                func.count(), func.coalesce(func.sum(_table.c.bytes), 0),
                func.max(_table.c.updated_at))).first()
        out["blobs"] = int(row[0] or 0)
        out["bytes"] = int(row[1] or 0)
        out["newest"] = row[2].isoformat(timespec="seconds") if row[2] else None
        _note_success()
    except Exception as exc:                            # noqa: BLE001
        _note_failure(exc)
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out
