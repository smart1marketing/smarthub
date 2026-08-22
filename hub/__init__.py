"""Smart 1 Hub — the shell application.

Owns: login/logout, dashboard, Client 360, Tools landing, Activity, Status,
plus serving the prebuilt Knack "Clients" app (which expects /static and
/data at the site root, so the hub serves those paths for it).
"""
import re
import json
import os
import shutil
import subprocess

import requests as _rq
from flask import (
    Flask, jsonify, make_response, redirect, render_template, request,
    send_from_directory,
)

from . import audit, auth, errors, knack_data
from hub.webargs import clamp_int

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENTS_APP = os.path.join(ROOT, "clients_app")

MODULES = [
    {"key": "clients", "label": "Clients", "href": "/clients", "tag": "Knack"},
    {"key": "google", "label": "Google", "href": "/google/", "tag": "GA4 · GTM"},
    {"key": "sites", "label": "Sites", "href": "/sites/", "tag": "Simvoly"},
    {"key": "suite", "label": "Suite", "href": "/suite/", "tag": "GHL"},
    {"key": "scans", "label": "Site Scans", "href": "/scans/", "tag": "Insites"},
    {"key": "tools", "label": "Tools", "href": "/tools", "tag": ""},
]


def current_user():
    return auth.verify_cookie_value(request.cookies.get(auth.COOKIE_NAME))


_MOUNT_ACTIVE_HUB = {
    "/tools": "tools", "/qa": "qa", "/activity": "activity",
    "/diagnostics": "diagnostics", "/client360": "client360", "/seo": "seo",
    "/clients": "clients", "/status": "status",
}


def _read_document(raw: bytes, filename: str) -> str:
    """Text out of a PDF or DOCX. Returns "" rather than raising.

    A scanned PDF has no text layer, so this legitimately returns nothing —
    the caller says so plainly rather than reporting a failure.
    """
    name = (filename or "").lower()
    try:
        if name.endswith(".pdf"):
            try:
                from pypdf import PdfReader
            except ImportError:
                from PyPDF2 import PdfReader  # type: ignore
            import io as _io
            reader = PdfReader(_io.BytesIO(raw))
            return "\n".join((pg.extract_text() or "") for pg in reader.pages[:40])
        if name.endswith((".docx", ".doc")):
            import io as _io
            from docx import Document
            return "\n".join(p.text for p in Document(_io.BytesIO(raw)).paragraphs)
        return raw.decode("utf-8", errors="ignore")
    except Exception:  # noqa: BLE001
        return ""


def _proposal_text_for(client: str, filename: str) -> str:
    """Fetch a proposal already uploaded against a client and read it."""
    try:
        from . import proposals
        import requests as _r
        for rec in proposals.list_proposals(client):
            if rec.get("filename") == filename or rec.get("id") == filename:
                url = rec.get("url")
                if not url:
                    return ""
                resp = _r.get(url, timeout=30)
                if not resp.ok:
                    return ""
                return _read_document(resp.content, rec.get("filename", ""))
    except Exception:  # noqa: BLE001
        pass
    return ""


