"""Alter existing PostgreSQL investors table to the normalized schema.

What this does (non-destructive — existing rows are kept):
  1. Rename columns: type→investor_type, average→average_check
  2. Fix types:  stages/sectors/countries/featured_lists TEXT → TEXT[]
                 aum/average_check TEXT → NUMERIC
                 connections TEXT → INTEGER
                 popular TEXT → BOOLEAN
  3. Add missing columns: value_add, scrape_date
  4. Create investor_team + investor_portfolio tables
  5. Populate investor_portfolio from existing investments JSON column
  6. Add GIN indexes on array columns
  7. Set generated=FALSE on all rows so the scraper re-fills with fixed data

Run once:
  cd opencv-scrapper && .venv/bin/python alter_pg_schema.py
"""

import json
import logging
import os

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("alter")

PG_PARAMS = {
    "host":     os.getenv("PG_HOST",     "10.0.0.3"),
    "port":     int(os.getenv("PG_PORT", "5432")),
    "dbname":   os.getenv("PG_DATABASE", "openvc"),
    "user":     os.getenv("PG_USER",     "openvc"),
    "password": os.getenv("PG_PASSWORD", ""),
}


def col_exists(cur, table, col):
    cur.execute("""
        SELECT 1 FROM information_schema.columns
        WHERE table_name=%s AND column_name=%s
    """, (table, col))
    return cur.fetchone() is not None


def table_exists(cur, table):
    cur.execute("""
        SELECT 1 FROM information_schema.tables
        WHERE table_name=%s
    """, (table,))
    return cur.fetchone() is not None


