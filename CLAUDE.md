# CLAUDE.md — zahcomputers-scrapping

This file provides guidance to Claude Code when working in this repository.
It extends the original architecture notes with everything learned in active dev sessions.

---

## NocoDB — live instance

| | |
|---|---|
| URL | `https://anchor.dacoders.com` |
| Base name | zahcomputers |
| Base ID | `pl1dzby064y3coj` |
| Tables | `Products`, `PriceHistory` |
| API token | in `.env` as `NOCODB_API_TOKEN` |

The megacomputer base (`pr1f191l3pjr0d4`) is a **separate** base on the same NocoDB instance —
the two scrapers never share tables.

---

## CRITICAL — Cloudflare bypass: use byparr DIRECT, NOT the Coolify URL

**Root cause of all past "FlareSolverr timeout" failures:**
The Coolify sslip.io reverse-proxy in front of byparr times out on long solve requests (~27 s).
byparr itself works fine. The fix: call it at its **container IP** directly.

```bash
# Find current IP
docker inspect byparr | grep '"IPAddress"'
# Typically 10.0.10.4

# Test
curl -X POST http://10.0.10.4:8191/v1 \
  -H 'Content-Type: application/json' \
  -d '{"cmd":"request.get","url":"https://zahcomputers.pk","maxTimeout":60000}'
```

Set `.env`: `FLARESOLVERR_URL=http://10.0.10.4:8191`  — **not** the sslip.io URL.

---

## Webshare residential proxies — configured but currently dead

10 Webshare proxy IPs in `.env` as `OUTBOUND_PROXY_URLS` (comma-separated).
Credentials: user=`jxcvfgyr`, pass=`5zacv1hkoaxm`.
As of 2026-06-02 all 10 return `ERR_TUNNEL_CONN` (subscription expired).

The scraper falls back to direct (no proxy) automatically when the pool is empty.
Direct + byparr-direct works fine. To re-enable: renew Webshare, update `.env` with fresh IPs,
delete `.cf-cookies.*.json` (each cache file is bound to its solving IP).

---

## Parser bug fixed 2026-06-02 — th/td rows + Elementor fallback

The original `_mine_description_tables` only accepted rows with exactly two `<td>`,
silently dropping all `<th><td>` rows (ASUS boards, PSUs, GPUs, RAM, etc.) and
spec tables inside WoodMart's `.wd-single-content` Elementor widget.
This caused ~18 % of products to have 0 specs.

Fixed by adding to `src/parse.py`:
- `_row_key_value` — handles 2-td, th+td, and 2-th row shapes
- `_spec_table_roots` — falls back to `.wd-single-content` / `.elementor-tab-content` /
  `.wc-tab` when `#tab-description` has no tables
- `&nbsp;` / HTML entity cleanup on keys

The fix is already in `src/parse.py`. A re-scrape will fill previously-missing specs.

---

## Data state (as of 2026-06-02)

| | |
|---|---|
| Total products | ~8,878 |
| With specs | ~7,515 (85 %) |
| Spec-less, spec-relevant (still missing) | ~336 (104 in-stock) |
| Spec-less, accessories (genuinely none) | ~1,100 |

Remaining 336 are mostly local brands (EASE/Boost/SAMA) with no spec table on zahcomputers —
not a parser bug, the source data isn't there.

---

## Spec backfill tools (in `../price-sync/`)

