import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from xml.etree import ElementTree as ET

import httpx
import pytest
from conftest import Clock, Response, Session

from kleinanzeigen_mcp.config import Settings
from kleinanzeigen_mcp.models import DomainError
from kleinanzeigen_mcp.transport import PacedSession, RequestBudget
from kleinanzeigen_mcp.upstream import Runtime, SellerClient, ShippingKleinanzeigenAPI


def budget(clock=None, **kwargs):
    c = clock or Clock()
    return RequestBudget(clock=c, sleep=c.sleep, uniform=lambda a, b: b, **kwargs)


def code(expected, fn):
    with pytest.raises(DomainError) as caught:
        fn()
    assert caught.value.detail.code == expected
    return caught.value.detail


def test_shared_pacing_including_exception_and_client_last():
    c = Clock()
    b = budget(c)
    raw = Session([Response(), OSError("SECRET"), Response()], c)
    owner = type("Owner", (), {"_last": 0})()
    public = PacedSession(raw, b, owner=owner)
    public.get("https://example.test/public")
    code("WRITE_OUTCOME_UNKNOWN", lambda: public.post("https://example.test/upload"))
    other = PacedSession(raw, b)
    other.get("https://example.test/cdn")
    assert [round(x[0], 2) for x in raw.calls] == [100, 101.9, 103.8]
    assert owner._last > 0


@pytest.mark.parametrize("method,attempts", [("GET", 3), ("PUT", 1), ("POST", 1), ("DELETE", 1)])
def test_attempt_policy(method, attempts):
    c = Clock()
    raw = Session([Response(503)] * 4, c)
    s = PacedSession(raw, budget(c))
    code(
        "UPSTREAM_UNAVAILABLE" if method == "GET" else "WRITE_OUTCOME_UNKNOWN",
        lambda: s.request(method, "https://example.test/route"),
    )
    assert len(raw.calls) == attempts
    assert all(x[3]["timeout"] <= 25 for x in raw.calls)


def test_retry_after_cooldown_and_deadline():
    c = Clock()
    b = budget(c, operation_timeout=4)
    raw = Session([Response(429, headers={"Retry-After": "90"})], c)
    s = PacedSession(raw, b)
    detail = code("RATE_LIMITED", lambda: s.get("https://example.test"))
    assert detail.retry_after_seconds >= 90
    code("RATE_LIMITED", lambda: s.get("https://example.test"))
    assert len(raw.calls) == 1
    c.now += 91
    s.get("https://example.test")
    with b.operation():
        c.now += 4
        code("UPSTREAM_UNAVAILABLE", lambda: s.post("https://example.test"))
    assert len(raw.calls) == 2


def test_all_lanes_are_serialized_and_busy():
    b = RequestBudget(lock_timeout=0.02)
    entered, release = threading.Event(), threading.Event()

    def worker():
        with b.operation():
            entered.set()
            release.wait(2)

    with ThreadPoolExecutor() as pool:
        f = pool.submit(worker)
        assert entered.wait(1)
        code("BUSY", lambda: b.operation().__enter__())
        release.set()
        f.result()


def test_runtime_is_lazy_public_independent_and_closes(tmp_path):
    sessions = []

    def factory():
        s = Session()
        sessions.append(s)
        return s

    r = Runtime(Settings(token_path=tmp_path / "missing"), session_factory=factory, budget=budget())
    assert not sessions
    with r.operation() as api:
        api._get("https://example.test/search")
    code("AUTH_REQUIRED", lambda: r.account())
    assert len(sessions) >= 1
    r.close()
    assert all(s.closed for s in sessions)


def write_token(path, **values):
    path.write_text(json.dumps({"refresh_token": "dummy-refresh", **values}))
    path.chmod(0o600)


