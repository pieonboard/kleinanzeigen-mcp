"""Synthetic messaging through real MCP services and paced transport. No live fixtures."""

import json
import time
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


def fixture(name):
    return json.loads((Path(__file__).parent / "fixtures" / f"{name}.json").read_text())


def listing(**changes):
    return {"id": "101", "title": "Synthetic lamp", "user-id": "999", **changes}


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
        assert len(result.content[0].text.encode()) <= 1024 * 1024
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


async def test_inbox_source_fields_partition_and_raw_page_continuation(h):
    h.responses(fixture("conversations"), {"conversations": []})
    result = await h.ok("list_conversations", page=4, page_size=3, pages=2)
    assert [c["conversation_id"] for c in result["conversations"]] == [
        "unread-thread",
        "read-thread",
        "103",
    ]
    assert result["ordering_scope"] == "scanned_pages"
    assert result["scanned"] == result["count"] == 3
    assert result["pages_fetched"] == 2 and result["unread_returned"] == 1
    assert result["has_more"] is False and result["next_page"] is None
    read, unknown = result["conversations"][1:]
    assert read["counterparty"] == "Synthetic buyer"
    assert read["unread"] is False and read["unread_count"] == 0
    assert read["listing_id"] == "101" and read["last_message_preview"] == "Read preview"
    assert unknown["unread"] is unknown["unread_count"] is unknown["listing_price"] is None
    assert [c[1] for c in h.raw.calls] == ["GET", "GET"]
    assert [c[3]["params"] for c in h.raw.calls] == [{"page": 4, "size": 3}, {"page": 5, "size": 3}]


async def test_filtered_empty_full_inbox_page_is_resumable(h):
    raw = fixture("conversations")["data"]["items"][0]
    h.responses({"conversations": [raw]})
    result = await h.ok("list_conversations", page_size=1, unread_only=True)
    assert result["count"] == 0 and result["scanned"] == 1
    assert result["next_page"] == 1 and result["has_more"] is None
    h.responses(fixture("conversations"))
    unsorted = await h.ok("list_conversations", unread_first=False)
    assert unsorted["conversations"][0]["conversation_id"] == "read-thread"


@pytest.mark.parametrize(
    "bad", [{}, {"data": {}}, {"conversations": [None]}, {"conversations": [{"id": ""}]}]
)
async def test_malformed_inbox_does_not_claim_empty(h, bad):
    h.responses(bad)
    await h.error("list_conversations", "UPSTREAM_SCHEMA")


async def test_inbox_partial_failure_retains_complete_pages(h):
    h.responses({"conversations": [{"id": "first"}]}, Response(503), Response(503), Response(503))
    result = await h.ok("list_conversations", page_size=1, pages=2)
    assert result["partial"] is True and result["pages_fetched"] == 1
    assert result["scanned"] == 1 and result["next_page"] == 1 and result["has_more"] is None
    assert result["warnings"][0]["code"] == "UPSTREAM_UNAVAILABLE"


@pytest.mark.parametrize("name", ["get_conversation", "get_messages"])
async def test_thread_alias_one_put_full_text_chronology_and_unknown_direction(h, name):
    body = fixture("thread")
    body["messages"][1]["text"] = "  " + "ü\n" * 9000 + "  "
    h.responses(body)
    result = await h.ok(name, conversation_id="thread-synthetic")
    assert len(h.raw.calls) == 1
    assert h.raw.calls[0][1] == "PUT" and h.raw.calls[0][3]["params"] == {"contentWarnings": "true"}
    assert [m["message_id"] for m in result["messages"]] == [
        "41",
        "message-tie",
        "message-system",
        "message-new",
    ]
    assert result["messages"][0]["text"] == body["messages"][1]["text"]
    assert result["messages"][0]["attachment_count"] == 1
    assert result["messages"][0]["sender"] == "Synthetic seller"
    unknown = result["messages"][2]
    assert unknown["direction"] == "unknown" and unknown["sender"] is None
    assert all(m["unread"] is None for m in result["messages"])
    assert result["conversation"]["listing_price"] == 42.5
    assert result["conversation"]["unread_count"] == 1
    assert result["full_returned_payload"] is True and result["has_more"] is False
    assert result["next_offset"] is result["upstream_history_complete"] is None
    assert "offsets can shift" in result["warnings"][-1]["hint"]