def main():
    conn = psycopg2.connect(**PG_PARAMS)
    cur = conn.cursor()

    # ── 1. Rename columns ────────────────────────────────────────────────────
    log.info("step 1: renaming columns...")
    if col_exists(cur, "investors", "type") and not col_exists(cur, "investors", "investor_type"):
        cur.execute('ALTER TABLE investors RENAME COLUMN "type" TO investor_type')
        log.info("  renamed: type → investor_type")

    if col_exists(cur, "investors", "average") and not col_exists(cur, "investors", "average_check"):
        cur.execute("ALTER TABLE investors RENAME COLUMN average TO average_check")
        log.info("  renamed: average → average_check")

    conn.commit()

    # ── 2. Fix column types ──────────────────────────────────────────────────
    log.info("step 2: fixing column types...")

    # TEXT[] arrays — split on ', '
    for col in ("stages", "sectors", "countries_of_investment", "featured_lists"):
        cur.execute(f"SELECT data_type FROM information_schema.columns WHERE table_name='investors' AND column_name='{col}'")
        row = cur.fetchone()
        if row and row[0] == "text":
            cur.execute(f"""
                ALTER TABLE investors
                ALTER COLUMN {col} TYPE TEXT[]
                USING CASE WHEN {col} IS NULL OR trim({col})='' THEN NULL
                           ELSE string_to_array(trim({col}), ', ')
                      END
            """)
            log.info("  converted %s TEXT → TEXT[]", col)
    conn.commit()

    # NUMERIC
    for col in ("aum", "average_check"):
        if col_exists(cur, "investors", col):
            cur.execute(f"SELECT data_type FROM information_schema.columns WHERE table_name='investors' AND column_name='{col}'")
            row = cur.fetchone()
            if row and row[0] == "text":
                cur.execute(f"""
                    ALTER TABLE investors
                    ALTER COLUMN {col} TYPE NUMERIC
                    USING CASE WHEN {col}~'^[0-9.]+$' THEN {col}::NUMERIC ELSE NULL END
                """)
                log.info("  converted %s TEXT → NUMERIC", col)

    # INTEGER
    if col_exists(cur, "investors", "connections"):
        cur.execute("SELECT data_type FROM information_schema.columns WHERE table_name='investors' AND column_name='connections'")
        row = cur.fetchone()
        if row and row[0] == "text":
            cur.execute("""
                ALTER TABLE investors
                ALTER COLUMN connections TYPE INTEGER
                USING CASE WHEN connections~'^[0-9]+$' THEN connections::INTEGER ELSE NULL END
            """)
            log.info("  converted connections TEXT → INTEGER")

    # BOOLEAN
    if col_exists(cur, "investors", "popular"):
        cur.execute("SELECT data_type FROM information_schema.columns WHERE table_name='investors' AND column_name='popular'")
        row = cur.fetchone()
        if row and row[0] == "text":
            cur.execute("""
                ALTER TABLE investors
                ALTER COLUMN popular TYPE BOOLEAN
                USING CASE WHEN lower(popular) IN ('true','1','yes') THEN TRUE
                           WHEN lower(popular) IN ('false','0','no') THEN FALSE
                           ELSE FALSE END
            """)
            log.info("  converted popular TEXT → BOOLEAN")

    conn.commit()

    # ── 3. Add missing columns ────────────────────────────────────────────────
    log.info("step 3: adding missing columns...")
    new_cols = [
        ("value_add",   "TEXT"),
        ("scrape_date", "TIMESTAMPTZ"),
    ]
    for col, coltype in new_cols:
        if not col_exists(cur, "investors", col):
            cur.execute(f"ALTER TABLE investors ADD COLUMN {col} {coltype}")
            log.info("  added column: %s %s", col, coltype)
    conn.commit()

    # ── 3b. Add UNIQUE constraint on url (required for FK references) ────────
    cur.execute("""
        SELECT 1 FROM pg_constraint
        WHERE conrelid='investors'::regclass AND contype='u'
          AND conname='investors_url_key'
    """)
    if not cur.fetchone():
        cur.execute("ALTER TABLE investors ADD CONSTRAINT investors_url_key UNIQUE (url)")
        log.info("  added UNIQUE constraint on investors.url")
    conn.commit()

    # ── 4. Create linked tables ───────────────────────────────────────────────
    log.info("step 4: creating investor_team and investor_portfolio tables...")

    if not table_exists(cur, "investor_team"):
        cur.execute("""
            CREATE TABLE investor_team (
                id           SERIAL PRIMARY KEY,
                investor_url TEXT NOT NULL REFERENCES investors(url) ON DELETE CASCADE,
                airtable_id  TEXT,
                name         TEXT NOT NULL,
                picture      TEXT,
                role         TEXT,
                tagline      TEXT,
                linkedin_url TEXT,
                profile_slug TEXT
            )
        """)
        cur.execute("CREATE INDEX idx_team_investor_url ON investor_team (investor_url)")
        log.info("  created investor_team")

    if not table_exists(cur, "investor_portfolio"):
        cur.execute("""
            CREATE TABLE investor_portfolio (
                id           SERIAL PRIMARY KEY,
                investor_url TEXT NOT NULL REFERENCES investors(url) ON DELETE CASCADE,
                company_name TEXT NOT NULL,
                company_url  TEXT
            )
        """)
        cur.execute("CREATE INDEX idx_portfolio_investor_url ON investor_portfolio (investor_url)")
        log.info("  created investor_portfolio")

    conn.commit()

    # ── 5. Populate investor_portfolio from existing investments JSON ─────────
    log.info("step 5: migrating investments JSON → investor_portfolio...")
    if col_exists(cur, "investors", "investments"):
        cur.execute("SELECT url, investments FROM investors WHERE investments IS NOT NULL AND investments != ''")
        rows = cur.fetchall()
        portfolio_rows = []
        for url, inv_json in rows:
            try:
                companies = json.loads(inv_json)
                for c in companies:
                    name = (c.get("name") or "").strip()
                    if name:
                        portfolio_rows.append({
                            "investor_url": url,
                            "company_name": name,
                            "company_url":  c.get("url", ""),
                        })
            except (json.JSONDecodeError, TypeError):
                continue

        if portfolio_rows:
            # Only insert if investor_portfolio is still empty
            cur.execute("SELECT COUNT(*) FROM investor_portfolio")
            if cur.fetchone()[0] == 0:
                psycopg2.extras.execute_batch(
                    cur,
                    """INSERT INTO investor_portfolio (investor_url, company_name, company_url)
                       VALUES (%(investor_url)s, %(company_name)s, %(company_url)s)""",
                    portfolio_rows,
                )
                log.info("  inserted %d portfolio companies", len(portfolio_rows))
            else:
                log.info("  investor_portfolio already populated, skipping")
        conn.commit()

    # ── 6. GIN indexes on arrays ──────────────────────────────────────────────
    log.info("step 6: creating GIN indexes...")
    gin_indexes = {
        "idx_investors_stages":   "stages",
        "idx_investors_sectors":  "sectors",
        "idx_investors_countries": "countries_of_investment",
    }
    for idx_name, col in gin_indexes.items():
        cur.execute(f"SELECT 1 FROM pg_indexes WHERE indexname='{idx_name}'")
        if not cur.fetchone():
            cur.execute(f"CREATE INDEX {idx_name} ON investors USING GIN ({col})")
            log.info("  created GIN index on %s", col)
    conn.commit()

    # ── 7. Reset generated=FALSE so scraper re-fills with fixed parser ────────
    log.info("step 7: setting generated=FALSE on all rows...")
    cur.execute("UPDATE investors SET generated = FALSE, scrape_date = NULL")
    log.info("  reset %d rows", cur.rowcount)
    conn.commit()

    cur.close()
    conn.close()

    log.info("done — schema updated, all rows ready for re-scrape")
    log.info("next step: FORCE_RESCRAPE=true .venv/bin/python -m src.scraper formate-pg")


if __name__ == "__main__":
    main()
