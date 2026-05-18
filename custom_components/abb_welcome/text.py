"""Text helpers for ABB Welcome payloads."""

from __future__ import annotations

_MOJIBAKE_MARKERS = ("Ã", "Â", "â")
GATEWAY_TEXT_ENCODINGS = ("utf-8", "utf-8-sig", "cp1252", "latin-1")

_MOJIBAKE_REPLACEMENTS = {
    "â€˜": "‘",
    "â€™": "’",
    "â€œ": "“",
    "â€": "”",
    "â€“": "–",
    "â€”": "—",
    "â€¦": "…",
    "â€¢": "•",
    "Â ": " ",
    "Â ": " ",
}


def decode_gateway_text(raw: bytes) -> str:
    """Decode ABB gateway text payloads from bytes, preferring UTF-8.

    The gateway CGI endpoints often omit or misreport ``charset``.  Do not rely
    on ``requests.Response.text`` for these payloads; decode the bytes ourselves
    so UTF-8 labels such as ``Haustür`` are not interpreted as Latin-1/CP1252.
    """
    for encoding in GATEWAY_TEXT_ENCODINGS:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def repair_utf8_mojibake(value: str) -> str:
    """Repair common UTF-8-as-Latin-1/CP1252 mojibake in gateway labels.

    ABB gateway and portal payloads sometimes carry labels that have already
    been decoded once with the wrong single-byte encoding, for example
    ``Haustür`` arrives as ``HaustÃ¼r``.  Leave normal strings untouched and only
    try the reversible repair when typical mojibake marker characters are
    present.
    """
    if not value or not any(marker in value for marker in _MOJIBAKE_MARKERS):
        return value

    marker_count = sum(value.count(marker) for marker in _MOJIBAKE_MARKERS)
    for encoding in ("latin-1", "cp1252"):
        try:
            repaired = value.encode(encoding).decode("utf-8")
        except UnicodeError:
            continue
        repaired_marker_count = sum(
            repaired.count(marker) for marker in _MOJIBAKE_MARKERS
        )
        if repaired != value and repaired_marker_count < marker_count:
            return repaired

    repaired = value
    for bad, good in _MOJIBAKE_REPLACEMENTS.items():
        repaired = repaired.replace(bad, good)
    if repaired != value:
        return repaired

    return value
