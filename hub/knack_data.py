"""Read-only access to the Knack data JSONs that ship with the Clients app.

The files live in clients_app/data/ (committed to the repo, refreshed by the
existing `npm run refresh` flow / GitHub Action).  Loaded lazily and cached
until the file's mtime changes, so a data refresh + redeploy (or a mounted
newer file) is picked up automatically.
"""
import json
import os
import datetime as _dt
import threading

BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "clients_app", "data")

_cache: dict[str, tuple[float, object]] = {}
_lock = threading.Lock()


def _load(name: str):
    path = os.path.join(BASE, name)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    with _lock:
        hit = _cache.get(name)
        if hit and hit[0] == mtime:
            return hit[1]
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    with _lock:
        _cache[name] = (mtime, data)
    return data


def _records(data) -> list[dict]:
    if data is None:
        return []
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for key in ("records", "rows", "data", "items"):
            if isinstance(data.get(key), list):
                return [r for r in data[key] if isinstance(r, dict)]
        # dict of lists? take the longest list value
        lists = [v for v in data.values() if isinstance(v, list)]
        if lists:
            longest = max(lists, key=len)
            return [r for r in longest if isinstance(r, dict)]
    return []


def products() -> list[dict]:
    return _records(_load("products.json"))


def websites() -> list[dict]:
    return _records(_load("websites.json"))


def data_age_hours() -> float | None:
    import time
    try:
        mtime = os.path.getmtime(os.path.join(BASE, "products.json"))
    except OSError:
        return None
    return (time.time() - mtime) / 3600.0


def _num(v) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.replace("$", "").replace(",", "").strip()
        try:
            return float(s)
        except ValueError:
            return 0.0
    return 0.0


# Statuses that mean the row is over, whatever its dates say. Everything else
# is judged on its term. These two are unambiguous: 8,400 Complete rows and 70
# Revised rows, not one of which covers today.
_FINISHED_STATUSES = {"complete", "revised"}


def is_running(rec: dict) -> bool:
    """Is this insertion order delivering right now?

    The test used to be `status == "live"`, and it missed about a third of the
    work actually running. Knack's status vocabulary is wider than that:
    Assigned, Scheduled, Pending Assets, In Process, Needs Cancelled, Paused
    and Cancelled rows sit inside their dates and bill this month. A client
    whose only current products were two of those reported "0 products, $0"
    beside an end date — a row that says the client is both ending and has
    nothing, which is the fastest way to make a renewal queue stop being
    trusted.

    So either signal counts: the term covers today, or somebody has marked it
    Live. Deliberately a union rather than a swap. Judging on dates alone
    dropped 173 rows that Knack still calls Live but whose end date has passed
    — month-to-month arrangements, and IOs nobody has closed out — and that
    took Hern Marine from four products and $6,500 to "0 products, $0 beside
    an end date", which is the very row this was meant to remove. Widening a
    definition can only add work to a queue; narrowing it hides work, and the
    hidden kind is the expensive kind.

    Complete and Revised never count — finished and superseded — so a stray
    date on one cannot resurrect it.
    """
    status = str(rec.get("status", "")).strip().lower()
    if status in _FINISHED_STATUSES:
        return False
    if status == "live":
        return True

    from hub import dates as _dates
    start = _dates.to_date(rec.get("start"))
    end = _dates.to_date(rec.get("end"))
    if not (start and end):
        return False
    return start <= _dt.date.today() <= end


# The old name, kept because renaming a predicate across six modules in the
# same change as redefining it makes the redefinition impossible to review.
_is_live = is_running


def _period_label(yyyymm) -> str:
    s = str(yyyymm or "")
    months = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    if len(s) == 6 and s.isdigit():
        return f"{months[int(s[4:6])]} {s[:4]}"
    return s


# The one file in this module that is not re-readable from anywhere. Knack
# reports what is true today; it has no record of what was true last March, so
# a past period here exists in this file and nowhere else and cannot be
# recomputed at any price. It reads like a cache — it sits next to the Knack
# JSONs and is rewritten on every dashboard load — which is exactly why it is
# worth saying that it is not one. Through hub.jsonstore into the backup.
def _history_path() -> str:
    from . import jsonstore
    return os.path.join(jsonstore.data_root(), "hub-metrics-history.json")


