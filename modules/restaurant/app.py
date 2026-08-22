import io
import json
import os
import re
import threading
import time
import uuid
from collections import defaultdict, deque
from typing import Any

import requests
from dotenv import load_dotenv
from hub import jsonstore
from flask import Flask, jsonify, render_template, request


# reportlab is pure-Python (no system libraries) so the PDF builder deploys
# cleanly on Render's native Python runtime with no Docker/apt changes.
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    Image,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

load_dotenv()

app = Flask(__name__)

MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

# Standardized on GHL_WEBHOOK_URL (was SMART1_WEBHOOK_URL).
WEBHOOK_URL = os.getenv("GHL_WEBHOOK_URL", "").strip()

# Cloudinary stores the generated PDFs and (for shareable links) report JSON.
# The SDK reads CLOUDINARY_URL from the environment automatically.
CLOUDINARY_URL = os.getenv("CLOUDINARY_URL", "").strip()
REPORT_NAME = os.getenv("REPORT_NAME", "restaurant-market-report").strip().strip("/") or "restaurant-market-report"
REPORT_JSON_NAME = f"{REPORT_NAME}-json"
# "image" delivers PDFs inline in the browser (and enables thumbnails); "raw" is a
# safe fallback if your Cloudinary account restricts PDF delivery for image assets.
# Default "raw", not "image". Cloudinary accepts a PDF as an image asset and
# then refuses to deliver it unless the account has PDF delivery switched on —
# the upload succeeds and the link 403s when a diner clicks Download, so the
# failure only shows up in front of a customer. Every other landing page here
# uses raw; this defaulted the other way. "image" stays available for anyone
# who wants Cloudinary's PDF transformations and has enabled delivery.
PDF_RESOURCE_TYPE = os.getenv("PDF_RESOURCE_TYPE", "raw").strip().lower()
if PDF_RESOURCE_TYPE not in ("image", "raw"):
    PDF_RESOURCE_TYPE = "raw"
# Optional logo drawn at the top of the PDF (URL to a PNG/JPG on a dark-safe background).
PDF_LOGO_URL = os.getenv("PDF_LOGO_URL", "").strip()

# Absolute base for building shareable report links and the fallback PDF URL. If empty,
# the app derives it from the incoming request.
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
ENABLE_PDF = os.getenv("ENABLE_PDF", "1").strip() not in ("0", "false", "False", "")
# The generated PDF stays here: it is served straight off /static/reports/ and
# is rebuilt from the report JSON anyway, so it is a genuine cache.
REPORT_DIR = os.path.join(app.static_folder, "reports")

# The report JSON does not. It is what /r/<id> renders for a restaurant we
# handed a link to, and inside static/ it was thrown away by every deploy —
# the local fallback for "Cloudinary isn't configured" had nothing behind it.
# On the persistent disk and mirrored into the database, the link survives both.
REPORT_JSON_DIR = jsonstore.data_dir("restaurant-reports")

# CORS — the tool is embedded same-origin via iframe, but ALLOWED_ORIGINS is honored
# so the API can be called from an explicit allow-list if ever needed.
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*").strip()

# Basic abuse / cost protection.
HONEYPOT_FIELD = "company_website"          # hidden in the form; humans leave it blank
MIN_FILL_SECONDS = int(os.getenv("MIN_FILL_SECONDS", "3"))
RATE_LIMIT_MAX = int(os.getenv("RATE_LIMIT_MAX", "12"))
RATE_LIMIT_WINDOW = int(os.getenv("RATE_LIMIT_WINDOW_SEC", "60"))
# Separate buckets so partial-lead capture can never exhaust the budget that
# protects the expensive AI endpoint (and vice versa).
_rate_buckets: dict = {"ai": defaultdict(deque), "lead": defaultdict(deque)}
_rate_last_prune: dict = {"ai": 0.0, "lead": 0.0}
_rate_lock = threading.Lock()

# Single source of truth for the package menu. The AI prompt and the front-end grid
# both derive from this, so prices never drift between the two.
PACKAGES = [
    {"name": "Corner Table", "price": "$1,500/month",
     "blurb": "Entry trigger-driven presence for a single location or small local market."},
    {"name": "Local Favorite", "price": "$3,000/month",
     "blurb": "Balanced coverage across lunch, dinner, and delivery dayparts."},
    {"name": "Regional Draw", "price": "$5,500/month",
     "blurb": "Heavier push across a competitive market or multiple locations."},
    {"name": "Market Leader", "price": "$8,500/month",
     "blurb": "Maximum share of voice across a large or highly competitive metro."},
]
PACKAGE_MENU_STR = "\n".join(f"    * {p['price']} — {p['name']}" for p in PACKAGES)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