def test_real_authenticator_refresh_rotates_once_private_storage(tmp_path):
    path = tmp_path / "token.json"
    write_token(path, access_token="old", expires_at=0)
    raw = Session([Response(body={"access_token": "new", "refresh_token": "rotated"})])
    r = Runtime(
        Settings(token_path=path, user_id="1"), session_factory=lambda: raw, budget=budget()
    )
    with r.operation(account=True) as api:
        assert api._headers(authed=True)["X-EBAYK-USERID-TOKEN"] == "new"
        assert api._headers(authed=True)["X-EBAYK-USERID-TOKEN"] == "new"
    assert len(raw.calls) == 1
    assert raw.calls[0][3]["timeout"] <= 25
    saved = json.loads(path.read_text())
    assert saved["refresh_token"] == "rotated"
    assert saved["expires_at"] > 0
    assert path.stat().st_mode & 0o777 == 0o600


def test_revoked_auth_latches_until_token_replacement(tmp_path):
    path = tmp_path / "token.json"
    write_token(path)
    raw = Session(
        [Response(400, body={"error": "secret body"}), Response(body={"access_token": "new"})]
    )
    r = Runtime(
        Settings(token_path=path, user_id="1"), session_factory=lambda: raw, budget=budget()
    )

    def headers():
        with r.operation(account=True) as api:
            api._headers(authed=True)

    code("AUTH_EXPIRED", headers)
    code("AUTH_EXPIRED", headers)
    assert len(raw.calls) == 1
    replacement = tmp_path / "new.json"
    write_token(replacement, marker="replacement")
    os.replace(replacement, path)
    headers()
    assert len(raw.calls) == 2


@pytest.mark.parametrize(
    "status,authed,expected",
    [
        (401, False, "APP_CREDENTIALS_REJECTED"),
        (403, False, "APP_CREDENTIALS_REJECTED"),
        (401, True, "AUTH_EXPIRED"),
        (403, True, "AUTH_OR_PERMISSION_DENIED"),
    ],
)
def test_context_sensitive_errors(status, authed, expected, caplog):
    raw = Session([Response(status, body={"token": "SECRET"})])
    s = PacedSession(raw, budget())
    e = code(
        expected,
        lambda: s.request("GET", "https://example.test/private@example.test", account=authed),
    )
    assert "SECRET" not in str(e)
    assert "private@" not in caplog.text


def test_shipping_patch_and_upload(tmp_path, monkeypatch):
    import kleinanzeigen_mcp.upstream as upstream

    parts, closed = [], []

    class Mime:
        def addpart(self, **kwargs):
            parts.append(kwargs)

        def close(self):
            closed.append(True)

    monkeypatch.setattr(upstream, "CurlMime", Mime)
    path = tmp_path / "test.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"fixture")
    payload = json.loads(Path("tests/fixtures/picture_upload.json").read_text())
    raw = Session([Response(201, payload), Response(503)])
    api = ShippingKleinanzeigenAPI(budget=budget(), session=raw, user_id="1")
    monkeypatch.setattr(api, "_headers", lambda **kw: {"auth-test": str(kw)})
    assert api.upload_picture(str(path)).endswith("/xxl.png")
    code("WRITE_OUTCOME_UNKNOWN", lambda: api.upload_picture(str(path)))
    assert len(closed) == 2 and len(raw.calls) == 2
    assert parts[0] == {
        "name": "file",
        "content_type": "image/png",
        "filename": "test.png",
        "local_path": str(path),
    }
    args = dict(
        title="A & B",
        description="<safe>",
        category_id="1",
        location_id="2",
        price=42,
        price_type="FIXED",
        poster_type="PRIVATE",
        ad_type="OFFERED",
        contact_name="N",
        email="fixture@example.test",
        phone=None,
        attributes={},
        picture_urls=[],
        latitude=None,
        longitude=None,
    )
    xml = api._build_ad_xml(shipping_options=['a"&<>'], **args)
    root = ET.fromstring(xml)
    assert list(root)[-1].tag.endswith("shipping-options")
    assert list(root)[-1][0].attrib["id"] == 'a"&<>'
    assert "A &amp; B" in xml and "&lt;safe&gt;" in xml
    monkeypatch.setattr(
        api, "_request", lambda *a, **k: (_ for _ in ()).throw(DomainError("INVALID_ARGUMENT"))
    )
    code(
        "INVALID_ARGUMENT",
        lambda: api.post_ad(
            shipping_options=["abc"],
            **{k: v for k, v in args.items() if k not in {"poster_type", "ad_type"}},
        ),
    )
    assert not hasattr(api, "_posting_shipping_options")


