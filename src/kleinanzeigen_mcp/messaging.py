"""Text messaging with explicit local paging and recoverable write boundaries."""

from __future__ import annotations

import json
import unicodedata
from datetime import datetime
from typing import Any, Literal

from pydantic import JsonValue, TypeAdapter, ValidationError

from . import models as m
from .discovery import Discovery, boolean, integer, number, numeric_id, text
from .upstream import Runtime

JSON_LIMIT = 1024 * 1024


def opaque_id(value: Any) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    try:
        result = TypeAdapter(m.OpaqueID).validate_python(value, strict=True)
        result.encode("utf-8", errors="strict")
        if any(unicodedata.category(c) == "Cc" for c in result):
            raise ValueError
        return result
    except (ValidationError, ValueError, UnicodeError):
        raise m.DomainError("UPSTREAM_SCHEMA") from None


def summary(raw: dict[str, Any], *, opened: bool = False) -> m.Conversation:
    role = text(raw.get("role"))
    role = role.upper() if role else None
    counterparty = raw.get("sellerName") if role == "BUYER" else raw.get("buyerName")
    count = integer(raw.get("numUnread" if opened else "unreadMessagesCount"))
    cents = raw.get("adPriceInEuroCent")
    # Numeric strings are not evidence for a cents amount (C's opened shape).
    amount = number(cents) if type(cents) in (int, float) else None
    price = amount / 100 if amount is not None else None
    return m.Conversation(
        conversation_id=opaque_id(raw.get("id")),
        listing_id=numeric_id(raw.get("adId")),
        listing_title=text(raw.get("adTitle")),
        listing_status=text(raw.get("adStatus")),
        listing_price=price,
        currency="EUR",  # Established EUR-only account contract.
        listing_price_type=text(raw.get("adPriceType")),
        role=role,
        counterparty=text(counterparty or raw.get("sellerName") or raw.get("buyerName")),
        unread=(count > 0 if count is not None else None) if opened else boolean(raw.get("unread")),
        unread_count=count,
        last_message_at=text(raw.get("receivedDate")),
        last_message_preview=text(raw.get("textShortTrimmed")),
    )


def inbox_rows(body: Any) -> list[dict[str, Any]]:
    if not isinstance(body, dict):
        raise m.DomainError("UPSTREAM_SCHEMA")
    items = body.get("conversations", body.get("data"))
    if isinstance(items, dict):
        items = items.get("conversations", items.get("items"))
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise m.DomainError("UPSTREAM_SCHEMA")
    return items


def opened_thread(body: Any, expected_id: str) -> tuple[m.Conversation, list[dict[str, Any]]]:
    if not isinstance(body, dict):
        raise m.DomainError("UPSTREAM_SCHEMA")
    raw = body if "id" in body else body.get("data")
    if not isinstance(raw, dict):
        raise m.DomainError("UPSTREAM_SCHEMA")
    conv = summary(raw, opened=True)
    if conv.conversation_id != expected_id:
        raise m.DomainError(
            "UPSTREAM_SCHEMA", hint="The loaded thread ID did not match the request."
        )
    messages = raw.get("messages")
    if messages is None and isinstance(raw.get("data"), dict):
        messages = raw["data"].get("messages")
    if not isinstance(messages, list) or any(not isinstance(item, dict) for item in messages):
        raise m.DomainError("UPSTREAM_SCHEMA")
    return conv, messages