SYSTEM_PROMPT = """
You are the Smart 1 Marketing Restaurant Market Intelligence Architect.
Create a practical, sales-oriented market and geofencing report for a restaurant, bar, cafe, or foodservice business.

IMPORTANT ACCURACY RULES
- You do not have live access to maps, POS/foot-traffic databases, or exact census tables unless supplied in the request.
- Use geographic knowledge and conservative planning assumptions. Never claim a location or statistic was live-verified.
- Clearly label all population, household, frequent-diner, and traffic figures as AI planning estimates.
- Give ranges, confidence levels, and a short explanation of the assumptions.
- Do not invent precise street addresses. Use recognizable place names plus city/state. An address field may be null.
- Prefer real, well-known nearby businesses, venues, and landmarks you are reasonably confident exist. If uncertain, lower confidence.
- Avoid duplicate locations and avoid recommending polygons that cannot be practically geofenced. Favor specific points of interest.

TARGET TYPES TO CONSIDER
1. Office parks, business districts, and large employers (lunch and happy-hour daypart traffic)
2. Apartment, condo, and dense residential communities (dinner and delivery daypart traffic)
3. Hotels and short-term lodging clusters (tourist and business-traveler traffic)
4. Colleges, universities, and large high schools (value-driven, late-night, and delivery traffic)
5. Gyms, fitness studios, and grocery/health-food retail (health-conscious or post-workout traffic)
6. Shopping centers, movie theaters, and entertainment districts (pre/post-activity dining)
7. Stadiums, arenas, concert venues, and convention centers (event-driven surge traffic)
8. Competing restaurants and comparable concepts for conquesting
9. Tourist attractions, downtown/main-street corridors, and commuter corridors

FREQUENT-DINER ESTIMATION METHOD
Estimate the adult population and households in the requested market, then estimate the number of likely "frequent diner" households (households that eat out or order delivery multiple times per month) using a market-sensitive dine-out rate. Use lower rates for lower-density, lower-income areas and higher rates for dense urban, high-income, high-employment-density, or heavy-tourism areas. Adjust for daytime employment population, nearby residential density, income, competitive saturation, and the restaurant's category (QSR/fast-casual skews toward frequency; fine dining skews toward occasion-based visits).
Return low/base/high frequent-diner household estimates. Do not present the estimate as verified POS or loyalty data.

GEOFENCE GUIDANCE
- Recommend a practical radius or polygon approach for each location.
- Typical point-of-interest radii: 0.10-0.20 mile for compact venues (offices, competitors, cafes), 0.20-0.40 mile for larger venues (shopping centers, hotels, campuses), and polygons for stadiums/arenas/large campuses.
- Separate "location lookback" sites from "real-time/proximity" sites.
- Rank locations Priority 1, 2, or 3.

MEDIA AND TRIGGER RULES
- The campaign is trigger-driven around dayparts, weather, and local events rather than static always-on spend.
- ALLOWED channels ONLY: geofencing, location look-back retargeting, programmatic / data-driven targeted display, CTV/OTT, streaming audio, YouTube/online video, and website retargeting.
- NEVER recommend social media or social advertising (Facebook, Instagram, TikTok, LinkedIn, Snapchat, Pinterest, X, or any other social channel).
- NEVER recommend paid search, email, or SMS. Do not mention them anywhere in the report.
- Build practical triggers around dayparts, weather, and local demand signals, such as lunch rush windows, evening/dinner windows, extreme heat, rain/cold (comfort food and delivery), first snow, home sports team game days, local event nights, and holiday weekends.
- Keep trigger labels short and punchy (e.g. "Lunch rush 11-1", "90°+ heat", "Rainy day", "Home game night", "Holiday weekend").
- Do not imply that a trigger guarantees demand. Treat it as a budget-pacing and timing signal.

OUTPUT
Return only valid JSON matching the requested schema. Do not use markdown fences.
"""

REPORT_SCHEMA = {
    "name": "restaurant_report",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "market_summary": {"type": "string"},
            "market_type": {"type": "string"},
            "market_type_description": {"type": "string"},
            "market_opportunity": {"type": "string"},
            "market_profile": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "estimated_population_low": {"type": "integer"},
                    "estimated_population_base": {"type": "integer"},
                    "estimated_population_high": {"type": "integer"},
                    "estimated_households_low": {"type": "integer"},
                    "estimated_households_base": {"type": "integer"},
                    "estimated_households_high": {"type": "integer"},
                    "estimated_frequent_diner_households_low": {"type": "integer"},
                    "estimated_frequent_diner_households_base": {"type": "integer"},
                    "estimated_frequent_diner_households_high": {"type": "integer"},
                    "estimated_dine_out_rate_low": {"type": "number"},
                    "estimated_dine_out_rate_base": {"type": "number"},
                    "estimated_dine_out_rate_high": {"type": "number"},
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                    "assumptions": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "estimated_population_low",
                    "estimated_population_base",
                    "estimated_population_high",
                    "estimated_households_low",
                    "estimated_households_base",
                    "estimated_households_high",
                    "estimated_frequent_diner_households_low",
                    "estimated_frequent_diner_households_base",
                    "estimated_frequent_diner_households_high",
                    "estimated_dine_out_rate_low",
                    "estimated_dine_out_rate_base",
                    "estimated_dine_out_rate_high",
                    "confidence",
                    "assumptions",
                ],
            },
            "recommended_package": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "package_name": {"type": "string"},
                    "monthly_investment": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["package_name", "monthly_investment", "description"],
            },
            "media_channels": {"type": "array", "items": {"type": "string"}},
            "streaming_audio_note": {"type": "string"},
            "demand_triggers": {"type": "array", "items": {"type": "string"}},
            "monthly_plan": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "month": {"type": "string"},
                        "focus": {"type": "string"},
                        "message": {"type": "string"},
                        "triggers": {"type": "array", "items": {"type": "string"}},
                        "pacing": {"type": "string"},
                    },
                    "required": ["month", "focus", "message", "triggers", "pacing"],
                },
            },
            "geofence_locations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "name": {"type": "string"},
                        "city_state": {"type": "string"},
                        "address": {"type": ["string", "null"]},
                        "category": {"type": "string"},
                        "district_or_market": {"type": "string"},
                        "priority": {"type": "integer", "enum": [1, 2, 3]},
                        "recommended_method": {
                            "type": "string",
                            "enum": ["location_lookback", "real_time_proximity", "both"],
                        },
                        "recommended_radius_miles": {"type": "number"},
                        "audience_reason": {"type": "string"},
                        "best_message": {"type": "string"},
                        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                    },
                    "required": [
                        "name",
                        "city_state",
                        "address",
                        "category",
                        "district_or_market",
                        "priority",
                        "recommended_method",
                        "recommended_radius_miles",
                        "audience_reason",
                        "best_message",
                        "confidence",
                    ],
                },
            },
            "disclaimer": {"type": "string"},
        },
        "required": [
            "market_summary",
            "market_type",
            "market_type_description",
            "market_opportunity",
            "market_profile",
            "recommended_package",
            "media_channels",
            "streaming_audio_note",
            "demand_triggers",
            "monthly_plan",
            "geofence_locations",
            "disclaimer",
        ],
    },
    "strict": True,
}


# ---------------------------------------------------------------------------
# Request helpers: client IP, rate limiting, honeypot, validation
# ---------------------------------------------------------------------------

def _client_ip() -> str:
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr or "unknown"


def _prune_rate_hits(now: float, bucket: str) -> None:
    """Drop IP keys whose hits have all aged out so the registry can't grow forever."""
    if now - _rate_last_prune[bucket] < RATE_LIMIT_WINDOW * 5:
        return
    _rate_last_prune[bucket] = now
    hits = _rate_buckets[bucket]
    stale = [ip for ip, dq in hits.items() if not dq or now - dq[-1] > RATE_LIMIT_WINDOW]
    for ip in stale:
        del hits[ip]


