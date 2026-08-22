"""Smart 1 Hub — SEO Image Pipeline.

Upload a batch of images, let the AI name them the way a search engine wants
them named, review and edit every name and alt tag, then save the approved
set to Cloudinary as optimised WebP — filed under the client and project it
belongs to, and recorded in a searchable archive that stays with the account.

Ported from the standalone Node/Express prototype, with the changes needed to
make it a Hub tool rather than a separate service:

* **Python/Flask, one process.** The Hub is one app behind one login; a second
  Node service would mean another deploy, another URL, another password.
* **The image never round-trips through the browser.** The prototype returned
  every uploaded file to the page as base64 and posted it all back on save —
  five 10 MB images meant ~130 MB over the wire for one batch. Here the bytes
  stay on the server in a short-lived batch held between the two steps; the
  browser only ever sends back the edited text.
* **The AI is told who the client is.** Feeding it the company, page URL and
  project turns "two people shaking hands" into something that actually earns
  the ranking — filenames and alt text written for that page, not the pixels.
* **A real archive.** The prototype listed Cloudinary itself: capped at 100
  results, unsearchable, and slow. Every save is recorded locally, so the
  table searches instantly and survives folder reorganisation.
"""
from __future__ import annotations

import base64
import datetime as _dt
import io
import json
import os
import re
import secrets
import threading
from hub import jsonstore
import time
from pathlib import Path

import requests
from flask import Flask, jsonify, render_template, request
from hub.webargs import clamp_int

try:                                    # Hub activity log (absent standalone)
    from hub import audit as hub_audit
except Exception:                       # noqa: BLE001
    hub_audit = None

BASE_DIR = Path(__file__).parent
app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))
app.config["MAX_CONTENT_LENGTH"] = 60 * 1024 * 1024      # 5 x 10 MB + overhead

MAX_FILES = 5
MAX_FILE_BYTES = 10 * 1024 * 1024
ALLOWED_MIME = {
    "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp",
    "image/gif": "gif", "image/avif": "avif",
}
FOLDER_ROOT = os.environ.get("SEO_IMAGES_FOLDER", "smart1-seo-images")

# ------------------------------------------------------------- optimisation
# WebP + quality:auto alone does NOT fix an oversized image: a 6000px camera
# photo stays 6000px and still lands as a multi-megabyte asset. Real weight
# comes off by capping the longest edge first. 2400px covers full-bleed hero
# images on a retina display; nothing on a business website needs more.
SIZE_PRESETS = {
    "1200": ("Content image", 1200),
    "1600": ("Large content", 1600),
    "2400": ("Full-width hero", 2400),
    "full": ("Keep original size", 0),
}
DEFAULT_MAX_EDGE = int(os.environ.get("SEO_IMAGES_MAX_EDGE", "2400"))

# ---------------------------------------------------------------- Cloudinary
# The Hub already configures Cloudinary through CLOUDINARY_URL (the proposal
# builder uses it). The standalone prototype used three separate variables, so
# both are accepted and either one is enough.
_CLOUD_URL = (os.environ.get("CLOUDINARY_URL") or "").strip()
_CLOUD_NAME = (os.environ.get("CLOUDINARY_CLOUD_NAME") or "").strip()
_CLOUD_KEY = (os.environ.get("CLOUDINARY_API_KEY") or "").strip()
_CLOUD_SECRET = (os.environ.get("CLOUDINARY_API_SECRET") or "").strip()

try:
    import cloudinary
    import cloudinary.uploader
    if _CLOUD_URL.startswith("cloudinary://"):
        cloudinary.config(secure=True)                    # reads CLOUDINARY_URL
        CLOUD_READY = True
    elif _CLOUD_NAME and _CLOUD_KEY and _CLOUD_SECRET:
        cloudinary.config(cloud_name=_CLOUD_NAME, api_key=_CLOUD_KEY,
                          api_secret=_CLOUD_SECRET, secure=True)
        CLOUD_READY = True
    else:
        CLOUD_READY = False
except ImportError:                     # pragma: no cover
    cloudinary = None
    CLOUD_READY = False


