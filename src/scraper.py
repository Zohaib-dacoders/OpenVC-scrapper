"""OpenVC scraper — two phases:

  Phase 1 (list):   byparr → /search?page=N (836 pages, ~16,711 investors)
                    Extracts slug, name, type, countries, check size, stages,
                    thesis, reply rate → writes to NocoDB Investors table.

  Phase 2 / formate: tls-client + 11 static residential proxies (parallel) →
                    /fund/{slug} (direct full page, server-rendered)
                    Works directly on OpenVC_final_formate table:
                      - Reads rows where Generated != 1
                      - Extracts slug from the `url` field
                      - Scrapes detail page
                      - Updates the row with parsed fields + Generated=1

Usage:
  python -m src.scraper formate   # Scrape detail pages → update OpenVC_final_formate
  python -m src.scraper list      # Phase 1: list all pages into Investors table
  python -m src.scraper detail    # Phase 2: detail scrape using Investors table slugs
  python -m src.scraper all-pg    # Postgres-only: list -> detail -> formate, one run
"""

from __future__ import annotations

import logging
import os
import queue
import random
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote as _urlencode, unquote as _urldecode

import tls_client as _tls_client
import httpx
from dotenv import load_dotenv

from .parse import parse_detail_page, parse_detail_team, parse_list_page

# NOTE: NocoDB (`from .nocodb import NocoDB`) and the NocoDB schema bootstrap
# (`from .schema import ensure_schema`) are imported LAZILY inside main()'s
# legacy NocoDB branches only — the Postgres-only path (`*-pg` phases) must
# never import NocoDB. Type hints below that reference `NocoDB` are kept valid
# by `from __future__ import annotations` (PEP 563 — annotations are strings).

load_dotenv()
log = logging.getLogger("openvc.scraper")

# ── Config ────────────────────────────────────────────────────────────────────

TOTAL_PAGES = int(os.getenv("TOTAL_PAGES", "836"))
REQUEST_DELAY_MS = int(os.getenv("REQUEST_DELAY_MS", "2000"))
RESCRAPE_AFTER_HOURS = int(os.getenv("RESCRAPE_AFTER_HOURS", "168"))
FORCE_RESCRAPE = os.getenv("FORCE_RESCRAPE", "false").lower() == "true"
HTML_CACHE_DIR = Path(os.getenv("HTML_CACHE_DIR", "/tmp/openvc-html"))

PROXY_USER = os.getenv("DETAIL_PROXY_USER", "")
PROXY_PASS = os.getenv("DETAIL_PROXY_PASS", "")

NOCODB_BASE_ID = os.getenv("NOCODB_BASE_ID", "")

# OpenVC_final_formate table (base pkrjx6dyqwdne6z, read/write target for `formate` command)
FORMATE_BASE_ID   = os.getenv("FORMATE_BASE_ID",   "pkrjx6dyqwdne6z")
FORMATE_TABLE_ID  = os.getenv("FORMATE_TABLE_ID",  "mqrwxwqkj4oi03t")

# Normalized child tables (same base pkrjx6dyqwdne6z)
TEAM_TABLE_ID      = os.getenv("TEAM_TABLE_ID",      "mgae4vdgegg03ya")
PORTFOLIO_TABLE_ID = os.getenv("PORTFOLIO_TABLE_ID", "m62sqlk5v0d5jao")
# NocoDB-internal FK column that links child rows back to investors (set at insert time)
INVESTORS_FK_COL   = "nc_8cea___OpenCV_final_formate_id"

BASE_URL = "https://www.openvc.app"

HTML_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# ── 11 static residential proxies ────────────────────────────────────────────
# All tested 11/11 success against openvc.app detail pages in ~5s wall-clock.
# tls-client chrome120 fingerprint + residential IP = Cloudflare bot check passes.
# Each worker thread gets its own proxy so IPs never share concurrent load.

_PROXY_IPS = [
    ("82.29.239.32",    5180),   # SG  Dstny
    ("31.98.8.235",     5913),   # FR  Free Pro
    ("82.21.39.153",    7914),   # IT  Telecom Italia
    ("96.62.192.59",    7275),   # IT  Telecom Italia
    ("192.46.189.9",    6002),   # US  RCN
    ("82.21.51.89",     7852),   # IT  Telecom Italia
    ("82.22.181.197",   7908),   # IT  Telecom Italia
    ("45.56.137.172",   9237),   # US  GTT
    ("130.180.232.182", 8620),   # US  RCN
    ("72.46.139.49",    6609),   # US  Comcast
    ("45.56.177.233",   9034),   # US  GTT
]

RESIDENTIAL_PROXIES = [
    f"http://{PROXY_USER}:{PROXY_PASS}@{ip}:{port}"
    for ip, port in _PROXY_IPS
]

