"""GoHighLevel OAuth — install once at the agency, mint location tokens forever.

Why this exists
---------------
Every Suite call authenticated with one agency-level Private Integration Token
(``GHL_PRIVATE_TOKEN``). An agency token cannot read sub-account resources —
Forms is the one that surfaced it — and HighLevel has no API that creates a
Private Integration Token, so "just add a token per client" meant a manual
step in the UI for every client, for ever, plus somewhere to keep N secrets.

A Marketplace app is installed once at the agency. After that the Hub exchanges
the agency token for a **location-scoped** token on demand, for any sub-account,
with no per-client setup — including clients added next year.

The flow (HighLevel API version 2021-07-28, matching the rest of the Hub):

1. ``authorize_url()`` sends the agency owner to the marketplace consent page.
2. The callback hands us a code; ``exchange_code()`` swaps it for an agency
   access token + refresh token (``user_type=Company``) and stores them.
3. ``agency_token()`` keeps that pair alive, refreshing before expiry.
4. ``location_token(location_id)`` POSTs to ``/oauth/locationToken`` and caches
   the result — HighLevel issues these for 24 hours.

Nothing here is load-bearing until the app is installed: callers fall back to
the existing Private Integration Token, so the Hub behaves exactly as before
until the install happens.
"""
from __future__ import annotations

import json
import os
import threading
import time

import requests

API_BASE = os.environ.get("GHL_API_BASE", "https://services.leadconnectorhq.com")
API_VERSION = os.environ.get("GHL_API_VERSION", "2021-07-28")
MARKETPLACE_BASE = os.environ.get(
    "GHL_MARKETPLACE_BASE", "https://marketplace.gohighlevel.com")

CLIENT_ID = (os.environ.get("GHL_CLIENT_ID") or "").strip()
CLIENT_SECRET = (os.environ.get("GHL_CLIENT_SECRET") or "").strip()

# HighLevel's client_id is "<appId>-<suffix>", and installedLocations wants the
# appId half, not the whole thing. Overridable in case that ever stops holding.
APP_ID = (os.environ.get("GHL_APP_ID") or CLIENT_ID.split("-")[0]).strip()

# Scopes the app requests. Location tokens inherit whatever the agency token
# was granted, so anything missing here is missing everywhere — but adding a
# scope later requires re-consent, so ask for the full read set up front.
DEFAULT_SCOPES = (
    "locations.readonly "
    "forms.readonly forms/submissions.readonly "
    "contacts.readonly opportunities.readonly "
    "calendars.readonly conversations.readonly users.readonly"
)
SCOPES = (os.environ.get("GHL_OAUTH_SCOPES") or DEFAULT_SCOPES).strip()

# Refresh this long before the stated expiry, so a token is never handed out
# with seconds left on it.
_EXPIRY_MARGIN = 300

_lock = threading.Lock()
_loc_cache: dict[str, tuple[str, float]] = {}


class NotConnected(RuntimeError):
    """No agency install yet — the one-time consent step hasn't been done."""


class GhlOAuthError(RuntimeError):
    """HighLevel refused a token request."""


# ------------------------------------------------------------------ storage
def _data_dir() -> str:
    from . import jsonstore
    return jsonstore.data_root()


def _store_path() -> str:
    return os.environ.get("GHL_OAUTH_STORE") or os.path.join(
        _data_dir(), "ghl_oauth.json")


def _fernet():
    """Reuse the key Google Finder already encrypts its refresh tokens with.

    Optional: without it the file is still written, just unencrypted. A refresh
    token in the clear on the private disk is no worse than the Private
    Integration Token sitting in an environment variable, and refusing to work
    without the key would make this harder to adopt than the thing it replaces.
    """
    key = (os.environ.get("TOKEN_ENCRYPTION_KEY") or "").strip()
    if not key:
        return None
    try:
        from cryptography.fernet import Fernet
        return Fernet(key.encode("utf-8"))
    except Exception:  # noqa: BLE001 — bad key means store in the clear
        return None


