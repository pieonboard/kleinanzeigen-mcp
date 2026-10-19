# Operations notes

This is an offline-tested development build, not a production replacement. It publishes 24
core tools (25 contracts when `KA_ENABLE_ANALYSIS=true`) with explicit signatures,
output schemas and annotations. Services are injected through `create_server`.
The default lazy runtime implements seven discovery tools, four account reads
(list_my_ads, get_my_ad, watchlist and extend_status), and six messaging tools.
Owned-ad create/update/pause/activate/delete/upload use that same runtime. Renewal
performs its eligibility GET but returns an actionable `UNSUPPORTED` error until
The mapping is established by live validation. Optional analysis stays a stub.
There is no production fake mode
or environment variable that bypasses a real workflow. Test fakes live in tests.

## Guarded setup and verification

Python 3.13 is required. Always remove a stray `PYTHONPATH` before
venv setup, probes and checks. Do not rely on removing it after Python starts.

```bash
umask 077
/usr/bin/env -u PYTHONPATH /home/homelab/.local/bin/uv sync --frozen
/usr/bin/env -u PYTHONPATH .venv/bin/ruff check .
/usr/bin/env -u PYTHONPATH .venv/bin/ruff format --check .
/usr/bin/env -u PYTHONPATH .venv/bin/mypy src
/usr/bin/env -u PYTHONPATH .venv/bin/python -m pytest
/usr/bin/env -u PYTHONPATH /home/homelab/.local/bin/uv build
```

The suite grew as capabilities landed: 66 tests for the skeleton and transport, 122
once discovery and account reads were in, 208 with messaging (86 cases) and 331 with
the owned-ad writes (123 cases). Each step passed Ruff lint and format, strict mypy,
the offline lock check and a wheel/sdist build that verified the bundled category
data and license bytes: the wheel carries 18 entries, the sdist 24. No dependency
change was needed after the initial lock, and no marketplace call, live login or real
token was used at any point.

The resolved graph is committed in `uv.lock`. Tests deny TCP sockets and curl
requests; the stdio fake server installs its own child-process network guards.
The wheel contains only the package and distribution metadata; sdist inclusions
are explicit. Reference material, tests, fixtures, token files and caches are
excluded. Both distributions include the unchanged 159-node Samxel category tree
and exact MIT notice. Sdist also includes the SAFE runner. Offline tests compare
all category IDs against the installed monkrel catalog and check source-copy
hashes for the tree/notice; tests do not require `.refs/`.

Equivalent stdio launchers:

```bash
/usr/bin/env -u PYTHONPATH .venv/bin/python server.py
/usr/bin/env -u PYTHONPATH .venv/bin/python -m kleinanzeigen_mcp
/usr/bin/env -u PYTHONPATH .venv/bin/kleinanzeigen-mcp
```

`main()` sets umask 077, validates configuration once, disables banners and writes
redacted structured logs to stderr. Invalid configuration exits 2 with no values
or environment dump. See DESIGN §2 for all `KA_*` settings. No `.env` is loaded.
The optional HTTP run signature is checked locally. HTTP configuration accepts
loopback IP addresses only and maps `streamable-http` to `/mcp`; HTTP operational
validation remains open. No client registration or migration has been performed.

## Runtime and read recipes

`create_server` creates one `RuntimeServices` and `upstream.Runtime(settings)` per
process; tests may inject services or the runtime's clients/session factories.
Wrap every complete logical network operation in
`with runtime.operation(account=True, tool_name="kleinanzeigen_...") as api:`
(or `account=False` for public access). Keep multiple phases inside that context.
It covers account discovery, refresh, request pacing and all mutations under a
reentrant lock, with a ten-second acquisition bound. Client construction and MCP
initialization perform no upstream requests. Close the runtime during teardown;
`create_server` closes injected services that expose `close()`.

Public APIs, auth exchange, uploads, CDN and seller HTTP use the same budget.
Requests wait at least 1.5 seconds plus fresh jitter after the previous completion,
including failed attempts. Reads have at most three attempts. Mutations have one
attempt, with unknown outcomes reported explicitly; cancellation does not prove
a remote write was cancelled. A 429 sets a shared cooldown. Request timeouts are
capped by the remaining operation budget. Redirects are disabled to avoid hidden
unpaced attempts. Images follow redirects explicitly, at most three per image and
ten CDN GETs per invocation (excluding wire retries), on the same paced lane.
Each redirect must remain HTTPS on img.kleinanzeigen.de, port 443, without
credentials. CDN requests carry only an image Accept header, never account headers.

