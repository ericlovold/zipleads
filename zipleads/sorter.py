"""Sort a building permit into one of four kinds.

    new_occupant      a business moving in or a new space being created: the lead
    tenant_refresh    the existing occupant remodeling its own space
    building_systems  trade or envelope work: HVAC, plumbing, roof, windows, antennas
    residential       a home, apartment or condo, whatever the permit type says

Rules are checked in that order of precedence: residential first (a home is never a
lead), then new-occupant clues, then building systems, then the default. Every verdict
carries the clues that decided it, so a person can audit any call.

The rules came from real Minneapolis permits and the rep's feedback; see
tests/fixtures/permit_cases.json and docs/playbook.md. They are about permit language,
not about any one industry, so every profile shares them. Profiles choose which kinds
to drop and which to hide.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

NEW_OCCUPANT = "new_occupant"
TENANT_REFRESH = "tenant_refresh"
BUILDING_SYSTEMS = "building_systems"
RESIDENTIAL = "residential"
KINDS = (NEW_OCCUPANT, TENANT_REFRESH, BUILDING_SYSTEMS, RESIDENTIAL)


@dataclass(frozen=True)
class Verdict:
    kind: str
    confidence: str  # "high" or "low"
    reasons: tuple[str, ...] = field(default_factory=tuple)


def _tokens(*texts: str) -> set[str]:
    return {t for s in texts if s for t in re.split(r"[^a-z0-9]+", s.lower()) if t}


# --- residential -------------------------------------------------------------

# Coded values on permit type, work type or occupancy that mean housing.
# Minneapolis: SFD single family, TFD two family, 3to4, MFD multi family, Res, ExistRes.
_RESIDENTIAL_CODES = {"sfd", "tfd", "3to4", "mfd", "res", "existres", "residential", "dwelling"}
# IBC occupancy groups R-1..R-4 in the free text (R-2 apartments, R-3 houses).
_R_OCCUPANCY = re.compile(r"\boccupancy:?\s*(?:group\s*)?r-?[1-4]\b", re.I)
_RESIDENTIAL_TEXT = re.compile(
    r"\b(single[- ]family|two[- ]family|duplex|townhome|townhouse|condo(minium)?s?|"
    r"dwelling units?|residential|apartment units?|basement (bathroom|finish))\b",
    re.I,
)
# Owners or applicants that are housing entities or private persons' trusts.
_RESIDENTIAL_OWNER = re.compile(
    r"\b(apartments?|condo(minium)?s?|homeowners?|hoa|townhomes?|living trust|rev liv trust|"
    r"revocable trust)\b",
    re.I,
)
# "REMODEL UNIT 104": a numbered unit remodel with no business named is usually a home.
_UNIT_REMODEL = re.compile(r"\b(remodel|renovat\w*|update)\s+(of\s+)?unit\s*#?\s*\d+\b", re.I)

# --- new occupant ------------------------------------------------------------

_TI = re.compile(
    r"\b(tenant\s+(improvement|finish|build[- ]?out|fit[- ]?out|remodel)|"
    r"build[- ]?out|fit[- ]?out|white\s*box|vanilla\s*box)\b",
    re.I,
)
# A space being readied for someone not there yet: the site-visit cases.
_FUTURE_TENANT = re.compile(
    r"\b(future\s+tenants?|prospective\s+tenants?|spec(ulative)?\s+suites?|for\s+lease|"
    r"vacant\s+(suite|space|unit))\b",
    re.I,
)
_RELOCATION = re.compile(r"\b(relocat\w+|moving\s+(to|into))\b", re.I)
_EXISTING_TENANT = re.compile(r"\bexisting\s+(tenant|occupant|business)\b", re.I)
_NEW_THING = re.compile(
    r"\bnew\s+(?:[a-z&'-]+\s+){0,3}?"
    r"(store|shop|restaurant|cafe|coffee|bar|brewery|taproom|bakery|"
    r"facility|office|offices|clinic|salon|studio|gym|fitness|spa|venue|center|centre|"
    r"tenant|business|location|showroom|warehouse|daycare|school|suite|space)\b",
    re.I,
)
_NEW_BUILDING = re.compile(
    r"\b(new\s+(building|construction)|building\s+shell|core\s+and\s+shell)\b", re.I
)
_CO_YES = re.compile(r"certificate\s+of\s+occupancy\s+required:?\s*yes\b", re.I)
_CHANGE_OF_USE = re.compile(r"\bchange\s+of\s+(use|occupancy)\b", re.I)
_NAMED_TENANT = re.compile(r"\btenant\s*:\s*[A-Za-z0-9]", re.I)
_NEW_WORK_TYPES = {"newbldg", "newbuilding", "newconstruction", "new"}

# --- building systems ----------------------------------------------------------

_TRADE_PERMIT_TYPES = {
    "plumbing",
    "mechanical",
    "electrical",
    "fire",
    "sprinkler",
    "gas",
    "sign",
    "signs",
    "elevator",
    "wrecking",
    "demolition",
    "demo",
    "roofing",
    "plbg",
    "mech",
    "elec",
}
_SYSTEMS_WORK_TYPES = {
    "roofwind",
    "reroof",
    "roof",
    "window",
    "windows",
    "siding",
    "solar",
    "sign",
    "demo",
    "wreck",
    "fire",
    "comfdnr",
    "commfdnr",
}
_SYSTEMS_TEXT = re.compile(
    r"\b(hvac|duct\s*work|ductwork|boiler|furnace|air[- ]handl\w*|rooftop\s+unit|rtu|"
    r"roof(ing)?|re-?roof|windows?|antennas?|telecommunications?\s+equipment|cell\s+site|"
    r"generator|sprinklers?|fire\s+alarm|elevator|water\s+heater|repairs?|replac\w*|"
    r"damage|restoration|solar|siding|signage|demolition|parking\s+lot|fence|retaining\s+wall)\b",
    re.I,
)


def sort_permit(
    *,
    permit_type: str = "",
    work_type: str = "",
    occupancy: str = "",
    description: str = "",
    applicant: str = "",
) -> Verdict:
    codes = _tokens(permit_type, work_type, occupancy)
    text = description or ""

    # 1. Residential: a home is never a lead, whatever else the permit says.
    hit = codes & _RESIDENTIAL_CODES
    if hit:
        return Verdict(RESIDENTIAL, "high", (f"housing code: {', '.join(sorted(hit))}",))
    if m := _R_OCCUPANCY.search(text):
        return Verdict(RESIDENTIAL, "high", (f"residential occupancy: {m.group(0).strip()}",))
    if m := _RESIDENTIAL_TEXT.search(text):
        return Verdict(RESIDENTIAL, "high", (f"says '{m.group(0)}'",))
    if m := _RESIDENTIAL_OWNER.search(applicant or ""):
        return Verdict(RESIDENTIAL, "low", (f"owner looks residential: '{m.group(0)}'",))

    # 2. New occupant: someone is moving in, or a space is being created.
    new_reasons: list[str] = []
    if _CO_YES.search(text):
        new_reasons.append("new certificate of occupancy required")
    if _CHANGE_OF_USE.search(text):
        new_reasons.append("change of use")
    if _NAMED_TENANT.search(text):
        new_reasons.append("tenant named on permit")
    if m := _NEW_THING.search(text):
        new_reasons.append(f"says '{m.group(0).strip()}'")
    if m := _NEW_BUILDING.search(text):
        new_reasons.append(f"says '{m.group(0)}'")
    if m := _FUTURE_TENANT.search(text):
        new_reasons.append(f"says '{m.group(0)}'")
    if m := _RELOCATION.search(text):
        new_reasons.append(f"says '{m.group(0)}'")
    if codes & _NEW_WORK_TYPES or "new construction" in (work_type or "").lower():
        new_reasons.append(f"work type '{work_type}'")
    ti = _TI.search(text)
    if ti:
        if _EXISTING_TENANT.search(text) and not new_reasons:
            return Verdict(TENANT_REFRESH, "high", (f"'{ti.group(0)}' for the existing tenant",))
        new_reasons.append(f"says '{ti.group(0)}'")
    if new_reasons:
        return Verdict(NEW_OCCUPANT, "high", tuple(new_reasons))

    # A numbered-unit remodel with nothing commercial about it: usually an apartment.
    if m := _UNIT_REMODEL.search(text):
        return Verdict(RESIDENTIAL, "low", (f"'{m.group(0)}' with no business named",))

    # 3. Building systems: trade or envelope work on an occupied building.
    trade = codes & _TRADE_PERMIT_TYPES
    work = codes & _SYSTEMS_WORK_TYPES
    m = _SYSTEMS_TEXT.search(text)
    if trade or work or m:
        reasons = []
        if trade:
            reasons.append(f"trade permit: {', '.join(sorted(trade))}")
        if work:
            reasons.append(f"work type: {', '.join(sorted(work))}")
        if m:
            reasons.append(f"says '{m.group(0)}'")
        return Verdict(BUILDING_SYSTEMS, "high", tuple(reasons))

    # 4. Default: commercial work with no sign of a new occupant.
    return Verdict(TENANT_REFRESH, "low", ("commercial remodel, no new-occupant clue",))
