# kleinanzeigen-mcp

**An unofficial, two-way [Kleinanzeigen](https://www.kleinanzeigen.de) MCP server for AI agents.** Search listings, inspect ad photos with vision, message sellers, triage your inbox, and manage your own ads over the Model Context Protocol.

![Python](https://img.shields.io/badge/python-3.13-3776ab?logo=python&logoColor=white)
![MCP](https://img.shields.io/badge/MCP-stdio-6f42c1)
![Built with FastMCP](https://img.shields.io/badge/built%20with-FastMCP-4B8BBE)
![License](https://img.shields.io/badge/license-MIT-green)
![CI](https://github.com/pieonboard/kleinanzeigen-mcp/actions/workflows/ci.yml/badge.svg)
![Status](https://img.shields.io/badge/status-pre--release-orange)

> Pre-release. Search, images, messaging and ad management are implemented, and the read surface has been run against the real API in a read-only smoke test ([record](docs/validation-2026-10-16.md)). Live write verification (first contact, ad update, renewal) is the remaining step, so treat the write tools as experimental. Offline tests, lint, types and the live read smoke are green.

## Contents

- [Why](#why)
- [Features](#features)
- [Tools](#tools)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Verification and tests](#verification-and-tests)
- [Architecture](#architecture)
- [Safety and scope](#safety-and-scope)
- [Documentation](#documentation)
- [Credits](#credits)
- [License](#license)

## Why

Kleinanzeigen is Germany's largest classifieds marketplace and it has no public API for personal use. This server wraps the same private mobile API the Android app uses, through [monkrel/kleinanzeigen-api](https://github.com/monkrel/kleinanzeigen-api), and exposes it to MCP clients. An agent can then take on the tedious parts of buying and selling second-hand goods: finding the right listing, checking who is selling it, reading and answering messages, and keeping ads fresh.

It is built for one user, low volume and writes that stay under human approval. It is not a service, a crawler or an automation farm.

## Features

- Search with the full API filter set (location and radius, category attributes, price range, shipping, ad and poster type, sorting) plus client-side filters the API lacks: `title_only`, `require` and `exclude`. Scans stay bounded and report `next_page`, `scan_cap_reached` and `stop_reason` so you can see how far a search actually went.
- Ad photos returned as MCP image content. Pictures are downloaded within bounds and from an allowlisted host, so a vision-capable agent can judge an item's condition rather than trusting the listing text.
- Two-way seller messaging: first contact on any ad, replies, threads listed unread first, full history, mark read. An outcome that is not certain comes back as a typed partial or unknown error with recovery context instead of a silent retry.
- Management of your own ads: create, update, pause, activate, extend and delete, with picture upload, shipping options and per-ad extension eligibility.
- Seller checks: public profile and reputation summary, degrading to partial results when upstream refuses one half.
- Offline catalogs: a bundled 159-node category tree and category attribute filters parsed from the server's own search schema.
- A transport that paces itself: one serialized request lane with at least 1.5 seconds plus jitter between calls. Reads retry at most twice, a mutation gets exactly one wire attempt and is never replayed, every error carries a stable code and an actionable hint, and logs are structured with secrets redacted.

## Tools

24 tools, all prefixed `kleinanzeigen_`. Read tools carry `readOnlyHint`. Write tools are annotated as destructive or idempotent so a client can gate them under its own approval policy.

### Discovery (public)

| Tool | What it does |
|---|---|
| `kleinanzeigen_search` | Bounded listing search with the full filter set plus `title_only`/`require`/`exclude`, scan caps and continuation |
| `kleinanzeigen_get_listing` | Full detail for one ad: description, location, attributes, images, seller |
| `kleinanzeigen_get_ad_images` | Download ad photos and return them as MCP image content (bounded, CDN-allowlisted) |
| `kleinanzeigen_get_seller_info` | Public seller profile and reputation summary |
| `kleinanzeigen_search_locations` | Location and region lookup for search and radius filters |
| `kleinanzeigen_list_categories` | Browse the bundled category tree (offline, 159 nodes) |
| `kleinanzeigen_get_category_filters` | Category-specific attribute filters read from the server's own schema |

### Inbox and messaging (account)

| Tool | What it does |
|---|---|
| `kleinanzeigen_list_conversations` | Inbox summary, unread first within the pages scanned |
| `kleinanzeigen_get_conversation` | Open a thread once and return compacted history with local pagination |
| `kleinanzeigen_get_messages` | Alias for loading a thread |
| `kleinanzeigen_mark_read` | Mark one or more threads read in a single POST |
| `kleinanzeigen_contact_seller` | First contact on an ad: create the conversation, send your text once, or reuse a known thread. Ambiguous outcomes keep their recovery context |
| `kleinanzeigen_reply` | Send a reply in an existing thread, once |

### Owned ads (account)

| Tool | What it does |
|---|---|
| `kleinanzeigen_list_my_ads` · `kleinanzeigen_get_my_ad` | Your ads, paused ones included |
| `kleinanzeigen_create_ad` | Post one private offer from scalar attributes, with optional shipping IDs and picture URLs |
| `kleinanzeigen_update_ad` | Selective update of title, description, price and pictures through a minimal XML `PUT` |
| `kleinanzeigen_pause_ad` · `kleinanzeigen_activate_ad` | Set the desired state of an ad |
| `kleinanzeigen_renew_ad` | Extension check and extend. Blocked until the eligibility mapping has been confirmed live |
| `kleinanzeigen_delete_ad` | Permanent, destructive delete |
| `kleinanzeigen_upload_picture` | Upload a local image and return the hosted URL for create or update |
| `kleinanzeigen_watchlist` | Your saved ads |
| `kleinanzeigen_extend_status` | Per-ad extension eligibility with status and date |

A 25th tool, `kleinanzeigen_analyze_listings`, applies local heuristics to listings you pass in. It is experimental and stays unregistered unless you set `KA_ENABLE_ANALYSIS=true`.

## Quick start

### 1. Install

```bash
git clone https://github.com/pieonboard/kleinanzeigen-mcp.git
cd kleinanzeigen-mcp
uv venv --python 3.13 .venv
.venv/bin/pip install -e .
```

If pip reports a successful install while imports still resolve elsewhere, your shell is probably exporting a stray `PYTHONPATH`. Run the install as `/usr/bin/env -u PYTHONPATH .venv/bin/pip install -e .` to make it deterministic.

### 2. Log in (one time)

```bash
.venv/bin/kleinanzeigen-api login
```

This completes the OAuth (PKCE) flow in your browser and stores a token at `~/.kleinanzeigen_api/token.json`, mode `0600`. Only the account tools need it; public search works without a login.

### 3. Connect an MCP client

Any client that speaks stdio MCP works. `server.py` is the entry point, and the same server is available as the `kleinanzeigen-mcp` console script.

Claude Desktop, in `claude_desktop_config.json` (`~/Library/Application Support/Claude/` on macOS, `%APPDATA%\Claude\` on Windows):

```json
{
  "mcpServers": {
    "kleinanzeigen": {
      "command": "/path/to/kleinanzeigen-mcp/.venv/bin/python",
      "args": ["/path/to/kleinanzeigen-mcp/server.py"]
    }
  }
}
```

Cursor (`~/.cursor/mcp.json`), Windsurf, Cline and most other clients use the same shape.

Claude Code:

```bash
claude mcp add kleinanzeigen -- /path/to/kleinanzeigen-mcp/.venv/bin/python /path/to/kleinanzeigen-mcp/server.py
```

Codex CLI:

```bash
codex mcp add kleinanzeigen -- /path/to/kleinanzeigen-mcp/.venv/bin/python /path/to/kleinanzeigen-mcp/server.py
```

For loopback HTTP instead of stdio, set `KA_TRANSPORT=streamable-http`. It binds `127.0.0.1:8000` at path `/mcp`; a non-loopback bind is rejected.

## Configuration

All settings are `KA_*` environment variables, validated once at startup.

| Variable | Default | Meaning |
|---|---|---|
| `KA_TRANSPORT` | `stdio` | `stdio` or `streamable-http` (loopback only) |
| `KA_HOST` · `KA_PORT` | `127.0.0.1` · `8000` | HTTP bind address, which must be loopback |
| `KA_TOKEN_PATH` | `~/.kleinanzeigen_api/token.json` | Account token file (`KLEINANZEIGEN_TOKEN_DIR` is honoured as a fallback) |
| `KA_USER_ID` | unset | Own account id. Setting it skips one discovery call |
| `KA_CONTACT_NAME` | unset | Display name used when contacting sellers |
| `KA_REQUEST_INTERVAL` | `1.5` | Minimum seconds between requests, never lower than 1.5 |
| `KA_REQUEST_TIMEOUT` | `25` | Per-request timeout in seconds |
| `KA_OPERATION_TIMEOUT` | `180` | Per-tool operation budget in seconds |
| `KA_LOG_LEVEL` | `INFO` | Log level for the structured, redacted logs on stderr |
| `KA_UPLOAD_ROOTS` | unset | Optional colon-separated directory allowlist for `upload_picture` |
| `KA_ENABLE_ANALYSIS` | `false` | Registers the optional local analysis tool |

## Verification and tests

```bash
# Offline: sockets denied, synthetic fixtures, no real tokens
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy src
.venv/bin/python -m pytest

# Live and read-only: one reviewed invocation per tool, against the real API
.venv/bin/python smoke_test.py
```

336 offline tests pass with sockets denied, and lint, format and strict `mypy` are clean. The smoke runner refuses unknown tools and changed annotations by design, and it is never invoked by the test suite. `uv build` produces the wheel, which bundles the category data and the third-party license notice.

## Architecture

```
src/kleinanzeigen_mcp/
├── server.py       FastMCP factory, error-boundary middleware, stdio and HTTP entry
├── tools.py        24 explicit tool signatures, output schemas, annotations
├── models.py       typed results and the error model (stable codes with hints)
├── discovery.py    search, filters, catalogs, images, seller services
├── messaging.py    inbox normalization, threads, contact and reply workflows
├── ads.py          owned-ad workflows and extension eligibility
├── transport.py    single paced request lane, retries, cooldowns, budgets
├── upstream.py     account client subclass for shipping, upload and update, plus public extensions
└── config.py       validated KA_* settings
```

Four decisions explain most of the behaviour. Full rationale is in [docs/DESIGN.md](docs/DESIGN.md).

- Every upstream call passes through one process-wide lock, paced from call completion at 1.5 seconds plus jitter, because the account API reacts to bursty traffic.
- A read may be attempted three times with backoff and `Retry-After` support. A mutation gets one wire attempt and is never replayed; if delivery is uncertain it returns `WRITE_OUTCOME_UNKNOWN` or `PARTIAL_WRITE` with explicit recovery context.
- Every tool returns a success object or `{"error": {code, message, hint, retryable, ...}}`. Bare exception strings are not part of the contract.
- Public tools work without a login, and an account tool that has no token returns `AUTH_REQUIRED` with a one-line fix.

## Safety and scope

- Unofficial and not affiliated with Kleinanzeigen. Use it within their terms and at personal volume.
- Writes are gated by your MCP client's approval policy. The server never prompts and never approves on its own.
- Seller profile lookups can be refused for whole IP ranges, which is documented upstream anti-fraud behaviour. The tool falls back to the reputation summary instead of retrying or trying to evade the block.
- Run one account-mutating process against an account at a time.

## Documentation

| Document | Contents |
|---|---|
| [`docs/DESIGN.md`](docs/DESIGN.md) | Implementation blueprint: architecture, the 24-tool catalog with contracts, transport and retry policy, error model, test plan |
| [`docs/OPERATIONS.md`](docs/OPERATIONS.md) | Setup, verification recipes, registration and rollback, smoke and manual write checklists |
| [`docs/validation-2026-10-16.md`](docs/validation-2026-10-16.md) | Live read-smoke results, observed API shapes and known conditions |

## Credits

- [monkrel/kleinanzeigen-api](https://github.com/monkrel/kleinanzeigen-api) (MIT) is the unofficial Python client for the mobile API behind every account operation.
- [Samxel/kleinanzeigen-mcp](https://github.com/Samxel/kleinanzeigen-mcp) (MIT) supplied the bundled category tree, redistributed with its license notice in `src/kleinanzeigen_mcp/data/`. Its Android-app API documentation and search matching ideas informed the discovery layer.

## License

MIT. See [LICENSE](LICENSE). Redistributed third-party notices (MIT) stay in effect for the material described under [Credits](#credits).
