"""Bounded public reads and raw JAXB normalization.

Text matching, description cleaning and data adapted from Samxel (MIT); see
bundled data/SAMXEL-LICENSE.txt. No reference checkout is imported at runtime.
"""

from __future__ import annotations

import html
import json
import math
import re
import unicodedata
from collections import OrderedDict
from collections.abc import Callable
from functools import lru_cache
from importlib.resources import files
from typing import Any

from fastmcp.tools import ToolResult
from fastmcp.utilities.types import Image
from kleinanzeigen_api.categories import find_categories, resolve_category
from pydantic import JsonValue, ValidationError

from . import models as m
from .models import DomainError
from .upstream import Runtime

JSON_LIMIT = 1024 * 1024
IMAGE_LIMIT = 5 * 1024 * 1024
IMAGE_TOTAL = 15 * 1024 * 1024


class Cache[T]:
    """Bounded TTL cache, accessed only while the runtime operation lock is held."""

    def __init__(self, size: int, ttl: float, clock: Callable[[], float]):
        self.size, self.ttl, self.clock = size, ttl, clock
        self.items: OrderedDict[str, tuple[float, T]] = OrderedDict()

    def get(self, key: str) -> T | None:
        entry = self.items.get(key)
        if entry is None:
            return None
        if self.clock() >= entry[0]:
            del self.items[key]
            return None
        self.items.move_to_end(key)
        return entry[1]

    def put(self, key: str, value: T) -> T:
        self.items[key] = (self.clock() + self.ttl, value)
        self.items.move_to_end(key)
        while len(self.items) > self.size:
            self.items.popitem(last=False)
        return value


def value(node: Any) -> Any:
    for _ in range(20):
        if not isinstance(node, dict) or "value" not in node:
            return node
        node = node["value"]
    raise DomainError("UPSTREAM_SCHEMA")


def obj(node: Any) -> dict[str, Any]:
    node = value(node)
    if node is None:
        return {}
    if not isinstance(node, dict):
        raise DomainError("UPSTREAM_SCHEMA")
    return node


def rows(node: Any) -> list[dict[str, Any]]:
    if node is None or (isinstance(node, dict) and not node):
        return []
    if isinstance(node, dict) and isinstance(node.get("value"), list):
        node = node["value"]
    if isinstance(node, dict):
        node = [node]
    if not isinstance(node, list) or any(not isinstance(n, dict) for n in node):
        raise DomainError("UPSTREAM_SCHEMA")
    return node


def envelope(data: dict[str, Any], local: str) -> dict[str, Any]:
    for key, node in data.items():
        if key == local or key.endswith("}" + local):
            return obj(node)
    raise DomainError("UPSTREAM_SCHEMA")


def text(node: Any) -> str | None:
    node = value(node)
    if node is None or (isinstance(node, dict) and not node):
        # JAXB nil elements arrive as empty objects; treat them as absent.
        return None
    if not isinstance(node, str):
        raise DomainError("UPSTREAM_SCHEMA")
    return node


def number(node: Any) -> float | None:
    node = value(node)
    if node is None or node == "" or (isinstance(node, dict) and not node):
        return None
    try:
        result = float(node)
    except (ValueError, TypeError):
        raise DomainError("UPSTREAM_SCHEMA") from None
    if isinstance(node, bool) or not math.isfinite(result):
        raise DomainError("UPSTREAM_SCHEMA")
    return result


def integer(node: Any) -> int | None:
    n = number(node)
    if n is None:
        return None
    if n < 0 or not n.is_integer():
        raise DomainError("UPSTREAM_SCHEMA")
    return int(n)


def numeric_id(node: Any, *, required: bool = False) -> str | None:
    node = value(node)
    if (node is None or (isinstance(node, dict) and not node)) and not required:
        return None
    s = str(node)
    if not re.fullmatch(r"[0-9]{1,32}", s):
        raise DomainError("UPSTREAM_SCHEMA")
    return s


def boolean(node: Any) -> bool | None:
    node = value(node)
    if node is None or (isinstance(node, dict) and not node):
        return None
    if isinstance(node, bool):
        return node
    if isinstance(node, str) and node.lower() in {"true", "false"}:
        return node.lower() == "true"
    raise DomainError("UPSTREAM_SCHEMA")


