"""Pure parsing functions for OpenVC pages. No I/O — all functions take HTML
strings and return plain dicts or lists.
"""

import json
import logging
import re
from urllib.parse import quote, unquote

from selectolax.parser import HTMLParser, Node

log = logging.getLogger("openvc.parse")

_BASE_URL = "https://www.openvc.app"

# ── List page ────────────────────────────────────────────────────────────────


def parse_list_page(html: str) -> list[dict]:
    """Parse a /search?page=N page; return a list of investor dicts."""
    tree = HTMLParser(html)
    rows = tree.css("tbody tr")
    results = []
    for row in rows:
        parsed = _parse_list_row(row)
        if parsed:
            results.append(parsed)
    return results


def _parse_list_row(row: Node) -> dict | None:
    tds = row.css("td")
    if len(tds) < 11:
        return None

    # TD[0]: logo cell — slug and AirtableId from img src
    logo_link = tds[0].css_first("a")
    if not logo_link:
        return None
    href = logo_link.attributes.get("href", "")  # "fund/Curiosity%20VC"
    slug = unquote(href.replace("fund/", "").strip())
    if not slug:
        return None

    airtable_id, logo_url = "", ""
    logo_img = tds[0].css_first("img")
    if logo_img:
        src = logo_img.attributes.get("src", "")
        m = re.search(r"(rec[A-Za-z0-9]{10,})", src)
        if m:
            airtable_id = m.group(1)
        logo_url = (_BASE_URL + src[1:]) if src.startswith("./") else src

    # TD[1]: name + investor type
    name_link = tds[1].css_first("a.VClink, a.text-dark")
    name = name_link.text(strip=True) if name_link else tds[1].text(strip=True).split("\n")[0].strip()
    type_link = tds[1].css_first('a[href*="investor-lists"]')
    investor_type = type_link.text(strip=True) if type_link else ""
    is_verified = bool(tds[1].css_first(".badge-verified"))

    # TD[2]: top-2 countries + overflow count
    country_imgs = tds[2].css('img[src*="flag"]')
    countries_top = [img.attributes.get("alt", "").replace("Flag of ", "") for img in country_imgs]
    overflow_link = tds[2].css_first("a.VClink")
    overflow_str = overflow_link.text(strip=True).lstrip("+") if overflow_link else "0"
    countries_overflow = int(overflow_str) if overflow_str.isdigit() else 0

    # TD[3]: first check range
    check_text = tds[3].text(strip=True)
    check_min, check_max = _parse_check_size(check_text)

    # TD[4]: top-2 stages + overflow
    stage_links = tds[4].css('a[href*="investor-lists"]')
    stages_top = [a.text(strip=True) for a in stage_links if a.text(strip=True)]
    stage_overflow_link = tds[4].css_first("a.VClink")
    stage_overflow_str = stage_overflow_link.text(strip=True).lstrip("+") if stage_overflow_link else "0"
    stages_overflow = int(stage_overflow_str) if stage_overflow_str.isdigit() else 0

    # TD[6]: investment thesis (truncated)
    thesis = tds[6].text(strip=True) if len(tds) > 6 else ""

    # TD[10]: reply rate (hidden column, still in DOM)
    reply_rate = tds[10].text(strip=True) if len(tds) > 10 else ""

    return {
        "Slug": slug,
        "Name": name,
        "InvestorType": investor_type,
        "IsVerified": is_verified,
        "AirtableId": airtable_id,
        "LogoUrl": logo_url,
        "CountriesTop": ", ".join(countries_top),
        "CountriesOverflow": countries_overflow,
        "FirstCheckText": check_text,
        "FirstCheckMin": check_min,
        "FirstCheckMax": check_max,
        "StagesTop": ", ".join(stages_top),
        "StagesOverflow": stages_overflow,
        "InvestmentThesis": thesis,
        "ReplyRate": reply_rate,
        "ProfileUrl": f"{_BASE_URL}/fund/{quote(slug, safe='')}",
        "ListScraped": True,
        "IsActive": True,
    }


def _parse_check_size(text: str) -> tuple[float | None, float | None]:
    """Parse "$50k to $1M" → (50000.0, 1000000.0). Returns (None, None) on failure."""

    def _to_float(s: str) -> float | None:
        s = s.strip().lstrip("$").replace(",", "").lower()
        multipliers = {"k": 1_000, "m": 1_000_000}
        for suffix, mult in multipliers.items():
            if s.endswith(suffix):
                try:
                    return float(s[:-1]) * mult
                except ValueError:
                    return None
        try:
            return float(s)
        except ValueError:
            return None

    m = re.search(r"(\$[\d,.]+[kmKM]?)\s*(?:to|-)\s*(\$[\d,.]+[kmKM]?)", text, re.IGNORECASE)
    if m:
        return _to_float(m.group(1)), _to_float(m.group(2))
    m2 = re.search(r"(\$[\d,.]+[kmKM]?)", text, re.IGNORECASE)
    if m2:
        return _to_float(m2.group(1)), None
    return None, None


