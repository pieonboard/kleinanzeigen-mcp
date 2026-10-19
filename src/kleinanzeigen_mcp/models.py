"""Strict tool contracts and the redacted error boundary."""

from __future__ import annotations

import json
from typing import Annotated, Literal

from fastmcp.tools import ToolResult
from fastmcp.utilities.types import Image
from mcp.types import TextContent
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, JsonValue


def trimmed(value: object) -> object:
    return value.strip() if isinstance(value, str) else value


MessageText = Annotated[str, BeforeValidator(trimmed), Field(min_length=1, max_length=5000)]
AdTitle = Annotated[str, BeforeValidator(trimmed), Field(min_length=1, max_length=200)]
AdDescription = Annotated[str, BeforeValidator(trimmed), Field(min_length=1, max_length=20000)]

NumericID = Annotated[str, Field(pattern=r"^[0-9]{1,32}$", min_length=1, max_length=32)]
OpaqueID = Annotated[
    str, Field(pattern=r"^[^\x00-\x20\x7f/\\?#,]{1,256}$", min_length=1, max_length=256)
]
Name = Annotated[str, Field(min_length=1, max_length=200)]
Query = Annotated[str, Field(min_length=1, max_length=500)]
Money = Annotated[float, Field(ge=0, allow_inf_nan=False)]
PriceType = Literal["FIXED", "NEGOTIABLE", "FREE"]
PageNumber = Annotated[int, Field(ge=0, le=399)]
PageSize = Annotated[int, Field(ge=1, le=50)]
Pages = Annotated[int, Field(ge=1, le=4)]
Term = Annotated[str, Field(min_length=1, max_length=100)]
Terms = Annotated[list[Term], Field(max_length=20)]
PictureURLs = Annotated[
    list[Annotated[str, Field(min_length=1, max_length=2048)]], Field(max_length=20)
]
ErrorCode = Literal[
    "INVALID_ARGUMENT",
    "AUTH_REQUIRED",
    "AUTH_EXPIRED",
    "AUTH_STORAGE_ERROR",
    "AUTH_OR_PERMISSION_DENIED",
    "APP_CREDENTIALS_REJECTED",
    "IP_BLOCKED",
    "NOT_FOUND",
    "RATE_LIMITED",
    "UPSTREAM_UNAVAILABLE",
    "UPSTREAM_SCHEMA",
    "BUSY",
    "LIMIT_EXCEEDED",
    "UNSUPPORTED",
    "WRITE_OUTCOME_UNKNOWN",
    "PARTIAL_WRITE",
    "INTERNAL_ERROR",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ErrorDetail(StrictModel):
    code: ErrorCode
    message: str
    hint: str
    retryable: bool = False
    http_status: int | None = None
    retry_after_seconds: float | None = None
    context: dict[str, JsonValue] = Field(default_factory=dict)


class Failure(StrictModel):
    error: ErrorDetail


class DomainError(Exception):
    def __init__(
        self,
        code: ErrorCode,
        *,
        message: str | None = None,
        hint: str | None = None,
        retryable: bool = False,
        http_status: int | None = None,
        retry_after_seconds: float | None = None,
        context: dict[str, JsonValue] | None = None,
    ):
        hints = {
            "AUTH_REQUIRED": "Run the separate kleinanzeigen-api login CLI.",
            "AUTH_EXPIRED": "Renew login separately, then replace the token file or restart.",
            "AUTH_STORAGE_ERROR": "Use a private, owner-readable regular token file (0600).",
            "WRITE_OUTCOME_UNKNOWN": (
                "Inspect remote state before deciding whether to repeat the write."
            ),
            "PARTIAL_WRITE": (
                "Inspect the completed phase before continuing; do not repeat automatically."
            ),
            "UNSUPPORTED": "This workflow is not implemented yet; see docs/OPERATIONS.md.",
            "INVALID_ARGUMENT": "Check the supplied fields, types and documented limits.",
            "RATE_LIMITED": "Retry deliberately after the indicated cooldown.",
            "BUSY": "Wait for the active operation to finish, then try again.",
            "NOT_FOUND": "Verify the resource ID and whether it was removed.",
        }
        self.detail = ErrorDetail(
            code=code,
            message=message or code.replace("_", " ").capitalize(),
            hint=hint or hints.get(code, "Check configuration or retry later."),
            retryable=retryable,
            http_status=http_status,
            retry_after_seconds=retry_after_seconds,
            context=context or {},
        )
        super().__init__(code)


def emit(
    payload: dict[str, JsonValue], *, failed: bool = False, images: list[Image] | None = None
) -> ToolResult:
    text = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    if len(text.encode("utf-8")) > 1024 * 1024:
        raise DomainError("LIMIT_EXCEEDED", hint="Request a smaller page or shorter summary.")
    return ToolResult(
        content=[TextContent(type="text", text=text), *(images or [])],
        structured_content=payload,
        is_error=failed,
    )


def failure(error: DomainError) -> ToolResult:
    return emit(Failure(error=error.detail).model_dump(mode="json"), failed=True)


class Success(StrictModel):
    partial: bool = False
    warnings: list[ErrorDetail] = Field(default_factory=list)


class Location(StrictModel):
    city: str | None
    postal_code: str | None
    latitude: float | None
    longitude: float | None


class Seller(StrictModel):
    seller_id: str | None
    name: str | None
    poster_type: str | None
    account_type: str | None


class Listing(Success):
    listing_id: str
    title: str
    description: str
    description_truncated: bool
    price: float | None
    currency: str | None
    price_type: str | None
    location: Location
    url: str | None
    created_at: str | None
    updated_at: str | None
    status: str | None
    images: list[str]
    seller: Seller | None
    category_id: str | None
    attributes: dict[str, JsonValue]
    attribute_keys: dict[str, JsonValue]
    size_m2: float | None
    rooms: float | None
    ad_type: str | None
    shipping_options: list[str]
    buy_now: bool | None
    is_top_ad: bool | None


class ResolvedLocation(StrictModel):
    id: str
    label: str


class ListingPage(Success):
    page: int
    count: int
    next_page: int | None
    has_more: bool | None
    listings: list[Listing]


class SearchResult(ListingPage):
    page_size: int
    pages_fetched: int
    scanned: int
    duplicates_removed: int
    total: int | None
    total_relation: Literal["exact", "at_least", "unknown"]
    scan_cap_reached: bool
    stop_reason: Literal["requested_pages", "scan_cap", "exhausted", "deadline", "upstream_error"]
    resolved_location: ResolvedLocation | None


class ImageMetadata(StrictModel):
    index: int
    url: str
    mime_type: str
    bytes: int


class ImagesResult(Success):
    listing_id: str
    count: int
    images: list[ImageMetadata]
    total_images: int
    next_start: int | None


class Rating(StrictModel):
    average_rating: float | None
    star_rating: JsonValue
    review_count: int | None


class Reputation(StrictModel):
    total: Rating | None
    manual: Rating | None
    automatic: Rating | None


class SellerResult(Success):
    seller_id: str
    name: str | None
    account_type: str | None
    member_since: str | None
    active_ads: int | None
    total_ads: int | None
    followers: int | None
    reply_speed: str | None
    average_rating: float | None
    badges: list[JsonValue]
    reputation: Reputation


class LocationMatch(StrictModel):
    id: str
    name: str
    region: str | None
    latitude: float | None
    longitude: float | None
    radius_km: float | None


class LocationsResult(Success):
    count: int
    locations: list[LocationMatch]
    truncated: bool


class Category(StrictModel):
    id: str
    title: str
    path: str
    parent_id: str | None


class CategoriesResult(Success):
    count: int
    total: int
    categories: list[Category]
    next_offset: int | None


class FilterValue(StrictModel):
    value: str
    label: str | None


class GlobalParameter(StrictModel):
    key: str
    label: str | None
    type: str | None
    search_param: str | None
    values: list[FilterValue]


class AttributeFilter(StrictModel):
    key: str
    label: str | None
    type: str | None
    multi_select: bool
    values: list[FilterValue]


class FiltersResult(Success):
    category_id: str
    global_parameters: list[GlobalParameter]
    attributes: list[AttributeFilter]


class Conversation(StrictModel):
    conversation_id: str
    listing_id: str | None
    listing_title: str | None
    listing_status: str | None
    listing_price: float | None
    currency: str | None
    listing_price_type: str | None
    role: str | None
    counterparty: str | None
    unread: bool | None
    unread_count: int | None
    last_message_at: str | None
    last_message_preview: str | None


class Message(StrictModel):
    message_id: str | None
    conversation_id: str
    listing_id: str | None
    listing_title: str | None
    sender: str | None
    timestamp: str | None
    text: str
    direction: Literal["sent", "received", "unknown"]
    unread: None
    message_type: str | None
    attachment_count: int


class ConversationsResult(Success):
    page: int
    count: int
    conversations: list[Conversation]
    scanned: int
    pages_fetched: int
    unread_returned: int
    next_page: int | None
    has_more: bool | None
    ordering_scope: Literal["scanned_pages"]


class ThreadResult(Success):
    conversation: Conversation
    messages: list[Message]
    count: int
    offset: int
    next_offset: int | None
    has_more: bool
    full_returned_payload: bool
    upstream_history_complete: bool | None


class MarkReadResult(Success):
    ok: Literal[True]
    action: Literal["conversations_marked_read"]
    conversation_ids: list[str]


class ContactResult(Success):
    ok: Literal[True]
    action: Literal["seller_contacted"]
    listing_id: str
    conversation_id: str
    conversation_created: bool
    message_sent: Literal[True]


class ReplyResult(Success):
    ok: Literal[True]
    action: Literal["reply_sent"]
    conversation_id: str


class AdWriteResult(Success):
    ok: Literal[True]
    action: Literal[
        "ad_created", "ad_updated", "ad_paused", "ad_activated", "ad_renewed", "ad_deleted"
    ]
    listing_id: str


class CreateResult(AdWriteResult):
    action: Literal["ad_created"]


class PauseResult(AdWriteResult):
    action: Literal["ad_paused"]


class ActivateResult(AdWriteResult):
    action: Literal["ad_activated"]


class RenewResult(AdWriteResult):
    action: Literal["ad_renewed"]


class DeleteResult(AdWriteResult):
    action: Literal["ad_deleted"]


class UpdateResult(AdWriteResult):
    action: Literal["ad_updated"]
    fields: list[str]


class UploadResult(Success):
    ok: Literal[True]
    action: Literal["picture_uploaded"]
    url: str


class Eligibility(StrictModel):
    listing_id: str
    eligible: bool | None
    reason: str | None


class ExtendResult(Success):
    count: int
    eligibility: list[Eligibility]
    schema_verified: bool


class AnalysisListing(StrictModel):
    listing_id: NumericID
    title: str
    description: str
    price: Money | None = None
    seller_id: NumericID | None = None
    poster_type: str | None = None
    account_type: str | None = None
    image_urls: list[str] = Field(default_factory=list)


class Signal(StrictModel):
    kind: Literal["dealer_signal", "damage_mention", "possible_duplicate"]
    evidence: str
    field: str
    related_listing_ids: list[str]


class Finding(StrictModel):
    listing_id: str
    signals: list[Signal]


class AnalysisResult(Success):
    findings: list[Finding]
    disclaimer: str


def output_schema(success: type[StrictModel]) -> dict[str, JsonValue]:
    # Separate namespaces avoid collisions while keeping each branch self-contained.
    a, b = success.model_json_schema(), Failure.model_json_schema()
    definitions = {**a.pop("$defs", {}), **b.pop("$defs", {})}
    return {"type": "object", "oneOf": [a, b], "$defs": definitions}
