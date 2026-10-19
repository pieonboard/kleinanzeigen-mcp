"""Real stdio processes; fakes and network guards stay outside the wheel."""

import asyncio
import base64
import json
import os
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
)
LISTING = {
    "listing_id": "1",
    "title": "Fixture",
    "description": "Vollständiger Text & Grüße",
    "description_truncated": False,
    "price": 42,
    "currency": "EUR",
    "price_type": "FIXED",
    "location": {"city": None, "postal_code": None, "latitude": None, "longitude": None},
    "url": None,
    "created_at": None,
    "updated_at": None,
    "status": None,
    "images": [],
    "seller": None,
    "category_id": "1",
    "attributes": {},
    "attribute_keys": {},
    "size_m2": None,
    "rooms": None,
    "ad_type": None,
    "shipping_options": [],
    "buy_now": None,
    "is_top_ad": None,
}
CONVERSATION = {
    "conversation_id": "thread",
    "listing_id": "1",
    "listing_title": "Fixture",
    "listing_status": None,
    "listing_price": None,
    "currency": None,
    "listing_price_type": None,
    "role": "BUYER",
    "counterparty": None,
    "unread": None,
    "unread_count": None,
    "last_message_at": None,
    "last_message_preview": None,
}
PAGE = {"page": 0, "count": 1, "next_page": None, "has_more": False, "listings": [LISTING]}
SAMPLES = {
    "search": (
        {},
        {
            **PAGE,
            "page_size": 25,
            "pages_fetched": 1,
            "scanned": 1,
            "duplicates_removed": 0,
            "total": 1,
            "total_relation": "exact",
            "scan_cap_reached": False,
            "stop_reason": "exhausted",
            "resolved_location": None,
        },
    ),
    "get_listing": ({"listing_id": "1"}, LISTING),
    "get_ad_images": (
        {"listing_id": "1"},
        {
            "listing_id": "1",
            "count": 1,
            "images": [
                {
                    "index": 0,
                    "url": "https://img.kleinanzeigen.de/test.png",
                    "mime_type": "image/png",
                    "bytes": len(PNG),
                }
            ],
            "total_images": 1,
            "next_start": None,
        },
    ),
    "get_seller_info": (
        {"seller_id": "1"},
        {
            "seller_id": "1",
            "name": None,
            "account_type": None,
            "member_since": None,
            "active_ads": None,
            "total_ads": None,
            "followers": None,
            "reply_speed": None,
            "average_rating": None,
            "badges": [],
            "reputation": {"total": None, "manual": None, "automatic": None},
        },
    ),
    "search_locations": ({"query": "Berlin"}, {"count": 0, "locations": [], "truncated": False}),
    "list_categories": ({}, {"count": 0, "total": 0, "categories": [], "next_offset": None}),
    "get_category_filters": (
        {"category": "1"},
        {"category_id": "1", "global_parameters": [], "attributes": []},
    ),
    "list_conversations": (
        {},
        {
            "page": 0,
            "count": 1,
            "conversations": [CONVERSATION],
            "scanned": 1,
            "pages_fetched": 1,
            "unread_returned": 0,
            "next_page": None,
            "has_more": False,
            "ordering_scope": "scanned_pages",
        },
    ),
    "mark_read": (
        {"conversation_ids": ["thread"]},
        {"ok": True, "action": "conversations_marked_read", "conversation_ids": ["thread"]},
    ),
    "contact_seller": (
        {"listing_id": "1", "text": "Hallo", "contact_name": "Fixture"},
        {
            "ok": True,
            "action": "seller_contacted",
            "listing_id": "1",
            "conversation_id": "thread",
            "conversation_created": True,
            "message_sent": True,
        },
    ),
    "reply": (
        {"conversation_id": "thread", "text": "Hallo"},
        {"ok": True, "action": "reply_sent", "conversation_id": "thread"},
    ),
    "list_my_ads": ({}, PAGE),
    "get_my_ad": ({"listing_id": "1"}, LISTING),
    "create_ad": (
        {
            "title": "Fixture",
            "description": "Text",
            "category_id": "1",
            "location_id": "2",
            "price": 42,
        },
        {"ok": True, "action": "ad_created", "listing_id": "1"},
    ),
    "update_ad": (
        {"listing_id": "1", "picture_urls": []},
        {"ok": True, "action": "ad_updated", "listing_id": "1", "fields": ["picture_urls"]},
    ),
    "upload_picture": (
        {"image_path": "fixture.png"},
        {"ok": True, "action": "picture_uploaded", "url": "https://img.kleinanzeigen.de/test.png"},
    ),
    "watchlist": ({}, PAGE),
    "extend_status": (
        {"listing_ids": ["1"]},
        {
            "count": 1,
            "eligibility": [{"listing_id": "1", "eligible": None, "reason": None}],
            "schema_verified": False,
        },
    ),
    "analyze_listings": (
        {"listings": [{"listing_id": "1", "title": "Fixture", "description": "Text"}]},
        {"findings": [], "disclaimer": "Fake result; analysis is not implemented."},
    ),
}
for _name, _action in [
    ("pause_ad", "ad_paused"),
    ("activate_ad", "ad_activated"),
    ("renew_ad", "ad_renewed"),
    ("delete_ad", "ad_deleted"),
]:
    SAMPLES[_name] = ({"listing_id": "1"}, {"ok": True, "action": _action, "listing_id": "1"})
for _name in ["get_conversation", "get_messages"]:
    SAMPLES[_name] = (
        {"conversation_id": "thread"},
        {
            "conversation": CONVERSATION,
            "messages": [],
            "count": 0,
            "offset": 0,
            "next_offset": None,
            "has_more": False,
            "full_returned_payload": True,
            "upstream_history_complete": None,
        },
    )