# Metrics worth trending. Snapshotted per Knack period so the dashboard can say
# which way each one moved, rather than only what it is today.
TRENDED = ("clients_live", "live_products", "live_budget_monthly",
           "websites_active", "hm_monthly", "estimated_total_monthly")


def _period_minus(period: str, months: int) -> str:
    """The YYYYMM that many months before `period`."""
    try:
        y, m = int(str(period)[:4]), int(str(period)[4:6])
    except (TypeError, ValueError):
        return ""
    total = y * 12 + (m - 1) - months
    return f"{total // 12:04d}{total % 12 + 1:02d}"


def _snapshot(period: str, values: dict) -> dict:
    """Record this period's metrics and return the whole history.

    Written every time the dashboard is read, so the current period is always
    up to date and past periods keep the last value they were given.
    """
    path = _history_path()
    from . import jsonstore
    hist = jsonstore.read_json(path, default={})
    if not isinstance(hist, dict):
        hist = {}
    if period:
        entry = hist.get(period) or {}
        entry.update({k: v for k, v in values.items() if v is not None})
        hist[period] = entry
        try:
            jsonstore.write_json(path, hist)
        except OSError:
            pass
    return hist


def _delta(now, before):
    """A movement, or an explicit "we don't have that yet".

    `available: False` is the point of this shape. A month we have no snapshot
    for is not a month where nothing changed, and rendering it as 0 would state
    something we do not know — the same mistake as showing an empty rate card
    as "no products".
    """
    if not isinstance(before, (int, float)) or not isinstance(now, (int, float)):
        return {"available": False}
    diff = now - before
    pct = round(diff / before * 100, 1) if before else None
    return {"available": True, "from": before, "diff": diff, "pct": pct,
            "dir": "up" if diff > 0 else "down" if diff < 0 else "flat"}


def _trends(period: str, current: dict) -> dict:
    """Each trended metric against last month and the same month last year."""
    hist = _snapshot(period, {k: current.get(k) for k in TRENDED})
    prev_m = hist.get(_period_minus(period, 1)) or {}
    prev_y = hist.get(_period_minus(period, 12)) or {}
    out = {}
    for k in TRENDED:
        out[k] = {"last_month": _delta(current.get(k), prev_m.get(k)),
                  "last_year": _delta(current.get(k), prev_y.get(k))}
    return out


def _website_movement(period, websites_active) -> int | None:
    """Websites carry no month-over-month fields, so the Hub snapshots the
    active count per Knack period and compares to the previous period."""
    path = _history_path()
    from . import jsonstore
    hist = jsonstore.read_json(path, default={})
    if not isinstance(hist, dict):
        hist = {}
    key = str(period or "")
    if key:
        entry = hist.get(key) or {}
        entry["websites_active"] = websites_active
        hist[key] = entry
        try:
            jsonstore.write_json(path, hist)
        except OSError:
            pass
    prev_keys = sorted(k for k in hist if k.isdigit() and k < key)
    if not prev_keys:
        return None
    prev = hist[prev_keys[-1]].get("websites_active")
    return websites_active - prev if isinstance(prev, (int, float)) else None


def month_over_month(prods: list[dict]) -> dict:
    """Per-client budget totals for this month vs last month, from the
    lastM/thisM active flags Knack exports on every IO row."""
    this_by, last_by = {}, {}
    for r in prods:
        client = str(r.get("client", "")).strip()
        if not client:
            continue
        m = _num(r.get("monthly"))
        if r.get("thisM"):
            this_by[client] = this_by.get(client, 0.0) + m
        if r.get("lastM"):
            last_by[client] = last_by.get(client, 0.0) + m
    new = sum(1 for c in this_by if c not in last_by)
    lost = sum(1 for c in last_by if c not in this_by)
    increased = sum(1 for c, v in this_by.items() if c in last_by and v > last_by[c] + 0.5)
    decreased = sum(1 for c, v in this_by.items() if c in last_by and v < last_by[c] - 0.5)
    return {"new": new, "lost": lost, "increased": increased, "decreased": decreased}


