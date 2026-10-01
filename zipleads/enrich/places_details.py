"""Business phone and website from Google Places.

Two calls at most per lead: a text search to find the place from name and
address (skipped when we already hold a place_id), then a details call with
an Enterprise-SKU mask limited to phone and website.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from zipleads.http import Http
from zipleads.normalize import normalize_address, normalize_name

SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
NEARBY_URL = "https://places.googleapis.com/v1/places:searchNearby"
NEARBY_RADIUS_M = 50.0  # a tower's permit point is the parcel centroid; tenants geocode at the door
DETAILS_URL = "https://places.googleapis.com/v1/places/{place_id}"
SEARCH_MASK = (
    "places.id,places.displayName,places.formattedAddress,places.types,places.businessStatus"
)
DETAILS_MASK = "id,nationalPhoneNumber,websiteUri,businessStatus"


@dataclass
class PlaceHit:
    place_id: str
    name: str
    address: str


@dataclass
class Contact:
    place_id: str = ""
    phone: str = ""
    website: str = ""


def _similar(a: str, b: str) -> bool:
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return False
    return na in nb or nb in na or na.split()[0] == nb.split()[0]


_DIRECTIONALS = {"n", "s", "e", "w", "ne", "nw", "se", "sw"}
# Text search also returns the address itself, the building, and geographic areas.
# None of those is a business.
_NOT_A_BUSINESS = {
    "street_address",
    "premise",
    "subpremise",
    "route",
    "intersection",
    "geocode",
    "postal_code",
    "locality",
    "sublocality",
    "neighborhood",
    "political",
    "plus_code",
    "administrative_area_level_1",
    "administrative_area_level_2",
    "country",
    "parking",
    "point_of_interest_only",
}


def is_business(place: dict) -> bool:
    """Reject results that are an address, a building, or an area rather than a business.

    A place typed only with address-like types is out. So is one whose name is
    just its own street address ("575 SE 9th St"), whatever its types say.
    """
    types = set(place.get("types") or [])
    real_types = types - _NOT_A_BUSINESS - {"point_of_interest", "establishment"}
    if types & _NOT_A_BUSINESS and not real_types:
        return False
    name = (place.get("displayName") or {}).get("text", "")
    formatted = place.get("formattedAddress", "")
    if _looks_like_address(name):
        return False
    if name and formatted and _street_key(name) == _street_key(formatted):
        return False
    return True


_ADDRESS_NAME = re.compile(
    r"^\d{1,6}\s+(?:[NSEW]{1,2}\s+)?[\w'.-]+(?:\s+[\w'.-]+)*\s+"
    r"(?:st|street|ave|avenue|blvd|boulevard|rd|road|dr|drive|ln|lane|ct|court|pl|place|"
    r"pkwy|parkway|hwy|highway|way|trail|trl|cir|circle)\b",
    re.IGNORECASE,
)


def _looks_like_address(name: str) -> bool:
    """'1500 Washington Ave S, Minneapolis, MN 55454' is an address, not a business."""
    return bool(_ADDRESS_NAME.match((name or "").strip()))


def _street_key(address: str) -> str:
    """House number plus street name, ignoring directionals and suffixes.

    '60 6TH ST S' and Google's '60 S 6th St' both become '60 6th'.
    """
    parts = normalize_address(address).split()
    if not parts:
        return ""
    number = parts[0]
    street = next((t for t in parts[1:] if t not in _DIRECTIONALS), "")
    return f"{number} {street}".strip()


def find_at_address(
    http: Http, api_key: str, address: str, city: str, state: str, limit: int = 5
) -> list[PlaceHit]:
    """Businesses Places lists at a street address. Used when a permit names no tenant."""
    if not address:
        return []
    query = ", ".join(x for x in (address, city, state) if x)
    page = http.post_json(
        SEARCH_URL,
        {"textQuery": query, "pageSize": limit},
        headers={"X-Goog-Api-Key": api_key, "X-Goog-FieldMask": SEARCH_MASK},
    )
    want = _street_key(address)
    hits = []
    for p in page.get("places", []):
        if not is_business(p):
            continue
        formatted = p.get("formattedAddress", "")
        if want and _street_key(formatted) != want:
            continue  # a nearby place, not this address
        hits.append(
            PlaceHit(
                place_id=p.get("id", ""),
                name=(p.get("displayName") or {}).get("text", ""),
                address=formatted,
            )
        )
    return hits


def find_near(
    http: Http, api_key: str, lat: float, lon: float, address: str, limit: int = 10
) -> list[PlaceHit]:
    """Businesses within a few meters of a coordinate, matched to the street address.

    Text search for a bare address returns the address, not the tenants. Nearby
    search around the permit's own coordinate returns what is actually there.
    """
    if not lat or not lon:
        return []
    page = http.post_json(
        NEARBY_URL,
        {
            "locationRestriction": {
                "circle": {"center": {"latitude": lat, "longitude": lon}, "radius": NEARBY_RADIUS_M}
            },
            "maxResultCount": limit,
        },
        headers={"X-Goog-Api-Key": api_key, "X-Goog-FieldMask": SEARCH_MASK},
    )
    want = _street_key(address)
    hits = []
    for p in page.get("places", []):
        if not is_business(p):
            continue
        formatted = p.get("formattedAddress", "")
        if not formatted or (want and _street_key(formatted) != want):
            continue  # next door, or no address to confirm it is this building
        hits.append(
            PlaceHit(
                place_id=p.get("id", ""),
                name=(p.get("displayName") or {}).get("text", ""),
                address=formatted,
            )
        )
    return hits


def find_place_id(http: Http, api_key: str, company_name: str, address: str) -> str:
    hit = find_by_name(http, api_key, company_name, address)
    return hit.place_id if hit else ""


def find_by_name(http: Http, api_key: str, company_name: str, where: str) -> PlaceHit | None:
    """Best Places match for a business name near `where` (an address or a city).

    Used for permits that name a tenant and for news leads that name a
    business but carry no address. Returns the first business-typed result
    whose name resembles the query.
    """
    if not company_name or not where:
        return None
    page = http.post_json(
        SEARCH_URL,
        {"textQuery": f"{company_name} {where}", "pageSize": 3},
        headers={"X-Goog-Api-Key": api_key, "X-Goog-FieldMask": SEARCH_MASK},
    )
    for p in page.get("places", []):
        if not is_business(p):
            continue
        name = (p.get("displayName") or {}).get("text", "")
        if _similar(company_name, name):
            return PlaceHit(
                place_id=p.get("id", ""), name=name, address=p.get("formattedAddress", "")
            )
    return None


def fetch_contact(http: Http, api_key: str, place_id: str) -> Contact:
    if not place_id:
        return Contact()
    d = http.get_json(
        DETAILS_URL.format(place_id=place_id),
        headers={"X-Goog-Api-Key": api_key, "X-Goog-FieldMask": DETAILS_MASK},
    )
    return Contact(
        place_id=place_id,
        phone=d.get("nationalPhoneNumber", "") or "",
        website=d.get("websiteUri", "") or "",
    )