# Chrome 120 headers — version must match tls-client chrome120 TLS fingerprint.
# CF validates Sec-CH-UA version against the TLS JA3/JA4 hash; mismatch → JS challenge.
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-CH-UA": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
    "Sec-CH-UA-Mobile": "?0",
    "Sec-CH-UA-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "max-age=0",
    "Connection": "keep-alive",
}


# ── Phase 1: list pages ───────────────────────────────────────────────────────

def _fetch_list_page(flaresolverr_url: str, page_num: int, session_id: str | None = None) -> str | None:
    """Fetch a single list page via byparr. session_id reuses same browser context."""
    url = f"{BASE_URL}/search?page={page_num}"
    payload: dict = {"cmd": "request.get", "url": url, "maxTimeout": 60000}
    if session_id:
        payload["session"] = session_id
    try:
        r = httpx.post(f"{flaresolverr_url}/v1", json=payload, timeout=90)
        data = r.json()
        if data.get("status") == "ok":
            html = data["solution"]["response"]
            if "Just a moment" not in html[:500] and len(html) > 5000:
                return html
            log.warning("list page %d: CF not cleared (len=%d)", page_num, len(html))
        else:
            log.warning("list page %d: byparr error %s", page_num, data.get("message", ""))
    except Exception as exc:
        log.warning("list page %d: %s", page_num, exc)
    return None


def run_list_phase(nocodb: NocoDB, table_ids: dict[str, str]) -> None:
    log.info("=== Phase 1: list pages (1..%d) ===", TOTAL_PAGES)
    existing = {r["Slug"]: r["Id"] for r in nocodb.list_all(table_ids["Investors"], fields=["Slug", "Id"])}
    log.info("existing investors: %d", len(existing))
    flaresolverr_url = os.getenv("FLARESOLVERR_URL", "http://10.0.10.4:8191")
    delay = REQUEST_DELAY_MS / 1000
    new_rows: list[dict] = []
    update_rows: list[dict] = []
    seen_update_ids: set = set()
    now = datetime.now(timezone.utc).isoformat()
    session_id = f"openvc-list-{int(time.time())}"
    log.info("byparr session: %s", session_id)

    for page_num in range(1, TOTAL_PAGES + 1):
        html = _fetch_list_page(flaresolverr_url, page_num, session_id)
        if not html:
            time.sleep(delay)
            continue
        investors = parse_list_page(html)
        for inv in investors:
            inv["LastSeen"] = now
            if inv["Slug"] in existing:
                nid = existing[inv["Slug"]]
                if nid not in seen_update_ids:
                    inv["Id"] = nid
                    update_rows.append(inv)
                    seen_update_ids.add(nid)
            else:
                new_rows.append(inv)

        if page_num % 50 == 0:
            if new_rows:
                nocodb.bulk_create(table_ids["Investors"], new_rows)
                for r in new_rows:
                    existing[r["Slug"]] = "__pending__"
                new_rows = []
            if update_rows:
                nocodb.bulk_update(table_ids["Investors"], update_rows)
                update_rows = []
                seen_update_ids = set()
            log.info("page %d/%d — investors: ~%d", page_num, TOTAL_PAGES, len(existing))

        time.sleep(delay)

    if new_rows:
        nocodb.bulk_create(table_ids["Investors"], new_rows)
    if update_rows:
        nocodb.bulk_update(table_ids["Investors"], update_rows)
    log.info("Phase 1 complete")


def run_list_phase_pg(pg) -> None:
    """Postgres version of run_list_phase.

    Same byparr/CF scraping logic — only the storage call differs: instead of
    NocoDB bulk_create/bulk_update on PascalCase fields, we upsert lightweight
    stubs (url + full_name) into the `investors` table. Existing rows (incl. the
    9,247 already-generated ones) are left untouched on conflict, so re-running
    list never clobbers rich detail data; only brand-new investors are inserted
    with generated=FALSE, ready for the detail phase to pick up.
    """
    log.info("=== Phase 1 (PG): list pages (1..%d) ===", TOTAL_PAGES)
    flaresolverr_url = os.getenv("FLARESOLVERR_URL", "http://10.0.10.4:8191")
    delay = REQUEST_DELAY_MS / 1000
    session_id = f"openvc-list-{int(time.time())}"
    log.info("byparr session: %s", session_id)

    batch: list[dict] = []
    seen_slugs: set = set()
    total_upserted = 0

    for page_num in range(1, TOTAL_PAGES + 1):
        html = _fetch_list_page(flaresolverr_url, page_num, session_id)
        if not html:
            time.sleep(delay)
            continue
        for inv in parse_list_page(html):
            slug = inv.get("Slug")
            if not slug or slug in seen_slugs:
                continue
            seen_slugs.add(slug)
            batch.append({"slug": slug, "full_name": inv.get("Name", "")})

        if page_num % 50 == 0:
            if batch:
                total_upserted += pg.upsert_investor_stubs(batch)
                batch = []
            log.info("page %d/%d — distinct slugs seen: %d", page_num, TOTAL_PAGES, len(seen_slugs))

        time.sleep(delay)

    if batch:
        total_upserted += pg.upsert_investor_stubs(batch)
    log.info("Phase 1 (PG) complete — %d stubs upserted (%d distinct slugs seen)",
             total_upserted, len(seen_slugs))


