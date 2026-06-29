"""Teable client for OpenVC scraper — drop-in replacement for pg.py.

Set these env vars instead of PG_*:
  TEABLE_BASE_URL   http://localhost:3000
  TEABLE_EMAIL      info@dacoders.com
  TEABLE_PASSWORD   ...
  TEABLE_TABLE_INV  investors table ID
  TEABLE_TABLE_TEAM team table ID
  TEABLE_TABLE_PORT portfolio table ID
"""

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

log = logging.getLogger("openvc.teable")

BASE_URL = os.getenv("TEABLE_BASE_URL", "http://localhost:3000")
EMAIL    = os.getenv("TEABLE_EMAIL",    "info@dacoders.com")
PASSWORD = os.getenv("TEABLE_PASSWORD", "")
TBL_INV  = os.getenv("TEABLE_TABLE_INV",  "tbl4yfjhBO6Ge0napsx")
TBL_TEAM = os.getenv("TEABLE_TABLE_TEAM", "tblzVRSU6JHKjAysJ0i")
TBL_PORT = os.getenv("TEABLE_TABLE_PORT", "tbli2OVuCHdf8xaPQcL")

PAGE  = 500
BATCH = 100


class TeableDB:
    """Matches the PostgresDB interface used by scraper.py's formate-pg phase."""

    def __init__(self):
        self._cookie = self._login()
        # Cache: url → teable record ID for investors
        self._url_cache: dict[str, str] = {}
        self._cache_loaded = False
        # Cache: field_name → {choice_name, ...} for multipleSelect fields
        self._choices_cache: dict[str, set] = {}
        log.info("Teable client ready (base=%s)", BASE_URL)

    # ── Internal HTTP ─────────────────────────────────────────────────────────

    def _login(self) -> str:
        data = json.dumps({"email": EMAIL, "password": PASSWORD}).encode()
        req  = urllib.request.Request(
            f"{BASE_URL}/api/auth/signin", data=data,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            for part in r.headers.get("Set-Cookie", "").split(";"):
                if part.strip().startswith("auth_session="):
                    return part.strip()
        raise RuntimeError("Teable login failed — no auth_session cookie")

    def _api(self, method: str, path: str, body=None, retry_auth: bool = True):
        url  = f"{BASE_URL}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req  = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": "application/json", "Cookie": self._cookie})
        try:
            with urllib.request.urlopen(req) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            txt = e.read().decode()
            if e.code == 401 and retry_auth:
                log.info("Teable session expired — re-logging in")
                self._cookie = self._login()
                return self._api(method, path, body, retry_auth=False)
            raise RuntimeError(f"HTTP {e.code} {method} {path}: {txt[:300]}") from e

    # ── Cache management ──────────────────────────────────────────────────────

    def _load_url_cache(self):
        if self._cache_loaded:
            return
        log.info("Loading investor URL → record ID cache from Teable...")
        skip = 0
        while True:
            resp = self._api("GET",
                f"/api/table/{TBL_INV}/record?take={PAGE}&skip={skip}&fieldKeyType=name")
            recs = resp.get("records", [])
            for rec in recs:
                u = rec.get("fields", {}).get("URL", "")
                if u:
                    self._url_cache[u] = rec["id"]
            skip += len(recs)
            if len(recs) < PAGE:
                break
            time.sleep(0.05)
        self._cache_loaded = True
        log.info("Cached %d investor URLs", len(self._url_cache))

    # ── Read (matches pg.py interface) ────────────────────────────────────────

    def list_pending(self, limit: int = 0) -> list[dict]:
        """Return investors where Generated=false, ordered by record creation."""
        filter_param = json.dumps({
            "filterSet": [{
                "fieldId": self._get_field_id(TBL_INV, "Generated"),
                "operator": "is",
                "value": False,
            }],
            "conjunction": "and",
        })
        results = []
        skip    = 0
        take    = min(PAGE, limit) if limit else PAGE
        while True:
            path = (f"/api/table/{TBL_INV}/record"
                    f"?take={take}&skip={skip}&fieldKeyType=name"
                    f"&filter={urllib.parse.quote(filter_param)}")
            resp = self._api("GET", path)
            recs = resp.get("records", [])
            for rec in recs:
                f = rec.get("fields", {})
                results.append({
                    "id":        rec["id"],   # Teable record ID (used for PATCH)
                    "url":       f.get("URL", ""),
                    "full_name": f.get("Name", ""),
                })
            skip += len(recs)
            if len(recs) < take:
                break
            if limit and len(results) >= limit:
                results = results[:limit]
                break
            time.sleep(0.05)
        return results

    def count_pending(self) -> int:
        return len(self.list_pending())

    def _get_field_id(self, table_id: str, field_name: str) -> str:
        """Resolve field name → field ID (needed for filters)."""
        fields = self._api("GET", f"/api/table/{table_id}/field")
        for f in fields:
            if f["name"] == field_name:
                return f["id"]
        raise RuntimeError(f"Field '{field_name}' not found in table {table_id}")

    # ── Write ─────────────────────────────────────────────────────────────────

    def upsert_investors(self, rows: list[dict]) -> int:
        """Create or update investor rows. Matches pg.py interface."""
        if not rows:
            return 0
        self._load_url_cache()
        self._load_ms_choices()  # load valid choices once per session

        _SKIP = {"team", "portfolio", "id"}
        to_create = []
        to_update = []  # list of (record_id, fields)

        for row in rows:
            url = row.get("url", "")
            if not url:
                continue
            fields = self._pg_to_teable(row)
            # Filter multipleSelect values to known choices only
            for ms in ("Stages", "Sectors", "Countries Invested"):
                if ms in fields and isinstance(fields[ms], list):
                    fields[ms] = self._filter_ms(ms, fields[ms]) or None
                    if fields[ms] is None:
                        del fields[ms]
            rec_id = self._url_cache.get(url)
            if rec_id:
                to_update.append((rec_id, fields))
            else:
                to_create.append((url, fields))

        # Batch create
        for i in range(0, len(to_create), BATCH):
            chunk = to_create[i:i+BATCH]
            resp  = self._api("POST", f"/api/table/{TBL_INV}/record",
                              {"records": [{"fields": f} for _, f in chunk]})
            for rec, (url, _) in zip(resp.get("records", []), chunk):
                self._url_cache[url] = rec["id"]
            time.sleep(0.05)

        # Batch update
        for i in range(0, len(to_update), BATCH):
            chunk = to_update[i:i+BATCH]
            self._api("PATCH", f"/api/table/{TBL_INV}/record",
                      {"records": [{"id": rid, "fields": f} for rid, f in chunk]})
            time.sleep(0.05)

        log.info("upserted %d investor rows (%d new, %d updated)",
                 len(rows), len(to_create), len(to_update))
        return len(rows)

    def upsert_team(self, rows: list[dict]) -> int:
        """Delete old team for each fund, insert fresh. Matches pg.py interface."""
        if not rows:
            return 0
        self._load_url_cache()

        by_url: dict[str, list] = {}
        for r in rows:
            by_url.setdefault(r["investor_url"], []).append(r)

        for url, members in by_url.items():
            inv_rec_id = self._url_cache.get(url)

            # Delete existing team rows for this investor (search by Investor URL text)
            self._delete_where_text(TBL_TEAM, "Investor URL", url)

            # Dedup by name
            seen: set = set()
            deduped   = []
            for m in members:
                if m["name"] not in seen:
                    seen.add(m["name"])
                    deduped.append(m)

            # Build record payloads
            records = []
            for m in deduped:
                f = {
                    "Name":        m.get("name", ""),
                    "Role":        m.get("role", "") or "",
                    "Description": m.get("description", "") or "",
                    "LinkedIn URL":m.get("linkedin_url", "") or "",
                    "Picture URL": m.get("picture", "") or "",
                    "Profile Slug":m.get("profile_slug", "") or "",
                    "Investor URL":url,
                }
                if inv_rec_id:
                    f["Investor"] = {"id": inv_rec_id}
                records.append({"fields": {k: v for k, v in f.items() if v}})

            for i in range(0, len(records), BATCH):
                self._api("POST", f"/api/table/{TBL_TEAM}/record",
                          {"records": records[i:i+BATCH]})
                time.sleep(0.05)

        log.info("upserted team for %d funds", len(by_url))
        return len(rows)

    def upsert_portfolio(self, rows: list[dict]) -> int:
        """Delete old portfolio for each fund, insert fresh. Matches pg.py interface."""
        if not rows:
            return 0
        self._load_url_cache()

        by_url: dict[str, list] = {}
        for r in rows:
            by_url.setdefault(r["investor_url"], []).append(r)

        for url, companies in by_url.items():
            inv_rec_id = self._url_cache.get(url)
            self._delete_where_text(TBL_PORT, "Investor URL", url)

            records = []
            for c in companies:
                f = {
                    "Company Name": c.get("company_name", ""),
                    "Company URL":  c.get("company_url", "") or "",
                    "Investor URL": url,
                }
                if inv_rec_id:
                    f["Investor"] = {"id": inv_rec_id}
                records.append({"fields": {k: v for k, v in f.items() if v}})

            for i in range(0, len(records), BATCH):
                self._api("POST", f"/api/table/{TBL_PORT}/record",
                          {"records": records[i:i+BATCH]})
                time.sleep(0.05)

        log.info("upserted portfolio for %d funds", len(by_url))
        return len(rows)

    # ── Private helpers ───────────────────────────────────────────────────────

    def _load_ms_choices(self) -> None:
        """Load multipleSelect choices from Teable once per session (cached)."""
        if self._choices_cache:
            return
        fields = self._api("GET", f"/api/table/{TBL_INV}/field")
        for f in fields:
            if f["type"] == "multipleSelect":
                self._choices_cache[f["name"]] = {
                    c["name"] for c in f.get("options", {}).get("choices", [])
                }
        log.info("Loaded choices: %s",
                 {k: len(v) for k, v in self._choices_cache.items()})

    def _filter_ms(self, field_name: str, values: list) -> list:
        """Return only values that are valid choices; log any unknowns."""
        known = self._choices_cache.get(field_name)
        if not known:
            return values
        valid   = [v for v in values if v in known]
        skipped = [v for v in values if v not in known]
        if skipped:
            log.warning("Skipping unknown choices for '%s': %s", field_name, skipped)
        return valid

    def _delete_where_text(self, table_id: str, field_name: str, value: str):
        """Delete all records in table where field_name == value."""
        try:
            fid = self._get_field_id(table_id, field_name)
        except RuntimeError:
            return
        filter_param = json.dumps({
            "filterSet": [{"fieldId": fid, "operator": "is",
                           "value": value}],
            "conjunction": "and",
        })
        skip = 0
        while True:
            path = (f"/api/table/{table_id}/record"
                    f"?take={PAGE}&skip={skip}"
                    f"&filter={urllib.parse.quote(filter_param)}")
            resp = self._api("GET", path)
            recs = resp.get("records", [])
            if not recs:
                break
            for rec in recs:
                self._api("DELETE", f"/api/table/{table_id}/record/{rec['id']}")
            if len(recs) < PAGE:
                break
            time.sleep(0.05)

    # Country normalization map — applied on every write so PG raw values never leak in
    _COUNTRY_MAP = {
        # US variants
        "US": "United States", "USA": "United States",
        "United States of America": "United States",
        # US state codes → United States
        "AL":"United States","AK":"United States","AZ":"United States",
        "AR":"United States","CO":"United States","CT":"United States",
        "DC":"United States","DE":"United States","FL":"United States",
        "GA":"United States","HI":"United States","ID":"United States",
        "IN":"United States","IA":"United States","KS":"United States",
        "KY":"United States","LA":"United States","ME":"United States",
        "MD":"United States","MA":"United States","MI":"United States",
        "MN":"United States","MS":"United States","MO":"United States",
        "MT":"United States","NE":"United States","NV":"United States",
        "NH":"United States","NJ":"United States","NM":"United States",
        "NY":"United States","NC":"United States","ND":"United States",
        "OH":"United States","OK":"United States","OR":"United States",
        "PA":"United States","RI":"United States","SC":"United States",
        "SD":"United States","TN":"United States","TX":"United States",
        "UT":"United States","VT":"United States","VA":"United States",
        "WA":"United States","WV":"United States","WI":"United States",
        "WY":"United States","PR":"United States","GU":"United States",
        # EU / EEA
        "DE":"Germany","Deutschland":"Germany",
        "GB":"United Kingdom","UK":"United Kingdom",
        "NL":"Netherlands","The Netherlands":"Netherlands",
        "FR":"France","ES":"Spain","IT":"Italy",
        "SE":"Sweden","NO":"Norway","DK":"Denmark",
        "FI":"Finland","AT":"Austria","BE":"Belgium",
        "CH":"Switzerland","PL":"Poland","PT":"Portugal",
        "IE":"Ireland","CZ":"Czech Republic","SK":"Slovakia",
        "HU":"Hungary","RO":"Romania","BG":"Bulgaria",
        "HR":"Croatia","EE":"Estonia","LV":"Latvia",
        "LT":"Lithuania","GR":"Greece","LU":"Luxembourg",
        "CY":"Cyprus","MT":"Malta","IS":"Iceland",
        "RS":"Serbia","ME":"Montenegro",
        "BA":"Bosnia and Herzegovina",
        "MK":"North Macedonia","MD":"Moldova",
        "UA":"Ukraine","BY":"Belarus","RU":"Russia","TR":"Turkey",
        # Americas
        "CA":"Canada","BR":"Brazil","MX":"Mexico",
        "AR":"Argentina","CL":"Chile","CO":"Colombia",
        "PE":"Peru","VE":"Venezuela","EC":"Ecuador","UY":"Uruguay",
        # Middle East / Asia
        "IL":"Israel",
        "AE":"UAE","United Arab Emirates":"UAE",
        "SA":"Saudi Arabia","EG":"Egypt",
        "JP":"Japan","KR":"South Korea",
        "CN":"China","HK":"Hong Kong","TW":"Taiwan",
        "SG":"Singapore","AU":"Australia","NZ":"New Zealand",
        "IN":"India","MY":"Malaysia","ID":"Indonesia",
        "TH":"Thailand","PH":"Philippines","VN":"Vietnam",
        "PK":"Pakistan","BD":"Bangladesh","LK":"Sri Lanka",
        # Africa
        "ZA":"South Africa","NG":"Nigeria","KE":"Kenya","MA":"Morocco",
        "GH":"Ghana","ET":"Ethiopia","TZ":"Tanzania","SN":"Senegal",
        # Caucasus / Central Asia
        "AM":"Armenia","GE":"Georgia","AZ":"Azerbaijan",
        "KZ":"Kazakhstan","UZ":"Uzbekistan",
        # Additional territories, small states, and remaining ISO codes
        "ON":"Canada",                       # Ontario province
        "VI":"United States",                # US Virgin Islands
        "VG":"British Virgin Islands",
        "BM":"Bermuda",
        "GG":"Guernsey","GI":"Gibraltar","IM":"Isle of Man","AX":"Åland Islands",
        "JO":"Jordan","KW":"Kuwait","QA":"Qatar","BH":"Bahrain",
        "IQ":"Iraq","IR":"Iran","LB":"Lebanon","OM":"Oman","PS":"Palestinian Territories",
        "MC":"Monaco","LI":"Liechtenstein","LU":"Luxembourg",
        "DO":"Dominican Republic","GT":"Guatemala","TT":"Trinidad and Tobago",
        "CI":"Côte d'Ivoire","TG":"Togo","UG":"Uganda","ZW":"Zimbabwe",
        "SI":"Slovenia","MU":"Mauritius","NP":"Nepal",
    }

    def _normalize_country(self, value: str) -> str:
        if not value:
            return value
        return self._COUNTRY_MAP.get(value, value)

    def _pg_to_teable(self, row: dict) -> dict:
        """Convert a PG investor row dict to Teable field dict."""
        import re as _re

        def to_arr(v):
            """Return Python list (for multipleSelect fields)."""
            if v is None: return None
            if isinstance(v, list): return [str(x) for x in v if x]
            return [s.strip() for s in str(v).split(",") if s.strip()]

        def stages_arr(v):
            """Parse stage list, stripping 'N. ' prefixes and '+N' suffixes."""
            VALID = {"Idea or Patent","Prototype","Early Revenue",
                     "Scaling","Growth","Pre-IPO","N/A"}
            items = to_arr(v) or []
            result = []
            for item in items:
                norm = _re.sub(r'(\w)\s*(\d+\.\s)', r'\1, \2', item)
                for part in norm.split(","):
                    p = _re.sub(r'^\d+\.\s*', '', part).strip()
                    p = _re.sub(r'\s*\+\d+$', '', p).strip()
                    if p in VALID and p not in result:
                        result.append(p)
            return result or None

        def arr_txt(v):
            """Comma-joined string for singleLineText array fields."""
            if v is None: return None
            return ", ".join(str(x) for x in v) if isinstance(v, list) else str(v)

        def num(v):
            try: return float(v)
            except: return None

        def dt(v):
            if v is None: return None
            return v.isoformat() if hasattr(v, "isoformat") else str(v)

        d = {
            "Name":               row.get("full_name") or "",
            "URL":                row.get("url") or "",
            "Generated":          bool(row.get("generated")),
            "Investor Type":      row.get("investor_type"),
            "Investor Subtype":   row.get("investor_subtype"),
            "City":               row.get("city"),
            "Country":            self._normalize_country(row.get("country") or ""),
            "Currency":           "USD" if row.get("currency") == "$" else row.get("currency"),
            "Investment Min":     num(row.get("investment_min")),
            "Investment Max":     num(row.get("investment_max")),
            "Average Check":      num(row.get("average_check")),
            "AUM":                num(row.get("aum")),
            "Stages":             stages_arr(row.get("stages")),
            "Sectors":            to_arr(row.get("sectors")),
            "Countries Invested": [{"USA":"United States","UK":"United Kingdom","UAE":"United Arab Emirates"}.get(v,v)
                                   for v in (to_arr(row.get("countries_of_investment")) or [])] or None,
            "Featured Lists":     arr_txt(row.get("featured_lists")),
            "Description":        row.get("description"),
            "Value Add":          row.get("value_add"),
            "Investment Thesis":  row.get("investment_thesis"),
            "Stage Focus":        row.get("company_stage_focus"),
            "Company":            row.get("company"),
            "Company Role":       row.get("company_role"),
            "Company URL":        row.get("company_url"),
            "Website":            row.get("website"),
            "LinkedIn":           row.get("linkedin"),
            "Twitter":            row.get("twitter"),
            "Reply Rate":         row.get("reply_rate"),
            "Response Time":      row.get("response_time"),
            "Lead Investor":      row.get("lead_investor"),
            "Picture URL":        row.get("picture"),
            "Scrape Date":        dt(row.get("scrape_date")),
        }
        # Strip None / empty / empty lists
        return {k: v for k, v in d.items()
                if v is not None and v != "" and v != []}

