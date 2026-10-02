"""Business-license applications: a business naming itself before it opens.

A permit usually names the contractor. A license application names the
business and the entity that owns it, at the address where it will operate,
weeks before opening. That makes it the best early signal for a rep who needs
a business name to call.

Two feed types, both configured per territory under [[license_feeds]]:

- legistar: council resolutions approving license applications. Saint Paul
  sends Class N licenses (liquor, entertainment, auto, etc.) to council, and
  every title follows one template, for example:
    "Approving the application for change of ownership to the Liquor Off Sale
     and Tobacco Shop license now held by University Liquor LLC d/b/a Sharrett
     Liquor (License ID #20250000436) for the premises located at 2389
     University Avenue West."
- arcgis: a city license layer with an application date (Minneapolis
  publishes one through its Business License Data Explorer).

Each application is classified into the sorter's kinds so the profile's
drop and hide rules apply to licenses the same way they apply to permits.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from zipleads.config import LicenseFeed
from zipleads.http import Http
from zipleads.models import Lead
from zipleads.normalize import extract_zip
from zipleads.sorter import NEW_OCCUPANT, TENANT_REFRESH, Verdict

LEGISTAR_API = "https://webapi.legistar.com/v1"
LEGISTAR_PAGE = 1000
ARCGIS_PAGE = 1000

# Actions on an existing licensee that say nothing about a business arriving.
_NOT_A_LEAD = re.compile(
    r"\b(adverse|suspen\w*|revo\w+|penalt\w*|deni\w+|deny|withdraw\w*|void\w*|"
    r"cancel\w*|expired?|closed|inactive|renewal|surrender\w*)\b",
    re.I,
)
# Licenses with no fixed premises: no site for a business service to go into.
_NO_SITE = re.compile(
    r"\b(mobile food|food truck|food cart|peddler|solicit\w*|taxi\w*|transportation network|"
    r"temporary|one[- ]day|special event|christmas tree|fireworks|pedicab|limousine|"
    r"gambling manager)\b",
    re.I,
)
_OWNERSHIP = re.compile(r"\b(change of ownership|transfer of (the )?license|new owner\w*)\b", re.I)
_EXISTING = re.compile(
    r"\b(amend\w*|add(ing)?\b|upgrade\w*|expan\w+|modif\w+|remov\w+|extension of|"
    r"increase|change (to|in) (the )?(hours|conditions|premises|floor plan))",
    re.I,
)


def classify_license(text: str, license_type: str = "", status: str = "") -> Verdict | None:
    """Kind for one license application, or None when it is not a lead.

    Order matters: an adverse action or a mobile license is never a lead,
    an ownership change always is, and an amendment to an existing license
    is a refresh. Anything else is a new license at a fixed site.
    """
    whole = f"{text} {license_type} {status}"
    if _NOT_A_LEAD.search(f"{text} {status}"):
        return None
    if _NO_SITE.search(f"{license_type} {text}"):
        return None
    if m := _OWNERSHIP.search(whole):
        return Verdict(NEW_OCCUPANT, "high", (f"ownership change: '{m.group(0)}'",))
    if m := _EXISTING.search(text):
        return Verdict(TENANT_REFRESH, "medium", (f"existing licensee: '{m.group(0)}'",))
    reason = f"new license application: {license_type}" if license_type else "new license"
    return Verdict(NEW_OCCUPANT, "medium", (reason,))


# --- Legistar (council resolutions) ----------------------------------------

_PREMISES = re.compile(
    r"premises\s+located\s+at\s+(?P<addr>.+?)\s*(?:\(|;|,?\s+in\s+Ward\b|$)", re.I
)
_LICENSE_ID = re.compile(r"License\s+ID\s*#?\s*(?P<id>\d+)", re.I)
_DBA = re.compile(r"\s+d/?b/?a\s+", re.I)
# The owning entity follows the last "for", "held by" or "application of" before d/b/a.
# A capital or digit must come next, which skips "for a license approval for ...".
_OWNER_LEAD_IN = re.compile(r"\b(?:for|held by|application of)\s+(?=[A-Z0-9])")
_DBA_END = re.compile(
    r"\s+for\s+the\b|\s*\(License\s+ID|\s+to\s+(?:add|upgrade|amend|remove)\b|"
    r"\s+for\s+(?:a|an)\b|\s+for\s+the\s+premises\b|[.;]\s*$",
    re.I,
)
_TYPES_HELD = re.compile(r"\bto\s+the\s+(?P<t>[A-Z].+?)\s+licenses?\s+now\s+held\s+by\b")
_TYPES_FOR = re.compile(r"\bfor\s+the\s+(?P<t>[A-Z].+?)\s*(?:licenses?\s*)?\(License\s+ID")
_ACTION = re.compile(r"\bapplication\s+for\s+(?P<a>.+?)\s+(?:for|to|now)\s", re.I)


def parse_legistar_title(title: str) -> dict | None:
    """Fields from a license resolution title, or None if it is not one."""
    title = " ".join((title or "").split())
    if not re.search(r"\bapplication\b", title, re.I):
        return None
    if not re.search(r"\blicen[sc]e", title, re.I):
        return None
    premises = _PREMISES.search(title)
    if not premises:
        return None
    address = premises.group("addr").strip().rstrip(".").strip()

    legal = dba = ""
    split = _DBA.search(title)
    if split:
        before, after = title[: split.start()], title[split.end() :]
        lead_ins = list(_OWNER_LEAD_IN.finditer(before))
        if lead_ins:
            legal = before[lead_ins[-1].end() :].strip(" ,")
        end = _DBA_END.search(after)
        dba = (after[: end.start()] if end else after).strip(" ,.")
    else:
        # No trade name: "... for Foo LLC for the Liquor On Sale (License ID ...)".
        m = re.search(r"\bfor\s+(?P<n>[A-Z0-9][^()]+?)\s+for\s+the\b", title)
        legal = m.group("n").strip() if m else ""
        if lead_ins := list(_OWNER_LEAD_IN.finditer(legal)):
            legal = legal[lead_ins[-1].end() :]

    types = ""
    if m := _TYPES_HELD.search(title) or _TYPES_FOR.search(title):
        types = m.group("t").strip()
        if lead_ins := list(re.finditer(r"\bfor\s+the\s+(?=[A-Z])", types)):
            types = types[lead_ins[-1].end() :]  # "for the X ... for the Liquor" keeps the last
    action = m.group("a").strip() if (m := _ACTION.search(title)) else ""
    lid = m.group("id") if (m := _LICENSE_ID.search(title)) else ""
    if not (legal or dba):
        return None
    return {
        "legal": legal,
        "dba": dba or legal,
        "license_types": types,
        "action": action,
        "license_id": lid,
        "address": address,
    }


def _legistar_date(value) -> str:
    return str(value or "")[:10]


def parse_matters(matters: list[dict], feed: LicenseFeed) -> list[Lead]:
    leads: list[Lead] = []
    site = f"https://{feed.client}.legistar.com"
    for m in matters:
        title = str(m.get("MatterTitle") or m.get("MatterName") or "")
        fields = parse_legistar_title(title)
        if fields is None:
            continue
        verdict = classify_license(title, fields["license_types"])
        if verdict is None:
            continue
        matter_file = str(m.get("MatterFile") or "")
        url = (
            f"{site}/LegislationDetail.aspx?ID={m.get('MatterId')}&GUID={m.get('MatterGuid')}"
            if m.get("MatterId") and m.get("MatterGuid")
            else site
        )
        leads.append(
            Lead(
                source=feed.name,
                signal=f"license:{(fields['license_types'] or 'application').lower()}"[:60],
                company_name=fields["dba"],
                applicant=fields["legal"] if fields["legal"] != fields["dba"] else "",
                address=fields["address"],
                city=feed.city,
                state=feed.state,
                zip=extract_zip(fields["address"]),
                signal_date=_legistar_date(m.get("MatterIntroDate")),
                evidence_url=url,
                description=" | ".join(
                    x
                    for x in (
                        matter_file,
                        fields["license_types"],
                        fields["action"],
                        str(m.get("MatterStatusName") or ""),
                    )
                    if x
                )[:500],
                kind=verdict.kind,
                raw={
                    "kind_confidence": verdict.confidence,
                    "kind_reasons": list(verdict.reasons),
                    "license_id": fields["license_id"],
                    "matter_id": m.get("MatterId"),
                    "legal_name": fields["legal"],
                    # Kept out of the description: it repeats the owner and the street,
                    # which the segment scrub would read as industry words.
                    "title": title,
                },
            )
        )
    return leads


def legistar_matters(http: Http, feed: LicenseFeed, since_days: int) -> list[dict]:
    """Every matter introduced in the last `since_days`, newest first.

    The date filter runs on the server; the title filter runs in parse_matters
    because Legistar's OData string functions vary between clients.
    """
    since = (datetime.now(UTC) - timedelta(days=since_days)).strftime("%Y-%m-%d")
    matters: list[dict] = []
    skip = 0
    while True:
        page = http.get_json(
            f"{LEGISTAR_API}/{feed.client}/matters",
            params={
                "$filter": f"MatterIntroDate ge datetime'{since}'",
                "$orderby": "MatterIntroDate desc",
                "$top": LEGISTAR_PAGE,
                "$skip": skip,
            },
        )
        if not isinstance(page, list):
            raise RuntimeError(f"{feed.name}: unexpected Legistar response {str(page)[:200]}")
        matters.extend(page)
        if len(page) < LEGISTAR_PAGE:
            break
        skip += len(page)
    return matters


def fetch_legistar(http: Http, feed: LicenseFeed, since_days: int) -> list[Lead]:
    """License applications among the matters introduced in the last `since_days`."""
    if not feed.client:
        return []
    return parse_matters(legistar_matters(http, feed, since_days), feed)


# --- ArcGIS license layer ---------------------------------------------------


def _epoch_ms_to_iso(value) -> str:
    if value in (None, ""):
        return ""
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC).date().isoformat()
    except (TypeError, ValueError, OSError):
        return str(value)[:10]


def _number(value) -> float:
    try:
        return float(value) if value not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0


def parse_license_features(features: list[dict], feed: LicenseFeed) -> list[Lead]:
    f = feed.fields
    leads: list[Lead] = []
    seen: set[str] = set()
    for feat in features:
        a = feat.get("attributes", {})

        def get(name: str, a=a) -> str:
            return str(a.get(name) or "").strip() if name else ""

        number = get(f.license_number)
        if number:
            if number in seen:
                continue
            seen.add(number)
        address = get(f.address)
        name = get(f.business_name) or get(f.legal_name)
        if not address or not name:
            continue
        license_type, status, text = get(f.license_type), get(f.status), get(f.description)
        verdict = classify_license(text, license_type, status)
        if verdict is None:
            continue
        legal = get(f.legal_name)
        leads.append(
            Lead(
                source=feed.name,
                signal=f"license:{(license_type or 'application').lower()}"[:60],
                company_name=name,
                applicant=legal if legal.lower() != name.lower() else "",
                address=address,
                city=feed.city,
                state=feed.state,
                zip=extract_zip(address),
                signal_date=_epoch_ms_to_iso(a.get(f.date)),
                evidence_url=feed.url,
                description=" | ".join(
                    x
                    for x in (
                        f"license {number}" if number else "",
                        license_type,
                        status,
                        text,
                    )
                    if x
                )[:500],
                kind=verdict.kind,
                raw={
                    "kind_confidence": verdict.confidence,
                    "kind_reasons": list(verdict.reasons),
                    "attributes": a,
                    "legal_name": legal,
                    "lat": _number(a.get(f.latitude)) if f.latitude else 0.0,
                    "lon": _number(a.get(f.longitude)) if f.longitude else 0.0,
                },
            )
        )
    return leads


def fetch_license_layer(http: Http, feed: LicenseFeed, since_days: int) -> list[Lead]:
    """Applications filed in the last `since_days`, paging through the layer."""
    if not feed.url:
        return []
    since = datetime.now(UTC) - timedelta(days=since_days)
    where = f"{feed.fields.date} >= TIMESTAMP '{since.strftime('%Y-%m-%d %H:%M:%S')}'"
    features: list[dict] = []
    offset = 0
    while True:
        page = http.get_json(
            f"{feed.url}/query",
            params={
                "where": where,
                "outFields": "*",
                "orderByFields": f"{feed.fields.date} DESC",
                "resultOffset": offset,
                "resultRecordCount": ARCGIS_PAGE,
                "f": "json",
            },
        )
        if "error" in page:
            raise RuntimeError(f"{feed.name}: {page['error']}")
        batch = page.get("features", [])
        features.extend(batch)
        if not page.get("exceededTransferLimit") or not batch:
            break
        offset += len(batch)
    return parse_license_features(features, feed)


def fetch_licenses(http: Http, feed: LicenseFeed, since_days: int) -> list[Lead]:
    if feed.type == "legistar":
        return fetch_legistar(http, feed, since_days)
    return fetch_license_layer(http, feed, since_days)
