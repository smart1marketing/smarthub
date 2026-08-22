"""QuickBooks Online connector for the Hub.

One-time OAuth connect (like the Google module), tokens persisted on the
Render disk, then Client 360 can look up a client's QuickBooks customer and
their recent invoices — each deep-linked into QuickBooks Online.

Env:
  QB_CLIENT_ID / QB_CLIENT_SECRET  — from your app at developer.intuit.com
  QB_ENVIRONMENT                   — "production" (default) or "sandbox"
  QB_REDIRECT_URI                  — optional override; defaults to
                                     https://<hub-host>/qb/callback
Intuit rotates refresh tokens on every refresh, so the stored token file is
rewritten each time; refresh tokens live ~100 days of inactivity, and any
Hub use within that window keeps the connection alive indefinitely.
"""
import base64
import json
import os
import threading
import time
from datetime import datetime, timezone

import requests
from itsdangerous import BadSignature, URLSafeTimedSerializer

AUTH_BASE = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
SCOPE = "com.intuit.quickbooks.accounting"

_lock = threading.Lock()


def _env(name, default=""):
    return os.environ.get(name, default)


def _api_base():
    if _env("QB_ENVIRONMENT", "production").lower().startswith("sand"):
        return "https://sandbox-quickbooks.api.intuit.com"
    return "https://quickbooks.api.intuit.com"


def configured() -> bool:
    return bool(_env("QB_CLIENT_ID") and _env("QB_CLIENT_SECRET"))


def _token_path() -> str:
    from . import jsonstore
    return os.path.join(jsonstore.data_root(), "quickbooks_tokens.json")


# Intuit rotates the refresh token on every refresh, so this file is the only
# copy of the live one — there is no way to re-derive it and no second place it
# exists. Through hub.jsonstore it is mirrored into the database, which is
# backed up; the disk it used to live on alone is not. Losing it means someone
# re-running the OAuth connect by hand, and until they do, Client 360 shows no
# invoices for anybody with no indication that a token is the reason.
def _load_tokens():
    from . import jsonstore
    return jsonstore.read_json(_token_path(), default=None)


def _save_tokens(tok):
    from . import jsonstore
    with _lock:
        jsonstore.write_json(_token_path(), tok)


def disconnect():
    # delete_json, not os.remove — the mirrored copy has to go too, or the
    # next _load_tokens() restores the file and the disconnect undoes itself.
    from . import jsonstore
    jsonstore.delete_json(_token_path())


def health() -> dict:
    """Why the QuickBooks connection may not be holding.

    Two things drop it silently and neither is visible from the UI:

      * **Intuit expires a refresh token after 100 days**, and after 24 hours
        if it is never used. The token rotates on every refresh, so an active
        connection stays alive — one that sits idle does not.
      * **The token lives in a JSON file on the Render disk.** If that disk
        isn't mounted, the file is written to the container filesystem and is
        gone on the next deploy, so the connection appears to drop every time
        you ship.

    Since the token is mirrored through ``hub.jsonstore`` the second one has a
    second line of defence: the copy in the database survives both a missing
    disk and a recreated one, and the file is rewritten from it on the next
    read. The warning below is therefore only raised when *neither* layer is
    holding — an unmounted disk on its own no longer drops the connection, and
    saying it does would send someone chasing a disk that is not the problem.
    """
    from . import jsonstore
    tok = _load_tokens() or {}
    path = _token_path()
    mirrored = jsonstore.available()
    persistent = os.path.isdir("/var/data") or mirrored
    now = time.time()
    obtained = tok.get("obtained_at") or tok.get("issued_at")
    age_days = round((now - obtained) / 86400, 1) if obtained else None
    # Intuit's refresh token lifetime.
    days_left = (100 - age_days) if age_days is not None else None

    problems = []
    if not persistent:
        problems.append(
            "Tokens are being written to the container filesystem, with no "
            "mounted disk and no database mirror to fall back on — the "
            "connection will drop on every deploy. Confirm the Render disk is "
            "mounted at /var/data, or that DATABASE_URL is set.")
    if tok and days_left is not None and days_left < 14:
        problems.append(
            f"The refresh token is {age_days} days old and Intuit expires them "
            f"at 100 days — about {round(days_left)} days left. Reconnect "
            f"before it lapses.")
    if not tok:
        problems.append("Not connected. Connect from System Status.")

    return {
        "connected": bool(tok.get("refresh_token") and tok.get("realm_id")),
        "token_path": path,
        "persistent_storage": persistent,
        "disk_mounted": os.path.isdir("/var/data"),
        "backed_up": mirrored,
        "token_file_exists": os.path.isfile(path),
        "realm_id": bool(tok.get("realm_id")),
        "access_expires_in": (round(tok.get("expires_at", 0) - now)
                              if tok.get("expires_at") else None),
        "refresh_age_days": age_days,
        "refresh_days_left": round(days_left) if days_left is not None else None,
        "environment": _env("QB_ENVIRONMENT") or "sandbox",
        "problems": problems,
        "ok": not problems,
    }


