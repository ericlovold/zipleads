"""CSV export in the profile's column order."""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path


def write_csv(rows: list[sqlite3.Row], columns: tuple[str, ...], out_path: str | Path) -> int:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row[c] for c in columns if c in row.keys()})
    return len(rows)