def summary() -> dict:
    raw = _load("products.json")
    prods = products()
    webs = websites()

    live = [r for r in prods if _is_live(r)]
    live_clients = {str(r.get("client", "")).strip() for r in live if r.get("client")}
    all_clients = {str(r.get("client", "")).strip() for r in prods if r.get("client")}
    live_budget = sum(_num(r.get("monthly")) for r in live)

    def _active(w):
        a = w.get("active")
        if isinstance(a, bool):
            return a
        return str(w.get("status", "")).strip().lower() == "active"

    active_sites = [w for w in webs if _active(w)]
    hm_monthly = sum(_num(w.get("hmMonthly")) for w in active_sites)

    this_period = raw.get("thisMonth") if isinstance(raw, dict) else None
    last_period = raw.get("lastMonth") if isinstance(raw, dict) else None
    mom = month_over_month(prods)
    try:
        movement = _website_movement(this_period, len(active_sites))
    except Exception:  # noqa: BLE001 — never break the dashboard on history I/O
        movement = None

    return {
        "clients_total": len(all_clients),
        "clients_live": len(live_clients),
        "live_products": len(live),
        "live_budget_monthly": round(live_budget),
        "websites_total": len(webs),
        "websites_active": len(active_sites),
        "hm_monthly": round(hm_monthly),
        "estimated_total_monthly": round(live_budget + hm_monthly),
        "new_customers": mom["new"],
        "lost_customers": mom["lost"],
        "increased_customers": mom["increased"],
        "decreased_customers": mom["decreased"],
        "website_movement": movement,
        # Which way each headline number moved, against last month and against
        # the same month a year ago. Absent history is reported as absent
        # rather than as no change.
        "trends": _trends(str(this_period or ""), {
            "clients_live": len(live_clients),
            "live_products": len(live),
            "live_budget_monthly": round(live_budget),
            "websites_active": len(active_sites),
            "hm_monthly": round(hm_monthly),
            "estimated_total_monthly": round(live_budget + hm_monthly),
        }),
        "this_period": _period_label(this_period),
        "last_period": _period_label(last_period),
        "data_age_hours": data_age_hours(),
    }


CREATIVE_EXCLUDE = ("sem", "website seo", "listings", "email blast")



# Creative links are recognised by the shape of the URL, not by a `kind` field.
#
# That field means two different things depending on where the row came from:
# in the committed export `kind` is the link type (gdrive, pdf, dropbox), but
# in the live rows from hub.knack_products it is the *product* type ("OTT",
# "Paid Search"). Filtering on it therefore worked against the export and, once
# products were read live, let every row through — and the `url` on a live row
# is the click-thru to the client's own website, not a piece of creative. The
# report would have counted landing pages as artwork.
#
# A Drive/Dropbox/PDF address is unmistakable; a client's homepage is not. So
# the URL decides.
_CREATIVE_HOSTS = ("drive.google.com", "docs.google.com", "dropbox.com",
                   "box.com", "wetransfer.com", "cloudinary.com",
                   "sharepoint.com", "onedrive.live.com", "vimeo.com",
                   "youtube.com", "youtu.be")
_CREATIVE_EXT = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4",
                 ".mov", ".psd", ".ai", ".zip", ".eps", ".tif", ".tiff")


def creative_kind(url: str, declared: str = "") -> str:
    """The link type, or "" when this is not a creative link.

    `declared` is honoured only when it is one of the export's own link types;
    a product name arriving in that slot is ignored rather than trusted.
    """
    u = str(url or "").strip().lower()
    if not u.startswith(("http://", "https://")):
        return ""
    d = str(declared or "").strip().lower()
    if d in ("gdrive", "gdoc", "pdf", "dropbox", "file", "image", "video"):
        return d
    for host in _CREATIVE_HOSTS:
        if host in u:
            return "gdrive" if "google.com" in host else host.split(".")[0]
    path = u.split("?")[0]
    for ext in _CREATIVE_EXT:
        if path.endswith(ext):
            return "pdf" if ext == ".pdf" else "file"
    return ""


