"""Process-local service dispatch with lazy, injected upstream clients."""

from __future__ import annotations

from typing import Any

from fastmcp.tools import ToolResult
from pydantic import JsonValue, ValidationError

from .ads import Ads
from .config import Settings
from .discovery import Discovery
from .messaging import Messaging
from .models import DomainError
from .upstream import Runtime


class RuntimeServices:
    DISCOVERY = frozenset(
        {
            "search",
            "get_listing",
            "get_ad_images",
            "get_seller_info",
            "search_locations",
            "list_categories",
            "get_category_filters",
        }
    )
    ACCOUNT_READS = frozenset({"list_my_ads", "get_my_ad", "watchlist", "extend_status"})
    AD_WRITES = frozenset(
        {
            "create_ad",
            "update_ad",
            "pause_ad",
            "activate_ad",
            "renew_ad",
            "delete_ad",
            "upload_picture",
        }
    )
    MESSAGING = frozenset(
        {
            "list_conversations",
            "get_conversation",
            "get_messages",
            "mark_read",
            "contact_seller",
            "reply",
        }
    )

    def __init__(self, settings: Settings, *, runtime: Runtime | None = None):
        self.runtime = runtime if runtime is not None else Runtime(settings)
        self.discovery = Discovery(self.runtime)
        self.ads = Ads(self.runtime)
        self.messaging = Messaging(self.runtime, self.discovery)

    def invoke(self, name: str, arguments: dict[str, Any]) -> ToolResult | dict[str, JsonValue]:
        service: Discovery | Ads | Messaging
        if name in self.DISCOVERY:
            service = self.discovery
        elif name in self.MESSAGING:
            service = self.messaging
        else:
            service = self.ads
        if name not in self.DISCOVERY | self.ACCOUNT_READS | self.AD_WRITES | self.MESSAGING:
            raise DomainError("UNSUPPORTED")
        try:
            result = getattr(service, name)(**arguments)
            return result if isinstance(result, ToolResult) else result.model_dump(mode="json")
        except (ValidationError, KeyError, TypeError, ValueError, RecursionError):
            raise DomainError("UPSTREAM_SCHEMA") from None

    def close(self) -> None:
        self.runtime.close()
