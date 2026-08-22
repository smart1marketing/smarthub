"""QA reports — data-quality and billing checks across Smart 1 Team + QuickBooks.

Each report returns {"columns": [...], "rows": [...], "note": str} where every
row is a list matching the columns.  A cell may also be a {"text","href"} dict,
which the report page renders as a link.  Everything else is plain text.

Knack-only reports run straight off clients_app/data/*.json; the two invoice
reports need a connected QuickBooks company and degrade with a friendly note
when it isn't connected.
"""
import datetime as _dt
import json
import os
import re

from . import jsonstore
from . import knack_data


# ------------------------------------------------------------------ helpers
def _num(v):
    return knack_data._num(v)


def _parse_date(s):
    """mm/dd/yyyy -> date, else None."""
    s = str(s or "").strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return _dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _money(v) -> str:
    try:
        return f"${float(v):,.0f}"
    except (TypeError, ValueError):
        return "$0"


def _c360_link(client: str) -> dict:
    from urllib.parse import quote
    return {"text": client, "href": "/client360?q=" + quote(client)}


def _client_groups() -> dict:
    """{client_name: {"rows": [...], "partner", "sales", "live": [...],
        "thisM": bool, "lastM": bool, "this_total", "last_total",
        "live_total", "has_dash": bool, "last_end": date|None}}"""
    groups: dict[str, dict] = {}
    for r in knack_data.products():
        client = str(r.get("client", "")).strip()
        if not client:
            continue
        g = groups.setdefault(client, {
            "rows": [], "live": [], "thisM": False, "lastM": False,
            "this_total": 0.0, "last_total": 0.0, "live_total": 0.0,
            "has_dash": False, "last_end": None,
            "partners": set(), "sales": set(),
        })
        g["rows"].append(r)
        if r.get("partner"):
            g["partners"].add(str(r["partner"]).strip())
        if r.get("sales"):
            g["sales"].add(str(r["sales"]).strip())
        m = _num(r.get("monthly"))
        if r.get("thisM"):
            g["thisM"] = True
            g["this_total"] += m
        if r.get("lastM"):
            g["lastM"] = True
            g["last_total"] += m
        if knack_data.is_running(r):
            g["live"].append(r)
            g["live_total"] += m
            if isinstance(r.get("dash"), str) and r["dash"].startswith("http"):
                g["has_dash"] = True
        end = _parse_date(r.get("end"))
        if end and (g["last_end"] is None or end > g["last_end"]):
            g["last_end"] = end
    return groups


def _is_active(g: dict) -> bool:
    return bool(g["live"]) or g["thisM"]


def _join(vals) -> str:
    """Join partner names, folding case-only duplicates together.

    Knack holds "MOTO" and "Moto" as separate partner values for the same
    company, so reports grouped by partner showed them as two rows with the
    revenue split between them. Case is not a meaningful distinction here.
    The first spelling encountered wins, so the display stays stable rather
    than flipping between runs.
    """
    seen, out = {}, []
    for v in vals:
        v = str(v or "").strip()
        if not v:
            continue
        k = v.lower()
        if k in seen:
            continue
        seen[k] = v
        out.append(v)
    return ", ".join(sorted(out, key=str.lower)) or "—"


def _norm_name(s: str) -> str:
    """Normalize a business name for QB<->Knack matching."""
    import re
    s = str(s or "").lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    drop = {"inc", "llc", "co", "corp", "company", "the", "of", "and", "dba",
            "ltd", "lp", "pllc", "pc", "group"}
    words = [w for w in s.split() if w and w not in drop]
    return " ".join(words)


# ------------------------------------------------- dashboard skip list
#
# Some clients genuinely don't need a dashboard — a one-off creative job, a
# partner who reports themselves. Without somewhere to record that, the same
# names sit on the report forever and people stop reading it.

# Both files below are human decisions with no upstream: who was excused from
# a report and why, and which partner owns an invoiced-off customer. Nothing
# can recompute them, so they go through hub.jsonstore to land in the database
# backup rather than only on a disk that is not backed up. Losing them would
# not look like data loss either — the reports would simply start flagging
# names that were settled months ago, and read as a regression in the checks.
def _skip_path() -> str:
    return os.path.join(jsonstore.data_root(), "dashboard_skips.json")


def _load_skips() -> dict:
    data = jsonstore.read_json(_skip_path(), default={})
    return data if isinstance(data, dict) else {}


def _dash_skipped(client: str) -> bool:
    return _norm_client(client) in _load_skips()


def skip_dashboard(client: str, actor: str = "", reason: str = "") -> dict:
    from datetime import datetime, timezone
    data = _load_skips()
    data[_norm_client(client)] = {
        "client": client, "by": actor,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "reason": reason,
    }
    jsonstore.write_json(_skip_path(), data)
    return {"ok": True, "skipped": len(data)}


def unskip_dashboard(client: str) -> dict:
    data = _load_skips()
    data.pop(_norm_client(client), None)
    jsonstore.write_json(_skip_path(), data)
    return {"ok": True, "skipped": len(data)}


def skipped_dashboards() -> dict:
    """The skip list, so a decision made months ago is reviewable."""
    rows = []
    for rec in sorted(_load_skips().values(), key=lambda r: r.get("client", "")):
        rows.append([
            _c360_link(rec.get("client", "")),
            rec.get("by") or "—",
            (rec.get("at") or "")[:10],
            rec.get("reason") or "—",
            {"actions": [{"label": "Un-skip", "action": "unskip-dashboard",
                          "client": rec.get("client", "")}]},
        ])
    return {"columns": ["Client", "Skipped by", "When", "Reason", ""],
            "rows": rows,
            "note": (f"{len(rows)} client(s) deliberately excluded from the "
                     f"No Dashboards report."
                     if rows else "Nothing skipped.")}


def _norm_client(v: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(v or "").lower())


# ------------------------------------------------------------------ reports
from hub import dates as _dates


def _end_bucket(end) -> str:
    """This month / next month / other, from the product end date.

    A renewal conversation is driven by when something stops, so the report is
    organised the way the work is: what ends now, what ends next, everything
    else.
    """
    import datetime as _dt
    if not end:
        return "Other"
    if isinstance(end, str):
        try:
            end = _dt.date.fromisoformat(end[:10])
        except ValueError:
            return "Other"
    today = _dt.date.today()
    if end.year == today.year and end.month == today.month:
        return "Ending this month"
    nxt_y, nxt_m = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    if end.year == nxt_y and end.month == nxt_m:
        return "Ending next month"
    return "Other"


