"""SQLite store. One row per business-in-place, merged across sources."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from zipleads.models import Lead
from zipleads.normalize import dedupe_key, normalize_address, normalize_name

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    dedupe_key    TEXT PRIMARY KEY,
    norm_name     TEXT NOT NULL,
    company_name  TEXT NOT NULL,
    applicant     TEXT NOT NULL DEFAULT '',
    address       TEXT NOT NULL DEFAULT '',
    city          TEXT NOT NULL DEFAULT '',
    state         TEXT NOT NULL DEFAULT 'MN',
    zip           TEXT NOT NULL DEFAULT '',
    phone         TEXT NOT NULL DEFAULT '',
    website       TEXT NOT NULL DEFAULT '',
    contact_name  TEXT NOT NULL DEFAULT '',
    contact_title TEXT NOT NULL DEFAULT '',
    contact_email TEXT NOT NULL DEFAULT '',
    sources       TEXT NOT NULL DEFAULT '',
    signals       TEXT NOT NULL DEFAULT '',
    signal_date   TEXT NOT NULL DEFAULT '',
    evidence_url  TEXT NOT NULL DEFAULT '',
    description   TEXT NOT NULL DEFAULT '',
    value         REAL NOT NULL DEFAULT 0,
    score         INTEGER NOT NULL DEFAULT 0,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    enriched_at   TEXT NOT NULL DEFAULT '',
    submitted_at  TEXT NOT NULL DEFAULT '',
    submitted_ref TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS sightings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key   TEXT NOT NULL,
    norm_name    TEXT NOT NULL,
    norm_address TEXT NOT NULL,
    source       TEXT NOT NULL,
    signal       TEXT NOT NULL,
    seen_at      TEXT NOT NULL,
    raw          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sightings_name ON sightings(norm_name);
CREATE TABLE IF NOT EXISTS geocode_cache (
    query   TEXT PRIMARY KEY,
    zip     TEXT NOT NULL,
    cached  TEXT NOT NULL
);
"""