| Tool | What it does |
|---|---|
| `spec_scraper.py` | Standalone scraper — byparr-direct + th/td parser. Fills NocoDB+WC for spec-less spec-relevant products without a full re-scrape. |
| `wayback_spec_backfill.py` | Fetches Wayback Machine archives → fixed parser → NocoDB+WC. Filled 138 products when live scraping was blocked. |
| `icecat_spec_backfill.py` | Open Icecat free tier works for sponsoring brands (ASUS/Dell/Kingston/Gigabyte…). 105 filled. user=`PrithveeRaj`, app_key=`fbe65502-277c-4f33-8ed2-5c80a1d64ccf`. Full account needed for 403 brands. |
| `sync_specs_to_wc.py` | Pushes NocoDB specs to matched WooCommerce products. |
| `fix_variant_specs.py` | Strips spec-bleed (scraper copied one variant's spec sheet onto siblings). Fixed 15 rows. |
| `fix_wrong_descriptions.py` | Detects wrong-model descriptions via title↔description-H1 token diff. Fixed 37 products. |

---

## Integration with price-sync pipeline

```
zahcomputers NocoDB (base pl1dzby064y3coj)
        ↓
../price-sync/zah_matcher.py   → ZahMatches (top-3 candidates per zah product)
        ↓
../price-sync/zah_pricer.py    → ZahPriceActions (pending approval queue)
        ↓  operator approves in NocoDB UI
../price-sync/zah_applier.py   → WooCommerce price update
```

The matcher cross-reads `MegacomputerProducts` from the megacomputer base (`pr1f191l3pjr0d4`).

---

## Commands

```bash
# First-time setup
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                       # then fill in NOCODB_*, FLARESOLVERR_URL

# Create the NocoDB tables (also runs automatically at the start of a scrape)
python -m src.schema

# Run a full scrape (sitemap_index -> product pages -> NocoDB)
python -m src.scraper

# Iterate on parsing without hitting the network or NocoDB
python -c "from src.parse import parse_product; print(parse_product(open('tests/samples/koorui_g2511x.html').read(), 'https://zahcomputers.pk/product/foo/'))"
```

There is **no database to provision** — NocoDB holds the data. The user creates
a *base* in the NocoDB UI (one click) and supplies `NOCODB_BASE_ID`; the
`Products` and `PriceHistory` *tables* are created automatically by
`ensure_schema` (`src/schema.py`). No test suite, linter, or formatter is
configured yet. `parse.py` is pure (no I/O) and is the natural target for tests
— canned sample pages live in `tests/samples/` (gitignored — re-fetch with the
scraper if missing).

## What this project is

A Python scraper that pulls **product pricing from [zahcomputers.pk](https://zahcomputers.pk)**
(a Pakistani electronics retailer) on a recurring schedule and stores it in a
**self-hosted NocoDB** (an Airtable-style no-code database) — the instance lives
at `NOCODB_BASE_URL` (currently `https://anchor.dacoders.com`).

- **Phase 1 — scraper** (built, this repo): sitemap_index → product pages → NocoDB.
- **Phase 2 — price-drop / restock alerts** (deferred): will be rebuilt later, reading from NocoDB.
- **Phase 3 — comparison storefront** (not built): a web frontend over the same data.

This repo is a sibling of `galaxy-scrapper/` (same shape, different site). The
two scrapers each write to **their own NocoDB base** so they stay independent.

## Architecture at a glance

```
GitHub Actions (cron, daily 02:00 UTC)
        │
        ▼
  src/scraper.py  ──asks for cookies──▶  src/flaresolverr.py  ──┐
        │                                    (CloudflareSession)│
        │  ◀── cookies + UA ──────────────────────────────────┘
        │  cached on disk to .cf-cookies.json (~hourly TTL)
        ▼
  sitemap_index.xml → product-sitemap*.xml → ~9.6k product URLs
        │
        ▼  cookied httpx (plain HTML, no headless browser)
  zahcomputers.pk product pages
        │
        ▼   JSON-LD + body class + HTML (gallery, short desc, spec tables)
  src/parse.py  (pure functions)
        │
        ▼
  src/nocodb.py  ──REST (xc-token)──▶  NocoDB
                                       (Products, PriceHistory)
```

**Key design decisions:**

- **Cloudflare bypass via FlareSolverr, cookies replayed on plain httpx.**
  zahcomputers.pk serves a JS challenge to bare httpx (403 "Just a moment..."),
  so a one-time call to FlareSolverr (Dockerised undetected-chromedriver) solves
  the challenge and returns `cf_clearance` + user-agent. The scraper caches
  those to disk and reuses them on every subsequent fetch until they expire
  (~30 min to 2 h). When CF re-challenges mid-scrape, the scraper detects the
  403/503 + "Just a moment" body, re-solves, and retries — with a single-flight
  lock so only one worker re-solves at a time.

- **JSON-LD first, HTML as fallback.** Yoast SEO emits `schema.org/Product`
  with title/sku/price/availability/image; the breadcrumb gives the category.
  Some products only carry the generic `Home > Shop` Yoast breadcrumb; for those
  we fall back to WoodMart's `<nav class=wd-breadcrumbs>` widget to get the real
  category. JSON-LD's `price` is nested under `offers[0].priceSpecification[0]`
  on this site — *not* directly under `offers[0]`.

- **Spec tables live in the description tab — and come in three shapes.**
  zahcomputers rarely uses WooCommerce's `.shop_attributes` table; instead, the
  spec data is a `<table>` inside `#tab-description`. Observed shapes:
  - **Flat 2-col** (e.g. KOORUI monitor): `Brand | KOORUI`, `Model | G2511X`, ...
  - **Notion-style multi-section** (e.g. Lenovo Legion): rows interspersed with
    one-cell "DESIGN", "SOFTWARE" section markers and repeating
    "Specification | Details" header rows.
  - **`.model-information-table` repeating** (e.g. Dell P3223QE):
    DeviceSpecifications.com-style; each cell key has an inline `<p>`
    description we strip; values use `<br>`-separated multi-unit measurements.
  `_mine_description_tables` handles all three by iterating every `<table>`
  inside `#tab-description`, keeping 2-cell rows with non-empty values and
  skipping the known header patterns.

- **Brand fallback chain.** JSON-LD usually doesn't include the brand on
  zahcomputers. We try `attributes["Brand"]` (spec table) → WoodMart
  `product_brand` taxonomy link → JSON-LD `brand` field → `None`. Don't infer
  from the title — too many false positives ("Used HP" → "Used").

- **Image harvesting prefers the gallery widget.** WooCommerce's
  `.woocommerce-product-gallery` links each `<figure>` to the original-size
  file via `<a href>`. That's the authoritative, ordered, clean URL list.
  Inline `<img>` tags in `#tab-description` are a secondary source for older
  products with no gallery widget — we canonicalise by stripping the
  `-<W>x<H>` WordPress size suffix before de-duplicating.

- **NocoDB instead of a SQL database.** The user chose NocoDB — it gives a free
  spreadsheet UI for browsing scraped data and a REST API for writes. There is
  no SQL, no `psycopg`; `src/nocodb.py` is a thin HTTP client. NocoDB has no
  native upsert, so the scraper loads all existing rows once per run and diffs
  in Python.

- **Tables are bootstrapped via the API, the base is not.** `ensure_schema`
  creates the `Products`/`PriceHistory` tables through NocoDB's Meta API if
  they're missing — so the user never hand-builds columns. The *base* is
  created via the NocoDB UI (or a one-off API call) and its UUID supplied via
  `NOCODB_BASE_ID`.

- **PriceHistory is written only on change.** A row is appended only when a
  product's price or stock differs from what NocoDB already holds — not every
  scrape. Daily full inserts would bloat the table (NocoDB slows past ~100k
  rows). `Products.CurrentPrice` always reflects the latest value;
  `PriceHistory` captures the changes.

- **URL slug as the logical key.** Many products do have a `sku` populated
  (unlike galaxy.pk where it was almost always empty), but it's not guaranteed.
  The URL slug is the canonical id; NocoDB's `Id` is the physical key.

## Repository layout

```
zahcomputers-scrapping/
├── CLAUDE.md                 (this file)
├── README.md                 (quickstart + NocoDB table column specs)
├── requirements.txt          (httpx, python-dotenv, tenacity, selectolax)
├── .env.example              (NOCODB_*, FLARESOLVERR_*, scraper tuning knobs)
├── .gitignore                (ignores .env, .cf-cookies.json, tests/samples/)
├── src/
│   ├── __init__.py
│   ├── flaresolverr.py       (CloudflareSession: cookie cache + re-solve)
│   ├── parse.py              (sitemap + JSON-LD + HTML extraction; pure functions)
│   ├── nocodb.py             (NocoDB REST client: Meta API + Data API)
│   ├── schema.py             (table column specs + ensure_schema bootstrap)
│   └── scraper.py            (async fetch + diff + write; python -m src.scraper)
├── tests/
│   ├── __init__.py
│   └── samples/              (gitignored — canned product HTML for parser iteration)
└── .github/workflows/
    └── scrape.yml            (daily cron + workflow_dispatch)
```

## Each piece, in one paragraph

### `src/flaresolverr.py`
`CloudflareSession` is a sync, file-backed handle on a Cloudflare-cleared
identity (cookies + matching UA). `get(target_url)` returns from the JSON cache
if it's fresh; otherwise it POSTs to FlareSolverr's `/v1` endpoint, parses the
solved response, writes the cache, returns. `invalidate()` deletes the cache so
the next `get()` re-solves — call this on 403/503. The cookies are bound to
(value, UA, IP), so re-using them from a different machine fails; clear the
cache when switching hosts.

### `src/parse.py`
Pure functions, no I/O. `parse_sitemap_urls(xml)` extracts `<loc>` entries with
a regex (works on both the index and sub-sitemaps). `is_product_sitemap(url)`
filters the index down to product-sitemap*.xml. `is_likely_product_url(url)`
keeps only URLs under `/product/`. `parse_product(html, url)` finds the JSON-LD
Product, mines the description-tab spec tables (three shapes — see above),
extracts the gallery, walks the brand fallback chain, and returns a flat dict.
Returns `None` if there's no Product JSON-LD (silently drops non-product pages
that slip through the sitemap filter).

### `src/nocodb.py`
A minimal NocoDB REST client (API v2). Auth is the `xc-token` header. Meta API:
`list_tables` / `create_table` / `list_columns` / `create_column` (used by the
schema bootstrap). Data API: `list_all(table_id)` pages through every record;
`bulk_create` / `bulk_update` chunk writes at 100 records/request (`CHUNK`).
Requests retry on 429/5xx via `tenacity` and raise `NocoDBError` on 4xx (bad
token / bad id — fail loud, don't retry).

### `src/schema.py`
Defines the `Products` (20 cols) and `PriceHistory` (6 cols) column specs as
`(name, uidt)` pairs. `ensure_schema(client, base_id)` lists the base's tables,
creates any that are missing, and **reconciles columns** on tables that already
exist — adding any column in the spec that the table lacks (additive only;
never renames/retypes/drops). So adding a column to the spec is enough; the
next run adds it in NocoDB.

### `src/scraper.py`
The orchestrator; entry point `python -m src.scraper`. Phases:
(1) `ensure_schema` + load existing Products (for the diff and the skip window);
(2) prime Cloudflare cookies via `CloudflareSession.get()`, then build an
`httpx.AsyncClient` with those cookies + UA;
(3) walk `sitemap_index.xml` → 10 `product-sitemap*.xml` → ~9.6k product URLs;
(4) async-fetch every page through the shared `Throttle`, decoding to a parsed
dict; on 403/503 with a "Just a moment" body, `CfRefresher` re-solves once
(single-flight via `asyncio.Lock`);
(5) batches of `BATCH_SIZE` (50) are handed to `write_batch` (in a worker
thread, since NocoDB's client is sync) — classifies each as new/updated, writes
PriceHistory only on change, persists immediately so an interrupted run keeps
everything already written;
(6) after the scrape, deactivate (`IsActive=false`) products whose slug is no
longer anywhere in the sitemap. `fetch` retries up to 4×; on 429 it pauses all
workers and slows the base rate. Exit 1 if >20% of scraped URLs failed; exit 2
if `NOCODB_BASE_ID` or `FLARESOLVERR_URL` aren't set.

### `.github/workflows/scrape.yml`
Daily at 02:00 UTC (07:00 PKT) plus a manual `workflow_dispatch` button. Uses
pip caching. The `NOCODB_*` and `FLARESOLVERR_URL` values come from repo
secrets. `concurrency: scrape` with `cancel-in-progress: false` prevents two
runs piling up. 180-minute job timeout (zahcomputers is ~9.6k products vs
galaxy's ~1.8k, so a full scrape can take ~2 h at the polite default of
1.3 req/sec).

## Conventions

- **Python 3.12+.**
- `parse.py` is pure (no I/O) — keep it that way so canned-HTML tests work.
- Async lives only in `scraper.py`'s fetch phase; `nocodb.py` is sync (the write
  phase is not latency-bound).
- Config comes from env vars via the `Config` dataclass — don't hardcode.
- Log via stdlib `logging`. Don't `print`.
- NocoDB column names are `PascalCase` (`CurrentPrice`, `InStock`) because
  that's the NocoDB convention and what the tables use. Match them exactly —
  a typo'd field is silently dropped by the API.

## Things to be careful about

- **The cookie cache is host-bound.** `cf_clearance` is tied to the IP that
  solved it. If you copy `.cf-cookies.json` between machines, scraping will
  start 403'ing immediately. Just `rm .cf-cookies.json` after the move; the
  next run re-solves.

- **FlareSolverr must be reachable from wherever the scraper runs.** Locally
  → easy. In CI → the FlareSolverr instance needs a public URL (we use the
  Coolify sslip.io subdomain). The endpoint has no auth, so its URL is
  semi-secret — keep it in a GitHub Actions secret rather than the workflow
  file.

- **Be polite — don't crank concurrency.** Defaults are deliberately gentle:
  `CONCURRENCY=2`, `REQUEST_DELAY_MS=1500` (~1.3 req/sec). Cloudflare's
  bot-management can re-challenge on bursts even with a valid cookie. The
  `Throttle` slows itself on 429s, but raising concurrency to "speed things
  up" trades fewer total seconds for more re-solve cycles, which is slower
  overall.

- **NocoDB field names must match the table exactly.** NocoDB silently ignores
  unknown keys in a write payload — a renamed/misspelled column means data
  vanishes with no error. If a field stops persisting, check the column name
  first. (There's an alignment sanity check in `scraper._product_fields`.)

- **`list_all` loads the whole Products table each run.** Fine at ~10k rows.
  If the catalog grows much past that, switch to fetching only the fields
  needed for the diff (already limited to Id/Slug/CurrentPrice/InStock/LastSeen).

- **PriceHistory growth.** Even change-only writes accumulate. When the table
  gets large, prune rows older than N months — don't switch back to writing
  every scrape.

- **Description tab "prose" is usually near-empty.** zahcomputers product pages
  are mostly spec tables and an `<h1>` repeat in the description tab. The
  `DescriptionHtml` field is what's left after stripping `<table>` and
  `<header class=section-header>` — expect 0–500 chars on most products. That's
  data, not a bug.

- **Some products have no real category — only "Shop".** When both the WC
  JSON-LD breadcrumb and the WoodMart breadcrumb widget are missing, the only
  source left is Yoast's generic "Home > Shop > Product". The fallback chain
  identifies this and returns `Shop` as the category — that's the truth of
  what's published. Don't fake a category.

- **Don't republish the full catalog as your own store** (Phase 3 risk). A
  comparison/affiliate model, or using the data as market research, is the
  safer path. Re-listing images and descriptions verbatim is copyright
  exposure.

## Phase 2 / Phase 3 (not built)

- **Phase 2 (alerts):** A worker that reads `PriceHistory` from NocoDB, detects
  price drops / restocks, and sends a notification. Rebuild against NocoDB
  when the user asks.
- **Phase 3 (storefront):** A web frontend over the NocoDB data. Likely a
  separate repo. NocoDB's shared grid/gallery views may cover part of this for
  free.

## Memory note

This project is a sibling of galaxy-scrapper — same skeleton, different site,
**separate NocoDB base**. If you change a structural decision in one, decide
whether the other should change too.
