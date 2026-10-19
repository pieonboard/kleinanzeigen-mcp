"""One serialized operation lane, completion-based pacing and typed wire failures."""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC
from email.utils import parsedate_to_datetime
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from curl_cffi.requests import RequestsError

from .models import DomainError


class Response(Protocol):
    status_code: int
    headers: Mapping[str, str]
    content: bytes

    def json(self) -> Any: ...


class Session(Protocol):
    def request(self, method: str, url: str, **kwargs: Any) -> Response: ...
    def close(self) -> None: ...


class RequestBudget:
    def __init__(
        self,
        *,
        interval: float = 1.5,
        request_timeout: float = 25,
        operation_timeout: float = 180,
        lock_timeout: float = 10,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        uniform: Callable[[float, float], float] = random.uniform,
        wall_clock: Callable[[], float] = time.time,
    ):
        if interval < 1.5 or request_timeout <= 0 or operation_timeout <= 0:
            raise ValueError("Invalid request budget")
        self.interval = interval
        self.request_timeout = request_timeout
        self.operation_timeout = operation_timeout
        self.lock_timeout = lock_timeout
        self.clock, self.sleep, self.uniform, self.wall_clock = clock, sleep, uniform, wall_clock
        self.lock = threading.RLock()
        self._local = threading.local()
        self._ready_at = 0.0
        self.cooldown_until = 0.0

    @contextmanager
    def operation(
        self, *, check_cooldown: bool = True, tool_name: str = "upstream"
    ) -> Iterator[None]:
        if not self.lock.acquire(timeout=self.lock_timeout):
            raise DomainError("BUSY", retryable=True)
        outer = not getattr(self._local, "depth", 0)
        try:
            if outer:
                self._local.deadline = self.clock() + self.operation_timeout
                self._local.correlation_id = uuid4().hex
                self._local.tool_name = tool_name
                if check_cooldown:
                    self.check_cooldown()
            self._local.depth = getattr(self._local, "depth", 0) + 1
            try:
                yield
            finally:
                self._local.depth -= 1
        finally:
            self.lock.release()

    def remaining(self) -> float:
        return max(0.0, getattr(self._local, "deadline", self.clock()) - self.clock())

    def check_cooldown(self) -> None:
        delay = self.cooldown_until - self.clock()
        if delay > 0:
            raise DomainError("RATE_LIMITED", retryable=True, retry_after_seconds=delay)

    def pause(self, delay: float, *, rate_limited: bool = False) -> None:
        if delay >= self.remaining():
            if rate_limited:
                raise DomainError(
                    "RATE_LIMITED",
                    retryable=True,
                    retry_after_seconds=max(0, self.cooldown_until - self.clock()),
                )
            raise DomainError(
                "UPSTREAM_UNAVAILABLE",
                retryable=True,
                hint="Operation deadline reached; retry deliberately later.",
            )
        if delay > 0:
            self.sleep(delay)

    def pace(self) -> None:
        self.pause(max(0, self._ready_at - self.clock()))

    def completed(self, owner: Any = None) -> None:
        self._ready_at = self.clock() + self.interval + self.uniform(0, 0.4)
        if owner is not None:
            owner._last = self.wall_clock()

    def retry_after(self, value: str | None) -> float:
        if value is None:
            return 0
        try:
            delay = float(value)
            return delay if 0 <= delay < float("inf") else 0
        except ValueError:
            try:
                date = parsedate_to_datetime(value)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=UTC)
                return max(0, date.timestamp() - self.wall_clock())
            except (TypeError, ValueError, OverflowError):
                return 0


class SafeResponse:
    """Never expose upstream text in errors; classify malformed acknowledged writes."""

    def __init__(self, response: Response, *, mutation: bool = False):
        self._response = response
        self.status_code = response.status_code
        self.headers = response.headers
        self.content = response.content
        self._mutation = mutation

    def json(self) -> Any:
        if len(self.content) > 8 * 1024 * 1024:
            raise DomainError("PARTIAL_WRITE" if self._mutation else "LIMIT_EXCEEDED")
        try:
            return self._response.json()
        except (ValueError, UnicodeError):
            raise DomainError("PARTIAL_WRITE" if self._mutation else "UPSTREAM_SCHEMA") from None


