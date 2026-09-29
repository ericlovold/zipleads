"""Decision-maker name, title, email and direct phone from ZoomInfo.

Spends ZoomInfo credits. Nothing here runs unless settings.zoominfo_enabled
is true, which requires credentials and ZOOMINFO_ENABLE=true.

Flow (ZoomInfo Enterprise API):
  1. POST /authenticate with username/password -> JWT (valid about an hour).
  2. POST /search/contact by company website or name plus the profile's
     titles. Search does not consume enrichment credits.
  3. POST /enrich/contact for the single best-ranked person. This is the
     credit-consuming call, one contact per lead.

Endpoint paths and field names follow ZoomInfo's public API reference but
were not exercised against a live account. Confirm on the first run.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from zipleads.http import Http

BASE_URL = "https://api.zoominfo.com"
AUTH_URL = f"{BASE_URL}/authenticate"
SEARCH_URL = f"{BASE_URL}/search/contact"
ENRICH_URL = f"{BASE_URL}/enrich/contact"
OUTPUT_FIELDS = [
    "id",
    "firstName",
    "lastName",
    "jobTitle",
    "email",
    "directPhoneDoNotCall",
    "directPhone",
    "phone",
    "companyName",
    "companyWebsite",
]


@dataclass
class Person:
    name: str = ""
    title: str = ""
    email: str = ""
    phone: str = ""
    zoominfo_id: str = ""


def domain_from_website(website: str) -> str:
    if not website:
        return ""
    host = urlparse(website if "://" in website else f"https://{website}").netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _rank(title: str, priority: tuple[str, ...]) -> int:
    t = (title or "").lower()
    for i, key in enumerate(priority):
        if key in t:
            return i
    return len(priority)


class ZoomInfoClient:
    def __init__(self, http: Http, username: str, password: str):
        self.http = http
        self.username = username
        self.password = password
        self._jwt = ""

    def _headers(self) -> dict:
        if not self._jwt:
            resp = self.http.post_json(
                AUTH_URL, {"username": self.username, "password": self.password}
            )
            self._jwt = resp.get("jwt", "")
            if not self._jwt:
                raise RuntimeError("zoominfo: authenticate returned no jwt")
        return {"Authorization": f"Bearer {self._jwt}", "Content-Type": "application/json"}

    def search_people(
        self, company_name: str, website: str, titles: tuple[str, ...]
    ) -> list[Person]:
        body: dict = {"rpp": 10, "page": 1}
        domain = domain_from_website(website)
        if domain:
            body["companyWebsite"] = domain
        elif company_name:
            body["companyName"] = company_name
        else:
            return []
        if titles:
            body["jobTitle"] = " OR ".join(titles)
        page = self.http.post_json(SEARCH_URL, body, headers=self._headers())
        people = [
            Person(
                name=f"{p.get('firstName', '')} {p.get('lastName', '')}".strip(),
                title=p.get("jobTitle", "") or "",
                zoominfo_id=str(p.get("id", "") or ""),
            )
            for p in page.get("data", [])
        ]
        return sorted(people, key=lambda p: _rank(p.title, titles))

    def enrich(self, person: Person) -> Person:
        if not person.zoominfo_id:
            return person
        resp = self.http.post_json(
            ENRICH_URL,
            {"matchPersonInput": [{"personId": person.zoominfo_id}], "outputFields": OUTPUT_FIELDS},
            headers=self._headers(),
        )
        results = (resp.get("data") or {}).get("result") or []
        if not results or not results[0].get("data"):
            return person
        d = results[0]["data"][0]
        name = f"{d.get('firstName', '')} {d.get('lastName', '')}".strip()
        return Person(
            name=name or person.name,
            title=d.get("jobTitle", "") or person.title,
            email=d.get("email", "") or "",
            phone=d.get("directPhone", "") or d.get("phone", "") or "",
            zoominfo_id=person.zoominfo_id,
        )

    def best_contact(
        self, company_name: str, website: str, titles: tuple[str, ...]
    ) -> Person | None:
        people = self.search_people(company_name, website, titles)
        if not people:
            return None
        return self.enrich(people[0])