def messages_from(
    rows: list[dict[str, Any]], conv: m.Conversation
) -> tuple[list[m.Message], list[m.ErrorDetail]]:
    messages = []
    for raw in rows:
        bound = text(raw.get("boundness") or raw.get("direction"))
        direction: Literal["sent", "received", "unknown"] = (
            "received"
            if (bound or "").upper() == "INBOUND"
            else "sent"
            if (bound or "").upper() == "OUTBOUND"
            else "unknown"
        )
        attachments = raw.get("attachments", [])
        if attachments is None:
            attachments = []
        if not isinstance(attachments, list):
            raise m.DomainError("UPSTREAM_SCHEMA")
        # Preserve an explicitly empty full text; never replace it with a preview.
        content = next(
            (raw[k] for k in ("text", "textShort", "title") if raw.get(k) is not None), ""
        )
        messages.append(
            m.Message(
                message_id=opaque_id(raw["messageId"])
                if raw.get("messageId") is not None
                else None,
                conversation_id=conv.conversation_id,
                listing_id=conv.listing_id,
                listing_title=conv.listing_title,
                sender=conv.counterparty
                if direction == "received"
                else "me"
                if direction == "sent"
                else None,
                timestamp=text(raw.get("receivedDate")),
                text=text(content) or "",
                direction=direction,
                unread=None,
                message_type=text(raw.get("type")),
                attachment_count=len(attachments),
            )
        )
    warnings = []
    try:
        dates = [datetime.fromisoformat(message.timestamp or "") for message in messages]
        # datetime refuses mixed naive/aware values rather than inventing a zone.
        messages = [
            message for _, message in sorted(zip(dates, messages, strict=True), key=lambda p: p[0])
        ]
    except (ValueError, TypeError):
        warnings.append(
            m.DomainError(
                "UPSTREAM_SCHEMA",
                hint="Dates cannot establish chronology; source order is preserved.",
            ).detail
        )
    return messages, warnings