def ai_ready() -> bool:
    return bool((os.environ.get("OPENAI_API_KEY") or "").strip())


def cloud_ready() -> bool:
    return CLOUD_READY


# ------------------------------------------------------------------ storage
def _data_dir() -> str:
    return jsonstore.data_dir("seo-images")


_INDEX_PATH = os.path.join(_data_dir(), "archive.json")
_index_lock = threading.Lock()


# The images themselves are on Cloudinary; this index is what says which
# client each one belongs to, what it was named and why. Cloudinary would
# still hold every file after a disk loss and nothing would be able to say
# whose they were — so the index goes through hub.jsonstore into the database
# backup. Same atomic write as before, plus a copy that outlives the disk.
def load_archive() -> list[dict]:
    data = jsonstore.read_json(_INDEX_PATH, default=[])
    return data if isinstance(data, list) else []


def save_archive(rows: list[dict]):
    with _index_lock:
        jsonstore.write_json(_INDEX_PATH, rows, indent=1)


# --------------------------------------------------- in-flight batch storage
# Analysed images wait here between "analyse" and "save" so the bytes never
# travel back through the browser. Batches are small, short-lived, and swept
# on every new upload.
_BATCH_TTL = 30 * 60                      # 30 minutes
_batches: dict[str, dict] = {}
_batch_lock = threading.Lock()


def _sweep_batches():
    cutoff = time.time() - _BATCH_TTL
    with _batch_lock:
        for key in [k for k, v in _batches.items() if v["at"] < cutoff]:
            _batches.pop(key, None)


def _put_batch(files: list[dict]) -> str:
    _sweep_batches()
    token = secrets.token_urlsafe(16)
    with _batch_lock:
        _batches[token] = {"at": time.time(), "files": files}
    return token


def _get_batch(token: str) -> list[dict] | None:
    with _batch_lock:
        entry = _batches.get(str(token or ""))
    if not entry or entry["at"] < time.time() - _BATCH_TTL:
        return None
    return entry["files"]


def _drop_batch(token: str):
    with _batch_lock:
        _batches.pop(str(token or ""), None)


# ------------------------------------------------------------------ helpers
def actor_name() -> str:
    return request.environ.get("s1hub.user") or "Unknown"


def _log(event: str, **extra):
    if hub_audit is not None:
        try:
            hub_audit.log("seo_images", event, actor=actor_name(), **extra)
        except Exception:                 # noqa: BLE001
            pass


def slugify(value: str, fallback: str = "image") -> str:
    """Lowercase, hyphenated, safe for a URL — the shape a filename should be."""
    s = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    return s[:90] or fallback


def _clean_alt(value: str) -> str:
    """Alt text: one line, no quotes that would break an HTML attribute."""
    s = re.sub(r"\s+", " ", str(value or "")).strip().replace('"', "'")
    return s[:180]


def _norm_url(value: str) -> str:
    u = str(value or "").strip()
    if u and not u.startswith(("http://", "https://")):
        u = "https://" + u
    return u[:400]


def _image_size(data: bytes) -> tuple[int, int] | None:
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            return im.size
    except Exception:                     # noqa: BLE001 — dimensions are a nicety
        return None


def downscale(data: bytes, max_edge: int) -> tuple[bytes, dict]:
    """Cap the longest edge before anything else happens.

    Done here rather than left to Cloudinary for three reasons: the upload
    itself gets faster (a 12 MB phone photo becomes ~400 KB before it leaves
    the server), Cloudinary stores and bills for less, and the saved asset is
    genuinely small rather than merely served small. EXIF rotation is applied
    first, otherwise a portrait phone photo caps on the wrong edge.
    """
    report = {"resized": False, "from": None, "to": None,
              "bytes_before": len(data), "bytes_after": len(data)}
    if not max_edge or max_edge <= 0:
        return data, report
    try:
        from PIL import Image, ImageOps
        with Image.open(io.BytesIO(data)) as im:
            im = ImageOps.exif_transpose(im)          # honour phone rotation
            w, h = im.size
            report["from"] = [w, h]
            if max(w, h) <= max_edge:
                report["to"] = [w, h]
                return data, report

            im.thumbnail((max_edge, max_edge), Image.LANCZOS)
            report["to"] = list(im.size)
            report["resized"] = True

            out = io.BytesIO()
            if im.mode in ("RGBA", "LA", "P"):        # keep transparency
                im.convert("RGBA").save(out, "WEBP", quality=88, method=5)
            else:
                im.convert("RGB").save(out, "WEBP", quality=88, method=5)
            resized = out.getvalue()
    except Exception:                     # noqa: BLE001 — never lose the upload
        return data, report

    # only keep the resize if it actually helped
    if len(resized) < len(data):
        report["bytes_after"] = len(resized)
        return resized, report
    report["to"] = report["from"]
    report["resized"] = False
    return data, report