# ── Detail scraping helpers ───────────────────────────────────────────────────

def _html_cache_path(slug: str) -> Path:
    safe = slug.replace("/", "_").replace(" ", "_")[:80]
    return HTML_CACHE_DIR / f"{safe}.html"


def _new_tls_session() -> _tls_client.Session:
    # random_tls_extension_order varies the ClientHello extension sequence so CF
    # cannot match us by a fixed JA3 full hash (which includes extension order).
    return _tls_client.Session(
        client_identifier="chrome120",
        random_tls_extension_order=True,
    )


def _fetch_detail(slug: str, proxy_url: str, session: _tls_client.Session) -> tuple[str | None, bool, int]:
    """Fetch /fund/{slug}. Returns (html, needs_new_session, status_code).

    needs_new_session=True → CF cleared the session or IP flagged; recreate before next call.
    Returns ("__404__", False, 404) when the investor profile no longer exists.
    status_code=429 → caller must back-off before retrying (rate limited).
    """
    url = f"{BASE_URL}/fund/{_urlencode(slug, safe='')}"
    try:
        r = session.get(url, headers=_BROWSER_HEADERS, proxy=proxy_url, timeout_seconds=25)
        html = r.text
        if r.status_code == 200:
            if "Just a moment" in html[:500] or len(html) < 10000:
                return None, True, 200  # CF JS challenge — recreate session
            return html, False, 200
        if r.status_code == 404:
            return "__404__", False, 404
        log.warning("detail %r: HTTP %d", slug, r.status_code)
        needs_new = r.status_code in (403, 503, 429)
        return None, needs_new, r.status_code
    except Exception as exc:
        log.warning("detail %r: %s", slug, exc)
        return None, False, 0


def _detail_worker(
    proxy_url: str,
    worker_id: int,
    work_q: "queue.Queue[dict | None]",
    results_q: "queue.Queue[tuple]",
    start_delay: float = 0.0,
) -> None:
    """Thread worker: one residential proxy, one tls-client session.

    CF evasion applied:
    - Chrome120 TLS fingerprint (tls-client)
    - Full browser Sec-* headers matching Chrome 120
    - Residential IP (proxy_url) — passes IP reputation check
    - Cookies preserved across requests within the session
    - Staggered start (start_delay) spreads initial burst across workers
    - Random 1.5–3.5s delay between requests (avoids rate-limit detection)
    - 429 back-off: sleep 45–75s when rate-limited, then recreate session
    - Session recreated on CF challenge / IP block (fresh cookies + new handshake)
    """
    if start_delay:
        time.sleep(start_delay)

    session = _new_tls_session()
    consecutive_fail = 0
    label = proxy_url.split("@")[1] if "@" in proxy_url else proxy_url
    log.info("worker %d started: %s", worker_id, label)

    while True:
        row = work_q.get()
        if row is None:  # poison pill — no more work
            work_q.task_done()
            break

        slug = row["_slug"]
        noco_id = row["Id"]
        cache_path = _html_cache_path(slug)

        # Serve from disk cache to skip re-fetch on restarts
        if cache_path.exists():
            try:
                html = cache_path.read_text(encoding="utf-8")
                if len(html) > 10000:
                    results_q.put(("cached", noco_id, slug, html))
                    work_q.task_done()
                    consecutive_fail = 0
                    continue
            except Exception:
                pass

        # Fetch with up to 3 attempts; handle CF challenge, 429 rate-limit, IP block
        html = None
        for attempt in range(3):
            html, needs_new, status = _fetch_detail(slug, proxy_url, session)
            if needs_new:
                session = _new_tls_session()
                log.info("worker %d: session refreshed (status=%d attempt=%d)", worker_id, status, attempt + 1)
            if html:
                break
            if status == 429:
                # Rate limited — back off this worker for 45–75s
                backoff = random.uniform(45, 75)
                log.warning("worker %d: 429 rate-limited on %r — backing off %.0fs", worker_id, slug, backoff)
                time.sleep(backoff)
            else:
                time.sleep(3 * (attempt + 1) + random.uniform(0, 2))

        if html and html != "__404__":
            try:
                cache_path.write_text(html, encoding="utf-8")
            except Exception:
                pass
            results_q.put(("fetched", noco_id, slug, html))
            consecutive_fail = 0
        elif html == "__404__":
            results_q.put(("not_found", noco_id, slug, None))
            consecutive_fail = 0
        else:
            results_q.put(("failed", noco_id, slug, None))
            consecutive_fail += 1
            if consecutive_fail >= 5:
                log.warning("worker %d: 5 consecutive failures — cooling 90s", worker_id)
                time.sleep(90)
                session = _new_tls_session()
                consecutive_fail = 0

        work_q.task_done()
        # 1.5–3.5s per request per IP — human-like cadence, below CF rate-limit threshold
        time.sleep(random.uniform(1.5, 3.5))

    log.info("worker %d done", worker_id)