def active_clients() -> dict:
    groups = _client_groups()
    buckets = {"Ending this month": [], "Ending next month": [], "Other": []}
    skipped_empty = 0

    for name in groups:
        g = groups[name]
        if not _is_active(g):
            continue
        # `thisM` is a boolean, so a client flagged as billed this month at
        # $0.00 with no live product still passed _is_active — which is why
        # rows showing "0 products, $0" were appearing. A client with neither
        # a live product nor actual billing is not an active client.
        if not g["live"] and not (g.get("this_total") or 0):
            skipped_empty += 1
            continue

        # A client with nothing live cannot be "ending" — whatever they had has
        # already stopped. Filing them under Ending this month put rows reading
        # "0 products" next to renewals that genuinely need a call, which is
        # the fastest way to make a queue stop being trusted. They still appear,
        # under Other, because they are billing and that is worth seeing.
        bucket = _end_bucket(g.get("last_end")) if g["live"] else "Other"

        partner = _join(g["partners"])
        buckets[bucket].append({
            "partner": (partner or "").lower(),
            "ends": g.get("last_end"),
            "name": name.lower(),
            # With nothing running, the last end date is not when this client
            # ends — it is when they stopped. Printing it bare under "Ends"
            # reads as something upcoming, next to a live monthly of $0. Say
            # which it is, and say what they actually billed, because that
            # billing is the only reason the row is here at all.
            "row": [
                _c360_link(name),
                partner,
                len(g["live"]) if g["live"] else {
                    "text": "none running", "muted": True},
                _money(g["live_total"]) if g["live"] else {
                    "text": _money(g.get("this_total") or 0) + " billed this month",
                    "muted": True},
                (_dates.fmt(g.get("last_end")) if g["live"]
                 else {"text": "ended " + _dates.fmt(g.get("last_end")), "muted": True}),
                "Yes" if g["has_dash"] else "No",
            ],
        })

    # Ending buckets group the work by who we call about it. Other is a
    # watch-list, so it leads with whatever expires soonest — sorted on the
    # real date, not the formatted string, or 01-05-27 would sort above
    # 12-31-26.
    for label in ("Ending this month", "Ending next month"):
        buckets[label].sort(key=lambda r: (r["partner"], _dates.sort_key(r["ends"]), r["name"]))
    # "Next to expire" means the next one, not the oldest one. A plain
    # ascending sort put July dates at the top of a list read in August —
    # those have already ended, so they are not what anyone is watching for.
    # Upcoming first, then the already-ended most-recent-first, then unknown.
    import datetime as _d
    _today = _d.date.today()

    def _other_key(r):
        d = _dates.to_date(r["ends"])
        if d is None:
            return (2, _d.date.max, r["partner"], r["name"])
        if d >= _today:
            return (0, d, r["partner"], r["name"])
        return (1, _d.date.max - (d - _d.date.min), r["partner"], r["name"])

    buckets["Other"].sort(key=_other_key)

    tones = {"Ending this month": "now", "Ending next month": "soon", "Other": "later"}
    rows = []
    for label in ("Ending this month", "Ending next month", "Other"):
        if not buckets[label]:
            continue
        rows.append([{"text": f"{label} ({len(buckets[label])})",
                      "group": True, "tone": tones[label]}, "", "", "", "", ""])
        rows.extend(r["row"] for r in buckets[label])

    total = sum(len(v) for v in buckets.values())
    return {
        "columns": ["Client", "Partner", "Live products", "Live monthly",
                    "Ends", "Dashboard"],
        "rows": rows,
        "note": (f"{total} active clients. Ending this month and next are "
                 f"grouped by partner; everything else leads with whatever "
                 f"expires soonest."
                 + (f" {skipped_empty} excluded with no live product and no "
                    f"billing." if skipped_empty else "")),
    }


def no_dashboards() -> dict:
    groups = _client_groups()
    by_partner: dict[str, list] = {}
    total = 0
    for name in sorted(groups, key=str.lower):
        g = groups[name]
        if not _is_active(g) or g["has_dash"]:
            continue
        # At least one LIVE product. A client billed this month with nothing
        # running doesn't need a dashboard — there's nothing to report on, so
        # chasing one is busywork that makes the list look longer than the
        # actual job.
        if not g["live"]:
            continue
        if _dash_skipped(name):
            continue
        total += 1
        prods = sorted({str(r.get("product") or "") for r in g["live"]} or
                       {str(r.get("product") or "") for r in g["rows"] if r.get("thisM")})
        partner = _join(g["partners"])
        by_partner.setdefault(partner, []).append([
            partner if partner != "—" else "(no partner)",
            _c360_link(name),
            len(g["live"]),
            ", ".join(p for p in prods if p)[:120] or "—",
            _money(g["live_total"] or g["this_total"]),
            {"actions": [
                {"label": "Add dashboard", "action": "add-dashboard",
                 "client": name},
                {"label": "Skip", "action": "skip-dashboard", "client": name,
                 "confirm": f"Skip {name}? It leaves this list until you "
                            f"un-skip it at the bottom of the page."},
            ]},
        ])
    rows, styles = [], []
    # Fold any remaining case-only duplicate GROUP keys into one bucket, so
    # "MOTO" and "Moto" don't render as two partners with split subtotals.
    folded = {}
    for k, v in list(by_partner.items()):
        canon = next((c for c in folded if c.lower() == k.lower()), k)
        folded.setdefault(canon, []).extend(v)
    by_partner = folded
    keys = sorted([k for k in by_partner if k != "—"], key=str.lower) + \
        (["—"] if "—" in by_partner else [])
    for k in keys:
        for r in by_partner[k]:
            rows.append(r)
            styles.append(None)
    return {
        "columns": ["Partner", "Client", "Live products",
                    "Products", "Monthly"],
        "rows": rows,
        "row_styles": styles,
        "note": (f"{total} active clients with no Smart 1 Dashboard link on any "
                 "live product — broken down by partner (clients with no partner "
                 "listed at the end)."),
    }


def stale_90() -> dict:
    groups = _client_groups()
    today = _dt.date.today()
    by_partner: dict[str, list] = {}
    total = 0
    for name, g in groups.items():
        if g["live"] or g["thisM"]:          # currently active — skip
            continue
        if not g["last_end"]:
            continue
        days = (today - g["last_end"]).days
        # 90 days quiet is the flag; 180 is the ceiling. Beyond six months
        # they aren't a lapsed client to chase, they're a former one, and
        # mixing the two makes the list too long to work through.
        if days < 90 or days > 180:
            continue
        total += 1
        last_total = max(g["last_total"], max(
            (_num(r.get("monthly")) for r in g["rows"]
             if _parse_date(r.get("end")) == g["last_end"]), default=0.0))
        partner = _join(g["partners"])
        by_partner.setdefault(partner, []).append((days, last_total, [
            partner if partner != "—" else "(no partner)",
            _c360_link(name),
            _join(g["sales"]),
            _dates.fmt(g["last_end"]),
            days,
            _money(last_total),
        ]))
    rows, styles = [], []
    # Fold any remaining case-only duplicate GROUP keys into one bucket, so
    # "MOTO" and "Moto" don't render as two partners with split subtotals.
    folded = {}
    for k, v in list(by_partner.items()):
        canon = next((c for c in folded if c.lower() == k.lower()), k)
        folded.setdefault(canon, []).extend(v)
    by_partner = folded
    keys = sorted([k for k in by_partner if k != "—"], key=str.lower) + \
        (["—"] if "—" in by_partner else [])
    grand = 0.0
    for k in keys:
        items = sorted(by_partner[k], key=lambda t: t[0])
        sub = sum(t[1] for t in items)
        grand += sub
        for _, _, r in items:
            rows.append(r)
            styles.append(None)
        label = k if k != "—" else "(no partner)"
        rows.append([f"{label} — subtotal", f"{len(items)} client(s)", "", "", "",
                     _money(sub)])
        styles.append("sub")
    return {
        "columns": ["Partner", "Client", "Salesperson", "Last product ended",
                    "Days since", "Last monthly"],
        "rows": rows,
        "row_styles": styles,
        "note": (f"{total} clients with no live product whose last IO ended 90+ "
                 f"days ago (up to 24 months back) — {_money(grand)}/mo of lapsed "
                 "billing, grouped by partner with subtotals; clients without a "
                 "partner listed at the end."),
    }