def _save(record: dict) -> None:
    raw = json.dumps(record).encode("utf-8")
    f = _fernet()
    blob = {"enc": bool(f)}
    blob["data"] = (f.encrypt(raw).decode("ascii") if f
                    else raw.decode("utf-8"))
    # Through hub.jsonstore, which keeps the same atomic write and adds the
    # database mirror. This file is the only copy of the agency refresh token:
    # if the disk is recreated it cannot be re-derived from anywhere, and every
    # Suite call quietly drops back to the Private Integration Token that
    # cannot read sub-account resources — the exact failure this module was
    # written to end. Recovery would mean an agency owner re-consenting to the
    # marketplace app, and nothing would say that is what happened.
    from . import jsonstore
    jsonstore.write_json(_store_path(), blob)


def _load() -> dict | None:
    from . import jsonstore
    blob = jsonstore.read_json(_store_path(), default=None)
    if not isinstance(blob, dict):
        return None
    data = blob.get("data")
    if not data:
        return None
    if blob.get("enc"):
        f = _fernet()
        if not f:
            return None                      # key rotated away; re-consent
        try:
            data = f.decrypt(data.encode("ascii")).decode("utf-8")
        except Exception:  # noqa: BLE001
            return None
    try:
        return json.loads(data)
    except ValueError:
        return None


def disconnect() -> None:
    """Forget the install. The app stays installed in HighLevel — this only
    drops our copy of the tokens, so re-consenting is the way back."""
    with _lock:
        _loc_cache.clear()
        # delete_json, not os.remove: dropping only the file would leave the
        # mirrored copy behind for the next _load() to restore, so disconnect
        # would appear to work and then silently undo itself.
        from . import jsonstore
        jsonstore.delete_json(_store_path())


# ------------------------------------------------------------------- config
def configured() -> bool:
    """True when the app credentials exist — not that anyone has consented."""
    return bool(CLIENT_ID and CLIENT_SECRET)


def connected() -> bool:
    return bool(_load())


def redirect_uri() -> str:
    base = (os.environ.get("PUBLIC_BASE_URL") or "").rstrip("/")
    return f"{base}/suite/oauth/callback"


def authorize_url(state: str) -> str:
    from urllib.parse import urlencode
    params = {
        "response_type": "code",
        "redirect_uri": redirect_uri(),
        "client_id": CLIENT_ID,
        "scope": SCOPES,
        "state": state,
    }
    return f"{MARKETPLACE_BASE}/oauth/chooselocation?" + urlencode(params)


# -------------------------------------------------------------- token calls
def _token_request(payload: dict) -> dict:
    r = requests.post(
        f"{API_BASE}/oauth/token",
        data=payload,
        headers={"Accept": "application/json",
                 "Content-Type": "application/x-www-form-urlencoded"},
        timeout=20,
    )
    try:
        data = r.json() if r.text else {}
    except ValueError:
        data = {}
    if not r.ok or not data.get("access_token"):
        msg = data.get("message") or data.get("error") or f"HTTP {r.status_code}"
        if isinstance(msg, list):
            msg = ", ".join(str(m) for m in msg)
        raise GhlOAuthError(f"HighLevel refused the token request: {msg}")
    return data


def _store_from_token_response(data: dict) -> dict:
    record = {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token", ""),
        "company_id": data.get("companyId") or os.environ.get("GHL_COMPANY_ID", ""),
        "user_type": data.get("userType", "Company"),
        "scope": data.get("scope", ""),
        "expires_at": time.time() + float(data.get("expires_in") or 86400),
        "obtained_at": time.time(),
    }
    _save(record)
    return record


def exchange_code(code: str) -> dict:
    """Finish the consent redirect. Returns the stored record."""
    if not configured():
        raise GhlOAuthError("GHL_CLIENT_ID / GHL_CLIENT_SECRET are not set.")
    data = _token_request({
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "user_type": "Company",
        "redirect_uri": redirect_uri(),
    })
    with _lock:
        _loc_cache.clear()      # new install, new grant: old location tokens die
        return _store_from_token_response(data)


def _refresh(record: dict) -> dict:
    if not record.get("refresh_token"):
        raise NotConnected("Stored install has no refresh token; re-connect.")
    data = _token_request({
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": record["refresh_token"],
        "user_type": "Company",
    })
    # HighLevel may return the same refresh token or a new one; keep the old
    # one when the response omits it, or the install becomes unrecoverable.
    data.setdefault("refresh_token", record["refresh_token"])
    data.setdefault("companyId", record.get("company_id", ""))
    return _store_from_token_response(data)


