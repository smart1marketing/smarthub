"""Knack object_135 — IO products, read live.

Products, campaigns and insertion orders have been served from JSON files
exported by hand into `clients_app/data/`. Nothing refreshed them, so Client
360 showed whatever was true on the day of the last export. That's why Icon
Solar's insertion orders looked stale: they were.

This reads the same records from the Knack API. The static files stay as a
fallback — if Knack is unreachable the Hub shows old data with a visible age
rather than an empty page, because a blank client record reads as "we have no
products" rather than "we couldn't reach Knack".

## Caching

object_135 is large — every product on every IO. A page render can't afford a
full pull, so records are cached on disk with a TTL and refreshed by the
scheduler. `age_minutes` is returned with every read so the UI can say how
fresh the number is instead of implying it's live.

## Field ids

Pinned as named constants. Knack field ids are opaque, and `field_2338`
appearing inline three files away is unmaintainable. If Knack renumbers, this
is the one file to change.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone

OBJECT = "object_135"

# --- object_135 field map -------------------------------------------------
F_PRODUCT_NUM      = "field_2640"   # Product #
F_PRODUCT_NAME     = "field_2775"   # Product Name
F_PRODUCT_TYPE     = "field_2467"   # Product Type - Text
F_TACTICS          = "field_2805"   # Tactic(s)
F_IO               = "field_2311"   # Insertion Order
F_IO_NUM           = "field_2469"   # IO #
F_START            = "field_2299"   # Start Date
F_END              = "field_2313"   # End Date
F_STATUS           = "field_2300"   # Status
F_IO_STATUS        = "field_2581"   # IO Status
F_CLIENT           = "field_2308"   # Client
F_CLIENT_ORG       = "field_2870"   # Client Organization
F_MEDIA_PARTNER    = "field_2307"   # Media Partner
F_IO_CAMPAIGN      = "field_2309"   # IO Campaign Name
F_PROD_CAMPAIGN    = "field_3340"   # Product Campaign Name
F_DISPLAY_CAMPAIGN = "field_3341"   # Display Campaign Name
F_MONTHLY_COST     = "field_2338"   # Monthly Cost
F_TOTAL_COST       = "field_2339"   # Total Cost
F_MO_BILLING       = "field_3206"   # Mo Billing Amount
F_DASHBOARD_URL    = "field_2820"   # Dashboard URL
F_DASH_VALUE       = "field_2976"   # Client Dashboard URL Value
F_TRAFFICKER       = "field_2312"   # Trafficker
F_SALES            = "field_3322"   # Internal Sales
F_RECORD_ID        = "field_3370"   # Record ID
F_CLICK_THRU       = "field_2413"   # Click Thru URL
# The creative file link (Drive/Dropbox/PDF) that the committed export exposes
# as `url`. The live reader never fetched it, which is why creative filed in
# Knack was invisible to the Stale Creative report once products were read
# live. Set KNACK_CREATIVE_FIELD to the real field id to switch it on; until
# then the row carries no creative link and the report falls back to the
# export, rather than mistaking the click-thru URL for artwork.
# External Creative Link 1-4. Knack holds up to four per product, so a product
# with a proof, a cutdown and two revisions is four rows here rather than one
# — which is also why the newest of them dates the creative, not the first.
F_CREATIVE_URLS    = [f.strip() for f in (
    os.environ.get("KNACK_CREATIVE_FIELDS")
    or "field_3422,field_3425,field_3426,field_3427").split(",") if f.strip()]
F_DISPLAY_CLICK    = "field_2414"   # Display Click Thru URL
F_GEO              = "field_2546"   # Geographic Target

CACHE_NAME = "knack_products.json"
DEFAULT_TTL_MINUTES = 180


# ---------------------------------------------------------------------------
# Value coercion
# ---------------------------------------------------------------------------

def _href(v) -> str:
    """Pull the URL out of a Knack link field.

    Link fields arrive as HTML — <a href="...">Dashboard</a> — and _text()
    correctly returns the label. For a dashboard we want the destination, and
    "Dashboard" as a URL silently produces a broken link on every product row.
    """
    if isinstance(v, dict):
        for k in ("url", "href", "value"):
            if v.get(k):
                return str(v[k]).strip()
    m = re.search(r'href=[\'"]([^\'"]+)', str(v or ""))
    if m:
        return m.group(1).strip()
    t = _text(v)
    return t if t.startswith("http") else ""


def _text(v) -> str:
    """Knack returns strings, dicts, lists of connections, and HTML."""
    if v is None:
        return ""
    if isinstance(v, list):
        return ", ".join(x for x in (_text(i) for i in v) if x)
    if isinstance(v, dict):
        for k in ("identifier", "label", "name", "value", "url", "email"):
            if v.get(k):
                return str(v[k]).strip()
        return ""
    s = re.sub(r"<[^>]+>", " ", str(v))
    return re.sub(r"\s+", " ", s).strip()


def _money(v) -> float:
    s = re.sub(r"[^0-9.\-]", "", _text(v))
    try:
        return round(float(s), 2) if s not in ("", "-", ".") else 0.0
    except ValueError:
        return 0.0


def _date(v) -> str:
    """Knack dates arrive as {'date': '01/2026', 'iso_timestamp': ...}."""
    if isinstance(v, dict):
        iso = v.get("iso_timestamp") or v.get("date_formatted") or v.get("date")
        if iso:
            return str(iso)[:10]
    return _text(v)[:10]


def _row(rec: dict) -> dict:
    """One product, flattened. Campaign name is carried deliberately — it was
    missing from the IO list, and a product without its campaign can't be
    matched to the creative made for it."""
    campaign = (_text(rec.get(F_PROD_CAMPAIGN))
                or _text(rec.get(F_DISPLAY_CAMPAIGN))
                or _text(rec.get(F_IO_CAMPAIGN)))
    return {
        "id": rec.get("id", ""),
        "record_id": _text(rec.get(F_RECORD_ID)),
        "product_num": _text(rec.get(F_PRODUCT_NUM)),
        "product": (_text(rec.get(F_PRODUCT_NAME))
                    or _text(rec.get(F_PRODUCT_TYPE))),
        "kind": _text(rec.get(F_PRODUCT_TYPE)),
        "tactics": _text(rec.get(F_TACTICS)),
        "io": _text(rec.get(F_IO_NUM)) or _text(rec.get(F_IO)),
        "campaign": campaign,
        "client": _text(rec.get(F_CLIENT)),
        "organization": _text(rec.get(F_CLIENT_ORG)),
        "partner": _text(rec.get(F_MEDIA_PARTNER)),
        "sales": _text(rec.get(F_SALES)),
        "trafficker": _text(rec.get(F_TRAFFICKER)),
        "status": _text(rec.get(F_STATUS)),
        "io_status": _text(rec.get(F_IO_STATUS)),
        "start": _date(rec.get(F_START)),
        "end": _date(rec.get(F_END)),
        "monthly": _money(rec.get(F_MONTHLY_COST)),
        "total": _money(rec.get(F_TOTAL_COST)),
        "billing": _money(rec.get(F_MO_BILLING)),
        "dash": (_href(rec.get(F_DASHBOARD_URL))
                 or _href(rec.get(F_DASH_VALUE))),
        "url": _href(rec.get(F_CLICK_THRU)) or _text(rec.get(F_CLICK_THRU)),
        "creative_urls": [u for u in (
            (_href(rec.get(f)) or _text(rec.get(f))) for f in F_CREATIVE_URLS
        ) if u],
        "display_url": (_href(rec.get(F_DISPLAY_CLICK))
                        or _text(rec.get(F_DISPLAY_CLICK))),
        "geo": _text(rec.get(F_GEO)),
    }


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def _cache_path() -> str:
    from . import jsonstore
    return os.path.join(jsonstore.data_root(), CACHE_NAME)


def _read_cache() -> dict:
    from . import jsonstore
    # restore=False: never pull this back from the mirror. A stale cache
    # restored after a disk loss would be served with a fresh-looking
    # age_minutes and be believed; an empty one just refetches from Knack.
    data = jsonstore.read_json(_cache_path(), default={}, restore=False)
    return data if isinstance(data, dict) else {}


def _write_cache(rows: list[dict]) -> None:
    from . import jsonstore
    payload = {"fetched": time.time(), "count": len(rows), "rows": rows}
    # durable=False, and this is the file the distinction was written for.
    # It is every product on every IO — the largest thing on the disk — and
    # the scheduler re-pulls it from Knack every three hours, so backing it up
    # would put megabytes into every database backup to save at most one API
    # call. Stated here so "not backed up" is a decision, not an oversight.
    jsonstore.write_json(_cache_path(), payload, durable=False)


def configured() -> bool:
    return bool((os.environ.get("KNACK_APP_ID") or "").strip()
                and (os.environ.get("KNACK_API_KEY") or "").strip())


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def fetch(limit: int = 12000) -> list[dict]:
    """Pull every product from Knack. Returns [] rather than raising."""
    if not configured():
        return []
    import requests
    headers = {"X-Knack-Application-Id": os.environ["KNACK_APP_ID"].strip(),
               "X-Knack-REST-API-Key": os.environ["KNACK_API_KEY"].strip(),
               "Accept": "application/json"}
    out, page = [], 1
    while len(out) < limit and page <= 120:
        try:
            r = requests.get(
                f"https://api.knack.com/v1/objects/{OBJECT}/records",
                headers=headers,
                params={"page": page, "rows_per_page": 1000},
                timeout=45)
            if not r.ok:
                break
            data = r.json()
        except Exception:                               # noqa: BLE001
            break
        recs = data.get("records") or []
        if not recs:
            break
        out.extend(_row(rec) for rec in recs)
        total_pages = int(data.get("total_pages") or 1)
        if page >= total_pages:
            break
        page += 1
    return out[:limit]


def refresh() -> dict:
    """Re-pull and cache. Called by the scheduler."""
    if not configured():
        return {"skipped": "KNACK_APP_ID / KNACK_API_KEY not set."}
    rows = fetch()
    if not rows:
        # Never overwrite a good cache with nothing — a transient Knack
        # failure would otherwise empty every client record at once.
        old = _read_cache()
        return {"error": "Knack returned no records; kept the existing cache.",
                "cached": old.get("count", 0)}
    _write_cache(rows)
    try:
        from hub import audit
        audit.log("knack", "products_refreshed", count=len(rows))
    except Exception:                                   # noqa: BLE001
        pass
    return {"fetched": len(rows),
            "clients": len({r["client"] for r in rows if r["client"]})}


def _running(rec) -> bool:
    """Deferred import: knack_data reads this module, so importing it at the
    top would be a cycle."""
    from hub.knack_data import is_running
    return is_running(rec)


def rows(max_age_minutes: int | None = None) -> dict:
    """Cached products, refreshing if stale. Falls back to the export."""
    ttl = max_age_minutes if max_age_minutes is not None else int(
        os.environ.get("KNACK_PRODUCTS_TTL_MINUTES") or DEFAULT_TTL_MINUTES)
    cache = _read_cache()
    age = (time.time() - cache.get("fetched", 0)) / 60 if cache else 1e9

    if cache and age <= ttl:
        return {"rows": cache.get("rows", []), "source": "knack",
                "age_minutes": round(age), "count": cache.get("count", 0)}

    if configured():
        got = fetch()
        if got:
            _write_cache(got)
            return {"rows": got, "source": "knack", "age_minutes": 0,
                    "count": len(got)}

    if cache.get("rows"):
        # Stale beats empty: a blank client record reads as "no products",
        # which is a different and wrong statement.
        return {"rows": cache["rows"], "source": "knack (stale)",
                "age_minutes": round(age), "count": cache.get("count", 0),
                "note": "Knack couldn't be reached — showing the last "
                        "successful pull."}

    return _from_export()


def _from_export() -> dict:
    """The old static JSON, used only when there's no cache at all."""
    try:
        from hub import knack_data
        base = knack_data.BASE
        path = os.path.join(base, "products.json")
        mtime = os.path.getmtime(path)
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        raw = data if isinstance(data, list) else data.get("records", [])
        return {"rows": raw, "source": "static export",
                "age_minutes": round((time.time() - mtime) / 60),
                "count": len(raw),
                "note": "From the committed JSON export, which nothing "
                        "refreshes. Set KNACK_APP_ID and KNACK_API_KEY to "
                        "read live."}
    except Exception:                                   # noqa: BLE001
        return {"rows": [], "source": "none", "age_minutes": None, "count": 0,
                "note": "No live connection and no export on disk."}


