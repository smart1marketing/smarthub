"""Where ad sets and the suite pages' live URLs live.

Two files on the persistent disk:

* ``adsets.json`` — every generated set. Same spec/client split as Radio Promo:
  ``spec: true`` and no client, or a client name from the Hub registry.
* ``pages.json`` — the live URL for each Smart 1 suite landing page, set once
  in the Pages panel. Kept out of the code because those URLs move, and a
  stale hardcoded URL would quietly produce ads for the wrong page.
"""
from __future__ import annotations

import datetime as _dt
import os
import re
import secrets
import threading

from .catalog import SUITE_PAGES
from hub import jsonstore

_lock = threading.Lock()
MAX_SETS = 4000


def now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def slugify(value: str, fallback: str = "spec") -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return s[:80] or fallback


def data_dir() -> str:
    base = "/var/data" if os.path.isdir("/var/data") else os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "data")
    path = os.path.join(base, "landing_ads")
    os.makedirs(path, exist_ok=True)
    return path


# Reads and writes go through hub.jsonstore, which keeps the atomic .tmp +
# rename this module already had and adds a mirror into the database. The
# Render disk these files live on is not part of the database backup and does
# not survive being recreated, and pages.json is the only record of where
# each live suite page actually sits — a URL somebody typed once.

def _read(name: str, fallback):
    return jsonstore.read_json(os.path.join(data_dir(), name), default=fallback)


def _write(name: str, data):
    with _lock:
        jsonstore.write_json(os.path.join(data_dir(), name), data, indent=1)


# -------------------------------------------------------------------- pages
def pages() -> list[dict]:
    saved = {p.get("slug"): p for p in _read("pages.json", []) if isinstance(p, dict)}
    out = []
    for page in SUITE_PAGES:
        row = dict(page)
        row["url"] = (saved.get(page["slug"], {}).get("url") or "").strip()
        row["updated_at"] = saved.get(page["slug"], {}).get("updated_at", "")
        out.append(row)
    return out


def set_page_url(slug: str, url: str) -> dict | None:
    rows = [p for p in _read("pages.json", []) if isinstance(p, dict) and p.get("slug") != slug]
    rows.append({"slug": slug, "url": (url or "").strip(), "updated_at": now()})
    _write("pages.json", rows)
    return next((p for p in pages() if p["slug"] == slug), None)


# ------------------------------------------------------------------ ad sets
def all_sets() -> list[dict]:
    rows = _read("adsets.json", [])
    return rows if isinstance(rows, list) else []


def create(fields: dict) -> dict:
    row = {
        "id": "la_" + secrets.token_urlsafe(9),
        "created_at": now(), "updated_at": now(),
        "spec": bool(fields.get("spec")),
        "client": (fields.get("client") or "").strip(),
        "client_slug": slugify(fields.get("client") or "", "spec"),
        "page_slug": (fields.get("page_slug") or "").strip(),
        "page_name": (fields.get("page_name") or "").strip(),
        "url": (fields.get("url") or "").strip(),
        "objective": (fields.get("objective") or "Lead generation").strip(),
        "notes": (fields.get("notes") or "").strip(),
        "audience": (fields.get("audience") or "").strip(),
        "brief": None,
        "formats": {},          # format id -> generated payload
        "art": [],              # generated banner backgrounds
        "status": "draft",
    }
    rows = all_sets()
    rows.insert(0, row)
    _write("adsets.json", rows)
    return row


def get(set_id: str) -> dict | None:
    return next((r for r in all_sets() if r.get("id") == set_id), None)


def update(set_id: str, changes: dict) -> dict | None:
    rows = all_sets()
    for i, row in enumerate(rows):
        if row.get("id") == set_id:
            row.update(changes)
            row["updated_at"] = now()
            if "client" in changes:
                row["client_slug"] = slugify(row.get("client") or "", "spec")
                row["spec"] = not bool(row.get("client"))
            rows[i] = row
            _write("adsets.json", rows[:MAX_SETS])
            return row
    return None


def delete(set_id: str) -> bool:
    rows = all_sets()
    kept = [r for r in rows if r.get("id") != set_id]
    if len(kept) == len(rows):
        return False
    _write("adsets.json", kept)
    return True


def library(query: str = "", scope: str = "all") -> list[dict]:
    q = str(query or "").strip().lower()
    out = []
    for row in all_sets():
        if scope == "spec" and not row.get("spec"):
            continue
        if scope == "client" and row.get("spec"):
            continue
        if q and q not in " ".join(str(row.get(k) or "") for k in (
                "client", "page_name", "url", "objective", "notes")).lower():
            continue
        out.append(row)
    return out


def cloud_folder(row: dict) -> str:
    root = os.environ.get("LANDING_ADS_FOLDER", "smart1-landing-ads")
    who = "spec" if row.get("spec") else (row.get("client_slug") or "client")
    page = slugify(row.get("page_slug") or row.get("page_name") or "page", "page")
    return f"{root}/{who}/{page}-{(row.get('created_at') or now())[:10]}"