def lost_by_partner() -> dict:
    groups = _client_groups()
    rows = []
    for name, g in groups.items():
        if g["lastM"] and not g["thisM"] and not g["live"]:
            partner = _join(g["partners"])
            rows.append((partner.lower(), [
                partner,
                _c360_link(name),
                _join(g["sales"]),
                _money(g["last_total"]),
            ]))
    rows.sort(key=lambda t: (t[0], str(t[1][1].get("text", "")).lower()))
    total = sum(_num(str(r[3]).replace("$", "")) for _, r in rows)
    return {
        "columns": ["Partner", "Client", "Salesperson", "Billing last month"],
        "rows": [r for _, r in rows],
        "note": (f"{len(rows)} clients ran last month but have nothing live this "
                 f"month — {_money(total)}/mo walked out the door. Grouped by partner."),
    }


def _last_12_months() -> list[dict]:
    first = _dt.date.today().replace(day=1)
    out = []
    for _ in range(12):
        out.append({"ym": first.strftime("%Y%m"), "label": first.strftime("%b %Y")})
        first = (first - _dt.timedelta(days=1)).replace(day=1)
    return out


def _month_bounds(ym: str):
    import calendar
    y, m = int(ym[:4]), int(ym[4:6])
    return _dt.date(y, m, 1), _dt.date(y, m, calendar.monthrange(y, m)[1])


def _active_in_month(r: dict, mstart, mend) -> bool:
    """An IO counts for a month when its date range covers any of it and it
    actually ran (Live or Complete — cancelled/pending never count)."""
    status = str(r.get("status", "")).strip().lower()
    if status not in ("live", "complete"):
        return False
    s, e = _parse_date(r.get("start")), _parse_date(r.get("end"))
    if s and s > mend:
        return False
    if e and e < mstart:
        return False
    if not s and not e:          # undated: only trust currently-live rows
        return status == "live"
    return True


def _month_rollup(field: str, ym: str) -> dict:
    """{who: {"clients": {client: budget}, "products": n, "revenue": x}}"""
    mstart, mend = _month_bounds(ym)
    by: dict[str, dict] = {}
    for r in knack_data.products():
        who = str(r.get(field, "")).strip()
        client = str(r.get("client", "")).strip()
        if not who or not client:
            continue
        if not _active_in_month(r, mstart, mend):
            continue
        s = by.setdefault(who, {"clients": {}, "products": 0, "revenue": 0.0})
        m = _num(r.get("monthly"))
        s["clients"][client] = s["clients"].get(client, 0.0) + m
        s["products"] += 1
        s["revenue"] += m
    return by


def _prev_ym(ym: str) -> str:
    first, _ = _month_bounds(ym)
    return (first - _dt.timedelta(days=1)).strftime("%Y%m")


def _scorecard(field: str, month: str = "") -> dict:
    """Salesperson / partner scorecard for any of the last 12 months.
    Rows with zero active clients are hidden; each row is colored by the
    person's revenue vs the previous month (green up / yellow flat / red down)."""
    months = _last_12_months()
    ym = month if month in {m["ym"] for m in months} else months[0]["ym"]
    cur = _month_rollup(field, ym)
    prev = _month_rollup(field, _prev_ym(ym))
    totals = {"clients": 0, "prev_clients": 0, "products": 0,
              "prev_products": 0, "revenue": 0.0}

    rows, styles = [], []
    for who in sorted(cur, key=lambda k: -cur[k]["revenue"]):
        s = cur[who]
        if not s["clients"]:
            continue
        p = prev.get(who, {"clients": {}, "revenue": 0.0})
        new = sum(1 for c in s["clients"] if c not in p["clients"])
        lost = sum(1 for c in p["clients"] if c not in s["clients"])
        up = sum(1 for c, v in s["clients"].items()
                 if c in p["clients"] and v > p["clients"][c] + 0.5)
        down = sum(1 for c, v in s["clients"].items()
                   if c in p["clients"] and v < p["clients"][c] - 0.5)
        # New/Lost/Increased/Decreased were four columns saying what two
        # numbers already imply. The change now sits beside the number it
        # describes, which is where you read it.
        rows.append([
            who,
            {"text": len(s["clients"]), "delta": len(s["clients"]) - len(p["clients"]),
             "title": f"{new} new, {lost} lost vs last month"},
            {"text": s["products"], "delta": s["products"] - p.get("products", 0),
             "title": f"{up} increased, {down} decreased vs last month"},
            _money(s["revenue"]),
        ])
        diff = s["revenue"] - p["revenue"]
        styles.append("green" if diff > 0.5 else ("red" if diff < -0.5 else "yellow"))
        totals["clients"] += len(s["clients"])
        totals["prev_clients"] += len(p["clients"])
        totals["products"] += s["products"]
        totals["prev_products"] += p.get("products", 0)
        totals["revenue"] += s["revenue"]

    label = "salespeople" if field == "sales" else "partners"
    mlabel = next(m["label"] for m in months if m["ym"] == ym)
    return {
        "columns": [("Salesperson" if field == "sales" else "Partner"),
                    "Active clients", "Active products", "Monthly revenue"],
        "rows": rows + ([[
            {"text": "TOTAL", "group": True},
            {"text": totals["clients"],
             "delta": totals["clients"] - totals["prev_clients"]},
            {"text": totals["products"],
             "delta": totals["products"] - totals["prev_products"]},
            _money(totals["revenue"]),
        ]] if rows else []),
        # One style per row including the totals row, or the colouring shifts
        # by one and every partner shows the row above's verdict.
        "row_styles": styles + ([None] if rows else []),
        "month": ym,
        "month_options": months,
        "note": (f"{len(rows)} {label} active in {mlabel}, ranked by monthly revenue. "
                 "Row color = revenue vs the previous month (green up · yellow flat · red down). "
                 "Counts an IO in a month when its start/end dates cover it."),
    }


def salesperson_scorecard(month: str = "") -> dict:
    return _scorecard("sales", month)


def partner_scorecard(month: str = "") -> dict:
    return _scorecard("partner", month)


# ------------------------------------- missing Google accounts (GA / GTM)
GTM_PRIORITY_KEYWORDS = ("display", "radio", "podcast", "audio", "seo",
                         "search engine marketing", "pay per click", "sem",
                         "paid search", "retargeting")


def _active_within(g: dict, days: int = 60) -> bool:
    """Has this client had a product running in the last `days`?

    Analytics and GTM only matter for a site we're currently driving traffic
    to. A client who stopped four months ago will show as missing forever, and
    every one of those makes the report less likely to be read.
    """
    if g["live"] or g["thisM"]:
        return True
    end = g.get("last_end")
    if not end:
        return False
    return (_dt.date.today() - end).days <= days


def _google_coverage(name: str, g: dict) -> dict:
    """What Google plumbing we have for a client: website GA/GTM fields plus
    manually attached accounts."""
    from . import seo
    webs = seo._client_websites(name)
    att = seo.get_links(name)
    domain = ""
    for w in webs:
        d = str(w.get("domain") or "").strip()
        if d:
            domain = d.replace("https://", "").replace("http://", "").strip("/")
            break
    return {
        "has_ga": bool(att.get("analytics")) or any(str(w.get("ga") or "").strip() for w in webs),
        "has_gtm": bool(att.get("gtm")) or any(str(w.get("gtm") or "").strip() for w in webs),
        "domain": domain,
    }