def _creative_items(prod_records: list[dict]) -> list[dict]:
    """Creative file links (PDF / Drive / Dropbox / file) grouped for display,
    newest year first — mirrors the Clients module's creative section."""
    seen = set()
    items = []
    for r in prod_records:
        # All four External Creative Link fields, not just the first.
        candidates = [u for u in (r.get("creative_urls") or []) if u]             or [r.get("url") or r.get("creative_url")]
        url = next((u for u in candidates if creative_kind(u, r.get("kind"))), None)
        if not url:
            continue
        kind = creative_kind(url, r.get("kind"))
        pname = str(r.get("product", "")).lower()
        if any(x in pname for x in CREATIVE_EXCLUDE):
            continue
        key = (r.get("io"), url)
        if key in seen:
            continue
        seen.add(key)
        ts = str(r.get("ts") or "")
        year = ts[:4] if len(ts) >= 4 and ts[:4].isdigit() else str(r.get("start", ""))[-4:]
        items.append({
            "year": year,
            "product": r.get("product"),
            "campaign": r.get("campaign"),
            "io": r.get("io"),
            "url": url,
            "kind": kind,
            "start": r.get("start"),
        })
    items.sort(key=lambda i: (i["year"], str(i.get("start") or "")), reverse=True)
    return items


def _parse_gtm(value) -> dict | None:
    """websites.json holds strings like 'AdOps: GTM-TG6FPR8M'."""
    s = str(value or "").strip()
    if not s:
        return None
    label, _, rest = s.partition(":")
    if rest.strip():
        return {"login": label.strip(), "id": rest.strip()}
    return {"login": "", "id": s}


def _product_source() -> tuple[list[dict], str, int | None]:
    """The product records to build Client 360 from, live if we can get them.

    Client 360 read these from the static export in clients_app/data, which is
    only ever as current as the last manual refresh — so a client's insertion
    orders showed last month's line-up while the Knack pull reported success,
    because the two are different sources and only one of them was live.

    hub.knack_products reads object_135 from the API and emits rows with the
    same field names, so it can be swapped in here. The export stays as the
    fallback: stale beats empty, because a client record showing no products
    reads as "this client has none" rather than "we couldn't reach Knack".
    """
    try:
        from hub import knack_products
        data = knack_products.rows()
        if data.get("source") == "knack" and data.get("rows"):
            return data["rows"], "knack", data.get("age_minutes")
    except Exception:                                   # noqa: BLE001
        pass
    return products(), "export", None


