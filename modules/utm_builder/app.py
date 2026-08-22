"""Smart 1 Hub — UTM Builder.

Builds tagged campaign URLs, keeps them, and makes them findable again.

Three things make this more than a URL concatenator:

* **Links are filed against a client and, where it applies, the product they
  belong to.** A tagged URL is worthless six months later if nobody can say
  which campaign it served. Products come from the client's live Knack
  records, so the link is tied to real billable work rather than a typed note.
* **The source and medium vocabularies are editable.** Consistency is the
  whole point of UTM tagging — "facebook" and "Facebook" and "fb" split one
  campaign into three in Analytics. The Hub keeps one editable list per
  parameter, seeded with sensible defaults, and every builder picks from it.
* **Bulk build.** One campaign across eight placements is eight links that
  differ only by source and medium. Pick the combinations and get them all
  at once, consistently spelled.

Values are normalised the way Analytics expects: lowercased, spaces to
hyphens, no stray punctuation — done centrally so it can't be forgotten.
"""
from __future__ import annotations

import csv
import datetime as _dt
import io
import os
import re
import secrets
import threading
from pathlib import Path
from urllib.parse import (parse_qsl, quote, urlencode, urlparse, urlunparse)

from flask import Flask, Response, jsonify, render_template, request
from hub import jsonstore

try:
    from hub import audit as hub_audit
except Exception:                                     # noqa: BLE001
    hub_audit = None

BASE_DIR = Path(__file__).parent
app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))

UTM_FIELDS = ("utm_source", "utm_medium", "utm_campaign", "utm_term",
              "utm_content", "utm_id")

DEFAULT_VOCAB = {
    "utm_source": [
        "google", "bing", "facebook", "instagram", "linkedin", "youtube",
        "tiktok", "x", "pinterest", "yelp", "email", "newsletter", "sms",
        "direct-mail", "qr-code", "partner", "referral", "smart1",
    ],
    "utm_medium": [
        "cpc", "ppc", "display", "banner", "retargeting", "organic",
        "organic-social", "paid-social", "email", "sms", "affiliate",
        "referral", "print", "video", "audio", "ctv", "dooh", "geofence",
    ],
    "utm_campaign": [
        "spring-promo", "summer-promo", "fall-promo", "winter-promo",
        "always-on", "brand", "lead-gen", "grand-opening", "seasonal-service",
    ],
}

_lock = threading.Lock()


# ------------------------------------------------------------------ storage
def _data_dir() -> str:
    return jsonstore.data_dir("utm")


def _links_path() -> str:
    return os.path.join(_data_dir(), "links.json")


def _vocab_path() -> str:
    return os.path.join(_data_dir(), "vocabulary.json")


# Reads and writes go through hub.jsonstore, which keeps the atomic .tmp +
# rename this module already had and adds a mirror into the database. The
# Render disk these files live on is not part of the database backup and does
# not survive being recreated, and a tagged URL nobody can trace back to a
# campaign is the exact failure this module exists to prevent — and the
# vocabulary is the hand-curated spelling list that keeps them consistent.

def _read(path: str, fallback):
    return jsonstore.read_json(path, default=fallback)


def _write(path: str, data):
    with _lock:
        jsonstore.write_json(path, data, indent=1)


def load_vocab() -> dict:
    """Editable option lists, seeded from the defaults on first use."""
    stored = _read(_vocab_path(), {})
    out = {}
    for field in ("utm_source", "utm_medium", "utm_campaign"):
        vals = stored.get(field)
        out[field] = vals if isinstance(vals, list) else list(DEFAULT_VOCAB[field])
    return out


def save_vocab(vocab: dict):
    _write(_vocab_path(), vocab)


def load_links() -> list[dict]:
    rows = _read(_links_path(), [])
    return rows if isinstance(rows, list) else []


def save_links(rows: list[dict]):
    _write(_links_path(), rows[:8000])


# ------------------------------------------------------------------ helpers
def normalise(value: str) -> str:
    """Analytics treats 'Paid Social' and 'paid-social' as different values, so
    everything is lowercased and hyphenated once, here."""
    v = str(value or "").strip().lower()
    v = re.sub(r"[\s_]+", "-", v)
    v = re.sub(r"[^a-z0-9\-.+|]", "", v)
    v = re.sub(r"-{2,}", "-", v).strip("-")
    return v[:120]


def clean_base_url(url: str) -> tuple[str, dict]:
    """Split a URL into its base and any query parameters already on it, so
    existing tracking isn't silently discarded or duplicated."""
    url = str(url or "").strip()
    if not url:
        raise ValueError("Enter the URL you're linking to.")
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    parts = urlparse(url)
    if not parts.netloc or "." not in parts.netloc:
        raise ValueError("That doesn't look like a valid URL.")
    existing = dict(parse_qsl(parts.query, keep_blank_values=False))
    base = urlunparse((parts.scheme.lower(), parts.netloc.lower(), parts.path,
                       parts.params, "", ""))
    return base.rstrip("?"), existing


