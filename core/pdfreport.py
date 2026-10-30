"""PDF medication-safety report (uses reportlab)."""

import io
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

IST = timezone(timedelta(hours=5, minutes=30))
TEAL = colors.HexColor("#0f766e")
STATUS = {
    "DANGER": ("ACTION NEEDED - serious safety issues were found", "#b91c1c", "#fee2e2"),
    "ATTENTION_REQUIRED": ("ATTENTION - possible safety issues were found", "#b45309", "#fef3c7"),
    "NO_KNOWN_ISSUES": ("No known issues found in the current medicine list", "#15803d", "#dcfce7"),
}


def _t(value):
    """Escape for reportlab Paragraph and keep to characters the base font has."""
    text = "" if value is None else str(value)
    text = text.encode("latin-1", "replace").decode("latin-1")
    return escape(text)


def _title(value):
    return _t(str(value).title())


def build_safety_pdf(user, safety, generated_at=None):
    """Return the PDF bytes for ``safety`` (result of db.medication_safety_check)."""
    generated_at = generated_at or datetime.now(IST)
    styles = getSampleStyleSheet()

    h1 = ParagraphStyle("h1", parent=styles["Title"], textColor=TEAL, fontSize=20,
                        alignment=0, spaceAfter=2)
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], textColor=TEAL, fontSize=12.5,
                        spaceBefore=12, spaceAfter=4)
    body = ParagraphStyle("body", parent=styles["BodyText"], fontSize=9.5, leading=12.5)
    small = ParagraphStyle("small", parent=body, fontSize=8, leading=10.5,
                           textColor=colors.HexColor("#475569"))
    cell = ParagraphStyle("cell", parent=body, fontSize=8.5, leading=10.5)
    head = ParagraphStyle("head", parent=cell, textColor=colors.white, fontName="Helvetica-Bold")

    story = [
        Paragraph("PharmaShield - Medication Safety Report", h1),
        Paragraph(
            f"Prepared for <b>{_t(user.get('name', ''))}</b> "
            f"(ID {_t(user.get('user_id', ''))}) &nbsp;|&nbsp; "
            f"Generated {generated_at:%d %b %Y, %I:%M %p} IST",
            small,
        ),
        Spacer(1, 8),
    ]

    text, fg, bg = STATUS.get(safety["status"], STATUS["NO_KNOWN_ISSUES"])
    box = Table(
        [[Paragraph(f'<font color="{fg}"><b>{_t(text)}</b></font>', body)]],
        colWidths=[174 * mm],
    )
    box.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(bg)),
        ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor(fg)),
        ("LEFTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    story.append(box)

    summary = (
        f"Medicines: <b>{len(safety['medicines'])}</b> &nbsp;|&nbsp; "
        f"Allergy alerts: <b>{safety['allergy_count']}</b> &nbsp;|&nbsp; "
        f"Dose alerts: <b>{safety['dose_count']}</b> &nbsp;|&nbsp; "
        f"Duplicate ingredients: <b>{safety['duplicate_count']}</b> &nbsp;|&nbsp; "
        f"Interactions: <b>{safety['interaction_count']}</b>"
    )
    story += [Spacer(1, 6), Paragraph(summary, body)]

    def table(rows, widths, header):
        data = [[Paragraph(_t(h), head) for h in header]] + rows
        t = Table(data, colWidths=[w * mm for w in widths], repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), TEAL),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f1f5f9")]),
            ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#cbd5e1")),
            ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        return t

    def section(title, content):
        story.append(KeepTogether([Paragraph(title, h2), content]))

    # medicines
    if safety["medicines"]:
        rows = []
        for m in safety["medicines"]:
            ingredients = m.get("active_ingredients") or []
            if isinstance(ingredients, str):
                ingredients = [ingredients]
            rows.append([
                Paragraph(_t(m.get("medicine_name") or m.get("brand_name")), cell),
                Paragraph(_t(", ".join(ingredients) or "-"), cell),
                Paragraph(_t(m.get("dose") or "-"), cell),
                Paragraph(_t(m.get("frequency") or "-"), cell),
            ])
        section("Current medicines", table(rows, [48, 72, 26, 28], ["Medicine", "Active ingredients", "Dose", "Frequency"]))
    else:
        section("Current medicines", Paragraph("No medicines have been added yet.", body))

    # allergies
    if safety["allergy_alerts"]:
        rows = [[Paragraph(_t(a["allergy"]), cell), Paragraph(_title(a["ingredient"]), cell),
                 Paragraph(_t(", ".join(a["medicines"])), cell)] for a in safety["allergy_alerts"]]
        section("Allergy alerts", table(rows, [40, 40, 94], ["Your allergy", "Matching ingredient", "Found in"]))
    else:
        section("Allergy alerts", Paragraph("No medicine matches the saved allergies.", body))

    # dose
    if safety["dose_alerts"]:
        rows = [[Paragraph(_title(d["ingredient"]), cell), Paragraph(f"{d['daily_mg']} mg", cell),
                 Paragraph(f"{d['limit_mg']} mg", cell), Paragraph(_t(", ".join(d["medicines"])), cell)]
                for d in safety["dose_alerts"]]
        section("Daily dose alerts", table(rows, [38, 30, 34, 72], ["Ingredient", "Estimated per day", "Typical adult max", "From"]))
    else:
        section("Daily dose check", Paragraph(
            "No daily limit exceeded, based on the dose and frequency entered "
            "(one unit per intake is assumed if no dose was entered).", body))

    # duplicates
    if safety["duplicate_ingredients"]:
        rows = [[Paragraph(_title(d["ingredient"]), cell), Paragraph(_t(", ".join(d["medicines"])), cell)]
                for d in safety["duplicate_ingredients"]]
        section("Duplicate active ingredients", table(rows, [50, 124], ["Ingredient", "Found in"]))
    else:
        section("Duplicate active ingredients", Paragraph("None detected.", body))

    # interactions
    if safety["interactions"]:
        rows = []
        for i in safety["interactions"]:
            colour = "#b91c1c" if i["severity"] == "major" else "#b45309"
            rows.append([
                Paragraph(f'<font color="{colour}"><b>{_t(i["severity"].title() or "-")}</b></font>', cell),
                Paragraph(f"{_title(i['drug_a'])} + {_title(i['drug_b'])}", cell),
                Paragraph(_t(i["description"]), cell),
                Paragraph(_t(", ".join(i.get("medicines", []))), cell),
            ])
        section("Interactions", table(rows, [20, 38, 78, 38], ["Severity", "Ingredients", "What it means", "Your medicines"]))
    else:
        section("Interactions", Paragraph("No known interactions detected.", body))

    if safety.get("unrecognised"):
        story.append(Paragraph(
            "Not found in the database (could not be checked): "
            f"<b>{_t(', '.join(safety['unrecognised']))}</b>", small))

    story += [
        Spacer(1, 14),
        Paragraph(
            "<b>Important:</b> PharmaShield is a decision-support tool, not medical advice. "
            "The checks cover a limited set of well-known risks and cannot replace a doctor "
            "or pharmacist. Do not start, stop or change a medicine because of this report "
            "alone - show it to your doctor or pharmacist.", small),
    ]

    buffer = io.BytesIO()

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.HexColor("#64748b"))
        canvas.drawString(18 * mm, 10 * mm, "PharmaShield Medication Safety Report")
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"Page {doc.page}")
        canvas.restoreState()

    SimpleDocTemplate(
        buffer, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=16 * mm, bottomMargin=18 * mm, title="PharmaShield Medication Safety Report",
        author="PharmaShield",
    ).build(story, onFirstPage=footer, onLaterPages=footer)

    return buffer.getvalue()