# ------------------------------------------------------------------- the AI
_SYSTEM_PROMPT = """You are an SEO specialist naming image assets for a local business website.

You are given one image plus the business and page it will live on. Produce the
filename and alt text that will actually help that page rank and stay accessible.

Return JSON only: {"seoFilename": str, "altText": str}

seoFilename rules:
- lowercase, words separated by single hyphens, no file extension, 3-8 words
- describe what is genuinely IN the image first, then anchor it to the business
  or service when the image supports it (e.g. "technician-replacing-ac-condenser-fishers-in")
- include the city only when the image plausibly relates to a location or service area
- never keyword-stuff, never repeat a word, never use the company name twice
- no dates, no camera filenames, no "image", "photo", "picture", "img", "final", "v2"

altText rules:
- 8-16 words, under 125 characters, sentence case, no trailing period needed
- describe the image for someone who cannot see it — that is its first job
- work in a relevant service or location term ONLY where it is genuinely accurate
- never begin with "Image of" or "Picture of"
- never invent a brand, model, certification, person's name or place that is not
  clearly visible in the image

If the image is a logo, icon, screenshot or graphic rather than a photograph,
say so plainly in the alt text and name it accordingly."""


def generate_seo_data(image_bytes: bytes, mime: str, meta: dict) -> dict:
    """Ask the model for a filename and alt text for one image."""
    api_key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set, so images can't be named "
                           "automatically. You can still type names yourself.")
    data_url = f"data:{mime};base64," + base64.b64encode(image_bytes).decode()
    context = {
        "company": meta.get("company_name", ""),
        "website": meta.get("web_url", ""),
        "project": meta.get("project_name", ""),
        "page": meta.get("page_name", ""),
        "page_topic": meta.get("page_name") or meta.get("project_name") or "",
    }
    payload = {
        "model": os.environ.get("OPENAI_VISION_MODEL",
                                os.environ.get("OPENAI_MODEL", "gpt-4o")),
        "response_format": {"type": "json_object"},
        "temperature": 0.3,
        "max_tokens": 300,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "text",
                 "text": "Business and page context:\n" + json.dumps(context, indent=1)
                         + "\n\nName this image."},
                {"type": "image_url", "image_url": {"url": data_url, "detail": "low"}},
            ]},
        ],
    }
    r = requests.post("https://api.openai.com/v1/chat/completions",
                      headers={"Authorization": f"Bearer {api_key}",
                               "Content-Type": "application/json"},
                      json=payload, timeout=90)
    if not r.ok:
        raise RuntimeError(f"OpenAI returned {r.status_code}: {r.text[:200]}")
    try:  # record spend so /diagnostics doesn't under-report
        from hub import ai as _hub_ai
        _hub_ai.note_usage("seo_images", r.json(), purpose="alt_text")
    except Exception:  # noqa: BLE001
        pass
    out = json.loads(r.json()["choices"][0]["message"]["content"])
    return {"seoFilename": slugify(out.get("seoFilename"), "web-image"),
            "altText": _clean_alt(out.get("altText"))}