def connected() -> bool:
    tok = _load_tokens()
    return bool(tok and tok.get("refresh_token") and tok.get("realm_id"))


def _state_serializer():
    secret = _env("SECRET_KEY") or _env("SESSION_SECRET") or "s1hub"
    return URLSafeTimedSerializer(secret, salt="s1hub-qb-oauth")


def redirect_uri(request) -> str:
    override = _env("QB_REDIRECT_URI")
    if override:
        return override
    root = request.url_root
    if root.startswith("http://") and "localhost" not in root and "127.0.0.1" not in root:
        root = "https://" + root[len("http://"):]
    return root.rstrip("/") + "/qb/callback"


def authorize_url(request) -> str:
    from urllib.parse import urlencode
    state = _state_serializer().dumps({"n": 1})
    return AUTH_BASE + "?" + urlencode({
        "client_id": _env("QB_CLIENT_ID"),
        "response_type": "code",
        "scope": SCOPE,
        "redirect_uri": redirect_uri(request),
        "state": state,
    })


def _basic_auth():
    raw = f"{_env('QB_CLIENT_ID')}:{_env('QB_CLIENT_SECRET')}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def handle_callback(request) -> tuple[bool, str]:
    """Exchange the auth code; returns (ok, message)."""
    try:
        _state_serializer().loads(request.args.get("state", ""), max_age=600)
    except BadSignature:
        return False, "OAuth state check failed — try connecting again."
    code = request.args.get("code")
    realm_id = request.args.get("realmId")
    if not code or not realm_id:
        return False, "QuickBooks did not return an authorization code."
    r = requests.post(TOKEN_URL, data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri(request),
    }, headers={"Authorization": _basic_auth(), "Accept": "application/json"}, timeout=20)
    if not r.ok:
        return False, f"Token exchange failed (HTTP {r.status_code}): {r.text[:200]}"
    tok = r.json()
    _save_tokens({
        "access_token": tok.get("access_token"),
        "refresh_token": tok.get("refresh_token"),
        "expires_at": time.time() + int(tok.get("expires_in", 3600)),
        "realm_id": realm_id,
    })
    return True, "QuickBooks connected."


def _ensure_access_token():
    tok = _load_tokens()
    if not tok:
        raise RuntimeError("QuickBooks is not connected.")
    if time.time() < tok.get("expires_at", 0) - 120:
        return tok
    r = requests.post(TOKEN_URL, data={
        "grant_type": "refresh_token",
        "refresh_token": tok.get("refresh_token", ""),
    }, headers={"Authorization": _basic_auth(), "Accept": "application/json"}, timeout=20)
    if not r.ok:
        raise RuntimeError(f"QuickBooks token refresh failed (HTTP {r.status_code}) — reconnect from System Status.")
    fresh = r.json()
    tok.update({
        "access_token": fresh.get("access_token"),
        # Intuit rotates refresh tokens — always persist the new one.
        "refresh_token": fresh.get("refresh_token") or tok.get("refresh_token"),
        "expires_at": time.time() + int(fresh.get("expires_in", 3600)),
        # Intuit rotates the refresh token on every refresh, so its clock
        # restarts here. Recording that is what makes the 100-day expiry
        # predictable instead of a surprise.
        "obtained_at": time.time(),
    })
    _save_tokens(tok)
    return tok