def rate_ok(ip: str, bucket: str = "ai") -> bool:
    now = time.time()
    with _rate_lock:
        _prune_rate_hits(now, bucket)
        dq = _rate_buckets[bucket][ip]
        while dq and now - dq[0] > RATE_LIMIT_WINDOW:
            dq.popleft()
        if len(dq) >= RATE_LIMIT_MAX:
            return False
        dq.append(now)
        return True


def is_bot(data: dict) -> bool:
    """Honeypot + minimum fill-time check. Returns True for likely bots."""
    if str(data.get(HONEYPOT_FIELD, "")).strip():
        return True
    started = data.get("form_started_at")
    try:
        if started:
            elapsed = time.time() - (float(started) / 1000.0)
            if 0 <= elapsed < MIN_FILL_SECONDS:
                return True
    except (TypeError, ValueError):
        pass
    return False


def clean_payload(data: dict) -> dict:
    fields = [
        "restaurant_name",
        "website",
        "restaurant_zip",
        "target_radius",
        "cuisine_types",
        "service_style",
        "campaign_objective",
        "contact_name",
        "contact_email",
        "contact_phone",
        "notes",
    ]
    cleaned = {k: str(data.get(k, "")).strip()[:1500] for k in fields}
    if not re.fullmatch(r"\d{5}(-\d{4})?", cleaned["restaurant_zip"]):
        raise ValueError("A valid U.S. ZIP code is required.")
    # Website is required — the AI reads it to tailor the plan. Accept a bare domain
    # or a social page; normalize to a fetchable https URL.
    site = cleaned["website"]
    if not site:
        raise ValueError("A website, Facebook, Instagram, or Google listing is required.")
    if not re.match(r"^https?://", site, re.I):
        site = "https://" + site.lstrip("/")
    if not re.match(r"^https?://[^\s.]+\.[^\s.]{2,}", site, re.I):
        raise ValueError("Please enter a valid web address (e.g. yourrestaurant.com).")
    cleaned["website"] = site
    # lead_id lets GHL correlate the partial lead POST with the completed report POST.
    cleaned["lead_id"] = str(data.get("lead_id", "")).strip()[:64] or uuid.uuid4().hex[:16]
    return cleaned


ATTRIBUTION_KEYS = (
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "referrer_url", "landing_page_url",
)


def merge_attribution(payload: dict, data: dict) -> dict:
    """Copy click/attribution fields from the raw request into the webhook payload."""
    for key in ATTRIBUTION_KEYS:
        value = str(data.get(key, "")).strip()[:300]
        if value:
            payload[key] = value
    return payload


def _valid_contact(payload: dict) -> bool:
    return bool(payload.get("contact_name")) and bool(EMAIL_RE.match(payload.get("contact_email", "")))


# ---------------------------------------------------------------------------
# OpenAI report generation (with one retry)
# ---------------------------------------------------------------------------

def _ssrf_safe(url: str) -> bool:
    """Refuse anything that resolves inside our own network.

    Checks the resolved IPs, not the hostname — a public name can point at a
    private address, which is the whole trick.
    """
    import ipaddress
    import socket
    from urllib.parse import urlparse
    try:
        u = urlparse(url)
        if u.scheme not in ("http", "https"):
            return False
        if u.username or u.password:
            return False              # credentials in a URL are never ours
        host = u.hostname or ""
        if not host:
            return False
        for info in socket.getaddrinfo(host, u.port or (443 if u.scheme == "https" else 80),
                                       proto=socket.IPPROTO_TCP):
            ip = ipaddress.ip_address(info[4][0])
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
                return False
    except Exception:                 # noqa: BLE001
        return False                  # unresolvable is refused, not allowed
    return True


def fetch_site_text(url: str) -> str:
    """Best-effort fetch of the restaurant's website/social page as plain text so the
    AI can ground the report in their real menu, prices, and vibe. Returns '' on any
    failure so report generation is never blocked by a slow or missing site."""
    if not url:
        return ""
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url.lstrip("/")
    # This fetches a URL a stranger typed into a public form, from inside our
    # network. Without a guard, "http://169.254.169.254/latest/meta-data/"
    # reads the cloud metadata endpoint and pipes it into the AI prompt, where
    # it can surface in the returned report. The QA audit confirmed this was
    # reachable end to end.
    if not _ssrf_safe(url):
        app.logger.warning("Refused an unsafe website URL: %s", url[:120])
        return ""
    try:
        r = requests.get(
            url,
            timeout=6,
            allow_redirects=False,   # a redirect can point back at a private IP
            headers={"User-Agent": "Mozilla/5.0 (compatible; Smart1Bot/1.0; +https://smart1marketing.com)"},
        )
        # Follow redirects by hand so every hop is re-checked.
        hops = 0
        while r.is_redirect and hops < 4:
            nxt = r.headers.get("Location") or ""
            if not nxt.lower().startswith("http"):
                from urllib.parse import urljoin
                nxt = urljoin(r.url, nxt)
            if not _ssrf_safe(nxt):
                app.logger.warning("Refused an unsafe redirect: %s", nxt[:120])
                return ""
            r = requests.get(nxt, timeout=6, allow_redirects=False,
                             headers={"User-Agent": "Smart1Bot/1.0"})
            hops += 1
        r.raise_for_status()
        html = r.text or ""
    except Exception:
        app.logger.info("Website fetch skipped (unreachable): %s", url)
        return ""
    parts = []
    mt = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    if mt:
        parts.append("TITLE: " + re.sub(r"\s+", " ", mt.group(1)).strip())
    md = re.search(r'(?is)<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']', html)
    if md:
        parts.append("META: " + re.sub(r"\s+", " ", md.group(1)).strip())
    body = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", html)
    body = re.sub(r"(?is)<[^>]+>", " ", body)
    body = re.sub(r"&[a-z0-9#]+;", " ", body)
    body = re.sub(r"\s+", " ", body).strip()
    parts.append(body)
    return " \n".join(parts)[:4000]


