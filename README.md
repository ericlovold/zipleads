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
| ingest | Google Places text search | `businessStatus = FUTURE_OPENING` | Places key |
| enrich | Google Places details | business phone and website | Places key |
| enrich | ZoomInfo search + enrich | decision-maker name, title, email, direct phone | ZoomInfo creds and `ZOOMINFO_ENABLE=true` |

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # fill in keys; never commit .env
```

Permit layer field names in the shipped territory came from documentation,
not a live call. Confirm them before the first run and edit the territory
file if they differ:

```bash
python -m zipleads probe mpls_permits
```

## Run

```bash
python -m zipleads ingest                    # permits + news, plus places when a key is set
python -m zipleads enrich --limit 25         # phone/website via Places; ZoomInfo only if enabled
python -m zipleads export --out out/leads.csv
python -m zipleads mark-submitted <dedupe_key> ...   # after handing leads off
python -m zipleads stats
```

`--territory` and `--profile` override the `.env` defaults per run, so one
checkout can serve several territories and industries:

```bash
python -m zipleads --territory territories/austin.toml --profile profiles/default.toml ingest
```

Cron on a Mac Mini, weekdays at 6:15:

```
15 6 * * 1-5  cd /path/to/zipleads && .venv/bin/python -m zipleads ingest && .venv/bin/python -m zipleads export --out out/leads.csv
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
contact. Headlines the parser could not name a company from are penalized
but kept, so a human can still see them. Weights live in the profile.

## Tests

```bash
pytest
ruff check . && ruff format --check .
```

Every parser is tested against a recorded fixture in `tests/fixtures`. Live
endpoints were not reachable from the environment where this was written,
so the first live run is the real integration test.

## Unverified against live endpoints

- Permit layer field names and date field for Minneapolis and Saint Paul.
- Whether Places text search reliably returns `FUTURE_OPENING` places for an
  "opening soon" query. The status value itself is documented.
- ZoomInfo request and response field names. Endpoint paths follow the
  public reference; the JSON shapes in `tests/fixtures/zoominfo_*.json` are
  the assumption.
