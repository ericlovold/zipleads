"""Google News RSS search, one feed per territory city.

Free and unofficial. Each feed returns at most 100 items, so we run one
narrow query per city rather than one wide query. Company names are pulled
from the headline with a verb-pattern heuristic; anything it cannot parse is
kept with the headline as the company name and a low score, so a human can
still see it.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import quote

from zipleads.http import Http
from zipleads.models import Lead
from zipleads.territory import TerritoryMatcher

FEED_URL = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"

_VERB = (
    r"(?:sets? (?:a )?grand opening|announces? (?:a )?grand opening|plans? to open|"
    r"preparing to open|"
    r"set to open|is set to open|eyes? (?:[\w-]+ ){0,3}(?:opening|location)|readies|"
    r"breaks ground|signs? (?:a )?lease|signed a lease|to open|is opening|will open|opens|opening|"
    r"relocat\w*|is moving|moves|moving|expands|expanding|to expand|to move|"
    r"headed to|coming to|announces new|debuts|launches)"
)
_ORG_BEFORE_VERB = re.compile(rf"^(?P<org>[A-Z][^,:;]{{1,60}}?)\s+{_VERB}\b", re.IGNORECASE)
_POSSESSIVE = re.compile(r"^(?P<org>[A-Z][^,:;]{1,60}?)['’]s new (?:location|office|store|clinic)")
_TRAILING_SOURCE = re.compile(r"\s+-\s+[^-]{2,60}$")
_BASED_IN = re.compile(r"\b[\w. ]{2,30}-based\b", re.IGNORECASE)
_LEADING_JUNK = re.compile(
    r"^(?:exclusive|report|watch|photos|opinion|update)\s*:\s*", re.IGNORECASE
)

STATE_NAMES = {
    "AL": "Alabama",
    "AK": "Alaska",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "FL": "Florida",
    "GA": "Georgia",
    "HI": "Hawaii",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "IA": "Iowa",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "ME": "Maine",
    "MD": "Maryland",
    "MA": "Massachusetts",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MS": "Mississippi",
    "MO": "Missouri",
    "MT": "Montana",
    "NE": "Nebraska",
    "NV": "Nevada",
    "NH": "New Hampshire",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NY": "New York",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "UT": "Utah",
    "VT": "Vermont",
    "VA": "Virginia",
    "WA": "Washington",
    "WV": "West Virginia",
    "WI": "Wisconsin",
    "WY": "Wyoming",
    "DC": "District of Columbia",
}


def build_query(terms: tuple[str, ...], city: str, state: str, days: int) -> str:
    state_name = STATE_NAMES.get(state.upper(), state)
    return f'({" OR ".join(terms)}) "{city}" {state_name} when:{days}d'.strip()


def feed_url(query: str) -> str:
    return FEED_URL.format(q=quote(query, safe=""))


def clean_title(title: str) -> str:
    t = _TRAILING_SOURCE.sub("", title.strip())
    return _LEADING_JUNK.sub("", t).strip()


def extract_company(title: str) -> str:
    t = clean_title(title)
    for pat in (_POSSESSIVE, _ORG_BEFORE_VERB):
        m = pat.match(t)
        if m:
            org = m.group("org").strip(" '\"")
            org = re.sub(r"^(?:the|a|an)\s+", "", org, flags=re.IGNORECASE)
            org = re.sub(r"^[\w .]+-based\s+", "", org, flags=re.IGNORECASE)
            return org
    return ""


def parse_feed(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    items = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        source_el = item.find("source")
        source_name = (source_el.text or "").strip() if source_el is not None else ""
        try:
            pub_iso = parsedate_to_datetime(pub).date().isoformat() if pub else ""
        except (TypeError, ValueError):
            pub_iso = ""
        items.append({"title": title, "link": link, "published": pub_iso, "outlet": source_name})
    return items


def items_to_leads(
    items: list[dict],
    matcher: TerritoryMatcher,
    query_city: str,
    state: str,
    exclude_terms: tuple[str, ...] = (),
) -> list[Lead]:
    leads: list[Lead] = []
    for it in items:
        title = it["title"]
        lowered = f"{title} {it.get('outlet', '')}".lower()
        if any(term in lowered for term in exclude_terms):
            continue
        # "Minneapolis-based X relocating to Woodbury": the origin is not the lead's city.
        city = matcher.match_city(_BASED_IN.sub("", clean_title(title))) or query_city
        company = extract_company(title)
        leads.append(
            Lead(
                source="google_news",
                signal="news:headline" if company else "news:unparsed",
                company_name=company or clean_title(title)[:80],
                city=city,
                state=state,
                signal_date=it["published"],
                evidence_url=it["link"],
                description=f"{clean_title(title)} ({it['outlet']})"[:500],
                raw=it,
            )
        )
    return leads


def fetch_news(
    http: Http,
    matcher: TerritoryMatcher,
    terms: tuple[str, ...],
    days: int,
    exclude_terms: tuple[str, ...] = (),
) -> list[Lead]:
    state = matcher.territory.state
    leads: list[Lead] = []
    seen_links: set[str] = set()
    for city in matcher.cities():
        xml_text = http.get_text(feed_url(build_query(terms, city, state, days)))
        items = [i for i in parse_feed(xml_text) if i["link"] not in seen_links]
        seen_links.update(i["link"] for i in items)
        leads.extend(items_to_leads(items, matcher, city, state, exclude_terms))
    return leads