def agency_token() -> str:
    """A live agency access token, refreshed when it's close to expiring."""
    with _lock:
        record = _load()
        if not record:
            raise NotConnected(
                "The Smart 1 Suite app isn't connected yet. Open Suite and "
                "click Connect to authorise it once for the agency.")
        if record.get("expires_at", 0) - _EXPIRY_MARGIN <= time.time():
            try:
                record = _refresh(record)
            except GhlOAuthError:
                # Two gunicorn workers can refresh at the same moment; if the
                # other one won, its rotated token is already on disk and ours
                # is dead. Re-read before giving up.
                fresh = _load() or {}
                if fresh.get("access_token") and fresh.get("expires_at", 0) > time.time():
                    return fresh["access_token"]
                raise
        return record.get("access_token", "")


def company_id() -> str:
    record = _load() or {}
    return record.get("company_id") or os.environ.get("GHL_COMPANY_ID", "")


def location_token(location_id: str) -> str:
    """A token scoped to one sub-account, minted from the agency token.

    This is the part that makes Forms (and every other location-scoped
    resource) work without a hand-made Private Integration Token per client.
    HighLevel issues these for 24 hours; they're cached in memory, so a restart
    costs one extra call and nothing else.
    """
    location_id = (location_id or "").strip()
    if not location_id:
        raise GhlOAuthError("location_token() needs a location id.")

    with _lock:
        hit = _loc_cache.get(location_id)
        if hit and hit[1] - _EXPIRY_MARGIN > time.time():
            return hit[0]

    token = agency_token()          # outside the lock: may do a network refresh
    r = requests.post(
        f"{API_BASE}/oauth/locationToken",
        data={"companyId": company_id(), "locationId": location_id},
        headers={
            "Authorization": f"Bearer {token}",
            "Version": API_VERSION,
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        timeout=20,
    )
    try:
        data = r.json() if r.text else {}
    except ValueError:
        data = {}
    if not r.ok or not data.get("access_token"):
        msg = data.get("message") or data.get("error") or f"HTTP {r.status_code}"
        if isinstance(msg, list):
            msg = ", ".join(str(m) for m in msg)
        raise GhlOAuthError(
            f"Could not get a token for sub-account {location_id}: {msg}. "
            "Usually this means the app isn't installed on that sub-account.")

    expires_at = time.time() + float(data.get("expires_in") or 86400)
    with _lock:
        _loc_cache[location_id] = (data["access_token"], expires_at)
    return data["access_token"]


def installed_locations(limit: int = 500) -> list[dict]:
    """Sub-accounts the app is installed on — the ones location_token() can
    serve. Useful for spotting a client the install missed."""
    r = requests.get(
        f"{API_BASE}/oauth/installedLocations",
        params={"companyId": company_id(), "appId": APP_ID,
                "limit": str(limit), "skip": "0"},
        headers={"Authorization": f"Bearer {agency_token()}",
                 "Version": API_VERSION, "Accept": "application/json"},
        timeout=20,
    )
    if not r.ok:
        raise GhlOAuthError(f"Could not list installed sub-accounts: HTTP {r.status_code}")
    try:
        return (r.json() or {}).get("locations") or []
    except ValueError:
        return []


def status() -> dict:
    """Plain-language state for the Suite panel and /status."""
    record = _load()
    if not configured():
        return {"configured": False, "connected": False,
                "detail": "GHL_CLIENT_ID and GHL_CLIENT_SECRET are not set. "
                          "Create the Marketplace app, then add them."}
    if not record:
        return {"configured": True, "connected": False,
                "detail": "Not authorised yet — connect once as the agency owner."}
    left = int(record.get("expires_at", 0) - time.time())
    return {
        "configured": True,
        "connected": True,
        "company_id": record.get("company_id", ""),
        "scope": record.get("scope", ""),
        "expires_in_seconds": max(0, left),
        "cached_location_tokens": len(_loc_cache),
        "detail": "Connected. Sub-account tokens are minted on demand.",
    }
