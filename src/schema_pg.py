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

    -- About
    description          TEXT,
    value_add            TEXT,
    investment_thesis    TEXT,
    company_stage_focus  TEXT,

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

    -- Stats
    connections    INTEGER,
    popular        BOOLEAN,
    reply_rate     TEXT,
    response_time  TEXT,
    lead_investor  TEXT,

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
    role         TEXT,
    description  TEXT,
    linkedin_url TEXT,
    profile_slug TEXT
);
CREATE INDEX IF NOT EXISTS idx_team_investor_url
    ON investor_team (investor_url);


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
