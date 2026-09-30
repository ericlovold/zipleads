"""Fill a missing zip from a street address with the US Census geocoder.

Free, no key, no documented rate limit for light use. Results are cached in
the store so an address is looked up once. Any failure returns an empty zip
and the pipeline falls back to city-level matching.
"""

from __future__ import annotations

import logging
import re

from zipleads.http import Http
from zipleads.store import Store

log = logging.getLogger("zipleads.geocode")

CENSUS_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
_ZIP_AT_END = re.compile(r"\b(\d{5})(?:-\d{4})?\s*$")


class Geocoder:
    def __init__(self, http: Http, store: Store):
        self.http = http
        self.store = store

    def zip_for(self, address: str, city: str, state: str) -> str:
        if not address:
            return ""
        query = ", ".join(x for x in (address, city, state) if x)
        cached = self.store.cached_zip(query)
        if cached is not None:
            return cached
        zip_code = ""
        try:
            data = self.http.get_json(
                CENSUS_URL,
                params={"address": query, "benchmark": "Public_AR_Current", "format": "json"},
            )
            matches = (data.get("result") or {}).get("addressMatches") or []
            if matches:
                matched = matches[0].get("matchedAddress", "")
                m = _ZIP_AT_END.search(matched)
                zip_code = m.group(1) if m else ""
        except Exception as exc:  # geocoding is best-effort
            log.debug("geocode failed for %r: %s", query, exc)
            return ""
        self.store.cache_zip(query, zip_code)
        return zip_code
