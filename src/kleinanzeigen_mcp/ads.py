"""Owned ads, validated writes and explicitly unverified renewal eligibility."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import JsonValue

from . import models as m
from .discovery import ads_block, envelope, listing, number, numeric_id, obj, text
from .upstream import Runtime


def money(value: float) -> Decimal:
    """Decimal money without binary float rounding or scientific XML notation."""
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0 or int(amount.as_tuple().exponent) < -2:
        raise m.DomainError(
            "INVALID_ARGUMENT", hint="Use a nonnegative price with at most two decimal places."
        )
    return amount.normalize()


# Observed live 2026-10-16 (docs/validation-2026-10-16.md):
# `eligibleToExtend` turned out to be a date string, not a boolean; the real
# eligibility signal is the `extendAdStatus` enum. Only statuses observed live
# may be mapped - do not guess the positive case before it is seen.
_EXTEND_STATUS_ELIGIBLE: dict[str, bool] = {"CANNOT_BE_EXTENDED": False}


def extend_facts(raw: Any, listing_id: str) -> tuple[str | None, str | None]:
    """Extract (extendAdStatus, eligibleToExtend) for one ad from a raw list."""
    if not isinstance(raw, list):
        return None, None
    for item in raw:
        if not isinstance(item, dict):
            continue
        ad_id = item.get("adId")
        if isinstance(ad_id, bool) or not isinstance(ad_id, (int, str)):
            continue
        if str(ad_id) != listing_id:
            continue
        status = item.get("extendAdStatus")
        date = item.get("eligibleToExtend")
        return (
            status if isinstance(status, str) and status else None,
            date if isinstance(date, str) and date else None,
        )
    return None, None


class Ads:
    def __init__(self, runtime: Runtime):
        self.runtime = runtime

    def list_my_ads(self, page: int, page_size: int, query: str | None) -> m.ListingPage:
        with self.runtime.operation(account=True, tool_name="kleinanzeigen_list_my_ads") as api:
            return self._page(api.my_ads_raw(page=page, size=page_size, q=query), page, page_size)

    def watchlist(self, page: int, page_size: int) -> m.ListingPage:
        with self.runtime.operation(account=True, tool_name="kleinanzeigen_watchlist") as api:
            return self._page(api.watchlist_raw(page=page, size=page_size), page, page_size)

    @staticmethod
    def _page(raw: dict[str, Any], page: int, size: int) -> m.ListingPage:
        _, ads = ads_block(raw)
        if len(ads) > size:
            raise m.DomainError("UPSTREAM_SCHEMA")
        items = [listing(ad, account=True) for ad in ads]
        full = len(ads) == size
        return m.ListingPage(
            page=page,
            count=len(items),
            listings=items,
            next_page=page + 1 if full else None,
            has_more=None if full else False,
        )

    def get_my_ad(self, listing_id: str) -> m.Listing:
        with self.runtime.operation(account=True, tool_name="kleinanzeigen_get_my_ad") as api:
            raw = api.get_my_ad_raw(listing_id)
            return listing(envelope(raw, "ad") if "id" not in raw else raw, account=True)

    def create_ad(
        self,
        title: str,
        description: str,
        category_id: str,
        location_id: str,
        price_type: m.PriceType = "FIXED",
        price: float | None = None,
        contact_name: str | None = None,
        phone: str | None = None,
        attributes: dict[str, JsonValue] | None = None,
        picture_urls: list[str] | None = None,
        shipping_options: list[str] | None = None,
    ) -> m.CreateResult:
        amount = money(price) if price is not None else None
        with self.runtime.operation(account=True, tool_name="kleinanzeigen_create_ad") as api:
            email = getattr(api._require_auth(), "email", None)
            if not isinstance(email, str) or not email.strip():
                raise m.DomainError(
                    "INVALID_ARGUMENT",
                    message="Creating an ad requires an account email.",
                    hint=(
                        "Renew login separately with an account email, then retry. "
                        "Use search_locations to choose a precise city/postcode location."
                    ),
                )
            ad_id = api.post_ad(
                title=title,
                description=description,
                category_id=category_id,
                location_id=location_id,
                price=amount,
                price_type=price_type,
                contact_name=contact_name,
                email=email,
                phone=phone,
                attributes=attributes,
                picture_urls=picture_urls,
                shipping_options=shipping_options,
            )
            if not isinstance(ad_id, str) or not ad_id.isascii() or not ad_id.isdigit():
                raise m.DomainError(
                    "PARTIAL_WRITE",
                    message="Ad creation was acknowledged without a usable numeric ID.",
                    hint="Inspect owned ads before deciding whether to create again.",
                    context={"phase": "ad_created_id_unresolved"},
                )
            return m.CreateResult(ok=True, action="ad_created", listing_id=ad_id)

    def update_ad(
        self,
        listing_id: str,
        title: str | None = None,
        description: str | None = None,
        price: float | None = None,
        price_type: m.PriceType | None = None,
        picture_urls: list[str] | None = None,
    ) -> m.UpdateResult:
        fields = [
            key
            for key, value in {
                "title": title,
                "description": description,
                "price": price,
                "price_type": price_type,
                "picture_urls": picture_urls,
            }.items()
            if value is not None
        ]
        amount = money(price) if price is not None else None
        with self.runtime.operation(account=True, tool_name="kleinanzeigen_update_ad") as api:
            if (price is not None and price_type is None) or (
                price is None and price_type in {"FIXED", "NEGOTIABLE"}
            ):
                raw = api.get_my_ad_raw(listing_id)
                ad = envelope(raw, "ad") if "id" not in raw else raw
                if numeric_id(ad.get("id"), required=True) != listing_id:
                    raise m.DomainError("UPSTREAM_SCHEMA")
                current_price = obj(ad.get("price"))
                effective_type = price_type or {
                    "SPECIFIED_AMOUNT": "FIXED",
                    "PLEASE_CONTACT": "NEGOTIABLE",
                    "FIXED": "FIXED",
                    "NEGOTIABLE": "NEGOTIABLE",
                    "FREE": "FREE",
                }.get(text(current_price.get("price-type")) or "")
                if effective_type is None:
                    raise m.DomainError(
                        "UPSTREAM_SCHEMA",
                        hint="Read the owned ad and supply an explicit price_type and price.",
                    )
                if effective_type == "FREE" and amount not in (None, 0):
                    raise m.DomainError(
                        "INVALID_ARGUMENT", hint="Supply FIXED or NEGOTIABLE to price a FREE ad."
                    )
                if effective_type == "FREE":
                    # A zero price keeps FREE and must not serialize an amount.
                    price_type, amount = "FREE", None
                if price is None:
                    existing_amount = number(current_price.get("amount"))
                    if existing_amount is None:
                        raise m.DomainError(
                            "INVALID_ARGUMENT",
                            hint="Supply a price when changing to FIXED or NEGOTIABLE.",
                        )
                    amount = money(existing_amount)
            api.update_ad(
                listing_id,
                title=title,
                description=description,
                price=amount,
                price_type=price_type,
                picture_urls=picture_urls,
            )
            return m.UpdateResult(
                ok=True, action="ad_updated", listing_id=listing_id, fields=fields
            )

    def pause_ad(self, listing_id: str) -> m.PauseResult:
        with self.runtime.operation(account=True, tool_name="kleinanzeigen_pause_ad") as api:
            api.pause_ad(listing_id)
            return m.PauseResult(ok=True, action="ad_paused", listing_id=listing_id)

    def activate_ad(self, listing_id: str) -> m.ActivateResult:
        with self.runtime.operation(account=True, tool_name="kleinanzeigen_activate_ad") as api:
            api.activate_ad(listing_id)
            return m.ActivateResult(ok=True, action="ad_activated", listing_id=listing_id)

    def delete_ad(self, listing_id: str) -> m.DeleteResult:
        with self.runtime.operation(account=True, tool_name="kleinanzeigen_delete_ad") as api:
            api.delete_ad(listing_id)
            return m.DeleteResult(ok=True, action="ad_deleted", listing_id=listing_id)

    def upload_picture(
        self, image_path: str, filename: str | None = None, content_type: str | None = None
    ) -> m.UploadResult:
        with self.runtime.operation(account=True, tool_name="kleinanzeigen_upload_picture") as api:
            url = api.upload_picture(image_path, filename=filename, content_type=content_type)
            return m.UploadResult(ok=True, action="picture_uploaded", url=url)

    def renew_ad(self, listing_id: str) -> m.RenewResult:
        with self.runtime.operation(account=True, tool_name="kleinanzeigen_renew_ad") as api:
            status, date = extend_facts(api.extend_status([listing_id]), listing_id)
            if status is not None and _EXTEND_STATUS_ELIGIBLE.get(status) is False:
                # Observed live: the ad is not extendable yet; the date field carries the hint.
                detail = f" (date: {date})" if date else ""
                raise m.DomainError(
                    "UNSUPPORTED",
                    message="Kleinanzeigen reports this ad cannot be extended right now.",
                    hint=f"extendAdStatus={status}{detail}. No extension was attempted.",
                    context={"listing_id": listing_id, "phase": "eligibility_negative"},
                )
            # Positive/unknown extend-status semantics stay unverified: no extension attempt.
            raise m.DomainError(
                "UNSUPPORTED",
                message="Renewal eligibility cannot yet be established.",
                hint=(
                    "Positive extend-status semantics are still unverified; confirm on a live "
                    "eligible ad before renewing. No extension was attempted."
                ),
                context={"listing_id": listing_id, "phase": "eligibility_unverified"},
            )

    def extend_status(self, listing_ids: list[str]) -> m.ExtendResult:
        ids = list(dict.fromkeys(listing_ids))
        with self.runtime.operation(account=True, tool_name="kleinanzeigen_extend_status") as api:
            raw = api.extend_status(ids)
        entries: dict[str, dict[str, Any]] = {}
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                ad_id = item.get("adId")
                if isinstance(ad_id, bool) or not isinstance(ad_id, (int, str)):
                    continue
                if not isinstance(item.get("extendAdStatus"), str):
                    continue
                entries.setdefault(str(ad_id), item)
        if not entries:
            # Response without the observed shape keeps the honest null result.
            return m.ExtendResult(
                count=len(ids),
                eligibility=[
                    m.Eligibility(listing_id=lid, eligible=None, reason=None) for lid in ids
                ],
                schema_verified=False,
                warnings=[
                    m.DomainError(
                        "UNSUPPORTED",
                        hint=(
                            "Extend-status response did not match the evidenced shape; "
                            "eligibility left unknown. Do not infer renewal permission."
                        ),
                    ).detail
                ],
            )
        eligibility: list[m.Eligibility] = []
        warnings: list[Any] = []
        for lid in ids:
            item = entries.get(lid)
            if item is None:
                eligibility.append(m.Eligibility(listing_id=lid, eligible=None, reason=None))
                warnings.append(
                    m.DomainError(
                        "UPSTREAM_SCHEMA", hint=f"No extend status returned for ad {lid}."
                    ).detail
                )
                continue
            status = item["extendAdStatus"]
            eligible = _EXTEND_STATUS_ELIGIBLE.get(status)
            date = item.get("eligibleToExtend")
            reason = status
            if eligible is False and isinstance(date, str) and date:
                reason = f"{status} (date: {date})"
            if eligible is None:
                warnings.append(
                    m.DomainError(
                        "UNSUPPORTED",
                        hint=f"Extend status {status!r} is not yet mapped; treat as unknown.",
                    ).detail
                )
            eligibility.append(m.Eligibility(listing_id=lid, eligible=eligible, reason=reason))
        return m.ExtendResult(
            count=len(ids), eligibility=eligibility, schema_verified=True, warnings=warnings
        )
