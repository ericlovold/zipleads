from __future__ import annotations

import json
from pathlib import Path

import pytest

from zipleads.config import Profile, Settings, Territory, load_profile, load_territory
from zipleads.territory import TerritoryMatcher

ROOT = Path(__file__).parent.parent
FIXTURES = Path(__file__).parent / "fixtures"
TERRITORY_TOML = ROOT / "territories" / "twin-cities-comcast.toml"
PROFILE_TOML = ROOT / "profiles" / "telecom-new-business.toml"


class FakeHttp:
    """Records requests and replays canned responses keyed by URL substring."""

    def __init__(self, responses: dict[str, object] | None = None):
        self.responses = responses or {}
        self.calls: list[tuple[str, str, dict | None]] = []

    def _lookup(self, url: str):
        for needle, resp in self.responses.items():
            if needle in url:
                return resp
        raise AssertionError(f"no fake response for {url}")

    def get_json(self, url, params=None, headers=None):
        self.calls.append(("GET", url, params))
        return self._lookup(url)

    def get_text(self, url, params=None, headers=None):
        self.calls.append(("GET", url, params))
        return self._lookup(url)

    def post_json(self, url, body, headers=None, params=None):
        self.calls.append(("POST", url, body))
        resp = self._lookup(url)
        return resp(body) if callable(resp) else resp

    def put_json(self, url, body, headers=None, params=None):
        self.calls.append(("PUT", url, body))
        resp = self._lookup(url)
        return resp(body) if callable(resp) else resp


@pytest.fixture
def territory() -> Territory:
    return load_territory(TERRITORY_TOML)


@pytest.fixture
def profile() -> Profile:
    return load_profile(PROFILE_TOML)


@pytest.fixture
def matcher(territory) -> TerritoryMatcher:
    return TerritoryMatcher(territory)


@pytest.fixture
def make_settings(tmp_path):
    def build(places_key="", zi_user="", zi_pass="", zi_on=False) -> Settings:
        return Settings(
            google_places_api_key=places_key,
            zoominfo_username=zi_user,
            zoominfo_password=zi_pass,
            zoominfo_enable=zi_on,
            db_path=tmp_path / "leads.sqlite",
            territory_path=TERRITORY_TOML,
            profile_path=PROFILE_TOML,
            ingest_days=7,
        )

    return build


@pytest.fixture
def fixture_json():
    def load(name: str):
        return json.loads((FIXTURES / name).read_text(encoding="utf-8"))

    return load


@pytest.fixture
def fixture_text():
    def load(name: str) -> str:
        return (FIXTURES / name).read_text(encoding="utf-8")

    return load