async def test_explicit_local_pagination_reopens_once_per_call(h):
    h.responses(fixture("thread"), fixture("thread"), fixture("thread"))
    first = await h.ok("get_conversation", conversation_id="thread-synthetic", limit=2)
    second = await h.ok(
        "get_messages", conversation_id="thread-synthetic", offset=first["next_offset"], limit=2
    )
    assert first["has_more"] is True and first["next_offset"] == 2
    assert second["has_more"] is False and second["next_offset"] is None
    assert first["full_returned_payload"] is second["full_returned_payload"] is False
    assert [m["message_id"] for m in first["messages"] + second["messages"]] == [
        "41",
        "message-tie",
        "message-system",
        "message-new",
    ]
    empty = await h.ok("get_messages", conversation_id="thread-synthetic", offset=100)
    assert empty["messages"] == [] and empty["has_more"] is False
    assert len(h.raw.calls) == 3 and all(c[1] == "PUT" for c in h.raw.calls)


async def test_byte_budget_stops_between_whole_messages_and_continues(h):
    body = fixture("thread")
    for row in body["messages"]:
        row["text"] = "ä" * 180000
    h.responses(body, body)
    first = await h.ok("get_messages", conversation_id="thread-synthetic")
    assert first["count"] == 2 and first["next_offset"] == 2 and first["has_more"] is True
    assert all(m["text"] == "ä" * 180000 for m in first["messages"])
    second = await h.ok("get_messages", conversation_id="thread-synthetic", offset=2)
    assert second["count"] == 2 and second["has_more"] is False
    assert len(h.raw.calls) == 2


async def test_single_oversize_message_is_error_without_truncation(h):
    body = fixture("thread")
    body["messages"] = [{"text": "ä" * 600000}]
    h.responses(body)
    await h.error("get_messages", "LIMIT_EXCEEDED", conversation_id="thread-synthetic")
    assert len(h.raw.calls) == 1


@pytest.mark.parametrize("date", [None, "invalid", "2026-10-15T08:00:00"])
async def test_unestablished_chronology_preserves_source_order(h, date):
    body = fixture("thread")
    body["messages"][0]["receivedDate"] = date
    body.pop("numUnread")
    body["adPriceInEuroCent"] = "4250"
    h.responses({"data": body})
    result = await h.ok("get_conversation", conversation_id="thread-synthetic")
    assert result["messages"][0]["message_id"] == "message-new"
    assert result["warnings"][0]["code"] == "UPSTREAM_SCHEMA"
    assert result["conversation"]["unread"] is result["conversation"]["unread_count"] is None
    assert result["conversation"]["listing_price"] is None


async def test_empty_thread_and_empty_full_text(h):
    body = fixture("thread")
    body["messages"] = []
    h.responses(body)
    assert (await h.ok("get_messages", conversation_id="thread-synthetic"))[
        "full_returned_payload"
    ] is True
    body["messages"] = [{"text": "", "textShort": "Must not replace full text"}]
    h.responses(body)
    assert (await h.ok("get_messages", conversation_id="thread-synthetic"))["messages"][0][
        "text"
    ] == ""


async def test_mark_read_deduplicates_one_post_and_no_per_id_ack(h):
    h.responses(Response(204))
    result = await h.ok("mark_read", conversation_ids=["opaque-a", "opaque-b", "opaque-a"])
    assert result["conversation_ids"] == ["opaque-a", "opaque-b"]
    call = h.raw.calls[0]
    assert len(h.raw.calls) == 1 and call[1] == "POST"
    assert call[2].endswith("/conversations/read") and call[3]["params"] == {
        "ids": "opaque-a,opaque-b"
    }


async def test_first_contact_exact_create_name_then_reply_text(h):
    h.responses(listing(), fixture("create_conversation"), Response(204))
    result = await h.ok(
        "contact_seller", listing_id="101", text="  Hallo ä!\n\tGrüße  ", contact_name="P & Ä?"
    )
    assert result["conversation_created"] is result["message_sent"] is True
    assert result["conversation_id"] == "thread-created-synthetic" and "message_id" not in result
    read, create, reply = h.raw.calls
    assert read[1] == "GET" and "X-EBAYK-USERID-TOKEN" not in read[3]["headers"]
    assert (
        create[1] == "POST"
        and create[2] == "https://api.kleinanzeigen.de/api/users/700/create-conversation/101"
    )
    assert create[3]["params"] == {"contactName": "P & Ä?"}
    assert create[3]["json"] is create[3]["data"] is None
    assert reply[1] == "POST" and reply[2].endswith("/conversations/thread-created-synthetic")
    assert reply[3]["params"] == {
        "warnPhoneNumber": "false",
        "warnEmail": "false",
        "warnBankDetails": "false",
    }
    assert reply[3]["json"] == {"message": "Hallo ä!\n\tGrüße"}
    assert all(b[0] - a[0] >= 1.5 for a, b in zip(h.raw.calls, h.raw.calls[1:], strict=False))