def _call_openai(payload: dict, site_text: str = "") -> Any:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured.")
    site_block = ""
    if site_text:
        site_block = (
            "\n\nThe restaurant's own website/social page text is provided below. Use it to GROUND the report in "
            "this specific restaurant — infer cuisine specifics and signature items, price point, service style, "
            "hours and strong dayparts, neighborhoods or landmarks they mention, and brand tone. Let these refine "
            "market_summary, market_opportunity, demand_triggers, and the monthly_plan messaging so the report "
            "clearly reads as written for THIS restaurant. If the text is thin, generic, or irrelevant, rely on the "
            "structured inputs instead and do not invent specifics.\n"
            f"--- WEBSITE TEXT START ---\n{site_text}\n--- WEBSITE TEXT END ---\n"
        )
    user_prompt = (
        "\nBuild a trigger-driven Restaurant Demand & Geofencing Report from these inputs:\n"
        f"{json.dumps(payload, indent=2)}"
        f"{site_block}"
        "\n\nThe restaurant did NOT provide daypart patterns, a media budget, trigger preferences, or "
        "lists of nearby generators and competitors. You must supply all of these yourself:\n"
        "- Assume realistic lunch, happy-hour, dinner, and (if relevant) late-night daypart patterns from the "
        "restaurant's category and ZIP code.\n"
        "- Identify the local office parks, residential density, hotels, colleges, gyms, shopping/entertainment "
        "venues, stadiums/arenas, and competing restaurants yourself from geographic knowledge of the market, "
        "and include the best of them in geofence_locations.\n\n"
        "Populate every field of the schema:\n"
        "- market_summary: one or two sentences framing the trigger-driven dining demand opportunity "
        "for this restaurant and market (reference the restaurant name and area).\n"
        "- market_type: a short badge label for the market, e.g. 'Dense Urban Business District' "
        "or 'Suburban Residential / Family Market'. market_type_description: one sentence on the daypart pattern.\n"
        "- market_profile: low/base/high estimates for population, households, and likely frequent-diner "
        "households, plus a dine-out rate (as a percentage decimal such as 22.5, not 0.225), confidence, "
        "and assumptions.\n"
        "- market_opportunity: keep this SIMPLE — one short, plain sentence on the restaurant's opportunity "
        "in this market.\n"
        "- recommended_package: choose the best-fit package for this market from the Smart 1 package menu below. "
        "Use its exact NAME and monthly price as monthly_investment (e.g. '$3,000/month'), and write a short "
        "description of what that level buys. Pick the tier based on market size, competitive density, and "
        "the number of daypart/occasion opportunities (lunch, happy hour, dinner, delivery, events, catering).\n"
        "  SMART 1 PACKAGE MENU (use these, do not invent prices):\n"
        f"{PACKAGE_MENU_STR}\n"
        "- media_channels: ALLOWED channels/data only. ALWAYS include 'In-Market Diner & Foot-Traffic Audience "
        "Data' as one of the chips (we layer third-party in-market dining and delivery-app-user data across the "
        "plan). Then choose from: geofencing offices/residential/venues, location look-back retargeting, "
        "data-driven / programmatic targeted display, connected TV (CTV/OTT), streaming audio, YouTube/online "
        "video, website retargeting. Return 4-7 chips total. NEVER include paid search, email, SMS, or any "
        "social channel.\n"
        "- streaming_audio_note: a short recommendation to geotarget streaming audio (streaming radio) around "
        "nearby offices and residential density during commute and pre-meal windows, since diners stream audio "
        "while deciding where to eat. Specify a daypart running around lunch and evening commute windows.\n"
        "- demand_triggers: 5-8 short trigger labels for this market (e.g. 'Lunch rush 11-1', '90°+ patio "
        "weather', 'Rainy day', 'Home game night', 'Holiday weekend'). For markets with cold seasons, "
        "ALSO include relevant winter-weather advisory triggers such as 'Freeze warning', 'Ice storm', "
        "'Frost warning', 'First snow', and 'Cold snap' (these drive comfort-food dine-in and delivery "
        "demand). Choose triggers that fit the market's climate.\n"
        "- monthly_plan: all 12 months (January through December). Each month has a focus title, a short "
        "customer-facing message, 1-2 relevant demand trigger labels drawn from demand_triggers, and a "
        "'pacing' string. Match focus to the season and local calendar (patio/summer promotion, back-to-school, "
        "holiday parties/catering, football season, Valentine's/Mother's Day occasions, etc. as relevant).\n"
        "  BUDGET PACING RULE for the 'pacing' field: the recommended_package monthly_investment is the PEAK "
        "in-season monthly budget (100%). In shoulder months spend 60% of the package; in the slowest months "
        "spend 35% of the package. Classify each month as Peak, Shoulder, or Slow based on this market and "
        "concept, and set pacing to a short string with the tier, percent, and computed dollar amount — e.g. "
        "'Peak — 100% ($3,000)', 'Shoulder — 60% ($1,800)', 'Slow — 35% ($1,050)'. Compute the dollars from "
        "the chosen package price.\n"
        "- geofence_locations: 12-18 locations (offices, residential density, hotels, colleges, gyms, shopping/"
        "entertainment venues, stadiums/arenas, competitors, event venues). Prioritize locations inside the "
        "target radius; lower confidence for uncertain ones. Keep text concise.\n"
        "- disclaimer: a short note that figures are AI planning estimates for the market, not exact counts.\n"
    )
    # Over HTTP rather than the openai SDK — not a Hub dependency.
    import requests as _rq
    _r = _rq.post("https://api.openai.com/v1/responses",
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
        json={"model": MODEL,
              "input": [{"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt}],
              "text": {"format": {"type": "json_schema", **REPORT_SCHEMA}},
              "temperature": 0.25, "max_output_tokens": 8000},
        timeout=120)
    _r.raise_for_status()
    _data = _r.json()
    try:
        from hub import ai as _hub_ai
        _hub_ai.note_usage("restaurant", _data, purpose="market_report")
    except Exception:  # noqa: BLE001
        pass
    text = ""
    for _it in (_data.get("output") or []):
        for _pt in (_it.get("content") or []):
            if _pt.get("type") in ("output_text", "text"):
                text += _pt.get("text") or ""
    text = (text or _data.get("output_text") or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    return json.loads(text)


def generate_report(payload: dict) -> Any:
    """Generate the report, retrying once on transient API or JSON-parse failures."""
    # Fetch the site once (not per retry) so the AI can review the restaurant's own page.
    site_text = fetch_site_text(payload.get("website", ""))
    try:
        return _call_openai(payload, site_text)
    except Exception:
        app.logger.warning("OpenAI report attempt 1 failed; retrying once.", exc_info=True)
        time.sleep(1.2)
        return _call_openai(payload, site_text)


# ---------------------------------------------------------------------------
# Cloudinary helpers
# ---------------------------------------------------------------------------

def _cloud_name() -> str:
    m = re.search(r"@([^/?\s]+)", CLOUDINARY_URL)
    return m.group(1) if m else ""


def _cloudinary_upload(data: bytes, public_id: str, resource_type: str, fmt: str = "") -> dict:
    """Store through hub.storage, keeping this module's own public_ids.

    The `resource_type` argument is now advisory: hub.storage derives the real
    one from the filename, so the caller cannot ask for a PDF to be stored as
    an image. That request is exactly what this module used to default to, and
    it produces a link that 403s when a customer clicks Download.

    The return shape stays a dict with secure_url, because callers read it.
    """
    from hub import storage
    name = public_id if "." in public_id.rsplit("/", 1)[-1] else \
        f"{public_id}.{(fmt or resource_type or 'bin').lstrip('.')}"
    asset = storage.put("restaurant_reports", name, data,
                        public_id=public_id, overwrite=True)
    return {"secure_url": asset.url, "public_id": asset.public_id,
            "resource_type": asset.resource_type}


def _store_report_json(report: dict, report_id: str) -> bool:
    """Persist the report JSON in Cloudinary so /r/<id> can serve it later.
    Without Cloudinary, fall back to local (ephemeral) storage so /api/report/<id>
    still works for the PDF-download polling on the same instance."""
    if not CLOUDINARY_URL:
        try:
            jsonstore.write_json(
                os.path.join(REPORT_JSON_DIR, f"{report_id}.json"), report)
            return True
        except Exception:
            app.logger.exception("Local report JSON store failed")
            return False
    try:
        _cloudinary_upload(
            json.dumps(report).encode("utf-8"),
            public_id=f"{REPORT_JSON_NAME}/{report_id}",
            resource_type="raw",
            fmt="json",
        )
        return True
    except Exception:
        app.logger.exception("Report JSON store failed")
        return False


def _fetch_report_json(report_id: str):
    if not CLOUDINARY_URL:
        try:
            report = jsonstore.read_json(
                os.path.join(REPORT_JSON_DIR, f"{report_id}.json"), default=None)
            if report is not None:
                return report
            # Anything written before the move still lives under static/.
            legacy = os.path.join(REPORT_DIR, f"{report_id}.json")
            if os.path.isfile(legacy):
                with open(legacy, "r", encoding="utf-8") as fh:
                    return json.load(fh)
        except Exception:
            app.logger.exception("Local report JSON read failed")
        return None
    cloud = _cloud_name()
    if not cloud:
        return None
    url = f"https://res.cloudinary.com/{cloud}/raw/upload/{REPORT_JSON_NAME}/{report_id}.json"
    try:
        r = requests.get(url, timeout=8)
        if r.ok:
            return r.json()
    except requests.RequestException:
        app.logger.exception("Report JSON fetch failed")
    return None


# ---------------------------------------------------------------------------
# PDF report — reportlab, pure Python. Rendered in memory, stored in Cloudinary.
# ---------------------------------------------------------------------------

NAVY = colors.HexColor("#0a2240")
BLUE = colors.HexColor("#009ed2")
GREEN = colors.HexColor("#2dbb72")
GOLD = colors.HexColor("#f0b429")
LINE = colors.HexColor("#dbe5ed")
MUTED = colors.HexColor("#68798c")
AQUA = colors.HexColor("#eff9fc")

PDF_FOOTER_TEXT = "Smart 1 Marketing · (614) 536-0768 · smart1marketing.com"


def _pdf_footer(canvas, doc):
    """Draw the brand footer line at the bottom of every PDF page."""
    canvas.saveState()
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(MUTED)
    canvas.drawCentredString(letter[0] / 2.0, 0.35 * inch, PDF_FOOTER_TEXT)
    canvas.restoreState()


def _money_to_int(value: str):
    """'$3,000/month' -> 3000 (int) or None."""
    digits = re.sub(r"[^\d]", "", (value or "").split("/")[0])
    return int(digits) if digits else None


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "report").lower()).strip("-") or "report"


def _pdf_styles():
    ss = getSampleStyleSheet()
    body = ParagraphStyle("s1body", parent=ss["Normal"], fontName="Helvetica",
                          fontSize=9.5, leading=14, textColor=colors.HexColor("#25364b"))
    h2 = ParagraphStyle("s1h2", parent=ss["Heading2"], fontName="Helvetica-Bold",
                        fontSize=13, leading=16, textColor=NAVY, spaceBefore=16, spaceAfter=6)
    title = ParagraphStyle("s1title", parent=ss["Title"], fontName="Helvetica-Bold",
                           fontSize=22, leading=25, textColor=NAVY, alignment=TA_LEFT, spaceAfter=4)
    eyebrow = ParagraphStyle("s1eye", parent=body, fontName="Helvetica-Bold",
                             fontSize=8, textColor=BLUE, spaceAfter=2)
    small = ParagraphStyle("s1small", parent=body, fontSize=8, textColor=MUTED, leading=11)
    cell = ParagraphStyle("s1cell", parent=body, fontSize=8, leading=10.5)
    cellw = ParagraphStyle("s1cellw", parent=cell, textColor=colors.white)
    return dict(body=body, h2=h2, title=title, eyebrow=eyebrow, small=small, cell=cell, cellw=cellw)


def _gbb_table(report: dict, st: dict) -> Table:
    """Good/Better/Best 3-column pricing table; the recommended tier's column is
    shaded brand-blue (mirrors the on-screen GBB cards)."""
    rp = report.get("recommended_package", {}) or {}
    rec_name = (rp.get("package_name") or "").strip().lower()
    rec_price = (rp.get("monthly_investment") or "").strip()
    idx = next((i for i, p in enumerate(PACKAGES)
                if p["name"].lower() == rec_name or p["price"] == rec_price), 1)
    start = max(0, min(idx - 1, len(PACKAGES) - 3))
    trio = PACKAGES[start:start + 3]
    rec_col = idx - start
    labels = ["GOOD", "BETTER", "BEST"]
    header_row, body_row = [], []
    for i, p in enumerate(trio):
        cs = st["cellw"] if i == rec_col else st["cell"]
        header_row.append(Paragraph(f"<b>{labels[i]}</b>", cs))
        monthly = _money_to_int(p["price"])
        annual = f"${monthly * 12:,}/yr" if monthly else ""
        rec_tag = "<br/><font size=6.5><b>RECOMMENDED FOR YOUR MARKET</b></font>" if i == rec_col else ""
        body_row.append(Paragraph(
            f"<font size=13><b>{str(p['price']).split('/')[0]}</b></font> /month<br/>"
            f"<b>{p['name']}</b><br/><font size=7>{annual}</font><br/>"
            f"<font size=7>{p.get('blurb', '')}</font>{rec_tag}", cs))
    t = Table([header_row, body_row], colWidths=[2.4 * inch] * 3)
    style = [
        ("GRID", (0, 0), (-1, -1), 0.5, LINE),
        ("BACKGROUND", (0, 0), (-1, 0), AQUA),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        # Shade the recommended column brand-blue (header + body).
        ("BACKGROUND", (rec_col, 0), (rec_col, -1), BLUE),
        ("BOX", (rec_col, 0), (rec_col, -1), 1, NAVY),
    ]
    t.setStyle(TableStyle(style))
    return t


def _pacing_bar_table(report: dict, st: dict) -> Table:
    """Colored 3-bar budget-pacing visual (Peak / Shoulder / Slow) built from
    tables with proportional colored cell widths."""
    rp = report.get("recommended_package", {}) or {}
    monthly = _money_to_int(rp.get("monthly_investment", "")) or 3000
    tiers = [("Peak months", 1.00, GREEN), ("Shoulder months", 0.60, BLUE), ("Slow months", 0.35, GOLD)]
    max_bar = 3.6 * inch
    rows = []
    for label, frac, col in tiers:
        bar = Table([[""]], colWidths=[max(0.3 * inch, max_bar * frac)], rowHeights=[11])
        bar.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), col),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
        rows.append([
            Paragraph(f"<b>{label}</b> — {int(frac * 100)}%", st["cell"]),
            bar,
            Paragraph(f"<b>${monthly * frac:,.0f}</b>/mo", st["cell"]),
        ])
    t = Table(rows, colWidths=[1.7 * inch, 3.8 * inch, 1.4 * inch])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (1, 0), (1, -1), "LEFT"),
        ("LEFTPADDING", (1, 0), (1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return t


_LOGO_CACHE: dict = {}


def _logo_flowable():
    """Return a reportlab Image for the PDF logo (cached) or None."""
    if not PDF_LOGO_URL:
        return None
    try:
        if "bytes" not in _LOGO_CACHE:
            resp = requests.get(PDF_LOGO_URL, timeout=8)
            _LOGO_CACHE["bytes"] = resp.content if resp.ok else b""
        raw = _LOGO_CACHE.get("bytes") or b""
        if not raw:
            return None
        img = Image(io.BytesIO(raw))
        # scale to ~150px wide keeping aspect
        ratio = (img.imageHeight or 1) / (img.imageWidth or 1)
        img.drawWidth = 150
        img.drawHeight = 150 * ratio
        return img
    except Exception:
        app.logger.exception("PDF logo load failed")
        return None


def build_report_pdf(report: dict, restaurant: str, base_url: str) -> str:
    """Render the report JSON to a branded PDF, store it in Cloudinary (falling back
    to static/reports only if Cloudinary is not configured), and return its public URL
    (or '' on failure)."""
    if not ENABLE_PDF:
        return ""
    try:
        st = _pdf_styles()
        fmt = lambda n: f"{int(n):,}" if n is not None else "—"
        rng = lambda a, b: f"{fmt(a)}–{fmt(b)}"
        m = report.get("market_profile", {}) or {}
        rp = report.get("recommended_package", {}) or {}

        public_id_base = f"{_slug(restaurant)}-{int(time.time())}"

        story = []
        logo = _logo_flowable()
        if logo is not None:
            story.append(logo)
            story.append(Spacer(1, 8))
        story.append(Paragraph("SMART 1 MARKETING &nbsp;|&nbsp; RESTAURANT MARKET REPORT", st["eyebrow"]))
        story.append(Paragraph(restaurant or "Market Report", st["title"]))
        story.append(Paragraph(report.get("market_summary", ""), st["body"]))
        story.append(Spacer(1, 6))

        if report.get("market_type"):
            story.append(Paragraph(f"<b>{report.get('market_type')}</b> — {report.get('market_type_description','')}", st["small"]))

        stat_data = [[
            Paragraph(f"<b>{rng(m.get('estimated_population_low'), m.get('estimated_population_high'))}</b><br/><font size=7 color='#68798c'>ESTIMATED POPULATION</font>", st["cell"]),
            Paragraph(f"<b>{rng(m.get('estimated_households_low'), m.get('estimated_households_high'))}</b><br/><font size=7 color='#68798c'>ESTIMATED HOUSEHOLDS</font>", st["cell"]),
            Paragraph(f"<b>{rng(m.get('estimated_frequent_diner_households_low'), m.get('estimated_frequent_diner_households_high'))}</b><br/><font size=7 color='#68798c'>LIKELY FREQUENT-DINER HOUSEHOLDS</font>", st["cell"]),
        ]]
        stat = Table(stat_data, colWidths=[2.4 * inch] * 3)
        stat.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), AQUA),
            ("BOX", (0, 0), (-1, -1), 0.5, LINE),
            ("INNERGRID", (0, 0), (-1, -1), 0.5, LINE),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 10),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ]))
        story.append(Spacer(1, 8))
        story.append(stat)

        story.append(Paragraph("Your Market Opportunity", st["h2"]))
        story.append(Paragraph(report.get("market_opportunity", ""), st["body"]))

        story.append(Paragraph("Recommended Package", st["h2"]))
        story.append(Paragraph(f"<b>{rp.get('monthly_investment','')} — {rp.get('package_name','')}</b>", st["body"]))
        story.append(Paragraph(rp.get("description", ""), st["small"]))

        story.append(Paragraph("Suggested Investment — Good / Better / Best", st["h2"]))
        story.append(_gbb_table(report, st))
        story.append(Spacer(1, 4))
        story.append(Paragraph("Suggested budgets, not a quote — the highlighted tier is our recommendation for a market this size.", st["small"]))

        story.append(Paragraph("Budget Pacing Across the Year", st["h2"]))
        story.append(_pacing_bar_table(report, st))
        story.append(Spacer(1, 2))
        story.append(Paragraph("Spend the full tier in peak months, about 60% in shoulder months, and 35% in your slowest months (see the month-by-month plan).", st["small"]))

        chans = report.get("media_channels", []) or []
        if chans:
            story.append(Paragraph("Recommended Media Channels", st["h2"]))
            story.append(Paragraph(" &nbsp;•&nbsp; ".join(chans), st["body"]))

        trigs = report.get("demand_triggers", []) or []
        if trigs:
            story.append(Paragraph("Demand Triggers", st["h2"]))
            story.append(Paragraph(" &nbsp;•&nbsp; ".join(trigs), st["body"]))

        plan = report.get("monthly_plan", []) or []
        if plan:
            story.append(Paragraph("Month-by-Month Campaign Plan", st["h2"]))
            rows = [[Paragraph("<b>Month</b>", st["cellw"]), Paragraph("<b>Focus</b>", st["cellw"]), Paragraph("<b>Budget Pacing</b>", st["cellw"])]]
            for x in plan:
                rows.append([
                    Paragraph(x.get("month", ""), st["cell"]),
                    Paragraph(x.get("focus", ""), st["cell"]),
                    Paragraph(x.get("pacing", ""), st["cell"]),
                ])
            t = Table(rows, colWidths=[1.1 * inch, 3.3 * inch, 2.8 * inch], repeatRows=1)
            t.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), NAVY),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fbfc")]),
                ("GRID", (0, 0), (-1, -1), 0.5, LINE),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]))
            story.append(t)

        geo = sorted(report.get("geofence_locations", []) or [], key=lambda g: g.get("priority", 3))
        if geo:
            story.append(Paragraph("Recommended Geofence Locations", st["h2"]))
            rows = [[Paragraph(f"<b>{h}</b>", st["cellw"]) for h in ("P", "Location", "Category", "Method", "Radius", "Conf.")]]
            for g in geo:
                rows.append([
                    Paragraph(f"P{g.get('priority','')}", st["cell"]),
                    Paragraph(f"<b>{g.get('name','')}</b><br/>{g.get('city_state','')}", st["cell"]),
                    Paragraph(g.get("category", ""), st["cell"]),
                    Paragraph(str(g.get("recommended_method", "")).replace("_", " "), st["cell"]),
                    Paragraph(f"{g.get('recommended_radius_miles','')} mi", st["cell"]),
                    Paragraph(g.get("confidence", ""), st["cell"]),
                ])
            t = Table(rows, colWidths=[0.4 * inch, 2.2 * inch, 1.5 * inch, 1.3 * inch, 0.7 * inch, 0.6 * inch], repeatRows=1)
            t.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), NAVY),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fbfc")]),
                ("GRID", (0, 0), (-1, -1), 0.5, LINE),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]))
            story.append(t)

        story.append(Spacer(1, 12))
        story.append(Paragraph(report.get("disclaimer", ""), st["small"]))

        # Render to memory so we can push the bytes straight to Cloudinary.
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=letter, title=f"{restaurant} Market Report",
                                leftMargin=0.6 * inch, rightMargin=0.6 * inch,
                                topMargin=0.6 * inch, bottomMargin=0.6 * inch)
        doc.build(story, onFirstPage=_pdf_footer, onLaterPages=_pdf_footer)
        pdf_bytes = buffer.getvalue()

        # Primary path: store in Cloudinary and return its hosted URL.
        if CLOUDINARY_URL:
            try:
                # The resource type comes from the setting rather than being
                # repeated as a literal, so there is one source of truth and
                # the integrity check for "PDF uploaded as an image type"
                # stays meaningful instead of flagging a branch nobody uses.
                if PDF_RESOURCE_TYPE == "image":
                    res = _cloudinary_upload(pdf_bytes, public_id=f"{REPORT_NAME}/{public_id_base}",
                                             resource_type=PDF_RESOURCE_TYPE, fmt="pdf")
                else:
                    res = _cloudinary_upload(pdf_bytes, public_id=f"{REPORT_NAME}/{public_id_base}.pdf",
                                             resource_type=PDF_RESOURCE_TYPE)
                url = res.get("secure_url", "") or ""
                if url:
                    return url
            except Exception:
                app.logger.exception("Cloudinary PDF upload failed; using local fallback")

        # Fallback (local dev / Cloudinary not configured): write to static/reports.
        os.makedirs(REPORT_DIR, exist_ok=True)
        filename = f"{public_id_base}.pdf"
        with open(os.path.join(REPORT_DIR, filename), "wb") as fh:
            fh.write(pdf_bytes)
        base = base_url or PUBLIC_BASE_URL
        return f"{base.rstrip('/')}/static/reports/{filename}" if base else f"/static/reports/{filename}"
    except Exception:
        app.logger.exception("PDF generation failed")
        return ""


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------

