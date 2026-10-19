"""Opt-in SAFE stdio read smoke. Live execution is a deliberate manual step.

No test invokes main against a live service. Exit 0 requires every registered
SAFE tool to succeed without skips/partial results; 1 means failure, 2 degraded.
Only counts/codes are printed, never upstream text, IDs or personal data.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
from pathlib import Path
from typing import Any

from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from mcp.types import ImageContent

from kleinanzeigen_mcp import models as m

SAFE = frozenset(
    {
        "search",
        "get_listing",
        "get_ad_images",
        "get_seller_info",
        "search_locations",
        "list_categories",
        "get_category_filters",
        "list_my_ads",
        "get_my_ad",
        "list_conversations",
        "watchlist",
        "extend_status",
    }
)
WRITES = frozenset(
    {
        "get_conversation",
        "get_messages",
        "mark_read",
        "contact_seller",
        "reply",
        "create_ad",
        "update_ad",
        "pause_ad",
        "activate_ad",
        "renew_ad",
        "delete_ad",
        "upload_picture",
    }
)
ACCOUNT = frozenset(
    {"list_my_ads", "get_my_ad", "list_conversations", "watchlist", "extend_status"}
)
MODELS = {
    "search": m.SearchResult,
    "get_listing": m.Listing,
    "get_ad_images": m.ImagesResult,
    "get_seller_info": m.SellerResult,
    "search_locations": m.LocationsResult,
    "list_categories": m.CategoriesResult,
    "get_category_filters": m.FiltersResult,
    "list_my_ads": m.ListingPage,
    "get_my_ad": m.Listing,
    "list_conversations": m.ConversationsResult,
    "watchlist": m.ListingPage,
    "extend_status": m.ExtendResult,
    "analyze_listings": m.AnalysisResult,
}


def identifier(s: str) -> str:
    if not re.fullmatch(r"[0-9]{1,32}", s):
        raise argparse.ArgumentTypeError("Expected a numeric ID (1–32 digits).")
    return s


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--public-only", action="store_true")
    p.add_argument("--listing-id", type=identifier)
    p.add_argument("--seller-id", type=identifier)
    p.add_argument("--my-ad-id", type=identifier)
    p.add_argument("--category", default="217")
    p.add_argument("--query", default="Fahrrad")
    p.add_argument("--location-query", default="Berlin")
    return p


async def run(client: Client, args: argparse.Namespace) -> int:
    inventory = await client.list_tools()
    tools = {t.name.removeprefix("kleinanzeigen_"): t for t in inventory}
    expected = SAFE | WRITES
    if (
        len(tools) != len(inventory)
        or any(not t.name.startswith("kleinanzeigen_") for t in inventory)
        or set(tools) not in (expected, expected | {"analyze_listings"})
    ):
        print("FAIL inventory: unreviewed or missing tools")
        return 1
    safe = SAFE | ({"analyze_listings"} if "analyze_listings" in tools else set())
    for name in safe:
        tool = tools[name]
        if not tool.annotations or tool.annotations.read_only_hint is not True:
            print("FAIL inventory: SAFE annotation changed")
            return 1
        if not tool.output_schema or tool.output_schema.get("type") != "object":
            print("FAIL inventory: output schema missing")
            return 1
    called: set[str] = set()
    failures = skips = degraded = 0
    account_available = True

    def skip(name: str, reason: str) -> None:
        nonlocal skips
        called.add(name)
        skips += 1
        print(f"SKIP {name}: {reason}")

    async def call(name: str, **arguments: Any) -> dict[str, Any] | None:
        nonlocal failures, degraded, skips, account_available
        if name not in safe or name in called or not tools[name].annotations.read_only_hint:
            raise RuntimeError("Unreviewed or repeated invocation")
        if name in ACCOUNT and (args.public_only or not account_available):
            skip(name, "public-only" if args.public_only else "no preexisting login")
            return None
        called.add(name)
        result = await client.call_tool("kleinanzeigen_" + name, arguments, raise_on_error=False)
        payload = result.structured_content
        if result.is_error:
            failure = m.Failure.model_validate(payload)
            if name in ACCOUNT and failure.error.code == "AUTH_REQUIRED":
                account_available = False
                skips += 1
                print(f"SKIP {name}: no preexisting login")
                return None
            failures += 1
            print(f"FAIL {name}: {failure.error.code}")
            return None
        model = MODELS[name].model_validate(payload, strict=True)
        data = model.model_dump(mode="json")
        if name == "get_ad_images":
            count = sum(isinstance(block, ImageContent) for block in result.content)
            if count != data["count"]:
                failures += 1
                print(f"FAIL {name}: image content mismatch")
                return None
        codes = [w["code"] for w in data["warnings"]]
        if name == "extend_status" and not data["schema_verified"] and not codes:
            codes = ["UNSUPPORTED"]
        if data["partial"] or codes:
            degraded += 1
            print(f"DEGRADED {name}: {','.join(codes) or 'partial'}")
        else:
            print(f"PASS {name}: count={data.get('count', 1)}")
        return data

    await call("list_categories", limit=1)
    await call("get_category_filters", category=args.category)
    await call("search_locations", query=args.location_query, limit=1)
    search = await call(
        "search", query=args.query, category=args.category, pages=1, page_size=1, summary_chars=400
    )
    hit = next(iter(search["listings"]), None) if search else None
    listing_id = args.listing_id or (hit["listing_id"] if hit else None)
    detail = None
    if listing_id:
        detail = await call("get_listing", listing_id=listing_id)
        await call("get_ad_images", listing_id=listing_id, max_images=1)
    else:
        skip("get_listing", "no public listing fixture")
        skip("get_ad_images", "no public listing fixture")
    seller = (detail or hit or {}).get("seller") or {}
    seller_id = args.seller_id or seller.get("seller_id")
    if seller_id:
        await call("get_seller_info", seller_id=seller_id)
    else:
        skip("get_seller_info", "no seller fixture")
    mine = await call("list_my_ads", page_size=1)
    owned = args.my_ad_id or (
        mine["listings"][0]["listing_id"] if mine and mine["listings"] else None
    )
    if owned and not args.public_only:
        await call("get_my_ad", listing_id=owned)
        await call("extend_status", listing_ids=[owned])
    else:
        skip("get_my_ad", "public-only" if args.public_only else "no owned fixture")
        skip("extend_status", "public-only" if args.public_only else "no owned fixture")
    await call("watchlist", page_size=1)
    await call("list_conversations", page_size=1, pages=1)
    if "analyze_listings" in safe:
        supplied = detail or hit
        if supplied:
            await call(
                "analyze_listings",
                listings=[
                    {
                        "listing_id": supplied["listing_id"],
                        "title": supplied["title"],
                        "description": supplied["description"],
                        "price": supplied["price"],
                    }
                ],
            )
        else:
            skip("analyze_listings", "no listing fixture")
    assert called == safe
    print(f"SAFE result: failures={failures} skips={skips} degraded={degraded}")
    return 1 if failures else 2 if skips or degraded else 0


async def main_async(args: argparse.Namespace) -> int:
    root = Path(__file__).resolve().parent
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    transport = StdioTransport(
        command="/usr/bin/env",
        args=["-u", "PYTHONPATH", str(root / ".venv/bin/python"), str(root / "server.py")],
        env=env,
        cwd=str(root),
        keep_alive=False,
    )
    try:
        async with Client(transport) as client:
            return await run(client, args)
    except Exception as exc:
        # Never print exception messages: protocol/validation failures can carry data.
        print(f"FAIL runner: {type(exc).__name__}")
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async(parser().parse_args())))