def _fallback_names(meta: dict, original: str, index: int) -> dict:
    """No API key, or the model failed — give the team a sane starting point
    to edit rather than a dead end."""
    stem = Path(original or "").stem
    base = slugify(" ".join(x for x in (meta.get("company_name"),
                                        meta.get("page_name") or meta.get("project_name"),
                                        stem) if x), "web-image")
    return {"seoFilename": (base or "web-image")[:90],
            "altText": _clean_alt(
                f"{meta.get('company_name') or 'Business'} "
                f"{meta.get('page_name') or meta.get('project_name') or ''} image "
                f"{index + 1}".strip())}


# =====================================================================
# Pages
# =====================================================================
def _version_label() -> str:
    try:
        from hub import version
        return version.label()
    except Exception:                     # noqa: BLE001
        return ""


@app.route("/")
def index():
    return render_template("index.html", cloud_ready=cloud_ready(),
                           ai_ready=ai_ready(), presets=SIZE_PRESETS,
                           default_edge=str(DEFAULT_MAX_EDGE),
                           version=_version_label(),
                           prefill_company=request.args.get("company", ""),
                           prefill_url=request.args.get("url", ""),
                           prefill_project=request.args.get("project", ""))


@app.route("/gallery")
def gallery_page():
    """The archive on its own, optionally scoped to one client — what the
    Client 360 'See client image gallery' button opens."""
    return render_template("gallery.html", version=_version_label(),
                           company=request.args.get("company", ""),
                           client_slug=request.args.get("client_slug", ""))


@app.route("/house")
def house_page():
    """Manage the sites we run in house."""
    return render_template("house.html", version=_version_label())


@app.route("/health")
def health():
    return jsonify({"ok": True, "cloudinary": cloud_ready(), "ai": ai_ready(),
                    "version": _version_label()})


# =====================================================================
# Step 1 — analyse
# =====================================================================
@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    meta = {
        "company_name": (request.form.get("company_name") or "").strip()[:200],
        "web_url": _norm_url(request.form.get("web_url")),
        "project_name": (request.form.get("project_name") or "").strip()[:200],
        "page_name": (request.form.get("page_name") or "").strip()[:200],
        # which client record this belongs to, when one was picked
        "client_slug": (request.form.get("client_slug") or "").strip()[:120],
        "client_source": (request.form.get("client_source") or "").strip()[:20],
    }
    size_key = (request.form.get("max_edge") or str(DEFAULT_MAX_EDGE)).strip()
    max_edge = SIZE_PRESETS.get(size_key, (None, DEFAULT_MAX_EDGE))[1]
    missing = [label for key, label in (("company_name", "Company name"),
                                        ("web_url", "Web URL"),
                                        ("project_name", "Project name"))
               if not meta[key]]
    if missing:
        return jsonify({"error": ", ".join(missing) + " is required."}), 400

    uploads = [f for f in request.files.getlist("images") if f and f.filename]
    if not uploads:
        return jsonify({"error": "Choose at least one image."}), 400
    if len(uploads) > MAX_FILES:
        return jsonify({"error": f"Up to {MAX_FILES} images at a time."}), 400

    files, warnings = [], []
    for i, up in enumerate(uploads):
        data = up.read()
        if not data:
            warnings.append(f"{up.filename} was empty and has been skipped.")
            continue
        if len(data) > MAX_FILE_BYTES:
            warnings.append(f"{up.filename} is over 10 MB and has been skipped.")
            continue
        mime = (up.mimetype or "").lower()
        if mime not in ALLOWED_MIME:
            warnings.append(f"{up.filename} isn't a supported image type and "
                            "has been skipped.")
            continue

        original_bytes = len(data)
        # Shrink FIRST — the AI reads a 2400px image as well as a 6000px one,
        # and everything downstream gets cheaper.
        data, resize = downscale(data, max_edge)
        if resize["resized"]:
            mime = "image/webp"

        try:
            seo = generate_seo_data(data, mime, meta)
            ai_used = True
            note = ""
        except Exception as exc:          # noqa: BLE001 — never lose the upload
            seo = _fallback_names(meta, up.filename, i)
            ai_used = False
            note = str(exc)[:200]
            if note not in warnings:
                warnings.append("Named without AI — " + note)

        dims = _image_size(data)
        files.append({
            "id": secrets.token_hex(6),
            "original_name": up.filename[:200],
            "mime": mime,
            "bytes": len(data),
            "original_bytes": original_bytes,
            "resize": resize,
            "width": dims[0] if dims else None,
            "height": dims[1] if dims else None,
            "seo_filename": seo["seoFilename"],
            "alt_text": seo["altText"],
            "ai": ai_used,
            "note": note,
            "data": data,                 # stays server-side
        })

    if not files:
        return jsonify({"error": "None of those files could be used. "
                                 + " ".join(warnings)}), 400

    token = _put_batch(files)
    return jsonify({
        "ok": True,
        "batch": token,
        "meta": meta,
        "warnings": warnings,
        "max_edge": max_edge,
        "files": [{
            "id": f["id"],
            "original_name": f["original_name"],
            "seo_filename": f["seo_filename"],
            "alt_text": f["alt_text"],
            "bytes": f["bytes"],
            "original_bytes": f["original_bytes"],
            "resize": f["resize"],
            "width": f["width"], "height": f["height"],
            "ai": f["ai"],
            # a small preview so the page can show what it's naming
            "preview": "data:" + f["mime"] + ";base64,"
                       + base64.b64encode(f["data"]).decode(),
        } for f in files],
    })


