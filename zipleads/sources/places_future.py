"""Google Places API (New): businesses with businessStatus = FUTURE_OPENING.

The only source that fires before a business opens and already has a
listing. Text search bills at the Pro SKU per call; the field mask stays
inside Pro. Phone and website (Enterprise SKU) are fetched later in enrich,
only for leads that survived the territory filter.

Cost: one call per (search area x profile query x page). A territory with
85 cities and two queries is 170 calls per page level. MAX_PAGES defaults
to 1; raise it only when a run shows nextPageToken with FUTURE_OPENING hits.

Live result, Eagan MN, 2026-09-30: "opening soon" returned 20 OPERATIONAL,
"coming soon" returned 15 with no status and 5 OPERATIONAL. Zero
FUTURE_OPENING. Text search ranks on the query words, and pre-opening
listings do not carry those words, so this source is off by default. Kept
for experiments with other query strategies.
"""

from __future__ import annotations

from zipleads.http import Http
from zipleads.models import Lead
from zipleads.territory import TerritoryMatcher

SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = ",".join(
    [
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.addressComponents",
        "places.businessStatus",
        "places.primaryType",
        "nextPageToken",
    ]
)
FUTURE_OPENING = "FUTURE_OPENING"
MAX_PAGES = 1


def _component(place: dict, kind: str) -> str:
    for c in place.get("addressComponents", []) or []:
        if kind in c.get("types", []):
            return c.get("shortText") or c.get("longText") or ""
    return ""


def parse_places(places: list[dict], matcher: TerritoryMatcher) -> list[Lead]:
    leads: list[Lead] = []
    for p in places:
        if p.get("businessStatus") != FUTURE_OPENING:
            continue
        zip_code = _component(p, "postal_code")[:5]
        if not matcher.contains_zip(zip_code):
            continue
        leads.append(
            Lead(
                source="places_future",
                signal="places:future_opening",
                company_name=(p.get("displayName") or {}).get("text", ""),
                address=p.get("formattedAddress", ""),
                city=_component(p, "locality"),
                state=_component(p, "administrative_area_level_1") or matcher.territory.state,
                zip=zip_code,
                evidence_url=f"https://www.google.com/maps/place/?q=place_id:{p.get('id', '')}",
                description=p.get("primaryType", ""),
                raw={"place_id": p.get("id"), "primaryType": p.get("primaryType")},
            )
        )
    return leads


def fetch_future_openings(
    http: Http,
    api_key: str,
    matcher: TerritoryMatcher,
    queries: tuple[str, ...],
    max_pages: int = MAX_PAGES,
) -> list[Lead]:
    headers = {"X-Goog-Api-Key": api_key, "X-Goog-FieldMask": FIELD_MASK}
    leads: list[Lead] = []
    seen: set[str] = set()
    for area in matcher.search_areas():
        for query in queries:
            body: dict = {"textQuery": f"{query} {area}", "pageSize": 20}
            for _ in range(max_pages):
                page = http.post_json(SEARCH_URL, body, headers=headers)
                places = [p for p in page.get("places", []) if p.get("id") not in seen]
                seen.update(p.get("id") for p in places)
                leads.extend(parse_places(places, matcher))
                token = page.get("nextPageToken")
                if not token:
                    break
                body = {**body, "pageToken": token}
    return leads
