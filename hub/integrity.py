"""Integration hygiene — the audit that stops old bugs coming back.

Consolidating twenty modules onto the shared services is a large change with,
right now, no bug at the end of it: every image path already caps the longest
edge before converting, and no PDF is uploaded with an image resource type. So
rewriting working code would be risk without payoff.

What is worth doing is making the invariants *checkable*, because every one of
these was a real, shipped defect at some point and each came back the moment a
new module was written without knowing about it:

  * **PDF uploaded as an image type** — Cloudinary accepts it and then refuses
    to deliver it, so the upload succeeds, no fallback fires, and the
    customer's download link 403s.
  * **Converting without resizing** — WebP alone leaves a 6000px camera photo
    at 6000px. The cap has to come first.
  * **Modules that never write to the activity log** — Scans called a function
    that did not exist, inside a bare except, and no scan reached /activity for
    the life of the module.
  * **OpenAI called outside the shared client** — spend that never appears in
    the cost estimate, so the number quietly understates the bill.
  * **Unclamped list limits** — `?limit=-1` was a 500 on Postgres and a full
    table dump on SQLite.
  * **JSON written to the disk with no copy in the database** — the disk is
    not in the database backup and comes back empty if it is recreated, and a
    module reading an empty file looks like a module with nothing in it.

Read-only and cheap: it reads source, never runs it, and touches no API.
"""
from __future__ import annotations

import os
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Third-party code is not ours to fix, and a local virtualenv sits inside the
# repo — without these, the scan reports the openai package itself for "spend
# not recorded" and buries the findings that are actually actionable.
SKIP_DIRS = {"_attic", "__pycache__", ".git", "node_modules",
             ".venv", "venv", "env", "site-packages", ".tox", "build", "dist"}
# Files that legitimately mention these patterns without doing the thing.
SELF = {"hub/integrity.py", "hub/storage.py", "hub/images.py", "hub/ai.py",
        "hub/quotas.py", "hub/diagnostics.py", "hub/demo.py", "hub/demos.py"}


def _sources():
    for p in ROOT.rglob("*.py"):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        rel = p.relative_to(ROOT).as_posix()
        if rel in SELF:
            continue
        try:
            yield rel, p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue


def _module_of(rel: str) -> str:
    parts = rel.split("/")
    if parts[0] == "modules" and len(parts) > 1:
        return parts[1]
    return parts[0] if parts[0] != "hub" else rel


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def check_pdf_resource_type() -> list[dict]:
    """A PDF or DOC uploaded as resource_type="image" cannot be delivered."""
    out = []
    for rel, src in _sources():
        for m in re.finditer(r'resource_type\s*=\s*"image"', src):
            window = src[max(0, m.start() - 700):m.start() + 200].lower()
            if any(k in window for k in (".pdf", "'pdf'", '"pdf"', "docx", "proposal")):
                out.append({
                    "file": rel, "module": _module_of(rel),
                    "detail": "An upload near a PDF/DOC path uses "
                              'resource_type="image". Cloudinary will accept it '
                              "and then refuse to deliver it — the link 403s.",
                    "fix": 'Use "raw", or route it through hub.storage.put(), '
                           "which derives the type from the file.",
                })
    return out