class FakeServices:
    def invoke(self, name, arguments):
        from fastmcp.utilities.types import Image

        from kleinanzeigen_mcp.models import DomainError, emit

        if arguments.get("listing_id") == "9":
            raise RuntimeError("SECRET upstream-body private@example.test")
        if arguments.get("listing_id") == "8":
            raise DomainError("NOT_FOUND")
        result = SAMPLES[name][1]
        return emit(
            result, images=[Image(data=PNG, format="png")] if name == "get_ad_images" else None
        )


async def start_process(args, env=None):
    child_env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"PYTHONPATH", "KLEINANZEIGEN_REFRESH_TOKEN", "KLEINANZEIGEN_TOKEN_DIR"}
    }
    child_env.update(KA_TOKEN_PATH="/nonexistent/test-only-token.json")
    child_env.update(env or {})
    return await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=2 * 1024 * 1024,
        env=child_env,
    )


async def rpc(process, method, params, identifier=1):
    message = {"jsonrpc": "2.0", "method": method, "params": params}
    if identifier is not None:
        message["id"] = identifier
    process.stdin.write((json.dumps(message) + "\n").encode())
    await process.stdin.drain()
    if identifier is None:
        return None
    while True:
        line = await asyncio.wait_for(process.stdout.readline(), timeout=10)
        assert line, "Server closed stdout"
        result = json.loads(line)  # Every stdout line must be protocol JSON, never a banner.
        assert result["jsonrpc"] == "2.0"
        if result.get("id") == identifier:
            assert "error" not in result, result
            return result["result"]


async def initialize(process):
    await rpc(
        process,
        "initialize",
        {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "offline-test", "version": "1"},
        },
    )
    await rpc(process, "notifications/initialized", {}, None)


async def stop(process):
    process.stdin.close()
    await asyncio.wait_for(process.wait(), 10)
    err = (await process.stderr.read()).decode()
    assert process.returncode == 0, err
    assert all(secret not in err for secret in ["SECRET", "upstream-body", "private@example.test"])


async def test_all_fake_tools_stdio_schemas_errors_and_image():
    p = await start_process([sys.executable, str(Path(__file__).resolve()), "--serve-fakes"])
    try:
        await initialize(p)
        tools = (await rpc(p, "tools/list", {}))["tools"]
        assert len(tools) == 25
        for tool in tools:
            name = tool["name"].removeprefix("kleinanzeigen_")
            arguments, expected = SAMPLES[name]
            r = await rpc(p, "tools/call", {"name": tool["name"], "arguments": arguments})
            assert not r.get("isError", False), r
            assert r["structuredContent"] == expected
            assert json.loads(r["content"][0]["text"]) == expected
            Draft202012Validator(tool["outputSchema"]).validate(expected)
            if name == "get_ad_images":
                assert r["content"][1]["type"] == "image"
                assert base64.b64decode(r["content"][1]["data"]) == PNG
        schema = next(t["outputSchema"] for t in tools if t["name"] == "kleinanzeigen_get_listing")
        for arguments, code in [
            ({"listing_id": "../"}, "INVALID_ARGUMENT"),
            ({"listing_id": "8"}, "NOT_FOUND"),
            ({"listing_id": "9"}, "INTERNAL_ERROR"),
        ]:
            r = await rpc(
                p, "tools/call", {"name": "kleinanzeigen_get_listing", "arguments": arguments}
            )
            assert r["isError"]
            assert r["structuredContent"]["error"]["code"] == code
            Draft202012Validator(schema).validate(r["structuredContent"])
            assert json.loads(r["content"][0]["text"]) == r["structuredContent"]
    finally:
        await stop(p)


@pytest.mark.parametrize(
    "args",
    [
        [sys.executable, "server.py"],
        [sys.executable, "-m", "kleinanzeigen_mcp"],
        [str(Path(".venv/bin/kleinanzeigen-mcp").resolve())],
    ],
)
async def test_launchers_are_equivalent_without_startup_io(args):
    p = await start_process(args)
    try:
        await initialize(p)
        assert len((await rpc(p, "tools/list", {}))["tools"]) == 24
    finally:
        await stop(p)


def test_http_signature_is_supported():
    import inspect

    from fastmcp import FastMCP

    inspect.signature(FastMCP.run_http_async).bind(
        None, transport="http", host="127.0.0.1", port=8000, path="/mcp", show_banner=False
    )


if __name__ == "__main__":
    # The child is network-denied independently of pytest's parent fixtures.
    import pytest_socket
    from curl_cffi import requests

    from kleinanzeigen_mcp.config import Settings
    from kleinanzeigen_mcp.server import create_server

    pytest_socket.disable_socket(allow_unix_socket=True)

    def denied(*args, **kwargs):
        raise AssertionError("Live curl forbidden in stdio child")

    requests.Session.request = denied
    create_server(settings=Settings(enable_analysis=True), services=FakeServices()).run(
        transport="stdio", show_banner=False
    )


async def test_framework_logging_uses_redacted_stderr():
    script = """
import logging
from kleinanzeigen_mcp.config import Settings
from kleinanzeigen_mcp.server import configure_logging
configure_logging(Settings())
try:
    raise RuntimeError('SECRET-BODY')
except RuntimeError:
    logging.getLogger('fastmcp.server.server').exception('SECRET-BODY')
"""
    p = await start_process([sys.executable, "-c", script])
    stdout, stderr = await p.communicate()
    assert p.returncode == 0, stderr.decode()
    assert stdout == b""
    assert b"SECRET-BODY" not in stderr
    records = [json.loads(line) for line in stderr.splitlines()]
    assert records and records[-1]["event"] == "external_log"
