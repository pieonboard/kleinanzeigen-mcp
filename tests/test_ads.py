"""Offline ad writes. Update request is reconstructed, never a live capture.

The extend-status fixture is a sanitized copy of the shape observed live on
2026-10-16 (docs/validation-2026-10-16.md).
"""

import json
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from conftest import Clock, Response, Session
from fastmcp import Client
from jsonschema import Draft202012Validator

from kleinanzeigen_mcp.config import Settings
from kleinanzeigen_mcp.server import create_server
from kleinanzeigen_mcp.services import RuntimeServices
from kleinanzeigen_mcp.transport import RequestBudget
from kleinanzeigen_mcp.upstream import Runtime

BASE = "https://api.kleinanzeigen.de/api/users/700/ads"
NS = {
    k: f"http://www.ebayclassifiedsgroup.com/schema/{k if k != 'pic' else 'picture'}/v1"
    for k in ("ad", "types", "pic", "shipping", "attribute", "category", "location", "payment")
}
CREATE = dict(
    title="  Tisch & Stuhl  ",
    description="  Erste Zeile\n\nZweite <Zeile>  ",
    category_id="10",
    location_id="20",
    price=42.10,
)


def fixture(name):
    return json.loads((Path(__file__).parent / "fixtures" / f"{name}.json").read_text())


class Harness:
    def __init__(self, tmp_path, **settings):
        self.clock = Clock()
        self.raw = Session(clock=self.clock)
        self.settings = Settings(token_path=tmp_path / "dummy.json", user_id="700", **settings)
        self.settings.token_path.write_text(
            json.dumps(
                {
                    "refresh_token": "synthetic-refresh",
                    "access_token": "synthetic-access",
                    "email": "fixture@example.invalid",
                    "expires_at": time.time() + 3600,
                }
            )
        )
        self.settings.token_path.chmod(0o600)
        self.runtime = Runtime(
            self.settings,
            session_factory=lambda: self.raw,
            budget=RequestBudget(clock=self.clock, sleep=self.clock.sleep, uniform=lambda a, b: 0),
        )
        self.services = RuntimeServices(self.settings, runtime=self.runtime)
        self.client = Client(create_server(settings=self.settings, services=self.services))

    def responses(self, *bodies):
        self.raw.responses.extend(
            Response(body=b) if isinstance(b, (dict, list)) else b for b in bodies
        )

    async def call(self, name, **args):
        result = await self.client.call_tool("kleinanzeigen_" + name, args, raise_on_error=False)
        assert json.loads(result.content[0].text) == result.structured_content
        schema = next(
            t.output_schema
            for t in await self.client.list_tools()
            if t.name == "kleinanzeigen_" + name
        )
        Draft202012Validator(schema).validate(result.structured_content)
        return result

    async def ok(self, name, **args):
        result = await self.call(name, **args)
        assert not result.is_error, result.structured_content
        return result.structured_content

    async def error(self, name, code, **args):
        result = await self.call(name, **args)
        assert result.is_error
        detail = result.structured_content["error"]
        assert detail["code"] == code, detail
        return detail


@pytest.fixture
async def h(tmp_path):
    harness = Harness(tmp_path)
    async with harness.client:
        yield harness


async def test_create_full_xml_post_and_shipping_cleanup(h):
    h.responses(Response(201, headers={"Location": BASE + "/123.json"}))
    result = await h.ok(
        "create_ad",
        **CREATE,
        contact_name="P & Q",
        phone="012345",
        attributes={"condition": "<gut>", "count": 2, "flag": True},
        picture_urls=["https://img.kleinanzeigen.de/a.jpg?a=1&b=2"],
        shipping_options=['observed-"&<>'],
    )
    assert result["action"] == "ad_created" and result["listing_id"] == "123"
    _, method, url, kw = h.raw.calls[0]
    assert method == "POST" and url == BASE + ".json"
    assert kw["headers"]["Content-Type"] == "application/xml"
    assert "Authorization" in kw["headers"]
    xml = kw["data"].decode()
    root = ET.fromstring(xml)
    assert root.find("ad:title", NS).text == "Tisch & Stuhl"
    assert root.find("ad:description", NS).text == "Erste Zeile\n\nZweite <Zeile>"
    assert root.find("ad:price/types:amount", NS).text == "42.1"
    assert root.find("ad:price/types:price-type/types:value", NS).text == "SPECIFIED_AMOUNT"
    assert root.find("ad:poster-type/ad:value", NS).text == "PRIVATE"
    assert root.find("ad:ad-type/ad:value", NS).text == "OFFERED"
    assert root.find("payment:buy-now", NS).attrib == {"selected": "false"}
    assert root.find("ad:email", NS).text == "fixture@example.invalid"
    assert root.find("attribute:attributes/attribute:attribute/attribute:value", NS).text == "<gut>"
    assert list(root)[-1].tag == "{" + NS["shipping"] + "}shipping-options"
    assert list(root)[-1][0].attrib == {"id": 'observed-"&<>'}
    assert not hasattr(h.runtime.account(), "_posting_shipping_options")