# =====================================================================
# Step 2 — rename one image with the AI (per-card "try again")
# =====================================================================
@app.route("/api/rename", methods=["POST"])
def api_rename():
    body = request.get_json(silent=True) or {}
    files = _get_batch(body.get("batch"))
    if files is None:
        return jsonify({"error": "That batch has expired — re-upload the images."}), 410
    hit = next((f for f in files if f["id"] == body.get("id")), None)
    if hit is None:
        return jsonify({"error": "Unknown image."}), 404
    meta = body.get("meta") or {}
    try:
        seo = generate_seo_data(hit["data"], hit["mime"], meta)
    except Exception as exc:              # noqa: BLE001
        return jsonify({"error": str(exc)}), 502
    hit["seo_filename"], hit["alt_text"] = seo["seoFilename"], seo["altText"]
    return jsonify({"ok": True, "id": hit["id"],
                    "seo_filename": hit["seo_filename"], "alt_text": hit["alt_text"]})


# =====================================================================
# Step 3 — save the approved set to Cloudinary
# =====================================================================
@app.route("/api/finalize", methods=["POST"])
def api_finalize():
    if not cloud_ready():
        return jsonify({"error": "Cloudinary isn't configured. Set CLOUDINARY_URL "
                                 "(or the cloud name / key / secret) and redeploy."}), 503

    body = request.get_json(silent=True) or {}
    files = _get_batch(body.get("batch"))
    if files is None:
        return jsonify({"error": "That batch has expired — re-upload the images."}), 410

    meta = body.get("meta") or {}
    company = str(meta.get("company_name") or "").strip()[:200]
    project = str(meta.get("project_name") or "").strip()[:200]
    if not company or not project:
        return jsonify({"error": "Company and project are required."}), 400
    web_url = _norm_url(meta.get("web_url"))
    page = str(meta.get("page_name") or "").strip()[:200]

    approved = body.get("files") or []
    by_id = {f["id"]: f for f in files}
    folder = f"{FOLDER_ROOT}/{slugify(company, 'client')}/{slugify(project, 'project')}"
    stamp = _dt.datetime.now()
    saved, errors, used_names = [], [], set()

    for item in approved:
        src = by_id.get(str(item.get("id")))
        if src is None:
            continue
        name = slugify(item.get("seo_filename") or src["seo_filename"], "web-image")
        alt = _clean_alt(item.get("alt_text") or src["alt_text"])
        # two images in one batch can end up with the same name once edited
        base_name, n = name, 2
        while name in used_names:
            name = f"{base_name}-{n}"
            n += 1
        used_names.add(name)

        try:
            # Through hub.storage. The bytes are already WebP by this point
            # (the pipeline converts before saving), so the .webp name carries
            # what format= used to assert, and the resource type is derived
            # rather than stated. Folder and id are unchanged, so nothing moves.
            from hub import storage
            _asset = storage.put(
                "seo_images", f"{name}.webp", src["data"],
                folder=folder, public_id=f"{folder}/{name}", overwrite=False,
                context={"company": company, "url": web_url, "project": project,
                         "page": page or "N/A", "alt": alt})
            result = {"secure_url": _asset.url, "public_id": _asset.public_id,
                      "bytes": _asset.bytes, "format": "webp"}
        except Exception as exc:          # noqa: BLE001
            errors.append(f"{name}: {exc}")
            continue

        # Into the client's main gallery, under SEO images. These used to stop
        # at Cloudinary, which meant the optimised images made for a client's
        # own pages were not visible on the one page the client is sent to.
        # Filing never raises and is not allowed to fail the save — the image
        # is already stored and the page already has its URL.
        try:
            from modules.image_picker import filing
            filing.file_asset(
                client_name=company, public_id=result.get("public_id", ""),
                url=result.get("secure_url", ""), kind="seo_image",
                key=(page or project or "")[:80],
                label=f"SEO images — {page or project}" if (page or project)
                      else "SEO images",
                filename=f"{name}.webp", alt=alt, resource_type="image",
                size_bytes=result.get("bytes"), provider="seo_image",
                saved_by=actor_name())
        except Exception:                 # noqa: BLE001
            pass

        saved.append({
            "id": secrets.token_hex(8),
            "public_id": result.get("public_id", ""),
            "url": result.get("secure_url", ""),
            "filename": name + ".webp",
            "seo_filename": name,
            "alt_text": alt,
            "company": company,
            "company_slug": slugify(company, "client"),
            # the client record this belongs to, so Client 360 can find it
            "client_slug": str(meta.get("client_slug") or "").strip()[:120]
                           or slugify(company, "client"),
            "client_source": str(meta.get("client_source") or "").strip()[:20],
            "web_url": web_url,
            "project": project,
            "page": page,
            "width": result.get("width"),
            "height": result.get("height"),
            "bytes": result.get("bytes"),
            "original_name": src["original_name"],
            "original_bytes": src["bytes"],
            "saved_at": stamp.strftime("%Y-%m-%d %H:%M"),
            "saved_date": stamp.date().isoformat(),
            "saved_by": actor_name(),
        })

    if saved:
        rows = load_archive()
        rows = saved + rows
        save_archive(rows[:5000])
        _log("images_saved", detail=company, project=project, count=len(saved))

    if not saved:
        return jsonify({"error": "Nothing was saved. " + " ".join(errors)}), 502

    _drop_batch(body.get("batch"))
    return jsonify({"ok": True, "saved": saved, "errors": errors})