def no_analytics() -> dict:
    groups = _client_groups()
    rows, styles = [], []
    for name in sorted(groups, key=str.lower):
        g = groups[name]
        if not _active_within(g, 60):
            continue
        cov = _google_coverage(name, g)
        if cov["has_ga"]:
            continue
        rows.append([
            _c360_link(name),
            _join(g["partners"]),
            _join(g["sales"]),
            cov["domain"] or "—",
            _money(g["live_total"] or g["this_total"]),
            {"search_attach": name, "kind": "analytics", "q": cov["domain"] or name},
        ])
        styles.append(None)
    return {
        "columns": ["Client", "Partner", "Salesperson", "Website",
                    "Monthly", "Analytics account"],
        "rows": rows,
        "row_styles": styles,
        "note": (f"{len(rows)} active clients with no Google Analytics on file "
                 "(website record or attached account). Search your connected "
                 "Google logins and attach the right property — it's saved to "
                 "the client universally and the client drops off this report."),
    }


def _gtm_from_scan(domain: str) -> str:
    """A GTM container the site scan actually saw on the page.

    Our records can be wrong or simply blank while the tag is live. Reporting
    a client as missing GTM when the scan found one on their homepage sends
    someone to install a second container — which then double-counts every
    event. The page is the authority here, not the record.
    """
    if not domain:
        return ""
    try:
        from modules.scans.app import latest_payload_for_domain
        payload = latest_payload_for_domain(domain) or {}
    except Exception:                                   # noqa: BLE001
        return ""
    for ns in ("google_tag_manager", "tag_manager", "analytics"):
        sec = payload.get(ns)
        if not isinstance(sec, dict):
            continue
        for key in ("container_id", "gtm_id", "gtm_container", "id"):
            val = str(sec.get(key) or "").strip()
            if val.upper().startswith("GTM-"):
                return val
    blob = json.dumps(payload)[:400000]
    m = re.search(r"GTM-[A-Z0-9]{4,10}", blob)
    return m.group(0) if m else ""


def no_gtm() -> dict:
    groups = _client_groups()
    priority, suggested = [], []
    found_on_site = 0
    for name in sorted(groups, key=str.lower):
        g = groups[name]
        # Only clients running something in the last 60 days. A tag on a site
        # we aren't driving traffic to isn't work worth chasing.
        if not _active_within(g, 60):
            continue
        cov = _google_coverage(name, g)
        if cov["has_gtm"]:
            continue
        scan_gtm = _gtm_from_scan(cov.get("domain") or "")
        if scan_gtm:
            # It IS installed — our record just doesn't know. Show it rather
            # than listing them as missing.
            found_on_site += 1
            cov["scan_gtm"] = scan_gtm
        active_products = {str(r.get("product") or "").lower() for r in g["rows"]
                           if knack_data.is_running(r) or r.get("thisM")}
        is_priority = any(any(k in p for k in GTM_PRIORITY_KEYWORDS) for p in active_products)
        row = [
            _c360_link(name),
            _join(g["partners"]),
            ", ".join(sorted({str(r.get("product") or "") for r in g["live"]}))[:100] or "—",
            _money(g["live_total"] or g["this_total"]),
            ({"pill": "ok", "text": f"GTM Found · {cov['scan_gtm']}",
              "title": "The site scan saw this container on the page — our "
                       "record just doesn't have it. Copy it onto the website "
                       "record rather than installing a second one."}
             if cov.get("scan_gtm") else
             {"search_attach": name, "kind": "gtm", "q": cov["domain"] or name}),
        ]
        (priority if is_priority else suggested).append(row)
    rows, styles = [], []
    if priority:
        rows.append([f"Running display / audio / SEO / paid search / retargeting — needs GTM ({len(priority)})",
                     "", "", "", ""])
        styles.append("sub")
        for r in priority:
            rows.append(r)
            styles.append(None)
    if suggested:
        rows.append([f"Suggested clients for GTM ({len(suggested)})", "", "", "", ""])
        styles.append("sub")
        for r in suggested:
            rows.append(r)
            styles.append(None)
    return {
        "columns": ["Client", "Partner", "Live products", "Monthly", "GTM container"],
        "rows": rows,
        "row_styles": styles,
        "note": (f"{len(priority) + len(suggested)} active clients with no GTM container on file. "
                 "The first group runs tag-dependent products (display, audio, SEO, paid "
                 "search, retargeting); the rest are suggested candidates. Attaching a "
                 "container saves it to the client universally and removes them here."),
    }


# ------------------------------------------- GHL: Accounting Requests
GHL_BASE = "https://services.leadconnectorhq.com"
_ghl_cache: dict = {}


def _ghl(path: str, params=None, method: str = "GET", body=None):
    import requests as _rq
    # Smart 1 Marketing lookups use their own sub-account token when provided.
    token = (os.environ.get("SMART1SUITE_PRIVATE_TOKEN", "").strip()
             or os.environ.get("GHL_PRIVATE_TOKEN", ""))
    if not token:
        raise RuntimeError("SMART1SUITE_PRIVATE_TOKEN / GHL_PRIVATE_TOKEN is not configured.")
    headers = {"Authorization": f"Bearer {token}",
               "Version": os.environ.get("GHL_API_VERSION", "2021-07-28"),
               "Accept": "application/json", "Content-Type": "application/json"}
    r = _rq.request(method, GHL_BASE + path, params=params, json=body,
                    headers=headers, timeout=20)
    if not r.ok:
        raise RuntimeError(f"GHL {method} {path} failed (HTTP {r.status_code}): {r.text[:180]}")
    return r.json() if r.text else {}


def _accounting_location() -> tuple[str, str]:
    """(location_id, name) of the Smart 1 Marketing sub-account.
    SUITE_COMPANY_ID pins it directly (preferred); falls back to
    GHL_ACCOUNTING_LOCATION_ID, then a name search."""
    override = (os.environ.get("SUITE_COMPANY_ID", "").strip()
                or os.environ.get("GHL_ACCOUNTING_LOCATION_ID", "").strip())
    if override:
        return override, "Smart 1 Marketing"
    if "acct_loc" in _ghl_cache:
        return _ghl_cache["acct_loc"]
    data = _ghl("/locations/search", {
        "companyId": os.environ.get("GHL_COMPANY_ID", ""), "limit": "500"})
    locs = data.get("locations") or []
    hit = next((l for l in locs
                if "smart 1 marketing" in str(l.get("name", "")).lower()), None)
    if not hit:
        raise RuntimeError('No GHL sub-account named "Smart 1 Marketing" found — '
                           "set GHL_ACCOUNTING_LOCATION_ID to pin the location.")
    _ghl_cache["acct_loc"] = (hit.get("id") or hit.get("_id"), hit.get("name"))
    return _ghl_cache["acct_loc"]


def _accounting_pipeline(location_id: str) -> dict:
    key = "acct_pipe:" + location_id
    if key in _ghl_cache:
        return _ghl_cache[key]
    data = _ghl("/opportunities/pipelines", {"locationId": location_id})
    pipes = data.get("pipelines") or []
    hit = next((p for p in pipes
                if "accounting request" in str(p.get("name", "")).lower()), None)
    if not hit:
        names = ", ".join(str(p.get("name")) for p in pipes) or "none"
        raise RuntimeError(f'No "Accounting Requests" pipeline in that location '
                           f"(found: {names}).")
    _ghl_cache[key] = hit
    return hit