def check_convert_without_resize() -> list[dict]:
    """Converting to WebP without capping the longest edge shrinks nothing."""
    out = []
    for rel, src in _sources():
        # Only a real Pillow save counts. `format="webp"` as a Cloudinary
        # upload kwarg is a delivery instruction, not a local conversion, and
        # flagging it produced a false positive on page_image_optimizer.
        converts = re.search(r'\.save\(\s*\w+\s*,\s*["\']WEBP["\']', src, re.I)
        if not converts:
            continue
        # An export/download path renders at the size the user explicitly
        # chose (1x/2x/3x), so capping it would override their choice. That is
        # correct behaviour, not a missing resize — image_creator's export was
        # the other false positive.
        if re.search(r"as_attachment|send_file|attachment;|Content-Disposition", src, re.I):
            continue
        caps = re.search(r"\.thumbnail\(|max_edge|MAX_EDGE|\.resize\(|LANCZOS"
                         r"|c_limit|crop.*limit", src, re.I)
        if not caps:
            out.append({
                "file": rel, "module": _module_of(rel),
                "detail": "Converts to WebP but never caps the longest edge. "
                          "A 6000px camera photo stays 6000px and stays huge.",
                "fix": "Cap the edge first — hub.images.optimise() does it in "
                       "the right order, with EXIF rotation applied before the cap.",
            })
    return out


def check_untracked_openai() -> list[dict]:
    """OpenAI calls that bypass hub.ai, so their spend never reaches /diagnostics."""
    out = []
    markers = ("/v1/chat/completions", "/v1/responses", "/v1/images/generations",
               "chat.completions.create", "responses.create")
    for rel, src in _sources():
        if not any(m in src for m in markers):
            continue
        if ("hub.ai" in src or "from hub import ai" in src
                or "from . import ai" in src):
            continue
        out.append({
            "file": rel, "module": _module_of(rel),
            "detail": "Calls OpenAI without recording usage, so this spend is "
                      "invisible in the cost estimate.",
            "fix": "Add hub.ai.note_usage() (raw HTTP) or note_sdk_usage() "
                   "(OpenAI SDK) after the response — one line, no logic change.",
        })
    return out


def check_silent_modules() -> list[dict]:
    """A module that never writes to the activity log is unauditable."""
    out = []
    seen: dict[str, bool] = {}
    for rel, src in _sources():
        if not rel.startswith("modules/"):
            continue
        mod = _module_of(rel)
        if mod.endswith(".py"):        # modules/__init__.py is not a module
            continue
        logs = ("hub import audit" in src or "hub.audit" in src
                or "audit.log(" in src or "for_module(" in src)
        seen[mod] = seen.get(mod, False) or logs
    for mod, logs in sorted(seen.items()):
        if not logs:
            out.append({
                "file": f"modules/{mod}/", "module": mod,
                "detail": "Never writes to the activity log, so nothing this "
                          "module does is attributable.",
                "fix": "log = audit.for_module(\"" + mod + "\") and call it on "
                       "the actions that matter.",
            })
    return out


def check_unclamped_limits() -> list[dict]:
    """A limit read straight from the query string with no bounds."""
    out = []
    for rel, src in _sources():
        # The helper's own docstring quotes the bad pattern as the example
        # of what not to write. Flagging it would be the check reporting
        # its own fix as the defect.
        if rel.replace("\\", "/").endswith("hub/webargs.py"):
            continue
        # The same defect arrives on a form field or a JSON body, not just
        # the query string, so all three are covered.
        # Two patterns, because the names mean different things by source.
        # On a query string "items" and "n" are page sizes; in a JSON body
        # they are almost always the payload itself — a proposal's line items,
        # a spot count — and matching those made the check cry wolf on twelve
        # call sites that were never limits. A check nobody trusts is worse
        # than no check.
        pats = (r"""request\.args\.get\(\s*["'](limit|items|per_page|n)["']""",
                r"""(?:request\.form|body|data)\.get\("""
                r"""\s*["'](limit|per_page|page_size)["']""")
        for m in [m for pat in pats for m in re.finditer(pat, src)]:
            # Look BEHIND as well as ahead: the usual clamp wraps the read
            # rather than following it — max(1, min(500, int(request.args...)))
            # — so a forward-only window reports correctly-clamped code as
            # unclamped. A check that cries wolf gets ignored.
            line_start = src.rfind("\n", 0, m.start()) + 1
            window = src[line_start:m.start() + 260]
            if re.search(r"\bmin\(|\bmax\(|clamp", window):
                continue
            out.append({
                "file": rel, "module": _module_of(rel),
                "detail": f"?{m.group(1)}= is used without clamping. "
                          "?limit=-1 was a 500 on Postgres and a full table "
                          "dump on SQLite.",
                "fix": "from hub.webargs import clamp_int — it parses safely "
                       "and clamps both ends. Do not add another local "
                       "min()/max(); that is how two files ended up with "
                       "max(1, max(1, min(...))).",
            })
    return out


