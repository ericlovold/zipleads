"""Configuration: secrets from the environment, territory and profile from TOML.

- Settings: API keys and paths. Keys never live in code.
- Territory: WHERE. A zip list with optional city labels, plus that area's
  permit layers. Any state, any zips.
- Profile: WHAT. Industry-specific search terms, scoring weights, contact
  titles, export columns.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


def load_dotenv(path: str | os.PathLike = ".env") -> None:
    """Minimal .env loader: KEY=VALUE lines, # comments. Real env vars win."""
    p = Path(path)
    if not p.is_file():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    google_places_api_key: str
    zoominfo_username: str
    zoominfo_password: str
    zoominfo_enable: bool
    db_path: Path
    territory_path: Path
    profile_path: Path
    ingest_days: int
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = ""
    mail_from: str = ""
    mail_to: tuple[str, ...] = ()

    @property
    def mail_enabled(self) -> bool:
        return bool(self.smtp_host and self.mail_from and self.mail_to)

    @property
    def places_enabled(self) -> bool:
        return bool(self.google_places_api_key)

    @property
    def zoominfo_enabled(self) -> bool:
        return self.zoominfo_enable and bool(self.zoominfo_username and self.zoominfo_password)


def load_settings() -> Settings:
    load_dotenv()
    env = os.environ
    return Settings(
        google_places_api_key=env.get("GOOGLE_PLACES_API_KEY", "").strip(),
        zoominfo_username=env.get("ZOOMINFO_USERNAME", "").strip(),
        zoominfo_password=env.get("ZOOMINFO_PASSWORD", "").strip(),
        zoominfo_enable=_bool(env.get("ZOOMINFO_ENABLE"), False),
        db_path=Path(env.get("ZIPLEADS_DB", "data/leads.sqlite")),
        territory_path=Path(env.get("ZIPLEADS_TERRITORY", "territories/twin-cities-comcast.toml")),
        profile_path=Path(env.get("ZIPLEADS_PROFILE", "profiles/telecom-new-business.toml")),
        ingest_days=int(env.get("INGEST_DAYS", "7")),
        smtp_host=env.get("SMTP_HOST", "").strip(),
        smtp_port=int(env.get("SMTP_PORT", "465")),
        smtp_user=env.get("SMTP_USER", "").strip(),
        # Google shows App Passwords as "abcd efgh ijkl mnop" with non-breaking spaces;
        # the SMTP AUTH exchange is ASCII-only, so drop every kind of whitespace.
        smtp_password="".join(ch for ch in env.get("SMTP_PASSWORD", "") if not ch.isspace()),
        mail_from=env.get("MAIL_FROM", "").strip(),
        mail_to=tuple(a.strip() for a in env.get("MAIL_TO", "").split(",") if a.strip()),
    )


# --- Territory -----------------------------------------------------------


@dataclass(frozen=True)
class PermitFieldMap:
    applicant: str = "APPLICANT"
    address: str = "ADDRESS"
    permit_type: str = "PERMIT_TYPE"
    work_type: str = "WORK_TYPE"
    description: str = "DESCRIPTION"
    date: str = "ISSUE_DATE"
    value: str = "VALUATION"
    occupancy: str = ""  # optional: e.g. "Commercial" / "Residential"
    permit_number: str = ""  # optional: cited in the lead description
    status: str = ""  # optional: included in description for the reader
    applicant_person: str = ""  # optional: who filed, shown next to the applicant
    latitude: str = ""  # optional: lets enrich look up businesses at the site by coordinate
    longitude: str = ""


@dataclass(frozen=True)
class PermitLayer:
    name: str
    url: str
    city: str
    state: str
    fields: PermitFieldMap


@dataclass(frozen=True)
class Territory:
    name: str
    state: str
    zips: dict[str, str]  # zip -> city label ("" when unknown)
    permit_layers: tuple[PermitLayer, ...]
    path: Path

    def cities(self) -> list[str]:
        seen: dict[str, None] = {}
        for city in self.zips.values():
            if city:
                seen.setdefault(city, None)
        return list(seen)


