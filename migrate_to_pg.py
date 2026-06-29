"""One-time migration: NocoDB OpenVC_final_formate → PostgreSQL.

What this does:
  1. Drops old flat investors table (wrong types, missing columns)
  2. Creates new normalized schema (investors + investor_team + investor_portfolio)
  3. Reads all rows from NocoDB
  4. Transforms:
       - comma-separated strings → TEXT[] arrays
       - team JSON → investor_team rows
       - investments JSON → investor_portfolio rows
       - column renames (type → investor_type, average → average_check, etc.)
  5. Bulk-inserts into PostgreSQL
  6. Sets generated=FALSE on all rows so the scraper re-parses fresh HTML
     into the new schema (value_add, fixed countries, team table)

Run once:
  cd opencv-scrapper && .venv/bin/python migrate_to_pg.py
"""

import json
import logging
import os
import sys

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("migrate")

# ── NocoDB config ─────────────────────────────────────────────────────────────
import httpx

NOCODB_URL   = os.environ["NOCODB_BASE_URL"]
NOCODB_TOKEN = os.environ["NOCODB_API_TOKEN"]
FORMATE_TABLE_ID = os.getenv("FORMATE_TABLE_ID", "mqrwxwqkj4oi03t")

# ── PostgreSQL config ─────────────────────────────────────────────────────────
PG_PARAMS = {
    "host":     os.getenv("PG_HOST",     "10.0.0.3"),
    "port":     int(os.getenv("PG_PORT", "5432")),
    "dbname":   os.getenv("PG_DATABASE", "openvc"),
    "user":     os.getenv("PG_USER",     "openvc"),
    "password": os.getenv("PG_PASSWORD", ""),
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _split(val: str | None) -> list[str]:
    """'USA, Canada' → ['USA', 'Canada']. None → []."""
    if not val:
        return []
    return [v.strip() for v in val.split(",") if v.strip()]


def _to_int(val) -> int | None:
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _to_numeric(val) -> float | None:
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _transform_investor(row: dict) -> dict:
    """Map NocoDB row → PostgreSQL investors row."""
    return {
        "url":              row.get("url") or "",
        "full_name":        row.get("fullName"),
        "picture":          row.get("picture"),
        "investor_type":    row.get("type"),
        "investor_subtype": row.get("investor_subtype"),
        "city":             row.get("city"),
        "country":          row.get("country"),
        "currency":         row.get("currency"),
        "investment_min":   _to_numeric(row.get("investment_min")),
        "investment_max":   _to_numeric(row.get("investment_max")),
        "average_check":    _to_numeric(row.get("average")),
        "aum":              _to_numeric(row.get("aum")),
        # Arrays
        "stages":                  _split(row.get("stages")),
        "sectors":                 _split(row.get("sectors")),
        "countries_of_investment": _split(row.get("countries_of_investment")),
        "featured_lists":          _split(row.get("featured_lists")),
        # About
        "description":         row.get("description"),
        "value_add":           row.get("value_add"),
        "investment_thesis":   row.get("investment_thesis"),
        "company_stage_focus": row.get("company_stage_focus"),
        # Contact
        "company":      row.get("company"),
        "company_role": row.get("company_role"),
        "company_url":  row.get("company_url"),
        "website":      row.get("website"),
        "linkedin":     row.get("linkedin"),
        "twitter":      row.get("twitter"),
        "facebook":     row.get("facebook"),
        "crunchbase":   row.get("crunchbase"),
        "angellist":    row.get("angellist"),
        # Stats
        "connections":   _to_int(row.get("connections")),
        "popular":       bool(row.get("popular")),
        "reply_rate":    row.get("reply_rate"),
        "response_time": row.get("response_time"),
        "lead_investor": row.get("lead_investor"),
        # Re-scrape everything with the new parser
        "generated":  False,
        "scrape_date": None,
    }


def _transform_team(row: dict) -> list[dict]:
    """NocoDB team JSON → list of investor_team rows."""
    url  = row.get("url", "")
    raw  = row.get("team")
    if not raw:
        return []
    try:
        members = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    result = []
    for m in members:
        if not m.get("Name"):
            continue
        result.append({
            "investor_url": url,
            "airtable_id":  m.get("AirtableId", ""),
            "name":         m.get("Name", ""),
            "picture":      m.get("Picture", ""),
            "role":         m.get("Role", ""),
            "tagline":      m.get("Tagline", ""),
            "linkedin_url": m.get("LinkedInUrl", ""),
            "profile_slug": m.get("ProfileSlug", ""),
        })
    return result


def _transform_portfolio(row: dict) -> list[dict]:
    """NocoDB investments JSON → list of investor_portfolio rows."""
    url = row.get("url", "")
    raw = row.get("investments")
    if not raw:
        return []
    try:
        companies = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    result = []
    for c in companies:
        name = c.get("name", "").strip()
        if name:
            result.append({
                "investor_url": url,
                "company_name": name,
                "company_url":  c.get("url", ""),
            })
    return result


# ── NocoDB reader ─────────────────────────────────────────────────────────────

def read_all_nocodb() -> list[dict]:
    log.info("reading all rows from NocoDB...")
    headers = {"xc-token": NOCODB_TOKEN}
    rows, offset = [], 0
    while True:
        r = httpx.get(
            f"{NOCODB_URL}/api/v2/tables/{FORMATE_TABLE_ID}/records",
            headers=headers,
            params={"limit": 100, "offset": offset},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        page = data.get("list", [])
        rows.extend(page)
        if not page or data.get("pageInfo", {}).get("isLastPage", True):
            break
        offset += len(page)
        if offset % 1000 == 0:
            log.info("  read %d rows...", offset)
    log.info("total NocoDB rows: %d", len(rows))
    return rows


# ── PostgreSQL writer ─────────────────────────────────────────────────────────

INVESTOR_COLS = [
    "url", "full_name", "picture", "investor_type", "investor_subtype",
    "city", "country", "currency", "investment_min", "investment_max",
    "average_check", "aum", "stages", "sectors", "countries_of_investment",
    "featured_lists", "description", "value_add", "investment_thesis",
    "company_stage_focus", "company", "company_role", "company_url",
    "website", "linkedin", "twitter", "facebook", "crunchbase", "angellist",
    "connections", "popular", "reply_rate", "response_time", "lead_investor",
    "generated", "scrape_date",
]

_PLACEHOLDERS = ", ".join(["%s"] * len(INVESTOR_COLS))
_COL_NAMES    = ", ".join(INVESTOR_COLS)
_UPDATES      = ", ".join(f"{c} = EXCLUDED.{c}" for c in INVESTOR_COLS if c != "url")

INSERT_INVESTOR_SQL = f"""
    INSERT INTO investors ({_COL_NAMES})
    VALUES ({_PLACEHOLDERS})
    ON CONFLICT (url) DO UPDATE SET {_UPDATES}
"""


def write_to_postgres(investors: list[dict], team_rows: list[dict], portfolio_rows: list[dict]) -> None:
    log.info("connecting to PostgreSQL...")
    conn = psycopg2.connect(**PG_PARAMS)
    cur = conn.cursor()

    # Drop + recreate for clean migration
    log.info("dropping old tables...")
    cur.execute("DROP TABLE IF EXISTS investor_portfolio CASCADE")
    cur.execute("DROP TABLE IF EXISTS investor_team CASCADE")
    cur.execute("DROP TABLE IF EXISTS investors CASCADE")
    conn.commit()

    # Create new schema
    log.info("creating new schema...")
    from src.schema_pg import DDL
    cur.execute(DDL)
    conn.commit()

    # Insert investors
    log.info("inserting %d investors...", len(investors))
    batch_size = 500
    for i in range(0, len(investors), batch_size):
        batch = investors[i:i + batch_size]
        rows_as_tuples = [[r[c] for c in INVESTOR_COLS] for r in batch]
        psycopg2.extras.execute_batch(cur, INSERT_INVESTOR_SQL, rows_as_tuples)
        conn.commit()
        log.info("  investors: %d/%d", min(i + batch_size, len(investors)), len(investors))

    # Insert team
    log.info("inserting %d team members...", len(team_rows))
    if team_rows:
        psycopg2.extras.execute_batch(
            cur,
            """INSERT INTO investor_team
               (investor_url, airtable_id, name, picture, role, tagline, linkedin_url, profile_slug)
               VALUES (%(investor_url)s, %(airtable_id)s, %(name)s, %(picture)s,
                       %(role)s, %(tagline)s, %(linkedin_url)s, %(profile_slug)s)""",
            team_rows,
        )
        conn.commit()

    # Insert portfolio
    log.info("inserting %d portfolio companies...", len(portfolio_rows))
    if portfolio_rows:
        psycopg2.extras.execute_batch(
            cur,
            """INSERT INTO investor_portfolio (investor_url, company_name, company_url)
               VALUES (%(investor_url)s, %(company_name)s, %(company_url)s)""",
            portfolio_rows,
        )
        conn.commit()

    cur.close()
    conn.close()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    noco_rows = read_all_nocodb()

    investors, team_rows, portfolio_rows = [], [], []
    skipped = 0
    for row in noco_rows:
        url = row.get("url", "")
        if not url:
            skipped += 1
            continue
        investors.append(_transform_investor(row))
        team_rows.extend(_transform_team(row))
        portfolio_rows.extend(_transform_portfolio(row))

    log.info("transformed: %d investors, %d team members, %d portfolio companies, %d skipped",
             len(investors), len(team_rows), len(portfolio_rows), skipped)

    write_to_postgres(investors, team_rows, portfolio_rows)

    log.info("migration complete — all investors set to generated=FALSE for fresh re-scrape")
    log.info("next step: FORCE_RESCRAPE=true .venv/bin/python -m src.scraper formate-pg")


if __name__ == "__main__":
    main()
