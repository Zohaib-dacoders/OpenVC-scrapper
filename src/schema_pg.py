"""PostgreSQL schema for OpenVC scraper.

Three normalized tables:
  investors        — one row per fund/investor, multi-value fields as TEXT[]
  investor_team    — one row per team member (linked to investors.url)
  investor_portfolio — one row per portfolio company (linked to investors.url)
"""

DDL = """
CREATE TABLE IF NOT EXISTS investors (
    id               SERIAL PRIMARY KEY,
    url              TEXT UNIQUE NOT NULL,
    full_name        TEXT,
    picture          TEXT,
    investor_type    TEXT,
    investor_subtype TEXT,

    -- Location
    address          TEXT,
    city             TEXT,
    country          TEXT,

    -- Investment size
    currency         TEXT,
    investment_min   NUMERIC,
    investment_max   NUMERIC,
    average_check    NUMERIC,
    aum              NUMERIC,

    -- Multi-value: stored as proper arrays (filterable, indexable)
    stages                   TEXT[],
    sectors                  TEXT[],
    countries_of_investment  TEXT[],
    featured_lists           TEXT[],

    -- About  (column names match the OpenVC page section labels)
    description          TEXT,   -- "Who we are"
    value_add            TEXT,   -- "Value add"
    investment_thesis    TEXT,   -- personal thesis (first team member tagline)
    funding_requirements TEXT,   -- "Funding Requirements" (was: company_stage_focus)

    -- Contact / social
    company      TEXT,
    company_role TEXT,
    company_url  TEXT,
    website      TEXT,
    linkedin     TEXT,
    twitter      TEXT,
    facebook     TEXT,
    crunchbase   TEXT,
    angellist    TEXT,

    -- Location (extra)
    branch_offices TEXT[],   -- non-HQ offices ("Munich, Germany", "Switzerland"…)

    -- Stats
    connections    INTEGER,
    popular        BOOLEAN,
    reply_rate     TEXT,
    response_time  TEXT,
    lead_investor  TEXT,      -- Yes / No (derived)
    lead           TEXT,      -- exact OpenVC value: Always / Sometimes / Never / N/A

    -- Scrape metadata
    generated      BOOLEAN NOT NULL DEFAULT FALSE,
    detail_fetched BOOLEAN NOT NULL DEFAULT FALSE,
    scrape_date    TIMESTAMPTZ
);

-- Additive migration for pre-existing DBs (CREATE TABLE IF NOT EXISTS above is a
-- no-op once the table exists, so a fresh column must be added explicitly).
-- detail_fetched tracks the list->detail->formate handoff: the detail phase sets
-- it TRUE once a fund's /fund/{slug} HTML is cached to disk, and the detail-
-- pending query is `generated=FALSE AND detail_fetched=FALSE`.
ALTER TABLE investors ADD COLUMN IF NOT EXISTS detail_fetched BOOLEAN NOT NULL DEFAULT FALSE;
-- 2026-06-30: clearer tagging + missing fields (idempotent — runs every startup).
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_name='investors' AND column_name='company_stage_focus') THEN
    ALTER TABLE investors RENAME COLUMN company_stage_focus TO funding_requirements;
  END IF;
END $$;
ALTER TABLE investors ADD COLUMN IF NOT EXISTS funding_requirements TEXT;
ALTER TABLE investors ADD COLUMN IF NOT EXISTS branch_offices TEXT[];
ALTER TABLE investors ADD COLUMN IF NOT EXISTS lead TEXT;
-- gap #1: keep the raw full HQ address string, not just the parsed city/country.
ALTER TABLE investors ADD COLUMN IF NOT EXISTS address TEXT;

-- GIN indexes for array containment queries:
--   SELECT * FROM investors WHERE 'USA' = ANY(countries_of_investment);
CREATE INDEX IF NOT EXISTS idx_investors_stages
    ON investors USING GIN (stages);
CREATE INDEX IF NOT EXISTS idx_investors_sectors
    ON investors USING GIN (sectors);
CREATE INDEX IF NOT EXISTS idx_investors_countries
    ON investors USING GIN (countries_of_investment);
CREATE INDEX IF NOT EXISTS idx_investors_inv_min
    ON investors (investment_min);


CREATE TABLE IF NOT EXISTS investor_team (
    id           SERIAL PRIMARY KEY,
    investor_url TEXT NOT NULL REFERENCES investors(url) ON DELETE CASCADE,
    airtable_id  TEXT,
    name         TEXT NOT NULL,
    picture      TEXT,
    role         TEXT,          -- normalised to OpenVC's 11 canonical roles (gap #12)
    role_raw     TEXT,          -- original profile title, preserved
    description  TEXT,
    linkedin_url TEXT,
    profile_slug TEXT
);
CREATE INDEX IF NOT EXISTS idx_team_investor_url
    ON investor_team (investor_url);
-- gap #12: original role title kept alongside the normalised value.
ALTER TABLE investor_team ADD COLUMN IF NOT EXISTS role_raw TEXT;


CREATE TABLE IF NOT EXISTS investor_portfolio (
    id           SERIAL PRIMARY KEY,
    investor_url TEXT NOT NULL REFERENCES investors(url) ON DELETE CASCADE,
    company_name TEXT NOT NULL,
    company_url  TEXT
);
CREATE INDEX IF NOT EXISTS idx_portfolio_investor_url
    ON investor_portfolio (investor_url);
"""


def ensure_schema(conn) -> None:
    """Create all tables + indexes if they don't exist yet."""
    with conn.cursor() as cur:
        cur.execute(DDL)
    conn.commit()