async def test_config_contact_name_and_cached_listing(h):
    h.runtime.settings = h.settings.model_copy(update={"contact_name": "Configured name"})
    h.responses(listing())
    await h.ok("get_listing", listing_id="101")
    h.responses({"id": 12345}, Response())
    result = await h.ok("contact_seller", listing_id="101", text="Hello")
    assert result["conversation_id"] == "12345"
    assert h.raw.calls[1][3]["params"] == {"contactName": "Configured name"}
    assert len(h.raw.calls) == 3


@pytest.mark.parametrize(
    "args",
    [
        {"text": "   "},
        {"text": "a\x00b"},
        {"text": "a\x01b"},
        {"text": "a\ud800b"},
        {"text": "ü" * 5001},
        {"listing_id": "../escape"},
        {"conversation_id": "thread/bad"},
        {"contact_name": "   "},
        {"contact_name": "n\x00m"},
        {"contact_name": "x" * 201},
    ],
)
async def test_contact_input_validation_precedes_any_request(h, args):
    values = {"listing_id": "101", "text": "Hallo", "contact_name": "Synthetic name", **args}
    await h.error("contact_seller", "INVALID_ARGUMENT", **values)
    assert not h.raw.calls and h.runtime._account is None


async def test_missing_name_does_not_infer_from_account(h):
    await h.error("contact_seller", "INVALID_ARGUMENT", listing_id="101", text="Hallo")
    assert not h.raw.calls


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"user-id": "700"}, "INVALID_ARGUMENT"),
        ({"ad-status": "REMOVED"}, "NOT_FOUND"),
        ({"id": "102"}, "UPSTREAM_SCHEMA"),
    ],
)
async def test_listing_preflight_prevents_mutation(h, changes, code):
    h.responses(listing(**changes))
    await h.error("contact_seller", code, listing_id="101", text="Hallo", contact_name="Synthetic")
    assert [c[1] for c in h.raw.calls] == ["GET"]


async def test_missing_listing_prevents_contact(h):
    h.responses(Response(404))
    await h.error(
        "contact_seller", "NOT_FOUND", listing_id="101", text="Hallo", contact_name="Synthetic"
    )
    assert len(h.raw.calls) == 1


async def test_deliberate_existing_thread_reuse_needs_no_name_or_create(h):
    h.responses(listing(), fixture("thread"), Response())
    result = await h.ok(
        "contact_seller", listing_id="101", conversation_id="thread-synthetic", text="Hallo"
    )
    assert result["conversation_created"] is False
    assert [c[1] for c in h.raw.calls] == ["GET", "PUT", "POST"]
    assert not any("create-conversation" in c[2] for c in h.raw.calls)


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"adId": "102"}, "INVALID_ARGUMENT"),
        ({"adId": None}, "INVALID_ARGUMENT"),
        ({"role": "SELLER"}, "INVALID_ARGUMENT"),
        ({"role": None}, "INVALID_ARGUMENT"),
        ({"id": "other"}, "UPSTREAM_SCHEMA"),
    ],
)
async def test_reuse_must_match_listing_and_buyer_role_before_send(h, changes, code):
    body = {**fixture("thread"), **changes}
    h.responses(listing(), body)
    await h.error(
        "contact_seller", code, listing_id="101", conversation_id="thread-synthetic", text="Hallo"
    )
    assert [c[1] for c in h.raw.calls] == ["GET", "PUT"]


@pytest.mark.parametrize(
    "response,code,created",
    [
        (Response(400), "INVALID_ARGUMENT", False),
        (Response(401), "AUTH_EXPIRED", False),
        (Response(403), "AUTH_OR_PERMISSION_DENIED", False),
        (Response(404), "NOT_FOUND", False),
        (Response(429), "RATE_LIMITED", False),
        (Response(503), "WRITE_OUTCOME_UNKNOWN", None),
        (OSError("private upstream body"), "WRITE_OUTCOME_UNKNOWN", None),
    ],
)
async def test_create_failure_no_send_no_replay_retains_context(h, response, code, created):
    h.responses(listing(), response)
    detail = await h.error(
        "contact_seller", code, listing_id="101", text="Hallo", contact_name="Synthetic"
    )
    assert detail["retryable"] is False
    ctx = detail["context"]
    assert ctx["listing_id"] == "101" and ctx["phase"] == "create_conversation"
    assert ctx["conversation_created"] is created and ctx["message_status"] == "not_sent"
    assert len(h.raw.calls) == 2


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"data": {"id": "speculative"}},
        {"conversationId": "speculative"},
        {"id": ""},
        {"id": "../invalid"},
        {"id": True},
        {"id": "\ud800"},
        [],
        Response(body=ValueError("secret JSON")),
        Response(content=b"x" * (8 * 1024 * 1024 + 1)),
    ],
)
async def test_acknowledged_create_unknown_id_is_partial_and_never_guessed(h, body):
    h.responses(listing(), body)
    detail = await h.error(
        "contact_seller", "PARTIAL_WRITE", listing_id="101", text="Hallo", contact_name="Synthetic"
    )
    ctx = detail["context"]
    assert (
        ctx["phase"] == "conversation_created_id_unresolved" and ctx["create_acknowledged"] is True
    )
    assert ctx["conversation_created"] is True and ctx["conversation_id"] is None
    assert ctx["message_status"] == "not_sent" and detail["retryable"] is False
    assert len(h.raw.calls) == 2