def send_webhook(payload: dict, report: Any, status: str, pdf_url: str = "",
                 report_view_url: str = "") -> None:
    """Deliver the lead through the Hub's lead panel.

    Was a fire-and-forget POST that returned silently with no WEBHOOK_URL, so
    a blank env var discarded every lead while the visitor saw success. The
    panel stores first and forwards second.
    """
    try:
        from hub import leads as hub_leads
        hub_leads.capture_and_deliver(
            source="restaurant",
            page="Restaurant Market Plan",
            fields={
                "name": payload.get("contact_name", ""),
                "email": payload.get("contact_email", ""),
                "phone": payload.get("contact_phone", ""),
                "company": payload.get("restaurant_name", ""),
                "report_status": status,
            },
            pdf_url=pdf_url or "",
            client=payload.get("restaurant_name", ""))
        # Attribute the report to the client, so the work shows on
        # their 360 record rather than only in the lead panel.
        try:
            from hub import audit as _audit
            _audit.log("restaurant", "restaurant_report",
                       client=payload.get("restaurant_name", "") or None)
        except Exception:  # noqa: BLE001 — logging never breaks a lead
            pass
        return
    except Exception:  # noqa: BLE001
        pass

    if not WEBHOOK_URL:
        return
    report = report or {}
    mp = report.get("market_profile", {}) or {}
    rp = report.get("recommended_package", {}) or {}
    monthly = _money_to_int(rp.get("monthly_investment", ""))
    body = {
        # --- Contact / lead fields ---
        **payload,
        "lead_id": payload.get("lead_id", ""),
        "source": "Smart 1 Restaurant Market Intelligence",
        "report_status": status,
        # --- Opportunity fields ---
        "opportunity_name": f"{payload.get('restaurant_name', 'Lead')} — Restaurant Market Report",
        "recommended_package": rp.get("package_name", ""),
        "recommended_investment": rp.get("monthly_investment", ""),
        "opportunity_value_monthly": monthly,
        "opportunity_value_annual": monthly * 12 if monthly else None,
        # --- Report custom fields ---
        "market_type": report.get("market_type", ""),
        "market_summary": report.get("market_summary", ""),
        "est_households": mp.get("estimated_frequent_diner_households_base"),
        "estimated_frequent_diner_households_base": mp.get("estimated_frequent_diner_households_base"),
        "demand_triggers": ", ".join(report.get("demand_triggers", []) or []),
        # Cloudinary-hosted PDF + shareable interactive report link.
        "report_pdf_url": pdf_url,
        "report_view_url": report_view_url,
        "report_json": json.dumps(report, separators=(",", ":"))[:60000] if report else "",
    }
    try:
        requests.post(WEBHOOK_URL, json=body, timeout=12)
    except requests.RequestException:
        app.logger.exception("Webhook delivery failed")