def test_upload_accepted_malformed_is_partial(tmp_path, monkeypatch):
    p = tmp_path / "file.gif"
    p.write_bytes(b"GIF89a fixture")
    api = ShippingKleinanzeigenAPI(budget=budget(), session=Session([Response(201)]))
    monkeypatch.setattr(api, "_headers", lambda **kw: {})
    code("PARTIAL_WRITE", lambda: api.upload_picture(str(p)))


def test_seller_httpx_uses_same_lane_without_account_headers():
    c = Clock()
    b = budget(c)
    seen = []

    def handle(request):
        seen.append((c(), request))
        return httpx.Response(200, json={"public": True})

    client = SellerClient(
        b,
        headers={"Authorization": "Basic dummy"},
        client=httpx.Client(transport=httpx.MockTransport(handle)),
    )
    client.profile("1")
    client.reputation("1")
    assert seen[1][0] - seen[0][0] == pytest.approx(1.9)
    assert all("X-EBAYK-USERID-TOKEN" not in x[1].headers for x in seen)
    client.close()


def test_completion_pacing_is_shared_across_refresh_upload_cdn_and_seller(tmp_path, monkeypatch):

    c = Clock()
    b = budget(c)
    path = tmp_path / "token.json"
    write_token(path)
    image = tmp_path / "test.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    raw = Session(
        [
            Response(),
            Response(body={"access_token": "fixture-token"}),
            Response(),
            Response(201, body={"url": "https://img.kleinanzeigen.de/new.png"}),
            Response(),
        ],
        c,
    )
    r = Runtime(Settings(token_path=path, user_id="1"), session_factory=lambda: raw, budget=b)
    with r.operation() as api:
        api._get("https://example.test/public")
    with r.operation(account=True) as api:
        api._request("GET", "https://example.test/account")
        api.upload_picture(str(image))
    r.cdn_get("https://img.kleinanzeigen.de/fixture.png")
    seen = []

    def seller_wire(request):
        seen.append(c())
        return httpx.Response(200, json={})

    seller = SellerClient(
        b,
        headers=r.public()._headers(),
        client=httpx.Client(transport=httpx.MockTransport(seller_wire)),
    )
    seller.profile("1")
    assert [round(t, 1) for t in [*(x[0] for x in raw.calls), *seen]] == [
        100,
        101.9,
        103.8,
        105.7,
        107.6,
        109.5,
    ]
    assert "X-EBAYK-USERID-TOKEN" not in raw.calls[-1][3]["headers"]
    r.close()
    seller.close()


def test_retry_after_http_date_and_timeout_reduction():
    from email.utils import formatdate

    c = Clock()
    b = RequestBudget(
        clock=c, sleep=c.sleep, uniform=lambda a, b: 0, wall_clock=lambda: 1000, operation_timeout=8
    )
    raw = Session(
        [Response(429, headers={"Retry-After": formatdate(1003, usegmt=True)}), Response()], c
    )
    PacedSession(raw, b).get("https://example.test")
    assert [x[0] for x in raw.calls] == [100, 103]
    assert raw.calls[1][3]["timeout"] == 5


