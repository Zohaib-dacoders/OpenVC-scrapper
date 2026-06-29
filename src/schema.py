"""NocoDB table bootstrap for the OpenVC investor scraper.

Two tables: Investors (one row per fund/investor) and InvestorTeam (one row
per team member). Schema is additive — columns are never renamed or dropped;
add a new column here and the next run creates it in NocoDB automatically.

Standalone use:  python -m src.schema
"""

import logging
import os
import sys

from dotenv import load_dotenv

from .nocodb import NocoDB

load_dotenv()
log = logging.getLogger("openvc.schema")

INVESTORS_COLUMNS: list[tuple[str, str]] = [
    ("Slug", "SingleLineText"),           # "Curiosity VC" — URL slug (primary display key)
    ("Name", "SingleLineText"),
    ("InvestorType", "SingleLineText"),   # VC firm, Corporate VC, Solo Angel, etc.
    ("IsVerified", "Checkbox"),
    ("IsLead", "Checkbox"),              # leads rounds (vs. follows only)
    ("AirtableId", "SingleLineText"),     # recXXX from logo img src
    ("LogoUrl", "URL"),
    ("Themes", "LongText"),              # JSON array — ALL themes incl. hidden (detail)
    ("Countries", "LongText"),           # JSON array of all country names (detail page)
    ("CountriesTop", "SingleLineText"),  # "UK, Canada" — top 2 from list page
    ("CountriesOverflow", "Number"),     # +N more countries beyond top 2
    ("FirstCheckText", "SingleLineText"), # "$50k to $1M" raw
    ("FirstCheckMin", "Decimal"),
    ("FirstCheckMax", "Decimal"),
    ("FundingStages", "LongText"),       # JSON array of stage strings (detail page)
    ("StagesTop", "SingleLineText"),     # "3. Early Revenue, 4. Scaling" — top 2
    ("StagesOverflow", "Number"),        # +N more stages beyond top 2
    ("InvestmentThesis", "LongText"),    # truncated from list; team tagline from detail
    ("AboutUs", "LongText"),            # "Who we are" paragraph (detail page)
    ("FundingRequirements", "LongText"), # "Funding Requirements" paragraph (detail)
    ("ValueAdd", "LongText"),           # "Value add" paragraph (detail page)
    ("HQLocation", "SingleLineText"),   # HQ street address (detail page)
    ("PortfolioJson", "LongText"),      # JSON array of {name, url} portfolio companies
    ("ReplyRate", "SingleLineText"),     # "67%"
    ("RespondsIn", "SingleLineText"),    # "2 weeks", "3 days"
    ("ProfileUrl", "URL"),
    ("ListScraped", "Checkbox"),
    ("DetailScraped", "Checkbox"),
    ("RawHtmlPath", "SingleLineText"),   # path to saved HTML file for re-parsing
    ("LastSeen", "DateTime"),
    ("IsActive", "Checkbox"),
]

INVESTOR_TEAM_COLUMNS: list[tuple[str, str]] = [
    ("FundSlug", "SingleLineText"),     # FK → Investors.Slug
    ("AirtableId", "SingleLineText"),   # recXXX person ID
    ("Name", "SingleLineText"),
    ("Role", "SingleLineText"),         # GP/MD, VP, Partner, Analyst, etc.
    ("Tagline", "LongText"),            # "I invest in..."
    ("LinkedInUrl", "URL"),
    ("ProfileSlug", "SingleLineText"),  # "u/elliot-schouten"
]

TABLES: dict[str, list[tuple[str, str]]] = {
    "Investors": INVESTORS_COLUMNS,
    "InvestorTeam": INVESTOR_TEAM_COLUMNS,
}


def ensure_schema(client: NocoDB, base_id: str) -> dict[str, str]:
    existing = {t["title"]: t["id"] for t in client.list_tables(base_id)}
    resolved: dict[str, str] = {}
    for title, columns in TABLES.items():
        if title in existing:
            table_id = existing[title]
            resolved[title] = table_id
            log.info("table %r already exists (%s)", title, table_id)
            have = set(client.list_columns(table_id))
            for name, uidt in columns:
                if name not in have:
                    client.create_column(table_id, name, uidt)
                    log.info("  + added missing column %r (%s)", name, uidt)
        else:
            table_id = client.create_table(base_id, title, columns)
            resolved[title] = table_id
            log.info("created table %r (%s)", title, table_id)
    return resolved


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    base_id = os.getenv("NOCODB_BASE_ID", "")
    if not base_id:
        log.error("NOCODB_BASE_ID must be set")
        sys.exit(2)
    with NocoDB() as client:
        tables = ensure_schema(client, base_id)
    print("Resolved tables:")
    for title, table_id in tables.items():
        print(f"  {title}: {table_id}")


if __name__ == "__main__":
    main()
