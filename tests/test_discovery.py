"""Synthetic public/account reads through the real services, transport and MCP boundary."""

import base64
import copy
import hashlib
import json
import time
from pathlib import Path

import httpx
import pytest
from conftest import Clock, Response, Session
from fastmcp import Client
from jsonschema import Draft202012Validator
from kleinanzeigen_api.categories import all_categories
from mcp.types import ImageContent

from kleinanzeigen_mcp.config import Settings
from kleinanzeigen_mcp.discovery import Cache, categories
from kleinanzeigen_mcp.server import create_server
from kleinanzeigen_mcp.services import RuntimeServices
from kleinanzeigen_mcp.transport import RequestBudget
from kleinanzeigen_mcp.upstream import CurlSession, HttpxSession, Runtime, SellerClient

NS = "{http://www.ebayclassifiedsgroup.com/schema/ad/v1}"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
)


def fixture(name):
    return json.loads((Path(__file__).parent / "fixtures" / f"{name}.json").read_text())


def ad(identifier, title="RTX4070", description="128 GB"):
    return {"id": identifier, "title": {"value": title}, "description": {"value": description}}


def page(*ads, total=None):
    block = {"ad": list(ads)}
    if total is not None:
        block["paging"] = {"numFound": total}
    return {NS + "ads": {"value": block}}