def _send_webhook_async(*args, **kwargs) -> None:
    threading.Thread(target=send_webhook, args=args, kwargs=kwargs, daemon=True).start()


def _finalize_report(payload: dict, report: dict, base_url: str, report_id: str) -> None:
    """Background work after the interactive report is returned to the browser:
    build+store the PDF, persist the report JSON, and send the completed webhook."""
    try:
        pdf_url = build_report_pdf(report, payload.get("restaurant_name", "Market Report"), base_url)
        # Persist the restaurant name inside the report so /r/<id> can title the shared view,
        # and the PDF URL so the browser can poll /api/report/<id> and offer a download button.
        report["_restaurant"] = payload.get("restaurant_name", "")
        report["report_pdf_url"] = pdf_url
        stored = _store_report_json(report, report_id)
        base = (PUBLIC_BASE_URL or base_url or "").rstrip("/")
        report_view_url = f"{base}/r/{report_id}" if (stored and base) else ""
        send_webhook(payload, report, "completed", pdf_url, report_view_url)
    except Exception:
        app.logger.exception("Report finalize failed")


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------

@app.after_request
def _apply_cors(resp):
    origin = request.headers.get("Origin")
    if ALLOWED_ORIGINS == "*":
        resp.headers["Access-Control-Allow-Origin"] = "*"
    elif origin and origin in [o.strip() for o in ALLOWED_ORIGINS.split(",") if o.strip()]:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return render_template(
        "index.html",
        packages_json=json.dumps(PACKAGES),
        honeypot_field=HONEYPOT_FIELD,
        report_view_id="",
    )


