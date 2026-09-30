# zipleads

Find businesses that are about to open, move, or build out a space, inside
any set of zip codes, for any industry. Signals come from public permit
data, local news, and Google Places. Leads are deduped across sources,
scored, enriched with a phone and a decision-maker, and exported as CSV.

Two files drive a run:

- **Territory** (where): zips, optional city labels, and that area's permit
  layers. `territories/twin-cities-comcast.toml` ships as the first one.
- **Profile** (what): search terms, permit filters, contact titles, scoring
  weights, export columns. `profiles/telecom-new-business.toml` and
  `profiles/default.toml` ship.

```
sources  ->  territory filter  ->  dedupe + score  ->  enrich  ->  export CSV
```

| Stage | Source | Signal | Needs |
|---|---|---|---|
| ingest | ArcGIS permit layers listed in the territory | permit issued matching profile terms | nothing |
| ingest | Google News RSS, one feed per territory city | "opens", "relocates", "expands" headlines | nothing |
| ingest (off by default) | Google Places text search | `businessStatus = FUTURE_OPENING` | Places key |
| enrich | Google Places details | business phone and website | Places key |
| enrich | ZoomInfo search + enrich | decision-maker name, title, email, direct phone | ZoomInfo creds and `ZOOMINFO_ENABLE=true` |

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # fill in keys; never commit .env
```

Minneapolis permit field names in the shipped territory were confirmed
against the live layer. For any new layer, confirm before the first run and
edit the territory file if they differ:

```bash
python -m zipleads probe mpls_permits
```

## Run

```bash
python -m zipleads ingest                    # permits + news (add --sources permits,news,places to experiment)
python -m zipleads enrich --limit 25         # phone/website via Places; ZoomInfo only if enabled
python -m zipleads export --out out/leads.csv
python -m zipleads send                      # export + email the CSV to MAIL_TO (--dry-run to preview)
python -m zipleads mark-submitted <dedupe_key> --ref <portal id>   # after handing leads off
python -m zipleads stats
```

`send` is the daily hand-off: a ranked summary in the body, the full CSV
attached, one email to everyone in `MAIL_TO`. `mark-submitted --ref` stores
the receiving system's id so its status export can be joined back to ours.

`--territory` and `--profile` override the `.env` defaults per run, so one
checkout can serve several territories and industries:

```bash
python -m zipleads --territory territories/austin.toml --profile profiles/default.toml ingest
```

Cron on a Mac Mini, weekdays at 6:15:

```
15 6 * * 1-5  cd /path/to/zipleads && .venv/bin/python -m zipleads ingest && .venv/bin/python -m zipleads enrich --limit 25 && .venv/bin/python -m zipleads send
```

## New territory

```bash
python -m zipleads new-territory --name "Austin" --state TX --zips 78701,78702,78703 --out territories/austin.toml
```

Then fill in city labels (they drive news queries) and any ArcGIS permit
layers for that area. Most large US cities publish permits on an ArcGIS Hub;
the layer URL ends in `/FeatureServer/0`.

## Cost controls

- **Places** text search bills at the Pro SKU. Calls per ingest run equal
  search areas times profile queries times pages, one page by default. The
  shipped territory has 85 city labels and 2 queries, so about 170 calls per
  run. Phone and website (Enterprise SKU) are fetched only in `enrich`, only
  for leads already inside the territory, and skipped when the place id is
  already known.
- **ZoomInfo** search does not consume enrichment credits; one enrich call
  per lead does. Nothing runs without `ZOOMINFO_ENABLE=true`, and `--limit`
  caps each run. Tests prove no ZoomInfo call happens when disabled.

## Scoring

A rough priority, not a probability. Highest source weight, plus a bonus
when several sources agree, plus a multi-site bonus when the same company
appears at two or more addresses, plus bonuses for an attached phone and
contact. Permit valuation adds a tiered bonus as a proxy for build-out size.
Headlines the parser could not name a company from are penalized but kept,
so a human can still see them. Weights live in the profile.

## Segment scrub

The profile's `[segments]` table is a keyword pass over company name,
description, and Places primary type:

- `exclude` drops the lead before storage (education, for the telecom profile).
- `deprioritize` keeps it, tags it `flag:deprioritized:<term>`, and applies a
  penalty. The tag shows in the email body so a human can decide (dental,
  hospitality, franchise).
- `boost` tags `flag:boost:<term>` and adds a bonus for segments that tend to
  buy bigger circuits (offices, clinics, warehouses, professional services).

It is a keyword match, so it will miss some and mis-flag others. Treat it as a
first pass, not a filter you trust blindly.

## Tests

```bash
pytest
ruff check . && ruff format --check .
```

Every parser is tested against a recorded fixture in `tests/fixtures`. Live
endpoints were not reachable from the environment where this was written,
so the first live run is the real integration test.

## Verified against live endpoints

- Minneapolis CCS Permits: field names, same-day freshness, `value` is null,
  `totalFees` is populated and used as the size proxy.
- Places text search does not surface `FUTURE_OPENING` listings (Eagan MN,
  2026-09-30, 0 of 40). Places is enrichment-only by default.

## Unverified against live endpoints

- Saint Paul permit layer URL and field names (Minneapolis is confirmed).
- ZoomInfo request and response field names. Endpoint paths follow the
  public reference; the JSON shapes in `tests/fixtures/zoominfo_*.json` are
  the assumption.