@pytest.mark.parametrize(
    "body",
    [
        {"id": 123},
        {"id": {"value": "123"}},
        {"{http://www.ebayclassifiedsgroup.com/schema/ad/v1}ad": {"value": {"id": "123"}}},
    ],
)
async def test_create_numeric_id_body(h, body):
    h.responses(Response(201, body))
    assert (await h.ok("create_ad", **CREATE))["listing_id"] == "123"


@pytest.mark.parametrize(
    "body",
    [
        {},
        [],
        {"id": ""},
        {"id": "oops"},
        {"id": "١٢"},
        {"id": True},
        ValueError("private upstream body"),
    ],
)
async def test_create_acknowledged_without_usable_id_is_partial(h, body):
    h.responses(Response(201, body))
    detail = await h.error("create_ad", "PARTIAL_WRITE", **CREATE)
    assert detail["context"]["phase"] == "ad_created_id_unresolved"
    assert detail["retryable"] is False and len(h.raw.calls) == 1
    assert not hasattr(h.runtime.account(), "_posting_shipping_options")


async def test_create_missing_email_rejected_before_post(h):
    api = h.runtime.account()
    api._auth_provider._t.pop("email", None)
    detail = await h.error("create_ad", "INVALID_ARGUMENT", **CREATE)
    assert "email" in detail["message"] and "search_locations" in detail["hint"]
    assert h.raw.calls == []


@pytest.mark.parametrize(
    "patch",
    [
        {"title": " "},
        {"title": "t" * 201},
        {"description": "d" * 20001},
        {"description": "bad\x00"},
        {"title": "bad\ud800"},
        {"category_id": "../1"},
        {"location_id": ""},
        {"price": None},
        {"price": -1},
        {"price": 1.001},
        {"price": float("inf")},
        {"price_type": "FREE", "price": 2},
        {"attributes": {"bad": []}},
        {"attributes": {"bad": {"nested": "x"}}},
        {"attributes": {"bad": None}},
        {"attributes": {"bad": float("inf")}},
        {"attributes": {"bad": "x" * 2001}},
        {"attributes": {"bad\x00": "x"}},
        {"attributes": {"bad": "\ud800"}},
        {"attributes": {str(i): i for i in range(51)}},
        {"contact_name": "n" * 201},
        {"phone": "p" * 51},
        {"shipping_options": ["a"] * 21},
        {"shipping_options": ["x" * 101]},
        {"shipping_options": ["\x00"]},
    ],
)
async def test_create_validation_no_requests(h, patch):
    await h.error("create_ad", "INVALID_ARGUMENT", **(CREATE | patch))
    assert h.raw.calls == []


@pytest.mark.parametrize("price", [None, 0])
async def test_create_free_omits_amount(h, price):
    h.responses({"id": "123"})
    await h.ok("create_ad", **(CREATE | {"price_type": "FREE", "price": price}))
    root = ET.fromstring(h.raw.calls[0][3]["data"])
    assert root.find("ad:price/types:amount", NS) is None
    assert root.find("ad:price/types:price-type/types:value", NS).text == "FREE"


async def test_update_exact_reconstructed_request(h):
    f = fixture("update_request")
    assert "RECONSTRUCTION" in f["provenance"] and "not a live capture" in f["provenance"]
    result = await h.ok("update_ad", **f["arguments"])
    assert result["fields"] == ["title", "description", "price", "price_type", "picture_urls"]
    _, method, url, kw = h.raw.calls[0]
    assert (method, url) == (f["method"], f["url"])
    assert kw["headers"]["Content-Type"] == f["content_type"]
    assert kw["data"].decode() == f["body"]
    assert len(h.raw.calls) == 1


