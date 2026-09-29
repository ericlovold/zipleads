"""Thin HTTP wrapper so sources are testable with a fake client."""

from __future__ import annotations

from typing import Any, Protocol

import requests

DEFAULT_TIMEOUT = 30
USER_AGENT = "zipleads/0.1 (+lead pipeline; contact via repo owner)"


class Http(Protocol):
    def get_json(
        self, url: str, params: dict | None = None, headers: dict | None = None
    ) -> Any: ...
    def get_text(
        self, url: str, params: dict | None = None, headers: dict | None = None
    ) -> str: ...
    def post_json(self, url: str, body: dict, headers: dict | None = None) -> Any: ...


class RequestsHttp:
    def __init__(self, timeout: int = DEFAULT_TIMEOUT):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.timeout = timeout

    def get_json(self, url: str, params: dict | None = None, headers: dict | None = None) -> Any:
        r = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def get_text(self, url: str, params: dict | None = None, headers: dict | None = None) -> str:
        r = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
        r.raise_for_status()
        return r.text

    def post_json(self, url: str, body: dict, headers: dict | None = None) -> Any:
        r = self.session.post(url, json=body, headers=headers, timeout=self.timeout)
        r.raise_for_status()
        return r.json()