@app.get("/r/<rid>")
def shared_report(rid):
    rid = re.sub(r"[^a-zA-Z0-9]", "", rid)[:32]
    return render_template(
        "index.html",
        packages_json=json.dumps(PACKAGES),
        honeypot_field=HONEYPOT_FIELD,
        report_view_id=rid,
    )


@app.get("/api/report/<rid>")
def api_report(rid):
    rid = re.sub(r"[^a-zA-Z0-9]", "", rid)[:32]
    report = _fetch_report_json(rid)
    if report is None:
        return jsonify({"ok": False, "error": "Report not found."}), 404
    return jsonify({"ok": True, "report": report})


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/api/lead", methods=["POST", "OPTIONS"])
def api_lead():
    """Fire a partial-lead webhook the moment the form is submitted, so the lead is
    captured even if report generation fails or the visitor leaves."""
    if request.method == "OPTIONS":
        return ("", 204)
    data = request.get_json(silent=True) or {}
    if is_bot(data):
        return jsonify({"ok": True})  # silently accept, do nothing
    if not rate_ok(_client_ip(), bucket="lead"):
        return jsonify({"ok": False, "error": "Too many requests."}), 429
    try:
        payload = clean_payload(data)
    except ValueError:
        # Don't hard-fail lead capture on a bad ZIP/website; salvage EVERY field we can.
        payload = {k: str(data.get(k, "")).strip()[:1500] for k in
                   ("restaurant_name", "website", "restaurant_zip", "target_radius",
                    "cuisine_types", "service_style", "campaign_objective", "notes",
                    "contact_name", "contact_email", "contact_phone")}
        payload["lead_id"] = str(data.get("lead_id", "")).strip()[:64] or uuid.uuid4().hex[:16]
    merge_attribution(payload, data)
    # Need at least something identifying before forwarding.
    if not (payload.get("restaurant_name") or payload.get("website") or payload.get("restaurant_zip")):
        return jsonify({"ok": True, "lead_id": payload.get("lead_id", "")})
    # No valid contact yet? Still forward — that's the point of partial capture.
    status = "new" if _valid_contact(payload) else "partial"
    _send_webhook_async(payload, None, status)
    return jsonify({"ok": True, "lead_id": payload.get("lead_id", "")})


