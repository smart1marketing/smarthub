import datetime
import json
import os
import re
import tempfile
from hub import jsonstore
import uuid
from typing import Any

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, send_file

try:
    import cloudinary
    import cloudinary.uploader
    cloudinary.config(secure=True)  # reads CLOUDINARY_URL from the environment
    CLOUDINARY_READY = bool(os.getenv("CLOUDINARY_URL", "").strip())
except Exception:  # pragma: no cover
    cloudinary = None
    CLOUDINARY_READY = False

try:
    from fpdf import FPDF
    from fpdf.enums import XPos, YPos
    from fpdf.fonts import FontFace
    FPDF_READY = True
except Exception:  # pragma: no cover
    FPDF_READY = False
    # Stand-ins so the ReportPDF class below can still be defined. Without
    # them the class statement raises NameError at import and the entire
    # landing page 503s — losing the page, not just the PDF download, which
    # is the opposite of what the FPDF_READY flag is for. Nothing constructs
    # ReportPDF unless FPDF_READY is true.
    FPDF = object
    XPos = YPos = FontFace = None

load_dotenv()

app = Flask(__name__)

MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
# Standardized on GHL_WEBHOOK_URL (falls back to the old SMART1_WEBHOOK_URL if present).
WEBHOOK_URL = (os.getenv("GHL_WEBHOOK_URL", "") or os.getenv("SMART1_WEBHOOK_URL", "")).strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")

# Was tempfile.gettempdir(). A report here is what /r/<id> serves to a dealer
# we sent the link to, so a temp directory meant every shared link 404'd on the
# next deploy — "This report link is no longer available", for a report that
# was fine an hour ago. On the persistent disk and mirrored to the database,
# the link keeps working. The generated PDF stays alongside it and is not
# mirrored: it is rebuilt from this JSON, so it is a genuine cache.
REPORTS_DIR = jsonstore.data_dir("boat-reports")


def slugify(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (s or "").strip().lower()).strip("-")
    return s or "dealership"


def _pdf_text(s: Any) -> str:
    """Sanitize text for the core-font PDF (latin-1)."""
    s = "" if s is None else str(s)
    for a, b in {"—": "-", "–": "-", "‘": "'", "’": "'",
                 "“": '"', "”": '"', "…": "...", "•": "-",
                 " ": " ", "→": ">"}.items():
        s = s.replace(a, b)
    return s.encode("latin-1", "replace").decode("latin-1")


def save_report(report: Any, dealer_name: str) -> str:
    rid = uuid.uuid4().hex[:12]
    jsonstore.write_json(os.path.join(REPORTS_DIR, f"{rid}.json"),
                         {"report": report, "dealer_name": dealer_name})
    return rid


def base_url() -> str:
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL
    base = request.url_root.rstrip("/")
    if base.startswith("http://"):
        base = "https://" + base[len("http://"):]
    return base


NAVY = (10, 34, 64)   # brand navy #0A2240
BLUE = (0, 158, 210)  # brand blue #009ED2
TEAL = (23, 201, 176)
GREY = (108, 122, 140)
INK = (37, 54, 75)
LINE = (214, 226, 236)
CARD = (247, 250, 252)
CONSULT_URL = "https://smart1marketing.com/boatmarketingconsult"
SOURCE_NAME = "Smart 1 Boat Dealer Market Intelligence"


