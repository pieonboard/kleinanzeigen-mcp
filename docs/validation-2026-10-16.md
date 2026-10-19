# Live validation record: read-only smoke, 2026-10-16

Live runs of this server against the real API. Reads only: no writes, no messages, no
login changes. Executed through `smoke_test.py`, one reviewed invocation per tool, paced
at 1.5 seconds plus jitter. Host: Linux, Python 3.13, pinned dependencies.

## Run 1: 2 failures

```
FAIL get_category_filters: UPSTREAM_SCHEMA
FAIL search: UPSTREAM_SCHEMA
SKIP get_listing / get_ad_images / get_seller_info (no search fixture)
DEGRADED extend_status (no mapping yet)
PASS list_categories, search_locations, list_my_ads, get_my_ad, watchlist, list_conversations
```

Diagnosis, from structural probes with no personal data recorded:

- `search`: live responses carry `store-title: {}`, an empty object where a nil element
  belongs. The strict leaf helpers rejected empty objects and that failed the whole ad
  normalization. Fixed by normalizing empty nodes to "absent" in `text`, `number`,
  `boolean`, `numeric_id` and `rows`.
- `get_category_filters`: the search-options block carries list-valued marker entries
  (`histograms`, `histogramsExpand`) that are not parameter objects. Fixed by skipping
  entries whose unwrapped value is not an object.
- Both fixes have regression tests.

## Run 2, after the parsing fix: 0 failures, 0 skips, 2 degraded

```
PASS get_category_filters, search, get_listing, get_ad_images, ...
DEGRADED get_seller_info: IP_BLOCKED (known upstream behavior, see below)
DEGRADED extend_status (mapping not yet implemented)
```

`get_ad_images` returned real image content over MCP with the expected count, and
`get_listing` parsed a live ad detail cleanly.

## Run 3, after the extend-status mapping: 0 failures, 0 skips, 1 degraded

```
PASS all 11, DEGRADED get_seller_info: IP_BLOCKED
SAFE result: failures=0 skips=0 degraded=1
```

## Extend-status shape, observed live

```
[{adId: int, adEndDate: str, eligibleToExtend: str, adExtendedEndDate: str,
  extendAdStatus: str, extensionPrice: int}]
```

`eligibleToExtend` is a date (`25.11.2026` for the ad probed), not a boolean, despite the
name. The eligibility signal is the `extendAdStatus` enum, observed as
`CANNOT_BE_EXTENDED`.

Mapping: `CANNOT_BE_EXTENDED` sets `eligible=false`, with the status and date carried in
the reason and `schema_verified=true`. Every other status stays `null` with a warning
until it has been observed live, so no positive case is guessed.

`renew_ad` returns a typed error with the date on the negative case. The positive path
stays blocked until an eligible ad has been verified on a live account.

The sanitized shape is captured in `tests/fixtures/extend_status.json`.

## Known live condition: seller profile endpoint

`GET api.kleinanzeigen.de/.../profile.json` answers 403 with the documented
blocked-range marker from this host's address, while every other endpoint works. The
reputation half of the same lookup returned 200 in the same call, so the tool returns a
partial result with an `IP_BLOCKED` warning. No retry and no attempt to evade the block.
Search and ad lookups are unaffected.

## Remaining live items

1. First contact (`create-conversation` plus `reply`), which needs a legitimate and
   deliberately chosen seller interaction.
2. `update_ad` against a disposable owned ad: selective fields, an empty picture list,
   and a paused ad.
3. A positive extend-status case and `renew_ad` on an eligible ad.
4. Photo delivery through a messaging client on a real message.

No marketplace write was attempted anywhere in this record.
