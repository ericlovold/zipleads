"""Orchestration: ingest -> territory filter -> store -> score; enrich."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from zipleads.config import Profile, Settings, Territory
from zipleads.enrich import places_details
from zipleads.enrich.zoominfo import ZoomInfoClient
from zipleads.http import Http
from zipleads.models import Lead
from zipleads.score import score_lead
from zipleads.sources import arcgis_permits, google_news, places_future
from zipleads.store import Store
from zipleads.territory import TerritoryMatcher

log = logging.getLogger("zipleads")

BUILTIN_SOURCES = ("permits", "news", "places")


@dataclass
class Context:
    settings: Settings
    territory: Territory
    profile: Profile
    http: Http
    store: Store

    def __post_init__(self) -> None:
        self.matcher = TerritoryMatcher(self.territory)


@dataclass
class IngestReport:
    fetched: dict[str, int] = field(default_factory=dict)
    kept: int = 0
    created: int = 0
    merged: int = 0
    dropped_out_of_territory: int = 0
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
    )
    ctx.store.set_score(key, score)
    return score


def _absorb(ctx: Context, leads: list[Lead], report: IngestReport) -> None:
    for lead in leads:
        if not in_territory(lead, ctx.matcher):
            report.dropped_out_of_territory += 1
            continue
        key, created = ctx.store.upsert(lead)
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
        if name == "news":
            leads = google_news.fetch_news(
                ctx.http, ctx.matcher, ctx.profile.news_terms, ctx.settings.ingest_days
            )
        elif name == "places":
            if not ctx.settings.places_enabled:
                log.info("places: GOOGLE_PLACES_API_KEY not set, skipping")
                return
            leads = places_future.fetch_future_openings(
                ctx.http,
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
    contacts_found: int = 0
    zoominfo_skipped: bool = False
    errors: dict[str, str] = field(default_factory=dict)


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
    for row in ctx.store.needs_enrichment(limit):
        key = row["dedupe_key"]
        report.attempted += 1
        updates: dict[str, str] = {}
        try:
            if settings.places_enabled and not row["phone"]:
                place_id = _place_id_from_raw(ctx.store, key) or places_details.find_place_id(
                    ctx.http, settings.google_places_api_key, row["company_name"], row["address"]
                )
                contact = places_details.fetch_contact(
                    ctx.http, settings.google_places_api_key, place_id
                )
                if contact.phone:
                    updates["phone"] = contact.phone
                    report.phones_found += 1
                if contact.website:
                    updates["website"] = contact.website
            website = updates.get("website") or row["website"]
            if zoominfo and not row["contact_email"]:
                person = zoominfo.best_contact(
                    row["company_name"], website, ctx.profile.contact_titles
                )
                if person and (person.email or person.name):
                    updates["contact_name"] = person.name
                    updates["contact_title"] = person.title
                    updates["contact_email"] = person.email
                    if person.phone and not (updates.get("phone") or row["phone"]):
                        updates["phone"] = person.phone
                    if person.email:
                        report.contacts_found += 1
        except Exception as exc:
            log.exception("enrich %s failed", key)
            report.errors[key] = f"{type(exc).__name__}: {exc}"
            continue
        if updates:
            ctx.store.update_fields(key, **updates)
        ctx.store.mark_enriched(key)
        _rescore(ctx, key)
    return report
