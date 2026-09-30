"""Business phone and website from Google Places.

Two calls at most per lead: a text search to find the place from name and
address (skipped when we already hold a place_id), then a details call with
an Enterprise-SKU mask limited to phone and website.
"""

from __future__ import annotations

from dataclasses import dataclass

from zipleads.http import Http
from zipleads.normalize import normalize_address, normalize_name

SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
DETAILS_URL = "https://places.googleapis.com/v1/places/{place_id}"
SEARCH_MASK = "places.id,places.displayName,places.formattedAddress"
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


def find_place_id(http: Http, api_key: str, company_name: str, address: str) -> str:
    if not company_name or not address:
        return ""
    page = http.post_json(
        SEARCH_URL,
        {"textQuery": f"{company_name} {address}", "pageSize": 3},
        headers={"X-Goog-Api-Key": api_key, "X-Goog-FieldMask": SEARCH_MASK},
    )
    for p in page.get("places", []):
        if _similar(company_name, (p.get("displayName") or {}).get("text", "")):
            return p.get("id", "")
    return ""


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
