from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field


@dataclass
class Lead:
    """One business-level signal. Deduped on `dedupe_key` in the store."""

    source: str
    signal: str
    company_name: str
    applicant: str = ""  # who filed the permit (usually a contractor), not the business
    address: str = ""
    city: str = ""
    state: str = "MN"
    zip: str = ""
    phone: str = ""
    website: str = ""
    contact_name: str = ""
    contact_title: str = ""
    contact_email: str = ""
    signal_date: str = ""
    evidence_url: str = ""
    description: str = ""
    value: float = 0.0
    raw: dict = field(default_factory=dict)

    def raw_json(self) -> str:
        return json.dumps(self.raw, sort_keys=True, default=str)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["raw"] = self.raw_json()
        return d
