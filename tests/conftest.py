"""Offline fixtures. pytest-socket denies all TCP; curl has its own guard."""

import pytest
from curl_cffi import requests as creq


@pytest.fixture(autouse=True)
def deny_curl(monkeypatch):
    monkeypatch.delenv("KLEINANZEIGEN_REFRESH_TOKEN", raising=False)
    monkeypatch.delenv("KLEINANZEIGEN_TOKEN_DIR", raising=False)

    def denied(*args, **kwargs):
        raise AssertionError("Real curl networking is forbidden in tests")

    monkeypatch.setattr(creq.Session, "request", denied)


class Clock:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, delay):
        self.sleeps.append(delay)
        self.now += delay


class Response:
    def __init__(self, status=200, body=None, headers=None, content=b""):
        self.status_code = status
        self.body = {} if body is None else body
        self.headers = headers or {}
        self.content = content

    def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


class Session:
    def __init__(self, responses=(), clock=None):
        self.responses = list(responses)
        self.calls = []
        self.clock = clock
        self.closed = False

    def request(self, method, url, **kwargs):
        self.calls.append((self.clock() if self.clock else 0, method, url, kwargs))
        item = self.responses.pop(0) if self.responses else Response()
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.closed = True