# ── OpenVC_final_formate → parse → update same table ─────────────────────────

_FORMATE_PREFIX = f"{BASE_URL}/fund/"


def _slug_from_url(url: str) -> str | None:
    """Extract plain slug from https://www.openvc.app/fund/Slug%20Name."""
    if not url or _FORMATE_PREFIX not in url:
        return None
    return _urldecode(url.split("/fund/", 1)[1].strip())


def _map_to_formate_cols(parsed: dict) -> dict:
    """Map parse_detail_page() keys → OpenVC_final_formate column names.

    Only fields we can confidently derive are set; everything else stays
    as-is in the table so we don't blank out existing data.
    """
    update: dict = {"Generated": 1}

    if parsed.get("LogoUrl"):
        update["picture"] = parsed["LogoUrl"]

    # Comma-separated strings (match old table format)
    if parsed.get("FundingStages"):
        update["stages"] = parsed["FundingStages"]

    if parsed.get("Themes"):
        update["sectors"] = parsed["Themes"]

    if parsed.get("Countries"):
        update["countries_of_investment"] = parsed["Countries"]

    if parsed.get("FirstCheckMin") is not None:
        update["investment_min"] = parsed["FirstCheckMin"]
    if parsed.get("FirstCheckMax") is not None:
        update["investment_max"] = parsed["FirstCheckMax"]

    if "IsLead" in parsed:
        update["lead_investor"] = "Yes" if parsed["IsLead"] else "No"

    if parsed.get("ReplyRate"):
        update["reply_rate"] = parsed["ReplyRate"]
    if parsed.get("RespondsIn"):
        update["response_time"] = parsed["RespondsIn"]

    # Description: prefer "Who we are" text
    if parsed.get("AboutUs"):
        update["description"] = parsed["AboutUs"]
    elif parsed.get("FundingRequirements"):
        update["description"] = parsed["FundingRequirements"]

    if parsed.get("InvestmentThesis"):
        update["investment_thesis"] = parsed["InvestmentThesis"]

    # Company-stage focus (separate from description)
    if parsed.get("FundingRequirements") and parsed.get("AboutUs"):
        update["company_stage_focus"] = parsed["FundingRequirements"]

    if parsed.get("ValueAdd"):
        update["value_add"] = parsed["ValueAdd"]

    if parsed.get("PortfolioJson"):
        try:
            import json as _json
            update["investments"] = len(_json.loads(parsed["PortfolioJson"]))
        except (ValueError, TypeError):
            pass

    # City + country from improved location parser
    if parsed.get("HQCity"):
        update["city"] = parsed["HQCity"]
    if parsed.get("HQCountry"):
        update["country"] = parsed["HQCountry"]

    # New fields extracted by updated parse_detail_page
    if parsed.get("LinkedInUrl"):
        update["linkedin"] = parsed["LinkedInUrl"]

    if parsed.get("InvestorSubtype"):
        update["investor_subtype"] = parsed["InvestorSubtype"]

    if parsed.get("FeaturedLists"):
        update["featured_lists"] = parsed["FeaturedLists"]

    return update


def _write_team_and_portfolio(
    nocodb: NocoDB,
    noco_id: int,
    investor_url: str,
    team_members: list[dict],
    portfolio_json: str | None,
) -> None:
    """Write team + portfolio rows to child tables, linked to the investor via FK."""
    import json as _json

    # ── Team ─────────────────────────────────────────────────────────────────
    if team_members:
        # Delete stale rows from a previous run for this investor
        nocodb.delete_where(TEAM_TABLE_ID, INVESTORS_FK_COL, noco_id)
        team_rows = [
            {
                INVESTORS_FK_COL: noco_id,   # FK → investors table (creates the NocoDB link)
                "InvestorUrl": investor_url,
                "Name":        m.get("Name", ""),
                "Picture":     m.get("Picture", ""),
                "Role":        m.get("Role", ""),
                "Tagline":     m.get("Tagline", ""),
                "LinkedInUrl": m.get("LinkedInUrl", ""),
                "ProfileSlug": m.get("ProfileSlug", ""),
                "AirtableId":  m.get("AirtableId", ""),
            }
            for m in team_members if m.get("Name")
        ]
        if team_rows:
            nocodb.create_rows(TEAM_TABLE_ID, team_rows)

    # ── Portfolio ─────────────────────────────────────────────────────────────
    if portfolio_json:
        try:
            companies = _json.loads(portfolio_json) if isinstance(portfolio_json, str) else portfolio_json
        except (ValueError, TypeError):
            companies = []
        if companies:
            nocodb.delete_where(PORTFOLIO_TABLE_ID, INVESTORS_FK_COL, noco_id)
            portfolio_rows = [
                {
                    INVESTORS_FK_COL: noco_id,   # FK → investors table
                    "InvestorUrl": investor_url,
                    "CompanyName": c.get("name", ""),
                    "CompanyUrl":  c.get("url", ""),
                }
                for c in companies if c.get("name")
            ]
            if portfolio_rows:
                nocodb.create_rows(PORTFOLIO_TABLE_ID, portfolio_rows)


