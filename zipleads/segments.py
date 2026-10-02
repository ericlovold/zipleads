"""Segment scrub: drop or flag leads by industry keywords.

Comcast's referral program cannot pay on education, discounts dental (Delta
national agreement), and has extra rules for hospitality and some franchises.
There is no clean data source for any of that, so this is a keyword pass over
the company name, description, and Places primary type. It will miss some
and mis-flag others; flagged leads stay in the export so a human can decide.
"""

from __future__ import annotations

import re
from functools import lru_cache

from zipleads.config import Profile
from zipleads.models import Lead
from zipleads.score import FLAG_BOOST, FLAG_DEPRIORITIZED


def _hay(lead: Lead) -> str:
    return " ".join(
        x for x in (lead.company_name, lead.description, str(lead.raw.get("primaryType", ""))) if x
    ).lower()


# "University Ave" and "College St" are places, not schools.
_STREET = (
    r"ave|avenue|st|street|blvd|boulevard|rd|road|dr|drive|pkwy|parkway|ln|lane|way|ct|"
    r"court|pl|place|hwy|highway|cir|circle|ter|terrace|trl|trail"
)


@lru_cache(maxsize=512)
def _term_pattern(term: str) -> re.Pattern:
    # Starts on a word boundary, so "office" does not match inside "postoffice", and
    # stays open at the end so "manufactur" still matches "manufacturing".
    # "St" is also "Saint": "Academy St. Paul" is a school, "College St" is a street.
    street = rf"\w*\.?\s+(?:{_STREET})\b(?!\.?\s*(?:paul|cloud|louis|anthony|michael)\b)"
    return re.compile(rf"\b{re.escape(term)}(?!{street})")


def _hit(hay: str, terms: tuple[str, ...]) -> str:
    """The most specific (longest) matching term, or empty string."""
    matches = [t for t in terms if _term_pattern(t).search(hay)]
    return max(matches, key=len) if matches else ""


def excluded_by(profile: Profile, lead: Lead) -> str:
    """The exclude term that matched, or empty string."""
    return _hit(_hay(lead), profile.segment_exclude)


def flags_for(profile: Profile, lead: Lead) -> list[str]:
    """Signal tags to attach: flag:deprioritized:<term> and/or flag:boost:<term>."""
    hay = _hay(lead)
    flags = []
    if term := _hit(hay, profile.segment_deprioritize):
        flags.append(f"{FLAG_DEPRIORITIZED}:{term}")
    if term := _hit(hay, profile.segment_boost):
        flags.append(f"{FLAG_BOOST}:{term}")
    return flags