# =====================================================================
# Clients — search what we already have, or add a house site
# =====================================================================
@app.route("/api/save-one", methods=["POST"])
def api_save_one():
    """Save a single already-processed image into a client's gallery.

    Used by the Image Optimizer, which does its own resizing and just needs
    somewhere to put the result. Deliberately does not re-encode: the file
    arriving here has already been sized and converted by the tool the person
    was using, and running it through WebP again would undo their choices.
    """
    company = (request.form.get("company") or "").strip()
    upload = request.files.get("file")
    if not company:
        return jsonify({"error": "A client is required."}), 400
    if not upload or not upload.filename:
        return jsonify({"error": "No file received."}), 400
    if not cloud_ready():
        return jsonify({"error": "The client gallery isn't configured "
                                 "(CLOUDINARY_URL is not set)."}), 400

    data = upload.read(25 * 1024 * 1024)
    if not data:
        return jsonify({"error": "That file was empty."}), 400

    base = re.sub(r"[^a-z0-9]+", "-",
                  os.path.splitext(upload.filename)[0].lower()).strip("-") or "image"
    folder = f"{FOLDER_ROOT}/{slugify(company, 'client')}/optimized"
    try:
        # Same shared uploader as the pipeline above. This save is new in
        # v1.54; routing it through hub.storage from the start keeps the module
        # on one path rather than reintroducing a second one.
        from hub import storage
        _ext = (os.path.splitext(upload.filename)[1] or ".png").lower()
        _asset = storage.put(
            "seo_images", f"{base}{_ext}", data,
            folder=folder, public_id=f"{folder}/{base}", overwrite=False,
            context={"company": company, "source": "image-optimizer"})
        result = {"secure_url": _asset.url, "public_id": _asset.public_id,
                  "bytes": _asset.bytes}
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"The gallery rejected it "
                                 f"({type(exc).__name__})."}), 502

    url = result.get("secure_url", "")
    try:
        # Same archive the pipeline writes, so it shows in the client gallery
        # alongside everything else rather than only existing in Cloudinary.
        rows = load_archive()
        rows.insert(0, {
            "company": company, "url": url,
            "public_id": result.get("public_id", ""),
            "filename": upload.filename, "alt_text": "",
            "project": "Optimized", "page": "",
            "bytes": result.get("bytes", len(data)),
            "saved_by": actor_name(), "saved_at": _version_label(),
        })
        save_archive(rows)
        _log("optimizer_saved", company=company)
    except Exception:  # noqa: BLE001
        pass          # the image is stored; the index entry is a convenience

    return jsonify({"ok": True, "url": url,
                    "note": f"Saved to {company}'s gallery."})


