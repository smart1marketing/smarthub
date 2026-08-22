"""Fan Radio — persistence.

One JSON file per project on the persistent disk, plus a small index so the
library lists fast without opening every project. Audio goes to Cloudinary
when it's configured and to the same disk when it isn't, so a missing
credential degrades to "works, stored locally" rather than "silently loses
the render".

Two decisions worth stating:

* **Share tokens are random, not derived.** ``secrets.token_urlsafe(24)``,
  not ``{client-slug}-{timestamp}``. The Suite audit found guessable report
  URLs across eight apps — a competitor who knows a company name and a date
  can walk the whole set. A customer approval page carries scripts that
  haven't aired yet; it gets a real token.
* **Feedback is written before anything else happens.** No webhook, no
  notification, no redirect runs until the comment is on disk. Every lead
  the audit found being destroyed was destroyed by a fire-and-forget POST
  standing in for storage.
"""
from __future__ import annotations

import os
import re
import secrets
import threading
from datetime import datetime, timezone
from hub import jsonstore

_lock = threading.RLock()


# ------------------------------------------------------------------ paths
def data_dir() -> str:
    base = "/var/data" if os.path.isdir("/var/data") else os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), "data")
    path = os.path.join(base, "fan_radio")
    os.makedirs(os.path.join(path, "projects"), exist_ok=True)
    os.makedirs(os.path.join(path, "audio"), exist_ok=True)
    return path


def _project_path(pid: str) -> str:
    return os.path.join(data_dir(), "projects", f"{pid}.json")


def _index_path() -> str:
    return os.path.join(data_dir(), "index.json")


# Reads and writes go through hub.jsonstore, which keeps the atomic .tmp +
# rename this module already had and adds a mirror into the database. The
# Render disk these files live on is not part of the database backup and does
# not survive being recreated, and a project here is the only copy of
# every script version a client approved.

def _read(path: str, fallback):
    return jsonstore.read_json(path, default=fallback)


def _write(path: str, data) -> None:
    with _lock:
        jsonstore.write_json(path, data, indent=1)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower())
    return re.sub(r"-{2,}", "-", s).strip("-") or "untitled"


# --------------------------------------------------------------- projects
def new_id() -> str:
    return secrets.token_urlsafe(9)


def new_token() -> str:
    return secrets.token_urlsafe(24)


def create(fields: dict, actor: str = "") -> dict:
    pid = new_id()
    project = {
        "id": pid,
        "created": now(),
        "updated": now(),
        "created_by": actor,
        "scope": fields.get("scope") or "spec",
        "client": fields.get("client") or "",
        "company": fields.get("company") or "",
        "home_url": fields.get("home_url") or "",
        "promotion": fields.get("promotion") or "",
        "notes": fields.get("notes") or "",
        "team_context": fields.get("team_context") or "",
        "tone": fields.get("tone") or "warm",
        "banned": fields.get("banned") or [],
        "pronunciation": fields.get("pronunciation") or [],
        "brief": {},
        "voice": {},
        "spots": [],
        "share": {"token": new_token(), "enabled": False, "opened": 0,
                  "headline": "", "intro": "", "cta_label": "", "cta_url": ""},
        "feedback": [],
    }
    save(project)
    return project


def save(project: dict) -> dict:
    project["updated"] = now()
    _write(_project_path(project["id"]), project)
    _reindex(project)
    return project


def load(pid: str) -> dict | None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{4,40}", str(pid or "")):
        return None
    return _read(_project_path(pid), None)


def delete(pid: str) -> bool:
    project = load(pid)
    if not project:
        return False
    try:
        # delete_json, not os.remove — dropping the file alone would leave
        # the mirrored copy to be restored by the next read, so the delete
        # would appear to work and then quietly undo itself.
        jsonstore.delete_json(_project_path(pid))
    except OSError:
        return False
    rows = [r for r in index() if r.get("id") != pid]
    _write(_index_path(), rows)
    return True


def _summarise(project: dict) -> dict:
    spots = project.get("spots") or []
    approved = len([s for s in spots if s.get("status") == "approved"])
    changes = len([s for s in spots if s.get("status") == "changes"])
    return {
        "id": project["id"],
        "company": project.get("company") or "",
        "client": project.get("client") or "",
        "scope": project.get("scope") or "spec",
        "created": project.get("created"),
        "updated": project.get("updated"),
        "created_by": project.get("created_by") or "",
        "spots": len(spots),
        "approved": approved,
        "changes": changes,
        "shared": bool((project.get("share") or {}).get("enabled")),
        "feedback": len(project.get("feedback") or []),
    }


def _reindex(project: dict) -> None:
    rows = [r for r in index() if r.get("id") != project["id"]]
    rows.insert(0, _summarise(project))
    rows.sort(key=lambda r: r.get("updated") or "", reverse=True)
    _write(_index_path(), rows[:4000])


def index() -> list[dict]:
    rows = _read(_index_path(), [])
    return rows if isinstance(rows, list) else []