class Harness:
    def __init__(self, tmp_path):
        self.clock = Clock()
        self.raw = Session(clock=self.clock)
        self.settings = Settings(token_path=tmp_path / "synthetic-token.json", user_id="700")
        self.runtime = Runtime(
            self.settings,
            budget=RequestBudget(clock=self.clock, sleep=self.clock.sleep, uniform=lambda a, b: 0),
            session_factory=lambda: self.raw,
        )
        self.services = RuntimeServices(self.settings, runtime=self.runtime)
        self.client = Client(create_server(settings=self.settings, services=self.services))

    def responses(self, *bodies):
        self.raw.responses.extend(Response(body=b) if isinstance(b, dict) else b for b in bodies)

    def login(self):
        path = self.settings.token_path
        path.write_text(
            json.dumps(
                {
                    "refresh_token": "synthetic-refresh",
                    "access_token": "synthetic-access",
                    "expires_at": time.time() + 3600,
                }
            )
        )
        path.chmod(0o600)

    async def call(self, name, **kwargs):
        result = await self.client.call_tool("kleinanzeigen_" + name, kwargs, raise_on_error=False)
        payload = result.structured_content
        assert json.loads(result.content[0].text) == payload
        tool = next(t for t in await self.client.list_tools() if t.name == "kleinanzeigen_" + name)
        Draft202012Validator(tool.output_schema).validate(payload)
        assert len(result.content[0].text.encode()) <= 1024 * 1024
        return result

    async def ok(self, name, **kwargs):
        result = await self.call(name, **kwargs)
        assert not result.is_error, result.structured_content
        return result.structured_content

    async def error(self, name, code, **kwargs):
        result = await self.call(name, **kwargs)
        assert result.is_error
        assert result.structured_content["error"]["code"] == code
        return result.structured_content["error"]

    def seller(self, profile, reputation):
        requests = []

        def handler(request):
            requests.append(request)
            item = profile if "profile.json" in request.url.path else reputation
            if isinstance(item, httpx.Response):
                return item
            return httpx.Response(200, json=item)

        self.runtime._seller = SellerClient(
            self.runtime.budget,
            headers=self.runtime.public()._headers(),
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        return requests


@pytest.fixture
async def h(tmp_path):
    harness = Harness(tmp_path)
    async with harness.client:
        yield harness


async def test_listing_jaxb_raw_enrichment_and_cached_images(h):
    h.responses(fixture("listing"), Response(content=PNG, headers={"Content-Type": "image/png"}))
    data = await h.ok("get_listing", listing_id="101")
    assert data["title"] == "RTX4070 & Speicher"
    assert data["description"] == "Sehr gut\n128 GB & Zubehör"
    assert data["description_truncated"] is False
    assert data["price"] == 42.5 and data["currency"] == "EUR"
    assert data["attributes"] == {"Wohnfläche": "90", "Zimmer": "3", "Tags": "one"}
    assert data["attribute_keys"]["synthetic.tags"] == ["one", "two"]
    assert data["size_m2"] == 90 and data["rooms"] == 3
    assert data["seller"] == {
        "seller_id": "999",
        "name": "Synthetic Seller",
        "poster_type": "PRIVATE",
        "account_type": "COMMERCIAL",
    }
    assert data["status"] == "PAUSED" and data["ad_type"] == "OFFERED"
    assert data["buy_now"] is False and data["is_top_ad"] is True
    assert data["shipping_options"] == ["synthetic-shipping"]
    result = await h.call("get_ad_images", listing_id="101", max_images=1)
    assert not result.is_error
    assert isinstance(result.content[1], ImageContent)
    assert base64.b64decode(result.content[1].data) == PNG
    assert result.structured_content["images"][0]["url"].endswith("/test.png")
    assert len(h.raw.calls) == 2
    assert h.raw.calls[1][0] - h.raw.calls[0][0] >= 1.5
    assert h.raw.calls[1][3]["headers"] == {"Accept": "image/*"}
    assert h.runtime._auth is None


async def test_unavailable_booleans_currency_seller_and_detail_ttl(h):
    h.responses(ad("1"), ad("1", title="Changed"))
    one = await h.ok("get_listing", listing_id="1")
    assert one["buy_now"] is one["is_top_ad"] is one["currency"] is one["seller"] is None
    assert (await h.ok("get_listing", listing_id="1"))["title"] == "RTX4070"
    assert len(h.raw.calls) == 1
    h.clock.now += 61
    assert (await h.ok("get_listing", listing_id="1"))["title"] == "Changed"


@pytest.mark.parametrize(
    "body", [{}, {NS + "ad": {"value": {"id": "1"}}}, {NS + "ads": {"value": {}}}]
)
async def test_malformed_success_is_not_empty_resource(h, body):
    h.responses(body)
    await h.error("get_listing", "UPSTREAM_SCHEMA", listing_id="1")


async def test_detail_not_found_and_overlarge(h):
    h.responses(Response(status=404), ad("1", description="x" * (1024 * 1024)))
    await h.error("get_listing", "NOT_FOUND", listing_id="2")
    await h.error("get_listing", "LIMIT_EXCEEDED", listing_id="1")


async def test_search_singleton_total_and_no_detail_hydration(h):
    h.responses(fixture("search"))
    result = await h.ok("search", page_size=1)
    assert result["total"] == 10000 and result["total_relation"] == "at_least"
    assert result["count"] == result["scanned"] == 1 and result["next_page"] == 1
    assert result["has_more"] is True
    assert result["listings"][0]["seller"]["seller_id"] == "999"
    assert len(h.raw.calls) == 1
    params = h.raw.calls[0][3]["params"]
    assert params["includeTopAds"] == "false" and params["limitTotalResultCount"] == "true"


async def test_search_processes_all_survivors_and_continues_same_size(h):
    h.responses(page(ad(1), ad(2)), page(ad(3), ad(4)), page(ad(5)))
    first = await h.ok("search", pages=2, page_size=2)
    assert [x["listing_id"] for x in first["listings"]] == ["1", "2", "3", "4"]
    assert first["count"] == 4 and first["next_page"] == 2 and first["has_more"] is None
    second = await h.ok("search", page=first["next_page"], page_size=2)
    assert [x["listing_id"] for x in second["listings"]] == ["5"]
    assert second["next_page"] is None and second["has_more"] is False
    assert [c[3]["params"]["size"] for c in h.raw.calls] == [2, 2, 2]


async def test_filtered_empty_page_dedup_and_full_text_before_compaction(h):
    h.responses(
        page(ad(1, description="unwanted"), ad(2, description="no match")),
        page(ad(2, description="128gb"), ad(3, description="x" * 200 + " 128gb")),
    )
    data = await h.ok("search", pages=2, page_size=2, require=["128 GB"], summary_chars=100)
    assert data["pages_fetched"] == 2 and data["scanned"] == 4
    assert data["duplicates_removed"] == 1
    assert [x["listing_id"] for x in data["listings"]] == ["3"]
    assert data["listings"][0]["description_truncated"] is True
    assert len(data["listings"][0]["description"]) == 100
    assert data["next_page"] == 2


@pytest.mark.parametrize(
    "title,query",
    [("RTX4070", "RTX 4070"), ("RTX 4070", "RTX4070"), ("Grün RTX4070", "Grun RTX 4070")],
)
async def test_title_fused_model_and_accent_matching(h, title, query):
    h.responses(page(ad(1, title), ad(2, "Other", description=query)))
    data = await h.ok("search", query=query, title_only=True)
    assert [x["listing_id"] for x in data["listings"]] == ["1"]


@pytest.mark.parametrize(
    "scope,mode,expected",
    [("text", "substring", []), ("title", "token", ["1", "3"]), ("title", "substring", ["3"])],
)
async def test_exclude_legacy_and_token_compound_semantics(h, scope, mode, expected):
    h.responses(page(ad(1, "Prozessor"), ad(2, "Phone Pro"), ad(3, "Phone", "Pro")))
    data = await h.ok("search", exclude=["pro"], exclude_scope=scope, exclude_mode=mode)
    assert [x["listing_id"] for x in data["listings"]] == expected
    h.responses(page(ad(4, "Ladekabel")))
    assert (await h.ok("search", exclude=["kabel"], exclude_mode="token"))["count"] == 0


async def test_nonmultiple_scan_cap_and_four_page_200_limit(h):
    h.responses(page(ad(1), ad(2)), page(ad(3), ad(4)))
    result = await h.ok("search", pages=4, page_size=2, scan_cap=5)
    assert result["scanned"] == 4 and result["pages_fetched"] == 2
    assert result["scan_cap_reached"] and result["stop_reason"] == "scan_cap"
    assert result["next_page"] == 2 and len(h.raw.calls) == 2
    h.responses(*(page(*(ad(i + p * 50) for i in range(50))) for p in range(4)))
    data = await h.ok("search", pages=4, page_size=50)
    assert data["scanned"] == 200 and data["count"] == 200
    assert data["pages_fetched"] == 4 and data["next_page"] == 4


async def test_offset_boundary_and_invalid_start(h):
    h.responses(page(*(ad(i) for i in range(30)), total=10000))
    data = await h.ok("search", page=332, pages=4, page_size=30)
    assert data["next_page"] == 333 and data["stop_reason"] == "scan_cap"
    assert len(h.raw.calls) == 1
    await h.error("search", "INVALID_ARGUMENT", page=399, page_size=50)


async def test_failed_second_page_retains_first_and_continuation(h):
    h.responses(page(ad(1)), Response(status=503), Response(status=503), Response(status=503))
    result = await h.ok("search", pages=2, page_size=1)
    assert result["partial"] is True and result["count"] == 1 and result["next_page"] == 1
    assert result["stop_reason"] == "upstream_error"
    assert result["warnings"][0]["code"] == "UPSTREAM_UNAVAILABLE"
    assert len(h.raw.calls) == 4


async def test_byte_cap_retains_only_complete_pages_and_retries_next(h):
    first = page(ad(1, description="x" * 600000))
    second = page(ad(2, description="y" * 600000))
    h.responses(first, second, second)
    result = await h.ok("search", pages=2, page_size=1)
    assert result["count"] == 1 and result["pages_fetched"] == 1 and result["next_page"] == 1
    assert result["partial"] and result["warnings"][0]["code"] == "LIMIT_EXCEEDED"
    next_ = await h.ok("search", page=1, page_size=1)
    assert next_["listings"][0]["listing_id"] == "2"
    h.responses(page(ad(3, description="z" * 1100000)))
    await h.error("search", "LIMIT_EXCEEDED", page_size=1)


async def test_locations_nested_singleton_resolution_cache_and_no_web(h):
    h.responses(fixture("locations"), page())
    result = await h.ok("search_locations", query="Berlin", limit=1)
    assert result["truncated"] and result["locations"][0]["region"] == "Synthetic Region"
    data = await h.ok("search", location="Berlin", distance_km=10)
    assert data["resolved_location"] == {"id": "10", "label": "Berlin"}
    params = h.raw.calls[1][3]["params"]
    assert params["locationId"] == "10" and params["distanceUnit"] == "KM"
    assert len(h.raw.calls) == 2
    assert h.raw.calls[0][3]["params"] == {"depth": 1, "q": "Berlin"}
    h.responses(Response(status=403))
    await h.error("search", "APP_CREDENTIALS_REJECTED", location="Other")
    assert len(h.raw.calls) == 3 and all("/api/" in c[2] for c in h.raw.calls)


async def test_location_exact_prefix_first_selection(h):
    body = fixture("locations")
    root = next(iter(body.values()))["value"]
    root["location"] = [
        {"id": 1, "localized-name": {"value": "Other"}},
        {"id": 2, "localized-name": {"value": "Berlinchen"}},
        {"id": 3, "localized-name": {"value": "Berlin (Region)"}},
    ]
    h.responses(body, page())
    assert (await h.ok("search", location="Berlin"))["resolved_location"]["id"] == "3"
    h.responses(body, page())
    assert (await h.ok("search", location="Berl"))["resolved_location"]["id"] == "2"
    h.responses(body, page())
    assert (await h.ok("search", location="Unknown"))["resolved_location"]["id"] == "1"


async def test_catalog_159_id_consistency_license_and_no_clients(h):
    data = await h.ok("list_categories", limit=159)
    assert data["total"] == data["count"] == 159 and data["next_offset"] is None
    assert {c["id"] for c in data["categories"]} == {str(c.id) for c in all_categories()}
    assert len({c.id for c in categories()}) == 159
    assert h.runtime._public is h.runtime._account is None and not h.raw.calls
    roots = [c for c in data["categories"] if c["parent_id"] is None]
    children = await h.ok("list_categories", parent_id=roots[0]["id"], limit=1)
    assert children["next_offset"] == 1 and children["categories"][0]["parent_id"] == roots[0]["id"]
    await h.error("list_categories", "INVALID_ARGUMENT", parent_id="999999")
    assert (await h.ok("list_categories", query="buro"))["count"] > 0
    assert (await h.ok("list_categories", offset=200))["count"] == 0
    path = Path(__file__).parents[1]
    for filename, digest in {
        "SAMXEL-LICENSE.txt": "754ebfc3ac4c624b21784b534a6fe1f4188f5a3efedbc1fc55da3e5b3f13608d",
        "categories.json": "7a9fc64dbe9b95c0ba27c0f0e1f2d653ab4c208db1ab468788bb1b5868f08c1e",
    }.items():
        assert (
            hashlib.sha256(
                (path / "src/kleinanzeigen_mcp/data" / filename).read_bytes()
            ).hexdigest()
            == digest
        )


async def test_category_resolution_ambiguity_candidates_and_metadata_nested(h):
    h.responses(fixture("metadata"), page())
    result = await h.ok("get_category_filters", category="PC-Zubehör & Software")
    assert result["category_id"] == "225"
    assert result["global_parameters"][0]["values"] == [
        {"value": "PRICE_ASCENDING", "label": "Preis aufsteigend"}
    ]
    assert len(result["attributes"]) == 3
    assert result["attributes"][0]["multi_select"] is True
    assert result["attributes"][1]["multi_select"] is False
    assert result["attributes"][2]["multi_select"] is True
    await h.ok(
        "search",
        category="225",
        attributes={"synthetic.kind": ["one", "two"]},
        zip_code="12345",
        min_price=1,
        max_price=50,
        poster_type="PRIVATE",
        picture_required=True,
        shippable=True,
        buy_now_only=True,
        sort_type="PRICE_ASCENDING",
        ad_type=None,
    )
    assert len(h.raw.calls) == 2  # schema cache, no ignored search extras
    params = h.raw.calls[-1][3]["params"]
    assert params["attr[synthetic.kind]"] == "one,two" and params["priceCurrency"] == "EUR"
    assert params["zipcode"] == "12345" and params["posterType"] == "PRIVATE"
    assert params["shippable"] == params["buyNowOnly"] == params["pictureRequired"] == "true"
    assert "adType" not in params
    for attrs in (
        {"unknown": "one"},
        {"synthetic.condition": ["new"]},
        {"synthetic.kind": "invalid"},
    ):
        await h.error("search", "INVALID_ARGUMENT", category="225", attributes=attrs)
    err = await h.error("get_category_filters", "INVALID_ARGUMENT", category="Elektronik")
    assert err["context"]["candidate_ids"] and len(h.raw.calls) == 2


async def test_metadata_singleton_attribute_and_expiry(h):
    body = fixture("metadata")
    block = next(iter(body.values()))["value"]
    block["attributes"]["attribute"] = block["attributes"]["attribute"][0]
    h.responses(body, body)
    assert len((await h.ok("get_category_filters", category="225"))["attributes"]) == 1
    h.clock.now += 86401
    await h.ok("get_category_filters", category="225")
    assert len(h.raw.calls) == 2


async def test_account_reads_own_status_scoped_routes_paging_and_eligibility(h):
    h.login()
    h.responses(fixture("search"), fixture("listing"), page(), {"unknown": {"eligible": True}})
    my = await h.ok("list_my_ads", page=1, page_size=1, query="synthetic")
    assert my["has_more"] is None and my["next_page"] == 2
    assert my["listings"][0]["status"] == "PAUSED"
    mine = await h.ok("get_my_ad", listing_id="101")
    assert mine["seller"]["seller_id"] == "999" and mine["buy_now"] is False
    saved = await h.ok("watchlist")
    assert saved["count"] == 0 and saved["has_more"] is False
    status = await h.ok("extend_status", listing_ids=["101", "101", "102"])
    assert status["schema_verified"] is False and status["count"] == 2
    assert all(e["eligible"] is e["reason"] is None for e in status["eligibility"])
    assert status["warnings"][0]["code"] == "UNSUPPORTED"
    assert [c[1] for c in h.raw.calls] == ["GET"] * 4
    assert all("/api/users/700/" in c[2] for c in h.raw.calls)
    assert h.raw.calls[0][3]["params"]["q"] == "synthetic"
    assert h.raw.calls[-1][3]["params"] == {"adids": "101,102"}


async def test_account_eur_fallback_and_public_independence_missing_token(h):
    for name, arguments in [
        ("list_my_ads", {}),
        ("get_my_ad", {"listing_id": "1"}),
        ("watchlist", {}),
        ("extend_status", {"listing_ids": ["1"]}),
    ]:
        await h.error(name, "AUTH_REQUIRED", **arguments)
    assert not h.raw.calls
    h.responses(ad(1))
    assert (await h.ok("get_listing", listing_id="1"))["currency"] is None
    h.login()
    h.responses(ad(1))
    assert (await h.ok("get_my_ad", listing_id="1"))["currency"] == "EUR"


async def test_account_writes_require_auth_and_never_construct_clients(h):
    await h.error("reply", "AUTH_REQUIRED", conversation_id="thread", text="hello")
    await h.error("list_conversations", "AUTH_REQUIRED")
    await h.error("update_ad", "AUTH_REQUIRED", listing_id="1", picture_urls=[])
    await h.error("renew_ad", "AUTH_REQUIRED", listing_id="1")
    await h.error(
        "create_ad",
        "AUTH_REQUIRED",
        **dict(title="Title", description="Text", category_id="1", location_id="2", price=1),
    )
    assert h.runtime._public is h.runtime._account is None


async def test_seller_independent_halves_unknown_ratings_and_cache(h):
    calls = h.seller(fixture("profile"), fixture("reputation"))
    data = await h.ok("get_seller_info", seller_id="999")
    assert data["partial"] is False and data["followers"] == 0
    assert data["reputation"]["total"]["star_rating"] == {"upstream": "unknown-scale"}
    await h.ok("get_seller_info", seller_id="999")
    assert len(calls) == 2
    assert all("Bearer" not in r.headers.get("Authorization", "") for r in calls)
    h.clock.now += 901
    await h.ok("get_seller_info", seller_id="999")
    assert len(calls) == 4


@pytest.mark.parametrize("bad_half", ["profile", "reputation"])
async def test_seller_partial_and_total_failure(h, bad_half):
    bad = httpx.Response(403, content=b"IP-Bereich vorubergehend gesperrt")
    args = [fixture("profile"), fixture("reputation")]
    args[0 if bad_half == "profile" else 1] = bad
    calls = h.seller(*args)
    data = await h.ok("get_seller_info", seller_id="999")
    assert data["partial"] and len(calls) == 2
    assert data["warnings"][0]["code"] == (
        "IP_BLOCKED" if bad_half == "profile" else "APP_CREDENTIALS_REJECTED"
    )
    h.runtime._seller.close()
    h.seller({}, {})
    error = await h.error("get_seller_info", "UPSTREAM_SCHEMA", seller_id="998")
    assert error["context"] == {"profile": "UPSTREAM_SCHEMA", "reputation": "UPSTREAM_SCHEMA"}


async def test_malformed_profile_retains_reputation(h):
    h.seller({"contactName": ["invalid"]}, fixture("reputation"))
    data = await h.ok("get_seller_info", seller_id="999")
    assert data["partial"] and data["name"] is None and data["reputation"]["total"]


async def test_images_zero_partial_all_failed_and_start(h):
    h.responses(ad(1))
    assert (await h.ok("get_ad_images", listing_id="1"))["count"] == 0
    raw = fixture("listing")
    pics = next(iter(raw.values()))["value"]["pictures"]
    pics["picture"] = [copy.deepcopy(pics["picture"]) for _ in range(3)]
    h.responses(
        raw,
        Response(status=404),
        Response(content=PNG, headers={"Content-Type": "image/png"}),
        Response(content=PNG, headers={"Content-Type": "text/plain"}),
    )
    result = await h.call("get_ad_images", listing_id="101", max_images=2)
    assert result.structured_content["count"] == 1 and result.structured_content["partial"]
    assert result.structured_content["next_start"] == 2
    assert result.structured_content["warnings"][0]["context"] == {"index": 0}
    assert len(result.content) == 2
    error = await h.error("get_ad_images", "UPSTREAM_SCHEMA", listing_id="101", start=2)
    assert error["context"]["failed_indices"] == [2]


async def test_images_redirect_validation_and_no_credential_leak(h):
    h.responses(
        fixture("listing"), Response(status=302, headers={"Location": "https://evil.example/image"})
    )
    await h.error("get_ad_images", "INVALID_ARGUMENT", listing_id="101")
    assert len(h.raw.calls) == 2
    h.responses(
        Response(status=302, headers={"Location": "/final.png"}),
        Response(content=PNG, headers={"Content-Type": "image/png"}),
    )
    await h.ok("get_ad_images", listing_id="101")
    assert h.raw.calls[-1][2] == "https://img.kleinanzeigen.de/final.png"
    assert h.raw.calls[-1][0] - h.raw.calls[-2][0] >= 1.5


async def test_images_per_image_and_aggregate_budgets(h):
    raw = fixture("listing")
    pics = next(iter(raw.values()))["value"]["pictures"]
    pics["picture"] = [copy.deepcopy(pics["picture"]) for _ in range(5)]
    huge = PNG + b"x" * (5 * 1024 * 1024)
    medium = PNG + b"x" * (4 * 1024 * 1024 - len(PNG))
    h.responses(
        raw,
        *(
            Response(content=b, headers={"Content-Type": "image/png"})
            for b in (huge, medium, medium, medium, medium)
        ),
    )
    data = await h.ok("get_ad_images", listing_id="101", max_images=5)
    assert data["count"] == 3 and data["next_start"] == 4
    assert data["partial"] and data["warnings"][0]["context"]["index"] == 0


def test_cache_ttl_and_capacity():
    clock = Clock()
    cache = Cache(2, 60, clock)
    cache.put("a", 1)
    cache.put("b", 2)
    assert cache.get("a") == 1
    cache.put("c", 3)
    assert cache.get("b") is None and len(cache.items) == 2
    clock.now += 60
    assert cache.get("a") is None


def test_curl_streaming_limit_aborts_without_retaining_excess(monkeypatch):
    from curl_cffi.curl import CURL_WRITEFUNC_ERROR
    from curl_cffi.requests import RequestsError

    session = CurlSession()

    def request(*args, **kwargs):
        callback = kwargs["content_callback"]
        assert callback(b"1234") == 4
        assert callback(b"5678") == CURL_WRITEFUNC_ERROR
        raise RequestsError("synthetic-body-must-not-escape")

    monkeypatch.setattr(session.session, "request", request)
    with pytest.raises(Exception) as exc:
        session.request("GET", "https://img.kleinanzeigen.de/test", max_response_bytes=5)
    assert exc.value.detail.code == "LIMIT_EXCEEDED"
    session.close()


def test_httpx_streaming_limit():
    from kleinanzeigen_mcp.models import DomainError

    session = HttpxSession(
        httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"123456"))
        )
    )
    with pytest.raises(DomainError) as exc:
        session.request("GET", "https://synthetic.invalid/test", max_response_bytes=5)
    assert exc.value.detail.code == "LIMIT_EXCEEDED"
    session.close()