@pytest.mark.parametrize("body", [ValueError("SECRET"), [], {"no": "access_token"}])
def test_auth_malformed_response_is_typed(tmp_path, body):
    path = tmp_path / "token.json"
    write_token(path)
    raw = Session([Response(body=body)])
    r = Runtime(
        Settings(token_path=path, user_id="1"), session_factory=lambda: raw, budget=budget()
    )

    def access():
        with r.operation(account=True) as api:
            api._headers(authed=True)

    code("UPSTREAM_SCHEMA", access)


def test_auth_transient_cooldown_then_retry(tmp_path):
    path = tmp_path / "token.json"
    write_token(path)
    c = Clock()
    raw = Session([Response(503), Response(body={"access_token": "new"})], c)
    r = Runtime(
        Settings(token_path=path, user_id="1"), session_factory=lambda: raw, budget=budget(c)
    )

    def access():
        with r.operation(account=True) as api:
            api._headers(authed=True)

    code("UPSTREAM_UNAVAILABLE", access)
    code("UPSTREAM_UNAVAILABLE", access)
    assert len(raw.calls) == 1
    c.now += 6
    access()
    assert len(raw.calls) == 2


def test_private_and_corrupt_token_and_symlink(tmp_path):
    path = tmp_path / "token.json"
    write_token(path, access_token="cached", expires_at=9999999999)
    path.chmod(0o644)
    r = Runtime(Settings(token_path=path), session_factory=Session, budget=budget())
    r.account()
    assert path.stat().st_mode & 0o777 == 0o600
    r.close()
    path.write_text("bad JSON SECRET")
    code("AUTH_STORAGE_ERROR", r.account)
    path.unlink()
    target = tmp_path / "target.json"
    write_token(target)
    path.symlink_to(target)
    code("AUTH_STORAGE_ERROR", r.account)


def test_60_second_early_refresh_boundary(tmp_path, monkeypatch):
    import kleinanzeigen_api.auth as auth

    monkeypatch.setattr(auth.time, "time", lambda: 1000)
    path = tmp_path / "token.json"
    write_token(path, access_token="cached", expires_at=1061)
    raw = Session([Response(body={"access_token": "new", "expires_in": 120})])
    r = Runtime(Settings(token_path=path), session_factory=lambda: raw, budget=budget())
    with r.operation(account=True) as api:
        assert api._headers(authed=True)["X-EBAYK-USERID-TOKEN"] == "cached"
        assert not raw.calls
        monkeypatch.setattr(auth.time, "time", lambda: 1001)
        assert api._headers(authed=True)["X-EBAYK-USERID-TOKEN"] == "new"
    assert json.loads(path.read_text())["expires_at"] == 1121


@pytest.mark.parametrize(
    "body,expected",
    [
        ({"url": "https://img.kleinanzeigen.de/fallback.png"}, "fallback.png"),
        (
            {
                "{http://www.ebayclassifiedsgroup.com/schema/picture/v1}picture": {
                    "value": {
                        "link": {"rel": "detail", "href": "https://img.kleinanzeigen.de/detail.png"}
                    }
                }
            },
            "detail.png",
        ),
    ],
)
def test_upload_link_variants_and_200(tmp_path, monkeypatch, body, expected):
    image = tmp_path / "test.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    api = ShippingKleinanzeigenAPI(budget=budget(), session=Session([Response(200, body)]))
    monkeypatch.setattr(api, "_headers", lambda **kw: {})
    assert api.upload_picture(str(image)).endswith(expected)


def test_shipping_temporary_options_used_and_cleaned_on_success(monkeypatch):
    raw = Session([Response(201, headers={"Location": "/api/users/1/ads/42.json"})])
    api = ShippingKleinanzeigenAPI(budget=budget(), session=raw, user_id="1")
    monkeypatch.setattr(api, "_headers", lambda **kw: {})
    result = api.post_ad(
        title="Fixture",
        description="Text",
        category_id="1",
        location_id="2",
        email="fixture@example.test",
        shipping_options=["observed-option"],
    )
    assert result == "42"
    assert not hasattr(api, "_posting_shipping_options")
    wire = raw.calls[0]
    assert wire[1] == "POST"
    assert wire[3]["headers"]["Content-Type"] == "application/xml"
    xml = wire[3]["data"].decode()
    assert '<shipping:shipping-option id="observed-option"/>' in xml
    assert xml.endswith("</shipping:shipping-options></ad:ad>")