def load_territory(path: str | Path) -> Territory:
    path = Path(path)
    doc = tomllib.loads(path.read_text(encoding="utf-8"))
    state = str(doc.get("state", "")).strip()
    zips: dict[str, str] = {}
    for z in doc.get("zips", []):
        zips[str(z).strip()[:5]] = ""
    for city, city_zips in (doc.get("cities") or {}).items():
        for z in city_zips:
            zips[str(z).strip()[:5]] = city
    layers = []
    for layer in doc.get("permit_layers", []):
        fm = layer.get("fields", {})
        layers.append(
            PermitLayer(
                name=layer["name"],
                url=str(layer.get("url", "")).rstrip("/"),
                city=layer.get("city", ""),
                state=layer.get("state", state),
                fields=PermitFieldMap(**{k: str(v) for k, v in fm.items()}),
            )
        )
    if not zips:
        raise ValueError(f"{path}: territory has no zips")
    return Territory(
        name=doc.get("name", path.stem),
        state=state,
        zips=zips,
        permit_layers=tuple(layers),
        path=path,
    )


# --- Profile ---------------------------------------------------------------


@dataclass(frozen=True)
class Profile:
    name: str
    news_terms: tuple[str, ...]
    permit_include_terms: tuple[str, ...]
    permit_exclude_terms: tuple[str, ...]
    places_queries: tuple[str, ...]
    contact_titles: tuple[str, ...]
    source_weights: dict[str, int]
    multi_site_bonus: int
    phone_bonus: int
    contact_bonus: int
    news_only_penalty: int
    unparsed_penalty: int
    export_columns: tuple[str, ...]
    permit_exclude_types: tuple[str, ...] = ()  # exact tokens on permit type / occupancy
    news_exclude_terms: tuple[str, ...] = ()  # headline substrings that are never a lead
    segment_exclude: tuple[str, ...] = ()
    segment_deprioritize: tuple[str, ...] = ()
    segment_boost: tuple[str, ...] = ()
    deprioritize_penalty: int = -20
    boost_bonus: int = 10
    value_tiers: tuple[tuple[float, int], ...] = ()
    path: Path = field(default=Path("."))


DEFAULT_EXPORT_COLUMNS = (
    "contact_name",
    "company_name",
    "contact_email",
    "phone",
    "address",
    "city",
    "state",
    "zip",
    "contact_title",
    "website",
    "signals",
    "sources",
    "signal_date",
    "evidence_url",
    "description",
    "score",
    "first_seen",
    "dedupe_key",
)


def load_profile(path: str | Path) -> Profile:
    path = Path(path)
    doc = tomllib.loads(path.read_text(encoding="utf-8"))
    score = doc.get("score", {})
    permits = doc.get("permits", {})
    segments = doc.get("segments", {})
    tiers = tuple((float(t[0]), int(t[1])) for t in score.get("value_tiers", []))
    return Profile(
        name=doc.get("name", path.stem),
        news_terms=tuple(doc.get("news", {}).get("terms", [])),
        permit_include_terms=tuple(t.lower() for t in permits.get("include_terms", [])),
        permit_exclude_terms=tuple(t.lower() for t in permits.get("exclude_terms", [])),
        permit_exclude_types=tuple(t.lower() for t in permits.get("exclude_types", [])),
        news_exclude_terms=tuple(t.lower() for t in doc.get("news", {}).get("exclude_terms", [])),
        places_queries=tuple(doc.get("places", {}).get("queries", ["opening soon"])),
        contact_titles=tuple(doc.get("contacts", {}).get("titles", [])),
        source_weights={k: int(v) for k, v in score.get("source_weights", {}).items()},
        multi_site_bonus=int(score.get("multi_site_bonus", 30)),
        phone_bonus=int(score.get("phone_bonus", 10)),
        contact_bonus=int(score.get("contact_bonus", 10)),
        news_only_penalty=int(score.get("news_only_penalty", -5)),
        unparsed_penalty=int(score.get("unparsed_penalty", -10)),
        export_columns=tuple(doc.get("export", {}).get("columns", DEFAULT_EXPORT_COLUMNS)),
        segment_exclude=tuple(t.lower() for t in segments.get("exclude", [])),
        segment_deprioritize=tuple(t.lower() for t in segments.get("deprioritize", [])),
        segment_boost=tuple(t.lower() for t in segments.get("boost", [])),
        deprioritize_penalty=int(segments.get("deprioritize_penalty", -20)),
        boost_bonus=int(segments.get("boost_bonus", 10)),
        value_tiers=tuple(sorted(tiers)),
        path=path,
    )