def json_size(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (UnicodeError, ValueError):
        raise m.DomainError("UPSTREAM_SCHEMA") from None


def validate_text(value: str) -> str:
    try:
        value = TypeAdapter(m.MessageText).validate_python(value, strict=True)
        value.encode("utf-8", errors="strict")
    except (ValidationError, UnicodeError):
        raise m.DomainError("INVALID_ARGUMENT") from None
    if any(unicodedata.category(c) == "Cc" and c not in "\n\r\t" for c in value):
        raise m.DomainError("INVALID_ARGUMENT")
    return value


class Messaging:
    def __init__(self, runtime: Runtime, discovery: Discovery):
        self.runtime, self.discovery = runtime, discovery
        self.completed: dict[tuple[str, str], float] = {}

    def list_conversations(
        self, page: int, page_size: int, unread_only: bool, unread_first: bool, pages: int
    ) -> m.ConversationsResult:
        result = m.ConversationsResult(
            page=page,
            count=0,
            conversations=[],
            scanned=0,
            pages_fetched=0,
            unread_returned=0,
            next_page=page,
            has_more=None,
            ordering_scope="scanned_pages",
        )
        with self.runtime.operation(
            account=True, tool_name="kleinanzeigen_list_conversations"
        ) as api:
            for current in range(page, page + pages):
                try:
                    raw = inbox_rows(api.conversations_raw(page=current, size=page_size))
                    if len(raw) > page_size:
                        raise m.DomainError("UPSTREAM_SCHEMA")
                    items = [summary(row) for row in raw]
                    candidate = result.model_copy(deep=True)
                    candidate.conversations.extend(items)
                    candidate.scanned += len(items)
                    candidate.pages_fetched += 1
                    candidate.next_page = current + 1 if len(raw) == page_size else None
                    candidate.has_more = None if len(raw) == page_size else False
                    # Bound complete raw pages before filtering, reserving warning metadata.
                    if json_size(candidate.model_dump(mode="json")) > JSON_LIMIT - 2048:
                        raise m.DomainError("LIMIT_EXCEEDED", hint="Use a smaller page_size.")
                    result = candidate
                except m.DomainError as exc:
                    if not result.pages_fetched:
                        raise
                    result.partial = True
                    result.warnings.append(exc.detail)
                    break
                if result.has_more is False:
                    break
        if unread_first:
            result.conversations.sort(key=lambda c: c.unread is not True)
        if unread_only:
            result.conversations = [c for c in result.conversations if c.unread is True]
        result.count = len(result.conversations)
        result.unread_returned = sum(c.unread is True for c in result.conversations)
        return result

    def get_conversation(
        self, conversation_id: str, offset: int, limit: int | None
    ) -> m.ThreadResult:
        return self._thread(conversation_id, offset, limit, "kleinanzeigen_get_conversation")

    def get_messages(self, conversation_id: str, offset: int, limit: int | None) -> m.ThreadResult:
        return self._thread(conversation_id, offset, limit, "kleinanzeigen_get_messages")

    def _thread(
        self, conversation_id: str, offset: int, limit: int | None, tool_name: str
    ) -> m.ThreadResult:
        with self.runtime.operation(account=True, tool_name=tool_name) as api:
            conv, raw = opened_thread(api.conversation(conversation_id), conversation_id)
            messages, warnings = messages_from(raw, conv)
            warnings.append(
                m.DomainError(
                    "UNSUPPORTED",
                    hint=(
                        "Continuation reopens and refetches this thread; offsets can shift "
                        "as messages arrive. Upstream history completeness and loaded/unread "
                        "effects are unverified."
                    ),
                ).detail
            )
            result = m.ThreadResult(
                conversation=conv,
                messages=[],
                count=0,
                offset=offset,
                next_offset=None,
                has_more=False,
                full_returned_payload=False,
                upstream_history_complete=None,
                warnings=warnings,
            )
            # Reserve the largest possible paging fields, then append whole messages.
            overhead = result.model_copy(
                update={"count": len(messages), "next_offset": max(9999, len(messages))}
            )
            remaining = JSON_LIMIT - json_size(overhead.model_dump(mode="json"))
            for message in messages[offset : None if limit is None else offset + limit]:
                size = json_size(message.model_dump(mode="json")) + (2 if result.messages else 0)
                if size > remaining:
                    if not result.messages:
                        raise m.DomainError(
                            "LIMIT_EXCEEDED",
                            hint="One full message cannot fit in the JSON output budget.",
                        )
                    break
                result.messages.append(message)
                remaining -= size
            result.count = len(result.messages)
            result.has_more = offset + result.count < len(messages)
            result.next_offset = offset + result.count if result.has_more else None
            result.full_returned_payload = offset == 0 and result.count == len(messages)
            if json_size(result.model_dump(mode="json")) > JSON_LIMIT:
                raise m.DomainError("LIMIT_EXCEEDED")
            return result

    def mark_read(self, conversation_ids: list[str]) -> m.MarkReadResult:
        ids = list(dict.fromkeys(conversation_ids))
        with self.runtime.operation(account=True, tool_name="kleinanzeigen_mark_read") as api:
            api.mark_read(ids)
        return m.MarkReadResult(ok=True, action="conversations_marked_read", conversation_ids=ids)

    def reply(self, conversation_id: str, text: str) -> m.ReplyResult:
        content = validate_text(text)
        try:
            with self.runtime.operation(account=True, tool_name="kleinanzeigen_reply") as api:
                api.reply(conversation_id, content)
        except m.DomainError as exc:
            if exc.detail.code == "WRITE_OUTCOME_UNKNOWN":
                raise self._failure(
                    exc,
                    "reply",
                    {
                        "conversation_id": conversation_id,
                        "phase": "reply",
                        "message_status": "unknown",
                    },
                ) from None
            raise
        return m.ReplyResult(ok=True, action="reply_sent", conversation_id=conversation_id)

    @staticmethod
    def _failure(exc: m.DomainError, phase: str, context: dict[str, JsonValue]) -> m.DomainError:
        code = exc.detail.code
        if phase == "create_conversation" and code == "PARTIAL_WRITE":
            context.update(
                phase="conversation_created_id_unresolved",
                create_acknowledged=True,
                conversation_created=True,
                message_status="not_sent",
            )
        if phase == "reply" and context.get("conversation_created") is True:
            code = "PARTIAL_WRITE"
        return m.DomainError(
            code,
            http_status=exc.detail.http_status,
            retry_after_seconds=exc.detail.retry_after_seconds,
            hint=(
                "Inspect the inbox/open the thread before deciding whether to send again. "
                "Continue with the known conversation_id via reply or explicit thread reuse; "
                "do not repeat creation automatically."
            ),
            context={**context, "cause_code": exc.detail.code},
        )

    def contact_seller(
        self, listing_id: str, text: str, contact_name: str | None, conversation_id: str | None
    ) -> m.ContactResult:
        content = validate_text(text)
        effective_name = contact_name
        if conversation_id is None and effective_name is None:
            effective_name = self.runtime.settings.contact_name
        if effective_name is not None:
            try:
                effective_name = (
                    TypeAdapter(m.Name).validate_python(effective_name, strict=True).strip()
                )
                effective_name.encode("utf-8", errors="strict")
                if not effective_name or any(
                    unicodedata.category(c) == "Cc" for c in effective_name
                ):
                    raise ValueError
            except (ValidationError, ValueError, UnicodeError):
                raise m.DomainError(
                    "INVALID_ARGUMENT", hint="Provide a valid contact_name (1–200 characters)."
                ) from None
        if conversation_id is None and effective_name is None:
            raise m.DomainError(
                "INVALID_ARGUMENT",
                hint="Supply contact_name or configure KA_CONTACT_NAME for creation.",
            )
        phase = "validation"
        created: bool | None = False
        try:
            with self.runtime.operation(
                account=True, tool_name="kleinanzeigen_contact_seller"
            ) as api:
                ad = self.discovery.get_listing(listing_id)
                if ad.listing_id != listing_id:
                    raise m.DomainError("UPSTREAM_SCHEMA")
                if (ad.status or "").upper() in {"DELETED", "REMOVED", "EXPIRED"}:
                    raise m.DomainError("NOT_FOUND")
                if (
                    ad.seller
                    and ad.seller.seller_id is not None
                    and ad.seller.seller_id == api.user_id
                ):
                    raise m.DomainError("INVALID_ARGUMENT", hint="Do not contact your own listing.")
                if conversation_id is not None:
                    phase = "conversation_load"
                    conv, _ = opened_thread(api.conversation(conversation_id), conversation_id)
                    phase = "validation"
                    if conv.listing_id != listing_id or conv.role != "BUYER":
                        raise m.DomainError(
                            "INVALID_ARGUMENT",
                            hint="Reuse requires a matching listing_id and BUYER role.",
                        )
                else:
                    phase = "create_conversation"
                    created = None
                    assert effective_name is not None  # Validated before entering the operation.
                    body = api.start_conversation(listing_id, effective_name)
                    created = True
                    # Synthetic contract only: top-level id. No speculative fallback paths.
                    try:
                        conversation_id = opaque_id(
                            body.get("id") if isinstance(body, dict) else None
                        )
                    except m.DomainError:
                        raise m.DomainError("PARTIAL_WRITE") from None
                phase = "reply"
                api.reply(conversation_id, content)
                now = self.runtime.budget.clock()
                self.completed = {
                    key: expiry for key, expiry in self.completed.items() if expiry > now
                }
                key = (listing_id, " ".join(unicodedata.normalize("NFC", content).split()))
                warnings = []
                if key in self.completed:
                    warnings.append(
                        m.DomainError(
                            "INVALID_ARGUMENT",
                            message="A matching contact completed here within 10 minutes.",
                            hint=(
                                "Warning only: no durable deduplication or delivery guarantee. "
                                "Inspect the thread before repeating."
                            ),
                            context={"repeat_contact": True, "listing_id": listing_id},
                        ).detail
                    )
                self.completed[key] = now + 600
                return m.ContactResult(
                    ok=True,
                    action="seller_contacted",
                    listing_id=listing_id,
                    conversation_id=conversation_id,
                    conversation_created=created is True,
                    message_sent=True,
                    warnings=warnings,
                )
        except m.DomainError as exc:
            if phase == "validation":
                raise
            status = (
                "unknown"
                if exc.detail.code in {"WRITE_OUTCOME_UNKNOWN", "PARTIAL_WRITE"}
                else "not_sent"
            )
            # A clear create rejection establishes that no conversation was created.
            if phase == "create_conversation" and status == "not_sent":
                created = False
            raise self._failure(
                exc,
                phase,
                {
                    "phase": phase,
                    "listing_id": listing_id,
                    "conversation_id": conversation_id,
                    "conversation_created": created,
                    "message_status": status if phase == "reply" else "not_sent",
                },
            ) from None
