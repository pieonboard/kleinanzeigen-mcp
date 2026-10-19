import asyncio
import json
import threading
import time

import pytest
from fastmcp import Client
from jsonschema import Draft202012Validator

from kleinanzeigen_mcp.config import Settings
from kleinanzeigen_mcp.models import DomainError
from kleinanzeigen_mcp.server import create_server

HISTORICAL = {
    "search",
    "get_listing",
    "list_conversations",
    "get_conversation",
    "get_messages",
    "reply",
    "list_my_ads",
    "get_my_ad",
    "create_ad",
    "update_ad",
    "pause_ad",
    "activate_ad",
    "renew_ad",
    "delete_ad",
    "upload_picture",
    "watchlist",
}


async def test_inventory_schemas_annotations_and_no_startup_io():
    for enabled, count in [(False, 24), (True, 25)]:
        async with Client(create_server(settings=Settings(enable_analysis=enabled))) as client:
            tools = await client.list_tools()
            assert len(tools) == count
            by_name = {t.name.removeprefix("kleinanzeigen_"): t for t in tools}
            assert HISTORICAL <= by_name.keys()
            for tool in tools:
                assert tool.output_schema["type"] == "object"
                assert len(tool.output_schema["oneOf"]) == 2
                Draft202012Validator.check_schema(tool.output_schema)
                assert tool.input_schema.get("additionalProperties") is False
                assert tool.annotations is not None
            for name in [
                "get_conversation",
                "get_messages",
                "mark_read",
                "pause_ad",
                "activate_ad",
            ]:
                a = by_name[name].annotations
                assert (
                    a.read_only_hint,
                    a.destructive_hint,
                    a.idempotent_hint,
                    a.open_world_hint,
                ) == (False, False, True, True)
            for name in ["update_ad", "delete_ad"]:
                assert by_name[name].annotations.destructive_hint is True
                assert by_name[name].annotations.idempotent_hint is True
            assert by_name["list_categories"].annotations.open_world_hint is False
            p = by_name["search"].input_schema["properties"]
            assert p["page"]["minimum"] == 0 and p["page"]["maximum"] == 399
            assert p["page_size"]["maximum"] == 50
            assert p["summary_chars"]["default"] is None
            assert p["include_top_ads"]["default"] is False


@pytest.mark.parametrize(
    "arguments",
    [{"listing_id": "../secret"}, {"listing_id": "1", "extra": True}, {"listing_id": ""}, {}],
)
async def test_argument_errors_are_typed(arguments):
    async with Client(create_server()) as client:
        r = await client.call_tool("kleinanzeigen_get_listing", arguments, raise_on_error=False)
        assert r.is_error
        assert r.structured_content["error"]["code"] == "INVALID_ARGUMENT"
        assert json.loads(r.content[0].text) == r.structured_content


async def test_body_errors_are_safe_and_typed():
    class Broken:
        def invoke(self, name, arguments):
            if arguments["listing_id"] == "1":
                raise DomainError("NOT_FOUND")
            raise RuntimeError("secret-token upstream-body private@example.test")

    async with Client(create_server(services=Broken())) as client:
        for identifier, code in [("1", "NOT_FOUND"), ("2", "INTERNAL_ERROR")]:
            r = await client.call_tool(
                "kleinanzeigen_get_listing", {"listing_id": identifier}, raise_on_error=False
            )
            assert r.structured_content["error"]["code"] == code
            assert "secret-token" not in str(r)


async def test_sync_worker_does_not_block_heartbeat():
    main = threading.get_ident()
    observed = []

    class Blocking:
        def invoke(self, name, arguments):
            observed.append(threading.get_ident())
            time.sleep(0.15)
            return {"ok": True, "action": "reply_sent", "conversation_id": "thread"}

    ticks = []

    async def heartbeat():
        for _ in range(5):
            await asyncio.sleep(0.02)
            ticks.append(time.monotonic())

    async with Client(create_server(services=Blocking())) as client:
        await asyncio.gather(
            client.call_tool("kleinanzeigen_reply", {"conversation_id": "thread", "text": "Hallo"}),
            heartbeat(),
        )
    assert observed[0] != main
    assert len(ticks) == 5
    assert ticks[-1] - ticks[0] < 0.14


def test_config_validation_and_token_precedence(tmp_path):
    assert (
        Settings.from_env({"KLEINANZEIGEN_TOKEN_DIR": str(tmp_path)}).token_path
        == tmp_path / "token.json"
    )
    for env in [
        {"KA_HOST": "0.0.0.0"},
        {"KA_REQUEST_INTERVAL": "1"},
        {"KA_ENABLE_ANALYSIS": "perhaps"},
        {"KA_REQUEST_TIMEOUT": "nan"},
        {"KA_OPERATION_TIMEOUT": "1"},
    ]:
        with pytest.raises(ValueError):
            Settings.from_env(env)


