"""Explicit MCP signatures, validation and thin injected service bindings."""

from __future__ import annotations

import json
import logging
import math
import unicodedata
from collections.abc import Callable
from decimal import Decimal
from typing import Annotated, Any, Literal, Protocol
from urllib.parse import urlsplit
from uuid import uuid4

from fastmcp import FastMCP
from fastmcp.tools import FunctionTool, ToolResult
from mcp.types import TextContent, ToolAnnotations
from pydantic import Field, JsonValue, ValidationError

from . import models as m
from .models import (
    AdDescription,
    AdTitle,
    AnalysisListing,
    DomainError,
    MessageText,
    Money,
    Name,
    NumericID,
    OpaqueID,
    PageNumber,
    Pages,
    PageSize,
    PictureURLs,
    Query,
    Term,
    Terms,
    emit,
    failure,
    output_schema,
)


class Services(Protocol):
    def invoke(self, name: str, arguments: dict[str, Any]) -> ToolResult | dict[str, JsonValue]: ...


class UnimplementedServices:
    def invoke(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        raise DomainError("UNSUPPORTED")


R = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True
)
RL = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
W = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
)
S = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True
)
U = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True
)


def validate_arguments(name: str, a: dict[str, Any]) -> None:
    for key, value in a.items():
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, str):
                if not item.strip() or any(
                    unicodedata.category(c) == "Cc" and c not in "\n\r\t" for c in item
                ):
                    raise DomainError("INVALID_ARGUMENT")
                try:
                    item.encode("utf-8", errors="strict")
                except UnicodeError:
                    raise DomainError("INVALID_ARGUMENT") from None
        if key in {"text", "title", "description"} and isinstance(value, str):
            a[key] = value.strip()
    if name == "search":
        if sum(a[k] is not None for k in ("location", "location_id", "zip_code")) > 1:
            raise DomainError("INVALID_ARGUMENT")
        has_location = any(a[k] is not None for k in ("location", "location_id", "zip_code"))
        if (
            a["distance_km"] is not None or a["sort_type"] == "DISTANCE_ASCENDING"
        ) and not has_location:
            raise DomainError("INVALID_ARGUMENT")
        if a["scan_cap"] < a["page_size"] or a["page"] * a["page_size"] >= 10000:
            raise DomainError("INVALID_ARGUMENT")
        if (
            a["min_price"] is not None
            and a["max_price"] is not None
            and a["min_price"] > a["max_price"]
        ):
            raise DomainError("INVALID_ARGUMENT")
        if (a["title_only"] and not a["query"]) or (a["attributes"] and not a["category"]):
            raise DomainError("INVALID_ARGUMENT")
    if name == "list_conversations" and a["pages"] * a["page_size"] > 200:
        raise DomainError("INVALID_ARGUMENT")
    if name in {"create_ad", "update_ad"}:
        price = a["price"]
        if (
            price is not None
            and Decimal(str(price)).as_tuple().exponent not in ("n", "N", "F")
            and int(Decimal(str(price)).as_tuple().exponent) < -2
        ):
            raise DomainError(
                "INVALID_ARGUMENT", hint="Write prices allow at most two decimal places."
            )
        if name == "create_ad":
            if a["price_type"] != "FREE" and price is None:
                raise DomainError("INVALID_ARGUMENT")
            if a["attributes"] and any(
                not isinstance(v, (str, int, float, bool))
                or (isinstance(v, float) and not math.isfinite(v))
                or len(str(v)) > 2000
                for v in a["attributes"].values()
            ):
                raise DomainError(
                    "INVALID_ARGUMENT",
                    hint=(
                        "Posting attributes accept only finite scalar values, "
                        "at most 2000 characters."
                    ),
                )
            for key, value in (a["attributes"] or {}).items():
                for item in (key, str(value)):
                    try:
                        item.encode("utf-8", errors="strict")
                    except UnicodeError:
                        raise DomainError("INVALID_ARGUMENT") from None
                    if not item.strip() or any(
                        unicodedata.category(c) == "Cc" and c not in "\n\r\t" for c in item
                    ):
                        raise DomainError("INVALID_ARGUMENT")
        elif not any(v is not None for k, v in a.items() if k != "listing_id"):
            raise DomainError("INVALID_ARGUMENT")
        if a["price_type"] == "FREE" and price not in (None, 0):
            raise DomainError("INVALID_ARGUMENT")
        for url in a["picture_urls"] or []:
            try:
                p = urlsplit(url)
                port = p.port
            except ValueError:
                raise DomainError(
                    "INVALID_ARGUMENT", hint="Use a valid HTTPS picture URL."
                ) from None
            if (
                p.scheme != "https"
                or p.hostname != "img.kleinanzeigen.de"
                or p.username
                or p.password
                or port not in (None, 443)
                or any(ord(c) < 33 or ord(c) == 127 for c in url)
            ):
                raise DomainError("INVALID_ARGUMENT")
    for key in ("conversation_ids", "listing_ids"):
        if key in a:
            a[key] = list(dict.fromkeys(a[key]))