def _norm(v: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(v or "").lower())


def for_client(client: str) -> dict:
    """Every product for one client, newest first.

    Matches on client and organisation, since Knack holds both and a product
    is filed under whichever the salesperson used.
    """
    data = rows()
    want = _norm(client)
    mine = [r for r in data["rows"]
            if _norm(r.get("client")) == want or _norm(r.get("organization")) == want]
    mine.sort(key=lambda r: str(r.get("start") or ""), reverse=True)
    live = [r for r in mine if _running(r)]
    return {
        "client": client, "products": mine, "count": len(mine),
        "live": len(live),
        "monthly": round(sum(r.get("monthly", 0) for r in live), 2),
        "campaigns": sorted({r["campaign"] for r in mine if r.get("campaign")}),
        "source": data["source"], "age_minutes": data["age_minutes"],
        "note": data.get("note", ""),
    }


def status() -> dict:
    """Freshness, for Diagnostics."""
    cache = _read_cache()
    age = (time.time() - cache.get("fetched", 0)) / 60 if cache else None
    return {
        "configured": configured(),
        "cached_records": cache.get("count", 0),
        "age_minutes": round(age) if age is not None else None,
        "last_refresh": (datetime.fromtimestamp(cache["fetched"], tz=timezone.utc)
                         .isoformat(timespec="seconds") if cache.get("fetched") else None),
        "ttl_minutes": int(os.environ.get("KNACK_PRODUCTS_TTL_MINUTES")
                           or DEFAULT_TTL_MINUTES),
        "note": ("Reading live from Knack." if cache.get("count") else
                 "No live pull yet — Client 360 is showing the static export, "
                 "which nothing refreshes."),
    }