async def test_safe_smoke_runs_reviewed_reads_only_with_synthetic_fixtures(capsys):
    from test_stdio import FakeServices

    import smoke_test

    class Recorded(FakeServices):
        def __init__(self):
            self.calls = []

        def invoke(self, name, arguments):
            self.calls.append((name, arguments))
            if name == "extend_status":
                # Only this fake describes verified eligibility; the server never does.
                return {
                    "count": 1,
                    "eligibility": [{"listing_id": "1", "eligible": True, "reason": None}],
                    "schema_verified": True,
                }
            return super().invoke(name, arguments)

    service = Recorded()
    args = smoke_test.parser().parse_args(["--seller-id", "1"])
    async with Client(create_server(services=service)) as client:
        code = await smoke_test.run(client, args)
    assert code == 0
    assert {name for name, _ in service.calls} == smoke_test.SAFE
    assert len(service.calls) == 12
    supplied = dict(service.calls)
    assert supplied["search"]["pages"] == supplied["search"]["page_size"] == 1
    assert supplied["search"]["summary_chars"] == 400
    assert supplied["get_ad_images"]["max_images"] == 1
    output = capsys.readouterr().out
    assert "Fixture" not in output and "Grüße" not in output


async def test_safe_smoke_public_only_skips_and_refuses_unreviewed_tools(capsys):
    from test_stdio import FakeServices

    import smoke_test

    async with Client(create_server(services=FakeServices())) as client:
        args = smoke_test.parser().parse_args(["--public-only", "--seller-id", "1"])
        assert await smoke_test.run(client, args) == 2
        output = capsys.readouterr().out
        assert output.count("SKIP ") == 5

        class Unreviewed:
            async def list_tools(self):
                tools = await client.list_tools()
                tools.append(tools[0].model_copy(update={"name": "kleinanzeigen_unknown_read"}))
                return tools

            async def call_tool(self, *args, **kwargs):
                pytest.fail("No call allowed after inventory changes")

        assert await smoke_test.run(Unreviewed(), args) == 1
        assert "unreviewed" in capsys.readouterr().out

        class WrongAnnotation(Unreviewed):
            async def list_tools(self):
                tools = await client.list_tools()
                next(
                    t for t in tools if t.name == "kleinanzeigen_search"
                ).annotations.read_only_hint = False
                return tools

        assert await smoke_test.run(WrongAnnotation(), args) == 1
        assert "annotation changed" in capsys.readouterr().out