@app.route("/api/clients")
def api_clients():
    """Type-ahead over every client the Hub knows: Knack, website records
    and house URLs."""
    try:
        from hub import clients_registry
    except Exception as exc:              # noqa: BLE001 — standalone dev
        return jsonify({"clients": [], "error": str(exc)})
    q = request.args.get("q", "")
    rows = clients_registry.search_clients(q, limit=12)
    return jsonify({"clients": [{
        "name": r["name"], "slug": r["slug"], "url": r.get("url", ""),
        "domain": r.get("domain", ""), "source": r.get("source", ""),
        "is_house": r.get("is_house", False), "is_seo": r.get("is_seo", False),
        "product_count": r.get("product_count", 0),
    } for r in rows]})


@app.route("/api/clients/house")
def api_house_clients():
    try:
        from hub import clients_registry
    except Exception as exc:              # noqa: BLE001
        return jsonify({"clients": [], "error": str(exc)})
    return jsonify({"clients": clients_registry.house_clients()})


@app.route("/api/clients/house/delete", methods=["POST"])
def api_delete_house_client():
    try:
        from hub import clients_registry
    except Exception as exc:              # noqa: BLE001
        return jsonify({"error": str(exc)}), 500
    body = request.get_json(silent=True) or {}
    ok = clients_registry.delete_house_client(str(body.get("slug") or ""))
    if ok:
        clients_registry.all_clients(refresh=True)
        _log("house_client_removed", detail=str(body.get("slug") or ""))
    return jsonify({"ok": ok})


@app.route("/api/clients/house", methods=["POST"])
def api_add_house_client():
    """Register a site we run in house so it can be picked like any client."""
    try:
        from hub import clients_registry
    except Exception as exc:              # noqa: BLE001
        return jsonify({"error": str(exc)}), 500
    body = request.get_json(silent=True) or {}
    try:
        row = clients_registry.add_house_client(
            body.get("name", ""), body.get("url", ""), body.get("notes", ""),
            actor=actor_name())
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    clients_registry.all_clients(refresh=True)
    _log("house_client_added", detail=row["name"], url=row.get("url", ""))
    return jsonify({"ok": True, "client": row})


@app.route("/api/projects")
def api_projects():
    """Projects already used for a client — so a second batch joins the first
    instead of starting a near-duplicate."""
    company = (request.args.get("company") or "").strip().lower()
    q = (request.args.get("q") or "").strip().lower()
    seen: dict[str, dict] = {}
    for r in load_archive():
        if company and str(r.get("company", "")).lower() != company:
            continue
        proj = str(r.get("project") or "").strip()
        if not proj or (q and q not in proj.lower()):
            continue
        key = (str(r.get("company", "")).lower(), proj.lower())
        entry = seen.setdefault(str(key), {
            "project": proj, "company": r.get("company", ""),
            "web_url": r.get("web_url", ""), "pages": set(),
            "count": 0, "last": r.get("saved_at", "")})
        entry["count"] += 1
        if r.get("page"):
            entry["pages"].add(r["page"])
        if str(r.get("saved_at", "")) > str(entry["last"]):
            entry["last"] = r.get("saved_at", "")
    out = [{**v, "pages": sorted(v["pages"])} for v in seen.values()]
    out.sort(key=lambda p: str(p["last"]), reverse=True)
    return jsonify({"projects": out[:25]})


