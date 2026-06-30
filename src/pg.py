"""PostgreSQL client for OpenVC scraper.

Replaces nocodb.py for the formate-pg phase.
Uses psycopg2 with UPSERT on url (ON CONFLICT DO UPDATE).
"""

import logging
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.parse import quote as _urlencode

import psycopg2
import psycopg2.extras

from .schema_pg import ensure_schema

log = logging.getLogger("openvc.pg")

# Investor detail URL is the table's logical key. Keep this derivation byte-for-byte
# identical to scraper.py's `f"{BASE_URL}/fund/{quote(slug, safe='')}"` so stubs
# written by the list phase round-trip to the URLs the detail/formate phases build.
BASE_URL = os.getenv("OPENVC_BASE_URL", "https://www.openvc.app")


def url_from_slug(slug: str) -> str:
    return f"{BASE_URL}/fund/{_urlencode(slug, safe='')}"


def _params() -> dict:
    return {
        "host":     os.getenv("PG_HOST",     ""),
        "port":     int(os.getenv("PG_PORT", "5432")),
        "dbname":   os.getenv("PG_DATABASE", "openvc"),
        "user":     os.getenv("PG_USER",     "openvc"),
        "password": os.getenv("PG_PASSWORD", ""),
    }


@contextmanager
def _conn():
    conn = psycopg2.connect(**_params())
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