def _query(sql: str):
    tok = _ensure_access_token()
    r = requests.get(
        f"{_api_base()}/v3/company/{tok['realm_id']}/query",
        params={"query": sql, "minorversion": "70"},
        headers={"Authorization": f"Bearer {tok['access_token']}", "Accept": "application/json"},
        timeout=20,
    )
    if not r.ok:
        if r.status_code == 403 and ("3100" in r.text or "ApplicationAuthorizationFailed" in r.text):
            raise RuntimeError(
                "QuickBooks rejected the app for this company (error 3100). This almost always "
                "means the keys and environment don't match: you connected with the app's "
                "DEVELOPMENT keys while QB_ENVIRONMENT=production (or vice versa). In "
                "developer.intuit.com open your app -> Keys & credentials, copy the PRODUCTION "
                "Client ID/Secret into QB_CLIENT_ID / QB_CLIENT_SECRET, redeploy, then "
                "Disconnect and Connect QuickBooks again from System Status."
            )
        raise RuntimeError(f"QuickBooks query failed (HTTP {r.status_code}): {r.text[:200]}")
    return (r.json() or {}).get("QueryResponse", {})


def _esc(s: str) -> str:
    return str(s or "").replace("'", "\\'")


def customer_link(cid) -> str:
    return f"https://app.qbo.intuit.com/app/customerdetail?nameId={cid}"


# ---------------------------------------------------------------------------
# Public invoice links
# ---------------------------------------------------------------------------
#
# The old invoice_link() pointed at app.qbo.intuit.com, which requires a
# QuickBooks login — so sending it to a client meant they couldn't open it, and
# opening it internally meant signing in first. QuickBooks does expose a public
# link: the read-only `InvoiceLink` field, available from minorversion 36 and
# only when explicitly requested with `include=invoiceLink`. That URL opens the
# invoice with no login and offers Pay Now.
#
# Two constraints worth knowing:
#   * It is production-only. A sandbox company returns nothing, which reads as
#     "broken" unless you know to expect it.
#   * The links are not permanent, which is why they're cached and refreshed
#     rather than fetched once and stored forever.

_LINK_CACHE_NAME = "quickbooks_invoice_links.json"


def _links_path() -> str:
    from . import jsonstore
    return os.path.join(jsonstore.data_root(), _LINK_CACHE_NAME)


def _load_links() -> dict:
    from . import jsonstore
    data = jsonstore.read_json(_links_path(), default={})
    return data if isinstance(data, dict) else {}


def _save_links(data: dict) -> None:
    # durable=False, and this is the one file here that genuinely earns it:
    # every link is re-fetched from QuickBooks by the invoice_links scheduler
    # job three times a day, so a lost disk costs a few hours of shareable
    # links rather than anything unrecoverable. Stated rather than assumed.
    from . import jsonstore
    with _lock:
        jsonstore.write_json(_links_path(), data, durable=False)


def fetch_public_link(invoice_id) -> str:
    """The no-login URL for one invoice, straight from QuickBooks."""
    tok = _ensure_access_token()
    r = requests.get(
        f"{_api_base()}/v3/company/{tok['realm_id']}/invoice/{invoice_id}",
        params={"minorversion": "70", "include": "invoiceLink"},
        headers={"Authorization": f"Bearer {tok['access_token']}",
                 "Accept": "application/json"},
        timeout=20,
    )
    if not r.ok:
        return ""
    try:
        return str((r.json().get("Invoice") or {}).get("InvoiceLink") or "")
    except ValueError:
        return ""