def run_formate_phase(nocodb: NocoDB, limit: int = 0) -> None:
    """Scrape detail pages for every row in OpenVC_final_formate where Generated != 1.

    Uses 11 parallel tls-client workers (one per residential proxy).
    Updates the row in-place with parsed fields + sets Generated=1 when done.
    Also writes team members to InvestorTeam and portfolio to InvestorPortfolio,
    linking both back to the investor row via NocoDB Link columns.
    limit: if > 0, process at most this many rows (useful for test runs).
    """
    table_id = FORMATE_TABLE_ID
    log.info("=== Formate phase: scraping OpenVC_final_formate (%s) ===", table_id)

    all_rows = nocodb.list_all(table_id, fields=["Id", "url", "fullName", "Generated"])
    log.info("total rows: %d", len(all_rows))

    # Build lookup so we can get investor_url from noco_id in the results loop
    id_to_url: dict[int, str] = {r["Id"]: r.get("url", "") for r in all_rows}

    pending = []
    skipped = 0
    for row in all_rows:
        # Generated=1 (or True) means already scraped — skip unless FORCE_RESCRAPE
        if row.get("Generated") and not FORCE_RESCRAPE:
            skipped += 1
            continue
        slug = _slug_from_url(row.get("url", ""))
        if not slug:
            log.warning("row %d: cannot parse slug from url=%r", row["Id"], row.get("url"))
            skipped += 1
            continue
        row["_slug"] = slug
        pending.append(row)
        if limit and len(pending) >= limit:
            break

    log.info("pending: %d | already Generated (skipped): %d", len(pending), skipped)
    if not pending:
        log.info("nothing to do")
        return

    # ── Queue + 11 workers ────────────────────────────────────────────────────
    work_q: queue.Queue = queue.Queue()
    for row in pending:
        work_q.put(row)
    for _ in RESIDENTIAL_PROXIES:
        work_q.put(None)  # one poison pill per worker

    results_q: queue.Queue = queue.Queue()
    threads = []
    for i, proxy_url in enumerate(RESIDENTIAL_PROXIES):
        # Stagger starts: 3s apart — spreads the initial burst so CF doesn't see
        # 11 simultaneous connections from different IPs all hitting the same site.
        start_delay = i * 3.0
        t = threading.Thread(
            target=_detail_worker,
            args=(proxy_url, i + 1, work_q, results_q, start_delay),
            daemon=True,
            name=f"formate-worker-{i+1}",
        )
        t.start()
        threads.append(t)

    # ── Drain results → parse → bulk-update NocoDB in batches of 50 ──────────
    updates: list[dict] = []
    done = failed = not_found = cached = 0
    total = len(pending)

    def _flush():
        if updates:
            nocodb.bulk_update(table_id, updates)
            updates.clear()

    while any(t.is_alive() for t in threads) or not results_q.empty():
        try:
            item = results_q.get(timeout=5)
        except queue.Empty:
            continue

        status, noco_id, slug, html = item
        investor_url = id_to_url.get(noco_id, "")

        if status in ("fetched", "cached"):
            parsed = parse_detail_page(html, slug)
            row_update = _map_to_formate_cols(parsed)
            row_update["Id"] = noco_id
            team_members = parse_detail_team(html, slug)
            updates.append(row_update)
            if status == "cached":
                cached += 1
            done += 1
            # Write team + portfolio to linked child tables immediately (not batched)
            try:
                _write_team_and_portfolio(
                    nocodb,
                    noco_id,
                    investor_url,
                    team_members or [],
                    parsed.get("PortfolioJson"),
                )
            except Exception as e:
                log.warning("linked-table write failed for %s: %s", slug, e)
        elif status == "not_found":
            # Profile deleted on OpenVC — mark Generated=1 so we don't retry
            updates.append({"Id": noco_id, "Generated": 1})
            not_found += 1
        else:
            failed += 1

        if len(updates) >= 50:
            _flush()

        processed = done + failed + not_found
        if processed % 100 == 0 and processed > 0:
            log.info(
                "progress %d/%d — scraped=%d cached=%d failed=%d not_found=%d",
                processed, total, done, cached, failed, not_found,
            )

    for t in threads:
        t.join()

    _flush()
    log.info(
        "Formate phase complete — scraped=%d (cached=%d) failed=%d not_found=%d",
        done, cached, failed, not_found,
    )


# ── Phase 2 (Investors table) ─────────────────────────────────────────────────