def find_by_token(token: str) -> dict | None:
    """Linear scan of the index, then one file open. Fine at this scale and
    it keeps the token out of any filename."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,64}", str(token or "")):
        return None
    for row in index():
        project = load(row.get("id") or "")
        if project and (project.get("share") or {}).get("token") == token:
            return project
    return None


# ------------------------------------------------------------------ spots
def spot_id() -> str:
    return secrets.token_urlsafe(6)


def upsert_spot(project: dict, spot: dict) -> dict:
    spots = project.setdefault("spots", [])
    for i, existing in enumerate(spots):
        if existing.get("id") == spot.get("id"):
            spot.setdefault("versions", existing.get("versions") or [])
            spots[i] = spot
            return spot
    spot.setdefault("id", spot_id())
    spot.setdefault("versions", [])
    spot.setdefault("status", "draft")
    spots.append(spot)
    return spot


def get_spot(project: dict, sid: str) -> dict | None:
    for spot in project.get("spots") or []:
        if spot.get("id") == sid:
            return spot
    return None


def push_version(spot: dict, why: str, actor: str = "") -> None:
    """Append-only history. A draft is never overwritten in place."""
    if not spot.get("script"):
        return
    spot.setdefault("versions", []).append({
        "at": now(), "by": actor, "why": why,
        "script": spot.get("script"), "words": len(spot["script"].split()),
    })
    spot["versions"] = spot["versions"][-40:]


def order_key(spot: dict) -> tuple:
    order = {"pregame": 0, "gameday": 1, "postgame": 2}
    outcome = {"neutral": 0, "win": 1, "loss": 2}
    return (order.get(spot.get("daypart"), 9), int(spot.get("seconds") or 0),
            outcome.get(spot.get("outcome"), 9))


def sort_spots(project: dict) -> None:
    project["spots"] = sorted(project.get("spots") or [], key=order_key)


# --------------------------------------------------------------- feedback
def add_feedback(project: dict, entry: dict) -> dict:
    """Written to disk by the caller immediately after. Kept small and
    append-only; nothing here is ever edited by the customer afterwards."""
    entry = {
        "at": now(),
        "name": str(entry.get("name") or "")[:80],
        "spot_id": entry.get("spot_id") or "",
        "action": entry.get("action") or "comment",
        "comment": str(entry.get("comment") or "")[:4000],
    }
    project.setdefault("feedback", []).append(entry)
    project["feedback"] = project["feedback"][-500:]
    return entry


# ------------------------------------------------------------- audio file
def cloudinary_ready() -> bool:
    return bool(os.environ.get("CLOUDINARY_URL")
                or (os.environ.get("CLOUDINARY_CLOUD_NAME")
                    and os.environ.get("CLOUDINARY_API_KEY")))


def folder() -> str:
    return os.environ.get("FAN_RADIO_FOLDER", "smart1-fan-radio")


def store_audio(project: dict, spot: dict, audio: bytes) -> dict:
    """Cloudinary when configured, disk when not. Always returns a URL that
    works from the customer page."""
    scope = "spec" if (project.get("scope") != "client") else slugify(
        project.get("client") or project.get("company"))
    public_id = (f"{folder()}/{scope}/{slugify(project.get('company'))}"
                 f"-{spot.get('daypart')}-{spot.get('seconds')}"
                 f"-{secrets.token_urlsafe(6)}")

    if cloudinary_ready():
        try:
            import cloudinary
            import cloudinary.uploader
            if os.environ.get("CLOUDINARY_URL"):
                cloudinary.config(secure=True)
            else:
                cloudinary.config(
                    cloud_name=os.environ["CLOUDINARY_CLOUD_NAME"],
                    api_key=os.environ["CLOUDINARY_API_KEY"],
                    api_secret=os.environ.get("CLOUDINARY_API_SECRET"),
                    secure=True)
            # Through hub.storage. The note this call site carried — that
            # Cloudinary types audio as 'video' — is now encoded in
            # hub.storage.resource_type_for, so it holds for every module
            # rather than only the two that wrote it down.
            from hub import storage
            asset = storage.put(
                "fan_radio", f"{public_id}.mp3",
                audio if isinstance(audio, bytes) else audio.read(),
                public_id=public_id, overwrite=False,
                context={"project": project.get("id") or "",
                         "daypart": spot.get("daypart") or ""})
            return {"url": asset.url, "where": "cloudinary",
                    "public_id": asset.public_id}
        except Exception as exc:                      # noqa: BLE001
            # Fall through to disk — a render that cost money is never
            # thrown away because the upload failed.
            local = _write_local(spot, audio)
            local["warning"] = f"Cloudinary upload failed ({type(exc).__name__}); kept on disk."
            return local
    return _write_local(spot, audio)


def _write_local(spot: dict, audio: bytes) -> dict:
    name = f"{spot.get('id')}-{secrets.token_urlsafe(4)}.mp3"
    path = os.path.join(data_dir(), "audio", name)
    with open(path, "wb") as fh:
        fh.write(audio)
    return {"url": f"audio/{name}", "where": "disk", "file": name}


def local_audio_path(name: str) -> str | None:
    if not re.fullmatch(r"[A-Za-z0-9_-]+\.mp3", str(name or "")):
        return None
    path = os.path.join(data_dir(), "audio", name)
    return path if os.path.isfile(path) else None