def build_url(base: str, params: dict, fragment: str = "") -> str:
    base_clean, existing = clean_base_url(base)
    merged = {k: v for k, v in existing.items() if not k.lower().startswith("utm_")}
    for field in UTM_FIELDS:
        val = params.get(field)
        if val:
            merged[field] = val
    query = urlencode(merged, quote_via=quote, safe="|.+-")
    url = base_clean + ("?" + query if query else "")
    if fragment:
        url += "#" + fragment.lstrip("#")
    return url


def actor_name() -> str:
    return request.environ.get("s1hub.user") or "Unknown"


def _log(event: str, **extra):
    if hub_audit is not None:
        try:
            hub_audit.log("utm", event, actor=actor_name(), **extra)
        except Exception:                             # noqa: BLE001
            pass


def _version() -> str:
    try:
        from hub import version
        return version.label()
    except Exception:                                 # noqa: BLE001
        return ""


# =====================================================================
# Pages
# =====================================================================
@app.route("/")
def index():
    return render_template("index.html", version=_version(), vocab=load_vocab(),
                           client=request.args.get("client", ""),
                           url=request.args.get("url", ""))


@app.route("/health")
def health():
    return jsonify({"ok": True, "version": _version(),
                    "links": len(load_links())})


# =====================================================================
# Building
# =====================================================================
@app.route("/api/build", methods=["POST"])
def api_build():
    """Preview one or many links without saving anything."""
    body = request.get_json(silent=True) or {}
    base = body.get("url", "")
    try:
        clean_base_url(base)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    campaign = normalise(body.get("utm_campaign", ""))
    if not campaign:
        return jsonify({"error": "A campaign name is required — it's what ties "
                                 "the links together in Analytics."}), 400

    sources = [normalise(s) for s in (body.get("sources") or []) if normalise(s)]
    mediums = [normalise(m) for m in (body.get("mediums") or []) if normalise(m)]
    if not sources:
        return jsonify({"error": "Pick at least one source."}), 400
    if not mediums:
        return jsonify({"error": "Pick at least one medium."}), 400
    if len(sources) * len(mediums) > 60:
        return jsonify({"error": "That's more than 60 combinations — narrow it down."}), 400

    common = {"utm_campaign": campaign,
              "utm_term": normalise(body.get("utm_term", "")),
              "utm_content": normalise(body.get("utm_content", "")),
              "utm_id": normalise(body.get("utm_id", ""))}
    fragment = str(body.get("fragment") or "").strip()

    links = []
    for s in sources:
        for m in mediums:
            params = {**common, "utm_source": s, "utm_medium": m}
            links.append({"utm_source": s, "utm_medium": m, **common,
                          "url": build_url(base, params, fragment)})
    return jsonify({"links": links, "count": len(links)})


@app.route("/api/links", methods=["GET"])
def api_links():
    q = (request.args.get("q") or "").strip().lower()
    client = (request.args.get("client") or "").strip().lower()
    rows = load_links()
    if client:
        rows = [r for r in rows if str(r.get("client", "")).lower() == client]
    if q:
        rows = [r for r in rows if any(
            q in str(r.get(k, "")).lower()
            for k in ("url", "base_url", "client", "product", "utm_campaign",
                      "utm_source", "utm_medium", "utm_content", "utm_term",
                      "label", "created_by"))]
    try:
        limit = max(1, max(1, min(2000, int(request.args.get("limit") or 300))))
    except (TypeError, ValueError):
        limit = 300
    return jsonify({"links": rows[:limit], "total": len(load_links())})


@app.route("/api/links", methods=["POST"])
def api_save_links():
    """Save a built batch against a client and product."""
    body = request.get_json(silent=True) or {}
    incoming = body.get("links") or []
    if not incoming:
        return jsonify({"error": "Nothing to save."}), 400

    client = str(body.get("client") or "").strip()[:200]
    product = str(body.get("product") or "").strip()[:200]
    label = str(body.get("label") or "").strip()[:200]
    base_url = str(body.get("url") or "").strip()[:600]
    now = _dt.datetime.now()

    rows = load_links()
    existing = {r.get("url") for r in rows}
    saved, skipped = [], 0
    for link in incoming:
        url = str(link.get("url") or "").strip()
        if not url:
            continue
        if url in existing:                            # the same link twice is noise
            skipped += 1
            continue
        existing.add(url)
        record = {
            "id": "utm_" + secrets.token_hex(5),
            "url": url,
            "base_url": base_url,
            "client": client,
            "client_slug": str(body.get("client_slug") or "")[:120],
            "product": product,
            "label": label,
            "created": now.strftime("%Y-%m-%d %H:%M"),
            "created_date": now.date().isoformat(),
            "created_by": actor_name(),
        }
        for f in UTM_FIELDS:
            record[f] = str(link.get(f) or "")
        saved.append(record)

    if saved:
        save_links(saved + rows)
        _log("links_saved", detail=client or "unfiled", count=len(saved),
             campaign=saved[0].get("utm_campaign", ""))
    return jsonify({"ok": True, "saved": len(saved), "skipped": skipped,
                    "links": load_links()[:300]})