Use scoped `search(query=..., category=..., pages=1, page_size=25,
summary_chars=400)` for triage. Filters inspect returned list text before
compaction; this does not verify text omitted upstream. Use get_listing for full
detail and get_ad_images for actual image blocks. Search returns every survivor
on each accepted complete page; continue with next_page and the **same page_size**.
Totals precede filtering/deduplication; 10,000 is a lower bound. A filtered empty
page does not establish exhaustion. Byte limits preserve complete pages and
continuation, or return LIMIT_EXCEEDED if even one page cannot fit.

Use list_categories offline, then get_category_filters for exact machine keys,
supported values and multi-select flags. Search validates attributes against the
raw nested schema. Location lookup uses only the public locations endpoint;
there is no website fallback, crawl, frontier probing or cache busting. Caches
are bounded: details 256/60 seconds, schemas 159/24 hours, locations 256/24 hours,
seller halves 256 sellers/15 minutes with independent expiries. Seller failures
preserve the usable half; known profile IP blocks return IP_BLOCKED without retry.

Owned ads and watchlist use authenticated, account-scoped GETs and retain paused
status. A full page gives candidate next_page and has_more=null. Extend status
performs its GET but returns null eligibility/reason, schema_verified=false and a
warning until the mapping is verified. These reads do not trigger ad writes.

Authenticator is constructed only for account access. Its explicit token path
follows `KA_TOKEN_PATH`, then `KLEINANZEIGEN_TOKEN_DIR/token.json`, then the library's
home default. Existing storage must be a regular owner-owned file, tightened to
0600 when possible. Corrupt/unusable storage returns `AUTH_STORAGE_ERROR`; public
reads stay independent. Rejected refresh/account 401 latches `AUTH_EXPIRED` until
token replacement or restart. Transient refresh failure permits a later retry
after a short cooldown. Never invoke interactive login in the server.

## Update transport inspection (release blocker)

Re-inspected the newly installed pinned `kleinanzeigen-api==0.4.0` distribution
offline on 2026-10-16: `hasattr(KleinanzeigenAPI, "update_ad")` is **False**.
The distribution has no update implementation to adapt. This agrees with the
historical record: the old server calls a
missing method, while a historical transport test asserts XML PUT to
`/api/users/{uid}/ads/{id}.json` with `Content-Type: application/xml`.

This release reconstructs the **ASSERTED BUT UNVERIFIED** transport on
`ShippingKleinanzeigenAPI.update_ad`. `tests/fixtures/update_request.json` is an
explicit reconstruction, not a live capture or recovered implementation. Exact
request tests establish local construction only: escaped text, `<types:amount>42`,
the monkrel namespaces/price types/picture encoding, and omission of unrelated
fields. No speculative endpoint or fallback is implemented.

Null parameters mean unchanged; `picture_urls=[]` sends an empty pictures element.
Title/description/pictures updates require no existing-ad read. Price-only changes
read the current price type and omit a replacement type, preserving it by the
asserted selective contract. A zero price on an existing FREE ad sends FREE without
an amount. Changing to FIXED/NEGOTIABLE without a price requires a usable existing
amount, obtained with at most one owned-ad GET. Unknown type or missing amount
blocks the write with an actionable error. Only supplied fields appear in the
result's `fields` list; an existing amount needed for a price-type change is sent
inside the price element. Unrelated shipping/attributes are never reconstructed.

A full switch remains blocked on controlled live checks: selective field retention,
empty pictures, paused ads, and preservation of shipping/unknown attributes. If the
asserted PUT is rejected, gather sanitized evidence before another deliberate
attempt. Do not replace an ad by delete/create.

## Owned-ad writes (offline)

Create validates title/description, numeric IDs, Decimal money (two decimal places),
scalar attributes, CDN picture URLs and supplied shipping IDs before posting.
Account email is required before the XML POST; use search_locations to select a
precise city/postcode rather than a whole region. PRIVATE/OFFERED and disabled
buy-now defaults come from monkrel. Acknowledged creation without a nonempty ASCII
numeric ID returns PARTIAL_WRITE; inspect owned ads before creating again.

Pause/activate use the library's account-scoped PUTs. Delete is permanent and uses
the library's DELETE route without `.json`. Upload wraps the existing CurlMime
patch: resolved readable regular file, <=20 MiB, optional upload roots, detected
JPEG/PNG/GIF/WebP and matching supplied MIME, safe filename <=255 characters.
All mutations use the shared paced session and have one wire attempt. Upload
acknowledgment without a usable URL is PARTIAL_WRITE; unknown outcomes must be
inspected before any deliberate repeat. Temporary shipping state and MIME objects
are cleaned up on success and failure.

Renew calls extend_status for the listing but never calls extend_ad while the
eligibility mapping is unknown. There is deliberately no extend_status.json fixture
and no guessed eligibility flag. An eligible ad must first supply sanitized
elegible and ineligible responses; the mapping is then implemented and tested
before expiry extension is verified on a permitted ad. No ranking bump/republish behavior is claimed.

## Text messaging (offline)

