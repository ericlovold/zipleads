"""Building permits from an ArcGIS FeatureServer layer.

Each territory lists its own permit layers with their field names. The
profile decides which permits count (include/exclude terms on permit type,
work type, and description).

A permit applicant is often the contractor, not the tenant. The lead still
carries the address and the work description, which is enough to look the
tenant up during enrichment.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from zipleads.config import PermitLayer, Profile
from zipleads.http import Http
from zipleads.models import Lead
from zipleads.normalize import extract_zip

PAGE_SIZE = 1000

# IBC occupancy groups R-1..R-4 are residential (hotels are R-1, apartments R-2).
_RESIDENTIAL_OCCUPANCY = re.compile(r"\boccupancy:?\s*(?:group\s*)?r-?[1-4]\b", re.IGNORECASE)

# "tenant improvement for Northstar Dental, suite 300" -> "Northstar Dental".
# Stops at punctuation or a location word. Case-insensitive because permit
# comments arrive in every case imaginable.
_NAME = (
    r"(?!(?:a|an|new|existing|future|this|the|our|their)\b)"
    r"(?P<name>[A-Za-z0-9&'.][A-Za-z0-9&'.\- ]{2,60}?)"
    r"(?=\s*(?:[,.;:|()\n]|\s+-\s+|\s+(?:at|in|on|located|suite|ste|floor|fl|unit)\b|$))"
)
_TENANT_PATTERNS = (
    re.compile(r"\btenant\s*[:\-]\s*(?:the\s+)?" + _NAME, re.IGNORECASE),
    re.compile(r"\bfor\s+(?:the\s+)?" + _NAME, re.IGNORECASE),
)
_NUMBER_WORDS = {"one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"}
_TRADE_WORDS = {
    "electrical",
    "plumbing",
    "mechanical",
    "hvac",
    "heating",
    "cooling",
    "sprinkler",
    "fire",
    "alarm",
    "ada",
    "restroom",
    "restrooms",
    "bathroom",
    "bathrooms",
    "kitchen",
    "unit",
    "units",
    "floor",
    "floors",
    "suite",
    "suites",
    "roof",
    "roofing",
    "deck",
    "signage",
    "sign",
    "signs",
    "parking",
    "elevator",
    "permit",
    "permits",
    "inspection",
}
_GENERIC = {
    *_NUMBER_WORDS,
    *_TRADE_WORDS,
    "tenant",
    "office",
    "space",
    "building",
    "commercial",
    "restaurant",
    "retail",
    "warehouse",
    "remodel",
    "renovation",
    "improvement",
    "improvements",
    "occupancy",
}


def extract_tenant(text: str) -> str:
    """Business named in a permit description, or empty string.

    In mixed-case text a real name starts with a capital, which rejects
    "for two (2) ADA restrooms". In all-caps or all-lowercase text that test
    is meaningless, so only the generic-word filter applies.
    """
    text = text or ""
    mixed_case = text != text.upper() and text != text.lower()
    for pattern in _TENANT_PATTERNS:
        for m in pattern.finditer(text):
            name = m.group("name").strip(" -.")
            words = name.lower().split()
            if not words or len(words) > 8:
                continue
            if any(w in _GENERIC for w in words[:1]) or all(w in _GENERIC for w in words):
                continue
            if mixed_case and not name[0].isupper():
                continue
            return name
    return ""


def layer_fields(http: Http, layer: PermitLayer) -> list[dict]:
    """Field list from the layer's metadata, for the `probe` command."""
    meta = http.get_json(layer.url, params={"f": "pjson"})
    return meta.get("fields", [])


def sample_features(http: Http, layer: PermitLayer, count: int) -> list[dict]:
    """Most recent raw records, for the `probe --sample` command."""
    page = http.get_json(
        f"{layer.url}/query",
        params={
            "where": "1=1",
            "outFields": "*",
            "orderByFields": f"{layer.fields.date} DESC",
            "resultRecordCount": count,
            "f": "json",
        },
    )
    return [f.get("attributes", {}) for f in page.get("features", [])]


def _epoch_ms_to_iso(value) -> str:
    if value in (None, ""):
        return ""
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC).date().isoformat()
    except (TypeError, ValueError, OSError):
        return str(value)