@app.route("/api/links/delete", methods=["POST"])
def api_delete_links():
    body = request.get_json(silent=True) or {}
    ids = set(body.get("ids") or ([body["id"]] if body.get("id") else []))
    if not ids:
        return jsonify({"error": "Nothing selected."}), 400
    rows = load_links()
    kept = [r for r in rows if r.get("id") not in ids]
    save_links(kept)
    _log("links_deleted", count=len(rows) - len(kept))
    return jsonify({"ok": True, "removed": len(rows) - len(kept),
                    "links": kept[:300]})


@app.route("/api/links/export")
def api_export():
    q = (request.args.get("q") or "").strip().lower()
    client = (request.args.get("client") or "").strip().lower()
    rows = load_links()
    if client:
        rows = [r for r in rows if str(r.get("client", "")).lower() == client]
    if q:
        rows = [r for r in rows if any(
            q in str(r.get(k, "")).lower()
            for k in ("url", "client", "product", "utm_campaign", "utm_source"))]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Client", "Product", "Label", "Campaign", "Source", "Medium",
                "Term", "Content", "ID", "Landing page", "Tagged URL",
                "Created", "Created by"])
    for r in rows:
        w.writerow([r.get("client", ""), r.get("product", ""), r.get("label", ""),
                    r.get("utm_campaign", ""), r.get("utm_source", ""),
                    r.get("utm_medium", ""), r.get("utm_term", ""),
                    r.get("utm_content", ""), r.get("utm_id", ""),
                    r.get("base_url", ""), r.get("url", ""),
                    r.get("created", ""), r.get("created_by", "")])
    return Response(buf.getvalue(), mimetype="text/csv", headers={
        "Content-Disposition": 'attachment; filename="smart1-utm-links.csv"'})


# =====================================================================
# Editable vocabularies
# =====================================================================
@app.route("/api/vocab", methods=["GET"])
def api_vocab():
    return jsonify({"vocab": load_vocab(), "defaults": DEFAULT_VOCAB})


@app.route("/api/vocab", methods=["POST"])
def api_vocab_update():
    """Add, remove, rename or reset the options for one parameter."""
    body = request.get_json(silent=True) or {}
    field = str(body.get("field") or "")
    if field not in ("utm_source", "utm_medium", "utm_campaign"):
        return jsonify({"error": "Unknown parameter."}), 400

    vocab = load_vocab()
    values = vocab.get(field, [])

    if body.get("reset"):
        values = list(DEFAULT_VOCAB[field])
    elif body.get("remove"):
        values = [v for v in values if v != normalise(body["remove"])]
    elif body.get("rename"):
        old, new = normalise(body["rename"]), normalise(body.get("to", ""))
        if not new:
            return jsonify({"error": "Give it a new name."}), 400
        values = [new if v == old else v for v in values]
    elif body.get("add"):
        new = normalise(body["add"])
        if not new:
            return jsonify({"error": "That value isn't usable once cleaned up."}), 400
        if new in values:
            return jsonify({"error": f"“{new}” is already on the list."}), 400
        values.append(new)
    elif isinstance(body.get("values"), list):
        values = [normalise(v) for v in body["values"] if normalise(v)]
    else:
        return jsonify({"error": "Nothing to change."}), 400

    seen, clean = set(), []
    for v in values:
        if v and v not in seen:
            seen.add(v)
            clean.append(v)
    vocab[field] = clean
    save_vocab(vocab)
    _log("vocabulary_updated", field=field, count=len(clean))
    return jsonify({"ok": True, "vocab": vocab})


# =====================================================================
# Clients and their products
# =====================================================================
@app.route("/api/clients")
def api_clients():
    try:
        from hub import clients_registry
    except Exception as exc:                          # noqa: BLE001
        return jsonify({"clients": [], "error": str(exc)})
    rows = clients_registry.search_clients(request.args.get("q", ""), limit=12)
    return jsonify({"clients": [{
        "name": r["name"], "slug": r["slug"], "domain": r.get("domain", ""),
        "url": r.get("url", ""), "is_house": r.get("is_house", False),
        "is_seo": r.get("is_seo", False),
        "products": r.get("products", [])} for r in rows]})


@app.route("/api/products")
def api_products():
    """The live products on a client's account — what a link gets filed under."""
    client = (request.args.get("client") or "").strip()
    if not client:
        return jsonify({"products": []})
    try:
        from hub import clients_registry
        hit = clients_registry.find_client(client)
    except Exception as exc:                          # noqa: BLE001
        return jsonify({"products": [], "error": str(exc)})
    return jsonify({"products": (hit or {}).get("products", [])})