def public_link(invoice_id) -> str:
    """Cached public link, fetched on first use.

    Reads the cache rather than calling QuickBooks on every page render — an
    invoice list would otherwise make one API call per row.
    """
    iid = str(invoice_id)
    cache = _load_links()
    hit = cache.get(iid)
    if hit and hit.get("url"):
        return hit["url"]
    # A cache miss must never break the page that asked. This is called while
    # rendering an invoice list; before this guard, a disconnected QuickBooks
    # raised out of invoice_link() and took the whole card down.
    try:
        url = fetch_public_link(iid)
    except Exception:                                   # noqa: BLE001
        return ""
    if url:
        cache[iid] = {"url": url, "fetched": time.time()}
        _save_links(cache)
    return url


def refresh_public_links(days: int = 120, limit: int = 400) -> dict:
    """Refresh the cache. Called by the scheduler three times a day.

    Only recent and unpaid invoices: a paid invoice from last year doesn't
    need a live payment link, and refreshing everything would burn the API
    quota for no benefit.
    """
    import datetime as _dt
    if not connected():
        return {"skipped": "QuickBooks isn't connected."}
    since = (_dt.date.today() - _dt.timedelta(days=days)).isoformat()
    try:
        rows = _query(
            f"SELECT Id, DocNumber, Balance, TxnDate FROM Invoice "
            f"WHERE TxnDate >= '{since}' ORDERBY TxnDate DESC "
            f"MAXRESULTS {int(limit)}"
        ).get("Invoice", [])
    except Exception as exc:                            # noqa: BLE001
        return {"error": f"{type(exc).__name__}"}

    cache = _load_links()
    fetched = failed = 0
    for inv in rows:
        iid = str(inv.get("Id") or "")
        if not iid:
            continue
        url = fetch_public_link(iid)
        if url:
            cache[iid] = {"url": url, "fetched": time.time(),
                          "doc": inv.get("DocNumber"),
                          "balance": float(inv.get("Balance") or 0)}
            fetched += 1
        else:
            failed += 1
    _save_links(cache)
    return {"invoices_checked": len(rows), "links_fetched": fetched,
            "no_link": failed, "cached_total": len(cache),
            "note": ("InvoiceLink is production-only — a sandbox company "
                     "returns nothing for every invoice."
                     if failed and not fetched else "")}


def invoice_link(iid) -> str:
    """Prefer the public link; fall back to the QuickBooks app URL.

    The fallback still requires a login, so it's only useful internally. When
    the public link exists it is strictly better for both audiences.
    """
    pub = public_link(iid)
    return pub or f"https://app.qbo.intuit.com/app/invoice?txnId={iid}"


def link_status() -> dict:
    """How much of the invoice list has a public link, for Diagnostics."""
    cache = _load_links()
    with_url = sum(1 for v in cache.values() if v.get("url"))
    newest = max((v.get("fetched", 0) for v in cache.values()), default=0)
    return {"cached": len(cache), "with_public_link": with_url,
            "last_refresh": (datetime.fromtimestamp(newest, tz=timezone.utc)
                             .isoformat(timespec="seconds") if newest else None),
            "path": _links_path()}


def _customer_dict(c: dict) -> dict:
    created = ((c.get("MetaData") or {}).get("CreateTime") or "")[:10]
    since_label, years = "", None
    if created:
        import datetime as _dt
        try:
            d = _dt.date.fromisoformat(created)
            years = round((_dt.date.today() - d).days / 365.25, 1)
            since_label = d.strftime("%b %Y")
        except ValueError:
            pass
    return {
        "id": c.get("Id"),
        "name": c.get("DisplayName"),
        "balance": c.get("Balance", 0),
        "link": customer_link(c.get("Id")),
        "customer_since": created,
        "customer_since_label": since_label,
        "customer_years": years,
    }


def find_customers(q: str, limit: int = 5) -> list[dict]:
    rows = _query(
        f"SELECT * FROM Customer "
        f"WHERE DisplayName LIKE '%{_esc(q)}%' MAXRESULTS {int(limit)}"
    ).get("Customer", [])
    return [_customer_dict(c) for c in rows]


