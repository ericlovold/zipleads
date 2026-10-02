"""Orchestration: ingest -> territory filter -> store -> score; enrich."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from zipleads.config import Profile, Settings, Territory
from zipleads.enrich import places_details
from zipleads.enrich.zoominfo import ZoomInfoClient
from zipleads.geocode import Geocoder
from zipleads.http import BudgetedHttp, BudgetExhausted, Http
from zipleads.models import Lead
from zipleads.normalize import extract_zip, normalize_address, normalize_name
from zipleads.score import score_lead
from zipleads.segments import excluded_by, flags_for
from zipleads.sources import arcgis_permits, google_news, licenses, places_future
from zipleads.store import Store
from zipleads.territory import TerritoryMatcher

log = logging.getLogger("zipleads")

BUILTIN_SOURCES = ("permits", "licenses", "news", "places")
# Places text search does not surface FUTURE_OPENING listings (verified live, Eagan MN,
# 2026-09-30: 0 of 40). It stays available with `--sources places` but is off by default.
DEFAULT_SOURCES = ("permits", "licenses", "news")


@dataclass
class Context:
    settings: Settings
    territory: Territory
    profile: Profile
    http: Http
    store: Store

    def __post_init__(self) -> None:
        self.matcher = TerritoryMatcher(self.territory)
        self.geocoder = Geocoder(self.http, self.store)


@dataclass
class IngestReport:
    fetched: dict[str, int] = field(default_factory=dict)
    kept: int = 0
    created: int = 0
    merged: int = 0
    dropped_out_of_territory: int = 0
    dropped_by_segment: dict[str, int] = field(default_factory=dict)
    dropped_by_kind: dict[str, int] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)


def in_territory(lead: Lead, matcher: TerritoryMatcher) -> bool:
    """Zip wins when present. News has no zip, so a territory city is enough."""
    if lead.zip:
        return matcher.contains_zip(lead.zip)
    return bool(lead.city) and matcher.match_city(lead.city) != ""


def _rescore(ctx: Context, key: str) -> int:
    row = ctx.store.get(key)
    assert row is not None
    score = score_lead(
        ctx.profile,
        sources=row["sources"],
        signals=row["signals"],
        distinct_addresses=ctx.store.distinct_addresses(row["norm_name"]),
        phone=row["phone"],
        contact_email=row["contact_email"],
        value=row["value"],
    )
    ctx.store.set_score(key, score)
    return score


def _absorb(ctx: Context, leads: list[Lead], report: IngestReport) -> None:
    for lead in leads:
        if lead.kind and lead.kind in ctx.profile.sorter_drop:
            report.dropped_by_kind[lead.kind] = report.dropped_by_kind.get(lead.kind, 0) + 1
            continue
        if not lead.zip and lead.address:
            lead.zip = ctx.geocoder.zip_for(lead.address, lead.city, lead.state)
        if not in_territory(lead, ctx.matcher):
            report.dropped_out_of_territory += 1
            continue
        if term := excluded_by(ctx.profile, lead):
            report.dropped_by_segment[term] = report.dropped_by_segment.get(term, 0) + 1
            continue
        key, created = ctx.store.upsert(lead)
        for flag in flags_for(ctx.profile, lead):
            ctx.store.add_signal(key, flag)
        _rescore(ctx, key)
        report.kept += 1
        if created:
            report.created += 1
        else:
            report.merged += 1


def _run_source(ctx: Context, name: str, report: IngestReport) -> None:
    try:
        if name == "permits":
            for layer in ctx.territory.permit_layers:
                if not layer.url:
                    log.info("%s: no url in territory, skipping", layer.name)
                    continue
                try:
                    leads = arcgis_permits.fetch_permits(
                        ctx.http, layer, ctx.profile, ctx.settings.ingest_days
                    )
                except Exception as exc:
                    log.exception("%s failed", layer.name)
                    report.errors[layer.name] = f"{type(exc).__name__}: {exc}"
                    continue
                report.fetched[layer.name] = len(leads)
                _absorb(ctx, leads, report)
            return
        if name == "licenses":
            for feed in ctx.territory.license_feeds:
                if not feed.configured:
                    log.info("%s: no url/client in territory, skipping", feed.name)
                    continue
                try:
                    leads = licenses.fetch_licenses(ctx.http, feed, ctx.settings.ingest_days)
                except Exception as exc:
                    log.exception("%s failed", feed.name)
                    report.errors[feed.name] = f"{type(exc).__name__}: {exc}"
                    continue
                report.fetched[feed.name] = len(leads)
                _absorb(ctx, leads, report)
            return
        if name == "news":
            leads = google_news.fetch_news(
                ctx.http,
                ctx.matcher,
                ctx.profile.news_terms,
                ctx.settings.ingest_days,
                ctx.profile.news_exclude_terms,
            )
        elif name == "places":
            if not ctx.settings.places_enabled:
                log.info("places: GOOGLE_PLACES_API_KEY not set, skipping")
                return
            leads = places_future.fetch_future_openings(
                BudgetedHttp(ctx.http, ctx.settings.places_max_calls, label="google places"),
                ctx.settings.google_places_api_key,
                ctx.matcher,
                ctx.profile.places_queries,
            )
        else:
            raise ValueError(f"unknown source {name!r}")
    except Exception as exc:  # one bad source must not sink the run
        log.exception("%s failed", name)
        report.errors[name] = f"{type(exc).__name__}: {exc}"
        return
    report.fetched[name] = len(leads)
    _absorb(ctx, leads, report)


def ingest(ctx: Context, sources: tuple[str, ...]) -> IngestReport:
    report = IngestReport()
    for name in sources:
        _run_source(ctx, name, report)
    return report


@dataclass
class EnrichReport:
    attempted: int = 0
    phones_found: int = 0
    companies_found: int = 0  # address-only leads that Places resolved to one business
    contacts_found: int = 0
    zoominfo_skipped: bool = False
    places_calls: int = 0
    stopped_at_budget: bool = False  # hit PLACES_MAX_CALLS_PER_RUN; the rest wait for next run
    errors: dict[str, str] = field(default_factory=dict)


def _same_place(ctx: Context, row, found_address: str) -> bool:
    """Is a by-name Places match the location this lead is about?

    A lead with its own address only needs the match inside the territory. A
    lead known only by city (news) must match in that city: "Chick-fil-A" in
    Shakopee news resolving to the Chanhassen store is a different location.
    """
    zip_code = extract_zip(found_address)
    if zip_code and not ctx.matcher.contains_zip(zip_code):
        return False
    if row["address"]:
        return True
    lead_city = normalize_address(row["city"] or "").replace("saint ", "st ")
    found_city = normalize_address(ctx.matcher.match_city(found_address) or "").replace(
        "saint ", "st "
    )
    return bool(lead_city) and lead_city == found_city


def _coords_from_raw(store: Store, key: str) -> tuple[float, float]:
    """Latitude/longitude from any permit or license sighting of this lead, else (0, 0)."""
    row = store.conn.execute(
        "SELECT raw FROM sightings WHERE dedupe_key = ? "
        "AND (source LIKE '%permits' OR source LIKE '%licenses') LIMIT 1",
        (key,),
    ).fetchone()
    if not row:
        return 0.0, 0.0
    try:
        raw = json.loads(row[0])
        return float(raw.get("lat") or 0), float(raw.get("lon") or 0)
    except (ValueError, TypeError, AttributeError):
        return 0.0, 0.0


def _place_id_from_raw(store: Store, key: str) -> str:
    """A places_future lead already knows its place_id; reuse it and skip a search."""
    row = store.conn.execute(
        "SELECT raw FROM sightings WHERE dedupe_key = ? AND source = 'places_future' LIMIT 1",
        (key,),
    ).fetchone()
    if not row:
        return ""
    try:
        return json.loads(row[0]).get("place_id") or ""
    except (ValueError, AttributeError):
        return ""


def enrich(ctx: Context, limit: int) -> EnrichReport:
    settings = ctx.settings
    report = EnrichReport(zoominfo_skipped=not settings.zoominfo_enabled)
    zoominfo = (
        ZoomInfoClient(ctx.http, settings.zoominfo_username, settings.zoominfo_password)
        if settings.zoominfo_enabled
        else None
    )
    places = BudgetedHttp(ctx.http, settings.places_max_calls, label="google places")
    for row in ctx.store.needs_enrichment(limit, hide_kinds=ctx.profile.sorter_hide):
        key = row["dedupe_key"]
        report.attempted += 1
        updates: dict[str, str] = {}
        try:
            company_name = row["company_name"]
            place_id = _place_id_from_raw(ctx.store, key)
            if settings.places_enabled and not company_name and row["address"]:
                lat, lon = _coords_from_raw(ctx.store, key)
                if lat and lon:
                    hits = places_details.find_near(
                        places, settings.google_places_api_key, lat, lon, row["address"]
                    )
                else:
                    hits = places_details.find_at_address(
                        places,
                        settings.google_places_api_key,
                        row["address"],
                        row["city"],
                        row["state"],
                    )
                if len(hits) == 1:
                    company_name = hits[0].name
                    place_id = hits[0].place_id
                    updates["company_name"] = company_name
                    updates["norm_name"] = normalize_name(company_name)
                    report.companies_found += 1
                elif hits:
                    names = "; ".join(h.name for h in hits)
                    updates["description"] = f"Places lists here: {names} | {row['description']}"[
                        :500
                    ]
            if settings.places_enabled and not row["phone"]:
                if not place_id and company_name:
                    where = row["address"] or ", ".join(x for x in (row["city"], row["state"]) if x)
                    hit = places_details.find_by_name(
                        places, settings.google_places_api_key, company_name, where
                    )
                    if hit and hit.address and not _same_place(ctx, row, hit.address):
                        hit = None  # same name, different town: an existing location, not this one
                    if hit:
                        place_id = hit.place_id
                        if not row["address"] and hit.address:
                            updates["address"] = hit.address
                            if not row["zip"]:
                                updates["zip"] = extract_zip(hit.address)
                contact = places_details.fetch_contact(
                    places, settings.google_places_api_key, place_id
                )
                if contact.phone:
                    updates["phone"] = contact.phone
                    report.phones_found += 1
                if contact.website:
                    updates["website"] = contact.website
            website = updates.get("website") or row["website"]
            if zoominfo and company_name and not row["contact_email"]:
                person = zoominfo.best_contact(company_name, website, ctx.profile.contact_titles)
                if person and (person.email or person.name):
                    updates["contact_name"] = person.name
                    updates["contact_title"] = person.title
                    updates["contact_email"] = person.email
                    if person.phone and not (updates.get("phone") or row["phone"]):
                        updates["phone"] = person.phone
                    if person.email:
                        report.contacts_found += 1
        except BudgetExhausted as exc:
            # Leave this lead un-enriched so the next run picks it up first.
            log.warning("%s; stopping enrich", exc)
            report.attempted -= 1
            report.stopped_at_budget = True
            break
        except Exception as exc:
            log.exception("enrich %s failed", key)
            report.errors[key] = f"{type(exc).__name__}: {exc}"
            continue
        if updates:
            ctx.store.update_fields(key, **updates)
        ctx.store.mark_enriched(key)
        _rescore(ctx, key)
    report.places_calls = places.calls
    return report
