"""Minimal NocoDB REST client (API v2).

NocoDB replaces the database layer: the scraper reads/writes records over HTTP
instead of SQL. Auth is an API token sent as the `xc-token` header.

Records are addressed by NocoDB's own `Id` field (auto primary key). Our logical
key is the `Slug` column — callers map slug -> Id themselves via `list_all`.
"""

import logging
import os

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

log = logging.getLogger("zah-scraper.nocodb")

# NocoDB caps bulk insert/update payloads; 100/request is comfortably under the limit.
CHUNK = 100


class NocoDBError(RuntimeError):
    pass


_RETRYABLE = (httpx.TransportError, httpx.HTTPStatusError)


class NocoDB:
    def __init__(self, base_url: str | None = None, token: str | None = None, timeout: float = 30.0):
        self.base_url = (base_url or os.environ["NOCODB_BASE_URL"]).rstrip("/")
        token = token or os.environ["NOCODB_API_TOKEN"]
        self._client = httpx.Client(
            headers={"xc-token": token, "Content-Type": "application/json"},
            timeout=timeout,
        )

    def __enter__(self) -> "NocoDB":
        return self

    def __exit__(self, *exc) -> None:
        self._client.close()

    def _records_url(self, table_id: str) -> str:
        return f"{self.base_url}/api/v2/tables/{table_id}/records"

    @retry(
        stop=stop_after_attempt(12),
        wait=wait_exponential_jitter(initial=5, max=120),
        retry=retry_if_exception_type(_RETRYABLE),
        reraise=True,
    )
    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        r = self._client.request(method, url, **kwargs)
        # Retry transient failures; fail loudly on client errors (bad token, bad table id).
        if r.status_code == 429 or r.status_code >= 500:
            log.warning("NocoDB %s %s -> %d (will retry)", method, url, r.status_code)
            r.raise_for_status()
        if r.status_code >= 400:
            raise NocoDBError(f"{method} {url} -> {r.status_code}: {r.text[:300]}")
        return r

    # --- Meta API: table bootstrap -------------------------------------------------

    def list_tables(self, base_id: str) -> list[dict]:
        """Return the tables in a base — each dict has at least `id` and `title`."""
        url = f"{self.base_url}/api/v2/meta/bases/{base_id}/tables"
        return self._request("GET", url).json().get("list", [])

    def create_table(self, base_id: str, title: str, columns: list[tuple[str, str]]) -> str:
        """Create a table from (column_name, uidt) pairs; return its table id.
        The first column becomes NocoDB's display value."""
        url = f"{self.base_url}/api/v2/meta/bases/{base_id}/tables"
        payload = {
            "title": title,
            "table_name": title,
            "columns": [
                {"title": name, "column_name": name, "uidt": uidt}
                for name, uidt in columns
            ],
        }
        return self._request("POST", url, json=payload).json()["id"]

    def list_columns(self, table_id: str) -> list[str]:
        """Return the column titles of an existing table."""
        url = f"{self.base_url}/api/v2/meta/tables/{table_id}"
        cols = self._request("GET", url).json().get("columns", [])
        return [c.get("title") for c in cols if c.get("title")]

    def create_column(self, table_id: str, name: str, uidt: str) -> None:
        """Add a single column to an existing table."""
        url = f"{self.base_url}/api/v2/meta/tables/{table_id}/columns"
        self._request("POST", url, json={"title": name, "column_name": name, "uidt": uidt})

    # --- Data API: records ---------------------------------------------------------

    def list_all(self, table_id: str, fields: list[str] | None = None, page_size: int = 100) -> list[dict]:
        """Fetch every record in a table, following pagination.

        NocoDB caps how many rows it returns per page regardless of the
        requested `limit`, so the offset must advance by the number of rows
        actually returned — not by `page_size`. Advancing by `page_size` would
        skip rows and request offsets past the end (NocoDB rejects those)."""
        records: list[dict] = []
        offset = 0
        while True:
            params: dict = {"limit": page_size, "offset": offset}
            if fields:
                params["fields"] = ",".join(fields)
            data = self._request("GET", self._records_url(table_id), params=params).json()
            page = data.get("list", [])
            records.extend(page)
            if not page or data.get("pageInfo", {}).get("isLastPage", True):
                break
            offset += len(page)
        return records

    def bulk_create(self, table_id: str, rows: list[dict]) -> int:
        """Insert records. `rows` are field dicts without an Id."""
        return self._chunked("POST", table_id, rows)

    def bulk_update(self, table_id: str, rows: list[dict]) -> int:
        """Update records. Each row MUST include its NocoDB `Id`."""
        for row in rows:
            if "Id" not in row:
                raise NocoDBError("bulk_update row is missing 'Id'")
        return self._chunked("PATCH", table_id, rows)

    def _chunked(self, method: str, table_id: str, rows: list[dict]) -> int:
        url = self._records_url(table_id)
        done = 0
        for i in range(0, len(rows), CHUNK):
            chunk = rows[i : i + CHUNK]
            self._request(method, url, json=chunk)
            done += len(chunk)
            log.info("%s %d/%d records", method, done, len(rows))
        return done

    def create_rows(self, table_id: str, rows: list[dict]) -> list[int]:
        """Insert rows and return their NocoDB IDs."""
        if not rows:
            return []
        url = self._records_url(table_id)
        ids = []
        for i in range(0, len(rows), CHUNK):
            chunk = rows[i : i + CHUNK]
            resp = self._request("POST", url, json=chunk).json()
            if isinstance(resp, list):
                ids.extend(r["Id"] for r in resp if "Id" in r)
            elif isinstance(resp, dict) and "Id" in resp:
                ids.append(resp["Id"])
        return ids

    def delete_where(self, table_id: str, field: str, value: int | str) -> int:
        """Delete all rows where field equals value using server-side filter."""
        url = self._records_url(table_id)
        ids = []
        offset = 0
        while True:
            data = self._request("GET", url, params={
                "limit": 100, "offset": offset,
                "fields": "Id",
                "where": f"({field},eq,{value})",
            }).json()
            page = data.get("list", [])
            ids.extend(r["Id"] for r in page)
            if not page or data.get("pageInfo", {}).get("isLastPage", True):
                break
            offset += len(page)
        if not ids:
            return 0
        for i in range(0, len(ids), 25):
            self._request("DELETE", url, json=[{"Id": id} for id in ids[i : i + 25]])
        return len(ids)