def test_raw_hooks_and_not_found_semantics():
    raw = Session(
        [
            Response(body={"raw": "kept"}),
            Response(404),
            Response(body={"nested": "metadata"}),
            Response(body={"nested": "locations"}),
        ]
    )
    api = ShippingKleinanzeigenAPI(budget=budget(), session=raw)
    assert api.search_page_extended(
        query="Fixture",
        location_id="2",
        distance_km=10,
        min_price=1,
        attributes={"key": ["a", "b"]},
        shippable=True,
    ) == {"raw": "kept"}
    p = raw.calls[0][3]["params"]
    assert p["q"] == "Fixture" and p["distanceUnit"] == "KM"
    assert p["priceCurrency"] == "EUR" and p["attr[key]"] == "a,b"
    assert p["shippable"] == "true" and p["includeTopAds"] == "false"
    assert api._fetch_ad_json("9") is None
    assert api.category_filters_raw("1") == {"nested": "metadata"}
    assert api.locations_raw("Berlin") == {"nested": "locations"}
    assert raw.calls[-1][3]["params"] == {"depth": 1, "q": "Berlin"}


def test_profile_block_is_typed_and_not_retried():
    seen = []

    def wire(request):
        seen.append(request)
        return httpx.Response(403, text="Dieser IP-Bereich ist gesperrt SECRET")

    seller = SellerClient(
        budget(), headers={}, client=httpx.Client(transport=httpx.MockTransport(wire))
    )
    code("IP_BLOCKED", lambda: seller.profile("1"))
    assert len(seen) == 1


def test_shutdown_closes_during_cooldown(tmp_path):
    raw = Session([Response(429, headers={"Retry-After": "999"})])
    r = Runtime(
        Settings(token_path=tmp_path / "missing"), session_factory=lambda: raw, budget=budget()
    )
    code("RATE_LIMITED", lambda: r.public()._get("https://example.test"))
    r.close()
    assert raw.closed


def test_concurrent_operations_keep_all_phases_together():
    import time

    b = RequestBudget(interval=1.5, sleep=lambda _: None)
    events = []

    class Wire(Session):
        def request(self, method, url, **kwargs):
            events.append((url, threading.get_ident()))
            time.sleep(0.01)
            return Response()

    a, other = PacedSession(Wire(), b), PacedSession(Wire(), b)

    def operation(session, label):
        with b.operation():
            session.get("https://example.test/" + label + "-first")
            time.sleep(0.01)
            session.post("https://example.test/" + label + "-last")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(operation, a, "account")
        second = pool.submit(operation, other, "public")
        first.result()
        second.result()
    assert events[0][1] == events[1][1]
    assert events[2][1] == events[3][1]
    assert events[0][1] != events[2][1]


async def test_cancelled_worker_retains_write_lock():
    import anyio
    from fastmcp import Client

    from kleinanzeigen_mcp.server import create_server

    b = RequestBudget(lock_timeout=0.02)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    class SlowWrite:
        def invoke(self, name, arguments):
            with b.operation():
                entered.set()
                release.wait(2)
                finished.set()
                return {"ok": True, "action": "reply_sent", "conversation_id": "thread"}

    async with Client(create_server(services=SlowWrite())) as client:
        async with anyio.create_task_group() as group:

            async def invoke():
                await client.call_tool(
                    "kleinanzeigen_reply", {"conversation_id": "thread", "text": "Hallo"}
                )

            group.start_soon(invoke)
            while not entered.is_set():
                await anyio.sleep(0.005)
            group.cancel_scope.cancel()
            # Cancellation cannot claim the remote write was cancelled.
            code("BUSY", lambda: b.operation().__enter__())
            release.set()
        assert await anyio.to_thread.run_sync(finished.wait, 1)


