from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from zipleads import mail, pipeline
from zipleads.config import load_profile, load_settings, load_territory
from zipleads.export import write_csv
from zipleads.http import RequestsHttp
from zipleads.sources import arcgis_permits
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
        default=",".join(pipeline.BUILTIN_SOURCES),
        help=f"comma list from {','.join(pipeline.BUILTIN_SOURCES)}",
    )

    e = sub.add_parser("enrich", help="attach phone/website (Places) and contact (ZoomInfo)")
    e.add_argument("--limit", type=int, default=25, help="max leads to enrich this run")

    x = sub.add_parser("export", help="write unsubmitted leads to CSV, best first")
    x.add_argument("--out", default="out/leads.csv")
    x.add_argument("--limit", type=int, default=None)

    m = sub.add_parser("mark-submitted", help="record that leads were handed off")
    m.add_argument("keys", nargs="+")
    m.add_argument("--ref", default="", help="receiving system's id, e.g. portal referral id")

    sd = sub.add_parser("send", help="export unsubmitted leads and email the CSV to MAIL_TO")
    sd.add_argument("--out", default="out/leads.csv")
    sd.add_argument("--limit", type=int, default=None)
    sd.add_argument("--dry-run", action="store_true", help="build the email, print it, do not send")

    pr = sub.add_parser("probe", help="print a permit layer's field names")
    pr.add_argument("layer", help="permit layer name from the territory file")

    sub.add_parser("stats", help="counts by source and submission state")

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

    if args.cmd == "probe":
        layer = next((ly for ly in territory.permit_layers if ly.name == args.layer), None)
        if layer is None or not layer.url:
            print(f"{args.layer}: not in territory or no url", file=sys.stderr)
            return 2
        for f in arcgis_permits.layer_fields(http, layer):
            print(f"{f.get('name'):40} {f.get('type', '')}")
        return 0

    store = Store(settings.db_path)
    ctx = pipeline.Context(settings, territory, profile, http, store)
    try:
        if args.cmd == "ingest":
            sources = tuple(s.strip() for s in args.sources.split(",") if s.strip())
            r = pipeline.ingest(ctx, sources)
            print(
                f"fetched={r.fetched} kept={r.kept} created={r.created} merged={r.merged} "
                f"dropped_out_of_territory={r.dropped_out_of_territory} "
                f"dropped_by_segment={r.dropped_by_segment}"
            )
            if r.errors:
                print(f"errors={r.errors}", file=sys.stderr)
                return 1
            return 0
        if args.cmd == "enrich":
            r = pipeline.enrich(ctx, args.limit)
            print(
                f"attempted={r.attempted} phones_found={r.phones_found} "
                f"contacts_found={r.contacts_found} zoominfo_skipped={r.zoominfo_skipped}"
            )
            if r.errors:
                print(f"errors={r.errors}", file=sys.stderr)
                return 1
            return 0
        if args.cmd == "export":
            n = write_csv(store.unsubmitted(args.limit), profile.export_columns, args.out)
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
            rows = store.unsubmitted(args.limit)
            write_csv(rows, profile.export_columns, args.out)
            msg = mail.build_message(settings, rows, Path(args.out), territory.name)
            if args.dry_run:
                print(msg.as_string()[:4000])
                return 0
            if not settings.mail_enabled:
                print("SMTP_HOST, MAIL_FROM and MAIL_TO must be set to send", file=sys.stderr)
                return 2
            mail.send(settings, msg)
            print(f"sent {len(rows)} leads to {', '.join(settings.mail_to)}")
            return 0
        if args.cmd == "stats":
            rows = store.all_leads()
            submitted = sum(1 for r in rows if r["submitted_at"])
            by_source: dict[str, int] = {}
            for r in rows:
                for s in r["sources"].split(","):
                    if s:
                        by_source[s] = by_source.get(s, 0) + 1
            print(f"leads={len(rows)} submitted={submitted} by_source={by_source}")
            return 0
    finally:
        store.close()
    return 0