async def test_search_deadline_keeps_completed_page(h, monkeypatch):
    original = h.raw.request

    def delayed(*args, **kwargs):
        result = original(*args, **kwargs)
        h.clock.now += 181
        return result

    monkeypatch.setattr(h.raw, "request", delayed)
    h.responses(page(ad(1)))
    data = await h.ok("search", pages=2, page_size=1)
    assert data["partial"] and data["stop_reason"] == "deadline"
    assert data["next_page"] == 1 and data["count"] == 1
    assert len(h.raw.calls) == 1


async def test_metadata_request_is_validated_before_first_search(h):
    h.responses(fixture("metadata"), page())
    await h.ok("search", category="225", attributes={"synthetic.kind": ["one"]})
    assert len(h.raw.calls) == 2
    assert h.raw.calls[0][2].endswith("/search-metadata/225.json")
    assert h.raw.calls[1][3]["params"]["attr[synthetic.kind]"] == "one"
    assert h.raw.calls[1][0] - h.raw.calls[0][0] >= 1.5


async def test_bad_second_search_page_retains_continuation_and_raw_page(h):
    malformed = ad(2)
    malformed["buy-now"] = {"selected": "not-a-boolean"}
    h.responses(page(ad(1)), page(malformed))
    data = await h.ok("search", pages=2, page_size=1)
    assert data["count"] == 1 and data["partial"] and data["next_page"] == 1
    assert data["warnings"][0]["code"] == "UPSTREAM_SCHEMA"
    assert data["pages_fetched"] == 1