# ── Detail page ──────────────────────────────────────────────────────────────


def parse_detail_page(html: str, slug: str) -> dict:
    """Parse a /fund/{slug}?modal=true page; return enrichment fields.

    Caller merges this dict into the existing NocoDB Investors row.
    """
    tree = HTMLParser(html)
    result: dict = {"Slug": slug, "DetailScraped": True}

    # AirtableId + LogoUrl from profile logo img
    logo = tree.css_first(".logo img, .investor-logo img")
    if logo:
        src = logo.attributes.get("src", "")
        m = re.search(r"(rec[A-Za-z0-9]{10,})", src)
        if m:
            result["AirtableId"] = m.group(1)
        if src:
            result["LogoUrl"] = (_BASE_URL + src[1:]) if src.startswith("./") else src

    # Themes / sectors — comma-separated string (matches old table format)
    themes_container = tree.css_first(".themes")
    if themes_container:
        themes = []
        for item in themes_container.css(".theme-item"):
            cls = item.attributes.get("class", "")
            if "more-theme-item" in cls or "less-theme-item" in cls:
                continue
            text = item.text(strip=True)
            if text:
                themes.append(text)
        if themes:
            result["Themes"] = ", ".join(themes)

    # Funding Stages — comma-separated string
    stages = [
        a.text(strip=True)
        for a in tree.css(".stages .stage-item")
        if a.text(strip=True)
    ]
    if stages:
        result["FundingStages"] = ", ".join(stages)

    # Countries of investment — only from the dedicated .countries section (a.country-item).
    # Grabbing all img[src*="/flags/"] picks up team-member country badges too, causing
    # false positives. The investment-countries anchors have class="country-item".
    seen_countries: set = set()
    countries = []
    for a in tree.css("a.country-item"):
        img = a.css_first("img")
        c = (img.attributes.get("alt", "").replace("Flag of ", "").strip() if img
             else a.text(strip=True).strip())
        if c and c not in seen_countries:
            countries.append(c)
            seen_countries.add(c)
    if countries:
        result["Countries"] = ", ".join(countries)

    # First check text + min/max
    result.update(_parse_check_block(tree))

    # IsLead — "Lead" field in the overview row ("N/A" → False, "Yes" → True)
    result["IsLead"] = _parse_is_lead(tree)

    # Reply rate + Responds in
    result.update(_parse_stats_block(tree))

    # About sections — "Who we are", "Funding Requirements", "Value add"
    result.update(_parse_about_sections(tree))

    # HQ location — split into city + country
    city, country = _parse_hq_location(tree)
    if city:
        result["HQCity"] = city
    if country:
        result["HQCountry"] = country

    # Fund-level LinkedIn URL (first button[village-data-url] that is LinkedIn)
    for btn in tree.css("button[village-data-url]"):
        url = btn.attributes.get("village-data-url", "")
        if "linkedin.com" in url.lower():
            result["LinkedInUrl"] = url
            break

    # Investor subtype from the overview .type section
    subtype = _parse_investor_subtype(tree)
    if subtype:
        result["InvestorSubtype"] = subtype

    # Featured investor lists — all investor-list links excluding stage items
    featured = _parse_featured_lists(tree)
    if featured:
        result["FeaturedLists"] = ", ".join(featured)

    # Portfolio companies
    portfolio = _parse_portfolio(tree)
    if portfolio:
        result["PortfolioJson"] = json.dumps(portfolio)

    # Investment thesis — first team member tagline (personal voice)
    taglines = _extract_taglines(tree)
    if taglines:
        result["InvestmentThesis"] = taglines[0]

    return result


