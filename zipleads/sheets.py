"""Google Sheets feedback loop.

One tab, one row per lead. The pipeline appends new leads after each daily
email; the rep types a status (submitted, working, sold, lost, junk) and the
portal reference; `sheet pull` reads those back before the next ingest.

Auth is a Google service account (a JSON key file). The sheet is shared
with the service account's email as an editor. Token acquisition is
injected so the Sheets calls can be tested offline.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from pathlib import Path

from zipleads.http import Http
from zipleads.store import Store

log = logging.getLogger("zipleads.sheets")

API = "https://sheets.googleapis.com/v4/spreadsheets"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

HEADER = [
    "key",
    "first_seen",
    "score",
    "company",
    "applicant",
    "address",
    "city",
    "zip",
    "phone",
    "website",
    "signals",
    "summary",
    "evidence",
    "status",
    "portal_ref",
    "notes",
]
STATUS_COL = HEADER.index("status")
REF_COL = HEADER.index("portal_ref")
NOTES_COL = HEADER.index("notes")

# Statuses that mean "the rep has it"; everything after submitted is still submitted to us.
SUBMITTED_STATUSES = {
    "submitted",
    "working",
    "sold",
    "lost",
    "install complete",
    "installed",
    "paid",
}
JUNK_STATUSES = {"junk", "skip", "bad", "ignore", "dup", "duplicate"}


def service_account_token_provider(key_path: str | Path) -> Callable[[], str]:
    """Returns a callable that yields a fresh OAuth token for the service account."""
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    creds = service_account.Credentials.from_service_account_file(str(key_path), scopes=SCOPES)

    def token() -> str:
        if not creds.valid:
            creds.refresh(Request())
        return creds.token

    return token


class SheetsClient:
    def __init__(self, http: Http, sheet_id: str, token: Callable[[], str], tab: str = "Leads"):
        self.http = http
        self.sheet_id = sheet_id
        self.token = token
        self.tab = tab

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token()}"}

    def get_rows(self) -> list[list[str]]:
        data = self.http.get_json(
            f"{API}/{self.sheet_id}/values/{self.tab}!A:P", headers=self._headers()
        )
        return data.get("values", [])

    def put_header(self) -> None:
        self.http.put_json(
            f"{API}/{self.sheet_id}/values/{self.tab}!A1:P1",
            {"range": f"{self.tab}!A1:P1", "majorDimension": "ROWS", "values": [HEADER]},
            params={"valueInputOption": "RAW"},
            headers=self._headers(),
        )

    def append_rows(self, rows: list[list[str]]) -> None:
        if not rows:
            return
        self.http.post_json(
            f"{API}/{self.sheet_id}/values/{self.tab}!A:P:append",
            {"majorDimension": "ROWS", "values": rows},
            params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
            headers=self._headers(),
        )


def _row_for(lead: sqlite3.Row) -> list[str]:
    applicant = lead["applicant"] if "applicant" in lead.keys() else ""
    return [
        lead["dedupe_key"],
        lead["first_seen"][:10],
        str(lead["score"]),
        lead["company_name"],
        applicant,
        lead["address"],
        lead["city"],
        lead["zip"],
        lead["phone"],
        lead["website"],
        lead["signals"],
        (lead["description"] or "")[:300],
        lead["evidence_url"],
        "",
        "",
        "",
    ]


def push(client: SheetsClient, leads: list[sqlite3.Row]) -> int:
    """Append leads not yet in the sheet. Returns how many were added."""
    rows = client.get_rows()
    if not rows:
        client.put_header()
        existing: set[str] = set()
    else:
        existing = {r[0] for r in rows[1:] if r}
    new = [_row_for(ld) for ld in leads if ld["dedupe_key"] not in existing]
    client.append_rows(new)
    return len(new)


def pull(client: SheetsClient, store: Store) -> dict[str, int]:
    """Read status/portal_ref/notes back into the store. Returns counts by action."""
    counts = {"submitted": 0, "junk": 0, "noted": 0, "unknown_status": 0}
    for r in client.get_rows()[1:]:
        if not r or not r[0]:
            continue
        key = r[0]
        status = (r[STATUS_COL] if len(r) > STATUS_COL else "").strip().lower()
        ref = (r[REF_COL] if len(r) > REF_COL else "").strip()
        notes = (r[NOTES_COL] if len(r) > NOTES_COL else "").strip()
        row = store.get(key)
        if row is None:
            continue
        if status in SUBMITTED_STATUSES:
            if store.mark_submitted(key, ref):
                counts["submitted"] += 1
            store.update_fields(key, status=status, notes=notes)
        elif status in JUNK_STATUSES:
            if row["status"] != "junk":
                counts["junk"] += 1
            store.update_fields(key, status="junk", notes=notes)
        elif status:
            counts["unknown_status"] += 1
            log.warning("unknown status %r for %s", status, key)
        elif notes and notes != row["notes"]:
            store.update_fields(key, notes=notes)
            counts["noted"] += 1
    return counts