async def test_account_page_byte_limit_never_silently_drops_rows(h):
    h.login()
    h.responses(page(ad(1, description="x" * 600000), ad(2, description="y" * 600000)))
    await h.error("list_my_ads", "LIMIT_EXCEEDED", page_size=2)


async def test_malformed_nested_metadata_is_typed_not_empty(h):
    body = fixture("metadata")
    block = next(iter(body.values()))["value"]
    block["attributes"]["attribute"][0]["supported-value"] = {"value": {"unexpected": "object"}}
    h.responses(body, {NS + "ads-search-options": {"value": {}}})
    await h.error("get_category_filters", "UPSTREAM_SCHEMA", category="225")
    await h.error("get_category_filters", "UPSTREAM_SCHEMA", category="225")
    assert not h.services.discovery.schemas.items


async def test_alias_numeric_ids_obey_shared_length_contract(h):
    await h.error("get_category_filters", "INVALID_ARGUMENT", category="1" * 33)
    await h.error("search", "INVALID_ARGUMENT", location="1" * 33)
    assert not h.raw.calls


async def test_seller_cached_half_survives_other_half_failure_and_expires_independently(h):
    calls = h.seller(httpx.Response(403), fixture("reputation"))
    first = await h.ok("get_seller_info", seller_id="999")
    assert first["warnings"][0]["code"] == "APP_CREDENTIALS_REJECTED"
    h.clock.now += 100
    h.runtime._seller.close()
    recovered = h.seller(fixture("profile"), httpx.Response(403))
    assert not (await h.ok("get_seller_info", seller_id="999"))["partial"]
    assert len(recovered) == 1 and "profile.json" in recovered[0].url.path
    h.clock.now += 802
    data = await h.ok("get_seller_info", seller_id="999")
    assert data["partial"] and data["name"] == "Synthetic Seller"
    assert len(calls) == 2 and len(recovered) == 2
    assert "reputation-summary" in recovered[-1].url.path


