from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from zipleads import mail, pipeline, sheets
from zipleads.config import load_profile, load_settings, load_territory
from zipleads.export import write_csv
from zipleads.http import RequestsHttp
from zipleads.sources import arcgis_permits, licenses
from zipleads.store import Store


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="zipleads", description="Local business leads by zip code")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--territory", help="territory TOML (default: ZIPLEADS_TERRITORY)")
    p.add_argument("--profile", help="profile TOML (default: ZIPLEADS_PROFILE)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("ingest", help="pull sources, filter to territory, store and score")
    s.add_argument(
        "--sources",
        default=",".join(pipeline.DEFAULT_SOURCES),
        help=f"comma list from {','.join(pipeline.BUILTIN_SOURCES)} "
        f"(default {','.join(pipeline.DEFAULT_SOURCES)})",
    )

    e = sub.add_parser("enrich", help="attach phone/website (Places) and contact (ZoomInfo)")
    e.add_argument("--limit", type=int, default=25, help="max leads to enrich this run")
    e.add_argument(
        "--redo", action="store_true", help="retry leads already tried that have no phone"
    )

    x = sub.add_parser("export", help="write unsubmitted leads to CSV, best first")
    x.add_argument("--out", default="out/leads.csv")
    x.add_argument("--limit", type=int, default=None)
    x.add_argument("--include-unparsed", action="store_true", help="also export headline-only news")

    m = sub.add_parser("mark-submitted", help="record that leads were handed off")
    m.add_argument("keys", nargs="+")
    m.add_argument("--ref", default="", help="receiving system's id, e.g. portal referral id")

    sd = sub.add_parser("send", help="export unsubmitted leads and email the CSV to MAIL_TO")
    sd.add_argument("--out", default="out/leads.csv")
    sd.add_argument("--limit", type=int, default=None)
    sd.add_argument("--include-unparsed", action="store_true", help="also send headline-only news")
    sd.add_argument("--dry-run", action="store_true", help="build the email, print it, do not send")

    pr = sub.add_parser("probe", help="print a permit or license layer's field names")
    pr.add_argument("layer", help="permit layer or license feed name from the territory file")
    pr.add_argument("--sample", type=int, default=0, help="also print N most recent raw records")

    sub.add_parser("stats", help="counts by source and submission state")

    sh = sub.add_parser("sheet", help="Google Sheet feedback loop")
    sh.add_argument("action", choices=["push", "pull"], help="push new leads / pull statuses back")

    rv = sub.add_parser(
        "review-permits",
        help="print recent permits with the sorter's kind and reasons (no storage)",
    )
    rv.add_argument("--days", type=int, default=None, help="look-back window (default INGEST_DAYS)")
    rv.add_argument("--kind", default="", help="only show this kind")

    rl = sub.add_parser(
        "review-licenses",
        help="print recent license applications with kind and reasons (no storage)",
    )
    rl.add_argument("--days", type=int, default=None, help="look-back window (default INGEST_DAYS)")

    fl = sub.add_parser("find-layers", help="list ArcGIS services whose name matches a word")
    fl.add_argument(
        "services_url", help="e.g. https://services.arcgis.com/<org>/arcgis/rest/services"
    )
    fl.add_argument("word", nargs="?", default="", help="e.g. license (case-insensitive)")

    pp = sub.add_parser("probe-places", help="run the profile's Places queries for one area")
    pp.add_argument("area", help='e.g. "Eagan, MN" or a zip')

    t = sub.add_parser("new-territory", help="write a territory TOML skeleton from zips")
    t.add_argument("--name", required=True)
    t.add_argument("--state", required=True, help="two-letter state code")
    t.add_argument("--zips", required=True, help="comma-separated zips")
    t.add_argument("--out", required=True, help="path to write, e.g. territories/austin.toml")
    return p


def _new_territory(name: str, state: str, zips: str, out: str) -> int:
    zip_list = sorted({z.strip()[:5] for z in zips.split(",") if z.strip()})
    body = [
        f'name = "{name}"',
        f'state = "{state.upper()}"',
        "",
        "# Move zips under [cities] as you learn their city labels; labels drive news queries.",
        "zips = [" + ", ".join(f'"{z}"' for z in zip_list) + "]",
        "",
        "[cities]",
        '# "Austin" = ["78701", "78702"]',
        "",
        "# Add this area's ArcGIS permit layers. Confirm field names with `zipleads probe`.",
        "# [[permit_layers]]",
        '# name = "austin_permits"',
        '# city = "Austin"',
        '# url = "https://services.arcgis.com/.../FeatureServer/0"',
        "# [permit_layers.fields]",
        '# applicant = "APPLICANT"',
        '# address = "ADDRESS"',
        '# permit_type = "PERMIT_TYPE"',
        '# work_type = "WORK_TYPE"',
        '# description = "DESCRIPTION"',
        '# date = "ISSUE_DATE"',
        '# value = "VALUATION"',
        "",
        "# License applications: a Legistar council feed, or an ArcGIS license layer.",
        "# Find layers with `zipleads find-layers <services url> license`.",
        "# [[license_feeds]]",
        '# name = "austin_licenses"',
        '# type = "legistar"   # or "arcgis" with url and [license_feeds.fields]',
        '# client = "austintexas"',
        '# city = "Austin"',
        "",
    ]
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(body), encoding="utf-8")
    print(f"wrote {path} with {len(zip_list)} zips")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if args.cmd == "new-territory":
        return _new_territory(args.name, args.state, args.zips, args.out)

    settings = load_settings()
    territory = load_territory(args.territory or settings.territory_path)
    profile = load_profile(args.profile or settings.profile_path)
    http = RequestsHttp()

    if args.cmd == "find-layers":
        base = args.services_url.rstrip("/")
        doc = http.get_json(base, params={"f": "json"})
        word = args.word.lower()
        hits = [sv for sv in doc.get("services", []) if word in str(sv.get("name", "")).lower()]
        for sv in hits:
            url = sv.get("url") or f"{base}/{sv.get('name')}/{sv.get('type')}"
            print(f"{sv.get('name', ''):45} {sv.get('type', ''):14} {url}")
        print(f"{len(hits)} of {len(doc.get('services', []))} services match {args.word!r}")
        return 0

    if args.cmd == "probe":
        feed = next((fd for fd in territory.license_feeds if fd.name == args.layer), None)
        if feed is not None and feed.type == "legistar":
            if not feed.client:
                print(f"{args.layer}: no client in territory", file=sys.stderr)
                return 2
            matters = licenses.legistar_matters(http, feed, settings.ingest_days)
            found = licenses.parse_matters(matters, feed)
            print(f"{len(matters)} matters in {settings.ingest_days} days, {len(found)} leads")
            for m in matters[: args.sample]:
                print("---")
                print(json.dumps(m, indent=1, default=str))
            return 0
        layer = feed or next((ly for ly in territory.permit_layers if ly.name == args.layer), None)
        if layer is None or not layer.url:
            print(f"{args.layer}: not in territory or no url", file=sys.stderr)
            return 2
        for f in arcgis_permits.layer_fields(http, layer):
            print(f"{f.get('name'):40} {f.get('type', '')}")
        if args.sample:
            for attrs in arcgis_permits.sample_features(http, layer, args.sample):
                print("---")
                print(json.dumps(attrs, indent=1, default=str))
        return 0

    if args.cmd == "review-permits":
        from collections import Counter

        days = args.days or settings.ingest_days
        counts: Counter[str] = Counter()
        for layer in territory.permit_layers:
            if not layer.url:
                continue
            leads = arcgis_permits.fetch_permits(http, layer, profile, days)
            for ld in sorted(leads, key=lambda x: (x.kind, x.address)):
                counts[ld.kind] += 1
                if args.kind and ld.kind != args.kind:
                    continue
                conf = ld.raw.get("kind_confidence", "")
                reasons = "; ".join(ld.raw.get("kind_reasons", []))
                print(f"{ld.kind:16} {conf:4} | {ld.address[:30]:30} | {reasons[:70]}")
                print(f"{'':21} | {ld.description[:110]}")
        print(f"\n{sum(counts.values())} permits in {days} days: {dict(counts)}")
        print(f"profile drops {list(profile.sorter_drop)}, hides {list(profile.sorter_hide)}")
        return 0

    if args.cmd == "review-licenses":
        from collections import Counter

        days = args.days or settings.ingest_days
        counts: Counter[str] = Counter()
        for feed in territory.license_feeds:
            if not feed.configured:
                print(f"{feed.name}: not configured, skipped")
                continue
            for ld in licenses.fetch_licenses(http, feed, days):
                counts[ld.kind] += 1
                reasons = "; ".join(ld.raw.get("kind_reasons", []))
                name, addr = ld.company_name[:30], ld.address[:30]
                print(f"{ld.kind:16} | {name:30} | {addr:30} | {reasons}")
                if ld.applicant:
                    print(f"{'':16} | owner: {ld.applicant}")
        print(f"\n{sum(counts.values())} license leads in {days} days: {dict(counts)}")
        print(f"profile drops {list(profile.sorter_drop)}, hides {list(profile.sorter_hide)}")
        return 0

    if args.cmd == "probe-places":
        if not settings.places_enabled:
            print("GOOGLE_PLACES_API_KEY not set", file=sys.stderr)
            return 2
        from collections import Counter

        from zipleads.sources import places_future

        headers = {
            "X-Goog-Api-Key": settings.google_places_api_key,
            "X-Goog-FieldMask": places_future.FIELD_MASK,
        }
        for query in profile.places_queries:
            page = http.post_json(
                places_future.SEARCH_URL,
                {"textQuery": f"{query} {args.area}", "pageSize": 20},
                headers=headers,
            )
            places = page.get("places", [])
            statuses = Counter(p.get("businessStatus", "?") for p in places)
            print(
                f'"{query} {args.area}": {len(places)} results, statuses={dict(statuses)}, '
                f"nextPage={'yes' if page.get('nextPageToken') else 'no'}"
            )
            for p in places:
                if p.get("businessStatus") == places_future.FUTURE_OPENING:
                    name = (p.get("displayName") or {}).get("text", "")
                    print(f"   FUTURE_OPENING  {name}  |  {p.get('formattedAddress', '')}")
        return 0

    store = Store(settings.db_path)
    ctx = pipeline.Context(settings, territory, profile, http, store)

    def sheet_client() -> sheets.SheetsClient | None:
        if not settings.sheet_enabled:
            return None
        token = sheets.service_account_token_provider(settings.service_account_json)
        return sheets.SheetsClient(http, settings.sheet_id, token, settings.sheet_tab)

    try:
        if args.cmd == "sheet":
            client = sheet_client()
            if client is None:
                print(
                    "GOOGLE_SHEET_ID and GOOGLE_SERVICE_ACCOUNT_JSON must be set", file=sys.stderr
                )
                return 2
            if args.action == "push":
                n = sheets.push(client, store.unsubmitted(hide_kinds=profile.sorter_hide))
                print(f"pushed {n} new leads to the sheet")
            else:
                counts = sheets.pull(client, store)
                print(f"pulled statuses: {counts}")
            return 0
        if args.cmd == "ingest":
            sources = tuple(s.strip() for s in args.sources.split(",") if s.strip())
            r = pipeline.ingest(ctx, sources)
            print(
                f"fetched={r.fetched} kept={r.kept} created={r.created} merged={r.merged} "
                f"dropped_out_of_territory={r.dropped_out_of_territory} "
                f"dropped_by_segment={r.dropped_by_segment} dropped_by_kind={r.dropped_by_kind}"
            )
            if r.errors:
                print(f"errors={r.errors}", file=sys.stderr)
                return 1
            return 0
        if args.cmd == "enrich":
            if args.redo:
                print(f"reset {store.reset_enrichment()} leads for another pass")
            r = pipeline.enrich(ctx, args.limit)
            print(
                f"attempted={r.attempted} phones_found={r.phones_found} "
                f"companies_found={r.companies_found} "
                f"contacts_found={r.contacts_found} zoominfo_skipped={r.zoominfo_skipped} "
                f"places_calls={r.places_calls}"
            )
            if r.stopped_at_budget:
                print(
                    "stopped at PLACES_MAX_CALLS_PER_RUN; remaining leads wait for the next run",
                    file=sys.stderr,
                )
            if r.errors:
                print(f"errors={r.errors}", file=sys.stderr)
                return 1
            return 0
        if args.cmd == "export":
            rows = store.unsubmitted(
                args.limit, include_unparsed=args.include_unparsed, hide_kinds=profile.sorter_hide
            )
            n = write_csv(rows, profile.export_columns, args.out)
            print(f"wrote {n} leads to {args.out}")
            return 0
        if args.cmd == "mark-submitted":
            missed = [k for k in args.keys if not store.mark_submitted(k, args.ref)]
            if missed:
                print(f"not found or already submitted: {missed}", file=sys.stderr)
                return 1
            print(f"marked {len(args.keys)} submitted")
            return 0
        if args.cmd == "send":
            rows = store.unsubmitted(
                args.limit, include_unparsed=args.include_unparsed, hide_kinds=profile.sorter_hide
            )
            write_csv(rows, profile.export_columns, args.out)
            msg = mail.build_message(settings, rows, Path(args.out), territory.name)
            if args.dry_run:
                print(msg.as_string()[:4000])
                return 0
            if not settings.mail_enabled:
                print("SMTP_HOST, MAIL_FROM and MAIL_TO must be set to send", file=sys.stderr)
                return 2
            try:
                mail.send(settings, msg)
            except mail.MailError as exc:
                print(f"send failed: {exc}", file=sys.stderr)
                return 1
            print(f"sent {len(rows)} leads to {', '.join(settings.mail_to)}")
            client = sheet_client()
            if client is not None:
                try:
                    print(f"pushed {sheets.push(client, rows)} new leads to the sheet")
                except Exception as exc:  # the email already went; do not fail the run
                    print(f"sheet push failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 0
        if args.cmd == "stats":
            rows = store.all_leads()
            submitted = sum(1 for r in rows if r["submitted_at"])
            by_source: dict[str, int] = {}
            for r in rows:
                for s in r["sources"].split(","):
                    if s:
                        by_source[s] = by_source.get(s, 0) + 1
            kinds: dict[str, int] = {}
            for r in rows:
                if r["kind"]:
                    kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
            print(f"permit kinds stored: {kinds}")
            statuses: dict[str, int] = {}
            for r in rows:
                if r["status"]:
                    statuses[r["status"]] = statuses.get(r["status"], 0) + 1
            print(
                f"leads={len(rows)} submitted={submitted} by_source={by_source} statuses={statuses}"
            )
            values = sorted(r["value"] for r in rows if r["value"])
            if values:
                pct = lambda q: values[min(len(values) - 1, int(q * len(values)))]  # noqa: E731
                print(
                    f"value (n={len(values)}): p50={pct(0.5):.0f} p75={pct(0.75):.0f} "
                    f"p90={pct(0.9):.0f} max={values[-1]:.0f}"
                )
            return 0
    finally:
        store.close()
    return 0