@pytest.mark.parametrize(
    "response,status",
    [
        (Response(400), "not_sent"),
        (Response(401), "not_sent"),
        (Response(403), "not_sent"),
        (Response(404), "not_sent"),
        (Response(429), "not_sent"),
        (Response(500), "unknown"),
        (Response(502), "unknown"),
        (OSError("secret"), "unknown"),
    ],
)
async def test_second_phase_failure_preserves_created_thread_for_recovery(h, response, status):
    h.responses(listing(), fixture("create_conversation"), response)
    detail = await h.error(
        "contact_seller", "PARTIAL_WRITE", listing_id="101", text="Hallo", contact_name="Synthetic"
    )
    ctx = detail["context"]
    assert ctx["phase"] == "reply" and ctx["conversation_id"] == "thread-created-synthetic"
    assert ctx["conversation_created"] is True and ctx["message_status"] == status
    assert detail["retryable"] is False and len(h.raw.calls) == 3
    if response.__class__ is Response and response.status_code == 401:
        assert h.runtime._auth_failed is True


async def test_reused_thread_unknown_send_preserves_context_without_claiming_creation(h):
    h.responses(listing(), fixture("thread"), OSError("secret"))
    detail = await h.error(
        "contact_seller",
        "WRITE_OUTCOME_UNKNOWN",
        listing_id="101",
        conversation_id="thread-synthetic",
        text="Hallo",
    )
    assert detail["context"]["conversation_created"] is False
    assert detail["context"]["message_status"] == "unknown"
    assert len(h.raw.calls) == 3


@pytest.mark.parametrize(
    "name,args",
    [
        ("reply", {"conversation_id": "thread", "text": "Hallo"}),
        ("mark_read", {"conversation_ids": ["thread"]}),
        ("get_messages", {"conversation_id": "thread"}),
    ],
)
async def test_all_messaging_mutations_one_attempt_on_unknown(h, name, args):
    h.responses(Response(503), Response())
    detail = await h.error(name, "WRITE_OUTCOME_UNKNOWN", **args)
    assert detail["retryable"] is False and len(h.raw.calls) == 1
    if name == "reply":
        assert detail["context"]["message_status"] == "unknown"
        assert "open the thread" in detail["hint"]


async def test_reply_does_not_open_thread_or_create_and_preserves_max_unicode(h):
    h.responses(Response())
    await h.ok("reply", conversation_id="thread", text="  " + "ü" * 5000 + "  ")
    assert len(h.raw.calls) == 1 and h.raw.calls[0][1] == "POST"
    assert h.raw.calls[0][3]["json"] == {"message": "ü" * 5000}


async def test_repeat_contact_warning_is_normalized_key_ten_minute_memory_only(h):
    for content, warning in [
        ("  Hallo\nGrüße  ", False),
        ("Hallo Grüße", True),
        ("Different", False),
    ]:
        if not h.raw.calls:
            h.responses(listing())
        h.responses(fixture("create_conversation"), Response())
        result = await h.ok(
            "contact_seller", listing_id="101", text=content, contact_name="Synthetic"
        )
        assert bool(result["warnings"]) is warning and result["message_sent"] is True
        if warning:
            assert result["warnings"][0]["context"]["repeat_contact"] is True
            assert "no durable deduplication" in result["warnings"][0]["hint"]
    assert len([c for c in h.raw.calls if c[1] == "POST"]) == 6
    h.clock.now += 601
    h.responses(listing(), fixture("create_conversation"), Response())
    expired = await h.ok(
        "contact_seller", listing_id="101", text="Hallo Grüße", contact_name="Synthetic"
    )
    assert not expired["warnings"]
    assert not RuntimeServices(h.settings, runtime=h.runtime).messaging.completed