def test_attempt_logs_have_safe_correlation_and_route_fields(caplog):
    import logging

    caplog.set_level(logging.INFO, logger="kleinanzeigen_mcp")
    raw = Session([Response()])
    b = budget()
    with b.operation(tool_name="kleinanzeigen_list_my_ads"):
        PacedSession(raw, b).get(
            "https://example.test/api/users/private@example.test/profile.json?secret=TOKEN",
            headers={"Authorization": "SECRET"},
        )
    record = json.loads(caplog.records[-1].getMessage())
    assert {
        "correlation_id",
        "tool",
        "phase",
        "route",
        "status",
        "elapsed",
        "attempt",
    } <= record.keys()
    assert record["tool"] == "kleinanzeigen_list_my_ads"
    assert record["route"] == "/api/users/{id}/profile.json"
    assert all(value not in caplog.text for value in ["private@", "TOKEN", "SECRET"])


def test_503_retry_after_is_lower_bound_and_deadline_reports_safe_delay():
    c = Clock()
    b = budget(c)
    raw = Session([Response(503, headers={"Retry-After": "12"}), Response()], c)
    PacedSession(raw, b).get("https://example.test")
    assert raw.calls[1][0] - raw.calls[0][0] >= 12
    c = Clock()
    raw = Session([Response(503, headers={"Retry-After": "90"})], c)
    detail = code(
        "RATE_LIMITED",
        lambda: PacedSession(raw, budget(c, operation_timeout=10)).get("https://example.test"),
    )
    assert detail.retry_after_seconds >= 90
    assert len(raw.calls) == 1


@pytest.mark.parametrize(
    "token",
    [
        {"refresh_token": []},
        {"refresh_token": {"secret": "invalid"}},
        {"refresh_token": "dummy", "expires_at": "not-a-time"},
        {"refresh_token": "dummy", "expires_at": float("inf")},
    ],
)
def test_semantically_corrupt_tokens_fail_without_wire(tmp_path, token):
    path = tmp_path / "token.json"
    path.write_text(json.dumps(token))
    path.chmod(0o600)
    raw = Session()
    r = Runtime(Settings(token_path=path), session_factory=lambda: raw, budget=budget())
    code("AUTH_STORAGE_ERROR", r.account)
    assert not raw.calls


def test_acknowledged_upload_with_invalid_relation_is_partial(tmp_path, monkeypatch):
    path = tmp_path / "fixture.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    body = {
        "{http://www.ebayclassifiedsgroup.com/schema/picture/v1}picture": {
            "value": {"link": [{"rel": [], "href": "https://img.kleinanzeigen.de/fixture"}]}
        }
    }
    api = ShippingKleinanzeigenAPI(budget=budget(), session=Session([Response(201, body)]))
    monkeypatch.setattr(api, "_headers", lambda **kw: {})
    detail = code("PARTIAL_WRITE", lambda: api.upload_picture(str(path)))
    assert detail.context["phase"] == "picture_uploaded_url_unresolved"


def test_corrected_semantic_token_storage_recovers(tmp_path):
    path = tmp_path / "token.json"
    path.write_text(json.dumps({"refresh_token": "dummy", "expires_at": "bad"}))
    path.chmod(0o600)
    raw = Session()
    r = Runtime(Settings(token_path=path), session_factory=lambda: raw, budget=budget())
    code("AUTH_STORAGE_ERROR", r.account)
    replacement = tmp_path / "fixed.json"
    write_token(replacement, access_token="cached", expires_at=9999999999)
    replacement.replace(path)
    with r.operation(account=True) as api:
        assert api._headers(authed=True)["X-EBAYK-USERID-TOKEN"] == "cached"
    assert not raw.calls