def scan_domains(client: str = "") -> dict:
    """Root domains worth scanning, from the click-thru URLs on live products.

    Some clients have no website on the registry record, so the bulk scanner
    skipped them entirely — nothing to scan, no report, invisible. But their
    live products carry a click-thru URL, which is by definition a page on
    their site.

    **Only the host is kept.** A click-thru is usually a deep link — a landing
    page, a tracked path, a specific product page. Scanning
    example.com/lp/spring-promo audits one page; scanning example.com audits
    the site. The path is where the campaign points, not what we're auditing.

    Subdomains ARE kept. fresno.waterdamagesvcs.com is a distinct site with
    its own content and its own problems, not a path on the parent — treating
    it as one would collapse dozens of real sites into a handful.
    """
    from hub.client_context import canonical_domain

    data = rows()
    want = _norm(client) if client else ""
    found: dict[str, dict] = {}

    for r in data["rows"]:
        if want and _norm(r.get("client")) != want and _norm(r.get("organization")) != want:
            continue
        if not _running(r):
            continue            # a finished campaign's landing page may be gone
        name = r.get("client") or r.get("organization") or ""
        for field in ("display_url", "url"):
            raw = r.get(field) or ""
            if not raw:
                continue
            host = canonical_domain(raw)
            if not host:
                continue
            key = (_norm(name), host)
            if key in found:
                continue
            found[key] = {
                "client": name, "domain": host, "from_field": field,
                "original": raw,
                # Flag it so the reviewer can see we reduced a deep link.
                "was_deep_link": canonical_domain(raw) != raw.strip()
                                 .replace("https://", "").replace("http://", "")
                                 .rstrip("/"),
            }

    out = list(found.values())
    out.sort(key=lambda x: (x["client"].lower(), x["domain"]))
    return {"domains": out, "count": len(out),
            "clients": len({d["client"] for d in out}),
            "source": data["source"], "age_minutes": data["age_minutes"],
            "note": "Root domains only — a click-thru is usually a deep link, "
                    "and scanning one landing page isn't auditing the site."}