@app.route("/api/analyze", methods=["POST", "OPTIONS"])
def analyze():
    if request.method == "OPTIONS":
        return ("", 204)
    data = request.get_json(silent=True) or {}
    if is_bot(data):
        return jsonify({"ok": False, "error": "Request rejected."}), 400
    if not rate_ok(_client_ip()):
        return jsonify({"ok": False, "error": "Too many requests. Please wait a minute and try again."}), 429
    try:
        payload = clean_payload(data)
        report = generate_report(payload)
        merge_attribution(payload, data)  # after the AI call so attr never leaks into the prompt
        report_id = uuid.uuid4().hex[:16]
        base_url = PUBLIC_BASE_URL or request.url_root
        # Serialize the response from a shallow copy BEFORE starting the background
        # thread, so its mutations of `report` can never leak into this response.
        response = jsonify({"ok": True, "report": dict(report), "report_id": report_id})
        # Return the interactive report to the browser immediately; do the heavy
        # PDF/Cloudinary/webhook work in the background.
        threading.Thread(target=_finalize_report,
                         args=(payload, report, base_url, report_id), daemon=True).start()
        return response
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        app.logger.exception("Analysis failed")
        # Still record the failed lead so nothing is lost.
        try:
            fail_payload = clean_payload(data)
            _send_webhook_async(fail_payload, None, "failed")
        except Exception:
            pass
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "The report could not be generated. Please try again.",
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            ),
            500,
        )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
