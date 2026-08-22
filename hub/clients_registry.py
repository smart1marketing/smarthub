"""One list of every client the Hub knows about, from every source.

Three kinds of record end up in the same picker:

* **Knack clients** — anyone with a product record. The billing system is the
  source of truth for who is a paying client.
* **Website records** — a site on file whose owner never matched a Knack
  client name exactly. Still a real client; still needs images named.
* **House URLs** — sites we run in house. No products today, possibly products
  later, and in the meantime they need exactly the same tooling. These live in
  the Hub's own store and are marked so nobody mistakes one for billable work.

Everything that asks "which client is this?" reads from here, so a house site
behaves like any other client without pretending to be a paying one.
"""
import datetime as _dt
import os
import re
import threading

from . import jsonstore
from . import knack_data

_lock = threading.Lock()


def _store_base() -> str:
    return jsonstore.data_root()


def _house_path() -> str:
    return os.path.join(_store_base(), "house_clients.json")


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")
    return s[:80] or "client"


def norm_domain(value: str) -> str:
    d = re.sub(r"^https?://", "", str(value or "").strip().lower())
    return d.removeprefix("www.").split("/")[0].split(":")[0]


# ------------------------------------------------------------- house clients
# House clients exist only here. A Knack client can be re-read from Knack; a
# house site has no upstream to be re-read from, so this file is the whole
# record and it goes through hub.jsonstore to reach the database backup.
def house_clients() -> list[dict]:
    rows = jsonstore.read_json(_house_path(), default=[])
    return rows if isinstance(rows, list) else []


def _write_house(rows: list[dict]):
    with _lock:
        jsonstore.write_json(_house_path(), rows, indent=1)


def add_house_client(name: str, url: str = "", notes: str = "",
                     actor: str = "") -> dict:
    """Register a site we run in house as a client."""
    name = str(name or "").strip()[:200]
    if not name:
        raise ValueError("A name is required.")
    url = str(url or "").strip()[:400]
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    rows = house_clients()
    key = slugify(name)
    if any(r.get("slug") == key for r in rows):
        raise ValueError(f"“{name}” is already on the house list.")

    # Don't let a house entry shadow a real client. Marking a paying account
    # "house" would hide its products and mislabel billable work.
    existing = next((c for c in all_clients()
                     if c["name"].lower() == name.lower() and not c["is_house"]), None)
    if existing:
        detail = (f" with {existing['product_count']} product"
                  f"{'s' if existing['product_count'] != 1 else ''}"
                  if existing.get("product_count") else "")
        raise ValueError(
            f"“{existing['name']}” is already a client{detail}, so it's "
            "selectable everywhere already — no need to add it as a house site.")
    row = {
        "slug": key,
        "name": name,
        "url": url,
        "domain": norm_domain(url),
        "notes": str(notes or "").strip()[:500],
        "added_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "added_by": str(actor or "").strip()[:120],
    }
    rows.insert(0, row)
    _write_house(rows)
    return row


def update_house_client(slug: str, updates: dict) -> dict | None:
    rows = house_clients()
    hit = next((r for r in rows if r.get("slug") == slug), None)
    if hit is None:
        return None
    if "name" in updates and str(updates["name"]).strip():
        hit["name"] = str(updates["name"]).strip()[:200]
    if "url" in updates:
        u = str(updates["url"] or "").strip()[:400]
        if u and not u.startswith(("http://", "https://")):
            u = "https://" + u
        hit["url"] = u
        hit["domain"] = norm_domain(u)
    if "notes" in updates:
        hit["notes"] = str(updates["notes"] or "").strip()[:500]
    _write_house(rows)
    return hit


def delete_house_client(slug: str) -> bool:
    rows = house_clients()
    if not any(r.get("slug") == slug for r in rows):
        return False
    _write_house([r for r in rows if r.get("slug") != slug])
    return True


# --------------------------------------------------------- the combined list
_cache: dict = {"at": 0.0, "rows": []}
_CACHE_SECONDS = 120