async def test_generic_profile_refusal_is_not_guessed_ip_block(h):
    h.seller(
        httpx.Response(403, content=b"Account blocked. Access gesperrt."), fixture("reputation")
    )
    data = await h.ok("get_seller_info", seller_id="999")
    assert data["warnings"][0]["code"] == "APP_CREDENTIALS_REJECTED"


async def test_image_redirects_share_ten_get_cap_and_keep_continuation(h):
    body = fixture("listing")
    pics = next(iter(body.values()))["value"]["pictures"]
    pics["picture"] = [copy.deepcopy(pics["picture"]) for _ in range(5)]
    h.responses(body)
    for _ in range(4):
        h.responses(
            Response(status=302, headers={"Location": "/one.png"}),
            Response(status=302, headers={"Location": "/two.png"}),
            Response(content=PNG, headers={"Content-Type": "image/png"}),
        )
    data = await h.ok("get_ad_images", listing_id="101", max_images=5)
    assert len(h.raw.calls) == 11  # one detail plus ten CDN GETs
    assert data["count"] == 3 and data["next_start"] == 4 and data["partial"]
    assert data["warnings"][0]["context"] == {"index": 3}


async def test_invalid_image_port_is_partial_without_requesting_it(h):
    body = fixture("listing")
    pics = next(iter(body.values()))["value"]["pictures"]
    pics["picture"] = [
        {"link": {"rel": "large", "href": "https://img.kleinanzeigen.de:bad/image"}},
        copy.deepcopy(pics["picture"]),
    ]
    h.responses(body, Response(content=PNG, headers={"Content-Type": "image/png"}))
    data = await h.ok("get_ad_images", listing_id="101")
    assert data["partial"] and data["count"] == 1 and len(h.raw.calls) == 2