@pytest.mark.parametrize(
    "field,value,tag",
    [
        ("title", "  Neu & schön  ", "ad:title"),
        ("description", "  New & safe\n\nAbsatz  ", "ad:description"),
        ("picture_urls", [], "pic:pictures"),
        ("price_type", "FREE", "ad:price"),
    ],
)
async def test_update_selective_no_unmodified_fields_or_get(h, field, value, tag):
    result = await h.ok("update_ad", listing_id="123", **{field: value})
    assert result["fields"] == [field]
    root = ET.fromstring(h.raw.calls[0][3]["data"])
    assert len(root) == 1 and list(root)[0] == root.find(tag, NS)
    if field == "picture_urls":
        assert len(list(root)[0]) == 0  # Explicit removal; omission means unchanged.
    assert "shipping" not in h.raw.calls[0][3]["data"].decode()
    assert len(h.raw.calls) == 1


async def test_historical_price_only_update_preserves_type_and_does_not_replace(h):
    h.responses(
        {
            "id": "123",
            "ad-status": "PAUSED",
            "price": {"amount": 10, "price-type": {"value": "PLEASE_CONTACT"}},
            "shipping-options": {"unknown": "retain"},
            "unknown": "retain",
        }
    )
    result = await h.ok("update_ad", listing_id="123", description="New & safe", price=42)
    assert result["fields"] == ["description", "price"]
    assert [c[1] for c in h.raw.calls] == ["GET", "PUT"]
    assert all(c[2] == BASE + "/123.json" for c in h.raw.calls)
    body = h.raw.calls[-1][3]["data"].decode()
    assert "New &amp; safe" in body and "<types:amount>42</types:amount>" in body
    assert "price-type" not in body and "shipping" not in body and "unknown" not in body
    assert h.raw.calls[1][0] - h.raw.calls[0][0] >= 1.5


@pytest.mark.parametrize("price_type", ["FIXED", "NEGOTIABLE"])
async def test_update_type_uses_existing_effective_price(h, price_type):
    h.responses({"id": "123", "price": {"amount": 12.34, "price-type": "FREE"}})
    result = await h.ok("update_ad", listing_id="123", price_type=price_type)
    assert result["fields"] == ["price_type"]
    assert b"<types:amount>12.34</types:amount>" in h.raw.calls[-1][3]["data"]


async def test_update_missing_effective_price_stops_after_read(h):
    h.responses({"id": "123", "price": {"price-type": "FREE"}})
    detail = await h.error("update_ad", "INVALID_ARGUMENT", listing_id="123", price_type="FIXED")
    assert "Supply a price" in detail["hint"] and len(h.raw.calls) == 1


async def test_update_unknown_existing_type_is_not_guessed(h):
    h.responses({"id": "123"})
    await h.error("update_ad", "UPSTREAM_SCHEMA", listing_id="123", price=42)
    assert len(h.raw.calls) == 1


async def test_update_free_requires_explicit_type_for_nonzero_amount(h):
    h.responses({"id": "123", "price": {"price-type": "FREE"}})
    await h.error("update_ad", "INVALID_ARGUMENT", listing_id="123", price=42)
    assert len(h.raw.calls) == 1
    h.responses({"id": "123", "price": {"price-type": "FREE"}})
    await h.ok("update_ad", listing_id="123", price=0)
    assert b"<types:amount>" not in h.raw.calls[-1][3]["data"]


@pytest.mark.parametrize(
    "patch",
    [
        {},
        {"title": " "},
        {"description": "\x00"},
        {"price": 1.001},
        {"price": -1},
        {"price_type": "FREE", "price": 1},
    ],
)
async def test_update_invalid_before_requests(h, patch):
    await h.error("update_ad", "INVALID_ARGUMENT", listing_id="123", **patch)
    assert h.raw.calls == []


@pytest.mark.parametrize("name", ["create_ad", "update_ad"])
@pytest.mark.parametrize(
    "urls",
    [
        ["http://img.kleinanzeigen.de/a.jpg"],
        ["https://elsewhere.invalid/a.jpg"],
        ["https://user:pass@img.kleinanzeigen.de/a.jpg"],
        ["https://img.kleinanzeigen.de:444/a.jpg"],
        ["https://img.kleinanzeigen.de:bad/a.jpg"],
        ["https://img.kleinanzeigen.de/a\x7f.jpg"],
        ["https://img.kleinanzeigen.de/a\n.jpg"],
        ["https://img.kleinanzeigen.de/" + "x" * 2048],
        ["https://img.kleinanzeigen.de/a.jpg"] * 21,
    ],
)
async def test_picture_url_validation(h, name, urls):
    args = CREATE if name == "create_ad" else {"listing_id": "123"}
    await h.error(name, "INVALID_ARGUMENT", **args, picture_urls=urls)
    assert h.raw.calls == []