def _ghl_custom_value(o: dict, *needles) -> str:
    """Pull a custom-field value off an opportunity (or its contact) whose
    id / key / name contains one of the needles."""
    sources = [o.get("customFields"), (o.get("contact") or {}).get("customFields"),
               o.get("customField")]
    for needle in needles:
        for src in sources:
            if not isinstance(src, list):
                continue
            for f in src:
                if not isinstance(f, dict):
                    continue
                key = " ".join(str(f.get(k) or "") for k in
                               ("id", "key", "fieldKey", "name", "fieldName")).lower()
                if needle in key:
                    v = (f.get("fieldValue") if f.get("fieldValue") is not None
                         else f.get("value") if f.get("value") is not None
                         else f.get("field_value"))
                    if isinstance(v, list):
                        return ", ".join(str(x) for x in v)
                    if isinstance(v, dict):
                        return ", ".join(str(x) for x in v.values())
                    if v is not None and str(v).strip():
                        return str(v)
    return ""


def _mmddyy(iso: str) -> str:
    s = str(iso or "")[:10]
    try:
        return _dates.fmt(s)
    except ValueError:
        return s


GHL_STATUSES = ("open", "won", "lost", "abandoned")


def accounting_requests() -> dict:
    columns = ["Request", "Company", "Detail", "Created", "Status", "Stage"]
    try:
        loc_id, loc_name = _accounting_location()
        pipe = _accounting_pipeline(loc_id)
    except RuntimeError as exc:
        # "error", not "note": a failed API call must never render as the
        # green "Nothing to report — all clear" empty state. An audit that
        # cannot reach its data source has found nothing BECAUSE it failed,
        # which is the opposite of a clean bill of health.
        return {"columns": columns, "rows": [], "error": str(exc)}
    stages = [{"id": s.get("id"), "name": s.get("name")}
              for s in (pipe.get("stages") or [])]
    stage_names = {s["id"]: s["name"] for s in stages}
    try:
        data = _ghl("/opportunities/search", {
            "location_id": loc_id, "pipeline_id": pipe.get("id"),
            "limit": 100})
    except RuntimeError as exc:
        # "error", not "note": a failed API call must never render as the
        # green "Nothing to report — all clear" empty state. An audit that
        # cannot reach its data source has found nothing BECAUSE it failed,
        # which is the opposite of a clean bill of health.
        return {"columns": columns, "rows": [], "error": str(exc)}
    opps = data.get("opportunities") or []
    rows = []
    for o in opps:
        contact = (o.get("contact") or {})
        organization = (_ghl_custom_value(o, "organization")
                        or contact.get("companyName") or o.get("name") or "(unnamed)")
        # The issue was read from one hardcoded custom-field id, which returns
        # nothing when the field is renamed or a different form is used — so
        # every row showed "—". Try the id first, then the field's own name,
        # so a rename doesn't silently empty the column.
        issue = (_ghl_custom_value(o, "29zlj", "checkbox")
                 or _ghl_custom_value(o, "issue", "reason", "request type",
                                      "what do you need", "problem")
                 or "")
        # The issue IS the request — "Buckeye Lake Winery" tells you who asked,
        # not what for, and a list of client names is not a work queue.
        request = issue.strip() or (o.get("name") or "").strip() or "(no issue given)"
        rows.append([
            {"text": request,
             "href": f"{os.environ.get('GHL_APP_BASE', 'https://app.gohighlevel.com')}"
                     f"/v2/location/{loc_id}/opportunities/list"},
            organization,
            # The request line is a one-liner; the form behind it holds the
            # detail. Rather than widening every row for the few people who
            # need it, put it behind a button.
            {"actions": [{"label": "Summary", "action": "form-summary",
                          "client": str(o.get("id") or "")}]},
            _mmddyy(o.get("createdAt")),
            {"status_select": o.get("id"),
             "current": str(o.get("status") or "open").lower()},
            {"stage_select": o.get("id"),
             "current": o.get("pipelineStageId"),
             "current_name": stage_names.get(o.get("pipelineStageId"), "")},
        ])
    return {
        "columns": columns,
        "rows": rows,
        "stages": stages,
        "statuses": list(GHL_STATUSES),
        "pipeline_id": pipe.get("id"),
        "note": (f"{len(rows)} requests in the \"{pipe.get('name')}\" pipeline "
                 f"({loc_name}). Status and stage are both editable right here — "
                 "changes update GHL immediately."),
    }


def set_accounting_stage(opp_id: str, stage_id: str = "", status: str = "") -> None:
    loc_id, _ = _accounting_location()
    pipe = _accounting_pipeline(loc_id)
    body = {}
    if stage_id:
        body = {"pipelineId": pipe.get("id"), "pipelineStageId": stage_id}
    elif status:
        body = {"status": status}
    try:
        _ghl(f"/opportunities/{opp_id}/status", method="PUT", body=body)
    except RuntimeError:
        _ghl(f"/opportunities/{opp_id}", method="PUT", body=body)


# ------------------------------------------- GHL: Smart 1 Suite SaaS billing
#
# Agency-level "SaaS Configurator" endpoints — distinct from the /opportunities
# calls above. Needs GHL_PRIVATE_TOKEN to be an Agency-level Private
# Integration Token with the SaaS Configurator (Agency-Access) scope enabled,
# plus GHL_COMPANY_ID. Reuses the same two env vars the Suite control panel
# already requires for /locations/search — no new config if that already
# works. Source: GoHighLevel's public OpenAPI spec (saas-v3.json / Agency-
# Access security), current as of Aug 2026. NOT YET RUN AGAINST LIVE DATA —
# the raw shapes below (subscriptionInfo, plan prices) are documented but the
# actual account has never called them from this app. Verify the first live
# run: open System Status, or run one report and read the note if it errors.
GHL_SAAS_VERSION = "v3"


def _ghl_saas(path: str, params=None):
    import requests as _rq
    token = os.environ.get("GHL_PRIVATE_TOKEN", "")
    if not token:
        raise RuntimeError("GHL_PRIVATE_TOKEN is not configured.")
    headers = {"Authorization": f"Bearer {token}", "Version": GHL_SAAS_VERSION,
               "Accept": "application/json"}
    r = _rq.get(GHL_BASE + path, params=params, headers=headers, timeout=20)
    if not r.ok:
        raise RuntimeError(
            f"GHL SaaS {path} failed (HTTP {r.status_code}): {r.text[:200]}. "
            "If this is a 401/403, the Private Integration Token most likely "
            "needs the SaaS Configurator scope added — it's a separate scope "
            "from the one that powers Locations/Opportunities.")
    return r.json() if r.text else {}


def _ghl_saas_locations() -> list:
    """Every sub-account GHL has ever put into Smart 1 Suite's SaaS mode,
    each with its subscriptionInfo (status, plan, Stripe ids), paginated."""
    company = os.environ.get("GHL_COMPANY_ID", "")
    if not company:
        raise RuntimeError("GHL_COMPANY_ID is not configured.")
    out, page = [], 1
    while True:
        data = _ghl_saas(f"/saas/saas-locations/{company}", {"page": page})
        locs = data.get("locations") if isinstance(data, dict) else data
        locs = locs or []
        out.extend(locs)
        pg = (data.get("pagination") or {}) if isinstance(data, dict) else {}
        if not locs or not pg.get("hasNext"):
            break
        page += 1
        if page > 50:            # sane ceiling — a real runaway would mean a bug
            break
    return out