async def test_no_message_name_or_body_leaks_on_failure(h, caplog, capsys):
    caplog.set_level("INFO", logger="kleinanzeigen_mcp")
    h.responses(listing(), fixture("create_conversation"), OSError("SECRET-UPSTREAM-BODY"))
    detail = await h.error(
        "contact_seller",
        "PARTIAL_WRITE",
        listing_id="101",
        text="SECRET-MESSAGE",
        contact_name="SECRET-NAME",
    )
    output = caplog.text + str(detail) + str(capsys.readouterr())
    for secret in ["SECRET-UPSTREAM-BODY", "SECRET-MESSAGE", "SECRET-NAME", "synthetic-access"]:
        assert secret not in output


@pytest.mark.parametrize(
    "name,args",
    [
        ("list_conversations", {"page_size": 100, "pages": 3}),
        ("get_messages", {"conversation_id": "thread", "offset": -1}),
        ("get_conversation", {"conversation_id": "thread", "limit": 2001}),
        ("mark_read", {"conversation_ids": []}),
        ("mark_read", {"conversation_ids": ["thread"] * 51}),
        ("reply", {"conversation_id": "thread", "text": "\x00"}),
    ],
)
async def test_invalid_pagination_ids_and_text_do_not_mutate(h, name, args):
    await h.error(name, "INVALID_ARGUMENT", **args)
    assert not h.raw.calls


@pytest.mark.parametrize("timeout,created,calls", [(1, False, 1), (3, True, 2)])
async def test_deadline_before_wire_preserves_definitely_unsent_state(h, timeout, created, calls):
    h.runtime.budget.operation_timeout = timeout
    h.responses(listing(), fixture("create_conversation"), Response())
    code = "PARTIAL_WRITE" if created else "UPSTREAM_UNAVAILABLE"
    detail = await h.error(
        "contact_seller", code, listing_id="101", text="Hallo", contact_name="Synthetic"
    )
    ctx = detail["context"]
    assert ctx["conversation_created"] is created and ctx["message_status"] == "not_sent"
    assert len(h.raw.calls) == calls
    assert ctx["conversation_id"] == ("thread-created-synthetic" if created else None)


async def test_repeat_warning_includes_listing_id_in_key_and_normalizes_nfc(h):
    h.responses(listing(), fixture("create_conversation"), Response())
    await h.ok("contact_seller", listing_id="101", text="Gru\u0308ße", contact_name="Synthetic")
    h.responses(fixture("create_conversation"), Response())
    repeated = await h.ok(
        "contact_seller", listing_id="101", text="Grüße", contact_name="Synthetic"
    )
    assert repeated["warnings"][0]["context"]["repeat_contact"] is True
    h.responses(listing(id="102"), fixture("create_conversation"), Response())
    other = await h.ok("contact_seller", listing_id="102", text="Grüße", contact_name="Synthetic")
    assert not other["warnings"]


async def test_stable_unread_partition_across_pages_and_scan_cap(h):
    rows = fixture("conversations")["data"]["items"]
    h.responses(
        {"conversations": rows[:2]},
        {"conversations": [{**rows[1], "id": "unread-next"}, {**rows[0], "id": "read-next"}]},
    )
    result = await h.ok("list_conversations", page_size=2, pages=2)
    assert [c["conversation_id"] for c in result["conversations"]] == [
        "unread-thread",
        "unread-next",
        "read-thread",
        "read-next",
    ]
    assert result["next_page"] == 2 and result["scanned"] == 4
    for start in range(4):
        h.responses({"conversations": [{"id": str(start * 50 + i)} for i in range(50)]})
    full = await h.ok("list_conversations", page_size=50, pages=4)
    assert full["count"] == full["scanned"] == 200 and full["pages_fetched"] == 4
    assert len(h.raw.calls) == 6


@pytest.mark.parametrize("bad", [None, "wrong", [{"text": {"unexpected": "object"}}]])
async def test_malformed_thread_messages_do_not_silently_disappear(h, bad):
    body = {**fixture("thread"), "messages": bad}
    h.responses(body)
    await h.error("get_messages", "UPSTREAM_SCHEMA", conversation_id="thread-synthetic")
    assert len(h.raw.calls) == 1


async def test_reused_thread_load_timeout_never_sends(h):
    h.responses(listing(), OSError("secret"))
    detail = await h.error(
        "contact_seller",
        "WRITE_OUTCOME_UNKNOWN",
        listing_id="101",
        conversation_id="thread-synthetic",
        text="Hallo",
    )
    assert detail["context"]["phase"] == "conversation_load"
    assert detail["context"]["conversation_created"] is False
    assert detail["context"]["message_status"] == "not_sent"
    assert len(h.raw.calls) == 2