@pytest.mark.parametrize(
    "name,method,suffix,action",
    [
        ("pause_ad", "PUT", "/paused/123.json", "ad_paused"),
        ("activate_ad", "PUT", "/active/123.json", "ad_activated"),
        ("delete_ad", "DELETE", "/123", "ad_deleted"),
    ],
)
async def test_owned_actions_exact_routes(h, name, method, suffix, action):
    h.responses(Response(204))
    result = await h.ok(name, listing_id="123")
    assert result["ok"] and result["action"] == action and result["listing_id"] == "123"
    assert len(h.raw.calls) == 1
    assert h.raw.calls[0][1:3] == (method, BASE + suffix)


@pytest.mark.parametrize("name", ["create_ad", "update_ad", "pause_ad", "activate_ad", "delete_ad"])
@pytest.mark.parametrize(
    "response,code",
    [
        (TimeoutError("secret"), "WRITE_OUTCOME_UNKNOWN"),
        (Response(503), "WRITE_OUTCOME_UNKNOWN"),
        (Response(429), "RATE_LIMITED"),
        (Response(400), "INVALID_ARGUMENT"),
        (Response(404), "NOT_FOUND"),
    ],
)
async def test_ad_write_one_attempt_and_no_compensation(h, name, response, code):
    h.responses(response)
    args = CREATE if name == "create_ad" else {"listing_id": "123"}
    if name == "update_ad":
        args = args | {"title": "Replacement"}
    detail = await h.error(name, code, **args)
    assert len(h.raw.calls) == 1
    if code == "WRITE_OUTCOME_UNKNOWN":
        assert detail["retryable"] is False
    assert not hasattr(h.runtime._account, "_posting_shipping_options")


async def test_renew_checks_status_and_never_guesses_eligibility(h):
    # Intentionally no fabricated raw eligibility shape, even a generic null is unknown.
    response = Response()
    response.body = None
    h.responses(response)
    detail = await h.error("renew_ad", "UNSUPPORTED", listing_id="123")
    assert detail["context"] == {"listing_id": "123", "phase": "eligibility_unverified"}
    assert "unverified" in detail["hint"] and "No extension was attempted" in detail["hint"]
    assert len(h.raw.calls) == 1
    assert h.raw.calls[0][1:3] == ("GET", BASE + "/extend/status")
    assert h.raw.calls[0][3]["params"] == {"adids": "123"}


async def test_extend_status_unmapped_shape_falls_back_to_null_warning(h):
    # A response without the observed shape keeps the honest null result.
    response = Response()
    response.body = None
    h.responses(response)
    result = await h.ok("extend_status", listing_ids=["123", "123", "124"])
    assert result["schema_verified"] is False and result["count"] == 2
    assert all(e["eligible"] is None and e["reason"] is None for e in result["eligibility"])
    assert result["warnings"] and "unknown" in result["warnings"][0]["hint"]
    assert h.raw.calls[0][3]["params"] == {"adids": "123,124"}


@pytest.fixture
def mime(monkeypatch):
    parts, closed = [], []

    class Mime:
        def addpart(self, **kwargs):
            parts.append(kwargs)

        def close(self):
            closed.append(True)

    monkeypatch.setattr("kleinanzeigen_mcp.upstream.CurlMime", Mime)
    return parts, closed


@pytest.mark.parametrize(
    "head,ctype",
    [
        (b"\xff\xd8\xff", "image/jpeg"),
        (b"\x89PNG\r\n\x1a\n", "image/png"),
        (b"GIF89a", "image/gif"),
        (b"RIFF0000WEBP", "image/webp"),
    ],
)
async def test_upload_wired_detects_mime_and_uses_paced_multipart(h, tmp_path, mime, head, ctype):
    image = tmp_path / "image.bin"
    image.write_bytes(head + b"synthetic")
    h.responses(
        Response(201, fixture("picture_upload")),
        Response(200, {"url": "https://img.kleinanzeigen.de/a.jpg"}),
    )
    await h.ok("upload_picture", image_path=str(image), filename="uploaded.bin", content_type=ctype)
    await h.ok("upload_picture", image_path=str(image))
    parts, closed = mime
    assert parts[0] == {
        "name": "file",
        "content_type": ctype,
        "filename": "uploaded.bin",
        "local_path": str(image.resolve()),
    }
    assert len(closed) == len(h.raw.calls) == 2
    assert h.raw.calls[0][1:3] == ("POST", "https://api.kleinanzeigen.de/api/pictures")
    assert "Authorization" in h.raw.calls[0][3]["headers"]
    assert h.raw.calls[1][0] - h.raw.calls[0][0] >= 1.5