def all_clients(refresh: bool = False) -> list[dict]:
    """Every client, deduplicated by name, newest knowledge winning.

    Knack is read on every call in principle, but 10k product rows is enough
    work that a short cache keeps the type-ahead instant.
    """
    import time
    if not refresh and _cache["rows"] and time.time() - _cache["at"] < _CACHE_SECONDS:
        return _cache["rows"]

    by_key: dict[str, dict] = {}

    # 1. websites give us domains
    web_by_client: dict[str, dict] = {}
    for w in knack_data.websites():
        nm = str(w.get("name") or "").strip()
        if not nm:
            continue
        key = nm.lower()
        cur = web_by_client.get(key)
        if cur is None or (not cur.get("liveUrl") and w.get("liveUrl")):
            web_by_client[key] = w

    # 2. Knack clients (anyone with a product) — the billing source of truth
    seo_clients: set[str] = set()
    for r in knack_data.products():
        nm = str(r.get("client") or "").strip()
        if not nm:
            continue
        key = nm.lower()
        entry = by_key.setdefault(key, {
            "name": nm, "slug": slugify(nm), "source": "knack",
            "url": "", "domain": "", "products": set(), "is_seo": False,
            "is_house": False, "live": False,
        })
        pname = str(r.get("product") or "")
        if pname:
            entry["products"].add(pname)
        if knack_data.is_running(r):
            entry["live"] = True
            if "seo" in pname.lower():
                entry["is_seo"] = True
                seo_clients.add(key)

    # 3. website records with no matching Knack client are still clients
    for key, w in web_by_client.items():
        nm = str(w.get("name") or "").strip()
        entry = by_key.setdefault(key, {
            "name": nm, "slug": slugify(nm), "source": "website",
            "url": "", "domain": "", "products": set(), "is_seo": False,
            "is_house": False, "live": str(w.get("status") or "").lower() == "live",
        })
        if not entry["domain"]:
            entry["domain"] = norm_domain(w.get("domain") or w.get("liveUrl") or "")
        if not entry["url"]:
            u = str(w.get("liveUrl") or "").strip() or str(w.get("domain") or "").strip()
            if u:
                entry["url"] = u if u.startswith("http") else "https://" + u

    # domains for Knack clients whose name matched a website record
    for key, entry in by_key.items():
        if entry["domain"]:
            continue
        w = web_by_client.get(key)
        if w is None:                     # loose match, same as Client 360 does
            flat = re.sub(r"[^a-z0-9]", "", key)[:12]
            if flat:
                w = next((x for k, x in web_by_client.items()
                          if flat in re.sub(r"[^a-z0-9]", "", k)), None)
        if w:
            entry["domain"] = norm_domain(w.get("domain") or w.get("liveUrl") or "")
            u = str(w.get("liveUrl") or "").strip() or str(w.get("domain") or "").strip()
            if u and not entry["url"]:
                entry["url"] = u if u.startswith("http") else "https://" + u

    # 4. house URLs — ours, no products, flagged as such
    for h in house_clients():
        key = str(h.get("name", "")).lower()
        entry = by_key.setdefault(key, {
            "name": h.get("name", ""), "slug": h.get("slug") or slugify(h.get("name", "")),
            "source": "house", "url": "", "domain": "", "products": set(),
            "is_seo": False, "is_house": True, "live": True,
        })
        entry["is_house"] = True
        entry["source"] = "house"
        entry["url"] = h.get("url") or entry["url"]
        entry["domain"] = h.get("domain") or entry["domain"]
        entry["notes"] = h.get("notes", "")

    rows = []
    for entry in by_key.values():
        products = sorted(entry.pop("products", set()))
        rows.append({**entry, "products": products, "product_count": len(products)})
    rows.sort(key=lambda r: r["name"].lower())

    _cache["rows"] = rows
    _cache["at"] = time.time()
    return rows


def search_clients(q: str, limit: int = 12) -> list[dict]:
    """Type-ahead: name first, then domain. Exact and prefix matches rank
    above 'contains' so typing a full name doesn't bury it."""
    q = str(q or "").strip().lower()
    rows = all_clients()
    if not q:
        return [r for r in rows if r["is_house"]][:limit]

    exact, prefix, contains = [], [], []
    for r in rows:
        name, dom = r["name"].lower(), (r.get("domain") or "").lower()
        if name == q or dom == q:
            exact.append(r)
        elif name.startswith(q) or dom.startswith(q):
            prefix.append(r)
        elif q in name or (dom and q in dom):
            contains.append(r)
    return (exact + prefix + contains)[:limit]


def find_client(name: str) -> dict | None:
    key = str(name or "").strip().lower()
    if not key:
        return None
    return next((r for r in all_clients() if r["name"].lower() == key), None)


def is_seo_client(name: str) -> bool:
    hit = find_client(name)
    return bool(hit and hit.get("is_seo"))