list_conversations scans at most four pages/200 rows without opening threads or
marking them read. Unread ordering is a stable partition over those scanned pages;
filtering does not change the raw next_page. Missing unread fields stay null. A
full final page returns has_more=null with a candidate next_page.
The raw inbox adapter preserves M's request while avoiding its dataclass's
false/zero defaults for unavailable unread fields.

get_conversation and get_messages share the same thread-loading implementation.
Each invocation makes one conversation PUT (a write), never a separate messages
call or automatic mark_read. Full text is returned oldest first when dates permit;
otherwise source order is preserved with a warning. Offset/limit and the JSON byte
cap slice locally at message boundaries, with next_offset. Continuation refetches
and reopens the thread; offsets may shift. upstream_history_complete stays null.

contact_seller validates text/name/IDs and reads listing context before mutation.
Known own/removed ads are rejected. A new contact performs exactly one
create-conversation POST with contactName only, then one reply POST with the
validated text in JSON. The sole accepted create-ID shape is **top-level `id`**
(an opaque string or integer normalized to a string). `create_conversation.json`
is a synthetic contract fixture, **not observed API evidence**. Replace it with a sanitized fixture
verified against the live API; no speculative nested or alternate ID lookup is
implemented. Creation may reuse a remote thread or notify the seller: neither
behavior nor actual first-message delivery is established by these mocks.

An acknowledged create with an unusable ID/JSON returns PARTIAL_WRITE with
phase=conversation_created_id_unresolved, create_acknowledged=true and
message_status=not_sent. An ambiguous create returns WRITE_OUTCOME_UNKNOWN with
phase=create_conversation and conversation_created=null. After acknowledged
creation, any reply failure is PARTIAL_WRITE with phase=reply, the known
conversation_id, conversation_created=true, and message_status=not_sent for a
definite rejection/pre-wire failure or unknown for a possibly delivered send.
cause_code retains the underlying typed error. These errors never permit automatic
replay. Inspect the inbox, then recover via reply or deliberate contact_seller reuse
with that ID. Reuse first opens the thread and requires the matching listing ID
and BUYER role, preventing contact with a buyer in the account's SELLER thread.

reply sends once without opening a thread; read context before composing. Its
unknown-outcome error instructs inspection before another send. mark_read removes
duplicate IDs and makes one POST; a void acknowledgment proves no per-ID result.
All mutations use the existing shared operation lock, pacing and one wire attempt.
Messages preserve interior whitespace/German Unicode, trim outer whitespace, and
reject empty/over-5,000-code-point text, invalid UTF-8, NUL and prohibited controls.
There is no outgoing attachment support.

Successful contacts record `(listing_id, normalized_text)` in process memory for
600 seconds. Normalization uses NFC and folds whitespace for this warning only;
the sent text is unchanged. A repeat emits an INVALID_ARGUMENT warning with
context.repeat_contact=true and still sends once. This is neither durable
deduplication nor exactly-once delivery; restart clears the record.

## SAFE stdio smoke (run deliberately)

The runner spawns the guarded venv/root launcher, inventories the exact reviewed
24/25 tools and checks readOnlyHint before invoking any SAFE tool. It refuses
unknown additions or changed SAFE annotations. Calls run once serially, with
search pages/size 1, summary 400 and at most one image. It discovers at most one
listing/seller/owned-ad ID from preceding results or uses supplied IDs. No fixture
crawl, scratch ad, messaging write or automatic write replay.

Live commands, run deliberately:

```bash
/usr/bin/env -u PYTHONPATH .venv/bin/python smoke_test.py --public-only
/usr/bin/env -u PYTHONPATH .venv/bin/python smoke_test.py \
  --listing-id <public-id> --seller-id <public-seller-id> --my-ad-id <owned-id>
```

The hardcoded SAFE set is search, get_listing, get_ad_images, get_seller_info,
search_locations, list_categories, get_category_filters, list_my_ads, get_my_ad,
list_conversations, watchlist and extend_status; optional analysis reuses listing
data. Thread loading aliases are writes and excluded. Missing fixtures, missing
preexisting login and public-only account checks print explicit SKIP. Exit 0
means all SAFE checks passed; 1 means failure; 2 means skipped/degraded. Logs show
only counts/codes. Known profile blocks are degraded, never green.
Thread listing is implemented; eligibility still carries an unverified schema
warning. A fully green live run is not yet possible.

## Later operational acceptance

Complete the live SAFE smoke run, register the server under a second name, and work
through the manual write checklist and the rollback instructions before repointing
an existing integration.
First-contact delivery, thread loaded/unread effects, renewal eligibility,
profile/IP behavior and host image ingestion remain live-verification items.
No live marketplace API, login flow or real token was used for the offline checks. The runner
is tested with injected fake services; its live entry point was not run.
