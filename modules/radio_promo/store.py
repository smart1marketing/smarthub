"""Where radio projects live.

One JSON file on the persistent disk, same pattern as the UTM Builder. A
project is either **attached to a client** (``client`` holds a name from the
Hub's client registry) or a **spec** piece (``spec: true``, no client) — which
is the only structural difference between the two kinds of work. Spec projects
can be attached to a client later without losing anything, so a spec spot that
wins the business becomes that client's first spot.

Every draft, rewrite, tighten and hand edit is appended to ``versions`` rather
than overwriting, so nothing a client approved can be silently lost.
"""
from __future__ import annotations

import datetime as _dt
import os
import re
import secrets
import threading
from hub import jsonstore

_lock = threading.Lock()
MAX_PROJECTS = 4000


def now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def slugify(value: str, fallback: str = "spec") -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return s[:80] or fallback


def data_dir() -> str:
    base = "/var/data" if os.path.isdir("/var/data") else os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "data")
    path = os.path.join(base, "radio_promo")
    os.makedirs(path, exist_ok=True)
    return path


def _path() -> str:
    return os.path.join(data_dir(), "projects.json")


# Reads and writes go through hub.jsonstore, which keeps the atomic .tmp +
# rename this module already had and adds a mirror into the database. The
# Render disk these files live on is not part of the database backup and does
# not survive being recreated, and every draft, rewrite and hand edit
# this module deliberately appends rather than overwrites lives in here.

def _read() -> list[dict]:
    rows = jsonstore.read_json(_path(), default=[])
    return rows if isinstance(rows, list) else []


def _write(rows: list[dict]):
    with _lock:
        jsonstore.write_json(_path(), rows[:MAX_PROJECTS], indent=1)


def all_projects() -> list[dict]:
    return _read()


def create(fields: dict) -> dict:
    row = {
        "id": "rp_" + secrets.token_urlsafe(9),
        "created_at": now(),
        "updated_at": now(),
        "status": "draft",
        "spec": bool(fields.get("spec")),
        "client": (fields.get("client") or "").strip(),
        "client_slug": slugify(fields.get("client") or "", "spec"),
        "project_name": (fields.get("project_name") or "").strip(),
        "team_member": (fields.get("team_member") or "").strip(),
        "company": (fields.get("company") or "").strip(),
        "home_url": (fields.get("home_url") or "").strip(),
        "landing_url": (fields.get("landing_url") or "").strip(),
        "promotion": (fields.get("promotion") or "").strip(),
        "disclaimer": (fields.get("disclaimer") or "").strip(),
        "pronunciations": fields.get("pronunciations") or [],
        "brand": fields.get("brand") or {},
        "analysis": None,
        "tone_id": "",
        "scripts": {},
        "voice_want": {},
        "voice_matches": [],
        "spots": [],
        "banner": None,
        "versions": [],
    }
    rows = _read()
    rows.insert(0, row)
    _write(rows)
    return row


def get(project_id: str) -> dict | None:
    for row in _read():
        if row.get("id") == project_id:
            return row
    return None


def update(project_id: str, changes: dict) -> dict | None:
    rows = _read()
    for i, row in enumerate(rows):
        if row.get("id") == project_id:
            row.update(changes)
            row["updated_at"] = now()
            if "client" in changes:
                row["client_slug"] = slugify(row.get("client") or "", "spec")
                row["spec"] = not bool(row.get("client"))
            rows[i] = row
            _write(rows)
            return row
    return None


def add_version(project_id: str, kind: str, payload: dict, actor: str = "") -> dict | None:
    row = get(project_id)
    if not row:
        return None
    versions = list(row.get("versions") or [])
    versions.append({"at": now(), "kind": kind, "actor": actor, "payload": payload})
    return update(project_id, {"versions": versions[-60:]})


def delete(project_id: str) -> bool:
    rows = _read()
    kept = [r for r in rows if r.get("id") != project_id]
    if len(kept) == len(rows):
        return False
    _write(kept)
    return True


def library(query: str = "", scope: str = "all") -> list[dict]:
    """``scope`` is ``all``, ``spec`` or ``client``."""
    q = str(query or "").strip().lower()
    out = []
    for row in _read():
        if scope == "spec" and not row.get("spec"):
            continue
        if scope == "client" and row.get("spec"):
            continue
        if q:
            haystack = " ".join(str(row.get(k) or "") for k in (
                "client", "company", "project_name", "team_member", "promotion")).lower()
            if q not in haystack:
                continue
        out.append(row)
    return out


def cloud_folder(row: dict) -> str:
    """``smart1-radio-promo/<spec|client-slug>/<project>-<date>``."""
    root = os.environ.get("RADIO_PROMO_FOLDER", "smart1-radio-promo")
    who = "spec" if row.get("spec") else (row.get("client_slug") or "client")
    project = slugify(row.get("project_name") or "project", "project")
    day = (row.get("created_at") or now())[:10]
    return f"{root}/{who}/{project}-{day}"