def _ghl_agency_plans() -> dict:
    """{saasPlanId: plan dict} — turns a location's saasPlanId into a plan
    title and its active monthly price."""
    company = os.environ.get("GHL_COMPANY_ID", "")
    data = _ghl_saas(f"/saas/agency-plans/{company}")
    plans = data if isinstance(data, list) else (data.get("plans") or [])
    return {p.get("planId"): p for p in plans if isinstance(p, dict) and p.get("planId")}


def _plan_monthly_price(plan: dict) -> float:
    for pr in (plan or {}).get("prices") or []:
        if pr.get("billingInterval") == "month" and pr.get("active", True):
            return _num(pr.get("amount"))
    return 0.0


# Subscription statuses that count as "billing" for these two reports.
# GHL/Stripe statuses seen in the wild: active, trialing, past_due, paused,
# canceled, incomplete, incomplete_expired, unpaid. "past_due" is included —
# they're still an active subscription that hasn't failed yet; "paused" and
# "canceled" are not.
_ACTIVE_SUB_STATUSES = {"active", "trialing", "past_due"}


def _ghl_billing_rows() -> list:
    """One row per GHL sub-account that has ever been in SaaS mode, resolved
    to a plan name/price and matched to its Smart 1 client record in Knack
    (by normalized business name — same fuzzy match used in invoice_off())."""
    locations = _ghl_saas_locations()
    try:
        plans = _ghl_agency_plans()
    except RuntimeError:
        plans = {}

    groups = _client_groups()
    knack_by_norm = {}
    for name, g in groups.items():
        n = _norm_name(name)
        if n:
            knack_by_norm.setdefault(n, (name, g))

    rows = []
    for loc in locations:
        info = loc.get("subscriptionInfo") or {}
        status = str(info.get("subscriptionStatus")
                     or loc.get("subscriptionStatus") or "").strip().lower()
        plan_id = info.get("saasPlanId") or loc.get("saasPlanId")
        plan = plans.get(plan_id) or {}
        raw_name = str(loc.get("name") or loc.get("locationId") or "").strip()
        n = _norm_name(raw_name)
        hit = knack_by_norm.get(n)
        if not hit:
            hit = next(((kn, kg) for norm, (kn, kg) in knack_by_norm.items()
                        if norm and n and (norm in n or n in norm)), None)
        rows.append({
            "location_id": loc.get("locationId"),
            "raw_name": raw_name or "(unnamed sub-account)",
            "status": status,
            "plan_title": plan.get("title") or plan_id or "—",
            "monthly": _plan_monthly_price(plan),
            "knack_name": hit[0] if hit else None,
            "knack_group": hit[1] if hit else None,
        })
    return rows


def ghl_billing_no_products() -> dict:
    """Report: Smart 1 Suite sub-accounts with active billing but nothing
    live on the marketing side — a pure-software client, or a mismatch
    between what GHL is charging and what Knack has on file."""
    columns = ["Client", "GHL sub-account", "Plan", "Monthly", "Billing status",
               "Matched in Knack"]
    try:
        rows_raw = _ghl_billing_rows()
    except RuntimeError as exc:
        # "error", not "note": a failed API call must never render as the
        # green "Nothing to report — all clear" empty state. An audit that
        # cannot reach its data source has found nothing BECAUSE it failed,
        # which is the opposite of a clean bill of health.
        return {"columns": columns, "rows": [], "error": str(exc)}
    rows = []
    total = 0.0
    for r in rows_raw:
        if r["status"] not in _ACTIVE_SUB_STATUSES:
            continue
        if r["knack_group"] and _is_active(r["knack_group"]):
            continue          # has a live Smart 1 marketing product — not this report
        client_cell = _c360_link(r["knack_name"]) if r["knack_name"] else r["raw_name"]
        rows.append((str(r["raw_name"]).lower(), [
            client_cell, r["raw_name"], r["plan_title"], _money(r["monthly"]),
            r["status"].replace("_", " ").title(),
            "Yes" if r["knack_name"] else "No match in Knack",
        ]))
        total += r["monthly"]
    rows.sort(key=lambda t: t[0])
    return {
        "columns": columns,
        "rows": [r for _, r in rows],
        "note": (f"{len(rows)} Smart 1 Suite sub-accounts billing (active, trialing "
                 f"or past-due) with no live Smart 1 marketing product on file in "
                 f"Knack — {_money(total)}/mo of Suite-only billing. \"No match in "
                 "Knack\" means the sub-account name couldn't be matched to any "
                 "Smart 1 client record at all, so double-check those by hand."),
    }


def ghl_billing_this_month() -> dict:
    """Report: every Smart 1 Suite sub-account with active billing right now,
    simplified to client / plan / monthly price / status — no Stripe or
    customer IDs, biggest bill first."""
    columns = ["Client", "GHL sub-account", "Plan", "Monthly", "Billing status"]
    try:
        rows_raw = _ghl_billing_rows()
    except RuntimeError as exc:
        # "error", not "note": a failed API call must never render as the
        # green "Nothing to report — all clear" empty state. An audit that
        # cannot reach its data source has found nothing BECAUSE it failed,
        # which is the opposite of a clean bill of health.
        return {"columns": columns, "rows": [], "error": str(exc)}
    rows = []
    total = 0.0
    for r in rows_raw:
        if r["status"] not in _ACTIVE_SUB_STATUSES:
            continue
        client_cell = _c360_link(r["knack_name"]) if r["knack_name"] else r["raw_name"]
        rows.append((r["monthly"], [
            client_cell, r["raw_name"], r["plan_title"], _money(r["monthly"]),
            r["status"].replace("_", " ").title(),
        ]))
        total += r["monthly"]
    rows.sort(key=lambda t: -t[0])
    return {
        "columns": columns,
        "rows": [r for _, r in rows],
        "note": (f"{len(rows)} Smart 1 Suite sub-accounts with active, trialing or "
                 f"past-due billing this month — {_money(total)}/mo total. Plan and "
                 "price come from the agency's SaaS Configurator plans."),
    }


# ---------------------------------------- invoice-off partner assignments
def _assign_path() -> str:
    return os.path.join(jsonstore.data_root(), "qa-invoice-partner.json")


def invoice_assignments() -> dict:
    data = jsonstore.read_json(_assign_path(), default={})
    return data if isinstance(data, dict) else {}


def assign_invoice_partner(customer: str, partner: str):
    data = invoice_assignments()
    data[str(customer)] = str(partner)
    jsonstore.write_json(_assign_path(), data, indent=1)


def partner_list() -> list[str]:
    return sorted({str(r.get("partner", "")).strip()
                   for r in knack_data.products() if str(r.get("partner", "")).strip()},
                  key=str.lower)


# ------------------------------------------------ QuickBooks-backed reports
def _qb_state():
    from . import quickbooks as qb
    if not qb.configured():
        return qb, ("QuickBooks isn't configured — set QB_CLIENT_ID / "
                    "QB_CLIENT_SECRET and connect from System Status.")
    if not qb.connected():
        return qb, ("QuickBooks isn't connected yet — use Connect QuickBooks "
                    "on the System Status page, then re-run this report.")
    return qb, None


def _month_keys(months: int = 4) -> list[str]:
    """['YYYY-MM' this month, last, 2 prior, 3 prior]"""
    first = _dt.date.today().replace(day=1)
    keys = []
    for _ in range(months):
        keys.append(first.strftime("%Y-%m"))
        first = (first - _dt.timedelta(days=1)).replace(day=1)
    return keys