def create_hub_app() -> Flask:
    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
        static_url_path="/assets",
    )
    app.config["MAX_CONTENT_LENGTH"] = 512 * 1024 * 1024

    # every unhandled exception in the hub app lands in the error log
    from flask import got_request_exception

    def _log_exc(sender, exception, **extra):  # noqa: ARG001
        try:
            errors.log_exception("hub", exception, path=request.path,
                                 actor=current_user() or "")
        except Exception:  # noqa: BLE001 — logging must never break a response
            pass
    got_request_exception.connect(_log_exc, app)

    # ---------------- auth ----------------
    @app.context_processor
    def _inject_demo_module():
        """Which walkthrough belongs to the page being rendered.

        The demo launcher reads <body data-module>. Hub pages had none, so no
        walkthrough button ever appeared on the Dashboard, Client 360, SEO or
        QA — which is most of where somebody would look for one.
        """
        path = (request.path or "/").rstrip("/") or "/"
        mapping = {"/": "hub", "/client360": "hub", "/seo": "seo",
                   "/qa": "qa", "/qa/stale-creative": "qa",
                   "/tools": "hub", "/diagnostics": "hub"}
        return {"hub_demo_module": mapping.get(path, "")}

    @app.context_processor
    def _inject_sidebar():
        """Expose the one shared nav to hub templates."""
        from .sidebar import render_sidebar

        def hub_sidebar(active=""):
            try:
                return render_sidebar(active or "").decode()
            except Exception:  # noqa: BLE001 — nav must never break a page
                return ""
        return {"hub_sidebar": hub_sidebar}

    @app.context_processor
    def _inject_version():
        """Every page footer shows the running build, so you can open the
        deployed site and confirm it matches what you pushed."""
        from . import version as _v
        return {"hub_version": _v.label(), "hub_version_info": _v.info()}

    @app.route("/api/version")
    def api_version():
        from . import version as _v
        return jsonify(_v.info())

    # ---------------- v7.5: diagnostics, quotas, cost ----------------
    @app.route("/diagnostics")
    def page_diagnostics():
        gate = _require_page()
        if gate:
            return gate
        return render_template("diagnostics.html", user=current_user(),
                               active="diagnostics")

    @app.route("/api/diagnostics")
    def api_diagnostics():
        """Live reachability of every external API.

        Deliberately never spends a credit: Insites has no free endpoint, so it
        reports `unverified` rather than starting a throwaway audit.
        """
        gate = _require_api()
        if gate:
            return gate
        from . import diagnostics
        return jsonify(diagnostics.run_all())

    @app.route("/api/quotas")
    def api_quotas():
        """Monthly usage against allowances, plus the OpenAI cost estimate."""
        gate = _require_api()
        if gate:
            return gate
        from . import quotas
        return jsonify(quotas.summary(request.args.get("month")))

    @app.route("/api/backup")
    def api_backup():
        """What of the JSON on the disk is mirrored into the database.

        The disk is not part of the database backup and does not survive being
        recreated, so this answers "what would we actually lose?" — including
        the files deliberately left out because they are rebuildable.
        """
        gate = _require_api()
        if gate:
            return gate
        from . import jsonstore
        out = jsonstore.status()
        out["restore_at_boot"] = app.config.get("HUB_JSONSTORE_RESTORE")
        return jsonify(out)

    @app.route("/api/integrity")
    def api_integrity():
        """Static audit for defect patterns that have each shipped before."""
        gate = _require_api()
        if gate:
            return gate
        from . import integrity
        return jsonify(integrity.run())

    @app.route("/api/quotas/warnings")
    def api_quota_warnings():
        """Just the providers needing attention — for a banner or a cron job."""
        gate = _require_api()
        if gate:
            return gate
        from . import quotas
        warns = quotas.warnings(request.args.get("month"))
        return jsonify({"warnings": warns, "count": len(warns),
                        "ok": not warns})

    @app.route("/api/client/brand")
    def api_client_brand():
        """Logos, colours and fonts for the Client 360 brand card."""
        gate = _require_api()
        if gate:
            return gate
        from .client_brand import brand_kit
        return jsonify(brand_kit(request.args.get("name", ""),
                                 request.args.get("domain", "")))

    @app.route("/api/client/work")
    def api_client_work():
        """Everything the Hub has made for this client, newest first."""
        gate = _require_api()
        if gate:
            return gate
        from .client_brand import work_log
        limit = clamp_int(request.args.get("limit"), 50, 1, 200)
        return jsonify(work_log(request.args.get("name", ""), limit))

    @app.route("/api/client/brand/push-to-suite", methods=["POST"])
    def api_brand_push():
        """Send the brand guide into the client's Smart 1 Suite sub-account."""
        gate = _require_api()
        if gate:
            return gate
        from .client_brand import brand_guide_payload
        body = request.get_json(silent=True) or {}
        client = str(body.get("name") or "")
        payload = brand_guide_payload(client, str(body.get("domain") or ""))
        if not payload.get("found"):
            return jsonify({"error": "No brand data on file for that client "
                                     "yet — run a Brandfetch lookup first."}), 400
        target = (os.environ.get("GHL_BRAND_WEBHOOK_URL") or "").strip()
        if not target:
            # Return the payload anyway so it's copy-pasteable. A missing
            # webhook shouldn't mean the work is unavailable.
            return jsonify({"ok": False, "delivered": False, "payload": payload,
                            "note": "Set GHL_BRAND_WEBHOOK_URL to deliver this "
                                    "automatically. The payload above is ready "
                                    "to paste into a Suite workflow meanwhile."})
        try:
            import requests as _rq
            r = _rq.post(target, json=payload, timeout=15)
            ok = r.ok
        except Exception:  # noqa: BLE001
            ok = False
        audit.log("brand", "pushed_to_suite", actor=current_user(),
                  client=client, ok=ok)
        return jsonify({"ok": ok, "delivered": ok, "payload": payload})

    @app.route("/api/client/context")
    def api_client_context():
        """Merged client record for prefilling any form in the Hub."""
        gate = _require_api()
        if gate:
            return gate
        from .client_context import context
        return jsonify(context(request.args.get("name", ""),
                               request.args.get("domain", "")))

    # NOT /sites/match: DispatcherMiddleware owns the whole /sites prefix and
    # forwards it to the Sites Admin app, so a hub route under it is never
    # reached — it 404s (or 503s when that module is down). Anything the hub
    # app serves has to live outside a mounted prefix.
    @app.route("/tools/sites-match")
    def page_sites_match():
        gate = _require_page()
        if gate:
            return gate
        return render_template("sites_match.html", user=current_user(),
                               active="sites")

    @app.route("/api/sites-match")
    def api_sites_match():
        """Propose a client for every unlinked Simvoly project. Read-only."""
        gate = _require_api()
        if gate:
            return gate
        from .sites_match import suggest
        return jsonify(suggest())

    @app.route("/api/sites-match/apply", methods=["POST"])
    def api_sites_match_apply():
        """Write only the matches a human accepted."""
        gate = _require_api()
        if gate:
            return gate
        from .sites_match import apply as apply_matches
        body = request.get_json(silent=True) or {}
        return jsonify(apply_matches(body.get("matches") or [],
                                     actor=current_user() or ""))

    @app.route("/api/db/urls")
    def api_db_urls():
        """Clients with no usable URL, and one domain filed under two names."""
        gate = _require_api()
        if gate:
            return gate
        from .client_context import url_audit
        return jsonify(url_audit())

    @app.route("/api/client/by-url")
    def api_client_by_url():
        """Resolve a client from a URL, whatever the name is filed as."""
        gate = _require_api()
        if gate:
            return gate
        from .client_context import resolve_by_url
        return jsonify(resolve_by_url(request.args.get("url", "")))

    @app.route("/api/db/structure")
    def api_db_structure():
        """Where client data lives, and where it can drift apart."""
        gate = _require_api()
        if gate:
            return gate
        from .client_context import structure_report
        return jsonify(structure_report())

    @app.route("/api/qb/health")
    def api_qb_health():
        """Why the QuickBooks connection may not be holding."""
        gate = _require_api()
        if gate:
            return gate
        try:
            from . import quickbooks as qb
            return jsonify(qb.health())
        except Exception as exc:  # noqa: BLE001
            return jsonify({"ok": False, "problems": [str(exc)]}), 200

    @app.route("/api/seo/llms-txt")
    def api_llms_txt_build():
        """Draft an llms.txt for a client from what the Hub already knows."""
        gate = _require_api()
        if gate:
            return gate
        from .llms_txt import build, load
        client = request.args.get("client", "")
        if request.args.get("saved") == "1":
            return jsonify({"client": client, "text": load(client)})
        return jsonify(build(client))

    @app.route("/api/seo/llms-txt", methods=["POST"])
    def api_llms_txt_save():
        gate = _require_api()
        if gate:
            return gate
        from .llms_txt import save
        body = request.get_json(silent=True) or {}
        client = str(body.get("client") or "")
        text = str(body.get("text") or "")
        if not client or not text.strip():
            return jsonify({"error": "Client and text are both required."}), 400
        if "NEED " in text:
            return jsonify({"error": "This still contains NEED placeholders. "
                                     "Fill them in first — a file with gaps is "
                                     "worse than none, because a model treats "
                                     "the whole thing as authoritative."}), 400
        return jsonify(save(client, text))

    @app.route("/llms/<slug>.txt")
    def public_llms_txt(slug):
        """Serve the approved file publicly, as plain text.

        Deliberately no login: the point is that an AI system can fetch it.
        Ideally this lives at the client's own domain root as /llms.txt — this
        URL is what you use until it can.
        """
        import re as _re
        from . import seo
        from .llms_txt import load

        def slugify(v):
            return _re.sub(r"[^a-z0-9]+", "-", str(v or "").lower()).strip("-")

        want = slugify(slug)
        try:
            from . import clients_registry
            names = [c.get("name", "") for c in clients_registry.all_clients()]
        except Exception:  # noqa: BLE001
            names = []
        for name in names:
            if name and slugify(name) == want:
                text = load(name)
                if text:
                    return app.response_class(
                        text, mimetype="text/plain; charset=utf-8")
        return app.response_class("Not found.\n", status=404,
                                  mimetype="text/plain; charset=utf-8")

    @app.route("/api/suite/blog/access")
    def api_blog_access():
        """Which blogs scopes the Suite token actually has."""
        gate = _require_api()
        if gate:
            return gate
        from .ghl_blog import check_access, BlogError
        try:
            return jsonify(check_access())
        except BlogError as exc:
            return jsonify({"ok": False, "problem": str(exc)}), 200

    @app.route("/api/suite/blog/publish-llms", methods=["POST"])
    def api_blog_publish_llms():
        """Publish a client's llms.txt to Suite as a blog post."""
        gate = _require_api()
        if gate:
            return gate
        from .ghl_blog import publish_llms_txt, BlogError
        from .llms_txt import load
        body = request.get_json(silent=True) or {}
        client = str(body.get("client") or "")
        text = str(body.get("text") or "") or load(client)
        if not client or not text.strip():
            return jsonify({"error": "Save the file before publishing it."}), 400
        if "NEED " in text:
            return jsonify({"error": "This still has NEED placeholders — fill "
                                     "them in before publishing."}), 400
        try:
            out = publish_llms_txt(client, text,
                                   post_id=str(body.get("post_id") or ""),
                                   status=str(body.get("status") or "PUBLISHED"))
        except BlogError as exc:
            return jsonify({"error": str(exc)}), 400
        # Remember the URL so Client 360 can link to it.
        try:
            from . import seo
            store = seo.load_store(client) or {}
            rec = store.get("llms_txt") or {}
            rec.update({"suite_url": out.get("url"), "post_id": out.get("post_id")})
            store["llms_txt"] = rec
            seo.save_store(client, store)
        except Exception:  # noqa: BLE001
            pass
        return jsonify(out)

    @app.route("/api/client/website-registry")
    def api_website_registry():
        """GA, GTM, platform, go-live and H&M fee from Knack object_153."""
        gate = _require_api()
        if gate:
            return gate
        from .knack_websites import enrich
        return jsonify(enrich(request.args.get("name", ""),
                              request.args.get("domain", "")))

    @app.route("/api/client/analytics-ids")
    def api_analytics_ids():
        """GA and GTM from BOTH Knack and Google, with whether they agree."""
        gate = _require_api()
        if gate:
            return gate
        from .analytics_ids import compare
        return jsonify(compare(request.args.get("name", ""),
                               request.args.get("domain", "")))

    @app.route("/api/qa/analytics-ids")
    def api_analytics_audit():
        """Every client where the two sources disagree, or we lack access."""
        gate = _require_api()
        if gate:
            return gate
        from .analytics_ids import audit_all
        return jsonify(audit_all())

    @app.route("/api/scans/stuck")
    def api_scans_stuck():
        """How many scans are stuck, and how long they've been there.

        Surfaced on Diagnostics because a stalled scan is an operational
        problem, not something you'd think to go looking for on the Scans
        page — it looks like work in progress until somebody counts.
        """
        gate = _require_api()
        if gate:
            return gate
        try:
            from modules.scans.app import Scan, SessionLocal
            from datetime import datetime, timedelta, timezone
            db = SessionLocal()
            try:
                rows = db.query(Scan).filter(Scan.status == "running").all()
                now = datetime.now(timezone.utc)
                buckets = {"under_15m": 0, "15m_to_1h": 0, "over_1h": 0,
                           "unresolvable": 0}
                oldest = None
                for r in rows:
                    c = r.created_at
                    if c is not None and c.tzinfo is None:
                        c = c.replace(tzinfo=timezone.utc)
                    age = (now - c).total_seconds() / 60 if c else 0
                    oldest = max(oldest or 0, age)
                    if not r.insites_report_id and age > 30:
                        buckets["unresolvable"] += 1
                    elif age < 15:
                        buckets["under_15m"] += 1
                    elif age < 60:
                        buckets["15m_to_1h"] += 1
                    else:
                        buckets["over_1h"] += 1
            finally:
                db.close()
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"Scans unavailable ({type(exc).__name__})."}), 200
        total = sum(buckets.values())
        return jsonify({
            "running": total, "buckets": buckets,
            "oldest_minutes": round(oldest) if oldest else 0,
            "state": ("error" if buckets["unresolvable"] else
                      "warn" if buckets["over_1h"] else "ok"),
            "advice": ("Scans with no Insites report id can never resolve — "
                       "they were started before the callback fix. Clear them "
                       "and re-run."
                       if buckets["unresolvable"] else
                       "Some scans have been running over an hour. Insites "
                       "audits normally take one to four minutes."
                       if buckets["over_1h"] else
                       "Nothing stalled."),
        })

    @app.route("/api/scheduler")
    def api_scheduler():
        """What the background jobs are doing."""
        gate = _require_api()
        if gate:
            return gate
        from . import scheduler as _sched
        out = _sched.status(app)
        out["boot_error"] = app.config.get("HUB_SCHEDULER_BOOT_ERROR")
        return jsonify(out)

    @app.route("/api/seo/schema-questions")
    def api_schema_questions():
        """The full question set, answered where possible."""
        gate = _require_api()
        if gate:
            return gate
        from .schema_questions import build
        return jsonify(build(request.args.get("client", ""),
                             use_ai=request.args.get("ai", "1") != "0"))

    @app.route("/api/seo/schema-questions/regenerate", methods=["POST"])
    def api_schema_regenerate():
        """Ask AI again for one question — the New AI button."""
        gate = _require_api()
        if gate:
            return gate
        from .schema_questions import regenerate_one
        body = request.get_json(silent=True) or {}
        return jsonify(regenerate_one(str(body.get("client") or ""),
                                      str(body.get("key") or "")))

    @app.route("/api/seo/schema-questions", methods=["POST"])
    def api_schema_answers_save():
        """Save approved and edited answers."""
        gate = _require_api()
        if gate:
            return gate
        from .schema_questions import save_answers, can_approve
        body = request.get_json(silent=True) or {}
        client = str(body.get("client") or "")
        if not client:
            return jsonify({"error": "No client given."}), 400
        out = save_answers(client, body.get("answers") or {}, current_user() or "")
        out.update(can_approve(client))
        return jsonify(out)

    @app.route("/api/client/utm")
    def api_client_utm():
        """Tracked links built for this client.

        Read from the UTM Builder's own store rather than duplicating them —
        two copies of a link is how one ends up stale and the other gets used.
        """
        gate = _require_api()
        if gate:
            return gate
        name = (request.args.get("name") or "").strip().lower()
        try:
            from modules.utm_builder.app import load_links
            rows = [r for r in (load_links() or [])
                    if str(r.get("client") or "").strip().lower() == name]
        except Exception as exc:  # noqa: BLE001
            return jsonify({"links": [], "error": f"{type(exc).__name__}"}), 200
        rows.sort(key=lambda r: str(r.get("created") or ""), reverse=True)
        return jsonify({"client": name, "count": len(rows), "links": rows[:40]})

    @app.route("/api/seo/blogs/image", methods=["POST"])
    def api_blog_image():
        """Generate, approve or delete a post's featured image."""
        gate = _require_api()
        if gate:
            return gate
        from . import blog_images as BI
        body = request.get_json(silent=True) or {}
        client = str(body.get("client") or "")
        pid = body.get("id")
        action = str(body.get("action") or "generate")
        actor = current_user() or ""
        if not client or pid is None:
            return jsonify({"error": "Client and post id are both required."}), 400
        if action == "approve":
            return jsonify(BI.approve(client, pid, actor))
        if action in ("delete", "reject"):
            return jsonify(BI.reject(client, pid, actor))
        return jsonify(BI.generate(client, pid, str(body.get("extra") or ""), actor))

    @app.route("/api/seo/blogs/image-status")
    def api_blog_image_status():
        gate = _require_api()
        if gate:
            return gate
        from . import blog_images as BI
        return jsonify(BI.status(request.args.get("client", "")))

    @app.route("/api/knack/products")
    def api_knack_products():
        """Live IO products for a client, with how fresh the data is."""
        gate = _require_api()
        if gate:
            return gate
        from .knack_products import for_client, status
        name = request.args.get("name", "")
        if not name:
            return jsonify(status())
        return jsonify(for_client(name))

    @app.route("/api/knack/products/refresh", methods=["POST"])
    def api_knack_products_refresh():
        gate = _require_api()
        if gate:
            return gate
        from .knack_products import refresh
        return jsonify(refresh())

    @app.route("/api/client/forms")
    def api_client_forms():
        """Suite forms with submissions, against the previous period."""
        gate = _require_api()
        if gate:
            return gate
        from .ghl_forms import summary
        return jsonify(summary(request.args.get("name", ""),
                               request.args.get("location", ""),
                               request.args.get("period", "this_month")))

    @app.route("/creative")
    def page_creative():
        """Creative tools, mirroring the Tools index."""
        gate = _require_page()
        if gate:
            return gate
        return render_template("creative.html", user=current_user(),
                               active="tools")

    @app.route("/api/scans/click-thru-domains")
    def api_click_thru_domains():
        """Root domains taken from click-thru URLs on live products.

        These are clients the bulk scanner used to skip for having no website
        on file, while their live campaigns pointed at one the whole time.
        """
        gate = _require_api()
        if gate:
            return gate
        from .knack_products import scan_domains
        return jsonify(scan_domains(request.args.get("client", "")))

    @app.route("/api/io/prefill")
    def api_io_prefill():
        """Start an IO from a client, their last IO, or a proposal."""
        gate = _require_api()
        if gate:
            return gate
        from . import io_prefill
        client = request.args.get("client", "")
        mode = request.args.get("mode", "new")
        if mode == "renewal":
            return jsonify(io_prefill.from_last_io(client))
        if mode == "creative":
            return jsonify(io_prefill.creative_for(client))
        return jsonify(io_prefill.from_client(client))

    @app.route("/api/io/from-proposal", methods=["POST"])
    def api_io_from_proposal():
        """Read a proposal — uploaded now, or already on the client — for an IO.

        Works without a client, so a prospect's proposal can start an IO
        before they exist in the system.
        """
        gate = _require_api()
        if gate:
            return gate
        from . import io_prefill
        client = (request.form.get("client") or "").strip()
        text, name = "", ""
        up = request.files.get("file")
        if up and up.filename:
            name = up.filename
            raw = up.read(8 * 1024 * 1024)
            text = _read_document(raw, name)
        else:
            body = request.get_json(silent=True) or {}
            client = client or str(body.get("client") or "")
            name = str(body.get("filename") or "")
            text = str(body.get("text") or "")
            if not text and client and name:
                text = _proposal_text_for(client, name)
        if not text.strip():
            return jsonify({"error": "Couldn't read any text from that "
                                     "proposal. If it's a scanned PDF there's "
                                     "no text layer to read."}), 400
        return jsonify(io_prefill.from_proposal(client, text, name))

    @app.route("/api/spec/<source>")
    def api_spec(source):
        """A campaign spec from a client, their last IO, or a proposal.

        One shape shared by the Proposal Builder and the IO Builder, so a
        proposal converts by loading rather than by retyping.
        """
        gate = _require_api()
        if gate:
            return gate
        from . import campaign_spec as CS
        client = request.args.get("client", "")
        if source == "last-io":
            spec = CS.from_last_io(client)
            if not spec:
                return jsonify({"error": "No previous IO for that client."}), 404
        elif source == "proposal":
            spec = CS.from_proposal_text(request.args.get("text", ""), client)
        else:
            spec = CS.from_client(client)
        out = spec.to_dict()
        out["ready_for_io"] = spec.ready_for_io()
        return jsonify(out)

    @app.route("/api/spec/to-io", methods=["POST"])
    def api_spec_to_io():
        """Convert a spec into what the IO intake consumes."""
        gate = _require_api()
        if gate:
            return gate
        from . import campaign_spec as CS
        body = request.get_json(silent=True) or {}
        spec = CS.CampaignSpec.from_dict(body.get("spec") or {})
        return jsonify(CS.to_io_payload(spec))

    @app.route("/api/client/suite-match")
    def api_suite_match():
        """Suite sub-accounts that look like this client.

        The card offered "Add to Smart 1 Suite" whether or not an account
        already existed under a slightly different name — which is how a
        client ends up with two sub-accounts and their history split across
        both. Search first, offer to attach, and only offer to create when
        nothing plausible comes back.
        """
        gate = _require_api()
        if gate:
            return gate
        name = (request.args.get("name") or "").strip()
        if not name:
            return jsonify({"matches": [], "searched": ""})
        import re as _re

        def norm(v):
            v = _re.sub(r"\b(llc|inc|ltd|co|corp|company|the|dba)\b", " ",
                        str(v or "").lower())
            return _re.sub(r"[^a-z0-9]+", "", v)

        want = norm(name)
        # Try the distinctive part too — "Icon Solar Power, LLC" should find
        # a sub-account called just "Icon Solar".
        terms = [name] + ([" ".join(name.split()[:2])] if len(name.split()) > 2 else [])
        seen, matches = set(), []
        try:
            from modules.suite_panel.app import ghl, _env
            for term in terms:
                data = ghl("/locations/search",
                           query={"companyId": _env("GHL_COMPANY_ID"),
                                  "limit": "10", "query": term}) or {}
                for loc in (data.get("locations") or []):
                    lid = loc.get("id") or loc.get("_id")
                    if not lid or lid in seen:
                        continue
                    seen.add(lid)
                    ln = loc.get("name") or ""
                    n = norm(ln)
                    exact = n == want
                    close = bool(n) and (n in want or want in n)
                    if exact or close:
                        matches.append({
                            "id": lid, "name": ln,
                            "website": loc.get("website") or "",
                            "confidence": "exact" if exact else "close",
                            "why": ("Name matches once LLC/Inc are ignored."
                                    if exact else
                                    f'"{ln}" looks like a variation of this client.'),
                        })
        except Exception as exc:  # noqa: BLE001
            return jsonify({"matches": [], "error": f"{type(exc).__name__}",
                            "searched": name}), 200
        matches.sort(key=lambda m: 0 if m["confidence"] == "exact" else 1)
        return jsonify({
            "searched": name, "matches": matches, "count": len(matches),
            "note": ("Attach one of these rather than creating a second "
                     "sub-account — a duplicate splits the client's history."
                     if matches else
                     "Nothing in Suite looks like this client. Search the full "
                     "list before creating one."),
        })

    @app.route("/sales/leads")
    def page_leads():
        """One panel for every lead, whatever produced it."""
        gate = _require_page()
        if gate:
            return gate
        return render_template("leads.html", user=current_user(), active="leads")

    @app.route("/api/leads")
    def api_leads():
        gate = _require_api()
        if gate:
            return gate
        from . import leads
        return jsonify(leads.listing(
            days=clamp_int(request.args.get("days"), 30, 1, 730),
            source=request.args.get("source", ""),
            page=request.args.get("page", ""),
            undelivered_only=request.args.get("undelivered") == "1"))

    @app.route("/api/leads/capture", methods=["POST"])
    def api_leads_capture():
        """Where every landing page and calculator posts.

        Unauthenticated on purpose — these come from public pages. It stores
        before it forwards, so a Suite outage can't destroy a lead.
        """
        from . import leads
        body = request.get_json(silent=True) or request.form.to_dict() or {}
        src = str(body.get("source") or "").strip()

        ip = leads.client_ip(request)
        allowed, retry_after = leads.rate_check(ip)
        if not allowed:
            # Recorded, because the number that stops a script is also the
            # number that could turn away a busy office sharing one address.
            # If real submissions start showing up here, raise
            # LEADS_RATE_LIMIT — a turned-away lead costs more than spam.
            audit.log("leads", "rate_limited", ip=ip, source=src[:60],
                      page=str(body.get("page") or "")[:120])
            return jsonify({
                "ok": False,
                "error": "Too many submissions from this connection. "
                         "Please try again shortly.",
            }), 429, {"Retry-After": str(retry_after)}

        if not src:
            return jsonify({"ok": False, "error": "source is required."}), 400
        fields = body.get("fields") if isinstance(body.get("fields"), dict) else {
            k: v for k, v in body.items()
            if k not in ("source", "page", "pdf_url", "client", "meta")}
        if not (fields.get("email") or fields.get("phone")):
            return jsonify({"ok": False,
                            "error": "An email or phone is required."}), 400
        return jsonify(leads.capture_and_deliver(
            src, str(body.get("page") or ""), fields,
            str(body.get("pdf_url") or ""), str(body.get("client") or ""),
            body.get("meta") if isinstance(body.get("meta"), dict) else None))

    @app.route("/api/leads/retry", methods=["POST"])
    def api_leads_retry():
        gate = _require_api()
        if gate:
            return gate
        from . import leads
        return jsonify(leads.retry_undelivered())

    @app.route("/api/rate-card")
    def api_rate_card():
        """The rate card, so the proposal quotes what the IO enforces."""
        gate = _require_api()
        if gate:
            return gate
        from . import rate_card as rc
        term = request.args.get("q", "")
        if term:
            return jsonify({"products": rc.search(term)})
        return jsonify({"products": rc.products(),
                        "categories": rc.categories(),
                        "drift": rc.check_drift(),
                        # So a caller can tell "no products" from "couldn't
                        # read the card" — they look the same otherwise.
                        "source": rc.status()})

    @app.route("/api/rate-card/plan", methods=["POST"])
    def api_rate_card_plan():
        """Cost a set of products: delivery per line, plus the IO's guardrails.

        This is what makes the proposal show a live breakdown instead of a
        blank page until Generate.
        """
        gate = _require_api()
        if gate:
            return gate
        from . import rate_card as rc
        items = (request.get_json(silent=True) or {}).get("items") or []
        lines, monthly = [], 0.0
        for i in items:
            p = rc.find(str(i.get("product") or "")) or {}
            budget = float(i.get("monthly") or 0)
            monthly += budget
            lines.append({**i, "listed_rate": p.get("listed_rate", ""),
                          "category": p.get("category", ""),
                          "requirements": p.get("requirements", ""),
                          "timeline": p.get("timeline", ""),
                          "delivery": rc.estimate_delivery(p, budget)})
        checks = rc.guardrails(items)
        return jsonify({
            "lines": lines,
            "monthly_total": round(monthly, 2),
            "annual_total": round(monthly * 12, 2),
            "guardrails": checks,
            "blocked": any(c["level"] == "block" for c in checks),
            "note": ("This plan can't be written as an IO as it stands."
                     if any(c["level"] == "block" for c in checks) else
                     "Within the rate card."),
        })

    @app.route("/api/qa/dashboard/<action>", methods=["POST"])
    def api_qa_dashboard(action):
        """Add a dashboard URL, or skip a client that doesn't need one."""
        gate = _require_api()
        if gate:
            return gate
        from . import qa
        body = request.get_json(silent=True) or {}
        client = str(body.get("client") or "").strip()
        if not client:
            return jsonify({"error": "client is required."}), 400
        actor = current_user() or ""
        if action == "skip":
            audit.log("qa", "dashboard_skipped", actor=actor, client=client,
                      reason=str(body.get("reason") or ""))
            return jsonify(qa.skip_dashboard(client, actor,
                                             str(body.get("reason") or "")))
        if action == "unskip":
            audit.log("qa", "dashboard_unskipped", actor=actor, client=client)
            return jsonify(qa.unskip_dashboard(client))
        if action == "add":
            from . import knack_api
            url = str(body.get("url") or "").strip()
            out = knack_api.set_dashboard_url(client, url)
            audit.log("qa", "dashboard_added", actor=actor, client=client,
                      ok=out.get("ok"), updated=out.get("updated"))
            return jsonify(out)
        return jsonify({"error": "Unknown action."}), 400

    @app.route("/api/qa/dashboard-skips")
    def api_qa_dashboard_skips():
        gate = _require_api()
        if gate:
            return gate
        from . import qa
        return jsonify(qa.skipped_dashboards())

    @app.route("/api/qa/form-summary/<opp_id>")
    def api_qa_form_summary(opp_id):
        """Everything the submitter actually filled in, for one request.

        The report shows one line per request because a queue has to be
        scannable. This returns the whole form for the moment someone needs
        the detail, without sending them into GoHighLevel to find it.
        """
        gate = _require_api()
        if gate:
            return gate
        try:
            from modules.suite_panel.app import ghl
            data = ghl(f"/opportunities/{opp_id}") or {}
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"Couldn't reach Smart 1 Suite "
                                     f"({type(exc).__name__})."}), 200
        opp = data.get("opportunity") or data
        contact = opp.get("contact") or {}
        fields = []
        for cf in (opp.get("customFields") or []):
            label = str(cf.get("name") or cf.get("id") or "").strip()
            value = cf.get("fieldValue")
            if isinstance(value, list):
                value = ", ".join(str(v) for v in value)
            value = str(value or "").strip()
            if label and value:
                fields.append({"label": label, "value": value[:2000]})
        return jsonify({
            "name": opp.get("name", ""),
            "contact": {"name": contact.get("name", ""),
                        "email": contact.get("email", ""),
                        "phone": contact.get("phone", ""),
                        "company": contact.get("companyName", "")},
            "status": opp.get("status", ""),
            "created": str(opp.get("createdAt") or "")[:10],
            "fields": fields,
            "note": ("" if fields else
                     "This request has no custom form fields recorded — the "
                     "submitter may have used a different form."),
        })

    @app.route("/api/providers")
    def api_providers():
        """Every provider, configured or not, with what breaks when it isn't.

        This is the answer to "is Cloudinary actually set?" — a question that
        went unanswered long enough for the whole v1.6.0 tool set to run
        degraded in production without anyone noticing.
        """
        gate = _require_api()
        if gate:
            return gate
        from .config import settings as _cfg
        rows = _cfg.status()
        return jsonify({
            "providers": rows,
            "missing_required": _cfg.missing_required(),
            "ok": not _cfg.missing_required(),
            "degraded": [r["name"] for r in rows if r["state"] == "warn"],
        })

    # ---------------- clients: one list from every source ----------------
    @app.route("/api/clients/search")
    def api_clients_search():
        gate = _require_api()
        if gate:
            return gate
        from . import clients_registry
        rows = clients_registry.search_clients(request.args.get("q", ""),
                                               limit=clamp_int(request.args.get("limit"), 12, 1, 500))
        return jsonify({"clients": rows})

    @app.route("/api/clients/house", methods=["GET", "POST"])
    def api_house_clients_hub():
        gate = _require_api()
        if gate:
            return gate
        from . import clients_registry
        if request.method == "GET":
            return jsonify({"clients": clients_registry.house_clients()})
        body = request.get_json(silent=True) or {}
        if body.get("delete"):
            ok = clients_registry.delete_house_client(str(body.get("slug") or ""))
            clients_registry.all_clients(refresh=True)
            return jsonify({"ok": ok, "clients": clients_registry.house_clients()})
        try:
            row = clients_registry.add_house_client(
                body.get("name", ""), body.get("url", ""), body.get("notes", ""),
                actor=current_user() or "")
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        clients_registry.all_clients(refresh=True)
        audit.log("hub", "house_client_added", actor=current_user(),
                  detail=row["name"])
        return jsonify({"ok": True, "client": row,
                        "clients": clients_registry.house_clients()})

    def _hub_user():
        """The signed-in user as a rich object, for the help/demo layer."""
        try:
            from . import identity
            u = identity.user_from_environ(request.environ)
            if u:
                return u
            name = current_user()
            return identity.User(email="", name=name, via="password") if name else None
        except Exception:  # noqa: BLE001
            return None

    def _login_cookie(user, nxt="/"):
        from . import identity
        if not nxt.startswith("/"):
            nxt = "/"
        resp = make_response(redirect(nxt))
        secure = (os.environ.get("NODE_ENV") == "production"
                  or os.environ.get("FLASK_ENV") == "production")
        # Both cookies: the rich one for v7 features, the legacy one so every
        # existing @requires_login check keeps working untouched.
        resp.set_cookie(identity.COOKIE_NAME, identity.issue_cookie(user),
                        max_age=identity.SESSION_TTL_SECONDS, httponly=True,
                        samesite="Lax", secure=secure)
        resp.set_cookie(auth.COOKIE_NAME, auth.issue_cookie_value(user.name),
                        max_age=auth.SESSION_TTL_SECONDS, httponly=True,
                        samesite="Lax", secure=secure)
        return resp

    @app.route("/auth/google")
    def auth_google():
        from . import identity
        if not identity.google_configured():
            return redirect("/login?error=google_not_configured")
        state = identity.new_state()
        redirect_uri = request.url_root.rstrip("/") + "/auth/google/callback"
        resp = make_response(redirect(identity.authorize_url(redirect_uri, state)))
        # State is held in a short-lived cookie and compared on the way back —
        # without it the callback accepts a code an attacker supplies.
        resp.set_cookie("s1_oauth_state", state, max_age=600, httponly=True,
                        samesite="Lax")
        resp.set_cookie("s1_oauth_next", request.args.get("next", "/"),
                        max_age=600, httponly=True, samesite="Lax")
        return resp

    @app.route("/auth/google/callback")
    def auth_google_callback():
        from . import identity
        sent = request.cookies.get("s1_oauth_state") or ""
        got = request.args.get("state") or ""
        if not sent or sent != got:
            audit.log("auth", "login_rejected", reason="state_mismatch")
            return render_template("login.html", next="/",
                                   error="That sign-in link expired. Try again."), 400
        code = request.args.get("code") or ""
        if not code:
            return redirect("/login")
        try:
            user = identity.complete_google_login(
                code, request.url_root.rstrip("/") + "/auth/google/callback")
        except identity.LoginRejected as exc:
            return render_template("login.html", next="/", error=str(exc)), 403
        nxt = request.cookies.get("s1_oauth_next") or "/"
        resp = _login_cookie(user, nxt)
        resp.delete_cookie("s1_oauth_state")
        resp.delete_cookie("s1_oauth_next")
        return resp

    @app.route("/auth/demo", methods=["POST"])
    def auth_demo():
        from . import identity
        try:
            user = identity.complete_demo_login(
                request.form.get("code") or "",
                request.form.get("name") or "")
        except identity.LoginRejected as exc:
            return render_template("login.html", next="/", error=str(exc)), 403
        return _login_cookie(user, "/")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        """The single sign-in page.

        Tries a real user account first, then falls back to the legacy shared
        PANEL_PASSWORD. The fallback stays until every account is migrated —
        removing it while somebody still depends on it locks them out of their
        own tool with no way back in.

        Unlike /signin, this deliberately tells you when an email has no
        account and points at sign-up. That does leak which addresses are
        registered, which is normally worth avoiding — but sign-up is already
        restricted to @smart1marketing.com, so the only thing an attacker
        learns is which colleagues have signed up yet. For an internal tool
        that trade is worth making; being told "wrong email or password" when
        you simply haven't registered is how people give up.
        """
        from . import identity
        google_on = (os.environ.get("HUB_GOOGLE_LOGIN", "").lower()
                     in {"1", "true", "yes", "on"}) and identity.google_configured()

        def page(error=None, offer_signup=False, last_email="", code=200):
            return render_template(
                "login.html", next=request.form.get("next") or request.args.get("next", "/"),
                error=error, offer_signup=offer_signup, last_email=last_email,
                google_enabled=google_on), code

        if request.method == "GET":
            if current_user():
                return redirect(request.args.get("next") or "/")
            return page()[0]

        # Last hop, not the first: the client-supplied first entry in
        # X-Forwarded-For is spoofable, and this exact mistake was flagged in
        # three separate apps during the suite audit.
        fwd = request.headers.get("X-Forwarded-For", "")
        ip = (fwd.split(",")[-1].strip() if fwd else (request.remote_addr or "?"))
        wait = auth.throttle_check(ip)
        if wait:
            return page(f"Too many attempts. Try again in "
                        f"{max(1, wait // 60)} minute(s).", code=429)

        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        nxt = request.form.get("next") or "/"
        if not nxt.startswith("/"):
            nxt = "/"

        # ---- 1. the shared password always works, checked FIRST ----
        #
        # Order matters and this is the bug that locked Todd out of his own
        # Hub: the three founding super admins are SEEDED as rows before
        # anyone sets a password, so by_email() finds them, the account branch
        # takes over, and the shared password is never reached. Checking the
        # shared password first means it is a genuine way back in rather than
        # one that silently stops working the moment accounts are seeded.
        shared_ok = bool(auth.panel_password()) and auth.check_password(password)

        # ---- 2. real user account ----
        account = None
        if not shared_ok:
            try:
                from . import users as _users
                from .users_routes import _login_response
                account = _users.by_email(email) if email else None
                if account is not None and account.password_hash:
                    try:
                        user = _users.authenticate(email, password)
                        auth.throttle_reset(ip)
                        return _login_response(user, nxt)
                    except _users.UserError as exc:
                        auth.throttle_fail(ip)
                        return page(str(exc), last_email=email, code=401)
            except ImportError:
                account = None

        # ---- 3. legacy shared password (emergency access) ----
        if shared_ok:
            auth.throttle_reset(ip)
            actor = email or "Shared login"
            audit.log("hub", "login_shared_password", actor=actor, ip=ip)
            resp = make_response(redirect(nxt))
            resp.set_cookie(
                auth.COOKIE_NAME, auth.issue_cookie_value(actor),
                max_age=auth.SESSION_TTL_SECONDS, httponly=True, samesite="Lax",
                secure=os.environ.get("NODE_ENV") == "production"
                or os.environ.get("FLASK_ENV") == "production")
            return resp

        # ---- 3. no account, and not the shared password ----
        auth.throttle_fail(ip)
        audit.log("hub", "login_failed", actor=email or "?", ip=ip)
        if email and account is None:
            return page(f"There's no account for {email} yet.",
                        offer_signup=True, last_email=email, code=401)
        return page("That email and password don't match.",
                    last_email=email, code=401)

    @app.route("/logout")
    def logout():
        resp = make_response(redirect("/login"))
        resp.delete_cookie(auth.COOKIE_NAME)
        return resp

    def _require_page():
        """Redirect helper for HTML pages."""
        if not current_user():
            return redirect("/login?next=" + request.path)
        return None

    # ---------------- shell pages ----------------
    @app.route("/")
    def dashboard():
        gate = _require_page()
        if gate:
            return gate
        return render_template("dashboard.html", user=current_user(), modules=MODULES, active="dashboard")

    @app.route("/client360")
    def client360():
        gate = _require_page()
        if gate:
            return gate
        return render_template("client360.html", user=current_user(), modules=MODULES,
                               active="c360", q=request.args.get("q", ""))

    @app.route("/tools")
    def tools():
        gate = _require_page()
        if gate:
            return gate
        return render_template("tools.html", user=current_user(), modules=MODULES, active="tools")

    # ---------------- SEO section ----------------
    @app.route("/seo")
    def seo_home():
        gate = _require_page()
        if gate:
            return gate
        return render_template("seo.html", user=current_user(), modules=MODULES, active="seo")

    @app.route("/seo/client")
    def seo_client_page():
        gate = _require_page()
        if gate:
            return gate
        name = (request.args.get("name") or "").strip()
        if not name:
            return redirect("/seo")
        return render_template("seo_client.html", user=current_user(), modules=MODULES,
                               active="seo", client=name)

    @app.route("/seo/webmaster")
    def seo_webmaster_page():
        gate = _require_page()
        if gate:
            return gate
        return render_template("seo_webmaster.html", user=current_user(),
                               modules=MODULES, active="seo")

    @app.route("/api/seo/tasks")
    def api_seo_tasks():
        """What has been raised for this client, and whether it can be."""
        gate = _require_api()
        if gate:
            return gate
        from . import seo_tasks, knack_api as _k
        client = (request.args.get("client") or "").strip()
        if not client:
            return jsonify({"error": "client is required."}), 400
        return jsonify({
            "counts": seo_tasks.status(client),
            "enabled": seo_tasks.enabled(),
            "knack_configured": _k.configured(),
            "due_field": seo_tasks.due_field(),
            "rules": {"faq_days": seo_tasks.DUE_DAYS_FAQ,
                      "schema_days": seo_tasks.DUE_DAYS_SCHEMA,
                      "blog_lead_days": seo_tasks.BLOG_LEAD_DAYS},
        })

    @app.route("/api/seo/webmaster")
    def api_seo_webmaster():
        """The roster only. Numbers arrive per row from /google — see
        hub/seo.webmaster_roster for why this route makes no Google call."""
        gate = _require_api()
        if gate:
            return gate
        from . import seo
        try:
            return jsonify({"clients": seo.webmaster_roster()})
        except Exception as exc:  # noqa: BLE001
            return jsonify({"clients": [], "error": str(exc)})

    @app.route("/api/seo/clients")
    def api_seo_clients():
        gate = _require_api()
        if gate:
            return gate
        from . import seo
        try:
            return jsonify({"clients": seo.seo_clients()})
        except Exception as exc:  # noqa: BLE001
            return jsonify({"clients": [], "error": str(exc)})

    @app.route("/api/seo/detail")
    def api_seo_detail():
        gate = _require_api()
        if gate:
            return gate
        from . import seo
        name = (request.args.get("name") or "").strip()
        try:
            return jsonify(seo.client_detail(name, full=bool(request.args.get("full"))))
        except Exception as exc:  # noqa: BLE001
            errors.log_exception("seo-detail", exc, path=request.path,
                                 actor=current_user() or "")
            return jsonify({"client": name, "error": str(exc)})

    @app.route("/api/seo/scan", methods=["POST"])
    def api_seo_scan():
        gate = _require_api()
        if gate:
            return gate
        from . import seo
        body = request.get_json(silent=True) or {}
        url = (body.get("url") or "").strip()
        client = (body.get("client") or "").strip()
        if not url:
            return jsonify({"error": "No URL provided."}), 400
        try:
            out = seo.scan_schema(url)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"Could not scan the site: {exc}"})
        if client:                      # cache the result on the client store
            import datetime as _dt
            from . import dates as _dates
            _now = _dt.datetime.now()
            out["at"] = _dates.fmt(_now) + _now.strftime(" %I:%M %p")
            store = seo.load_store(client)
            store["last_scan"] = out
            seo.save_store(client, store)
        audit.log("hub", "seo_scan", actor=current_user(), detail=url)
        return jsonify(out)

    @app.route("/api/seo/sitemap", methods=["POST"])
    def api_seo_sitemap():
        gate = _require_api()
        if gate:
            return gate
        from . import seo
        body = request.get_json(silent=True) or {}
        client = (body.get("client") or "").strip()
        url = (body.get("url") or "").strip()
        if not client or not url:
            return jsonify({"error": "client and url are required."}), 400
        try:
            pages = seo.sitemap_pages(url)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"Could not read the sitemap: {exc}"})
        store = seo.load_store(client)
        store["sitemap"] = pages
        store["site_url"] = url
        seo.save_store(client, store)
        done = set(store.get("pages", {}))
        return jsonify({"total": len(pages), "generated": len(done),
                        "remaining": [p for p in pages if p not in done][:10],
                        "pages": pages[:50]})

    @app.route("/api/seo/generate", methods=["POST"])
    def api_seo_generate():
        gate = _require_api()
        if gate:
            return gate
        from . import seo
        body = request.get_json(silent=True) or {}
        client = (body.get("client") or "").strip()
        if not client:
            return jsonify({"error": "client is required."}), 400
        store = seo.load_store(client)
        urls = body.get("urls")
        if not urls:
            done = set(store.get("pages", {}))
            urls = [p for p in store.get("sitemap", []) if p not in done][:10]
        if not urls:
            return jsonify({"pages": [], "questions": store.get("questions", []),
                            "done": True})
        try:
            out = seo.generate_for_pages(client, urls[:10])
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)})
        store = seo.load_store(client)
        remaining = [p for p in store.get("sitemap", []) if p not in store.get("pages", {})]
        out["remaining"] = len(remaining)
        out["total"] = len(store.get("sitemap", []))
        audit.log("hub", "seo_generate", actor=current_user(),
                  detail=f"{client}: {len(urls)} pages")
        # One ticket per page that actually got schema, not per page asked for.
        from . import seo_tasks
        done = [pg.get("url") for pg in (out.get("pages") or []) if pg.get("url")]
        out["tasks"] = seo_tasks.for_pages(client, done or urls[:10],
                                           kind="schema", actor=current_user())
        return jsonify(out)

    @app.route("/api/seo/page", methods=["POST"])
    def api_seo_page():
        gate = _require_api()
        if gate:
            return gate
        from . import seo
        body = request.get_json(silent=True) or {}
        client = (body.get("client") or "").strip()
        url = (body.get("url") or "").strip()
        if not client or not url:
            return jsonify({"error": "client and url are required."}), 400
        store = seo.load_store(client)
        page = store.setdefault("pages", {}).get(url)
        if page is None:
            return jsonify({"error": "Unknown page."}), 404
        if "schema" in body:
            sch = body["schema"]
            if isinstance(sch, str):
                try:
                    sch = json.loads(sch)
                except ValueError as exc:
                    return jsonify({"error": f"Schema is not valid JSON: {exc}"}), 400
            page["schema"] = sch
        if "approved" in body:
            page["approved"] = bool(body["approved"])
        if "posted" in body:
            page["posted"] = bool(body["posted"])
        seo.save_store(client, store)
        approved = sum(1 for p in store["pages"].values() if p.get("approved"))
        return jsonify({"ok": True, "approved_total": approved})

    @app.route("/api/seo/business", methods=["POST"])
    def api_seo_business():
        gate = _require_api()
        if gate:
            return gate
        from . import seo
        body = request.get_json(silent=True) or {}
        client = (body.get("client") or "").strip()
        if not client:
            return jsonify({"error": "client is required."}), 400
        store = seo.load_store(client)
        if isinstance(body.get("business_info"), dict):
            store.setdefault("business_info", {}).update(body["business_info"])
        if isinstance(body.get("answers"), dict):
            store.setdefault("answers", {}).update(
                {k: v for k, v in body["answers"].items() if str(v).strip()})
            store["questions"] = [q for q in store.get("questions", [])
                                  if q not in store["answers"]]
        seo.save_store(client, store)
        return jsonify({"ok": True, "questions": store.get("questions", [])})

    @app.route("/api/seo/setup", methods=["POST"])
    def api_seo_setup():
        gate = _require_api()
        if gate:
            return gate
        from . import seo
        body = request.get_json(silent=True) or {}
        client = (body.get("client") or "").strip()
        if not client:
            return jsonify({"error": "client is required."}), 400
        store = seo.load_store(client)
        setup = store.setdefault("setup", {})
        for k in ("access_method", "access_url", "login", "password",
                  "webmaster_status", "blogs_enabled", "blogs_per_month",
                  "blogs_frequency", "completed", "skipped_steps", "notes"):
            if k in body:
                setup[k] = body[k]
        seo.save_store(client, store)
        audit.log("hub", "seo_setup_saved", actor=current_user(), detail=client)
        return jsonify({"ok": True})

    @app.route("/api/seo/pages")
    def api_seo_pages():
        gate = _require_api()
        if gate:
            return gate
        from . import seo
        name = (request.args.get("name") or "").strip()
        store = seo.load_store(name)
        pages = list(store.get("pages", {}).values())
        remaining = [p for p in store.get("sitemap", []) if p not in store.get("pages", {})]
        return jsonify({"pages": pages, "total": len(store.get("sitemap", [])),
                        "remaining": len(remaining)})

    @app.route("/api/seo/checks", methods=["POST"])
    def api_seo_checks():
        gate = _require_api()
        if gate:
            return gate
        from . import seo
        body = request.get_json(silent=True) or {}
        client = (body.get("client") or "").strip()
        if not client:
            return jsonify({"error": "client is required."}), 400
        store = seo.load_store(client)
        checks = store.setdefault("checks", {})
        for k in ("schema", "listings"):
            if k in body:
                checks[k] = bool(body[k])
        if "setup" in body:
            store.setdefault("setup", {})["completed"] = bool(body["setup"])
        seo.save_store(client, store)
        audit.log("hub", "seo_checks", actor=current_user(), detail=client)
        return jsonify({"ok": True, "status": seo.client_status(store)})

    @app.route("/api/client/social")
    def api_client_social():
        gate = _require_api()
        if gate:
            return gate
        from . import seo
        name = (request.args.get("name") or "").strip()
        domain = (request.args.get("domain") or "").strip()
        social = seo.get_social(name, domain) if name else {}
        # Merge anything a site scan found. Brandfetch returns what a brand
        # publishes about itself — usually two or three profiles. A scan reads
        # the client's own pages and routinely finds more: a TikTok in the
        # footer, a LinkedIn nobody registered with Brandfetch.
        #
        # Saved values win. Someone who corrected a URL by hand should not
        # have it overwritten by the next scan.
        found_by_scan = []
        try:
            from modules.scans.app import latest_payload_for_domain
            from modules.scans.reports import social_profiles
            payload = latest_payload_for_domain(domain or name)
            for key, url in (social_profiles(payload or {}) or {}).items():
                if not str(social.get(key) or "").strip():
                    social[key] = url
                    found_by_scan.append(key)
        except Exception:  # noqa: BLE001
            pass
        return jsonify({"social": social, "from_scan": found_by_scan,
                        "note": (f"{len(found_by_scan)} profile(s) came from "
                                 f"the last site scan rather than Brandfetch."
                                 if found_by_scan else "")})

    @app.route("/api/client/social", methods=["POST"])
    def api_client_social_set():
        gate = _require_api()
        if gate:
            return gate
        from . import seo
        body = request.get_json(silent=True) or {}
        client = (body.get("client") or "").strip()
        if not client:
            return jsonify({"error": "client is required."}), 400
        social = seo.set_social(client, body.get("social") or {})
        audit.log("hub", "client_social_saved", actor=current_user(), detail=client)
        return jsonify({"ok": True, "social": social})

    # ------------- attached Google accounts (shared: SEO page + Client 360)
    @app.route("/api/client/links")
    def api_client_links():
        gate = _require_api()
        if gate:
            return gate
        from . import seo
        name = (request.args.get("name") or "").strip()
        return jsonify({"attached": seo.get_links(name) if name else {}})

    @app.route("/api/client/links", methods=["POST"])
    def api_client_links_set():
        gate = _require_api()
        if gate:
            return gate
        from . import seo
        body = request.get_json(silent=True) or {}
        client = (body.get("client") or "").strip()
        kind = (body.get("kind") or "").strip()
        if not client or kind not in seo.LINK_KINDS:
            return jsonify({"error": f"client and kind ({'|'.join(seo.LINK_KINDS)}) are required."}), 400
        att = seo.set_link(client, kind, body.get("data"),
                           remove=(body.get("remove") or "").strip())
        audit.log("hub", "client_account_attached", actor=current_user(),
                  detail=f"{client}: {kind}")
        return jsonify({"ok": True, "attached": att})

    @app.route("/api/client/profile")
    def api_client_profile():
        gate = _require_api()
        if gate:
            return gate
        from . import seo
        name = (request.args.get("name") or "").strip()
        return jsonify({"profile": seo.get_profile(name) if name else {}})

    @app.route("/api/client/profile", methods=["POST"])
    def api_client_profile_set():
        gate = _require_api()
        if gate:
            return gate
        from . import seo
        body = request.get_json(silent=True) or {}
        client = (body.get("client") or "").strip()
        if not client:
            return jsonify({"error": "client is required."}), 400
        prof = seo.set_profile(client, body)
        audit.log("hub", "client_profile_saved", actor=current_user(), detail=client)
        return jsonify({"ok": True, "profile": prof})

    @app.route("/api/client/notes", methods=["POST"])
    def api_client_notes():
        gate = _require_api()
        if gate:
            return gate
        from . import seo
        body = request.get_json(silent=True) or {}
        client = (body.get("client") or "").strip()
        text = (body.get("text") or "").strip()
        if not client or not text:
            return jsonify({"error": "client and text are required."}), 400
        prof = seo.add_note(client, text, author=current_user() or "")
        audit.log("hub", "client_note_added", actor=current_user(), detail=client)
        return jsonify({"ok": True, "profile": prof})

    @app.route("/api/client/tickets")
    def api_client_tickets():
        gate = _require_api()
        if gate:
            return gate
        from . import knack_api
        name = (request.args.get("name") or "").strip()
        website = (request.args.get("website") or "").strip()
        if not knack_api.configured():
            return jsonify({"configured": False, "tickets": []})
        try:
            return jsonify({"configured": True,
                            "tickets": knack_api.list_tickets(name, website)})
        except Exception as exc:  # noqa: BLE001
            errors.log_exception("knack-tickets", exc, path=request.path,
                                 actor=current_user() or "")
            return jsonify({"configured": True, "tickets": [], "error": str(exc)})

    @app.route("/api/client/tickets", methods=["POST"])
    def api_client_tickets_create():
        gate = _require_api()
        if gate:
            return gate
        from . import knack_api
        body = request.get_json(silent=True) or {}
        client = (body.get("client") or "").strip()
        subject = (body.get("subject") or "").strip()
        if not client or not subject:
            return jsonify({"error": "client and subject are required."}), 400
        if not knack_api.configured():
            return jsonify({"error": "Knack isn't configured — set KNACK_APP_ID and "
                                     "KNACK_API_KEY, then redeploy."}), 400
        try:
            rec = knack_api.create_ticket(
                client, (body.get("website") or "").strip(), subject,
                (body.get("description") or "").strip(),
                author=current_user() or "",
                requested_by=(body.get("requested_by") or "").strip())
        except Exception as exc:  # noqa: BLE001
            errors.log_exception("knack-tickets", exc, path=request.path,
                                 actor=current_user() or "")
            return jsonify({"error": str(exc)})
        audit.log("hub", "web_ticket_created", actor=current_user(),
                  detail=f"{client}: {subject[:60]}")
        return jsonify({"ok": True, "id": rec.get("id")})

    @app.route("/api/knack/campaign-fields")
    def api_knack_campaign_fields():
        """Live field mapping shown in the modal BEFORE anything is written."""
        gate = _require_api()
        if gate:
            return gate
        from . import knack_api
        kind = (request.args.get("kind") or "").strip()
        if kind not in ("change", "support"):
            return jsonify({"error": "kind must be change or support."}), 400
        if not knack_api.configured():
            return jsonify({"configured": False})
        try:
            info = knack_api.campaign_field_map(kind)
            return jsonify({"configured": True, **info})
        except Exception as exc:  # noqa: BLE001
            errors.log_exception("knack-campaign", exc, path=request.path,
                                 actor=current_user() or "")
            return jsonify({"configured": True, "error": str(exc)})

    @app.route("/api/knack/people")
    def api_knack_people():
        """Names from object_161 + object_109 for Requested By dropdowns."""
        gate = _require_api()
        if gate:
            return gate
        from . import knack_api
        if not knack_api.configured():
            return jsonify({"configured": False, "names": []})
        try:
            return jsonify({"configured": True, "names": knack_api.people_names()})
        except Exception as exc:  # noqa: BLE001
            errors.log_exception("knack-people", exc, path=request.path,
                                 actor=current_user() or "")
            return jsonify({"configured": True, "names": [], "error": str(exc)})

    @app.route("/api/client/campaign-request", methods=["POST"])
    def api_client_campaign_request():
        gate = _require_api()
        if gate:
            return gate
        from . import knack_api
        body = request.get_json(silent=True) or {}
        kind = (body.get("kind") or "").strip()
        client = (body.get("client") or "").strip()
        subject = (body.get("subject") or "").strip()
        if kind not in ("change", "support") or not client or not subject:
            return jsonify({"error": "kind (change|support), client and subject are required."}), 400
        if not knack_api.configured():
            return jsonify({"error": "Knack isn't configured — set KNACK_APP_ID and "
                                     "KNACK_API_KEY, then redeploy."}), 400
        try:
            rec = knack_api.create_campaign_request(
                kind, client, (body.get("campaign") or "").strip(),
                (body.get("io") or "").strip(), subject,
                (body.get("description") or "").strip(),
                author=current_user() or "",
                requested_by=(body.get("requested_by") or "").strip())
        except Exception as exc:  # noqa: BLE001
            errors.log_exception("knack-campaign", exc, path=request.path,
                                 actor=current_user() or "")
            return jsonify({"error": str(exc)})
        audit.log("hub", f"campaign_{kind}_request", actor=current_user(),
                  detail=f"{client}: {subject[:60]}")
        return jsonify({"ok": True, "id": rec.get("id")})

    @app.route("/api/client/website-hosted", methods=["POST"])
    def api_client_website_hosted():
        """Whether Smart 1 Marketing hosts this site.

        Stored through the existing website-override mechanism rather than a
        parallel store: overrides are already merged into every website dict
        on read, are already scoped per domain, and are already documented as
        hub-side corrections that never write back to Knack. A second store
        would need its own merge step and would drift.
        """
        gate = _require_api()
        if gate:
            return gate
        body = request.get_json(silent=True) or {}
        client = str(body.get("client") or "").strip()
        domain = str(body.get("domain") or "").strip()
        value = str(body.get("s1m_hosted") or "").strip().lower()
        if value not in ("", "yes", "no"):
            return jsonify({"error": "Value must be yes, no or blank."}), 400
        if not client:
            return jsonify({"error": "No client given."}), 400
        try:
            from . import seo
            seo.set_website_override(client, domain, {"s1m_hosted": value})
            audit.log("hub", "s1m_hosted_set", actor=current_user(),
                      client=client, domain=domain, value=value or "cleared")
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"Could not save ({type(exc).__name__})."}), 500
        return jsonify({"ok": True, "s1m_hosted": value})

    @app.route("/api/client/website-platform", methods=["POST"])
    def api_client_website_platform():
        """Hub-only correction of a website record's platform — Knack untouched."""
        gate = _require_api()
        if gate:
            return gate
        from . import seo
        body = request.get_json(silent=True) or {}
        client = (body.get("client") or "").strip()
        domain = (body.get("domain") or "").strip()
        platform = (body.get("platform") or "").strip()
        if not client or not domain or not platform:
            return jsonify({"error": "client, domain and platform are required."}), 400
        try:
            seo.set_website_override(client, domain, {"platform": platform})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        audit.log("hub", "website_platform_corrected", actor=current_user(),
                  detail=f"{client}: {domain} -> {platform}")
        return jsonify({"ok": True})

    @app.route("/api/websites/search")
    def api_websites_search():
        """Search the websites inventory — used to attach extra website
        records to a client (hub-only, never written back to Knack)."""
        gate = _require_api()
        if gate:
            return gate
        q = (request.args.get("q") or "").strip().lower()
        if not q:
            return jsonify({"results": []})
        out = []
        for w in knack_data.websites():
            hay = " ".join(str(w.get(k) or "") for k in ("name", "domain")).lower()
            if q in hay:
                out.append({"name": w.get("name"), "domain": w.get("domain"),
                            "platform": w.get("platform"), "status": w.get("status")})
            if len(out) >= 8:
                break
        return jsonify({"results": out})

    # ---------------- SEO blogs ----------------
    @app.route("/api/seo/blogs")
    def api_seo_blogs():
        gate = _require_api()
        if gate:
            return gate
        from . import seo
        name = (request.args.get("name") or "").strip()
        store = seo.load_store(name)
        blogs = store.get("blogs", {})
        # Archived posts stay in the store but leave the working list.
        _posts = [p for p in blogs.get("posts", []) if not p.get("archived")]
        return jsonify({"posts": _posts,
                        "archived": sum(1 for p in blogs.get("posts", [])
                                        if p.get("archived")),
                        "focus": blogs.get("focus", ""),
                        "questions": blogs.get("questions", []),
                        "frequency": store.get("setup", {}).get("blogs_frequency", ""),
                        "per_month": store.get("setup", {}).get("blogs_per_month", "")})

    @app.route("/api/seo/blogs/plan", methods=["POST"])
    def api_seo_blogs_plan():
        gate = _require_api()
        if gate:
            return gate
        from . import seo
        body = request.get_json(silent=True) or {}
        client = (body.get("client") or "").strip()
        if not client:
            return jsonify({"error": "client is required."}), 400
        try:
            out = seo.blog_plan(client, (body.get("focus") or "").strip(),
                                clamp_int(body.get("months"), 3, 1, 24),
                                (body.get("start") or "").strip())
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)})
        audit.log("hub", "seo_blog_plan", actor=current_user(),
                  detail=f"{client}: {len(out['posts'])} posts")
        from . import seo_tasks
        out["tasks"] = seo_tasks.for_posts(client, out.get("posts") or [],
                                           actor=current_user())
        return jsonify(out)

    @app.route("/api/seo/blogs/write", methods=["POST"])
    def api_seo_blogs_write():
        gate = _require_api()
        if gate:
            return gate
        from . import seo
        body = request.get_json(silent=True) or {}
        client = (body.get("client") or "").strip()
        ids = [int(i) for i in (body.get("ids") or []) if str(i).isdigit()]
        if not client or not ids:
            return jsonify({"error": "client and ids are required."}), 400
        try:
            out = seo.blog_write(client, ids)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)})
        audit.log("hub", "seo_blog_write", actor=current_user(),
                  detail=f"{client}: {len(out['written'])} posts")
        return jsonify(out)

    @app.route("/api/seo/blogs/update", methods=["POST"])
    def api_seo_blogs_update():
        gate = _require_api()
        if gate:
            return gate
        from . import seo
        body = request.get_json(silent=True) or {}
        client = (body.get("client") or "").strip()
        pid = body.get("id")
        store = seo.load_store(client)
        post = next((p for p in store.get("blogs", {}).get("posts", [])
                     if p["id"] == pid), None)
        if not client or post is None:
            return jsonify({"error": "Unknown client or post."}), 404
        if isinstance(body.get("title"), str) and body["title"].strip():
            post["title"] = body["title"].strip()
        if isinstance(body.get("content"), str):
            post["content"] = body["content"]
            if body["content"].strip():
                post["status"] = "written"
        if "posted" in body:
            post["posted"] = bool(body["posted"])
        if "archived" in body:
            # Archive hides a post from the working list without deleting it.
            # A deleted post takes its written content and its image with it,
            # which is rarely what "I'm done with this" means.
            post["archived"] = bool(body["archived"])
        if isinstance(body.get("answers"), dict):
            blogs = store.setdefault("blogs", {})
            blogs.setdefault("answers", {}).update(
                {k: v for k, v in body["answers"].items() if str(v).strip()})
            blogs["questions"] = [q for q in blogs.get("questions", [])
                                  if q not in blogs["answers"]]
        seo.save_store(client, store)
        return jsonify({"ok": True})

    @app.route("/api/seo/blogs/answers", methods=["POST"])
    def api_seo_blogs_answers():
        gate = _require_api()
        if gate:
            return gate
        from . import seo
        body = request.get_json(silent=True) or {}
        client = (body.get("client") or "").strip()
        if not client:
            return jsonify({"error": "client is required."}), 400
        store = seo.load_store(client)
        blogs = store.setdefault("blogs", {})
        if isinstance(body.get("answers"), dict):
            blogs.setdefault("answers", {}).update(
                {k: v for k, v in body["answers"].items() if str(v).strip()})
            blogs["questions"] = [q for q in blogs.get("questions", [])
                                  if q not in blogs["answers"]]
        seo.save_store(client, store)
        return jsonify({"ok": True, "questions": blogs.get("questions", [])})

    @app.route("/seo/blogs/<slug>.doc")
    def seo_blogs_doc(slug):
        gate = _require_page()
        if gate:
            return gate
        from . import seo
        match = next((c for c in seo.seo_clients() if c["slug"] == slug), None)
        name = match["client"] if match else slug.replace("-", " ")
        raw = (request.args.get("ids") or "").strip()
        ids = None
        if raw and raw.lower() != "all":
            ids = [int(x) for x in raw.split(",") if x.strip().isdigit()]
        body = seo.blogs_doc(name, ids)
        resp = make_response(body)
        resp.headers["Content-Type"] = "application/msword"
        resp.headers["Content-Disposition"] = f'attachment; filename="{slug}-blogs.doc"'
        return resp

    @app.route("/seo/blogs/<slug>/view")
    def seo_blogs_view(slug):
        gate = _require_page()
        if gate:
            return gate
        from . import seo
        match = next((c for c in seo.seo_clients() if c["slug"] == slug), None)
        name = match["client"] if match else slug.replace("-", " ")
        raw = (request.args.get("ids") or "").strip()
        ids = None
        if raw and raw.lower() != "all":
            ids = [int(x) for x in raw.split(",") if x.strip().isdigit()]
        return app.response_class(seo.blogs_doc(name, ids), mimetype="text/html")

    @app.route("/api/seo/compiled")
    def api_seo_compiled():
        gate = _require_api()
        if gate:
            return gate
        from . import seo
        name = (request.args.get("name") or "").strip()
        return jsonify(seo.compiled_json(name))

    @app.route("/seo/download/<slug>.<fmt>")
    def seo_download(slug, fmt):
        gate = _require_page()
        if gate:
            return gate
        from . import seo
        match = next((c for c in seo.seo_clients() if c["slug"] == slug), None)
        name = match["client"] if match else slug.replace("-", " ")
        if fmt == "html":
            body = seo.compiled_html(name)
            resp = make_response(body)
            resp.headers["Content-Type"] = "text/plain; charset=utf-8"
        else:
            resp = make_response(json.dumps(seo.compiled_json(name), indent=1))
            resp.headers["Content-Type"] = "application/json"
        resp.headers["Content-Disposition"] = f'attachment; filename="{slug}-schema.{fmt}"'
        return resp

    def _client_from_slug(slug: str) -> str:
        from . import seo
        match = next((c for c in seo.seo_clients() if c["slug"] == slug), None)
        return match["client"] if match else slug.replace("-", " ")

    def _urls_param() -> list[str]:
        """?urls=<one per line> — which saved pages a download covers."""
        raw = request.args.get("urls") or ""
        return [u.strip() for u in raw.split("\n") if u.strip()]

    # ---------------- business info: what we know, then GMB ----------------
    @app.route("/api/seo/enrich", methods=["POST"])
    def api_seo_enrich():
        """Complete a client's business info — Hub records first, then a
        Google Business Profile lookup for whatever is still blank."""
        gate = _require_api()
        if gate:
            return gate
        from . import seo
        body = request.get_json(silent=True) or {}
        client = (body.get("client") or "").strip()
        if not client:
            return jsonify({"error": "client is required."}), 400
        try:
            out = seo.enrich_business_info(client, force=bool(body.get("force")))
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 500
        if out.get("filled"):
            audit.log("hub", "seo_business_enriched", actor=current_user(),
                      detail=client, fields=", ".join(out["filled"]))
        return jsonify(out)

    # ---------------- saved schema pages: table, edit, delete ----------------
    @app.route("/api/seo/schema/pages")
    def api_seo_schema_pages():
        gate = _require_api()
        if gate:
            return gate
        from . import seo
        name = (request.args.get("name") or "").strip()
        return jsonify({"pages": seo.schema_pages_table(name)})

    @app.route("/api/seo/schema/page", methods=["POST"])
    def api_seo_schema_page_update():
        gate = _require_api()
        if gate:
            return gate
        from . import seo
        body = request.get_json(silent=True) or {}
        client = (body.get("client") or "").strip()
        url = (body.get("url") or "").strip()
        if not client or not url:
            return jsonify({"error": "client and url are required."}), 400
        if body.get("delete"):
            ok = seo.delete_page(client, url)
            return jsonify({"ok": ok, "pages": seo.schema_pages_table(client)})
        page = seo.update_page_meta(client, url, body)
        if page is None:
            return jsonify({"error": "That page is not saved for this client."}), 404
        return jsonify({"ok": True, "page": page,
                        "pages": seo.schema_pages_table(client)})

    @app.route("/api/seo/schema/detail")
    def api_seo_schema_detail():
        """Full JSON-LD for one saved page — powers the view/edit modal."""
        gate = _require_api()
        if gate:
            return gate
        from . import seo
        name = (request.args.get("name") or "").strip()
        url = (request.args.get("url") or "").strip()
        page = seo.load_store(name).get("pages", {}).get(url)
        if not page:
            return jsonify({"error": "Not found"}), 404
        return jsonify({"page": page})

    @app.route("/seo/schema/<slug>/download.<fmt>")
    def seo_schema_download(slug, fmt):
        """Schema for one page, the selected pages, or all of them."""
        gate = _require_page()
        if gate:
            return gate
        from . import seo
        name = _client_from_slug(slug)
        urls = _urls_param()
        wanted = {u.rstrip("/") for u in urls}
        suffix = "page" if len(urls) == 1 else ("selected" if urls else "all")
        store = seo.load_store(name)
        if fmt == "doc":
            body = seo.schema_doc(name, urls or None)
            ctype, ext = "application/msword", "doc"
        elif fmt == "json":
            picked = {u: p.get("schema") for u, p in store.get("pages", {}).items()
                      if not urls or u.rstrip("/") in wanted}
            body = json.dumps({"client": name, "pages": len(picked),
                               "schemas": picked}, indent=1)
            ctype, ext = "application/json", "json"
        else:
            out = [f"<!-- JSON-LD schema for {name} — generated by Smart 1 Hub -->"]
            for u, p in store.get("pages", {}).items():
                if urls and u.rstrip("/") not in wanted:
                    continue
                out.append(f"\n<!-- ===== {u} ===== -->")
                out.append('<script type="application/ld+json">')
                out.append(json.dumps(p.get("schema"), indent=1))
                out.append("</script>")
            body = "\n".join(out)
            ctype, ext = "text/plain; charset=utf-8", "html"
        resp = make_response(body)
        resp.headers["Content-Type"] = ctype
        resp.headers["Content-Disposition"] = \
            f'attachment; filename="{slug}-schema-{suffix}.{ext}"'
        return resp

    # ---------------------------- FAQ Builder ----------------------------
    @app.route("/api/seo/faq/generate", methods=["POST"])
    def api_faq_generate():
        gate = _require_api()
        if gate:
            return gate
        from . import faq
        body = request.get_json(silent=True) or {}
        client = (body.get("client") or "").strip()
        url = (body.get("url") or "").strip()
        if not client or not url:
            return jsonify({"error": "client and url are required."}), 400
        count = clamp_int(body.get("count"), 6, 1, 50)
        avoid = body.get("avoid") if isinstance(body.get("avoid"), list) else []
        try:
            return jsonify(faq.generate(client, url, count=count, avoid=avoid))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 500

    @app.route("/api/seo/faq/pages")
    def api_faq_pages():
        gate = _require_api()
        if gate:
            return gate
        from . import faq
        name = (request.args.get("name") or "").strip()
        return jsonify({"pages": faq.list_pages(name)})

    @app.route("/api/seo/faq/save", methods=["POST"])
    def api_faq_save():
        gate = _require_api()
        if gate:
            return gate
        from . import faq
        body = request.get_json(silent=True) or {}
        client = (body.get("client") or "").strip()
        url = (body.get("url") or "").strip()
        if not client or not url:
            return jsonify({"error": "client and url are required."}), 400
        try:
            page = faq.save_page(client, url, body.get("questions") or [],
                                 added_to_site=body.get("added_to_site", ""),
                                 title=body.get("title", ""),
                                 style=body.get("style"))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        audit.log("hub", "faq_page_saved", actor=current_user(), detail=client,
                  url=url, questions=len(page.get("questions", [])))
        # The FAQ exists in the Hub; somebody still has to put it on the page.
        # Raising the ticket must not be able to fail the save that earned it.
        from . import seo_tasks
        task = seo_tasks.for_faq(client, url, body.get("title", ""),
                                 actor=current_user())
        return jsonify({"ok": True, "page": page, "pages": faq.list_pages(client),
                        "task": task})

    @app.route("/api/seo/faq/page", methods=["POST"])
    def api_faq_page_update():
        gate = _require_api()
        if gate:
            return gate
        from . import faq
        body = request.get_json(silent=True) or {}
        client = (body.get("client") or "").strip()
        url = (body.get("url") or "").strip()
        if not client or not url:
            return jsonify({"error": "client and url are required."}), 400
        if body.get("delete"):
            ok = faq.delete_page(client, url)
            return jsonify({"ok": ok, "pages": faq.list_pages(client)})
        page = faq.update_page(client, url, body)
        if page is None:
            return jsonify({"error": "That FAQ page is not saved for this client."}), 404
        return jsonify({"ok": True, "page": page, "pages": faq.list_pages(client)})

    @app.route("/seo/faq/<slug>/download.<fmt>")
    def seo_faq_download(slug, fmt):
        """html = embeddable accordion, doc = customer review document,
        json = FAQPage schema only."""
        gate = _require_page()
        if gate:
            return gate
        from . import faq
        name = _client_from_slug(slug)
        urls = _urls_param()
        suffix = "page" if len(urls) == 1 else ("selected" if urls else "all")
        if fmt == "html":
            body = faq.accordion_html(name, urls or None,
                                      standalone=request.args.get("standalone") == "1")
            ctype, ext = "text/plain; charset=utf-8", "html"
        elif fmt == "json":
            body = faq.schema_html(name, urls or None)
            ctype, ext = "text/plain; charset=utf-8", "txt"
        else:
            body = faq.review_doc(name, urls or None)
            ctype, ext = "application/msword", "doc"
        resp = make_response(body)
        resp.headers["Content-Type"] = ctype
        resp.headers["Content-Disposition"] = \
            f'attachment; filename="{slug}-faq-{suffix}.{ext}"'
        return resp

    # ------------------ uploaded proposals (Client 360) ------------------
    @app.route("/api/proposal-to-io", methods=["POST"])
    def api_proposal_to_io():
        """Read a proposal we already sent and pull the IO fields out of it.

        The proposal is the agreement; the insertion order is what makes it
        real. Retyping one into the other is where the two drift apart — the
        proposal promises a package at a price and the IO ends up saying
        something slightly different, and nobody notices until billing.

        This extracts what it can and says how confident it is. It does not
        create the IO: the builder still asks for everything, and a field this
        could not find arrives empty rather than guessed, because a wrong
        number that looks filled in is worse than a blank one.
        """
        gate = _require_api()
        if gate:
            return gate

        body = request.get_json(silent=True) or {}
        url = str(body.get("url") or "").strip()
        client = str(body.get("client") or "").strip()
        if not url.startswith("https://"):
            return jsonify({"error": "That proposal has no readable file."}), 400

        # Only our own storage. This fetches a URL, so without the check it is
        # an SSRF hole that reads anything the server can reach.
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
        if not (host.endswith("cloudinary.com") or host.endswith("res.cloudinary.com")
                or host == (urlparse(request.url_root).hostname or "").lower()):
            return jsonify({"error": "That file isn't stored with us, so it "
                                     "can't be read."}), 400

        import requests as _rq
        try:
            r = _rq.get(url, timeout=20)
            r.raise_for_status()
            raw = r.content
        except Exception as exc:                        # noqa: BLE001
            return jsonify({"error": f"Couldn't fetch the proposal "
                                     f"({type(exc).__name__})."}), 502

        text = ""
        if raw[:5] == b"%PDF-":
            try:
                import io as _io
                from pypdf import PdfReader
                pages = PdfReader(_io.BytesIO(raw)).pages
                # First 12 pages: the plan and pricing are near the front, and
                # a 60-page appendix is cost without content.
                text = "\n".join((p.extract_text() or "") for p in pages[:12])
            except Exception as exc:                    # noqa: BLE001
                return jsonify({"error": f"That PDF couldn't be read "
                                         f"({type(exc).__name__})."}), 422
        else:
            try:
                text = raw.decode("utf-8", "ignore")
            except Exception:                           # noqa: BLE001
                text = ""

        text = " ".join(text.split())[:24000]
        if len(text) < 40:
            return jsonify({"error": "There's no readable text in that file — "
                                     "it may be a scan rather than a document.",
                            "fields": {}, "questions": []}), 200

        from . import ai as _ai, rate_card as _rc
        if not _ai.ready():
            return jsonify({"error": "AI isn't configured, so the proposal "
                                     "can't be read automatically.",
                            "fields": {}, "questions": []}), 200

        # The rate card is given to the model so a line it reads as "OTT" is
        # matched against what we actually sell, rather than invented.
        catalogue = [p.get("label", "") for p in (_rc.products() or [])][:120]

        schema_hint = {
            "client": "business name the proposal is addressed to",
            "monthly_total": "total monthly media spend as a number, or null",
            "term_months": "campaign length in months, or null",
            "start_date": "YYYY-MM-DD if stated, else null",
            "end_date": "YYYY-MM-DD if stated, else null",
            "products": "array of {product, monthly, notes} — product must be "
                        "one of the catalogue labels, or the closest match",
            "geography": "markets or radius named, else null",
            "notes": "anything a trafficker would need that has no field",
        }
        try:
            out = _ai.chat_json(
                [{"role": "system", "content":
                  "You read media proposals and extract the facts needed to "
                  "write an insertion order. Never invent a number: if the "
                  "proposal does not state something, return null for it. "
                  "Match products to the supplied catalogue; if nothing is a "
                  "reasonable match, use the proposal's own wording and say so "
                  "in notes. Return JSON only."},
                 {"role": "user", "content":
                  f"Catalogue: {catalogue}\n\nReturn JSON with these keys: "
                  f"{schema_hint}\n\nProposal text:\n{text}"}],
                module="io_builder", purpose="proposal_to_io")
        except Exception as exc:                        # noqa: BLE001
            return jsonify({"error": f"The proposal couldn't be read "
                                     f"({type(exc).__name__})."}), 502

        fields = out if isinstance(out, dict) else {}
        if client and not fields.get("client"):
            fields["client"] = client

        # What the IO needs and the proposal did not say. These become the
        # questions the builder asks, so the gap is explicit rather than a
        # blank field someone has to notice.
        asks = []
        if not fields.get("start_date"):
            asks.append({"key": "start_date", "q": "What date does the campaign start?"})
        if not fields.get("term_months"):
            asks.append({"key": "term_months", "q": "How many months does it run?"})
        if not fields.get("monthly_total"):
            asks.append({"key": "monthly_total", "q": "What is the monthly media spend?"})
        if not (fields.get("products") or []):
            asks.append({"key": "products", "q": "Which products should the IO carry?"})
        if not fields.get("geography"):
            asks.append({"key": "geography", "q": "Which markets or radius does it cover?"})

        # Run what was extracted past the same guardrails the IO enforces, so a
        # proposal that promises something unwritable is caught here rather
        # than at the end of the builder.
        checks = []
        try:
            items = [{"product": p.get("product", ""),
                      "monthly": p.get("monthly") or 0}
                     for p in (fields.get("products") or [])]
            if items:
                checks = _rc.guardrails(items)
        except Exception:                               # noqa: BLE001
            checks = []

        return jsonify({"fields": fields, "questions": asks,
                        "guardrails": checks,
                        "note": ("Read from the proposal. Anything it didn't "
                                 "state is left blank rather than guessed.")})

    @app.route("/api/client/proposals")
    def api_client_proposals():
        gate = _require_api()
        if gate:
            return gate
        from . import proposals
        name = (request.args.get("client") or "").strip()
        if not name:
            return jsonify({"proposals": []})
        return jsonify({"proposals": proposals.list_proposals(name),
                        "cloudinary": proposals.cloudinary_ready()})

    @app.route("/api/client/proposals/upload", methods=["POST"])
    def api_client_proposals_upload():
        gate = _require_api()
        if gate:
            return gate
        from . import proposals
        client = (request.form.get("client") or "").strip()
        upload = request.files.get("file")
        if not client:
            return jsonify({"error": "client is required."}), 400
        if upload is None or not upload.filename:
            return jsonify({"error": "Choose a PDF or Word file to upload."}), 400
        try:
            record = proposals.add_proposal(
                client, upload.filename, upload.read(),
                date_sent=request.form.get("date_sent", ""),
                title=request.form.get("title", ""),
                note=request.form.get("note", ""),
                actor=current_user() or "")
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"Upload failed: {exc}"}), 500
        audit.log("hub", "proposal_uploaded", actor=current_user(), detail=client,
                  name=record["filename"], date_sent=record["date_sent"])
        return jsonify({"ok": True, "proposal": record,
                        "proposals": proposals.list_proposals(client)})

    @app.route("/api/client/proposals/update", methods=["POST"])
    def api_client_proposals_update():
        gate = _require_api()
        if gate:
            return gate
        from . import proposals
        body = request.get_json(silent=True) or {}
        client = (body.get("client") or "").strip()
        pid = (body.get("id") or "").strip()
        if not client or not pid:
            return jsonify({"error": "client and id are required."}), 400
        if body.get("delete"):
            ok = proposals.delete_proposal(client, pid)
            return jsonify({"ok": ok, "proposals": proposals.list_proposals(client)})
        hit = proposals.update_proposal(client, pid, body)
        if hit is None:
            return jsonify({"error": "Not found"}), 404
        return jsonify({"ok": True, "proposal": hit,
                        "proposals": proposals.list_proposals(client)})

    @app.route("/api/client/proposals/file/<path:name>")
    def api_client_proposals_file(name):
        """Serves proposals kept on disk when Cloudinary isn't configured."""
        gate = _require_page()
        if gate:
            return gate
        from . import proposals
        path = proposals.local_file_path(name)
        if not path:
            return jsonify({"error": "Not found"}), 404
        return send_from_directory(os.path.dirname(path), os.path.basename(path))

    @app.route("/qa")
    def qa_home():
        gate = _require_page()
        if gate:
            return gate
        from . import qa
        seen, groups = [], {}
        for key, meta in qa.REPORTS.items():
            g = meta.get("group", "Reports")
            if g not in groups:
                groups[g] = []
                seen.append(g)
            groups[g].append((key, meta))
        # Two audits live outside REPORTS because they're modules with their
        # own pages, not table-returning functions. They still belong here —
        # somebody looking for "what's wrong" shouldn't have to know which
        # kind of thing each one is.
        extras = [
            ("Data Quality", "stale-creative", {
                "title": "Stale Creative",
                "desc": "How long since we last produced creative for each "
                        "active client — and who has never had any.",
                "ico": "&#9203;", "href": "/qa/stale-creative"}),
            ("Data Quality", "web-tickets", {
                "title": "Web Tickets",
                "desc": "Website change requests from Knack: what's open, "
                        "what's gone stale, and per-client history.",
                "ico": "&#127915;", "href": "/tools/tickets/"}),
        ]
        for g, key, meta in extras:
            if g not in groups:
                groups[g] = []
                seen.append(g)
            groups[g].append((key, meta))
        return render_template("qa.html", user=current_user(), modules=MODULES,
                               active="qa", groups=[(g, groups[g]) for g in seen])

    @app.route("/qa/<key>")
    def qa_report(key):
        gate = _require_page()
        if gate:
            return gate
        from . import qa
        meta = qa.REPORTS.get(key)
        if not meta:
            return redirect("/qa")
        return render_template("qa_report.html", user=current_user(), modules=MODULES,
                               active="qa", key=key, title=meta["title"])

    @app.route("/api/qa/<key>")
    def api_qa(key):
        gate = _require_api()
        if gate:
            return gate
        from . import qa
        if key not in qa.REPORTS:
            return jsonify({"error": f"Unknown report: {key}"}), 404
        try:
            out = qa.run(key, month=(request.args.get("month") or "").strip())
        except Exception as exc:  # noqa: BLE001 — reports must degrade gracefully
            out = {"key": key, "title": qa.REPORTS[key]["title"],
                   "columns": [], "rows": [], "error": str(exc)}
        audit.log("hub", "qa_report", actor=current_user(), detail=key)
        return jsonify(out)

    @app.route("/api/qa/accounting/status", methods=["POST"])
    def api_qa_accounting_status():
        gate = _require_api()
        if gate:
            return gate
        from . import qa
        body = request.get_json(silent=True) or {}
        opp_id = (body.get("id") or "").strip()
        stage_id = (body.get("stage_id") or "").strip()
        status = (body.get("status") or "").strip().lower()
        if not opp_id or (not stage_id and not status):
            return jsonify({"error": "id and stage_id or status are required."}), 400
        if status and status not in qa.GHL_STATUSES:
            return jsonify({"error": f"status must be one of {', '.join(qa.GHL_STATUSES)}."}), 400
        try:
            qa.set_accounting_stage(opp_id, stage_id, status)
        except Exception as exc:  # noqa: BLE001
            errors.log_exception("qa-accounting", exc, path=request.path,
                                 actor=current_user() or "")
            return jsonify({"error": str(exc)})
        audit.log("hub", "accounting_stage_changed", actor=current_user(),
                  detail=f"{opp_id} -> {stage_id}")
        return jsonify({"ok": True})

    @app.route("/api/qa/invoice-off/assign", methods=["POST"])
    def api_qa_invoice_assign():
        gate = _require_api()
        if gate:
            return gate
        from . import qa
        body = request.get_json(silent=True) or {}
        customer = (body.get("customer") or "").strip()
        partner = (body.get("partner") or "").strip()
        if not customer or not partner:
            return jsonify({"error": "customer and partner are required."}), 400
        qa.assign_invoice_partner(customer, partner)
        audit.log("hub", "qa_invoice_assigned", actor=current_user(),
                  detail=f"{customer} -> {partner}")
        return jsonify({"ok": True})

    @app.route("/activity")
    def activity():
        gate = _require_page()
        if gate:
            return gate
        return render_template("activity.html", user=current_user(), modules=MODULES, active="activity")

    @app.route("/status")
    def status():
        gate = _require_page()
        if gate:
            return gate
        return render_template("status.html", user=current_user(), modules=MODULES, active="status")

    # ---------------- Clients app (prebuilt Knack lookup) ----------------
    # The React bundle was built with absolute /static/... and /data/... URLs,
    # so the hub serves those two prefixes straight from clients_app/.
    @app.route("/clients")
    @app.route("/clients/")
    def clients_index():
        gate = _require_page()
        if gate:
            return gate
        from .sidebar import render_sidebar
        with open(os.path.join(CLIENTS_APP, "index.html"), "rb") as fh:
            body = fh.read()
        snippet = b'<link rel="stylesheet" href="/assets/theme.css">'
        if b"</head>" in body:
            body = body.replace(b"</head>", snippet + b"</head>", 1)
        bar = render_sidebar("clients")
        # Deep links from Client 360: /clients?q=<client> auto-fills and runs
        # the React app's search (native value setter so React sees the input).
        autosearch = b"""<script>
(function(){
  var q=new URLSearchParams(location.search).get('q'); if(!q) return;
  var tries=0;
  var t=setInterval(function(){
    tries++;
    var input=document.querySelector('input[placeholder^="Client, IO"]')||
              document.querySelector('input[type="search"]');
    if(input){
      clearInterval(t);
      var setter=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;
      setter.call(input,q);
      input.dispatchEvent(new Event('input',{bubbles:true}));
      input.focus();
    } else if(tries>60){clearInterval(t);}
  },250);
})();
</script>"""
        addition = autosearch + bar
        body = body.replace(b"</body>", addition + b"</body>", 1) if b"</body>" in body else body + addition
        return app.response_class(body, mimetype="text/html")

    @app.route("/static/<path:filename>")
    def clients_static(filename):
        return send_from_directory(os.path.join(CLIENTS_APP, "static"), filename)

    @app.route("/data/<path:filename>")
    def clients_data(filename):
        if not current_user():
            return jsonify({"error": "Not authenticated."}), 401
        return send_from_directory(os.path.join(CLIENTS_APP, "data"), filename)

    # ---------------- QuickBooks connect / lookup ----------------
    @app.route("/qb/connect")
    def qb_connect():
        gate = _require_page()
        if gate:
            return gate
        from . import quickbooks as qb
        if not qb.configured():
            return ("QuickBooks is not configured — set QB_CLIENT_ID and QB_CLIENT_SECRET "
                    "(from developer.intuit.com) and redeploy. <a href='/status'>Status</a>", 400)
        return redirect(qb.authorize_url(request))

    @app.route("/qb/callback")
    def qb_callback():
        gate = _require_page()
        if gate:
            return gate
        from . import quickbooks as qb
        ok, msg = qb.handle_callback(request)
        audit.log("hub", "quickbooks_connected" if ok else "quickbooks_connect_failed",
                  actor=current_user(), detail=msg)
        return redirect("/status?qb=" + ("connected" if ok else "error"))

    @app.route("/qb/disconnect", methods=["POST", "GET"])
    def qb_disconnect():
        gate = _require_page()
        if gate:
            return gate
        from . import quickbooks as qb
        qb.disconnect()
        audit.log("hub", "quickbooks_disconnected", actor=current_user())
        return redirect("/status")

    @app.route("/api/qb/invoices")
    def api_qb_invoices():
        gate = _require_api()
        if gate:
            return gate
        from . import quickbooks as qb
        q = (request.args.get("q") or "").strip()
        cid = (request.args.get("customer_id") or "").strip()
        if not q and not cid:
            return jsonify({"configured": qb.configured(), "connected": qb.connected(), "customers": []})
        try:
            return jsonify(qb.lookup(q, customer_id=cid or None))
        except Exception as exc:  # noqa: BLE001 — Client 360 must degrade gracefully
            return jsonify({"configured": qb.configured(), "connected": qb.connected(),
                            "customers": [], "error": str(exc)})

    @app.route("/api/qb/customers")
    def api_qb_customers():
        """Customer search for the C360 'attach QuickBooks customer' flow."""
        gate = _require_api()
        if gate:
            return gate
        from . import quickbooks as qb
        q = (request.args.get("q") or "").strip()
        if not q:
            return jsonify({"customers": []})
        try:
            return jsonify({"customers": qb.find_customers(q, limit=6)})
        except Exception as exc:  # noqa: BLE001
            return jsonify({"customers": [], "error": str(exc)})

    @app.route("/favicon.ico")
    def favicon():
        return ("", 204)

    @app.route("/health")
    @app.route("/healthz")
    def health():
        return jsonify({"status": "ok"})

    @app.route("/login/health")
    def login_health():
        """Why sign-in isn't working — readable WITHOUT signing in.

        That is the whole point: every other diagnostic in the Hub sits behind
        the login, which is useless when the login is the thing that's broken.
        Boot failures were being stored in app.config and never surfaced, so a
        users table that failed to create looked identical to a wrong password.

        Reports booleans and error *types* only. No secrets, no password, no
        token, no email addresses.
        """
        import traceback
        out = {"version": None, "panel_password_set": bool(auth.panel_password()),
               "google_button_enabled": (os.environ.get("HUB_GOOGLE_LOGIN", "").lower()
                                         in {"1", "true", "yes", "on"})}
        try:
            from . import version as _v
            out["version"] = _v.label()
        except Exception:  # noqa: BLE001
            pass

        out["db_boot_error"] = app.config.get("HUB_DB_BOOT_ERROR") or None
        out["users_registered"] = app.config.get("HUB_USERS_REGISTERED", None)
        if out["users_registered"] is False:
            out["signup_available"] = False
        out["users_boot_error"] = app.config.get("HUB_USERS_BOOT_ERROR") or None

        # Can we actually reach the users table? This is the failure that makes
        # /signup return a 500 with nothing to go on.
        try:
            from .users import User
            out["users_table"] = "ok"
            out["user_count"] = User.query.count()
            out["super_admins_seeded"] = User.query.filter_by(
                role="super_admin").count()
            out["super_admins_with_password"] = User.query.filter(
                User.role == "super_admin", User.password_hash != "").count()
        except Exception as exc:  # noqa: BLE001
            out["users_table"] = f"{type(exc).__name__}"
            out["users_table_detail"] = str(exc)[:200]
            out["user_count"] = None

        try:
            from .extensions import database_url
            url = database_url()
            out["database"] = ("postgres" if url.startswith("postgres")
                               else "sqlite" if url.startswith("sqlite") else "other")
            if url.startswith("sqlite"):
                path = url.replace("sqlite:///", "")
                out["sqlite_path"] = path
                out["sqlite_dir_writable"] = os.access(os.path.dirname(path) or ".", os.W_OK)
        except Exception as exc:  # noqa: BLE001
            out["database"] = f"error: {type(exc).__name__}"

        # Plain-English verdict, so nobody has to interpret the booleans.
        problems = []
        if app.config.get("HUB_USERS_REGISTERED") is False:
            problems.append(
                "The user-accounts blueprint failed to register, so /signup "
                "and /diagnostics/users return 404. Reason: "
                + str(app.config.get("HUB_USERS_BOOT_ERROR", "unknown"))
                + " — if it names flask_sqlalchemy, Flask-SQLAlchemy is "
                  "missing from requirements.txt.")
        try:
            from .config import settings as _cfg
            for w in _cfg.placeholder_warnings():
                problems.append(w["detail"])
        except Exception:  # noqa: BLE001
            pass
        if out.get("users_table") != "ok":
            problems.append("The user accounts table isn't reachable, so /signup "
                            "will fail. Usually DATABASE_URL is unset and the "
                            "disk fallback isn't writable.")
        if not out["panel_password_set"]:
            problems.append("PANEL_PASSWORD is not set, so the shared-password "
                            "fallback can't work either.")
        if out.get("db_boot_error"):
            problems.append("The database failed at boot: " + str(out["db_boot_error"])[:160])
        if not problems and out.get("super_admins_with_password", 0) == 0:
            problems.append("No super admin has set a password yet — go to "
                            "/signup and register todd@smart1marketing.com.")
        out["problems"] = problems
        out["ok"] = not problems
        return jsonify(out)

    # ---------------- JSON APIs (hub-side) ----------------
    def _require_api():
        if not current_user():
            return jsonify({"error": "Not authenticated."}), 401
        return None

    @app.route("/api/summary")
    def api_summary():
        gate = _require_api()
        if gate:
            return gate
        try:
            data = knack_data.summary()
        except Exception as exc:  # noqa: BLE001 — dashboard must never 500
            data = {"error": str(exc)}
        try:
            from . import seo
            seo_rows = seo.seo_clients()
            data["seo_clients"] = len(seo_rows)
            data["seo_billing_monthly"] = round(sum(c["billing"] for c in seo_rows))
        except Exception:  # noqa: BLE001 — SEO totals are additive, never break the dashboard
            data.setdefault("seo_clients", None)
            data.setdefault("seo_billing_monthly", None)
        return jsonify(data)

    @app.route("/api/c360")
    def api_c360():
        gate = _require_api()
        if gate:
            return gate
        q = request.args.get("q", "")
        try:
            groups = knack_data.search_client(q)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"groups": [], "error": str(exc)})
        return jsonify({"groups": groups})

    @app.route("/api/c360/sites")
    def api_c360_sites():
        """Best-effort search of the Simvoly admin's Postgres inventory."""
        gate = _require_api()
        if gate:
            return gate
        q = (request.args.get("q") or "").strip()
        dsn = os.environ.get("DATABASE_URL", "")
        if not q or not dsn:
            return jsonify({"results": [], "configured": bool(dsn)})
        try:
            import psycopg2
            import psycopg2.extras
            conn = psycopg2.connect(dsn)
            try:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(
                        """
                        SELECT p.project_id, p.name, p.status,
                               (SELECT w.domain FROM websites w
                                 WHERE w.project_id = p.project_id AND w.domain IS NOT NULL
                                 LIMIT 1) AS domain
                          FROM projects p
                         WHERE p.name ILIKE %s
                            OR p.project_id IN (
                                 SELECT w2.project_id FROM websites w2
                                  WHERE w2.name ILIKE %s OR w2.domain ILIKE %s)
                         ORDER BY p.name LIMIT 6
                        """,
                        (f"%{q}%", f"%{q}%", f"%{q}%"),
                    )
                    rows = [dict(r) for r in cur.fetchall()]
            finally:
                conn.close()
            return jsonify({"results": rows, "configured": True})
        except Exception as exc:  # noqa: BLE001
            return jsonify({"results": [], "configured": True, "error": str(exc)})

    @app.route("/api/errors")
    def api_errors():
        gate = _require_api()
        if gate:
            return gate
        limit = clamp_int(request.args.get("limit"), 50, 1, 300)
        return jsonify({"errors": errors.read(limit=limit)})

    @app.route("/api/errors/clear", methods=["POST"])
    def api_errors_clear():
        gate = _require_api()
        if gate:
            return gate
        errors.clear()
        audit.log("hub", "error_log_cleared", actor=current_user())
        return jsonify({"ok": True})

    @app.route("/api/activity")
    def api_activity():
        gate = _require_api()
        if gate:
            return gate
        limit = clamp_int(request.args.get("limit"), 300, 1, 1000)
        module = request.args.get("module") or None
        return jsonify({"entries": audit.read(limit=limit, module=module)})

    @app.route("/api/status")
    def api_status():
        gate = _require_api()
        if gate:
            return gate
        checks = []

        def add(name, status_, message):
            checks.append({"name": name, "status": status_, "message": message})

        # --- core config ---
        pw = auth.panel_password()
        if not pw:
            add("Panel password", "error", "PANEL_PASSWORD is not set — nobody can log in.")
        elif pw in ("change-me", "change-me-to-something-strong"):
            add("Panel password", "warn", "Still set to a placeholder — change it.")
        else:
            add("Panel password", "ok", "Configured.")
        add("Session secret", "ok" if os.environ.get("SECRET_KEY") or os.environ.get("SESSION_SECRET") else "warn",
            "Configured — logins survive restarts." if os.environ.get("SECRET_KEY") or os.environ.get("SESSION_SECRET")
            else "Not set — everyone is logged out on every restart/redeploy.")

        # --- Knack data ---
        age = knack_data.data_age_hours()
        if age is None:
            add("Smart 1 Team data", "error", "clients_app/data/products.json not found.")
        elif age > 48:
            add("Smart 1 Team data", "warn", f"Last refreshed {age / 24:.1f} days ago — run the refresh workflow.")
        else:
            add("Smart 1 Team data", "ok", f"Refreshed {age:.0f}h ago · {len(knack_data.products())} product rows · {len(knack_data.websites())} sites.")

        # --- GHL ---
        token, company = os.environ.get("GHL_PRIVATE_TOKEN"), os.environ.get("GHL_COMPANY_ID")
        if not token or not company:
            add("GoHighLevel API", "error", "GHL_PRIVATE_TOKEN and/or GHL_COMPANY_ID is not set.")
        else:
            try:
                r = _rq.get(
                    "https://services.leadconnectorhq.com/locations/search",
                    params={"companyId": company, "limit": "1"},
                    headers={"Authorization": f"Bearer {token}",
                             "Version": os.environ.get("GHL_API_VERSION", "2021-07-28"),
                             "Accept": "application/json"},
                    timeout=12,
                )
                add("GoHighLevel API", "ok" if r.ok else "error",
                    "Token is valid and can read sub-accounts." if r.ok else f"Token check failed (HTTP {r.status_code}).")
            except Exception as exc:  # noqa: BLE001
                add("GoHighLevel API", "error", f"Could not reach GHL: {exc}")

        # --- Simvoly ---
        skey = os.environ.get("SIMVOLY_API_KEY")
        if not skey:
            add("Smart 1 Sites Platform API", "warn", "SIMVOLY_API_KEY is not set — Smart 1 Sites module runs limited/mock.")
        else:
            base = os.environ.get("SIMVOLY_API_BASE_URL", "https://api.smart1sites.com").rstrip("/")
            try:
                r = _rq.get(f"{base}/api/v1/plans", headers={"X-CLIENT-KEY": skey, "Accept": "application/json"}, timeout=12)
                add("Smart 1 Sites Platform API", "ok" if r.ok else "error",
                    "Key is valid — plan catalog reachable." if r.ok else f"Key check failed (HTTP {r.status_code}).")
            except Exception as exc:  # noqa: BLE001
                add("Smart 1 Sites Platform API", "error", f"Could not reach Smart 1 Sites API: {exc}")
        add("Sites database", "ok" if os.environ.get("DATABASE_URL") else "warn",
            "DATABASE_URL configured." if os.environ.get("DATABASE_URL")
            else "DATABASE_URL not set — Sites inventory won't persist.")

        # --- Brandfetch ---
        bkey = os.environ.get("BRANDFETCH_API_KEY")
        if not bkey:
            add("Brandfetch API", "skipped", "Not configured — auto-fill from website is disabled (optional).")
        else:
            try:
                r = _rq.get("https://api.brandfetch.io/v2/brands/brandfetch.com",
                            headers={"Authorization": f"Bearer {bkey}"}, timeout=12)
                if r.ok:
                    add("Brandfetch API", "ok", "Key is valid.")
                elif r.status_code == 429:
                    add("Brandfetch API", "warn", "Key valid, but quota exhausted right now.")
                else:
                    add("Brandfetch API", "error", f"Key check failed (HTTP {r.status_code}).")
            except Exception as exc:  # noqa: BLE001
                add("Brandfetch API", "error", f"Could not reach Brandfetch: {exc}")

        # --- Google OAuth ---
        gid, gsec = os.environ.get("GOOGLE_CLIENT_ID"), os.environ.get("GOOGLE_CLIENT_SECRET")
        add("Google OAuth app", "ok" if gid and gsec else "warn",
            "Client ID + secret configured. Manage connected accounts in the Google module."
            if gid and gsec else "GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET not set — Google module disabled.")

        # --- QuickBooks ---
        from . import quickbooks as qb
        if not qb.configured():
            add("QuickBooks", "skipped",
                "QB_CLIENT_ID / QB_CLIENT_SECRET not set — invoice lookup disabled (optional).")
        elif not qb.connected():
            add("QuickBooks", "warn",
                "App configured but no company connected yet — use the Connect QuickBooks button below.")
        else:
            add("QuickBooks", "ok", "Connected — client invoice lookup active on Client 360.")

        # --- Sales section (Proposal + Sales Builder) ---
        add("OpenAI API", "ok" if os.environ.get("OPENAI_API_KEY") else "skipped",
            "Configured — AI proposal generation enabled." if os.environ.get("OPENAI_API_KEY")
            else "OPENAI_API_KEY not set — proposal generation falls back to templates (optional).")
        add("Cloudinary", "ok" if (os.environ.get("CLOUDINARY_URL") or "").startswith("cloudinary://") else "warn",
            "Configured — proposal PDFs and logs persist to Cloudinary."
            if (os.environ.get("CLOUDINARY_URL") or "").startswith("cloudinary://")
            else "CLOUDINARY_URL not set — proposals persist to the local disk only.")

        # --- Display Ad Builder (second process in this container) ---
        # Asked here rather than inferred from config, because "the token is
        # set" and "the renderer is answering" are different failures with
        # different fixes, and only one of them is visible from the outside.
        try:
            from hub import ad_builder_proxy
            ab = ad_builder_proxy.status()
            add("Display Ad Builder", "ok" if ab.get("ok") else "warn", ab.get("detail", ""))
        except Exception as _ab_exc:  # noqa: BLE001
            add("Display Ad Builder", "warn",
                f"Could not be checked: {_ab_exc}")

        # --- binaries for the PDF optimizer ---
        gs, qpdf = shutil.which("gs"), shutil.which("qpdf")
        if gs and qpdf:
            try:
                v = subprocess.run([gs, "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
            except Exception:  # noqa: BLE001
                v = "?"
            add("Ghostscript / qPDF", "ok", f"gs {v} · qpdf present — PDF optimizer ready.")
        else:
            add("Ghostscript / qPDF", "error", "Missing gs/qpdf — PDF optimizer will fail (Docker image installs these).")

        # --- persistent disk ---
        add("Persistent disk", "ok" if os.path.isdir("/var/data") else "warn",
            "/var/data mounted — audit log & tokens survive deploys." if os.path.isdir("/var/data")
            else "/var/data not mounted — audit log and Google tokens are ephemeral.")

        return jsonify({"checks": checks})

    # Shared SQLAlchemy instance. Must run BEFORE any module blueprint is
    # registered, because their models bind to it at import time and their
    # create_all() runs during registration.
    try:
        from .extensions import init_db
        init_db(app)
    except Exception as _db_exc:  # noqa: BLE001
        try:
            errors.log_exception("hub", _db_exc)
        except Exception:  # noqa: BLE001
            pass

    # ---------------- sidebar for blueprint-registered pages ----------------
    # Modules mounted through DispatcherMiddleware get the sidebar injected by
    # HubBar in wsgi.py. Modules registered as blueprints on this app do not,
    # and they don't extend base.html either — so Tickets, Calculators, Image
    # Picker, Page Images and Google Access rendered with no navigation at all.
    #
    # Editing five modules' templates would fix today and break again the next
    # time one is added. Injecting on the way out covers every one of them, and
    # anything registered later, automatically.
    CHROMELESS = ("/login", "/signup", "/reset", "/signin", "/account",
                  "/connect", "/api/", "/assets/", "/hub-", "/static/")

    @app.after_request
    def _inject_sidebar_response(resp):
        try:
            if resp.status_code != 200:
                return resp
            if not (resp.mimetype or "").startswith("text/html"):
                return resp
            path = request.path or "/"
            # Sign-in and the client-facing pages are deliberately chrome-free.
            if any(path.startswith(p) for p in CHROMELESS):
                return resp
            if resp.direct_passthrough:
                return resp
            body = resp.get_data()
            if b"s1hub-sb" in body or b'class="sidebar"' in body:
                return resp                      # already has one
            if b"</body>" not in body:
                return resp                      # a fragment, not a page
            from .sidebar import render_sidebar
            bar = render_sidebar(_MOUNT_ACTIVE_HUB.get(
                "/" + path.strip("/").split("/")[0], ""))
            # The help/demo/autofill layer has to come with the sidebar. It
            # was injected by HubBar for dispatcher-mounted modules and by
            # base.html for hub pages, which left blueprint-registered pages
            # — Tickets, Calculators, Page Images, Google Access, Stale
            # Creative — with neither. No scripts means no bubbles and no
            # walkthrough button, silently.
            # Tag <body> so the walkthrough launcher knows which tool it's on.
            # Only when the page hasn't already declared one.
            if b"data-module=" not in body and b"<body" in body:
                seg = path.strip("/").split("/")
                slug = seg[1] if len(seg) > 1 and seg[0] == "tools" else (seg[0] if seg else "")
                mod = {"tickets": "tickets", "calculators": "calculators",
                       "page-images": "page_image_optimizer",
                       "google-access": "google_access",
                       "image-picker": "image_picker",
                       "sites-match": "sites_admin",
                       "stale-creative": "qa", "qa": "qa"}.get(slug, "")
                if mod:
                    body = re.sub(rb"<body\b",
                                  b'<body data-module="' + mod.encode() + b'"',
                                  body, count=1)

            extra = b""
            if b"hub-help.js" not in body:
                extra = (b'<script defer src="/hub-help.js"></script>'
                         b'<script defer src="/hub-demo.js"></script>'
                    b'<script defer src="/hub-crumbs.js"></script>'
                         b'<script defer src="/hub-autofill.js"></script>'
                         b'<script defer src="/hub-accordion.js"></script>')
            if b"hub-help.css" not in body and b"</head>" in body:
                body = body.replace(
                    b"</head>",
                    b'<link rel="stylesheet" href="/hub-help.css"></head>', 1)
            resp.set_data(body.replace(b"</body>", bar + extra + b"</body>", 1))
        except Exception:  # noqa: BLE001 — never break a page over navigation
            pass
        return resp

    # ---------------- v7.9 blueprint tools ----------------
    # These ship as Flask blueprints rather than standalone apps, so they
    # register on the hub app directly. Each is wrapped: a tool that fails to
    # load must degrade to "that tool is missing", never to a dead Hub.
    for _label, _mod, _fn, _prefix in (
        ("Calculators", "modules.calculators", "register_calculators", "/tools/calculators"),
        ("Google Access", "modules.google_access", "register_google_access", "/tools/google-access"),
        ("Image Picker", "modules.image_picker", "register_image_picker", "/tools/image-picker"),
        ("Page Image Optimizer", "modules.page_image_optimizer", "register", "/tools/page-images"),
        ("Web Tickets", "modules.tickets", "register_tickets", "/tools/tickets"),
        # The Display Ad Builder is a Node service in the same container; this
        # registers the proxy that puts it behind the Hub login. Same wrapper
        # as the rest, so a renderer that will not start costs the Hub nothing.
        ("Display Ad Builder", "hub.ad_builder_proxy", "register", "/tools/display-ads"),
        # The client and proposal joins. Registered separately from the
        # proxy so a fault in one does not take the other down: the
        # builder is still usable without attach, and attach still
        # explains itself if the renderer is down.
        ("Display Ad Builder links", "hub.ad_builder_link", "register", "/tools/display-ads"),
    ):
        try:
            _m = __import__(_mod, fromlist=[_fn])
            _register = getattr(_m, _fn)
            import inspect
            if "url_prefix" in inspect.signature(_register).parameters:
                _register(app, url_prefix=_prefix)
            else:
                _register(app)
        except Exception as _tool_exc:  # noqa: BLE001
            try:
                errors.log_exception("hub", _tool_exc)
            except Exception:  # noqa: BLE001
                pass

    # ---------------- Commercial Builder ----------------
    # A blueprint, not a standalone Flask app, so it registers here rather
    # than mounting through DispatcherMiddleware in wsgi.py.
    try:
        from modules.commercial_builder import register_commercial_builder
        register_commercial_builder(app)
    except Exception as _cb_exc:  # noqa: BLE001
        try:
            errors.log_exception("hub", _cb_exc)
        except Exception:  # noqa: BLE001
            pass

    # ---------------- User accounts ----------------
    # Registered after init_db (models bind to the shared instance) and before
    # the help layer, so /diagnostics/users exists by the time the sidebar
    # renders. Seeds the founding super admins on first boot.
    try:
        from .users_routes import register_users
        register_users(app)
        app.config["HUB_USERS_REGISTERED"] = True
    except Exception as _users_exc:  # noqa: BLE001
        # This failing is why /signup returned 404 with nothing to go on:
        # Flask-SQLAlchemy was missing from requirements.txt, the import
        # raised, and the except swallowed it. Record the reason so
        # /login/health can say so instead of leaving you guessing.
        app.config["HUB_USERS_REGISTERED"] = False
        app.config["HUB_USERS_BOOT_ERROR"] = (
            f"{type(_users_exc).__name__}: {_users_exc}")
        try:
            errors.log_exception("hub", _users_exc)
        except Exception:  # noqa: BLE001
            pass

    # ---------------- Inbound Suite (GoHighLevel) webhooks ----------------
    try:
        from .ghl_hooks import register_ghl_hooks
        register_ghl_hooks(app)
    except Exception as _hook_exc:  # noqa: BLE001
        app.config["HUB_HOOKS_BOOT_ERROR"] = str(_hook_exc)
        try:
            errors.log_exception("hub", _hook_exc)
        except Exception:  # noqa: BLE001
            pass

    @app.route("/api/scheduler/run/<name>", methods=["POST"])
    def api_scheduler_run(name):
        """Run one job now, without waiting for its next slot."""
        gate = _require_api()
        if gate:
            return gate
        from . import scheduler as _s
        audit.log("scheduler", "manual_run", actor=current_user(), job=name)
        return jsonify(_s.run_now(name, app))

    # ---------------- background jobs ----------------
    # Started last, so every module it might call is registered first. Exactly
    # one worker actually runs jobs — see hub/scheduler.py for why that
    # matters with two gunicorn workers.
    try:
        from . import scheduler as _sched
        _sched.start(app)
    except Exception as _sched_exc:  # noqa: BLE001
        app.config["HUB_SCHEDULER_BOOT_ERROR"] = str(_sched_exc)
        try:
            errors.log_exception("hub", _sched_exc)
        except Exception:  # noqa: BLE001
            pass

    # ---------------- Partner resource pages ----------------
    # Static, self-contained pages served behind the login. Same defensive
    # registration as everything else here: a page that fails to load must
    # cost the dashboard a button, not the Hub a boot.
    try:
        from .partner import register as register_partner
        register_partner(app)
    except Exception as _pp_exc:  # noqa: BLE001
        try:
            errors.log_exception("hub", _pp_exc)
        except Exception:  # noqa: BLE001
            pass

    # ---------------- Stale Creative audit ----------------
    # Same defensive registration as the help layer below: an audit that fails
    # to load must not take the Hub with it.
    try:
        from .stale_creative import register_stale_creative
        register_stale_creative(app)
    except Exception as _sc_exc:  # noqa: BLE001
        try:
            errors.log_exception("hub", _sc_exc)
        except Exception:  # noqa: BLE001
            pass

    # Create any tables the newly registered blueprints declared. Runs AFTER
    # all of them, so a module registered later still gets its tables. Guarded:
    # a sleeping database must not take the Hub down at boot.
    try:
        from .extensions import create_all as _create_all
        _tbl_err = _create_all(app)
        if _tbl_err:
            app.config["HUB_DB_BOOT_ERROR"] = _tbl_err
    except Exception:  # noqa: BLE001
        pass

    # Refill the persistent disk from the database if this is a *new* disk.
    # JSON files on /var/data are outside the database backup and do not
    # survive the disk being recreated, so hub/jsonstore.py mirrors the ones
    # that are the only copy of something. On an ordinary boot this is two
    # cheap queries and a no-op; on the first boot after a disk is recreated
    # it is the whole recovery, and it has to happen here rather than lazily
    # because /diagnostics and the sidebar read those files before any user
    # does. Guarded like every other boot step — but recorded, not swallowed.
    try:
        from . import jsonstore
        app.config["HUB_JSONSTORE_RESTORE"] = jsonstore.maybe_restore()
    except Exception as _js_exc:  # noqa: BLE001
        app.config["HUB_JSONSTORE_RESTORE"] = {
            "ran": False, "reason": f"{type(_js_exc).__name__}: {_js_exc}"}
        try:
            errors.log_exception("jsonstore", _js_exc)
        except Exception:  # noqa: BLE001
            pass

    # ---------------- v7: help bubbles, tool walkthroughs, demo mode -------
    # Registered last, because it needs _hub_user (defined with the login
    # routes above). The fallbacks below are not decoration: an earlier build
    # wrapped this in a bare try/except, the registration failed on a NameError,
    # and every page in the Hub 500'd on an undefined `demo_banner`. A failure
    # here must degrade to "no help" — never to "no Hub".
    try:
        from .help_routes import register_help
        register_help(app, current_user_fn=_hub_user)
    except Exception as _help_exc:  # noqa: BLE001
        from markupsafe import Markup as _M
        app.jinja_env.globals.setdefault("demo_banner", lambda *a, **k: _M(""))
        app.jinja_env.globals.setdefault("help_dot", lambda *a, **k: _M(""))
        app.jinja_env.globals.setdefault("demo_launcher", lambda *a, **k: _M(""))
        app.jinja_env.globals.setdefault("help_text", lambda *a, **k: "")
        try:
            errors.log_exception("hub", _help_exc)
        except Exception:  # noqa: BLE001
            pass

    return app