def parse_detail_team(html: str, slug: str) -> list[dict]:
    """Parse team members from a detail page; returns InvestorTeam row dicts."""
    tree = HTMLParser(html)
    team_container = tree.css_first(".ventures-team")
    if not team_container:
        return []

    members = []
    for member_div in team_container.css(".d-flex.flex-xl-row"):
        user_div = member_div.css_first("[data-id]")
        airtable_id = user_div.attributes.get("data-id", "") if user_div else ""

        name_link = member_div.css_first(".investorname a")
        name = name_link.text(strip=True) if name_link else ""
        profile_slug = name_link.attributes.get("href", "") if name_link else ""

        # Profile picture — stored as "./images/users/<AirtableId>.jpg" relative URL
        picture_url = ""
        img_el = member_div.css_first(".user-image img, img.ire-object-cover")
        if img_el:
            src = img_el.attributes.get("src", "")
            if src:
                picture_url = (_BASE_URL + src[1:]) if src.startswith("./") else src

        role, tagline = "", ""
        details = member_div.css_first(".ventures-team-details")
        if details:
            paras = details.css("p")
            if paras:
                role = paras[0].text(strip=True)
            if len(paras) >= 2:
                tagline = paras[1].text(strip=True).strip('"')

        # LinkedIn URL is in `button[village-data-url]` (not a plain <a> link)
        linkedin_url = _extract_linkedin(member_div)

        if name:
            members.append({
                "FundSlug": slug,
                "AirtableId": airtable_id,
                "Name": name,
                "Picture": picture_url,
                "Role": role,
                "Tagline": tagline,
                "LinkedInUrl": linkedin_url,
                "ProfileSlug": profile_slug,
            })
    return members


# ── Private helpers ───────────────────────────────────────────────────────────


def _parse_check_block(tree: HTMLParser) -> dict:
    html = tree.html or ""
    idx = html.find("First check")
    if idx == -1:
        return {}
    snippet = html[idx: idx + 300]
    m = re.search(r"\$([\d,.]+[kKmM]?)\s*(?:to|-)\s*\$([\d,.]+[kKmM]?)", snippet, re.IGNORECASE)
    if m:
        text = m.group(0)
        lo, hi = _parse_check_size(text)
        return {"FirstCheckText": text, "FirstCheckMin": lo, "FirstCheckMax": hi}
    m2 = re.search(r"\$([\d,.]+[kKmM]?\+?)", snippet, re.IGNORECASE)
    if m2:
        text = m2.group(0)
        lo, _ = _parse_check_size(text)
        return {"FirstCheckText": text, "FirstCheckMin": lo}
    return {}


def _parse_stats_block(tree: HTMLParser) -> dict:
    result = {}
    html = tree.html or ""
    m = re.search(r"Reply Rate.*?<h3[^>]*>\s*(\d+%)\s*</h3>", html, re.DOTALL)
    if m:
        result["ReplyRate"] = m.group(1)
    m2 = re.search(r"Responds in.*?<h3[^>]*>\s*([^<]+?)\s*</h3>", html, re.DOTALL)
    if m2:
        result["RespondsIn"] = m2.group(1).strip()
    return result


def _parse_is_lead(tree: HTMLParser) -> bool:
    """Return True if the 'Lead' field is set to something other than N/A / empty."""
    for col in tree.css(".type .col-md-4, .type .col-4"):
        sub = col.css_first(".about-sub-title")
        if sub and "lead" in sub.text(strip=True).lower():
            val = col.css_first(".ire-text-black-full")
            if val:
                return val.text(strip=True).lower() not in ("n/a", "", "no", "-")
    return False


def _parse_about_sections(tree: HTMLParser) -> dict:
    """Parse 'Who we are', 'Funding Requirements', and 'Value add' paragraphs."""
    result = {}
    for section in tree.css(".about-description"):
        h3 = section.css_first("h3")
        if not h3:
            continue
        header = h3.text(strip=True).lower()
        # Strip the header text from the full section text to get just the body
        full = section.text(strip=True)
        body = full[len(h3.text(strip=True)):].strip()
        if not body:
            continue
        if "who we are" in header:
            result["AboutUs"] = body
        elif "value add" in header:
            result["ValueAdd"] = body
        elif "funding requirement" in header:
            result["FundingRequirements"] = body
    return result