def _month_label(ym: str) -> str:
    y, m = ym.split("-")
    months = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    return f"{months[int(m)]} {y}"


def billing_comparison() -> dict:
    qb, err = _qb_state()
    keys = _month_keys(4)                      # [this, last, prior2, prior3]
    columns = ["Customer", _month_label(keys[3]), _month_label(keys[2]),
               _month_label(keys[1]), _month_label(keys[0]),
               "Change vs last month"]
    if err:
        return {"columns": columns, "rows": [], "note": err, "needs_qb": True}
    data = qb.monthly_totals_by_customer(4)
    decreases, increases = [], []
    for name in sorted(data, key=str.lower):
        rec = data[name]
        vals = [rec["months"].get(k, 0.0) for k in keys]  # this..prior3
        this, last = vals[0], vals[1]
        delta = round(this - last, 2)
        if abs(delta) < 0.5:
            continue                            # unchanged — not listed
        cust_cell = ({"text": name, "href": qb.customer_link(rec["id"])}
                     if rec.get("id") else name)
        row = [cust_cell, _money(vals[3]), _money(vals[2]),
               _money(vals[1]), _money(vals[0]),
               ("▼ " if delta < 0 else "▲ ") + _money(abs(delta))]
        (decreases if delta < 0 else increases).append((abs(delta), row))
    decreases.sort(key=lambda t: -t[0])
    increases.sort(key=lambda t: -t[0])
    rows = [r for _, r in decreases] + [r for _, r in increases]

    # ---- "Compare invoices" narratives: each month vs the current month ----
    comparisons = []
    cur_key = keys[0]
    for prior in keys[1:]:
        cur_total = prior_total = 0.0
        inc, dec, started, stopped = [], [], [], []
        for name, rec in data.items():
            c = rec["months"].get(cur_key, 0.0)
            p = rec["months"].get(prior, 0.0)
            cur_total += c
            prior_total += p
            d = round(c - p, 2)
            if p and not c:
                stopped.append((p, name))
            elif c and not p:
                started.append((c, name))
            elif d > 0.5:
                inc.append((d, name))
            elif d < -0.5:
                dec.append((-d, name))
        inc.sort(reverse=True)
        dec.sort(reverse=True)
        started.sort(reverse=True)
        stopped.sort(reverse=True)
        net = round(cur_total - prior_total, 2)
        parts = [f"Total invoiced: {_money(cur_total)} this month vs "
                 f"{_money(prior_total)} in {_month_label(prior)} "
                 f"({'+' if net >= 0 else '−'}{_money(abs(net))} net)."]
        if dec:
            parts.append(f"{len(dec)} customer(s) are billing less now — biggest: "
                         + ", ".join(f"{n} (−{_money(v)})" for v, n in dec[:3]) + ".")
        if inc:
            parts.append(f"{len(inc)} customer(s) are billing more — biggest: "
                         + ", ".join(f"{n} (+{_money(v)})" for v, n in inc[:3]) + ".")
        if stopped:
            parts.append(f"{len(stopped)} billed in {_month_label(prior)} but have no "
                         "invoice this month: "
                         + ", ".join(f"{n} ({_money(v)})" for v, n in stopped[:3])
                         + ("…" if len(stopped) > 3 else "") + ".")
        if started:
            parts.append(f"{len(started)} are new since {_month_label(prior)}: "
                         + ", ".join(f"{n} (+{_money(v)})" for v, n in started[:3])
                         + ("…" if len(started) > 3 else "") + ".")
        if len(parts) == 1:
            parts.append("No customer-level changes beyond rounding.")
        comparisons.append({"month": _month_label(prior),
                            "vs": _month_label(cur_key),
                            "text": " ".join(parts)})

    return {
        "columns": columns,
        "rows": rows,
        "invoice_comparison": comparisons,
        "note": (f"{len(decreases)} customers invoiced less this month than last, "
                 f"{len(increases)} invoiced more (decreases listed first, biggest "
                 "swing at the top). Totals are summed QuickBooks invoices per "
                 "calendar month; customer names link into QuickBooks."),
    }


def invoice_off() -> dict:
    qb, err = _qb_state()
    columns = ["Customer", "Invoiced this month", "Active products / mo",
               "Difference", "Live products", "Partner"]
    if err:
        return {"columns": columns, "rows": [], "note": err, "needs_qb": True}
    assigned = invoice_assignments()
    data = qb.monthly_totals_by_customer(2)     # this + last month is plenty
    this_key = _month_keys(1)[0]

    groups = _client_groups()
    knack_by_norm: dict[str, tuple[str, dict]] = {}
    for name, g in groups.items():
        n = _norm_name(name)
        if n:
            knack_by_norm.setdefault(n, (name, g))

    rows = []
    matched_norms = set()
    for cust in sorted(data, key=str.lower):
        if cust in assigned:            # resolved to a partner — never show again
            continue
        rec = data[cust]
        invoiced = rec["months"].get(this_key, 0.0)
        n = _norm_name(cust)
        hit = knack_by_norm.get(n)
        if not hit:      # try containment both ways for near-matches
            hit = next(((kn, kg) for norm, (kn, kg) in knack_by_norm.items()
                        if norm and n and (norm in n or n in norm)), None)
        if not hit:
            continue     # QB customer with no Smart 1 Team client — skip
        kname, g = hit
        matched_norms.add(_norm_name(kname))
        expected = g["live_total"]
        diff = round(invoiced - expected, 2)
        if abs(diff) < 0.5:
            continue
        cust_cell = ({"text": cust, "href": qb.customer_link(rec["id"])}
                     if rec.get("id") else cust)
        rows.append((abs(diff), [
            cust_cell,
            _money(invoiced),
            _money(expected),
            ("▼ " if diff < 0 else "▲ ") + _money(abs(diff)),
            len(g["live"]),
            {"assign": cust},
        ]))
    # active Knack clients with NO invoice at all this month
    for name, g in groups.items():
        if not _is_active(g) or g["live_total"] < 0.5:
            continue
        if name in assigned:
            continue
        if _norm_name(name) in matched_norms:
            continue
        n = _norm_name(name)
        in_qb = any(n and (_norm_name(c) == n or n in _norm_name(c) or _norm_name(c) in n)
                    for c in data)
        if in_qb:
            continue
        rows.append((g["live_total"], [
            _c360_link(name), _money(0), _money(g["live_total"]),
            "▼ " + _money(g["live_total"]) + " (no invoice found)",
            len(g["live"]),
            {"assign": name},
        ]))
    rows.sort(key=lambda t: -t[0])
    return {
        "columns": columns,
        "rows": [r for _, r in rows],
        "partners": partner_list(),
        "note": (f"{len(rows)} customers whose QuickBooks invoices this month "
                 "don't match their active-product monthly total (matched by "
                 "business name; biggest gap first). ▼ = invoiced less than "
                 "active products, ▲ = invoiced more. Use \"Add to partner\" to "
                 "mark a record as handled by a partner — it's remembered and "
                 "won't show here again (nothing changes anywhere else)."),
    }


