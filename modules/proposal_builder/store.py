"""Proposal store — Cloudinary (raw assets) with a search index, mirrored to a
local data dir for dev/fallback. Port of lib/store.js."""
import json
import os
import re
import threading
from hub import jsonstore

import requests
from hub.webargs import clamp_int

FOLDER = os.environ.get("CLOUDINARY_FOLDER", "smart1-proposals")
CLOUD_READY = (os.environ.get("CLOUDINARY_URL") or "").startswith("cloudinary://")

try:
    import cloudinary
    import cloudinary.uploader
    if CLOUD_READY:
        cloudinary.config(secure=True)  # reads CLOUDINARY_URL
except ImportError:  # pragma: no cover
    cloudinary = None
    CLOUD_READY = False


def _data_dir():
    return jsonstore.data_dir("proposal-builder")


DATA_DIR = _data_dir()
_index = None
_lock = threading.Lock()


def _local_path(name):
    return os.path.join(DATA_DIR, name + ".json")


# Cloudinary already holds a copy of every proposal — but only when it is
# configured, and it was missing entirely before v7 (see render.yaml). The
# local file was the fallback for exactly that case and was itself unbacked,
# so the fallback had no fallback. Through hub.jsonstore it is mirrored into
# the database, which means a proposal survives losing either one.
def _local_read(name):
    return jsonstore.read_json(_local_path(name), default=None)


def _local_write(name, obj):
    try:
        jsonstore.write_json(_local_path(name), obj, indent=2)
    except OSError as exc:  # pragma: no cover
        print("local write failed:", exc)


def _cloud_upload(name, obj):
    if not CLOUD_READY:
        return None
    # Through hub.storage, keeping the same public_id so nothing moves. The
    # .json on the id is what the shared uploader derives "raw" from, rather
    # than the call site asserting it. hub.storage builds the data URI itself,
    # so the base64 step here is gone.
    from hub import storage
    pid = f"{FOLDER}/data/{name}.json"
    return storage.put("proposals", pid, json.dumps(obj).encode("utf-8"),
                       public_id=pid, overwrite=True).url


def _cloud_fetch(name):
    if not CLOUD_READY:
        return None
    try:
        import time
        url = cloudinary.utils.cloudinary_url(
            f"{FOLDER}/data/{name}.json", resource_type="raw", secure=True)[0]
        r = requests.get(url + f"?t={int(time.time() * 1000)}", timeout=12)
        if not r.ok:
            return None
        return r.json()
    except Exception:  # noqa: BLE001
        return None


def upload_pdf(record_id, version, pdf_bytes):
    """Upload a PDF to Cloudinary; returns secure URL or None."""
    if not CLOUD_READY:
        return None
    import base64
    data_uri = "data:application/pdf;base64," + base64.b64encode(pdf_bytes).decode()
    res = cloudinary.uploader.upload(
        data_uri, resource_type="raw",
        public_id=f"{FOLDER}/pdf/{record_id}-v{version}.pdf", overwrite=True)
    return res.get("secure_url")


def load_index():
    global _index
    with _lock:
        if _index is not None:
            return _index
    idx = _cloud_fetch("_index") or _local_read("_index") or []
    with _lock:
        _index = idx
    return idx


def _save_index():
    _local_write("_index", _index)
    try:
        _cloud_upload("_index", _index)
    except Exception as exc:  # noqa: BLE001
        print("index upload failed:", exc)


def get_proposal(pid):
    if not re.match(r"^[a-z0-9-]{8,64}$", str(pid), re.I):
        return None
    return _cloud_fetch("p-" + pid) or _local_read("p-" + pid)


def save_proposal(record):
    _local_write("p-" + record["id"], record)
    try:
        _cloud_upload("p-" + record["id"], record)
    except Exception as exc:  # noqa: BLE001
        print("proposal upload failed:", exc)
    load_index()
    c = record.get("customer") or {}
    meta = {
        "id": record["id"],
        "business": c.get("business_name", ""),
        "contact": c.get("contact_name", ""),
        "email": c.get("contact_email", ""),
        "industry": record.get("industry"),
        "industry_label": record.get("industry_label") or record.get("industry"),
        "salesperson": c.get("salesperson", ""),
        "status": record.get("status", "draft"),
        "monthly": record.get("monthly_investment") or "",
        "pdf_url": record.get("pdf_url", ""),
        "source_id": record.get("source_id", ""),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "versions": len(record.get("versions") or []),
    }
    with _lock:
        for i, r in enumerate(_index):
            if r["id"] == record["id"]:
                _index[i] = meta
                break
        else:
            _index.insert(0, meta)
        _index.sort(key=lambda r: str(r.get("updated_at") or ""), reverse=True)
    _save_index()
    return meta


def search_proposals(q="", industry="", salesperson="", limit=50):
    load_index()
    needle = (q or "").strip().lower()
    out = []
    for r in _index:
        if industry and r.get("industry") != industry:
            continue
        if salesperson and salesperson.lower() not in str(r.get("salesperson", "")).lower():
            continue
        if needle:
            hay = [r.get(k) for k in ("business", "contact", "email", "industry_label", "salesperson", "id")]
            if not any(needle in str(v or "").lower() for v in hay):
                continue
        out.append(r)
    # Had an upper bound but no lower one, so ?limit=-1 reached out[:-1] and
    # silently returned every result except the last.
    return out[:clamp_int(limit, 50, 1, 200)]


def cloudinary_ready():
    return CLOUD_READY