class PostgresDB:

    def __init__(self):
        self._params = _params()
        with _conn() as conn:
            ensure_schema(conn)
        log.info("PostgreSQL schema ready")

    # ── Read ──────────────────────────────────────────────────────────────────

    def list_pending(self, limit: int = 0) -> list[dict]:
        """Return investors where generated=FALSE, ordered by id."""
        sql = "SELECT id, url, full_name FROM investors WHERE generated = FALSE ORDER BY id"
        if limit:
            sql += f" LIMIT {limit}"
        with _conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql)
                return [dict(r) for r in cur.fetchall()]

    def count_pending(self) -> int:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM investors WHERE generated = FALSE")
                return cur.fetchone()[0]

    def list_pending_detail(self, limit: int = 0) -> list[dict]:
        """Return investors that still need their detail HTML fetched.

        A row is detail-pending when it has no structured data yet
        (generated=FALSE) AND its /fund/{slug} page hasn't been cached yet
        (detail_fetched=FALSE). The detail phase fetches + caches the HTML and
        flips detail_fetched via mark_detail_fetched(); formate-pg then parses
        the cache and flips generated.
        """
        sql = (
            "SELECT id, url, full_name FROM investors "
            "WHERE generated = FALSE AND detail_fetched = FALSE ORDER BY id"
        )
        if limit:
            sql += f" LIMIT {limit}"
        with _conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql)
                return [dict(r) for r in cur.fetchall()]

    # ── Write ─────────────────────────────────────────────────────────────────

    def upsert_investor_stubs(self, rows: list[dict]) -> int:
        """Insert list-phase stubs (url + full_name) into `investors`.

        Each row needs a "slug" (or "url"); "full_name" (or "name") is optional.
        The url is derived from the slug so it round-trips with the detail/formate
        phases. On INSERT, generated/detail_fetched default to FALSE so the new
        investor flows through detail -> formate. On CONFLICT we DO NOTHING — the
        9,247 already-rich rows (and any stub mid-pipeline) are left completely
        untouched, so re-running list never clobbers detail data or resets flags.
        """
        if not rows:
            return 0

        prepared: list[tuple] = []
        seen: set = set()
        for r in rows:
            slug = r.get("slug") or r.get("Slug")
            url = r.get("url") or (url_from_slug(slug) if slug else None)
            if not url or url in seen:
                continue
            seen.add(url)
            full_name = r.get("full_name") or r.get("name") or r.get("Name")
            prepared.append((url, full_name))

        if not prepared:
            return 0

        with _conn() as conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_batch(
                    cur,
                    """
                    INSERT INTO investors (url, full_name, generated, detail_fetched)
                    VALUES (%s, %s, FALSE, FALSE)
                    ON CONFLICT (url) DO NOTHING
                    """,
                    prepared,
                )
        log.info("upserted %d investor stubs", len(prepared))
        return len(prepared)

    def mark_detail_fetched(self, urls: list[str]) -> int:
        """Flag investors whose detail HTML has been cached to disk."""
        if not urls:
            return 0
        with _conn() as conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_batch(
                    cur,
                    "UPDATE investors SET detail_fetched = TRUE WHERE url = %s",
                    [(u,) for u in urls],
                )
        log.info("marked %d investors detail_fetched", len(urls))
        return len(urls)

    def upsert_investors(self, rows: list[dict]) -> int:
        """Upsert investor rows by url. Skips team/portfolio keys."""
        if not rows:
            return 0

        # Columns we write to the investors table
        _SKIP = {"team", "portfolio", "id"}
        _INVESTOR_COLS = [
            "url", "full_name", "picture", "investor_type", "investor_subtype",
            "address", "city", "country", "currency", "investment_min", "investment_max",
            "average_check", "aum", "stages", "sectors", "countries_of_investment",
            "featured_lists", "description", "value_add", "investment_thesis",
            "funding_requirements", "branch_offices", "company", "company_role", "company_url",
            "website", "linkedin", "twitter", "facebook", "crunchbase", "angellist",
            "connections", "popular", "reply_rate", "response_time", "lead_investor", "lead",
            "generated", "scrape_date",
        ]

        with _conn() as conn:
            with conn.cursor() as cur:
                for row in rows:
                    data = {k: v for k, v in row.items() if k in _INVESTOR_COLS and k not in _SKIP}
                    if "url" not in data:
                        continue
                    cols = list(data.keys())
                    vals = list(data.values())
                    placeholders = ", ".join(["%s"] * len(cols))
                    col_names = ", ".join(cols)
                    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c != "url")
                    sql = f"""
                        INSERT INTO investors ({col_names})
                        VALUES ({placeholders})
                        ON CONFLICT (url) DO UPDATE SET {updates}
                    """
                    cur.execute(sql, vals)
        log.info("upserted %d investor rows", len(rows))
        return len(rows)

    def upsert_team(self, rows: list[dict]) -> int:
        """Insert team members. Deletes old members for the fund first."""
        if not rows:
            return 0
        # Group by investor_url so we can delete old team in one shot
        by_url: dict[str, list] = {}
        for r in rows:
            by_url.setdefault(r["investor_url"], []).append(r)

        with _conn() as conn:
            with conn.cursor() as cur:
                for url, members in by_url.items():
                    cur.execute("DELETE FROM investor_team WHERE investor_url = %s", (url,))
                    # Dedup by name within each fund before insert
                    seen: set = set()
                    deduped = []
                    for m in members:
                        if m["name"] not in seen:
                            seen.add(m["name"])
                            deduped.append(m)
                    psycopg2.extras.execute_batch(
                        cur,
                        """
                        INSERT INTO investor_team
                            (investor_url, airtable_id, name, picture, role, role_raw, description, linkedin_url, profile_slug)
                        VALUES (%(investor_url)s, %(airtable_id)s, %(name)s, %(picture)s,
                                %(role)s, %(role_raw)s, %(description)s, %(linkedin_url)s, %(profile_slug)s)
                        """,
                        deduped,
                    )
        log.info("upserted team for %d funds (%d members)", len(by_url), len(rows))
        return len(rows)

    def upsert_portfolio(self, rows: list[dict]) -> int:
        """Insert portfolio companies. Deletes old entries for the fund first."""
        if not rows:
            return 0
        by_url: dict[str, list] = {}
        for r in rows:
            by_url.setdefault(r["investor_url"], []).append(r)

        with _conn() as conn:
            with conn.cursor() as cur:
                for url, companies in by_url.items():
                    cur.execute("DELETE FROM investor_portfolio WHERE investor_url = %s", (url,))
                    psycopg2.extras.execute_batch(
                        cur,
                        """
                        INSERT INTO investor_portfolio (investor_url, company_name, company_url)
                        VALUES (%(investor_url)s, %(company_name)s, %(company_url)s)
                        """,
                        companies,
                    )
        log.info("upserted portfolio for %d funds (%d companies)", len(by_url), len(rows))
        return len(rows)