async def test_exact_fifteen_mebibytes_stops_before_serialization_cap(h):
    body = fixture("listing")
    pics = next(iter(body.values()))["value"]["pictures"]
    pics["picture"] = [copy.deepcopy(pics["picture"]) for _ in range(3)]
    data = PNG + b"x" * (5 * 1024 * 1024 - len(PNG))
    h.responses(
        body, *(Response(content=data, headers={"Content-Type": "image/png"}) for _ in range(3))
    )
    result = await h.ok("get_ad_images", listing_id="101", max_images=3)
    assert result["count"] == 2 and result["next_start"] == 2


async def test_safe_smoke_missing_login_skips_without_repeated_account_calls(capsys):
    from test_stdio import FakeServices

    import smoke_test
    from kleinanzeigen_mcp.models import DomainError

    class MissingLogin(FakeServices):
        account_calls = []

        def invoke(self, name, arguments):
            if name in smoke_test.ACCOUNT:
                self.account_calls.append(name)
                raise DomainError("AUTH_REQUIRED")
            return super().invoke(name, arguments)

    service = MissingLogin()
    async with Client(create_server(services=service)) as client:
        assert (
            await smoke_test.run(client, smoke_test.parser().parse_args(["--seller-id", "1"])) == 2
        )
    assert service.account_calls == ["list_my_ads"]
    assert capsys.readouterr().out.count("SKIP ") == 5