@pytest.mark.parametrize(
    "kind",
    [
        "missing",
        "directory",
        "nonimage",
        "oversized",
        "mime",
        "filename_path",
        "filename_control",
        "filename_length",
        "root",
        "symlink_escape",
    ],
)
async def test_upload_file_checks_no_wire(h, tmp_path, mime, kind):
    image = tmp_path / "image.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    args = {"image_path": str(image)}
    if kind == "missing":
        image.unlink()
    elif kind == "directory":
        args["image_path"] = str(tmp_path)
    elif kind == "nonimage":
        image.write_text("not an image")
    elif kind == "oversized":
        with image.open("r+b") as f:
            f.truncate(20 * 1024 * 1024 + 1)
    elif kind == "mime":
        args["content_type"] = "image/jpeg"
    elif kind.startswith("filename"):
        args["filename"] = {
            "filename_path": "../x.png",
            "filename_control": "x\n.png",
            "filename_length": "x" * 256,
        }[kind]
    else:
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        object.__setattr__(h.settings, "upload_roots", (allowed,))
        if kind == "symlink_escape":
            link = allowed / "link.png"
            link.symlink_to(image)
            args["image_path"] = str(link)
    await h.error("upload_picture", "INVALID_ARGUMENT", **args)
    assert h.raw.calls == [] and mime == ([], [])


@pytest.mark.parametrize(
    "response,code",
    [
        (Response(201, {}), "PARTIAL_WRITE"),
        (Response(201, ValueError("private")), "PARTIAL_WRITE"),
        (Response(202), "WRITE_OUTCOME_UNKNOWN"),
        (Response(503), "WRITE_OUTCOME_UNKNOWN"),
        (TimeoutError("private"), "WRITE_OUTCOME_UNKNOWN"),
    ],
)
async def test_upload_failure_closes_mime_no_replay(h, tmp_path, mime, response, code):
    image = tmp_path / "image.gif"
    image.write_bytes(b"GIF87a synthetic")
    h.responses(response)
    await h.error("upload_picture", code, image_path=str(image))
    assert len(h.raw.calls) == len(mime[1]) == 1


async def test_extend_status_maps_evidenced_negative_status(h):
    # Sanitized live shape: eligibleToExtend is a DATE; the signal is extendAdStatus.
    h.responses(Response(body=fixture("extend_status")))
    result = await h.ok("extend_status", listing_ids=["123"])
    (entry,) = result["eligibility"]
    assert entry["eligible"] is False
    assert "CANNOT_BE_EXTENDED" in entry["reason"] and "25.11.2026" in entry["reason"]
    assert result["schema_verified"] is True and result["warnings"] == []


async def test_extend_status_unknown_and_missing_ids_are_typed(h):
    h.responses(
        Response(
            body=[
                {
                    "adId": 123,
                    "extendAdStatus": "SYNTHETIC_NEW",
                    "eligibleToExtend": "01.01.2027",
                }
            ]
        )
    )
    result = await h.ok("extend_status", listing_ids=["123", "124"])
    by_id = {e["listing_id"]: e for e in result["eligibility"]}
    assert by_id["123"]["eligible"] is None and by_id["123"]["reason"] == "SYNTHETIC_NEW"
    assert by_id["124"]["eligible"] is None and by_id["124"]["reason"] is None
    assert {w["code"] for w in result["warnings"]} == {"UNSUPPORTED", "UPSTREAM_SCHEMA"}
    assert result["schema_verified"] is True


async def test_renew_ad_negative_status_reports_date_and_never_extends(h):
    h.responses(Response(body=fixture("extend_status")))
    detail = await h.error("renew_ad", "UNSUPPORTED", listing_id="123")
    assert detail["context"] == {"listing_id": "123", "phase": "eligibility_negative"}
    assert "CANNOT_BE_EXTENDED" in detail["hint"] and "25.11.2026" in detail["hint"]
    assert "No extension was attempted" in detail["hint"]
    assert len(h.raw.calls) == 1