class ReportPDF(FPDF):
    dealer_name = ""

    def _wordmark(self, x, y, dark_bg=True):
        s = 7.2
        self.set_fill_color(*BLUE)
        self.rect(x, y, s, s, style="F", round_corners=True, corner_radius=1.6)
        self.set_fill_color(255, 255, 255)
        self.ellipse(x + s / 2 - 1.7, y + 1.3, 3.4, 3.4, style="F")
        self.rect(x + s / 2 - 1.0, y + 4.5, 2.0, 1.4, style="F")
        tx = x + s + 2.6
        ty = y + s / 2 - 3.4
        white = (255, 255, 255) if dark_bg else NAVY
        self.set_font("Helvetica", "B", 15)
        self.set_xy(tx, ty)
        self.set_text_color(*white)
        self.cell(self.get_string_width("SMART"), 6.8, "SMART", new_x=XPos.RIGHT, new_y=YPos.TOP)
        self.set_text_color(*BLUE)
        self.cell(self.get_string_width("1"), 6.8, "1", new_x=XPos.RIGHT, new_y=YPos.TOP)
        self.set_font("Helvetica", "", 15)
        self.set_text_color(*white)
        self.cell(self.get_string_width("MARKETING"), 6.8, "MARKETING", new_x=XPos.RIGHT, new_y=YPos.TOP)

    def header(self):
        first = self.page_no() == 1
        band_h = 32 if first else 17
        self.set_fill_color(*NAVY)
        self.rect(0, 0, self.w, band_h, style="F")
        self.set_fill_color(*TEAL)
        self.rect(0, band_h, self.w, 2.0, style="F")
        if first:
            self._wordmark(self.l_margin, 7)
            self.set_xy(self.l_margin, 17)
            self.set_font("Helvetica", "B", 18)
            self.set_text_color(255, 255, 255)
            self.cell(0, 8, "Weather Marketing Proposal", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            self.set_x(self.l_margin)
            self.set_font("Helvetica", "", 9.5)
            self.set_text_color(196, 214, 236)
            self.cell(0, 5, _pdf_text("Weather-triggered advertising plan prepared for your market"),
                      new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        else:
            self._wordmark(self.l_margin, 4.6)
            self.set_xy(self.l_margin, 6.6)
            self.set_font("Helvetica", "", 9)
            self.set_text_color(196, 214, 236)
            self.cell(self.w - 2 * self.l_margin, 6, _pdf_text(self.dealer_name or ""),
                      align="R", new_x=XPos.LMARGIN, new_y=YPos.TOP)
        self.set_y(band_h + (10 if first else 8))

    def footer(self):
        self.set_y(-14)
        self.set_draw_color(*LINE)
        self.set_line_width(0.2)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(1.5)
        self.set_font("Helvetica", "", 7)
        self.set_text_color(*GREY)
        self.cell(self.w - 2 * self.l_margin - 20, 4,
                  _pdf_text("Smart 1 Marketing · (614) 536-0768 · smart1marketing.com  -  AI market estimates; verify locations and figures before activation."),
                  new_x=XPos.RIGHT, new_y=YPos.TOP)
        self.set_font("Helvetica", "B", 7)
        self.cell(20, 4, f"{self.page_no()} / {{nb}}", align="R", new_x=XPos.LMARGIN, new_y=YPos.TOP)


def build_pdf(report: Any, dealer_name: str, payload: Any = None) -> str:
    """Render the report into a branded PDF and return the local file path."""
    r = report or {}
    p = payload or {}
    m = r.get("market_profile", {}) or {}
    pkg = r.get("recommended_package", {}) or {}
    pdf = ReportPDF(format="A4")
    pdf.dealer_name = dealer_name or "Boat Dealer"
    pdf.set_margins(16, 16, 16)
    pdf.set_auto_page_break(True, margin=18)
    pdf.alias_nb_pages()
    pdf.add_page()
    W = pdf.w - pdf.l_margin - pdf.r_margin
    L = pdf.l_margin

    def fmt(n):
        try:
            return f"{int(n):,}"
        except Exception:
            return str(n or "")

    def rng(a, b):
        return f"{fmt(a)}-{fmt(b)}"

    def keep(h):
        if pdf.get_y() + h > pdf.page_break_trigger:
            pdf.add_page()

    def wrap_count(text, width, style="", size=10):
        pdf.set_font("Helvetica", style, size)
        words = _pdf_text(text).split()
        if not words:
            return 1
        lines, cur = 1, ""
        for w in words:
            trial = (cur + " " + w).strip()
            if pdf.get_string_width(trial) <= width or not cur:
                cur = trial
            else:
                lines += 1
                cur = w
        return lines

    def label(txt):
        keep(22)
        pdf.ln(3)
        pdf.set_font("Helvetica", "B", 8.5)
        pdf.set_text_color(*GREY)
        pdf.cell(0, 5, _pdf_text(txt.upper()), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        y = pdf.get_y() + 1.2
        pdf.set_draw_color(*LINE)
        pdf.set_line_width(0.3)
        pdf.line(L, y, pdf.w - pdf.r_margin, y)
        pdf.ln(4)

    def body(txt, size=10.5):
        pdf.set_font("Helvetica", "", size)
        pdf.set_text_color(*INK)
        pdf.multi_cell(W, 5.6, _pdf_text(txt), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    def badge(txt):
        keep(12)
        pdf.set_font("Helvetica", "B", 9)
        tw = pdf.get_string_width(_pdf_text(txt)) + 10
        y0 = pdf.get_y()
        pdf.set_fill_color(*NAVY)
        pdf.rect(L, y0, tw, 7.5, style="F", round_corners=True, corner_radius=1.6)
        pdf.set_xy(L, y0)
        pdf.set_text_color(255, 255, 255)
        pdf.cell(tw, 7.5, _pdf_text(txt), align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(3)

    def info_card(pairs):
        cols = 3
        rows = (len(pairs) + cols - 1) // cols
        cell_h, pad = 13, 5
        card_h = rows * cell_h + pad
        keep(card_h + 4)
        x0, y0 = L, pdf.get_y()
        pdf.set_draw_color(*LINE)
        pdf.set_line_width(0.3)
        pdf.rect(x0, y0, W, card_h, style="D", round_corners=True, corner_radius=2.2)
        cw = W / cols
        for i, (lab, val) in enumerate(pairs):
            rr, cc = divmod(i, cols)
            cx = x0 + cc * cw + pad
            cy = y0 + rr * cell_h + pad
            pdf.set_xy(cx, cy)
            pdf.set_font("Helvetica", "B", 7)
            pdf.set_text_color(*GREY)
            pdf.cell(cw - pad, 4, _pdf_text(lab.upper()), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.set_xy(cx, cy + 4.4)
            pdf.set_font("Helvetica", "B", 10.5)
            pdf.set_text_color(*NAVY)
            pdf.multi_cell(cw - pad, 4.6, _pdf_text(val), new_x=XPos.LMARGIN, new_y=YPos.TOP)
        pdf.set_y(y0 + card_h + 3)

    def stat_cards(cards):
        keep(28)
        gap = 4
        cw = (W - gap * (len(cards) - 1)) / len(cards)
        y0, h = pdf.get_y(), 22
        for i, (big, lbl) in enumerate(cards):
            x = L + i * (cw + gap)
            pdf.set_fill_color(*CARD)
            pdf.set_draw_color(*LINE)
            pdf.set_line_width(0.3)
            pdf.rect(x, y0, cw, h, style="DF", round_corners=True, corner_radius=2.2)
            pdf.set_xy(x, y0 + 4)
            pdf.set_font("Helvetica", "B", 15)
            pdf.set_text_color(*NAVY)
            pdf.cell(cw, 8, _pdf_text(big), align="C", new_x=XPos.LMARGIN, new_y=YPos.TOP)
            pdf.set_xy(x + 3, y0 + 13)
            pdf.set_font("Helvetica", "", 7.5)
            pdf.set_text_color(*GREY)
            pdf.multi_cell(cw - 6, 3.6, _pdf_text(lbl), align="C", new_x=XPos.LMARGIN, new_y=YPos.TOP)
        pdf.set_y(y0 + h + 3)

    def pricing_cards(scenarios):
        keep(40)
        gap = 4
        n = len(scenarios)
        cw = (W - gap * (n - 1)) / n
        y0, h = pdf.get_y(), 34
        for i, sc in enumerate(scenarios):
            x = L + i * (cw + gap)
            rec = str(sc.get("tier", "")).lower() == "better"
            if rec:
                pdf.set_fill_color(239, 249, 252)
                pdf.set_draw_color(*BLUE)
                pdf.set_line_width(0.6)
            else:
                pdf.set_fill_color(255, 255, 255)
                pdf.set_draw_color(*LINE)
                pdf.set_line_width(0.3)
            pdf.rect(x, y0, cw, h, style="DF", round_corners=True, corner_radius=2.2)
            pdf.set_xy(x, y0 + 3.5)
            pdf.set_font("Helvetica", "B", 7)
            pdf.set_text_color(*(BLUE if rec else GREY))
            tier = str(sc.get("tier", "")).upper() + (" - RECOMMENDED" if rec else "")
            pdf.cell(cw, 4, _pdf_text(tier), align="C", new_x=XPos.LMARGIN, new_y=YPos.TOP)
            pdf.set_xy(x, y0 + 8.5)
            pdf.set_font("Helvetica", "B", 13)
            pdf.set_text_color(*NAVY)
            pdf.cell(cw, 6, _pdf_text(sc.get("monthly_investment", "")), align="C", new_x=XPos.LMARGIN, new_y=YPos.TOP)
            pdf.set_xy(x + 2, y0 + 15)
            pdf.set_font("Helvetica", "B", 8)
            pdf.set_text_color(*INK)
            pdf.multi_cell(cw - 4, 4, _pdf_text(sc.get("package_name", "")), align="C", new_x=XPos.LMARGIN, new_y=YPos.TOP)
            pdf.set_xy(x + 2.5, y0 + 21.5)
            pdf.set_font("Helvetica", "", 7)
            pdf.set_text_color(*GREY)
            pdf.multi_cell(cw - 5, 3.4, _pdf_text(sc.get("summary", "")), align="C", new_x=XPos.LMARGIN, new_y=YPos.TOP)
        pdf.set_y(y0 + h + 3)

    def chips(items):
        keep(14)
        x, y = L, pdf.get_y()
        line_h = 7.5
        pdf.set_font("Helvetica", "B", 8)
        for it in (items or []):
            t = _pdf_text(str(it))
            tw = pdf.get_string_width(t) + 7
            if x + tw > L + W:
                x = L
                y += line_h + 1.5
            pdf.set_fill_color(*CARD)
            pdf.set_draw_color(*LINE)
            pdf.set_line_width(0.3)
            pdf.rect(x, y, tw, line_h, style="DF", round_corners=True, corner_radius=1.6)
            pdf.set_xy(x, y)
            pdf.set_text_color(*NAVY)
            pdf.cell(tw, line_h, t, align="C", new_x=XPos.RIGHT, new_y=YPos.TOP)
            x += tw + 3
        pdf.set_y(y + line_h + 2)

    def page_break_before():
        if pdf.get_y() > 45:
            pdf.add_page()

    def media_cards(items):
        items = [str(x) for x in (items or [])]
        cols, gap, ch = 2, 4, 12
        cw = (W - gap * (cols - 1)) / cols
        rowy = pdf.get_y()
        for i, it in enumerate(items):
            col = i % cols
            if col == 0:
                keep(ch + 2)
                rowy = pdf.get_y()
            x = L + col * (cw + gap)
            pdf.set_fill_color(*CARD)
            pdf.set_draw_color(*LINE)
            pdf.set_line_width(0.3)
            pdf.rect(x, rowy, cw, ch, style="DF", round_corners=True, corner_radius=2)
            pdf.set_fill_color(*BLUE)
            pdf.rect(x + 2.5, rowy + 2.5, 7, 7, style="F", round_corners=True, corner_radius=1.4)
            pdf.set_xy(x + 2.5, rowy + 2.5)
            pdf.set_font("Helvetica", "B", 8)
            pdf.set_text_color(255, 255, 255)
            pdf.cell(7, 7, (it.strip()[:1] or "-").upper(), align="C", new_x=XPos.LMARGIN, new_y=YPos.TOP)
            pdf.set_xy(x + 11.5, rowy + 2.2)
            pdf.set_font("Helvetica", "B", 8)
            pdf.set_text_color(*NAVY)
            pdf.multi_cell(cw - 13.5, 3.8, _pdf_text(it), new_x=XPos.LMARGIN, new_y=YPos.TOP)
            if col == cols - 1:
                pdf.set_y(rowy + ch + 3)
        if len(items) % cols != 0:
            pdf.set_y(rowy + ch + 3)

    def explain_rows(rows):
        for lead, txt in rows:
            keep(14)
            pdf.set_font("Helvetica", "B", 9.5)
            pdf.set_text_color(*NAVY)
            pdf.multi_cell(W, 4.8, _pdf_text(lead), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.set_font("Helvetica", "", 9)
            pdf.set_text_color(*INK)
            pdf.multi_cell(W, 4.6, _pdf_text(txt), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(1.5)

    def note_box(text, accent=BLUE, fill=(239, 249, 252)):
        lines = wrap_count(text, W - 12, "", 9)
        h = lines * 4.8 + 8
        keep(h + 4)
        x0, y0 = L, pdf.get_y()
        pdf.set_fill_color(*fill)
        pdf.rect(x0, y0, W, h, style="F", round_corners=True, corner_radius=2)
        pdf.set_fill_color(*accent)
        pdf.rect(x0, y0, 1.6, h, style="F")
        pdf.set_xy(x0 + 6, y0 + 4)
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(*INK)
        pdf.multi_cell(W - 12, 4.8, _pdf_text(text), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_y(y0 + h + 3)

    def savings_box(sav):
        headline = sav.get("headline", "")
        summary = sav.get("summary", "")
        points = sav.get("points", []) or []
        hl = wrap_count(headline, W - 12, "B", 11) * 5.2
        sl = (wrap_count(summary, W - 12, "", 8.5) * 4.6) if summary else 0
        pl = sum(wrap_count(str(pt), W - 16, "", 8.5) * 4.6 + 1.2 for pt in points)
        h = 6 + 4 + 1 + hl + 1 + sl + 2 + pl + 4
        keep(h + 4)
        x0, y0 = L, pdf.get_y()
        pdf.set_fill_color(*NAVY)
        pdf.rect(x0, y0, W, h, style="F", round_corners=True, corner_radius=2.5)
        inx = x0 + 6
        pdf.set_xy(inx, y0 + 6)
        pdf.set_font("Helvetica", "B", 7.5)
        pdf.set_text_color(*TEAL)
        pdf.cell(W - 12, 4, _pdf_text("WHY THIS SAVES YOU MONEY"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_xy(inx, pdf.get_y() + 1)
        pdf.set_font("Helvetica", "B", 11)
        pdf.set_text_color(255, 255, 255)
        pdf.multi_cell(W - 12, 5.2, _pdf_text(headline), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        if summary:
            pdf.set_x(inx)
            pdf.set_font("Helvetica", "", 8.5)
            pdf.set_text_color(210, 224, 240)
            pdf.multi_cell(W - 12, 4.6, _pdf_text(summary), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(1)
        for pt in points:
            yb = pdf.get_y()
            pdf.set_xy(inx, yb)
            pdf.set_font("Helvetica", "B", 9)
            pdf.set_text_color(*TEAL)
            pdf.cell(4, 4.6, "+", new_x=XPos.RIGHT, new_y=YPos.TOP)
            pdf.set_font("Helvetica", "", 8.5)
            pdf.set_text_color(230, 238, 248)
            pdf.multi_cell(W - 16, 4.6, _pdf_text(str(pt)), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(1.2)
        pdf.set_y(y0 + h + 3)

    def cta_box():
        h = 27
        keep(h + 4)
        x0, y0 = L, pdf.get_y()
        pdf.set_fill_color(*NAVY)
        pdf.rect(x0, y0, W, h, style="F", round_corners=True, corner_radius=2.5)
        pdf.set_xy(x0 + 6, y0 + 5)
        pdf.set_font("Helvetica", "B", 12)
        pdf.set_text_color(255, 255, 255)
        pdf.cell(W - 12, 6, _pdf_text("Ready to turn this into a live campaign?"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_x(x0 + 6)
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(210, 224, 240)
        pdf.multi_cell(W - 12, 4.6, _pdf_text("A Smart 1 strategist will verify these locations, build your polygons, and tailor a budget that fits your dealership."),
                       new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_xy(x0 + 6, pdf.get_y() + 1)
        pdf.set_font("Helvetica", "B", 9.5)
        pdf.set_text_color(*TEAL)
        pdf.cell(W - 12, 6, _pdf_text("Book a consult:  " + CONSULT_URL), new_x=XPos.LMARGIN, new_y=YPos.NEXT, link=CONSULT_URL)
        pdf.set_y(y0 + h + 3)

    # ---- Title block ----
    pdf.set_font("Helvetica", "B", 21)
    pdf.set_text_color(*NAVY)
    pdf.multi_cell(W, 9, _pdf_text(dealer_name or "Boat Dealer"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", "", 9.5)
    pdf.set_text_color(*GREY)
    pdf.cell(0, 5, _pdf_text("Prepared by Smart 1 Marketing   -   " + datetime.date.today().strftime("%B %d, %Y")),
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(4)

    market_line = "  -  ".join([x for x in [p.get("dealer_zip", ""), p.get("target_radius", "")] if x]) or "-"
    info_card([
        ("Market", market_line),
        ("Est. boat-owner HH", rng(m.get("estimated_boat_owner_households_low"), m.get("estimated_boat_owner_households_high"))),
        ("Recommended plan", pkg.get("monthly_investment", "-")),
        ("Media approach", "Weather-triggered"),
        ("Budget pacing", "Peak / Shoulder / Off"),
        ("Prepared", datetime.date.today().strftime("%b %d, %Y")),
    ])

    if r.get("market_type"):
        badge(r.get("market_type"))
    if r.get("market_type_description"):
        body(r.get("market_type_description"))
    if r.get("market_summary"):
        pdf.ln(1)
        body(r.get("market_summary"))

    label("Market Estimate")
    stat_cards([
        (rng(m.get("estimated_population_low"), m.get("estimated_population_high")), "Estimated population"),
        (rng(m.get("estimated_households_low"), m.get("estimated_households_high")), "Estimated households"),
        (rng(m.get("estimated_boat_owner_households_low"), m.get("estimated_boat_owner_households_high")), "Likely boat-owner households"),
    ])

    if r.get("market_opportunity"):
        label("Your Market Opportunity")
        body(r.get("market_opportunity"))

    scenarios = r.get("pricing_scenarios") or []
    if scenarios:
        label("Investment Scenarios (Good / Better / Best)")
        pricing_cards(scenarios[:3])
    if pkg.get("package_name") or pkg.get("description"):
        pdf.set_font("Helvetica", "B", 11)
        pdf.set_text_color(*NAVY)
        pdf.multi_cell(W, 5.6, _pdf_text(f"Recommended: {pkg.get('monthly_investment','')} {pkg.get('package_name','')}"),
                       new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        if pkg.get("description"):
            body(pkg.get("description"))
    note_box("This is a suggested budget based on your market's size. In a quick consult we'll tailor a budget and plan that fits your dealership's goals, inventory, and cash flow.")

    sav = r.get("savings_comparison") or {}
    if sav.get("headline") or sav.get("summary"):
        label("How This Saves You Money")
        savings_box(sav)

    if r.get("media_channels"):
        label("Recommended Media Channels")
        media_cards(r.get("media_channels"))
    if r.get("streaming_audio_note"):
        label("Streaming Audio - Water-Access Daypart")
        body(r.get("streaming_audio_note"))
        pdf.ln(1)
        explain_rows([
            ("Where boaters listen", "Boaters stream music and podcasts on the dock, on the water, and on the drive to the ramp - screens-down moments most channels miss."),
            ("How we target", "We geofence this market's marinas, boat ramps, launch points, and lakeshore so ads reach people already at or near the water."),
            ("When it runs", "A sunrise-to-sunset daypart on boating-favorable days and weekends, so spend follows real on-the-water activity."),
        ])
    if r.get("weather_triggers"):
        label("Recommended Weather Triggers")
        chips(r.get("weather_triggers"))

    mp = r.get("monthly_plan") or []
    if mp:
        page_break_before()
        label("Month-by-Month Campaign Plan")
        head = FontFace(emphasis="BOLD", color=(255, 255, 255), fill_color=NAVY)
        pdf.set_font("Helvetica", "", 8.5)
        pdf.set_text_color(*INK)
        pdf.set_draw_color(*LINE)
        with pdf.table(headings_style=head, col_widths=(16, 60, 24), text_align="LEFT",
                       first_row_as_headings=True, borders_layout="HORIZONTAL_LINES") as t:
            t.row(["Month", "Focus & Message", "Budget"])
            for row in mp:
                focus = _pdf_text(str(row.get("focus", "")))
                msg = _pdf_text(str(row.get("message", "")))
                t.row([_pdf_text(row.get("month", "")), focus + ("\n" + msg if msg else ""), _pdf_text(row.get("pacing", ""))])

    geo = sorted(r.get("geofence_locations") or [], key=lambda x: x.get("priority", 3))
    if geo:
        page_break_before()
        label("Sample Geofence Locations to Target")
        head = FontFace(emphasis="BOLD", color=(255, 255, 255), fill_color=NAVY)
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(*INK)
        pdf.set_draw_color(*LINE)
        with pdf.table(headings_style=head, col_widths=(8, 40, 26, 22, 12), text_align="LEFT",
                       first_row_as_headings=True, borders_layout="HORIZONTAL_LINES") as t:
            t.row(["P", "Location", "Category", "Method", "Radius"])
            for x in geo:
                t.row([
                    _pdf_text("P" + str(x.get("priority", ""))),
                    _pdf_text(f"{x.get('name','')}\n{x.get('city_state','')}"),
                    _pdf_text(x.get("category", "")),
                    _pdf_text(str(x.get("recommended_method", "")).replace("_", " ")),
                    _pdf_text(f"{x.get('recommended_radius_miles','')} mi"),
                ])

    label("Talk to a Strategist")
    cta_box()

    if r.get("disclaimer"):
        pdf.ln(2)
        pdf.set_font("Helvetica", "I", 8)
        pdf.set_text_color(*GREY)
        pdf.multi_cell(W, 4.5, _pdf_text(r.get("disclaimer")), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    path = os.path.join(tempfile.gettempdir(), f"boat-report-{uuid.uuid4().hex[:10]}.pdf")
    pdf.output(path)
    return path


def upload_report_pdf(path: str, dealer_name: str, rid: str) -> str:
    """Upload the PDF to Cloudinary and return its secure delivery URL."""
    if not (cloudinary and CLOUDINARY_READY):
        return ""
    public_id = f"boat-reports/{slugify(dealer_name)}-boat-report-{rid}"
    # Through hub.storage: same public_id, so nothing moves. "auto" is
    # replaced by the type hub.storage derives from the filename, which for a
    # PDF is raw — the delivery that actually works.
    from hub import storage
    with open(path, "rb") as fh:
        data = fh.read()
    return storage.put("landing_reports", f"{public_id}.pdf", data,
                       public_id=public_id, overwrite=True).url


SYSTEM_PROMPT = """
You are the Smart 1 Marketing Boat Dealer Market Intelligence Architect.
Create a practical, sales-oriented market and geofencing report for a boat dealership.

IMPORTANT ACCURACY RULES
- You do not have live access to maps, state boat-registration databases, or exact census tables unless supplied in the request.
- Use geographic knowledge and conservative planning assumptions. Never claim a location or statistic was live-verified.
- Clearly label all population, household, boat-owner, and registration figures as AI planning estimates.
- Give ranges, confidence levels, and a short explanation of the assumptions.
- Do not invent precise street addresses. Use recognizable place names plus city/state. An address field may be null.
- Prefer real, well-known waterways and boating facilities you are reasonably confident exist. If uncertain, lower confidence.
- Avoid duplicate locations and avoid recommending open water polygons that cannot be practically geofenced. Favor access points and businesses.

TARGET TYPES TO CONSIDER
1. Public boat ramps and launch facilities
2. Marinas and yacht clubs
3. Major lakes, reservoirs, navigable rivers, bays, and coastal access zones
4. Boat storage, dry-stack storage, winterization, repair, fuel docks, and marine-service facilities
5. Fishing tackle, watersports, marine-supply, and boating-event locations
6. Competing boat dealers and boat-show venues for conquesting
7. Affluent/high-homeownership ZIP clusters close to boating access
8. Seasonal tourism corridors and lake communities

BOAT-OWNER ESTIMATION METHOD
Estimate the TOTAL population and households inside the dealer's full travel radius (target_radius), not just the ZIP's town or county. Treat the radius as a circle and size the area accordingly: about 1,960 sq mi at 25 miles, 7,850 sq mi at 50 miles, 17,700 sq mi at 75 miles, 31,400 sq mi at 100 miles, and 70,700 sq mi at 150 miles. Apply a realistic regional population density for the area around the ZIP (dense metro radii cover several million people; a 50-75 mile radius around most U.S. metros covers hundreds of thousands to a few million residents). Do NOT under-count the population — reflect the true scale of the selected radius. Then estimate likely boat-owning households using a market-sensitive ownership rate. Use lower rates for dense urban inland areas, moderate rates for lake/river markets, and higher rates for coastal or lake-heavy markets. Adjust for income, homeownership, vehicle/trailer storage capacity, nearby navigable water, fishing culture, seasonality, and requested boat categories.
Return low/base/high ownership estimates. Do not present the estimate as registered-vessel data.

GEOFENCE GUIDANCE
- Recommend a practical radius or polygon approach for each location.
- Typical point-of-interest radii: 0.10-0.25 mile for compact ramps/dealers, 0.25-0.50 mile for marinas/storage, and polygons for larger venues.
- Separate "location lookback" sites from "real-time/proximity" sites.
- Rank locations Priority 1, 2, or 3.

MEDIA AND WEATHER-TRIGGER RULES
- The entire campaign is weather-triggered. Build the plan around boating-friendly weather signals.
- ALLOWED channels ONLY: geofencing, location look-back retargeting, programmatic / data-driven targeted display, CTV/OTT, streaming audio, YouTube/online video, and website retargeting.
- NEVER recommend social media or social advertising (Facebook, Instagram, TikTok, LinkedIn, Snapchat, Pinterest, X, or any other social channel).
- NEVER recommend paid search, email, or SMS. Do not mention them anywhere in the report.
- Build practical triggers around boating-friendly weather, such as temperature thresholds, rain probability, severe weather, wind, consecutive warm days, holiday/weekend forecasts, first-warm-weekend and end-of-season opportunities, first frost, frost warnings, freeze warnings, ice storms, and winterization warnings.
- ALWAYS include cold-weather triggers where seasonally relevant (freeze warnings, ice storms, frost warnings) — these drive winterization, storage, and service demand and are core to the plan, not optional.
- Keep weather-trigger labels short and punchy (e.g. "70°+ weekend", "Sunny weekend", "First frost", "Frost warning", "Freeze warning", "Ice storm", "Holiday weekend forecast").
- Do not imply that weather guarantees demand. Treat it as a budget-pacing and timing signal.

OUTPUT
Return only valid JSON matching the requested schema. Do not use markdown fences.
"""

REPORT_SCHEMA = {
    "name": "boat_dealer_report",
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
                    "estimated_boat_owner_households_low": {"type": "integer"},
                    "estimated_boat_owner_households_base": {"type": "integer"},
                    "estimated_boat_owner_households_high": {"type": "integer"},
                    "estimated_ownership_rate_low": {"type": "number"},
                    "estimated_ownership_rate_base": {"type": "number"},
                    "estimated_ownership_rate_high": {"type": "number"},
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
                    "estimated_boat_owner_households_low",
                    "estimated_boat_owner_households_base",
                    "estimated_boat_owner_households_high",
                    "estimated_ownership_rate_low",
                    "estimated_ownership_rate_base",
                    "estimated_ownership_rate_high",
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
            "pricing_scenarios": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "tier": {"type": "string", "enum": ["Good", "Better", "Best"]},
                        "package_name": {"type": "string"},
                        "monthly_investment": {"type": "string"},
                        "summary": {"type": "string"},
                    },
                    "required": ["tier", "package_name", "monthly_investment", "summary"],
                },
            },
            "savings_comparison": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "headline": {"type": "string"},
                    "summary": {"type": "string"},
                    "points": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["headline", "summary", "points"],
            },
            "media_channels": {"type": "array", "items": {"type": "string"}},
            "streaming_audio_note": {"type": "string"},
            "weather_triggers": {"type": "array", "items": {"type": "string"}},
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
                        "waterway_or_market": {"type": "string"},
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
                        "waterway_or_market",
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
            "pricing_scenarios",
            "savings_comparison",
            "media_channels",
            "streaming_audio_note",
            "weather_triggers",
            "monthly_plan",
            "geofence_locations",
            "disclaimer",
        ],
    },
    "strict": True,
}


EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}")

LEAD_FIELDS = [
    "dealer_name",
    "website",
    "dealer_zip",
    "target_radius",
    "boat_types",
    "new_used",
    "campaign_objective",
    "contact_name",
    "contact_email",
    "contact_phone",
    "notes",
    "lead_id",  # optional client-generated id so GHL can merge partial + final records
]


def split_name(full_name: str) -> tuple:
    """Simple convention: first token = first name, everything after = last name.
    ("Mary Jo Van Dyke" -> first "Mary", last "Jo Van Dyke"). Imperfect for
    multi-word first names, but predictable for CRM mapping."""
    parts = (full_name or "").strip().split()
    if not parts:
        return "", ""
    return parts[0], " ".join(parts[1:])


def clean_payload(data: dict) -> dict:
    cleaned = {k: str(data.get(k, "")).strip()[:1500] for k in LEAD_FIELDS}
    if not re.fullmatch(r"\d{5}(-\d{4})?", cleaned["dealer_zip"]):
        raise ValueError("A valid U.S. ZIP code is required.")
    if not cleaned["website"] or "." not in cleaned["website"]:
        raise ValueError("A dealership website is required.")
    if not cleaned["contact_name"]:
        raise ValueError("Your name is required.")
    if not EMAIL_RE.fullmatch(cleaned["contact_email"]):
        raise ValueError("A valid email address is required.")
    return cleaned


def generate_report(payload: dict) -> Any:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured.")
    user_prompt = (
        "\nBuild a weather-triggered Boat Dealer Demand & Geofencing Report from these inputs:\n"
        f"{json.dumps(payload, indent=2)}"
        "\n\nThe dealer did NOT provide a boating season, a media budget, weather preferences, or "
        "lists of local waterways and competitors. You must supply all of these yourself:\n"
        "- Assume the boating season and its length from the dealer's ZIP code and region.\n"
        "- Identify the local lakes, rivers, reservoirs, bays, public ramps, marinas, storage/service "
        "facilities, marine retailers, boat shows, and competing boat dealers yourself from geographic "
        "knowledge of the market, and include the best of them in geofence_locations.\n\n"
        "Populate every field of the schema:\n"
        "- market_summary: one or two sentences framing the weather-triggered boating demand opportunity "
        "for this dealer and market (reference the dealer name and area).\n"
        "- market_type: a short badge label for the market, e.g. 'Northern / Seasonal Inland Lake Market' "
        "or 'Coastal / Year-Round Saltwater Market'. market_type_description: one sentence on the seasonal pattern.\n"
        "- market_profile: low/base/high estimates for population, households, and likely boat-owner households, "
        "plus ownership rate (as a percentage decimal such as 7.5, not 0.075), confidence, and assumptions. "
        "CRITICAL: the population and household figures must cover the ENTIRE target_radius (see area-by-radius guidance in the "
        "system rules), not just the ZIP's town. A 50-75 mile radius typically covers hundreds of thousands to several million "
        "people — size it to the radius and region, and do not under-count.\n"
        "- market_opportunity: keep this SIMPLE — one short, plain sentence on the dealer's opportunity in this market.\n"
        "- recommended_package: choose the best-fit package for this market from the Smart 1 package menu below. "
        "Use its exact NAME and monthly price as monthly_investment (e.g. '$5,000/month'), and write a short description of what that level buys. "
        "Pick the tier based on market size, competition, and season length.\n"
        "  SMART 1 PACKAGE MENU (use these, do not invent prices):\n"
        "    * $2,500/month — Harbor Starter\n"
        "    * $5,000/month — SmartForecast Ads\n"
        "    * $7,500/month — Season Surge Plan\n"
        "    * $10,000/month — Full Fleet Dominance\n"
        "- pricing_scenarios: provide EXACTLY three scenarios labeled 'Good', 'Better', and 'Best', scaled to this market's "
        "population and size, using the exact package names and prices from the menu above. For a small or single-lake market use "
        "the lower tiers (e.g. Good = Harbor Starter, Better = SmartForecast Ads, Best = Season Surge Plan); for a large or "
        "competitive multi-lake/coastal market use the higher tiers (e.g. Good = SmartForecast Ads, Better = Season Surge Plan, "
        "Best = Full Fleet Dominance). Good is a lean entry budget, Better is the balanced recommended level (align it with "
        "recommended_package), and Best is an aggressive share-of-voice level. Each scenario needs tier, package_name (exact), "
        "monthly_investment (e.g. '$5,000/month'), and a one-line summary of what that level buys. Base the choice on population "
        "and market size, not guesswork.\n"
        "- savings_comparison: explain how this weather-triggered plan saves money versus traditional always-on or flat-schedule "
        "advertising (radio, print, broad cable, untargeted digital). Provide a short headline, a 1-2 sentence summary, and 3-4 "
        "concrete points (e.g. pausing spend on low-demand/bad-weather days, no wasted impressions outside boating conditions, "
        "unused slow-period budget rolling forward, precise in-market boat-buyer targeting vs broad reach, sales + service + "
        "storage from one budget). Frame savings as directional, not guaranteed; do not invent exact percentages as promises.\n"
        "- media_channels: ALLOWED channels/data only. ALWAYS include 'In-Market Boat Buyer Audience Data' as one of the "
        "chips (we layer third-party in-market boat-shopper data across the plan). Then choose from: geofencing "
        "marinas/ramps & state parks, location look-back retargeting, data-driven / programmatic targeted display, connected "
        "TV (CTV/OTT), streaming audio, YouTube/online video, website retargeting. Return 4-7 chips total. NEVER include paid "
        "search, email, SMS, or any social channel.\n"
        "- streaming_audio_note: 2-3 sentences explaining the streaming-audio play for THIS market. Explain that boaters stream "
        "music and podcasts on the dock, on the water, and on the drive to the ramp (screens-down moments other channels miss), "
        "that we geofence this market's marinas, boat ramps, launch points, and lakeshore so ads reach people already at or near "
        "the water, and that we run a sunrise-to-sunset daypart on boating-favorable days and weekends so spend follows real "
        "on-the-water activity. Reference the market's own waters where natural.\n"
        "- weather_triggers: 6-9 short trigger labels for this market. Include warm-season demand triggers (e.g. '70°+ weekend', "
        "'Sunny weekend', 'Holiday weekend forecast', 'First warm weekend') AND cold-weather triggers that drive winterization, "
        "storage, and service (ALWAYS include 'Frost warning', 'Freeze warning', and 'Ice storm' where seasonally relevant, plus "
        "'First frost').\n"
        "- monthly_plan: all 12 months (January through December). Each month has a focus title, a short customer-facing "
        "message, 1-2 relevant weather trigger labels drawn from weather_triggers, and a 'pacing' string. Match focus to the "
        "season (spring/summer = sales & boating demand, fall = end-of-season & winterization, winter = storage/service & "
        "early-order/boat-show).\n"
        "  BUDGET PACING RULE for the 'pacing' field: the recommended_package monthly_investment is the PEAK / in-season "
        "monthly budget (100%). In shoulder-season months spend 35% of the package; in off-season months spend 20% of the "
        "package. Classify each month as Peak, Shoulder, or Off-season based on this market's boating season, and set pacing "
        "to a short string with the tier, percent, and computed dollar amount — e.g. 'Peak — 100% ($5,000)', "
        "'Shoulder — 35% ($1,750)', 'Off-season — 20% ($1,000)'. Compute the dollars from the chosen package price.\n"
        "- geofence_locations: 12-18 locations (boating access, marinas, storage/service, competitors, marine retail, "
        "event venues). Prioritize locations inside the target radius; lower confidence for uncertain ones. Keep text concise.\n"
        "- disclaimer: a short note that figures are AI planning estimates for the market, not exact counts.\n"
    )
    # Called over HTTP rather than through the openai SDK. Every other module
    # in the Hub does it this way and the SDK isn't in requirements.txt —
    # adding a dependency for one module is the wrong trade when the request
    # is a plain POST.
    import requests as _rq
    _resp = _rq.post(
        "https://api.openai.com/v1/responses",
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
        json={
            "model": MODEL,
            "input": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "text": {"format": {"type": "json_schema", **REPORT_SCHEMA}},
            "temperature": 0.25,
            "max_output_tokens": 8000,
        },
        timeout=120,
    )
    _resp.raise_for_status()
    _data = _resp.json()

    # Record the spend so it appears in the Hub's cost tracking, per the
    # migration rule in CLAUDE.md — this module is being edited anyway.
    try:
        from hub import ai as _hub_ai
        _hub_ai.note_usage("boat", _data, purpose="market_report")
    except Exception:  # noqa: BLE001
        pass

    text = ""
    for item in (_data.get("output") or []):
        for part in (item.get("content") or []):
            if part.get("type") in ("output_text", "text"):
                text += part.get("text") or ""
    text = (text or _data.get("output_text") or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    return json.loads(text)


def send_webhook(payload: dict, report: Any, status: str, report_url: str = "", report_pdf_url: str = "") -> None:
    """Deliver a lead through the Hub's lead panel.

    This used to be a fire-and-forget POST that returned silently when
    WEBHOOK_URL was unset — so with a blank env var every lead was discarded
    while the visitor saw success. The panel stores the lead first and
    forwards second, so a bad URL or a Suite outage delays delivery instead of
    destroying it, and undelivered leads stay visible.
    """
    rep_ = report or {}
    pkg_ = rep_.get("recommended_package", {}) or {}
    try:
        from hub import leads as hub_leads
        hub_leads.capture_and_deliver(
            source="boat",
            page="Boat Dealer Weather Marketing",
            fields={
                "name": payload.get("contact_name", ""),
                "email": payload.get("contact_email", ""),
                "phone": payload.get("contact_phone", ""),
                "company": payload.get("dealer_name", ""),
                "market_type": rep_.get("market_type", ""),
                "recommended_package": pkg_.get("package_name", ""),
                "monthly_investment": pkg_.get("monthly_investment", ""),
                "report_url": report_url or "",
                "report_status": status,
            },
            pdf_url=report_pdf_url or "",
            client=payload.get("dealer_name", ""),
            meta={"market_summary": (rep_.get("market_summary") or "")[:600]})
        # Attribute the report to the client, so the work shows on
        # their 360 record rather than only in the lead panel.
        try:
            from hub import audit as _audit
            _audit.log("boat", "boat_report",
                       client=payload.get("dealer_name", "") or None)
        except Exception:  # noqa: BLE001 — logging never breaks a lead
            pass
        return
    except Exception:                                   # noqa: BLE001
        pass          # fall through to the original webhook

    if not WEBHOOK_URL:
        return
    rep = report or {}
    pkg = rep.get("recommended_package", {}) or {}
    first_name, last_name = split_name(payload.get("contact_name") or "")
    market_type = (rep.get("market_type") or "").strip()
    package_name = (pkg.get("package_name") or "").strip()
    body = {
        **payload,
        "contact_first_name": first_name,
        "contact_last_name": last_name,
        "source": SOURCE_NAME,
        "report_status": status,
        "opportunity_name": f"{payload.get('dealer_name', '').strip()} — Weather Marketing Proposal".strip(" —"),
        "market_type": market_type,
        # Ready-to-use CRM tags for auto-segmentation (map these to GHL "Add Tag" actions):
        "market_tag": (f"Boat - {market_type}" if market_type else "Boat - Lead"),
        "package_tag": (f"Boat - {package_name}" if package_name else ""),
        "estimated_boat_owner_households_base": rep.get("market_profile", {}).get(
            "estimated_boat_owner_households_base"
        ),
        "recommended_package": package_name,
        "recommended_monthly_investment": pkg.get("monthly_investment", ""),
        "market_summary": rep.get("market_summary", ""),
        "report_url": report_url or "",
        "report_pdf_url": report_pdf_url or "",
        "report_json": json.dumps(rep, separators=(",", ":"))[:60000],
    }
    try:
        requests.post(WEBHOOK_URL, json=body, timeout=12)
    except requests.RequestException:
        app.logger.exception("Webhook delivery failed")


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.get("/api/report/<rid>")
def get_report(rid: str):
    if not re.fullmatch(r"[a-f0-9]{12}", rid or ""):
        return jsonify({"ok": False, "error": "Not found"}), 404
    path = os.path.join(REPORTS_DIR, f"{rid}.json")
    data = jsonstore.read_json(path, default=None)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "This report link is no longer available."}), 404
    return jsonify({"ok": True, "report": data["report"], "dealer_name": data.get("dealer_name", "")})


@app.get("/pdf/<rid>")
def get_report_pdf(rid: str):
    """Local PDF fallback used when Cloudinary is not configured (ephemeral storage)."""
    if not re.fullmatch(r"[a-f0-9]{12}", rid or ""):
        return jsonify({"ok": False, "error": "Not found"}), 404
    path = os.path.join(REPORTS_DIR, f"{rid}.pdf")
    if not os.path.exists(path):
        return jsonify({"ok": False, "error": "This PDF is no longer available."}), 404
    return send_file(path, mimetype="application/pdf", as_attachment=False,
                     download_name="smart1-boat-report.pdf")


# Fields we salvage from a partial (abandoned) form, plus click-attribution keys.
PARTIAL_FIELDS = [
    "dealer_name", "website", "dealer_zip", "target_radius", "boat_types",
    "new_used", "campaign_objective",
    "contact_name", "contact_email", "contact_phone",
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "referrer_url", "landing_page_url",
]


# --- Abuse guard -----------------------------------------------------------
# These endpoints are public (the landing page has no Hub login) and /api/analyze
# spends OpenAI credit per call, so without a ceiling the bill is set by whoever
# finds the URL. Shared with the other landing pages via hub.leads so there is
# one implementation and one convention, and so the address is read from the
# hop Render vouches for rather than the caller-supplied one.
ANALYZE_LIMIT = int(os.environ.get("LANDING_ANALYZE_LIMIT", "6"))
PARTIAL_LIMIT = int(os.environ.get("LANDING_PARTIAL_LIMIT", "30"))


def _rate_limited(bucket: str, limit: int) -> bool:
    try:
        from hub import leads as _hub_leads
        return _hub_leads.rate_limited(bucket, request, limit)
    except Exception:          # noqa: BLE001 — a guard must never 500 the page
        return False


@app.post("/api/partial-lead")
def partial_lead():
    """Best-effort capture of partially completed forms (sent via beacon on page exit).

    Relaxed on purpose: no email requirement, no ZIP validation — salvage whatever
    arrived and forward it to the webhook with report_status "partial". Always
    responds {ok:true}; this endpoint must never surface errors to the page.
    """
    if _rate_limited("partial", PARTIAL_LIMIT):
        return jsonify({"ok": True})     # silent, like the honeypot below
    try:
        data = request.get_json(silent=True, force=True) or {}
        # Honeypot: bots fill this hidden field. Pretend success, send nothing.
        if (data.get("company_website") or "").strip():
            return jsonify({"ok": True})
        body = {"source": SOURCE_NAME, "report_status": "partial"}
        lead_id = str(data.get("lead_id") or "").strip()[:100]
        if lead_id:
            body["lead_id"] = lead_id
        for k in PARTIAL_FIELDS:
            v = str(data.get(k) or "").strip()[:1500]
            if v:
                body[k] = v
        if WEBHOOK_URL and len(body) > 2:  # only forward if something beyond source/status arrived
            requests.post(WEBHOOK_URL, json=body, timeout=8)
    except Exception:
        app.logger.exception("Partial lead forward failed")
    return jsonify({"ok": True})


@app.post("/api/analyze")
def analyze():
    if _rate_limited("analyze", ANALYZE_LIMIT):
        return jsonify({"ok": False,
                        "error": "Too many requests from this connection. "
                                 "Please wait a few minutes and try again."}), 429
    try:
        data = request.get_json(silent=True) or {}
        # Honeypot: real users never fill this hidden field; bots do. Block silently.
        if (data.get("company_website") or "").strip():
            return jsonify({"ok": False, "error": "Submission could not be processed."}), 400
        payload = clean_payload(data)
        report = generate_report(payload)
        rid = save_report(report, payload.get("dealer_name", ""))
        report_url = f"{base_url()}/?r={rid}"
        report_pdf_url = ""
        if FPDF_READY:
            try:
                pdf_path = build_pdf(report, payload.get("dealer_name", ""), payload)
                report_pdf_url = upload_report_pdf(pdf_path, payload.get("dealer_name", ""), rid)
                if report_pdf_url:
                    # Uploaded to Cloudinary — the temp file is no longer needed.
                    try:
                        os.remove(pdf_path)
                    except OSError:
                        pass
                else:
                    # Cloudinary unconfigured/failed: keep the PDF locally and serve it
                    # from this app (ephemeral storage — cleared on redeploy).
                    os.replace(pdf_path, os.path.join(REPORTS_DIR, f"{rid}.pdf"))
                    report_pdf_url = f"{base_url()}/pdf/{rid}"
            except Exception:
                app.logger.exception("PDF generation/upload failed")
        send_webhook(payload, report, "completed", report_url, report_pdf_url)
        return jsonify({"ok": True, "report": report, "report_url": report_url, "report_pdf_url": report_pdf_url})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        app.logger.exception("Analysis failed")
        try:
            # Salvage whatever raw fields arrived, WITHOUT re-validating: clean_payload
            # would re-raise on the same bad input and the failure webhook would never send.
            raw = request.get_json(silent=True) or {}
            salvaged = {k: str(raw.get(k, "")).strip()[:1500] for k in LEAD_FIELDS}
            send_webhook(salvaged, None, "failed")
        except Exception:
            pass
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "Sorry — we couldn't generate your report just now. Please try again in a moment.",
                }
            ),
            500,
        )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
