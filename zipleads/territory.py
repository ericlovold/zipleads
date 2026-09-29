"""Zip filter and city matching over a loaded Territory."""

from __future__ import annotations

from zipleads.config import Territory
from zipleads.normalize import normalize_address


class TerritoryMatcher:
    def __init__(self, territory: Territory):
        self.territory = territory
        # "St. Paul" -> "st paul"; "Saint Paul" also maps to "st paul".
        self._city_tokens: dict[str, str] = {}
        for city in territory.cities():
            for part in city.split("/"):
                token = normalize_address(part).replace("saint ", "st ")
                if len(token) >= 4:
                    self._city_tokens.setdefault(token, city)

    def contains_zip(self, zip_code: str) -> bool:
        return zip_code.strip()[:5] in self.territory.zips

    def match_city(self, text: str) -> str:
        """Return the territory city mentioned last in text, or empty string.

        Last wins because relocation headlines name the destination last
        ("Eagan clinic moving to Woodbury"). Longer tokens are checked first so
        "st paul park" is not shadowed by "st paul".
        """
        hay = " " + normalize_address(text).replace("saint ", "st ") + " "
        best_label, best_pos = "", -1
        for token, label in sorted(self._city_tokens.items(), key=lambda kv: -len(kv[0])):
            pos = hay.rfind(f" {token} ")
            if pos > best_pos:
                best_label, best_pos = label, pos
        return best_label

    def cities(self) -> list[str]:
        return self.territory.cities()

    def search_areas(self) -> list[str]:
        """Text for place searches: city names when known, else bare zips."""
        areas = [f"{c}, {self.territory.state}".strip(", ") for c in self.cities()]
        areas += [z for z, city in self.territory.zips.items() if not city]
        return areas