def run_detail_phase(nocodb: NocoDB, table_ids: dict[str, str]) -> None:
    """Detail phase using the local Investors/InvestorTeam tables."""
    log.info("=== Phase 2: detail pages (Investors table) ===")

    all_investors = nocodb.list_all(
        table_ids["Investors"],
        fields=["Id", "Slug", "DetailScraped", "LastSeen"],
    )

    pending, skipped = [], 0
    for row in all_investors:
        if row.get("DetailScraped") and not FORCE_RESCRAPE:
            last = row.get("LastSeen", "")
            if last:
                try:
                    age_h = (
                        datetime.now(timezone.utc)
                        - datetime.fromisoformat(last.rstrip("Z") + "+00:00")
                    ).total_seconds() / 3600
                    if age_h < RESCRAPE_AFTER_HOURS:
                        skipped += 1
                        continue
                except Exception:
                    pass
        row["_slug"] = row["Slug"]
        pending.append(row)

    log.info("pending: %d | skipped: %d", len(pending), skipped)
    if not pending:
        log.info("nothing to do")
        return

    existing_team: dict[str, str] = {
        r["AirtableId"]: r["Id"]
        for r in nocodb.list_all(table_ids["InvestorTeam"], fields=["AirtableId", "Id"])
        if r.get("AirtableId")
    }

    work_q: queue.Queue = queue.Queue()
    for row in pending:
        work_q.put(row)
    for _ in RESIDENTIAL_PROXIES:
        work_q.put(None)

    results_q: queue.Queue = queue.Queue()
    threads = []
    for i, proxy_url in enumerate(RESIDENTIAL_PROXIES):
        t = threading.Thread(
            target=_detail_worker,
            args=(proxy_url, i + 1, work_q, results_q, i * 3.0),
            daemon=True,
        )
        t.start()
        threads.append(t)

    now = datetime.now(timezone.utc).isoformat()
    inv_updates: list[dict] = []
    team_new: list[dict] = []
    done = failed = not_found = 0
    total = len(pending)

    def _flush():
        if inv_updates:
            nocodb.bulk_update(table_ids["Investors"], inv_updates)
            inv_updates.clear()
        if team_new:
            nocodb.bulk_create(table_ids["InvestorTeam"], team_new)
            team_new.clear()

    while any(t.is_alive() for t in threads) or not results_q.empty():
        try:
            status, noco_id, slug, html = results_q.get(timeout=5)
        except queue.Empty:
            continue

        if status in ("fetched", "cached"):
            parsed = parse_detail_page(html, slug)
            parsed["Id"] = noco_id
            parsed["LastSeen"] = now
            inv_updates.append(parsed)
            for member in parse_detail_team(html, slug):
                aid = member.get("AirtableId", "")
                if aid and aid not in existing_team:
                    team_new.append(member)
                    existing_team[aid] = "pending"
            done += 1
        elif status == "not_found":
            not_found += 1
        else:
            failed += 1

        if len(inv_updates) >= 50 or len(team_new) >= 50:
            _flush()

        processed = done + failed + not_found
        if processed % 100 == 0 and processed > 0:
            log.info("progress %d/%d — done=%d failed=%d", processed, total, done, failed)

    for t in threads:
        t.join()
    _flush()
    log.info("Phase 2 complete: done=%d failed=%d not_found=%d", done, failed, not_found)