@pytest.mark.parametrize(
    "text,ok",
    [
        ("  " + "ü" * 5000 + "  ", True),
        ("a\x00b", False),
        ("a\ud800b", False),
        ("  ", False),
        ("a\x01b", False),
        ("Hallo\n\tGrüße", True),
    ],
)
async def test_message_validation_after_trim(text, ok):
    class Echo:
        def invoke(self, name, arguments):
            assert arguments["text"] == text.strip()
            return {"ok": True, "action": "reply_sent", "conversation_id": "thread"}

    async with Client(create_server(services=Echo())) as client:
        result = await client.call_tool(
            "kleinanzeigen_reply", {"conversation_id": "thread", "text": text}, raise_on_error=False
        )
        assert result.is_error is not ok
        if not ok:
            assert result.structured_content["error"]["code"] == "INVALID_ARGUMENT"


async def test_historical_parameters_and_all_annotation_tuples():
    historical = {
        "search": (
            "query location category distance_km min_price max_price "
            "exclude sort_type pages page_size"
        ),
        "get_listing": "listing_id",
        "list_conversations": "page page_size",
        "get_conversation": "conversation_id",
        "get_messages": "conversation_id",
        "reply": "conversation_id text",
        "list_my_ads": "page page_size query",
        "get_my_ad": "listing_id",
        "create_ad": (
            "title description category_id location_id price_type price "
            "contact_name phone attributes picture_urls shipping_options"
        ),
        "update_ad": "listing_id title description price price_type picture_urls",
        "pause_ad": "listing_id",
        "activate_ad": "listing_id",
        "renew_ad": "listing_id",
        "delete_ad": "listing_id",
        "upload_picture": "image_path filename content_type",
        "watchlist": "page page_size",
    }
    local = {"list_categories", "analyze_listings"}
    state = {"pause_ad", "activate_ad", "mark_read", "get_conversation", "get_messages"}
    writes = {"contact_seller", "reply", "create_ad", "upload_picture", "renew_ad"}
    destructive = {"update_ad", "delete_ad"}
    async with Client(create_server(settings=Settings(enable_analysis=True))) as client:
        for t in await client.list_tools():
            name = t.name.removeprefix("kleinanzeigen_")
            if name in historical:
                assert set(historical[name].split()) <= t.input_schema["properties"].keys()
            a = t.annotations
            assert a.read_only_hint == (name not in state | writes | destructive)
            assert a.destructive_hint == (name in destructive)
            assert a.idempotent_hint == (name not in writes)
            assert a.open_world_hint == (name not in local)


async def test_malformed_service_output_is_typed():
    class Bad:
        def invoke(self, name, arguments):
            return {"wrong": "SECRET"}

    async with Client(create_server(services=Bad())) as client:
        r = await client.call_tool(
            "kleinanzeigen_get_listing", {"listing_id": "1"}, raise_on_error=False
        )
        assert r.structured_content["error"]["code"] == "UPSTREAM_SCHEMA"


@pytest.mark.parametrize("as_tool_result", [False, True])
async def test_output_validation_does_not_emit_coerced_input_types(as_tool_result):
    class Bad:
        def invoke(self, name, arguments):
            from kleinanzeigen_mcp.models import emit

            payload = {"count": "0", "total": 0, "categories": [], "next_offset": None}
            return emit(payload) if as_tool_result else payload

    async with Client(create_server(services=Bad())) as client:
        r = await client.call_tool("kleinanzeigen_list_categories", {}, raise_on_error=False)
        assert r.is_error
        assert r.structured_content["error"]["code"] == "UPSTREAM_SCHEMA"


async def test_toolresult_output_cap_is_enforced():
    from fastmcp.tools import ToolResult

    class Oversized:
        def invoke(self, name, arguments):
            return ToolResult(
                content=[],
                structured_content={
                    "count": 1,
                    "total": 1,
                    "categories": [
                        {"id": "1", "title": "x" * 1024 * 1024, "path": "x", "parent_id": None}
                    ],
                    "next_offset": None,
                },
            )

    async with Client(create_server(services=Oversized())) as client:
        r = await client.call_tool("kleinanzeigen_list_categories", {}, raise_on_error=False)
        assert r.is_error
        assert r.structured_content["error"]["code"] == "LIMIT_EXCEEDED"