def _number(value) -> float:
    try:
        return float(value) if value not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0


def _tokens(*texts: str) -> set[str]:
    return {tok for t in texts if t for tok in re.split(r"[^a-z0-9]+", t.lower()) if tok}


def wanted(profile: Profile, *texts: str, type_fields: tuple[str, ...] = ()) -> bool:
    """Profile filter. `type_fields` are coded fields (permit type, occupancy) matched
    as whole tokens against exclude_types, so "Res" is caught but "restaurant" is not."""
    if profile.permit_exclude_types and _tokens(*type_fields) & set(profile.permit_exclude_types):
        return False
    hay = " ".join(t.lower() for t in texts if t)
    if _RESIDENTIAL_OCCUPANCY.search(hay):
        return False
    if profile.permit_include_terms and not any(t in hay for t in profile.permit_include_terms):
        return False
    return not any(t in hay for t in profile.permit_exclude_terms)


def parse_features(features: list[dict], layer: PermitLayer, profile: Profile) -> list[Lead]:
    f = layer.fields
    leads: list[Lead] = []
    for feat in features:
        a = feat.get("attributes", {})
        permit_type = str(a.get(f.permit_type) or "")
        work_type = str(a.get(f.work_type) or "")
        description = str(a.get(f.description) or "")
        occupancy = str(a.get(f.occupancy) or "") if f.occupancy else ""
        if not wanted(
            profile,
            permit_type,
            work_type,
            description,
            occupancy,
            type_fields=(permit_type, work_type, occupancy),
        ):
            continue
        permit_number = str(a.get(f.permit_number) or "") if f.permit_number else ""
        status = str(a.get(f.status) or "") if f.status else ""
        applicant = str(a.get(f.applicant) or "").strip()
        person = str(a.get(f.applicant_person) or "").strip() if f.applicant_person else ""
        if person and person.lower() != applicant.lower():
            applicant = f"{applicant} / {person}" if applicant else person
        address = str(a.get(f.address) or "").strip()
        if not address:
            continue  # the site is the lead; a permit with no site is noise
        leads.append(
            Lead(
                source=layer.name,
                signal=f"permit:{(work_type or permit_type).strip().lower()}"[:60],
                company_name=extract_tenant(description),
                applicant=applicant,
                address=address,
                city=layer.city,
                state=layer.state,
                zip=extract_zip(address),
                signal_date=_epoch_ms_to_iso(a.get(f.date)),
                evidence_url=layer.url,
                description=" | ".join(
                    x
                    for x in (
                        f"permit {permit_number}" if permit_number else "",
                        occupancy,
                        permit_type,
                        work_type,
                        status,
                        description,
                    )
                    if x
                )[:500],
                value=_number(a.get(f.value)),
                raw={
                    "attributes": a,
                    "value": a.get(f.value),
                    "lat": _number(a.get(f.latitude)) if f.latitude else 0.0,
                    "lon": _number(a.get(f.longitude)) if f.longitude else 0.0,
                },
            )
        )
    return leads


def fetch_permits(http: Http, layer: PermitLayer, profile: Profile, since_days: int) -> list[Lead]:
    """Pull permits issued in the last `since_days`, paging through the layer."""
    if not layer.url:
        return []
    since = datetime.now(UTC) - timedelta(days=since_days)
    where = f"{layer.fields.date} >= TIMESTAMP '{since.strftime('%Y-%m-%d %H:%M:%S')}'"
    features: list[dict] = []
    offset = 0
    while True:
        page = http.get_json(
            f"{layer.url}/query",
            params={
                "where": where,
                "outFields": "*",
                "orderByFields": f"{layer.fields.date} DESC",
                "resultOffset": offset,
                "resultRecordCount": PAGE_SIZE,
                "f": "json",
            },
        )
        if "error" in page:
            raise RuntimeError(f"{layer.name}: {page['error']}")
        batch = page.get("features", [])
        features.extend(batch)
        if not page.get("exceededTransferLimit") or not batch:
            break
        offset += len(batch)
    return parse_features(features, layer, profile)