def register_tools(server: FastMCP, services: Services, *, enable_analysis: bool = False) -> None:
    def tool(**options: Any) -> Callable[[Callable[..., ToolResult]], FunctionTool]:
        def decorate(fn: Callable[..., ToolResult]) -> FunctionTool:
            registered = FunctionTool.from_function(fn, **options)
            registered.parameters["additionalProperties"] = False
            server.add_tool(registered)
            return registered

        return decorate

    def call(name: str, args: dict[str, Any], model: type[m.StrictModel]) -> ToolResult:
        args = {k: v for k, v in args.items() if k not in {"call", "m"}}
        try:
            if (
                len(json.dumps(args, default=lambda x: x.model_dump(), ensure_ascii=True).encode())
                > 256 * 1024
            ):
                raise DomainError("LIMIT_EXCEEDED")
            validate_arguments(name, args)
            result = services.invoke(name, args)
            if isinstance(result, ToolResult):
                try:
                    if result.is_error:
                        m.Failure.model_validate(result.structured_content, strict=True)
                    else:
                        model.model_validate(result.structured_content, strict=True)
                except ValidationError:
                    raise DomainError("UPSTREAM_SCHEMA") from None
                canonical = emit(result.structured_content or {}, failed=result.is_error)
                result.content = [
                    canonical.content[0],
                    *(block for block in result.content if not isinstance(block, TextContent)),
                ]
                return result
            try:
                model.model_validate(result, strict=True)
            except ValidationError:
                raise DomainError("UPSTREAM_SCHEMA") from None
            return emit(result)
        except DomainError as exc:
            return failure(exc)
        except Exception as exc:
            logging.getLogger("kleinanzeigen_mcp").error("%s %s", type(exc).__name__, uuid4().hex)
            return failure(DomainError("INTERNAL_ERROR"))

    @tool(output_schema=output_schema(m.SearchResult), annotations=R, run_in_thread=True)
    def kleinanzeigen_search(
        query: Query | None = None,
        location: Name | None = None,
        category: Name | None = None,
        distance_km: Annotated[float, Field(gt=0, le=500, allow_inf_nan=False)] | None = None,
        min_price: Money | None = None,
        max_price: Money | None = None,
        exclude: Terms | None = None,
        sort_type: Literal[
            "RECOMMENDED",
            "PRICE_ASCENDING",
            "PRICE_DESCENDING",
            "DATE_DESCENDING",
            "DISTANCE_ASCENDING",
        ]
        | None = None,
        pages: Pages = 1,
        page_size: PageSize = 25,
        page: PageNumber = 0,
        location_id: NumericID | None = None,
        zip_code: Annotated[str, Field(pattern=r"^[0-9]{5}$")] | None = None,
        attributes: Annotated[
            dict[
                str,
                Annotated[str, Field(min_length=1, max_length=200)]
                | Annotated[
                    list[Annotated[str, Field(min_length=1, max_length=200)]],
                    Field(min_length=1, max_length=20),
                ],
            ],
            Field(max_length=30),
        ]
        | None = None,
        ad_type: Literal["OFFERED", "WANTED"] | None = "OFFERED",
        poster_type: Literal["PRIVATE", "COMMERCIAL"] | None = None,
        picture_required: bool = False,
        shippable: bool = False,
        buy_now_only: bool = False,
        include_top_ads: bool = False,
        title_only: bool = False,
        require: Terms | None = None,
        exclude_scope: Literal["text", "title"] = "text",
        exclude_mode: Literal["substring", "token"] = "substring",
        scan_cap: Annotated[int, Field(ge=1, le=200)] = 200,
        summary_chars: Annotated[int, Field(ge=100, le=2000)] | None = None,
    ) -> ToolResult:
        """Bounded search."""
        return call("search", locals(), m.SearchResult)

    @tool(output_schema=output_schema(m.Listing), annotations=R, run_in_thread=True)
    def kleinanzeigen_get_listing(listing_id: NumericID) -> ToolResult:
        """Bounded get listing."""
        return call("get_listing", locals(), m.Listing)

    @tool(output_schema=output_schema(m.ImagesResult), annotations=R, run_in_thread=True)
    def kleinanzeigen_get_ad_images(
        listing_id: NumericID,
        max_images: Annotated[int, Field(ge=1, le=10)] = 4,
        start: Annotated[int, Field(ge=0)] = 0,
    ) -> ToolResult:
        """Bounded get ad images."""
        return call("get_ad_images", locals(), m.ImagesResult)

    @tool(output_schema=output_schema(m.SellerResult), annotations=R, run_in_thread=True)
    def kleinanzeigen_get_seller_info(seller_id: NumericID) -> ToolResult:
        """Bounded get seller info."""
        return call("get_seller_info", locals(), m.SellerResult)

    @tool(output_schema=output_schema(m.LocationsResult), annotations=R, run_in_thread=True)
    def kleinanzeigen_search_locations(
        query: Annotated[str, Field(min_length=2, max_length=200)],
        limit: Annotated[int, Field(ge=1, le=50)] = 20,
    ) -> ToolResult:
        """Bounded search locations."""
        return call("search_locations", locals(), m.LocationsResult)

    @tool(output_schema=output_schema(m.CategoriesResult), annotations=RL, run_in_thread=True)
    def kleinanzeigen_list_categories(
        query: Name | None = None,
        parent_id: NumericID | None = None,
        limit: Annotated[int, Field(ge=1, le=159)] = 40,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> ToolResult:
        """Bounded list categories."""
        return call("list_categories", locals(), m.CategoriesResult)

    @tool(output_schema=output_schema(m.FiltersResult), annotations=R, run_in_thread=True)
    def kleinanzeigen_get_category_filters(category: Name) -> ToolResult:
        """Bounded get category filters."""
        return call("get_category_filters", locals(), m.FiltersResult)

    @tool(output_schema=output_schema(m.ConversationsResult), annotations=R, run_in_thread=True)
    def kleinanzeigen_list_conversations(
        page: PageNumber = 0,
        page_size: Annotated[int, Field(ge=1, le=100)] = 50,
        unread_only: bool = False,
        unread_first: bool = True,
        pages: Pages = 1,
    ) -> ToolResult:
        """Scan bounded inbox pages; unread ordering/filtering covers only those pages."""
        return call("list_conversations", locals(), m.ConversationsResult)

    @tool(output_schema=output_schema(m.ThreadResult), annotations=S, run_in_thread=True)
    def kleinanzeigen_get_conversation(
        conversation_id: OpaqueID,
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=2000)] | None = None,
    ) -> ToolResult:
        """Load once via PUT; paginate full text locally. May affect inbox state."""
        return call("get_conversation", locals(), m.ThreadResult)

    @tool(output_schema=output_schema(m.ThreadResult), annotations=S, run_in_thread=True)
    def kleinanzeigen_get_messages(
        conversation_id: OpaqueID,
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=2000)] | None = None,
    ) -> ToolResult:
        """Alias for thread loading; may affect inbox state."""
        return call("get_messages", locals(), m.ThreadResult)

    @tool(output_schema=output_schema(m.MarkReadResult), annotations=S, run_in_thread=True)
    def kleinanzeigen_mark_read(
        conversation_ids: Annotated[list[OpaqueID], Field(min_length=1, max_length=50)],
    ) -> ToolResult:
        """Mark the supplied threads read in one POST; duplicates are removed."""
        return call("mark_read", locals(), m.MarkReadResult)

    @tool(output_schema=output_schema(m.ContactResult), annotations=W, run_in_thread=True)
    def kleinanzeigen_contact_seller(
        listing_id: NumericID,
        text: MessageText,
        contact_name: Name | None = None,
        conversation_id: OpaqueID | None = None,
    ) -> ToolResult:
        """Create with contactName then send text once, or reuse a matching BUYER thread.

        Read listing/context before composing. On partial/unknown outcomes inspect
        the inbox and recover with a known thread ID; do not repeat creation blindly.
        """
        return call("contact_seller", locals(), m.ContactResult)

    @tool(output_schema=output_schema(m.ReplyResult), annotations=W, run_in_thread=True)
    def kleinanzeigen_reply(conversation_id: OpaqueID, text: MessageText) -> ToolResult:
        """Send the supplied text immediately, once."""
        return call("reply", locals(), m.ReplyResult)

    @tool(output_schema=output_schema(m.ListingPage), annotations=R, run_in_thread=True)
    def kleinanzeigen_list_my_ads(
        page: PageNumber = 0, page_size: PageSize = 25, query: Query | None = None
    ) -> ToolResult:
        """Bounded list my ads."""
        return call("list_my_ads", locals(), m.ListingPage)

    @tool(output_schema=output_schema(m.Listing), annotations=R, run_in_thread=True)
    def kleinanzeigen_get_my_ad(listing_id: NumericID) -> ToolResult:
        """Bounded get my ad."""
        return call("get_my_ad", locals(), m.Listing)

    @tool(output_schema=output_schema(m.CreateResult), annotations=W, run_in_thread=True)
    def kleinanzeigen_create_ad(
        title: AdTitle,
        description: AdDescription,
        category_id: NumericID,
        location_id: NumericID,
        price_type: Literal["FIXED", "NEGOTIABLE", "FREE"] = "FIXED",
        price: Money | None = None,
        contact_name: Name | None = None,
        phone: Annotated[str, Field(min_length=1, max_length=50)] | None = None,
        attributes: Annotated[dict[str, JsonValue], Field(max_length=50)] | None = None,
        picture_urls: PictureURLs | None = None,
        shipping_options: Annotated[list[Term], Field(max_length=20)] | None = None,
    ) -> ToolResult:
        """Post one PRIVATE/OFFERED ad with scalar attributes and optional shipping IDs.

        Requires an account email and precise city/postcode location. Inspect owned
        ads before repeating a partial/unknown creation.
        """
        return call("create_ad", locals(), m.CreateResult)

    @tool(output_schema=output_schema(m.UpdateResult), annotations=U, run_in_thread=True)
    def kleinanzeigen_update_ad(
        listing_id: NumericID,
        title: AdTitle | None = None,
        description: AdDescription | None = None,
        price: Money | None = None,
        price_type: Literal["FIXED", "NEGOTIABLE", "FREE"] | None = None,
        picture_urls: PictureURLs | None = None,
    ) -> ToolResult:
        """Selective XML PUT rebuilt from the asserted contract; live proof is pending.

        Null means unchanged; empty picture_urls intentionally clears pictures.
        Shipping/unknown-field retention and paused-ad updates require live validation.
        """
        return call("update_ad", locals(), m.UpdateResult)

    @tool(output_schema=output_schema(m.PauseResult), annotations=S, run_in_thread=True)
    def kleinanzeigen_pause_ad(listing_id: NumericID) -> ToolResult:
        """Bounded pause ad."""
        return call("pause_ad", locals(), m.PauseResult)

    @tool(output_schema=output_schema(m.ActivateResult), annotations=S, run_in_thread=True)
    def kleinanzeigen_activate_ad(listing_id: NumericID) -> ToolResult:
        """Bounded activate ad."""
        return call("activate_ad", locals(), m.ActivateResult)

    @tool(output_schema=output_schema(m.RenewResult), annotations=W, run_in_thread=True)
    def kleinanzeigen_renew_ad(listing_id: NumericID) -> ToolResult:
        """Check eligibility; renewal is blocked until the live mapping is verified."""
        return call("renew_ad", locals(), m.RenewResult)

    @tool(output_schema=output_schema(m.DeleteResult), annotations=U, run_in_thread=True)
    def kleinanzeigen_delete_ad(listing_id: NumericID) -> ToolResult:
        """Permanently delete an owned ad once; inspect state after an unknown outcome."""
        return call("delete_ad", locals(), m.DeleteResult)

    @tool(output_schema=output_schema(m.UploadResult), annotations=W, run_in_thread=True)
    def kleinanzeigen_upload_picture(
        image_path: Annotated[str, Field(min_length=1)],
        filename: Annotated[str, Field(min_length=1, max_length=255)] | None = None,
        content_type: Literal["image/jpeg", "image/png", "image/webp", "image/gif"] | None = None,
    ) -> ToolResult:
        """Bounded upload picture."""
        return call("upload_picture", locals(), m.UploadResult)

    @tool(output_schema=output_schema(m.ListingPage), annotations=R, run_in_thread=True)
    def kleinanzeigen_watchlist(page: PageNumber = 0, page_size: PageSize = 25) -> ToolResult:
        """Bounded watchlist."""
        return call("watchlist", locals(), m.ListingPage)

    @tool(output_schema=output_schema(m.ExtendResult), annotations=R, run_in_thread=True)
    def kleinanzeigen_extend_status(
        listing_ids: Annotated[list[NumericID], Field(min_length=1, max_length=50)],
    ) -> ToolResult:
        """Bounded extend status."""
        return call("extend_status", locals(), m.ExtendResult)

    if not enable_analysis:
        return

    @tool(output_schema=output_schema(m.AnalysisResult), annotations=RL, run_in_thread=True)
    def kleinanzeigen_analyze_listings(
        listings: Annotated[list[AnalysisListing], Field(min_length=1, max_length=50)],
    ) -> ToolResult:
        """Bounded analyze listings."""
        return call("analyze_listings", locals(), m.AnalysisResult)
