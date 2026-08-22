"""Thin Knack REST wrapper.

When this module is merged into Smart 1 Hub, `hub/knack_api.py` already holds
credentials and a session. `_hub_client()` picks that up if it is importable;
otherwise the module talks to Knack directly using env credentials. Either way
the rest of the module only sees plain dicts.
"""
import json
import os
import time

import requests

from . import config
from hub import jsonstore

_MEM = {}


def _hub_client():
    """Use the Hub's own Knack layer when it exists."""
    try:
        from hub import knack_api  # type: ignore
        return knack_api
    except Exception:
        return None


def configured():
    return bool(config.KNACK_APP_ID and config.KNACK_API_KEY) or _hub_client() is not None


def demo_mode():
    return config.FORCE_DEMO or not configured()


def _headers():
    return {
        "X-Knack-Application-Id": config.KNACK_APP_ID,
        "X-Knack-REST-API-Key": config.KNACK_API_KEY,
        "Content-Type": "application/json",
    }


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------
def list_objects():
    """[{key, name}] for every object in the app."""
    if demo_mode():
        return [
            {"key": "object_demo_tickets", "name": "Web Tickets"},
            {"key": "object_demo_clients", "name": "Clients"},
        ]
    url = f"{config.KNACK_BASE}/objects"
    r = requests.get(url, headers=_headers(), timeout=30)
    r.raise_for_status()
    data = r.json() or {}
    return [{"key": o.get("key"), "name": o.get("name")}
            for o in data.get("objects", [])]


def list_fields(object_key):
    """[{key, name, type}] for one object."""
    if demo_mode():
        from .demo import demo_fields
        return demo_fields(object_key)
    url = f"{config.KNACK_BASE}/objects/{object_key}/fields"
    r = requests.get(url, headers=_headers(), timeout=30)
    r.raise_for_status()
    data = r.json() or {}
    return [{"key": f.get("key"), "name": f.get("name"), "type": f.get("type")}
            for f in data.get("fields", [])]


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------
def _snapshot_path(object_key):
    return os.path.join(config.DATA_DIR, f"snapshot_{object_key}.json")


def fetch_records(object_key, force=False):
    """Every record in an object, paginated. Cached in memory and on disk.

    The disk snapshot exists so a Knack outage degrades to yesterday's numbers
    with a visible timestamp rather than an empty report.
    """
    if not object_key:
        return [], None

    hit = _MEM.get(object_key)
    now = time.time()
    if hit and not force and now - hit["at"] < config.CACHE_TTL:
        return hit["records"], hit["at"]

    if demo_mode():
        from .demo import demo_records
        records = demo_records(object_key)
        _MEM[object_key] = {"records": records, "at": now}
        return records, now

    hub = _hub_client()
    records = []
    try:
        if hub is not None and hasattr(hub, "get_all_records"):
            records = hub.get_all_records(object_key) or []
        else:
            page = 1
            while True:
                url = f"{config.KNACK_BASE}/objects/{object_key}/records"
                r = requests.get(url, headers=_headers(), timeout=60,
                                 params={"page": page, "rows_per_page": 1000})
                r.raise_for_status()
                body = r.json() or {}
                records.extend(body.get("records", []))
                if page >= int(body.get("total_pages") or 1):
                    break
                page += 1
    except Exception:
        cached = _read_snapshot(object_key)
        if cached:
            return cached["records"], cached["at"]
        raise

    _MEM[object_key] = {"records": records, "at": now}
    _write_snapshot(object_key, records, now)
    return records, now


def _write_snapshot(object_key, records, at):
    # durable=False. This is a copy of live Knack records kept only so the
    # page can render something when the API is slow or down; Knack is the
    # source of truth and re-reading it costs one request. Backing it up would
    # mean every ticket in the system riding along in the database backup for
    # no recovery value.
    try:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        jsonstore.write_json(_snapshot_path(object_key),
                             {"at": at, "records": records}, durable=False)
    except Exception:
        pass


def _read_snapshot(object_key):
    # restore=False for the same reason: a snapshot restored from a backup
    # would carry its original "at" timestamp and be presented as that fresh.
    return jsonstore.read_json(_snapshot_path(object_key), default=None,
                               restore=False)