@pytest.mark.parametrize(
    "method,auth_exchange,expected",
    [
        ("GET", False, "LIMIT_EXCEEDED"),
        ("POST", True, "LIMIT_EXCEEDED"),
        ("POST", False, "PARTIAL_WRITE"),
    ],
)
def test_stream_abort_preserves_pacing_and_read_auth_write_classification(
    method, auth_exchange, expected
):
    from kleinanzeigen_mcp.models import DomainError
    from kleinanzeigen_mcp.transport import PacedSession

    clock = Clock()
    raw = Session([DomainError("LIMIT_EXCEEDED")], clock=clock)
    budget = RequestBudget(clock=clock, sleep=clock.sleep, uniform=lambda a, b: 0)
    session = PacedSession(raw, budget, auth_exchange=auth_exchange)
    with pytest.raises(DomainError) as exc:
        session.request(method, "https://synthetic.invalid/resource")
    assert exc.value.detail.code == expected and len(raw.calls) == 1
    assert budget._ready_at == clock.now + 1.5


def test_curl_stream_callback_returns_downloaded_content(monkeypatch):
    session = CurlSession()

    def request(*args, **kwargs):
        assert kwargs["allow_redirects"] is False
        assert kwargs["content_callback"](b"1234") == 4
        assert kwargs["content_callback"](b"5") == 1
        return Response()

    monkeypatch.setattr(session.session, "request", request)
    assert (
        session.request("GET", "https://synthetic.invalid/resource", max_response_bytes=5).content
        == b"12345"
    )
    session.close()


def test_httpx_bounded_stream_keeps_decoded_json():
    import gzip

    payload = json.dumps(fixture("profile")).encode()
    session = HttpxSession(
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    content=gzip.compress(payload),
                    headers={"Content-Encoding": "gzip", "Content-Type": "application/json"},
                )
            )
        )
    )
    response = session.request("GET", "https://synthetic.invalid/profile")
    assert response.json() == fixture("profile")
    assert response.content == payload
    session.close()


def test_empty_jaxb_nodes_normalize_to_absent():
    from kleinanzeigen_mcp.discovery import boolean, number, numeric_id, rows, text
    from kleinanzeigen_mcp.models import DomainError

    assert text({}) is None and text({"value": {}}) is None
    assert number({}) is None and boolean({}) is None
    assert numeric_id({}) is None and rows({}) == []
    with pytest.raises(DomainError):
        numeric_id({}, required=True)


async def test_live_jaxb_empty_store_title_and_scalar_metadata_markers(h):
    """Live 2026-10-16: search ads carry `store-title: {}` (JAXB nil) and the
    search-options block carries list-valued `histograms` markers; both must be
    treated as absent/irrelevant instead of failing the whole parse."""
    body = fixture("metadata")
    options = next(iter(body.values()))["value"]
    options["histograms"] = {"value": []}
    options["histogramsExpand"] = {"value": []}
    h.responses(
        page(
            {
                "id": "9",
                "title": {"value": "Titel"},
                "description": {"value": "Text"},
                "store-title": {},
            }
        ),
        body,
    )
    result = await h.ok("search", query="Fahrrad", pages=1, page_size=1)
    assert result["count"] == 1 and result["listings"][0]["seller"] is None
    filters = await h.ok("get_category_filters", category="225")
    keys = {p["key"] for p in filters["global_parameters"]}
    assert "histograms" not in keys and "histogramsExpand" not in keys
    assert "sortType" in keys