def run_detail_phase_pg(pg) -> None:
    """Postgres version of run_detail_phase.

    Reads investors that still need their detail HTML (generated=FALSE AND
    detail_fetched=FALSE), fetches /fund/{slug} through the same 11-proxy
    tls-client worker pool, and lets each worker cache the HTML to disk
    (HTML_CACHE_DIR) — exactly as the NocoDB path does. The ONLY storage change:
    instead of writing parsed fields to NocoDB here, we just flip the
    `detail_fetched` flag for successfully-fetched (or 404) investors. The
    follow-on formate-pg phase consumes the cached HTML and writes the
    structured rows. Keeping detail and formate separate means an interrupted
    detail run resumes cleanly, and `all-pg` chains them in one process so the
    cache written here is read back immediately.
    """
    log.info("=== Phase 2 (PG): detail pages ===")

    pending = pg.list_pending_detail()
    log.info("pending detail: %d", len(pending))
    if not pending:
        log.info("nothing to do")
        return

    work_q: queue.Queue = queue.Queue()
    queued = 0
    for row in pending:
        slug = _slug_from_url(row.get("url", ""))
        if not slug:
            log.warning("row %s: cannot parse slug from url=%r", row.get("id"), row.get("url"))
            continue
        row["_slug"] = slug
        row["Id"] = row["id"]  # workers use row["Id"] as the opaque id
        work_q.put(row)
        queued += 1
    for _ in RESIDENTIAL_PROXIES:
        work_q.put(None)

    results_q: queue.Queue = queue.Queue()
    threads = []
    for i, proxy_url in enumerate(RESIDENTIAL_PROXIES):
        t = threading.Thread(
            target=_detail_worker,
            args=(proxy_url, i + 1, work_q, results_q, i * 3.0),
            daemon=True,
            name=f"pg-detail-worker-{i+1}",
        )
        t.start()
        threads.append(t)

    fetched_urls: list[str] = []
    done = failed = not_found = cached = 0
    total = queued

    def _flush():
        if fetched_urls:
            pg.mark_detail_fetched(fetched_urls)
            fetched_urls.clear()

    while any(t.is_alive() for t in threads) or not results_q.empty():
        try:
            status, _id, slug, html = results_q.get(timeout=5)
        except queue.Empty:
            continue

        url = f"{BASE_URL}/fund/{_urlencode(slug, safe='')}"
        if status in ("fetched", "cached"):
            # HTML already written to HTML_CACHE_DIR by the worker — just mark it.
            fetched_urls.append(url)
            if status == "cached":
                cached += 1
            done += 1
        elif status == "not_found":
            # No cache to consume; mark so detail won't retry. formate-pg will
            # re-hit the 404 once and set generated=TRUE for these.
            fetched_urls.append(url)
            not_found += 1
        else:
            failed += 1  # leave detail_fetched=FALSE so a future run retries

        if len(fetched_urls) >= 50:
            _flush()

        processed = done + failed + not_found
        if processed % 100 == 0 and processed > 0:
            log.info("progress %d/%d — done=%d cached=%d failed=%d not_found=%d",
                     processed, total, done, cached, failed, not_found)

    for t in threads:
        t.join()
    _flush()
    log.info("Phase 2 (PG) complete — fetched=%d (cached=%d) failed=%d not_found=%d",
             done, cached, failed, not_found)


# ── Entry point ───────────────────────────────────────────────────────────────

