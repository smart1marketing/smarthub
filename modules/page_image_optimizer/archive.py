"""Upload a finished image and file it under the client's SEO images.

Two halves:

1. Cloudinary upload — same folder, same context keys as the SEO Image
   Pipeline, so anything already reading that folder picks these up unchanged.

2. The archive record — this is the one place that has to know about the SEO
   Image Pipeline's own store. See RECORD_HOOK below.
"""

import json
import os
import re
import threading
import time

from . import settings
from hub import jsonstore
from hub.webargs import clamp_int

_LOCK = threading.Lock()

# Same defaulting bug as store.py: without HUB_DATA_DIR set this resolved to
# ./data in the container, so the archive of everything the tool had ever saved
# was discarded on each deploy. jsonstore's root resolves to the mounted disk.
FALLBACK_ARCHIVE = os.environ.get(
    "PAGE_IMAGES_ARCHIVE_FILE",
    os.path.join(jsonstore.data_root(), "page_image_optimizer_archive.json"),
)


def slug(text, fallback="untitled"):
    value = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return value[:60] or fallback


# --------------------------------------------------------------------------- #
# Cloudinary
# --------------------------------------------------------------------------- #

def upload(image_bytes, *, filename, company, project, page_name, page_url,
           alt, source_url):
    """Upload to smart1-seo-images/<company>/<project>/<filename>.webp."""
    folder = "/".join([
        settings.SEO_IMAGES_FOLDER, slug(company, "house"), slug(project, "general")
    ])
    context = {
        "company": company or "",
        "url": page_url or "",
        "project": project or "",
        "page": page_name or "",
        "alt": alt or "",
        "source_url": source_url or "",
    }

    if not settings.CLOUDINARY_URL:
        return {
            "url": "",
            "public_id": f"{folder}/{filename}",
            "stored": False,
            "note": "CLOUDINARY_URL is not set, so nothing was uploaded.",
        }

    import cloudinary
    import cloudinary.uploader

    cloudinary.config(secure=True)
    # Through hub.storage. The bytes handed in are already WebP (the module
    # converts before archiving), so the .webp name gives the shared derivation
    # what it needs and format= is no longer asserted here.
    from hub import storage
    _asset = storage.put("page_images", f"{filename}.webp", image_bytes,
                         folder=folder,
                         public_id=f"{folder}/{filename}",
                         overwrite=False)
    result = {"secure_url": _asset.url, "public_id": _asset.public_id,
              "bytes": _asset.bytes, "format": "webp"}
    return {
        "url": result.get("secure_url", ""),
        "public_id": result.get("public_id", ""),
        "bytes": result.get("bytes", len(image_bytes)),
        "width": result.get("width", 0),
        "height": result.get("height", 0),
        "stored": True,
    }


def delete(public_id):
    if not (settings.CLOUDINARY_URL and public_id):
        return False
    import cloudinary
    import cloudinary.uploader

    cloudinary.config(secure=True)
    result = cloudinary.uploader.destroy(public_id, resource_type="image")
    return result.get("result") == "ok"


# --------------------------------------------------------------------------- #
# The archive record
# --------------------------------------------------------------------------- #
#
# >>> INTEGRATION POINT <<<
#
# The SEO Image Pipeline (modules/seo_images) keeps its own archive table — the
# searchable one behind "See client image gallery". Images saved here should
# land in that same table so the client's SEO gallery is complete.
#
# I did not have modules/seo_images checked out in the session this was built
# in, so this resolves the writer at import time and falls back to a local JSON
# file. Point RECORD_HOOK at the real function and this tool disappears into
# the existing gallery. Everything else already matches (folder, context keys).
#
# Expected signature: record(**fields) -> record id or None
#
RECORD_HOOK = None

_CANDIDATES = [
    ("modules.seo_images.store", "add_record"),
    ("modules.seo_images.archive", "add_record"),
    ("modules.seo_images.app", "record_asset"),
]


def _resolve_hook():
    global RECORD_HOOK
    if RECORD_HOOK is not None:
        return RECORD_HOOK
    for module_name, attr in _CANDIDATES:
        try:
            module = __import__(module_name, fromlist=[attr])
            fn = getattr(module, attr, None)
            if callable(fn):
                RECORD_HOOK = fn
                return fn
        except Exception:  # noqa: BLE001
            continue
    return None


def archive_backend():
    """Which store the records are going to, for display in the UI."""
    return "seo-images" if _resolve_hook() else "local"


def record(**fields):
    """File one saved image in the client's SEO image archive."""
    fields.setdefault("saved_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    fields.setdefault("source", "page-image-optimizer")

    hook = _resolve_hook()
    if hook:
        try:
            return hook(**fields)
        except Exception:  # noqa: BLE001 - never lose the image over a bookkeeping error
            pass

    with _LOCK:
        os.makedirs(os.path.dirname(FALLBACK_ARCHIVE) or ".", exist_ok=True)
        rows = jsonstore.read_json(FALLBACK_ARCHIVE, default=[])
        if not isinstance(rows, list):
            rows = []
        rows.insert(0, fields)
        jsonstore.write_json(FALLBACK_ARCHIVE, rows[:5000])
    return fields.get("public_id")


def recent(limit=200, company=None):
    """Read back what this tool has saved (local fallback store only)."""
    rows = jsonstore.read_json(FALLBACK_ARCHIVE, default=[])
    if not isinstance(rows, list):
        return []
    if company:
        rows = [r for r in rows if slug(r.get("company")) == slug(company)]
    return rows[:clamp_int(limit, 200, 1, 1000)]