def check_bare_except_pass() -> list[dict]:
    """`except: pass` with no logging — how the Scans audit bug hid for weeks."""
    out = []
    pattern = re.compile(r"except[^\n:]*:\s*\n\s*pass\b")
    for rel, src in _sources():
        hits = len(pattern.findall(src))
        if hits >= 6:
            out.append({
                "file": rel, "module": _module_of(rel),
                "detail": f"{hits} silent `except: pass` blocks. Scans called a "
                          "function that did not exist inside one of these, and "
                          "no scan reached the activity log for the module's "
                          "entire life.",
                "fix": "Log the exception, or narrow the except to what you "
                       "actually expect. Silence is only safe when the failure "
                       "genuinely does not matter.",
            })
    return out



def check_shadowed_routes() -> list[dict]:
    """Hub routes hidden behind a mounted module's prefix.

    DispatcherMiddleware routes purely by URL prefix, so anything under
    /sites, /scans, /google and the rest goes to that module — a route
    registered on the hub app at /sites/match is never reached and 404s. This
    has now caught three separate features (Tickets, bulk scan, Match Sites),
    which is enough times to make it a check rather than a lesson.
    """
    import re
    try:
        src = (ROOT / "wsgi.py").read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    mounts = sorted(set(re.findall(r'"(/[a-z0-9/_-]+)":\s*_mount', src)))
    if not mounts:
        return []
    hub_src = (ROOT / "hub" / "__init__.py").read_text(encoding="utf-8", errors="ignore")
    out = []
    for m_route in re.finditer(r'@app\.route\(\s*"([^"]+)"', hub_src):
        path = m_route.group(1)
        for m in mounts:
            if path == m or path.startswith(m + "/"):
                out.append({
                    "file": "hub/__init__.py", "module": "hub",
                    "detail": f"{path} is registered on the hub app but sits "
                              f"under the mounted prefix {m}, so the request "
                              f"never reaches it — it 404s.",
                    "fix": f"Move it outside {m} (for example /tools/…), or "
                           f"register it inside that module instead.",
                })
                break
    return out


def check_shared_services() -> list[dict]:
    """Modules still doing Cloudinary, image work or settings themselves.

    Not a defect on its own — these all work. It is here so the migration is
    visible and shrinking, rather than something everyone means to get to. The
    rule is to move a module onto the shared code when you are already editing
    it for another reason.
    """
    # An image editor using PIL is not a module that should have used the
    # shared optimiser — cropping, rotating and resizing to an exact width are
    # operations hub.images does not offer and should not. Flagging it forever
    # produces a finding nobody can action, which is how a report stops being
    # read at all.
    editors = {"modules/image_optimizer/app.py"}

    out = []
    for rel, src in _sources():
        if not rel.startswith("modules/") or rel in editors:
            continue
        mod = _module_of(rel)
        uses_shared = ("hub.storage" in src or "hub.images" in src
                       or "from hub.config import" in src
                       or "from hub import config" in src)
        if uses_shared:
            continue
        own = []
        if "cloudinary.config(" in src:
            own.append("its own Cloudinary setup")
        if "thumbnail(" in src or "LANCZOS" in src:
            own.append("its own image resizing")
        if not own:
            continue
        out.append({
            "file": rel, "module": mod,
            "detail": f"{mod} has {' and '.join(own)} rather than using "
                      f"hub/storage.py and hub/images.py. A fix to the shared "
                      f"code will not reach it.",
            "fix": "Move it across next time you are editing this module for "
                   "another reason — not as a separate project.",
        })
    return out


