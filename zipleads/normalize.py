"""Name and address normalization so the same business dedupes across sources."""

from __future__ import annotations

import re

_SUFFIXES = {
    "llc",
    "l.l.c",
    "inc",
    "incorporated",
    "corp",
    "corporation",
    "co",
    "company",
    "ltd",
    "limited",
    "pllc",
    "pc",
    "pa",
    "lp",
    "llp",
    "dba",
}
_LEADING = {"the"}

_ADDRESS_ABBR = {
    "street": "st",
    "avenue": "ave",
    "boulevard": "blvd",
    "drive": "dr",
    "road": "rd",
    "lane": "ln",
    "court": "ct",
    "place": "pl",
    "parkway": "pkwy",
    "highway": "hwy",
    "north": "n",
    "south": "s",
    "east": "e",
    "west": "w",
    "northeast": "ne",
    "northwest": "nw",
    "southeast": "se",
    "southwest": "sw",
    "suite": "ste",
    "building": "bldg",
    "floor": "fl",
}

_ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")
_NON_ALNUM = re.compile(r"[^a-z0-9 ]+")
_SPACES = re.compile(r"\s+")


def normalize_name(name: str) -> str:
    """Lowercase, drop punctuation and entity suffixes, collapse whitespace."""
    s = _NON_ALNUM.sub(" ", name.lower().replace("&", " and "))
    tokens = [t for t in _SPACES.split(s) if t]
    while tokens and tokens[0] in _LEADING:
        tokens.pop(0)
    while tokens and tokens[-1] in _SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def normalize_address(address: str) -> str:
    """Lowercase, strip punctuation, standardize common street abbreviations."""
    s = _NON_ALNUM.sub(" ", address.lower())
    tokens = [_ADDRESS_ABBR.get(t, t) for t in _SPACES.split(s) if t]
    return " ".join(tokens)


def extract_zip(text: str) -> str:
    """The zip in an address string, or empty string.

    Takes the last 5-digit run so "11361 Fountains Dr, Maple Grove, MN 55369"
    yields 55369, not the street number.
    """
    matches = _ZIP_RE.findall(text or "")
    return matches[-1] if matches else ""


def dedupe_key(company_name: str, zip_code: str, city: str = "", address: str = "") -> str:
    """Stable key for the same business in the same place.

    Place is the city when known, else the zip. City wins because news has no
    zip and permits often have no zip until geocoded, and the same business
    seen by two sources must land on one key. Falls back to address + place
    when the business name is unknown (a permit filed by a contractor).
    """
    name = normalize_name(company_name)
    place = normalize_address(city) or zip_code.strip()
    if name and place:
        return f"{name}|{place}"
    if name:
        return f"{name}|"
    return f"@{normalize_address(address)}|{place}"
