"""Pure helpers for the admin dashboard and QR page (no Django / MongoDB)."""

from datetime import timedelta

RESULT_LABELS = {
    "VERIFIED_RECORD": ("Verified", "#16a34a"),
    "EXPIRED": ("Expired", "#dc2626"),
    "REPORTED": ("Flagged by users", "#ea580c"),
    "NO_RECORD": ("No record", "#64748b"),
}


def fill_days(counts, days, today):
    """
    counts: {"YYYY-MM-DD": n}. Returns one entry per day for the last ``days``
    days (oldest first), with zeros for days that have no scans, and a ``pct``
    (0-100) for drawing a bar chart.
    """
    rows = []

    for offset in range(days - 1, -1, -1):
        day = today - timedelta(days=offset)
        rows.append(
            {
                "date": day.isoformat(),
                "label": f"{day.day} {day:%b}",
                "n": int(counts.get(day.isoformat(), 0)),
            }
        )

    peak = max((r["n"] for r in rows), default=0)

    for r in rows:
        r["pct"] = 0 if peak == 0 else max(4, round(r["n"] * 100 / peak)) if r["n"] else 0

    return rows


def result_breakdown(counts):
    """{"EXPIRED": 3, ...} -> list of rows with label, colour, n and pct."""
    total = sum(counts.values())
    rows = []

    for key in ("VERIFIED_RECORD", "EXPIRED", "REPORTED", "NO_RECORD"):
        n = int(counts.get(key, 0))
        label, colour = RESULT_LABELS[key]
        rows.append(
            {
                "key": key,
                "label": label,
                "colour": colour,
                "n": n,
                "pct": round(n * 100 / total) if total else 0,
            }
        )

    for key, n in counts.items():
        if key not in RESULT_LABELS:
            rows.append(
                {"key": key, "label": str(key).title(), "colour": "#94a3b8",
                 "n": int(n), "pct": round(n * 100 / total) if total else 0}
            )

    return rows


def qr_payload(medicine):
    """The text stored in a batch's QR code (what the Verify page looks up)."""
    for field in ("qr_code", "verification_code", "batch_number"):
        value = str(medicine.get(field) or "").strip()

        if value:
            return value

    return ""


def with_string_id(doc):
    """
    Copy of a MongoDB document with its ``_id`` also available as ``id`` (string).
    Django templates cannot read names that start with an underscore (``doc._id``).
    """
    doc = dict(doc)

    if "_id" in doc:
        doc["id"] = str(doc["_id"])

    return doc

def mask_email(email):
    """raunak.singh@gmail.com -> r***h@gmail.com (shown on the OTP screen)."""
    name, _, domain = str(email).partition("@")

    if not domain:
        return "***"

    if len(name) <= 2:
        return f"{name[:1]}***@{domain}"

    return f"{name[0]}***{name[-1]}@{domain}"