def check_unbacked_json() -> list[dict]:
    """JSON written straight to the disk, with no copy in the database.

    Render's managed Postgres is backed up. The 5 GB disk mounted at /var/data
    is not, and an empty one is what comes back from a plan change, a region
    move or a resize. For a cache that is a non-event; for a file that is the
    only copy of something it is unrecoverable loss, and — because the module
    keeps working perfectly on an empty file — loss that announces itself as
    "the list is empty" rather than as an error.

    ``hub/jsonstore.py`` closes that by mirroring each write into the database
    and restoring on a miss. This check lists what has not moved across yet, so
    the remainder is a visible, shrinking number rather than something everyone
    means to get to. It is not a defect on its own: a module here works exactly
    as it always has, right up until the disk is recreated.
    """
    # Files that legitimately write JSON somewhere other than the data disk:
    # repo fixtures, build scripts and one-off tools, none of which hold state.
    exempt_files = {"hub/jsonstore.py", "hub/errors.py", "hub/audit.py",
                    "modules/ad_builder/scripts/fix_safezones.py",
                    "install_into_hub.py", "ui_check.py"}
    out = []
    for rel, src in _sources():
        if rel in exempt_files or "/scripts/" in rel or rel.startswith("tools/"):
            continue
        if "json.dump(" not in src:
            continue
        # json.dump (not dumps) always writes to a file object, so its
        # presence is the signal. Matching on a "*_DIR" name instead missed
        # every module that called its directory something else — REPORT_DIR,
        # REPORTS_DIR — which is most of the ones worth finding.
        if "jsonstore" in src:
            continue                    # already mirrored
        mod = _module_of(rel)
        out.append({
            "file": rel, "module": mod,
            "detail": f"{mod} writes JSON to the persistent disk without a "
                      f"copy in the database. The disk is outside the database "
                      f"backup and does not survive being recreated, so if "
                      f"this file is the only copy of something, it is "
                      f"unrecoverable.",
            "fix": "Read and write through hub/jsonstore.py — read_json / "
                   "write_json / delete_json — which keeps the same atomic "
                   "write and adds the mirror. If the file is genuinely "
                   "rebuildable, pass durable=False and say why.",
        })
    return out


CHECKS = [
    ("pdf_resource_type", "PDF uploaded as an image type", "high", check_pdf_resource_type),
    ("convert_without_resize", "Converts without resizing", "high", check_convert_without_resize),
    ("untracked_openai", "OpenAI spend not recorded", "medium", check_untracked_openai),
    ("silent_modules", "Modules that never log", "medium", check_silent_modules),
    ("unclamped_limits", "Unclamped query limits", "medium", check_unclamped_limits),
    ("shadowed_routes", "Routes hidden behind a mount", "high", check_shadowed_routes),
    ("bare_except_pass", "Silent exception handling", "low", check_bare_except_pass),
    ("shared_services", "Not yet on shared services", "low", check_shared_services),
    ("unbacked_json", "JSON on the disk with no backup", "medium", check_unbacked_json),
]


def run() -> dict:
    groups, total = [], 0
    for key, label, severity, fn in CHECKS:
        try:
            findings = fn()
        except Exception as exc:                        # noqa: BLE001
            findings = [{"file": "-", "module": "-",
                         "detail": f"Check failed: {type(exc).__name__}",
                         "fix": ""}]
        total += len(findings)
        groups.append({"key": key, "label": label, "severity": severity,
                       "count": len(findings), "findings": findings,
                       "state": "ok" if not findings else severity})
    return {
        "groups": groups,
        "total": total,
        "clean": total == 0,
        "note": "Static read of the source. Every pattern here was a real "
                "shipped defect at least once — the check exists so it cannot "
                "return unnoticed in a module written later.",
    }