def search_client(q: str, limit: int = 8) -> list[dict]:
    """Group products + website records by client for Client 360."""
    ql = (q or "").strip().lower()
    if not ql:
        return []
    groups: dict[str, dict] = {}

    raw_by_group: dict[str, list[dict]] = {}

    product_rows, product_source, product_age = _product_source()
    for r in product_rows:
        # Knack holds both a client and an organisation and a product is filed
        # under whichever the salesperson used, so match either — the live rows
        # carry both where the export only ever had one.
        client = str(r.get("client", "")).strip()
        org = str(r.get("organization", "")).strip()
        if ql in client.lower():
            pass
        elif org and ql in org.lower():
            client = client or org
        else:
            continue
        if not client:
            continue
        g = groups.setdefault(client.lower(), {"client": client, "products": [], "websites": []})
        raw_by_group.setdefault(client.lower(), []).append(r)
        g["products"].append({
            "product": r.get("product"),
            "campaign": r.get("campaign"),
            "io": r.get("io"),
            "status": r.get("status"),
            "monthly": r.get("monthly"),
            "sales": r.get("sales"),
            "partner": r.get("partner"),
            "start": r.get("start"),
            "end": r.get("end"),
            "dash": r.get("dash"),
        })

    for w in websites():
        hay = " ".join(str(w.get(k, "")) for k in ("name", "domain", "liveUrl")).lower()
        if ql not in hay:
            continue
        key = str(w.get("name", "")).strip().lower() or str(w.get("domain", "")).lower()
        # attach to an existing client group when names align, else own group
        target = None
        for gk, g in groups.items():
            if gk and (gk in key or key in gk):
                target = g
                break
        if target is None:
            target = groups.setdefault(key, {"client": w.get("name") or w.get("domain"), "products": [], "websites": []})
        target["websites"].append({
            "name": w.get("name"),
            "domain": w.get("domain"),
            "liveUrl": w.get("liveUrl"),
            "platform": w.get("platform"),
            "status": w.get("status"),
            "hmMonthly": w.get("hmMonthly"),
            "partner": w.get("partner"),
            "manager": w.get("manager"),
            "ga": w.get("ga"),
            "gtm": w.get("gtm"),
            "registrar": w.get("registrar"),
            "domainPurchased": w.get("domainPurchased"),
        })

    # Hub-attached website records (attach-only, never written back to Knack)
    try:
        from . import seo as _seo
        for g in groups.values():
            att = _seo.get_links(str(g["client"])).get("website", [])
            if not att:
                continue
            have = {str(w.get("domain") or "").lower() for w in g["websites"]}
            for w in _seo._client_websites(str(g["client"])):
                d = str(w.get("domain") or "").lower()
                if d and d in have:
                    continue
                have.add(d)
                g["websites"].append({
                    "name": w.get("name"), "domain": w.get("domain"),
                    "liveUrl": w.get("liveUrl"), "platform": w.get("platform"),
                    "status": w.get("status"), "hmMonthly": w.get("hmMonthly"),
                    "partner": w.get("partner"), "manager": w.get("manager"),
                    "ga": w.get("ga"), "gtm": w.get("gtm"),
                    "registrar": w.get("registrar"),
                    "domainPurchased": w.get("domainPurchased"),
                    "attached": True,
                })
    except Exception:  # noqa: BLE001 — attachments must never break search
        pass

    # hub-side website corrections (platform etc.) — display only
    try:
        from . import seo as _seo2
        for g in groups.values():
            _seo2.apply_website_overrides(str(g["client"]), g["websites"])
    except Exception:  # noqa: BLE001
        pass

    out = list(groups.values())
    # clients with most live products first
    out.sort(key=lambda g: (-len(g["products"]), str(g["client"]).lower()))
    for g in out:
        g["products"].sort(key=lambda p: (0 if is_running(p) else 1, str(p.get("product") or "")))

        # ---- header extras: billing, dashboard link, Smart 1 Site flag ----
        live = [p for p in g["products"] if is_running(p)]
        g["billing_monthly"] = round(sum(_num(p.get("monthly")) for p in live))
        g["dash_url"] = next(
            (p.get("dash") for p in live if isinstance(p.get("dash"), str) and p["dash"].startswith("http")),
            next((p.get("dash") for p in g["products"]
                  if isinstance(p.get("dash"), str) and p["dash"].startswith("http")), None),
        )
        g["smart1_site"] = any(
            "smart1" in str(w.get("platform", "")).replace(" ", "").lower() for w in g["websites"]
        )

        # ---- creative files + GTM containers ----
        gkey = str(g["client"]).strip().lower()
        g["creative"] = _creative_items(raw_by_group.get(gkey, []))[:24]
        gtms, seen_gtm = [], set()
        for w in g["websites"]:
            parsed = _parse_gtm(w.get("gtm"))
            if parsed and parsed["id"] not in seen_gtm:
                seen_gtm.add(parsed["id"])
                parsed["site"] = w.get("name") or w.get("domain")
                gtms.append(parsed)
        g["gtm_containers"] = gtms
        # Say where the products came from and how old they are. A stale export
        # that looks identical to live data is how last month's insertion
        # orders got read as this month's.
        g["products_source"] = product_source
        g["products_age_minutes"] = product_age
        g["products_note"] = (
            f"Live from Knack, {product_age} min old." if product_source == "knack"
            else "From the committed export in clients_app/data — nothing "
                 "refreshes it, so this may be out of date.")
    return out[:limit]