def run_formate_phase_pg(pg, limit: int = 0) -> None:
    """PostgreSQL version of run_formate_phase.

    Reads pending investors from PostgreSQL (generated=FALSE),
    re-parses from HTML cache, writes to investors + investor_team + investor_portfolio.
    """
    import json as _json
    from datetime import datetime, timezone
    from .pg import PostgresDB

    log.info("=== Formate-PG phase ===")
    pending_rows = pg.list_pending(limit=limit)
    log.info("pending: %d", len(pending_rows))
    if not pending_rows:
        log.info("nothing to do")
        return

    # Reuse the same worker infrastructure — just swap the result handler
    work_q: queue.Queue = queue.Queue()
    for row in pending_rows:
        slug = _slug_from_url(row.get("url", ""))
        if not slug:
            log.warning("row %s: bad url %r", row["id"], row.get("url"))
            continue
        row["_slug"] = slug
        row["Id"] = row["id"]  # workers use row["Id"] as the opaque id
        work_q.put(row)
    for _ in RESIDENTIAL_PROXIES:
        work_q.put(None)

    results_q: queue.Queue = queue.Queue()
    threads = []
    for i, proxy_url in enumerate(RESIDENTIAL_PROXIES):
        t = threading.Thread(
            target=_detail_worker,
            args=(proxy_url, i + 1, work_q, results_q, i * 3.0),
            daemon=True,
            name=f"pg-worker-{i+1}",
        )
        t.start()
        threads.append(t)

    inv_updates: list[dict] = []
    team_rows:   list[dict] = []
    port_rows:   list[dict] = []
    done = failed = not_found = cached = 0
    total = len(pending_rows)
    now = datetime.now(timezone.utc).isoformat()

    def _flush():
        if inv_updates:
            pg.upsert_investors(inv_updates)
            inv_updates.clear()
        if team_rows:
            pg.upsert_team(team_rows)
            team_rows.clear()
        if port_rows:
            pg.upsert_portfolio(port_rows)
            port_rows.clear()

    while any(t.is_alive() for t in threads) or not results_q.empty():
        try:
            item = results_q.get(timeout=5)
        except queue.Empty:
            continue

        status, _id, slug, html = item
        url = f"{BASE_URL}/fund/{_urlencode(slug, safe='')}"

        if status in ("fetched", "cached"):
            parsed   = parse_detail_page(html, slug)
            team_members = parse_detail_team(html, slug)

            # Map to PostgreSQL column names (snake_case, proper types)
            p = parsed
            inv_updates.append({
                "url":            url,
                "picture":        p.get("LogoUrl"),
                "stages":         [s.strip() for s in p["FundingStages"].split(",") if s.strip()] if p.get("FundingStages") else None,
                "sectors":        [s.strip() for s in p["Themes"].split(",") if s.strip()] if p.get("Themes") else None,
                "countries_of_investment": [c.strip() for c in p["Countries"].split(",") if c.strip()] if p.get("Countries") else None,
                "featured_lists": [s.strip() for s in p["FeaturedLists"].split(",") if s.strip()] if p.get("FeaturedLists") else None,
                "investment_min": p.get("FirstCheckMin"),
                "investment_max": p.get("FirstCheckMax"),
                "lead_investor":  "Yes" if p.get("IsLead") else (None if "IsLead" not in p else "No"),
                "reply_rate":     p.get("ReplyRate"),
                "response_time":  p.get("RespondsIn"),
                "description":    p.get("AboutUs") or p.get("FundingRequirements"),
                "value_add":      p.get("ValueAdd"),
                "investment_thesis":   p.get("InvestmentThesis"),
                "company_stage_focus": p.get("FundingRequirements") if p.get("AboutUs") else None,
                "address":        p.get("HQAddress"),
                "city":           p.get("HQCity"),
                "country":        p.get("HQCountry"),
                "linkedin":       p.get("LinkedInUrl"),
                "investor_subtype": p.get("InvestorSubtype"),
                "generated":      True,
                "scrape_date":    now,
            })

            for m in team_members:
                team_rows.append({
                    "investor_url": url,
                    "airtable_id":  m.get("AirtableId", ""),
                    "name":         m.get("Name", ""),
                    "picture":      m.get("Picture", ""),
                    "role":         m.get("Role", ""),
                    "role_raw":     m.get("RoleRaw", ""),
                    "description":  m.get("Tagline", ""),
                    "linkedin_url": m.get("LinkedInUrl", ""),
                    "profile_slug": m.get("ProfileSlug", ""),
                })

            if p.get("PortfolioJson"):
                try:
                    for c in _json.loads(p["PortfolioJson"]):
                        if c.get("name"):
                            port_rows.append({
                                "investor_url": url,
                                "company_name": c["name"],
                                "company_url":  c.get("url", ""),
                            })
                except Exception:
                    pass

            if status == "cached":
                cached += 1
            done += 1

        elif status == "not_found":
            inv_updates.append({"url": url, "generated": True, "scrape_date": now})
            not_found += 1
        else:
            failed += 1

        if len(inv_updates) >= 50:
            _flush()

        processed = done + failed + not_found
        if processed % 100 == 0 and processed > 0:
            log.info("progress %d/%d — done=%d cached=%d failed=%d not_found=%d",
                     processed, total, done, cached, failed, not_found)

    for t in threads:
        t.join()
    _flush()
    log.info("Formate-PG complete — done=%d (cached=%d) failed=%d not_found=%d",
             done, cached, failed, not_found)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    phase = sys.argv[1] if len(sys.argv) > 1 else "formate"
    # Optional limit: --limit N  OR  positional second arg (e.g. formate-teable 100)
    limit = 0
    if "--limit" in sys.argv:
        idx = sys.argv.index("--limit")
        if idx + 1 < len(sys.argv):
            limit = int(sys.argv[idx + 1])
    elif len(sys.argv) > 2:
        try:
            limit = int(sys.argv[2])
        except ValueError:
            pass

    if phase == "formate":
        # Work directly on OpenVC_final_formate (NocoDB) — legacy
        from .nocodb import NocoDB
        with NocoDB() as nocodb:
            run_formate_phase(nocodb, limit=limit)

    elif phase == "formate-pg":
        # PostgreSQL backend
        from .pg import PostgresDB
        pg = PostgresDB()
        run_formate_phase_pg(pg, limit=limit)

    elif phase in ("list-pg", "detail-pg", "all-pg"):
        # Postgres-only path — NEVER imports NocoDB. `all-pg` runs the whole
        # pipeline (list -> detail -> formate) in one process so the HTML cache
        # written by detail is consumed by formate in the same run.
        from .pg import PostgresDB
        pg = PostgresDB()
        if phase in ("list-pg", "all-pg"):
            run_list_phase_pg(pg)
        if phase in ("detail-pg", "all-pg"):
            run_detail_phase_pg(pg)
        if phase == "all-pg":
            run_formate_phase_pg(pg, limit=limit)

    elif phase == "formate-teable":
        # Teable backend (drop-in replacement for formate-pg)
        from .teable_client import TeableDB
        db = TeableDB()
        run_formate_phase_pg(db, limit=limit)

    elif phase in ("list", "detail", "all"):
        from .nocodb import NocoDB
        from .schema import ensure_schema
        if not NOCODB_BASE_ID:
            log.error("NOCODB_BASE_ID must be set in .env for phase: %s", phase)
            sys.exit(2)
        with NocoDB() as nocodb:
            table_ids = ensure_schema(nocodb, NOCODB_BASE_ID)
            log.info("tables: %s", table_ids)
            if phase in ("list", "all"):
                run_list_phase(nocodb, table_ids)
            if phase in ("detail", "all"):
                run_detail_phase(nocodb, table_ids)

    else:
        log.error("unknown phase %r — use: formate | formate-pg | list | detail | all "
                  "| list-pg | detail-pg | all-pg", phase)
        sys.exit(2)

    log.info("done")


if __name__ == "__main__":
    main()