# ------------------------------------------------------------------ registry
def uploads_not_in_suite() -> dict:
    """Galleries holding client files that never reached Smart 1 Suite.

    A client who has gone to the trouble of sending their photos has done the
    hard part. If those files then sit in our gallery and never reach their
    Suite media library, the work is invisible at the moment someone builds
    their page — and nobody finds out until they go looking for an image that
    "should be there".

    Three different reasons land here and they need different actions, so the
    report says which rather than lumping them together:

      no Suite location   the gallery was made for a prospect, or nobody has
                          attached the location yet — expected for a prospect,
                          a gap for a live client
      sync is off         someone turned it off deliberately
      failed              it tried and Suite refused; the error is shown
    """
    try:
        from modules.image_picker.models import PickerClient, SavedImage, session
    except Exception as exc:                            # noqa: BLE001
        return {"columns": ["Client"], "rows": [],
                "note": f"Client Image Uploads isn't available here ({type(exc).__name__})."}

    try:
        db = session()
        rows_ = db.query(SavedImage, PickerClient).join(
            PickerClient, SavedImage.client_id == PickerClient.id).all()
    except Exception as exc:                            # noqa: BLE001
        return {"columns": ["Client"], "rows": [],
                "note": f"Couldn't read the uploads database ({type(exc).__name__})."}

    by_client: dict = {}
    for img, client in rows_:
        # Only files the client sent us. Stock we picked for them is a
        # different question and has its own answer in the gallery.
        if (img.collection_kind or "") != "upload":
            continue
        b = by_client.setdefault(client.id, {
            "client": client, "total": 0, "waiting": 0,
            "reasons": {}, "last": None, "last_error": "",
        })
        b["total"] += 1
        if img.created_at and (b["last"] is None or img.created_at > b["last"]):
            b["last"] = img.created_at
        if img.ghl_status == "sent":
            continue
        b["waiting"] += 1
        if img.ghl_status == "error":
            reason = "Failed"
            b["last_error"] = (img.ghl_error or "")[:120]
        elif not (client.ghl_location_id or "").strip():
            reason = "No Suite location"
        elif not client.ghl_enabled:
            reason = "Sync is off"
        else:
            reason = "Queued"
        b["reasons"][reason] = b["reasons"].get(reason, 0) + 1

    rows = []
    for b in by_client.values():
        if not b["waiting"]:
            continue
        c = b["client"]
        reason = ", ".join(f"{k} ({v})" for k, v in sorted(b["reasons"].items()))
        rows.append([
            _c360_link(c.name) if getattr(c, "kind", "") == "client" else c.name,
            "Client" if getattr(c, "kind", "") == "client" else "Prospect",
            b["waiting"],
            b["total"],
            reason,
            _dates.fmt(b["last"]),
            b["last_error"] or "",
        ])

    # Most files waiting first — that is the biggest pile of work sitting
    # invisible — then the most recently active gallery.
    rows.sort(key=lambda r: (-r[2], r[0] if isinstance(r[0], str) else ""))

    waiting = sum(r[2] for r in rows)
    return {
        "columns": ["Client", "Type", "Not in Suite", "Uploaded", "Why",
                    "Last upload", "Last error"],
        "rows": rows,
        "note": (f"{waiting} uploaded file(s) across {len(rows)} galler"
                 f"{'y' if len(rows) == 1 else 'ies'} have not reached Smart 1 "
                 f"Suite. A prospect with no Suite location is expected; a "
                 f"client with one is not."
                 if rows else
                 "Every uploaded file has reached Smart 1 Suite."),
    }


REPORTS = {
    "uploads-not-in-suite": {
        "title": "Uploads Not In Suite",
        "desc": "Client files uploaded to a gallery that never reached their Smart 1 Suite media library — with the reason for each.",
        "ico": "&#8593;",
        "fn": uploads_not_in_suite,
    },
    "active-clients": {
        "title": "Active Clients",
        "desc": "Every client with a live product or billing this month — partner, salesperson, live monthly and dashboard status.",
        "ico": "&#9679;",
        "fn": active_clients,
        "group": "Clients",
    },
    "no-dashboards": {
        "title": "No Dashboards",
        "desc": "Active clients with no Smart 1 Dashboard link on any live product — they can't see their reporting.",
        "ico": "&#9888;",
        "fn": no_dashboards,
        "group": "Clients",
    },
    "stale-90": {
        "title": "No Live Product in 90 Days",
        "desc": "Clients gone quiet — last IO ended 90+ days ago and nothing live now. Win-back candidates.",
        "ico": "&#8987;",
        "fn": stale_90,
        "group": "Clients",
    },
    "lost-by-partner": {
        "title": "Ran Last Month, Not This Month",
        "desc": "Clients that billed last month with nothing live this month — grouped by partner so you can see who's churning.",
        "ico": "&#8595;",
        "fn": lost_by_partner,
        "group": "Clients",
    },
    "no-analytics": {
        "title": "Clients Without Analytics",
        "desc": "Active clients with no Google Analytics on file — search connected accounts and attach the right property.",
        "ico": "&#128200;",
        "fn": no_analytics,
        "group": "Clients",
    },
    "no-gtm": {
        "title": "Clients Without GTM",
        "desc": "Active clients with no GTM container — split into tag-dependent products vs suggested candidates.",
        "ico": "&#127991;",
        "fn": no_gtm,
        "group": "Clients",
    },
    "sales-scorecard": {
        "title": "Salesperson Scorecard",
        "desc": "Active clients, live products and billing per salesperson, with month-over-month new / lost / increased / decreased.",
        "ico": "&#127942;",
        "fn": salesperson_scorecard,
        "group": "Scorecards",
    },
    "partner-scorecard": {
        "title": "Partner Scorecard",
        "desc": "The same scorecard rolled up by media partner — who's growing and who's shrinking.",
        "ico": "&#129309;",
        "fn": partner_scorecard,
        "group": "Scorecards",
    },
    "accounting-requests": {
        "title": "Accounting Requests",
        "desc": "Every request in the Accounting Requests pipeline (Smart 1 Marketing · GHL) — change stages right from the report.",
        "ico": "&#128203;",
        "fn": accounting_requests,
        "group": "Accounting",
    },
    "ghl-billing-no-products": {
        "title": "Suite Billing, No Active Product",
        "desc": "Smart 1 Suite sub-accounts with active GHL billing but no live Smart 1 marketing product on file in Knack.",
        "ico": "&#128681;",
        "fn": ghl_billing_no_products,
        "group": "Suite (GoHighLevel)",
    },
    "ghl-billing-this-month": {
        "title": "Suite Billing This Month",
        "desc": "Every Smart 1 Suite sub-account with active GHL billing this month — client, plan and monthly price, simplified.",
        "ico": "&#128179;",
        "fn": ghl_billing_this_month,
        "group": "Suite (GoHighLevel)",
    },
    "billing-comparison": {
        "title": "Customer Billing Comparison",
        "desc": "QuickBooks invoices per customer: this month vs the last three. Decreases listed first, biggest swings on top.",
        "ico": "&#128181;",
        "fn": billing_comparison,
        "group": "Billing (QuickBooks)",
    },
    "invoice-off": {
        "title": "Invoice Off Report",
        "desc": "Customers whose invoiced amount this month doesn't match their active-product monthly total.",
        "ico": "&#9878;",
        "fn": invoice_off,
        "group": "Billing (QuickBooks)",
    },
}


def run(key: str, month: str = "") -> dict:
    meta = REPORTS.get(key)
    if not meta:
        raise KeyError(key)
    if key in ("sales-scorecard", "partner-scorecard"):
        out = meta["fn"](month)
    else:
        out = meta["fn"]()
    out["key"] = key
    out["title"] = meta["title"]
    return out