def clean(s: str | None) -> str:
    s = re.sub(r"<br\s*/?>", "\n", s or "", flags=re.I)
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def fold(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


def tokens(s: str) -> set[str]:
    out: set[str] = set()
    for token in re.findall(r"[a-z0-9]+", fold(s)):
        fused = re.fullmatch(r"([a-z]{2,4})(\d{3,5})", token)
        out.update(fused.groups() if fused else [token])
    return out


def has_term(s: str, term: str) -> bool:
    match = re.fullmatch(r"(\d+)\s*([a-z]+)", fold(term).strip())
    forms = [tokens(term)]
    if match:
        n, unit = match.groups()
        forms = [{n + unit}, {n, unit}]
    ts = tokens(s)
    return any(
        form and all(any(t == w or t.endswith(w) for t in ts) for w in form) for form in forms
    )


def image_urls(ad: dict[str, Any]) -> list[str]:
    out = []
    for pic in rows(obj(ad.get("pictures")).get("picture")):
        links = rows(pic.get("link"))
        by_rel = {text(n.get("rel")): text(n.get("href")) for n in links}
        best = next(
            (by_rel.get(rel) for rel in ("large", "XXL", "detail", "teaser") if by_rel.get(rel)),
            next((text(n.get("href")) for n in links if n.get("href")), None),
        )
        if best:
            out.append(best)
    return out


def shipping_ids(ad: dict[str, Any]) -> list[str]:
    result = []
    for option in rows(obj(ad.get("shipping-options")).get("shipping-option")):
        option_id = value(option.get("id"))
        if not isinstance(option_id, (str, int)) or isinstance(option_id, bool) or option_id == "":
            raise DomainError("UPSTREAM_SCHEMA")
        result.append(str(option_id))
    return result


def listing(raw: dict[str, Any], *, account: bool = False) -> m.Listing:
    ad = obj(raw)
    lid = numeric_id(ad.get("id"), required=True)
    title = text(ad.get("title"))
    if title is None:
        raise DomainError("UPSTREAM_SCHEMA")
    price, addr = obj(ad.get("price")), obj(ad.get("ad-address"))
    localized: dict[str, JsonValue] = {}
    machine: dict[str, JsonValue] = {}
    size = rooms_count = None
    for attr in rows(obj(ad.get("attributes")).get("attribute")):
        attr_key = text(attr.get("name"))
        if not attr_key:
            raise DomainError("UPSTREAM_SCHEMA")
        vals = rows(attr.get("value"))
        vs = [value(v.get("value")) for v in vals]
        if any(not isinstance(v, (str, int, float, bool, type(None))) for v in vs):
            raise DomainError("UPSTREAM_SCHEMA")
        # Historical localized keys retain the first machine value.
        localized[text(attr.get("localized-label")) or attr_key] = vs[0] if vs else None
        machine[attr_key] = vs[0] if len(vs) == 1 else vs
        if attr_key.endswith(".qm"):
            size = number(vs[0]) if vs else None
        if attr_key.endswith(".zimmer"):
            rooms_count = number(vs[0]) if vs else None
    seller = m.Seller(
        seller_id=numeric_id(ad.get("user-id")),
        name=text(ad.get("contact-name")) or text(ad.get("store-title")),
        poster_type=text(ad.get("poster-type")),
        account_type=text(ad.get("seller-account-type")),
    )
    top = None
    if ad.get("features-active") is not None:
        top = any(
            text(n.get("name")) == "TOPAD"
            for n in rows(obj(ad["features-active"]).get("feature-active"))
        )
    buy = obj(ad.get("buy-now"))
    return m.Listing(
        listing_id=lid or "",
        title=html.unescape(title),
        description=clean(text(ad.get("description"))),
        description_truncated=False,
        price=number(price.get("amount")),
        currency=text(price.get("currency-iso-code")) or ("EUR" if account else None),
        price_type=text(price.get("price-type")),
        location=m.Location(
            city=text(addr.get("state")),
            postal_code=text(addr.get("zip-code")),
            latitude=number(addr.get("latitude")),
            longitude=number(addr.get("longitude")),
        ),
        url=next(
            (
                text(n.get("href"))
                for n in rows(ad.get("link"))
                if text(n.get("rel")) == "self-public-website"
            ),
            None,
        ),
        created_at=text(ad.get("start-date-time")),
        updated_at=None,
        status=text(ad.get("ad-status")),
        images=image_urls(ad),
        seller=seller if any(v is not None for v in seller.model_dump().values()) else None,
        category_id=numeric_id(obj(ad.get("category")).get("id")),
        attributes=localized,
        attribute_keys=machine,
        size_m2=size,
        rooms=rooms_count,
        ad_type=text(ad.get("ad-type")),
        shipping_options=shipping_ids(ad),
        buy_now=boolean(buy.get("selected")),
        is_top_ad=top,
    )


def ads_block(data: dict[str, Any]) -> tuple[int | None, list[dict[str, Any]]]:
    block = envelope(data, "ads")
    if "ad" not in block and "paging" not in block:
        raise DomainError("UPSTREAM_SCHEMA")
    return integer(obj(block.get("paging")).get("numFound")), rows(block.get("ad"))


def resolve(value_: str) -> str:
    s = value_.strip()
    if re.fullmatch(r"[0-9]{1,32}", s):
        return s
    if s.isdigit():
        raise DomainError(
            "INVALID_ARGUMENT", hint="Numeric category IDs require 1–32 ASCII digits."
        )
    try:
        result = resolve_category(s)
        if result is not None:
            return str(result)
    except ValueError:
        pass
    raise DomainError(
        "INVALID_ARGUMENT",
        hint="Use an exact category name or numeric ID from list_categories.",
        context={"candidate_ids": [str(c.id) for c in find_categories(s, limit=6)]},
    )


@lru_cache(maxsize=1)
def categories() -> tuple[m.Category, ...]:
    tree = json.loads((files("kleinanzeigen_mcp") / "data/categories.json").read_text())
    out: list[m.Category] = []

    def walk(nodes: list[dict[str, Any]], trail: list[str], parent: str | None) -> None:
        for node in nodes:
            path = [*trail, node["title"]]
            cid = str(node["id"])
            out.append(
                m.Category(id=cid, title=node["title"], path=" > ".join(path), parent_id=parent)
            )
            walk(node["childs"], path, cid)

    walk(tree, [], None)
    return tuple(out)


def filter_values(node: Any) -> list[m.FilterValue]:
    result = []
    for n in rows(node):
        machine_value = value(n.get("value"))
        if not isinstance(machine_value, (str, int, float)) or isinstance(machine_value, bool):
            raise DomainError("UPSTREAM_SCHEMA")
        result.append(m.FilterValue(value=str(machine_value), label=text(n.get("localized-label"))))
    return result


def filters(data: dict[str, Any], category_id: str) -> m.FiltersResult:
    block = envelope(data, "ads-search-options")
    if not block:
        raise DomainError("UPSTREAM_SCHEMA")
    globals_ = []
    for key, node in block.items():
        if key == "attributes" or not isinstance(node, dict):
            continue
        node = value(node)
        if not isinstance(node, dict):
            # JAXB leaf markers (e.g. histograms lists) are not parameter objects.
            continue
        globals_.append(
            m.GlobalParameter(
                key=key,
                label=text(node.get("localized-label")),
                type=text(node.get("type")),
                search_param=text(node.get("search-param")),
                values=filter_values(node.get("supported-value")),
            )
        )
    attrs = []
    for attr in rows(obj(block.get("attributes")).get("attribute")):
        attr_key = text(attr.get("name"))
        if not attr_key:
            raise DomainError("UPSTREAM_SCHEMA")
        attrs.append(
            m.AttributeFilter(
                key=attr_key,
                label=text(attr.get("localized-label")),
                type=text(attr.get("type")),
                multi_select=boolean(attr.get("search-multi-select")) or False,
                values=filter_values(attr.get("supported-value")),
            )
        )
    return m.FiltersResult(category_id=category_id, global_parameters=globals_, attributes=attrs)


def locations(data: dict[str, Any]) -> list[m.LocationMatch]:
    out: list[m.LocationMatch] = []

    def walk(nodes: Any) -> None:
        for node in rows(nodes):
            region = rows(obj(node.get("regions")).get("region"))
            lid = numeric_id(node.get("id"), required=True)
            name = text(node.get("localized-name")) or text(node.get("id-name"))
            if not name:
                raise DomainError("UPSTREAM_SCHEMA")
            out.append(
                m.LocationMatch(
                    id=lid or "",
                    name=name,
                    region=text(region[0].get("localized-name")) if region else None,
                    latitude=number(node.get("latitude")),
                    longitude=number(node.get("longitude")),
                    radius_km=number(node.get("radius")),
                )
            )
            walk(node.get("location"))

    walk(envelope(data, "locations").get("location"))
    return out


class Discovery:
    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        clock = runtime.budget.clock
        self.details: Cache[dict[str, Any]] = Cache(256, 60, clock)
        self.schemas: Cache[m.FiltersResult] = Cache(159, 86400, clock)
        self.places: Cache[list[m.LocationMatch]] = Cache(256, 86400, clock)
        self.seller_halves: Cache[dict[str, tuple[float, dict[str, Any]]]] = Cache(256, 900, clock)

    def list_categories(
        self, query: str | None, parent_id: str | None, limit: int, offset: int
    ) -> m.CategoriesResult:
        catalog = categories()
        if parent_id is not None and parent_id not in {c.id for c in catalog}:
            raise DomainError("INVALID_ARGUMENT", hint="Choose a parent ID from list_categories.")
        matches = [
            c
            for c in catalog
            if (parent_id is None or c.parent_id == parent_id)
            and (query is None or fold(query) in fold(c.path))
        ]
        selected = matches[offset : offset + limit]
        return m.CategoriesResult(
            count=len(selected),
            total=len(matches),
            categories=selected,
            next_offset=offset + len(selected) if offset + len(selected) < len(matches) else None,
        )

    def _detail(self, listing_id: str) -> dict[str, Any]:
        cached = self.details.get(listing_id)
        if cached is not None:
            return cached
        data = self.runtime.public()._fetch_ad_json(listing_id)
        if data is None:
            raise DomainError("NOT_FOUND")
        ad = envelope(data, "ad") if "id" not in data else data
        # Validate before caching; malformed success must not turn into an empty listing.
        m.emit(listing(ad).model_dump(mode="json"))
        return self.details.put(listing_id, ad)

    def get_listing(self, listing_id: str) -> m.Listing:
        with self.runtime.operation(tool_name="kleinanzeigen_get_listing"):
            return listing(self._detail(listing_id))

    def _filters(self, category_id: str) -> m.FiltersResult:
        cached = self.schemas.get(category_id)
        if cached is not None:
            return cached
        schema = filters(self.runtime.public().category_filters_raw(category_id), category_id)
        m.emit(schema.model_dump(mode="json"))
        return self.schemas.put(category_id, schema)

    def get_category_filters(self, category: str) -> m.FiltersResult:
        cid = resolve(category)
        with self.runtime.operation(tool_name="kleinanzeigen_get_category_filters"):
            return self._filters(cid)

    def _locations(self, query: str) -> list[m.LocationMatch]:
        cached = self.places.get(query.casefold())
        if cached is not None:
            return cached
        return self.places.put(
            query.casefold(), locations(self.runtime.public().locations_raw(query))
        )

    def search_locations(self, query: str, limit: int) -> m.LocationsResult:
        with self.runtime.operation(tool_name="kleinanzeigen_search_locations"):
            matches = self._locations(query)
            return m.LocationsResult(
                count=min(limit, len(matches)),
                locations=matches[:limit],
                truncated=len(matches) > limit,
            )

    def search(self, **a: Any) -> m.SearchResult:
        cid = resolve(a["category"]) if a["category"] is not None else None
        with self.runtime.operation(tool_name="kleinanzeigen_search") as api:
            resolved = None
            location_id = a["location_id"]
            if a["location"] is not None:
                query = a["location"]
                if re.fullmatch(r"[0-9]{1,32}", query):
                    location_id = query
                else:
                    if query.isdigit():
                        raise DomainError("INVALID_ARGUMENT")
                    matches = self._locations(query)
                    if not matches:
                        raise DomainError(
                            "NOT_FOUND", hint="Use search_locations to choose a location."
                        )
                    exact = [
                        p
                        for p in matches
                        if p.name.split(" - ")[0].split(" (")[0].strip().lower() == query.lower()
                    ]
                    prefix = [
                        p
                        for p in matches
                        if "-" not in p.name and p.name.lower().startswith(query.lower())
                    ]
                    place = (exact or prefix or matches)[0]
                    location_id = place.id
                    resolved = m.ResolvedLocation(id=place.id, label=place.name)
            if a["attributes"]:
                schema = {f.key: f for f in self._filters(cid or "").attributes}
                for key, v in a["attributes"].items():
                    f = schema.get(key)
                    vs = v if isinstance(v, list) else [v]
                    if (
                        f is None
                        or (isinstance(v, list) and not f.multi_select)
                        or any(not s.strip() for s in vs)
                    ):
                        raise DomainError(
                            "INVALID_ARGUMENT",
                            hint="Use keys and multi-select flags from get_category_filters.",
                        )
                    if f.values and any(s not in {fv.value for fv in f.values} for s in vs):
                        raise DomainError(
                            "INVALID_ARGUMENT",
                            hint="Choose supported machine values from get_category_filters.",
                        )
            wire_keys = (
                "query",
                "zip_code",
                "distance_km",
                "min_price",
                "max_price",
                "sort_type",
                "ad_type",
                "poster_type",
                "attributes",
                "picture_required",
                "shippable",
                "buy_now_only",
                "include_top_ads",
                "page_size",
            )
            wire = {k: a[k] for k in wire_keys}
            wire.update(category_id=cid, location_id=location_id)
            result = m.SearchResult(
                page=a["page"],
                page_size=a["page_size"],
                count=0,
                listings=[],
                next_page=a["page"],
                has_more=None,
                pages_fetched=0,
                scanned=0,
                duplicates_removed=0,
                total=None,
                total_relation="unknown",
                scan_cap_reached=False,
                stop_reason="requested_pages",
                resolved_location=resolved,
            )
            seen: set[str] = set()
            for page in range(a["page"], a["page"] + a["pages"]):
                if (
                    result.scanned + a["page_size"] > a["scan_cap"]
                    or (page + 1) * a["page_size"] > 10000
                ):
                    result.scan_cap_reached = True
                    result.stop_reason = "scan_cap"
                    break
                if self.runtime.budget.remaining() <= 0:
                    if not result.pages_fetched:
                        raise DomainError(
                            "UPSTREAM_UNAVAILABLE", hint="Operation deadline reached; retry later."
                        )
                    result.stop_reason = "deadline"
                    result.partial = True
                    result.warnings.append(
                        DomainError("UPSTREAM_UNAVAILABLE", hint="Continue from next_page.").detail
                    )
                    break
                try:
                    total, raw = ads_block(api.search_page_extended(page=page, **wire))
                    if len(raw) > a["page_size"]:
                        raise DomainError("UPSTREAM_SCHEMA")
                    normalized = [listing(ad) for ad in raw]
                except DomainError as exc:
                    if not result.pages_fetched:
                        raise
                    result.partial = True
                    result.warnings.append(exc.detail)
                    result.stop_reason = (
                        "deadline" if self.runtime.budget.remaining() <= 0 else "upstream_error"
                    )
                    break
                except (ValidationError, ValueError, TypeError, RecursionError):
                    if not result.pages_fetched:
                        raise DomainError("UPSTREAM_SCHEMA") from None
                    result.partial = True
                    result.warnings.append(DomainError("UPSTREAM_SCHEMA").detail)
                    result.stop_reason = "upstream_error"
                    break
                candidate = result.model_copy(deep=True)
                page_ids = set()
                for item in normalized:
                    if item.listing_id in seen or item.listing_id in page_ids:
                        candidate.duplicates_removed += 1
                        continue
                    page_ids.add(item.listing_id)
                    full = item.title + "\n" + item.description
                    if a["title_only"] and not tokens(a["query"]).issubset(tokens(item.title)):
                        continue
                    if any(not has_term(full, term) for term in a["require"] or []):
                        continue
                    scope = item.title if a["exclude_scope"] == "title" else full
                    if any(
                        (term.casefold() in scope.casefold())
                        if a["exclude_mode"] == "substring"
                        else has_term(scope, term)
                        for term in a["exclude"] or []
                    ):
                        continue
                    if (
                        a["summary_chars"] is not None
                        and len(item.description) > a["summary_chars"]
                    ):
                        item.description = item.description[: a["summary_chars"]]
                        item.description_truncated = True
                    candidate.listings.append(item)
                candidate.count = len(candidate.listings)
                candidate.scanned += len(raw)
                candidate.pages_fetched += 1
                candidate.total = total
                candidate.total_relation = (
                    "unknown" if total is None else "at_least" if total >= 10000 else "exact"
                )
                exhausted = len(raw) < a["page_size"] or (
                    total is not None and total < 10000 and (page + 1) * a["page_size"] >= total
                )
                candidate.next_page = None if exhausted else page + 1
                candidate.has_more = (
                    False
                    if exhausted
                    else True
                    if total is not None and (page + 1) * a["page_size"] < total
                    else None
                )
                if exhausted:
                    candidate.stop_reason = "exhausted"
                try:
                    # Reserve room for continuation warnings without dropping a page.
                    if (
                        len(
                            json.dumps(
                                candidate.model_dump(mode="json"), ensure_ascii=False
                            ).encode()
                        )
                        > JSON_LIMIT - 4096
                    ):
                        raise DomainError("LIMIT_EXCEEDED")
                    m.emit(candidate.model_dump(mode="json"))
                except DomainError:
                    if not result.pages_fetched:
                        raise DomainError(
                            "LIMIT_EXCEEDED", hint="Use smaller page_size or summary_chars."
                        ) from None
                    result.partial = True
                    result.warnings.append(
                        DomainError(
                            "LIMIT_EXCEEDED",
                            hint="Continue from next_page using the same page_size.",
                        ).detail
                    )
                    break
                result = candidate
                seen.update(page_ids)
                if exhausted:
                    break
            if result.scanned >= a["scan_cap"]:
                result.scan_cap_reached = True
            return result

    def get_ad_images(self, listing_id: str, max_images: int, start: int) -> ToolResult:
        with self.runtime.operation(tool_name="kleinanzeigen_get_ad_images"):
            urls = image_urls(self._detail(listing_id))
            metadata: list[m.ImageMetadata] = []
            images: list[Image] = []
            warnings: list[m.ErrorDetail] = []
            request_slots: list[None] = [None] * 10
            used = 0
            index = start
            for index in range(start, min(len(urls), start + max_images)):
                if not request_slots:
                    break
                try:
                    response = self.runtime.cdn_get(urls[index], request_slots=request_slots)
                    data = response.content
                    mime = (
                        (
                            response.headers.get("Content-Type")
                            or response.headers.get("content-type")
                            or ""
                        )
                        .split(";")[0]
                        .strip()
                        .lower()
                    )
                    if not valid_image(data, mime):
                        raise DomainError(
                            "UPSTREAM_SCHEMA", hint="Image MIME and signature must agree."
                        )
                    if len(data) > IMAGE_LIMIT:
                        raise DomainError("LIMIT_EXCEEDED")
                    if (
                        used + len(data) > IMAGE_TOTAL
                        or (used + len(data)) * 4 / 3 > 20 * 1024 * 1024 - 65536
                    ):
                        # This index is unreturned and can be retried in a fresh image call.
                        break
                    used += len(data)
                    metadata.append(
                        m.ImageMetadata(
                            index=index, url=urls[index], mime_type=mime, bytes=len(data)
                        )
                    )
                    images.append(Image(data=data, format=mime.split("/")[1]))
                except DomainError as exc:
                    warnings.append(exc.detail.model_copy(update={"context": {"index": index}}))
                index += 1
            if not metadata and warnings:
                raise DomainError(
                    warnings[0].code,
                    hint=warnings[0].hint,
                    context={"failed_indices": [w.context["index"] for w in warnings]},
                )
            result = m.ImagesResult(
                listing_id=listing_id,
                count=len(metadata),
                images=metadata,
                total_images=len(urls),
                next_start=index if index < len(urls) else None,
                partial=bool(warnings),
                warnings=warnings,
            )
            # 15 MiB binary => <=20 MiB base64; reserve JSON/framing overhead too.
            if used * 4 / 3 + len(result.model_dump_json().encode()) + 4096 > 20 * 1024 * 1024:
                raise DomainError("LIMIT_EXCEEDED", hint="Request fewer images.")
            return m.emit(result.model_dump(mode="json"), images=images)

    def get_seller_info(self, seller_id: str) -> m.SellerResult:
        with self.runtime.operation(tool_name="kleinanzeigen_get_seller_info"):
            client = self.runtime.seller()
            halves: dict[str, dict[str, Any]] = {}
            warnings = []
            saved = self.seller_halves.get(seller_id) or {}
            for half in ("profile", "reputation"):
                entry = saved.get(half)
                cached = entry[1] if entry and entry[0] > self.runtime.budget.clock() else None
                try:
                    raw = cached if cached is not None else getattr(client, half)(seller_id)
                    validate_half(half, raw)
                    halves[half] = raw
                    if cached is None:
                        saved[half] = (self.runtime.budget.clock() + 900, raw)
                        self.seller_halves.put(seller_id, saved)
                except DomainError as exc:
                    warnings.append(exc.detail.model_copy(update={"context": {"service": half}}))
            if not halves:
                raise DomainError(
                    warnings[0].code,
                    hint=warnings[0].hint,
                    context={str(w.context["service"]): w.code for w in warnings},
                )
            profile, reputation = halves.get("profile", {}), halves.get("reputation", {})
            counters = obj(profile.get("counters"))
            return m.SellerResult(
                seller_id=seller_id,
                name=text(profile.get("contactName")),
                account_type=text(profile.get("posterType")),
                member_since=text(profile.get("userSince")),
                active_ads=integer(counters.get("onlineAds")),
                total_ads=integer(counters.get("historicalAds")),
                followers=integer(counters.get("followers")),
                reply_speed=text(obj(profile.get("replyIndicators")).get("replySpeed")),
                average_rating=number(obj(profile.get("userRatings")).get("averageRating")),
                badges=obj(profile.get("userBadges")).get("badges") or [],
                reputation=m.Reputation(
                    **{key: rating(reputation.get(key)) for key in ("total", "manual", "automatic")}
                ),
                partial=bool(warnings),
                warnings=warnings,
            )


def rating(raw: Any) -> m.Rating | None:
    if raw is None:
        return None
    node = obj(raw)
    return m.Rating(
        average_rating=number(node.get("averageRating")),
        star_rating=node.get("starRating"),
        review_count=integer(node.get("ratingReviewCount")),
    )


def validate_half(half: str, raw: Any) -> None:
    if not isinstance(raw, dict) or not raw:
        raise DomainError("UPSTREAM_SCHEMA")
    try:
        if half == "profile":
            if not any(
                k in raw for k in ("id", "contactName", "userSince", "posterType", "counters")
            ):
                raise DomainError("UPSTREAM_SCHEMA")
            numeric_id(raw.get("id"))
            for k in ("contactName", "posterType", "userSince"):
                text(raw.get(k))
            for counter in ("onlineAds", "historicalAds", "followers"):
                integer(obj(raw.get("counters")).get(counter))
            text(obj(raw.get("replyIndicators")).get("replySpeed"))
            number(obj(raw.get("userRatings")).get("averageRating"))
            if not isinstance(obj(raw.get("userBadges")).get("badges", []), list):
                raise DomainError("UPSTREAM_SCHEMA")
        else:
            if not any(k in raw for k in ("total", "manual", "automatic")):
                raise DomainError("UPSTREAM_SCHEMA")
            for key in ("total", "manual", "automatic"):
                rating(raw.get(key))
        # Arbitrary rating representations and badges still have a hard JSON cap.
        if len(json.dumps(raw, allow_nan=False).encode()) > JSON_LIMIT:
            raise DomainError("LIMIT_EXCEEDED")
    except (ValidationError, ValueError, TypeError):
        raise DomainError("UPSTREAM_SCHEMA") from None


def valid_image(data: bytes, mime: str) -> bool:
    return bool(
        (mime == "image/jpeg" and data.startswith(b"\xff\xd8\xff"))
        or (mime == "image/png" and data.startswith(b"\x89PNG\r\n\x1a\n"))
        or (mime == "image/gif" and data.startswith((b"GIF87a", b"GIF89a")))
        or (mime == "image/webp" and data.startswith(b"RIFF") and data[8:12] == b"WEBP")
    )