def customer_by_id(customer_id) -> dict | None:
    rows = _query(
        f"SELECT * FROM Customer WHERE Id = '{_esc(customer_id)}'"
    ).get("Customer", [])
    return _customer_dict(rows[0]) if rows else None


def invoices_for_customer(customer_id, limit: int = 8) -> list[dict]:
    rows = _query(
        f"SELECT Id, DocNumber, TxnDate, DueDate, TotalAmt, Balance FROM Invoice "
        f"WHERE CustomerRef = '{_esc(customer_id)}' "
        f"ORDERBY TxnDate DESC MAXRESULTS {int(limit)}"
    ).get("Invoice", [])
    import datetime as _dt
    today = _dt.date.today().isoformat()
    out = []
    for inv in rows:
        balance = float(inv.get("Balance") or 0)
        due = inv.get("DueDate") or ""
        if balance <= 0:
            status = "Paid"
        elif due and due < today:
            status = "Overdue"
        else:
            status = "Open"
        out.append({
            "id": inv.get("Id"),
            "public_link": bool(public_link(inv.get("Id"))),
            "doc_number": inv.get("DocNumber"),
            "date": inv.get("TxnDate"),
            "due_date": due,
            "total": inv.get("TotalAmt", 0),
            "balance": balance,
            "status": status,
            "link": invoice_link(inv.get("Id")),
        })
    return out


def _query_all(sql_base: str, page_size: int = 1000):
    """Run a QBO query with pagination (STARTPOSITION loop)."""
    rows, start = [], 1
    while True:
        resp = _query(f"{sql_base} STARTPOSITION {start} MAXRESULTS {page_size}")
        batch = None
        for v in resp.values():
            if isinstance(v, list):
                batch = v
                break
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < page_size:
            break
        start += page_size
        if start > 20000:  # safety cap
            break
    return rows


def invoices_since(date_iso: str) -> list[dict]:
    """All invoices on/after date_iso: customer name, date, total."""
    rows = _query_all(
        f"SELECT Id, TotalAmt, TxnDate, CustomerRef FROM Invoice "
        f"WHERE TxnDate >= '{_esc(date_iso)}' ORDERBY TxnDate"
    )
    out = []
    for inv in rows:
        ref = inv.get("CustomerRef") or {}
        out.append({
            "id": inv.get("Id"),
            "customer_id": ref.get("value"),
            "customer": ref.get("name") or "",
            "date": inv.get("TxnDate") or "",
            "total": float(inv.get("TotalAmt") or 0),
            "link": invoice_link(inv.get("Id")),
        })
    return out


def monthly_totals_by_customer(months: int = 4) -> dict:
    """{customer_name: {"id": qbid, "months": {"YYYY-MM": total}}} for the
    current month and the (months-1) prior months."""
    import datetime as _dt
    today = _dt.date.today()
    first = today.replace(day=1)
    for _ in range(months - 1):
        first = (first - _dt.timedelta(days=1)).replace(day=1)
    invoices = invoices_since(first.isoformat())
    out: dict = {}
    for inv in invoices:
        name = inv["customer"]
        if not name:
            continue
        ym = inv["date"][:7]
        rec = out.setdefault(name, {"id": inv.get("customer_id"), "months": {}})
        rec["months"][ym] = round(rec["months"].get(ym, 0) + inv["total"], 2)
    return out


def lookup(q: str, customer_id=None) -> dict:
    """Customer match(es) for a client name — or one attached customer by id —
    each with recent invoices."""
    if not configured():
        return {"configured": False, "connected": False, "customers": []}
    if not connected():
        return {"configured": True, "connected": False, "customers": []}
    if customer_id:
        ids = [x.strip() for x in str(customer_id).split(",") if x.strip()]
        customers = [c for c in (customer_by_id(i) for i in ids) if c]
    else:
        customers = find_customers(q, limit=3)
    for c in customers:
        try:
            c["invoices"] = invoices_for_customer(c["id"])
        except RuntimeError as exc:
            c["invoices"] = []
            c["error"] = str(exc)
    return {"configured": True, "connected": True, "customers": customers}