class PacedSession:
    def __init__(
        self,
        session: Session,
        budget: RequestBudget,
        *,
        owner: Any = None,
        account: bool = False,
        auth_exchange: bool = False,
    ):
        self.raw = session
        self.budget = budget
        self.owner = owner
        self.account = account
        self.auth_exchange = auth_exchange

    def get(self, url: str, **kwargs: Any) -> SafeResponse:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> SafeResponse:
        return self.request("POST", url, **kwargs)

    def request(
        self,
        method: str,
        url: str,
        *,
        account: bool | None = None,
        accepted: tuple[int, ...] | None = None,
        profile: bool = False,
        **kwargs: Any,
    ) -> SafeResponse:
        method = method.upper()
        read = method == "GET"
        mutation = not read and not self.auth_exchange
        authed = self.account if account is None else account
        accepted = accepted or ((200,) if read or self.auth_exchange else (200, 201, 204))
        b = self.budget
        with b.operation():
            b.check_cooldown()
            for attempt in range(1, (3 if read else 1) + 1):
                b.pace()
                timeout = min(
                    float(kwargs.get("timeout", b.request_timeout)),
                    b.request_timeout,
                    b.remaining(),
                )
                if timeout <= 0:
                    raise DomainError("UPSTREAM_UNAVAILABLE", retryable=True)
                started = b.clock()
                status: int | None = None
                try:
                    try:
                        response = self.raw.request(method, url, **{**kwargs, "timeout": timeout})
                        status = response.status_code
                    finally:
                        b.completed(self.owner)
                        static = {
                            "api",
                            "ads",
                            "ads.json",
                            "users",
                            "public",
                            "profile.json",
                            "pictures",
                            "oauth",
                            "token",
                            "locations.json",
                            "search-metadata",
                            "messagebox",
                            "conversations",
                            "read",
                            "paused",
                            "active",
                            "extend",
                            "status",
                            "watchlist.json",
                            "create-conversation",
                            "user-reputation-service",
                            "reputation-summary",
                        }
                        route = "/" + "/".join(
                            part if part in static else "{id}"
                            for part in urlsplit(url).path.split("/")
                            if part
                        )
                        phase = (
                            "auth_exchange"
                            if self.auth_exchange
                            else (
                                "account_read"
                                if authed and read
                                else "public_read"
                                if read
                                else "mutation"
                            )
                        )
                        logging.getLogger("kleinanzeigen_mcp").info(
                            json.dumps(
                                {
                                    "correlation_id": b._local.correlation_id,
                                    "tool": b._local.tool_name,
                                    "phase": phase,
                                    "route": route,
                                    "status": status,
                                    "elapsed": b.clock() - started,
                                    "attempt": attempt,
                                }
                            )
                        )
                except DomainError as exc:
                    if mutation and exc.detail.code == "LIMIT_EXCEEDED":
                        raise DomainError("PARTIAL_WRITE") from None
                    raise
                except (OSError, httpx.TransportError, RequestsError):
                    if read and attempt < 3:
                        b.pause(1.5 * attempt + b.uniform(0, 1.5))
                        continue
                    raise DomainError(
                        "WRITE_OUTCOME_UNKNOWN" if mutation else "UPSTREAM_UNAVAILABLE",
                        retryable=not mutation,
                    ) from None
                status = response.status_code
                if status in accepted:
                    if len(response.content) > kwargs.get("max_response_bytes", 8 * 1024 * 1024):
                        raise DomainError("PARTIAL_WRITE" if mutation else "LIMIT_EXCEEDED")
                    return SafeResponse(response, mutation=mutation)
                transient = status in (429, 500, 502, 503, 504)
                retry_after = (
                    b.retry_after(
                        response.headers.get("Retry-After") or response.headers.get("retry-after")
                    )
                    if transient
                    else 0.0
                )
                if status == 429:
                    b.cooldown_until = max(
                        b.cooldown_until,
                        b.clock() + max(retry_after, 1.5 * attempt + b.uniform(0, 1.5)),
                    )
                if read and transient and retry_after >= b.remaining():
                    raise DomainError(
                        "RATE_LIMITED",
                        http_status=status,
                        retryable=True,
                        retry_after_seconds=retry_after,
                    )
                if read and transient and attempt < 3:
                    delay = max(
                        1.5 * attempt + b.uniform(0, 1.5), retry_after, b.cooldown_until - b.clock()
                    )
                    b.pause(delay, rate_limited=status == 429)
                    continue
                if mutation and (status >= 500 or 200 <= status < 300):
                    raise DomainError("WRITE_OUTCOME_UNKNOWN", http_status=status)
                if self.auth_exchange and status in (400, 401, 403):
                    raise DomainError("AUTH_EXPIRED", http_status=status)
                if status == 401:
                    raise DomainError(
                        "AUTH_EXPIRED" if authed else "APP_CREDENTIALS_REJECTED", http_status=status
                    )
                if status == 403 and profile:
                    marker = response.content[:65536].decode("utf-8", errors="replace").lower()
                    if "ip-bereich" in marker and "gesperrt" in marker:
                        raise DomainError(
                            "IP_BLOCKED",
                            http_status=status,
                            hint="Use reputation data and diagnose the connection manually.",
                        )
                if status == 403:
                    raise DomainError(
                        "AUTH_OR_PERMISSION_DENIED" if authed else "APP_CREDENTIALS_REJECTED",
                        http_status=status,
                    )
                if status == 404:
                    raise DomainError("NOT_FOUND", http_status=status)
                if status == 400:
                    raise DomainError("INVALID_ARGUMENT", http_status=status)
                if status == 429:
                    raise DomainError(
                        "RATE_LIMITED",
                        retryable=read,
                        http_status=status,
                        retry_after_seconds=max(0, b.cooldown_until - b.clock()),
                    )
                raise DomainError(
                    "UPSTREAM_UNAVAILABLE", retryable=read or self.auth_exchange, http_status=status
                )
        raise AssertionError("unreachable")

    def close(self) -> None:
        self.raw.close()
