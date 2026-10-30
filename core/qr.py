"""Helpers for turning scanned QR / barcode text into a lookup code."""

import re
from urllib.parse import parse_qs, unquote, urlparse

QUERY_KEYS = ("code", "c", "id", "qr", "batch", "v")


def extract_code(raw):
    """
    Return the code to look up from whatever a scanner produced.

    Accepts a plain code ("PSQR-482913"), a URL that carries the code
    ("https://x.in/v?code=PSQR-482913" or ".../verify/PSQR-482913"), or text
    with stray whitespace / control characters.
    """
    text = re.sub(r"[\x00-\x1f\x7f]", "", str(raw or "")).strip()

    if not text:
        return ""

    if re.match(r"^https?://", text, re.I):
        parsed = urlparse(text)
        query = parse_qs(parsed.query)

        for key in QUERY_KEYS:
            if query.get(key):
                return query[key][0].strip()

        segments = [unquote(p) for p in parsed.path.split("/") if p]

        if segments:
            return segments[-1].strip()

    return text[:200]
