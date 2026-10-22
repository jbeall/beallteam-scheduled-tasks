"""Phone number helpers. Dialpad sends E.164 (+1XXXXXXXXXX); shortcodes are bare digits."""

from __future__ import annotations

import re

_NON_DIGITS = re.compile(r"\D")
_PHONE_LIKE = re.compile(r"[\d\s()+\-.]+")


def digits(value: str | None) -> str:
    return _NON_DIGITS.sub("", value or "")


def normalize(value: str | None) -> str | None:
    """Return +1XXXXXXXXXX for NANP numbers, +<digits> for other international numbers,
    bare digits for shortcodes, or None when there is nothing to normalize."""
    raw = (value or "").strip()
    d = digits(raw)
    if not d:
        return None
    if len(d) == 10:
        return f"+1{d}"
    if len(d) == 11 and d.startswith("1"):
        return f"+{d}"
    if raw.startswith("+"):
        return f"+{d}"
    return d


def is_shortcode(value: str | None) -> bool:
    normalized = normalize(value)
    return normalized is not None and not normalized.startswith("+")


def pretty(value: str | None) -> str:
    normalized = normalize(value)
    if normalized and normalized.startswith("+1") and len(normalized) == 12:
        d = normalized[2:]
        return f"({d[:3]}) {d[3:6]}-{d[6:]}"
    return normalized or ""


def looks_like_phone(name: str | None) -> bool:
    """True for Dialpad display names that are just a number, e.g. '(555) 010-0199'."""
    return bool(name) and bool(_PHONE_LIKE.fullmatch(name.strip()))