# =====================================================================
# Archive
# =====================================================================
@app.route("/api/gallery")
def api_gallery():
    q = (request.args.get("q") or "").strip().lower()
    company = (request.args.get("company") or "").strip().lower()
    client_slug = (request.args.get("client_slug") or "").strip().lower()
    rows = load_archive()
    if company:
        rows = [r for r in rows if str(r.get("company", "")).lower() == company]
    if client_slug:
        rows = [r for r in rows
                if str(r.get("client_slug", "")).lower() == client_slug
                or str(r.get("company_slug", "")).lower() == client_slug]
    if q:
        rows = [r for r in rows if any(
            q in str(r.get(k, "")).lower()
            for k in ("company", "project", "page", "seo_filename", "alt_text",
                      "web_url", "saved_by", "original_name"))]
    try:
        limit = clamp_int(request.args.get("limit"), 200, 1, 1000)
    except (TypeError, ValueError):
        limit = 200
    return jsonify({"gallery": rows[:limit], "total": len(load_archive())})


@app.route("/api/gallery/update", methods=["POST"])
def api_gallery_update():
    """Edit alt text after the fact, or drop a record."""
    body = request.get_json(silent=True) or {}
    rid = str(body.get("id") or "")
    rows = load_archive()
    hit = next((r for r in rows if r.get("id") == rid), None)
    if hit is None:
        return jsonify({"error": "Not found."}), 404

    if body.get("delete"):
        if hit.get("public_id") and cloud_ready():
            try:
                cloudinary.uploader.destroy(hit["public_id"], invalidate=True)
            except Exception as exc:      # noqa: BLE001 — record still goes
                print("cloudinary destroy failed:", exc)
        save_archive([r for r in rows if r.get("id") != rid])
        _log("image_deleted", detail=hit.get("company", ""),
             name=hit.get("filename", ""))
        return jsonify({"ok": True})

    if "alt_text" in body:
        hit["alt_text"] = _clean_alt(body["alt_text"])
        if hit.get("public_id") and cloud_ready():
            try:                          # keep Cloudinary's copy in step
                cloudinary.uploader.explicit(
                    hit["public_id"], type="upload",
                    context=f"alt={hit['alt_text']}")
            except Exception as exc:      # noqa: BLE001
                print("cloudinary context update failed:", exc)
    save_archive(rows)
    return jsonify({"ok": True, "row": hit})


@app.route("/api/gallery/export")
def api_gallery_export():
    """The archive as CSV — what a developer or client actually wants."""
    import csv
    q = (request.args.get("q") or "").strip().lower()
    rows = load_archive()
    if q:
        rows = [r for r in rows if any(
            q in str(r.get(k, "")).lower()
            for k in ("company", "project", "page", "seo_filename", "alt_text"))]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Company", "Web URL", "Project", "Page", "Filename", "Alt text",
                "Live URL", "Width", "Height", "KB", "Saved", "Saved by"])
    for r in rows:
        w.writerow([r.get("company", ""), r.get("web_url", ""), r.get("project", ""),
                    r.get("page", ""), r.get("filename", ""), r.get("alt_text", ""),
                    r.get("url", ""), r.get("width", ""), r.get("height", ""),
                    round((r.get("bytes") or 0) / 1024), r.get("saved_at", ""),
                    r.get("saved_by", "")])
    from flask import Response
    return Response(buf.getvalue(), mimetype="text/csv", headers={
        "Content-Disposition": 'attachment; filename="smart1-seo-images.csv"'})
