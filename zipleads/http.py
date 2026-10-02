"""Thin HTTP wrapper so sources are testable with a fake client."""

from __future__ import annotations

import logging
import time
from typing import Any, Protocol

import requests

log = logging.getLogger("zipleads.http")

DEFAULT_TIMEOUT = 30
RETRIES = 3
BACKOFF_SECONDS = 1.5
USER_AGENT = "zipleads/0.1 (+lead pipeline; contact via repo owner)"


class Http(Protocol):
    def get_json(
        self, url: str, params: dict | None = None, headers: dict | None = None
    ) -> Any: ...
    def get_text(
        self, url: str, params: dict | None = None, headers: dict | None = None
    ) -> str: ...
    def post_json(
        self, url: str, body: dict, headers: dict | None = None, params: dict | None = None
    ) -> Any: ...
    def put_json(
        self, url: str, body: dict, headers: dict | None = None, params: dict | None = None
    ) -> Any: ...


class RequestsHttp:
    """requests.Session with retry on 5xx and connection errors (not on 4xx)."""

    def __init__(self, timeout: int = DEFAULT_TIMEOUT, retries: int = RETRIES, sleep=time.sleep):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.timeout = timeout
        self.retries = retries
        self.sleep = sleep

    def _send(self, method: str, url: str, **kwargs) -> requests.Response:
        last: Exception | None = None
        for attempt in range(self.retries):
            try:
                r = self.session.request(method, url, timeout=self.timeout, **kwargs)
                if r.status_code >= 500:
                    raise requests.HTTPError(f"{r.status_code} Server Error for {url}", response=r)
                r.raise_for_status()
                return r
            except (requests.ConnectionError, requests.Timeout) as exc:
                last = exc
            except requests.HTTPError as exc:
                if exc.response is None or exc.response.status_code < 500:
                    raise
                last = exc
            delay = BACKOFF_SECONDS * (2**attempt)
            log.warning("%s %s failed (%s); retry in %.1fs", method, url, last, delay)
            self.sleep(delay)
        assert last is not None
        raise last

    def get_json(self, url: str, params: dict | None = None, headers: dict | None = None) -> Any:
        return self._send("GET", url, params=params, headers=headers).json()

    def get_text(self, url: str, params: dict | None = None, headers: dict | None = None) -> str:
        return self._send("GET", url, params=params, headers=headers).text

    def post_json(
        self, url: str, body: dict, headers: dict | None = None, params: dict | None = None
    ) -> Any:
        return self._send("POST", url, json=body, headers=headers, params=params).json()

    def put_json(
        self, url: str, body: dict, headers: dict | None = None, params: dict | None = None
    ) -> Any:
        return self._send("PUT", url, json=body, headers=headers, params=params).json()


class BudgetExhausted(RuntimeError):
    """A metered API hit its per-run call ceiling."""


class BudgetedHttp:
    """Counts requests through `inner` and refuses the one past `max_calls`.

    Wrap a metered API (Google Places) so a bug or a big backlog cannot run up a bill.
    The check happens before the request, so the ceiling is never exceeded.
    """

    def __init__(self, inner: Http, max_calls: int, label: str = "api"):
        self.inner = inner
        self.max_calls = max_calls
        self.label = label
        self.calls = 0

    def _spend(self) -> None:
        if self.calls >= self.max_calls:
            raise BudgetExhausted(f"{self.label}: per-run limit of {self.max_calls} calls reached")
        self.calls += 1

    def get_json(self, url: str, params: dict | None = None, headers: dict | None = None) -> Any:
        self._spend()
        return self.inner.get_json(url, params=params, headers=headers)

    def get_text(self, url: str, params: dict | None = None, headers: dict | None = None) -> str:
        self._spend()
        return self.inner.get_text(url, params=params, headers=headers)

    def post_json(
        self, url: str, body: dict, headers: dict | None = None, params: dict | None = None
    ) -> Any:
        self._spend()
        return self.inner.post_json(url, body, headers=headers, params=params)

    def put_json(
        self, url: str, body: dict, headers: dict | None = None, params: dict | None = None
    ) -> Any:
        self._spend()
        return self.inner.put_json(url, body, headers=headers, params=params)