def _parse_hq_location(tree: HTMLParser) -> tuple[str, str]:
    """Return (city, country) from the HQ location block.

    Handles messy real-world formats:
      "2128 Sand Hill Rd, 94025 Menlo Park, CA, California, US"
      "53113 Bonn, Deutschland"
      "Karlsruhe, Germany"
      "rue Auber 75009 Paris France"
      "Île-de-France 75002, FR"
    """
    loc = tree.css_first(".locations .place-name")
    if not loc:
        return "", ""
    addr = loc.text(strip=True).replace(" HQ", "").replace("HQ", "").strip()
    if not addr:
        return "", ""

    parts = [p.strip() for p in addr.split(",") if p.strip()]
    if not parts:
        return "", ""

    # Country: last segment only if it looks like a real country
    # (2-letter ISO code OR letters/hyphens only, no digits)
    last = parts[-1]
    country = last if re.match(r'^[A-Za-z][A-Za-z\s\-\.\']{0,30}$', last) else ""

    # City: scan right-to-left through remaining parts
    candidates = parts[:-1] if country else parts
    city = ""
    for part in reversed(candidates):
        # Skip 2-letter state codes (CA, NY ...)
        if re.match(r'^[A-Z]{2}$', part):
            continue
        # Bare zip code — skip
        if re.match(r'^\d{4,6}$', part):
            continue
        # "ZIPCODE CityName" — strip the leading zip, keep city
        m = re.match(r'^\d{4,6}\s+(.+)', part)
        if m:
            city = m.group(1).strip()
            break
        # "CityName ZIPCODE" — strip the trailing zip, keep city
        m = re.match(r'^(.+?)\s+\d{4,6}$', part)
        if m:
            candidate = m.group(1).strip()
            if not re.match(r'^[A-Z]{2}$', candidate):
                city = candidate
            continue
        # "street ZIPCODE CityName [Country]" — ZIP embedded in middle, take text after it
        m = re.search(r'\b\d{4,6}\b\s+(.+)', part)
        if m:
            candidate = m.group(1).strip()
            if candidate and candidate != country:
                # Strip trailing country-like word (e.g. "Paris France" → "Paris")
                words = candidate.split()
                if len(words) > 1 and re.match(r'^[A-Z][a-zA-Z]{2,}$', words[-1]):
                    if not country:
                        country = words[-1]
                    candidate = " ".join(words[:-1])
                city = candidate
                break
        # Long street address (starts with number, long) — skip
        if re.match(r'^\d+\s+\S', part) and len(part) > 25:
            continue
        city = part
        break

    # No comma at all → try "ZIPCODE City Country" format
    if not city and not country and len(parts) == 1:
        words = parts[0].split()
        word_parts = [w for w in words if not re.match(r'^\d{4,6}$', w)]
        if len(word_parts) >= 2:
            country = word_parts[-1]
            city = " ".join(word_parts[:-1])
        elif word_parts:
            city = word_parts[0]

    # Final cleanup: bare 2-letter state code is not a city name
    if re.match(r'^[A-Z]{2}$', city):
        city = ""

    return city, country


def _parse_investor_subtype(tree: HTMLParser) -> str:
    """Extract 'Investor type' value from the .type overview section."""
    for col in tree.css(".type .col-md-4, .type .col-4"):
        label = col.css_first(".about-sub-title")
        if label and "investor type" in label.text(strip=True).lower():
            val = col.css_first(".ire-text-black-full, a.text-dark")
            if val:
                return val.text(strip=True)
    return ""


# Stage label prefixes to filter out from featured lists (e.g. "3. Early Revenue")
_STAGE_PATTERN = re.compile(r'^\d+\.')


def _parse_featured_lists(tree: HTMLParser) -> list[str]:
    """Return investor list names from a[href*='investor-lists'], excluding stage items."""
    seen: set = set()
    result = []
    for a in tree.css('a[href*="investor-lists"]'):
        text = a.text(strip=True)
        if not text or text in seen:
            continue
        # Skip numbered stage items like "3. Early Revenue"
        if _STAGE_PATTERN.match(text):
            continue
        seen.add(text)
        result.append(text)
    return result


def _parse_portfolio(tree: HTMLParser) -> list[dict]:
    portfolio = []
    for item in tree.css(".ventures-portfolio-item"):
        name_el = item.css_first("h3.company-name")
        if not name_el:
            continue
        name = name_el.text(strip=True)
        url_el = item.css_first("p.ire-opacity-60 a")
        url = url_el.attributes.get("href", "") if url_el else ""
        if name:
            portfolio.append({"name": name, "url": url})
    return portfolio


def _extract_linkedin(member_div: Node) -> str:
    """Extract LinkedIn URL from team member div.

    OpenVC encodes LinkedIn in `button[village-data-url]`, not a plain <a> link.
    """
    # Try button with village-data-url (primary location)
    btn = member_div.css_first("button[village-data-url]")
    if btn:
        url = btn.attributes.get("village-data-url", "")
        if "linkedin" in url.lower():
            return url
    # Fallback: plain anchor (older profile format)
    a = member_div.css_first('a[href*="linkedin"]')
    if a:
        return a.attributes.get("href", "")
    return ""


def _extract_taglines(tree: HTMLParser) -> list[str]:
    team = tree.css_first(".ventures-team")
    if not team:
        return []
    taglines = []
    for member_div in team.css(".d-flex.flex-xl-row"):
        details = member_div.css_first(".ventures-team-details")
        if details:
            paras = details.css("p")
            if len(paras) >= 2:
                t = paras[1].text(strip=True).strip('"')
                if t:
                    taglines.append(t)
    return taglines