_MERGE_FIELDS = (
    "applicant",
    "address",
    "city",
    "state",
    "zip",
    "phone",
    "website",
    "contact_name",
    "contact_title",
    "contact_email",
    "signal_date",
    "evidence_url",
    "description",
)


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _merge_list(existing: str, item: str) -> str:
    items = [x for x in existing.split(",") if x]
    if item and item not in items:
        items.append(item)
    return ",".join(items)


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()

    # Columns added after the first release, with their DDL. A database created by an
    # older version gets them on open; CREATE TABLE IF NOT EXISTS alone would not.
    _ADDED_COLUMNS = {
        "applicant": "TEXT NOT NULL DEFAULT ''",
        "value": "REAL NOT NULL DEFAULT 0",
        "submitted_ref": "TEXT NOT NULL DEFAULT ''",
    }

    def _migrate(self) -> None:
        have = {row[1] for row in self.conn.execute("PRAGMA table_info(leads)")}
        for name, ddl in self._ADDED_COLUMNS.items():
            if name not in have:
                self.conn.execute(f"ALTER TABLE leads ADD COLUMN {name} {ddl}")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def upsert(self, lead: Lead) -> tuple[str, bool]:
        """Insert or merge. Returns (dedupe_key, created)."""
        key = dedupe_key(lead.company_name, lead.zip, lead.city, lead.address)
        norm_name = normalize_name(lead.company_name)
        now = _now()
        cur = self.conn.cursor()
        row = cur.execute("SELECT * FROM leads WHERE dedupe_key = ?", (key,)).fetchone()
        if row is None:
            cur.execute(
                """INSERT INTO leads (
                       dedupe_key, norm_name, company_name, applicant, address, city, state, zip,
                       phone, website, contact_name, contact_title, contact_email,
                       sources, signals, signal_date, evidence_url, description, value,
                       first_seen, last_seen)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    key,
                    norm_name,
                    lead.company_name,
                    lead.applicant,
                    lead.address,
                    lead.city,
                    lead.state,
                    lead.zip,
                    lead.phone,
                    lead.website,
                    lead.contact_name,
                    lead.contact_title,
                    lead.contact_email,
                    lead.source,
                    lead.signal,
                    lead.signal_date,
                    lead.evidence_url,
                    lead.description,
                    lead.value,
                    now,
                    now,
                ),
            )
            created = True
        else:
            updates = {"last_seen": now}
            updates["sources"] = _merge_list(row["sources"], lead.source)
            updates["signals"] = _merge_list(row["signals"], lead.signal)
            for f in _MERGE_FIELDS:
                incoming = getattr(lead, f)
                if incoming and not row[f]:
                    updates[f] = incoming
            if lead.value > row["value"]:
                updates["value"] = lead.value
            sets = ", ".join(f"{k} = ?" for k in updates)
            cur.execute(f"UPDATE leads SET {sets} WHERE dedupe_key = ?", (*updates.values(), key))
            created = False
        cur.execute(
            """INSERT INTO sightings (
                   dedupe_key, norm_name, norm_address, source, signal, seen_at, raw)
               VALUES (?,?,?,?,?,?,?)""",
            (
                key,
                norm_name,
                normalize_address(lead.address),
                lead.source,
                lead.signal,
                now,
                lead.raw_json(),
            ),
        )
        self.conn.commit()
        return key, created

    def distinct_addresses(self, norm_name: str) -> int:
        """How many distinct non-empty addresses this company has been seen at."""
        if not norm_name:
            return 0
        row = self.conn.execute(
            "SELECT COUNT(DISTINCT norm_address) FROM sightings "
            "WHERE norm_name = ? AND norm_address != ''",
            (norm_name,),
        ).fetchone()
        return int(row[0])

    def cached_zip(self, query: str) -> str | None:
        row = self.conn.execute(
            "SELECT zip FROM geocode_cache WHERE query = ?", (query,)
        ).fetchone()
        return None if row is None else row[0]

    def cache_zip(self, query: str, zip_code: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO geocode_cache (query, zip, cached) VALUES (?,?,?)",
            (query, zip_code, _now()),
        )
        self.conn.commit()

    def add_signal(self, key: str, signal: str) -> None:
        row = self.get(key)
        if row is None:
            return
        self.update_fields(key, signals=_merge_list(row["signals"], signal))

    def set_score(self, key: str, score: int) -> None:
        self.conn.execute("UPDATE leads SET score = ? WHERE dedupe_key = ?", (score, key))
        self.conn.commit()

    def update_fields(self, key: str, **fields: str) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE leads SET {sets} WHERE dedupe_key = ?", (*fields.values(), key))
        self.conn.commit()

    def mark_enriched(self, key: str) -> None:
        self.update_fields(key, enriched_at=_now())

    def mark_submitted(self, key: str, ref: str = "") -> bool:
        """Record the hand-off. `ref` is the receiving system's id (e.g. portal referral id)."""
        cur = self.conn.execute(
            "UPDATE leads SET submitted_at = ?, submitted_ref = ? "
            "WHERE dedupe_key = ? AND submitted_at = ''",
            (_now(), ref, key),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def get(self, key: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM leads WHERE dedupe_key = ?", (key,)).fetchone()

    def all_leads(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM leads ORDER BY score DESC, first_seen ASC"
        ).fetchall()

    def unsubmitted(self, limit: int | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM leads WHERE submitted_at = '' ORDER BY score DESC, first_seen ASC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return self.conn.execute(sql).fetchall()

    def reset_enrichment(self) -> int:
        """Allow another enrich pass over unsubmitted leads still missing a phone."""
        cur = self.conn.execute(
            "UPDATE leads SET enriched_at = '' WHERE submitted_at = '' AND phone = ''"
        )
        self.conn.commit()
        return cur.rowcount

    def needs_enrichment(self, limit: int) -> list[sqlite3.Row]:
        """Unsubmitted leads missing a phone or a contact, never enriched, best first."""
        return self.conn.execute(
            """SELECT * FROM leads
               WHERE submitted_at = '' AND enriched_at = ''
                 AND (phone = '' OR contact_email = '')
               ORDER BY score DESC, first_seen ASC LIMIT ?""",
            (int(limit),),
        ).fetchall()
