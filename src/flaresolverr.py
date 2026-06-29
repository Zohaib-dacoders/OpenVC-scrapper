"""Cloudflare bypass via FlareSolverr.

zahcomputers.pk sits behind a Cloudflare JS challenge — plain httpx hits a 403
"Just a moment...". FlareSolverr (a Dockerised undetected-chromedriver) solves
the challenge once; we cache the `cf_clearance` cookie + matching user-agent
and replay them on every subsequent httpx request until they expire.

The cookie is bound to (cookie value, user-agent, IP). Always replay all three
together. If `proxy_url` is set, both FlareSolverr's solve AND the replay must
go through that same proxy, or Cloudflare will reject the replay.

**FlareSolverr v3 proxy contract gotchas (learned the hard way):**
- `user:pass@host:port` in the proxy URL silently fails — Chromium can't
  authenticate to the proxy from that URL form and serves its built-in error
  page. The wrapper here parses the URL and sends `{url, username, password}`
  as separate fields instead.
- The solve takes longer through a residential proxy; default `maxTimeout` of
  60 s is too short. We bump to 180 s when a proxy is configured.

Sync only — the scraper hops into a worker thread for these calls.
"""

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

log = logging.getLogger("zah-scraper.flaresolverr")


def _parse_proxy(proxy_url: str) -> tuple[str, str | None, str | None]:
    """Split 'http://user:pass@host:port' into ('http://host:port', user, pass).
    FlareSolverr's v3 contract wants username/password as separate fields."""
    p = urlparse(proxy_url)
    base = f"{p.scheme}://{p.hostname}:{p.port}"
    return base, p.username, p.password


def redact_proxy(proxy_url: str) -> str:
    """For log messages — replace the password with ***."""
    if not proxy_url:
        return ""
    p = urlparse(proxy_url)
    if p.password:
        return proxy_url.replace(p.password, "***")
    return proxy_url


def _cache_path_for(proxy_url: str | None, override: str = "") -> str:
    """Cache file path. Per-proxy when proxy_url is set, so rotating the proxy
    doesn't clobber yesterday's still-warm cookies for a different IP."""
    if override:
        return override
    if not proxy_url:
        return ".cf-cookies.json"
    h = hashlib.sha1(proxy_url.encode()).hexdigest()[:8]
    return f".cf-cookies.{h}.json"


@dataclass
class CloudflareSession:
    """A reusable Cloudflare-cleared session: cookies + matching user-agent.

    Persists to a JSON file so re-runs within the session window skip the solve.
    If `proxy_url` is set, the solve goes through it (and so must any replay).
    """

    flaresolverr_url: str
    proxy_url: str = ""
    cache_path: str = ""                    # set explicitly to override, otherwise derived from proxy_url
    session_ttl_seconds: int = 3600
    request_timeout_ms: int = 0             # 0 = pick from proxy presence (60s/180s)

    def __post_init__(self) -> None:
        self.cache_path = _cache_path_for(self.proxy_url, self.cache_path)
        if not self.request_timeout_ms:
            self.request_timeout_ms = 180000 if self.proxy_url else 60000

    def get(self, target_url: str, force_refresh: bool = False) -> tuple[dict[str, str], str]:
        if not force_refresh:
            cached = self._load()
            if cached:
                return cached["cookies"], cached["user_agent"]
        return self._solve_and_store(target_url)

    def invalidate(self) -> None:
        try:
            os.unlink(self.cache_path)
            log.info("invalidated cookie cache at %s", self.cache_path)
        except FileNotFoundError:
            pass

    def _load(self) -> dict | None:
        try:
            with open(self.cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return None
        age = time.time() - data.get("solved_at", 0)
        if age > self.session_ttl_seconds:
            log.info("cookie cache %ds old (> %ds TTL); will re-solve", int(age), self.session_ttl_seconds)
            return None
        return data

    def _solve_and_store(self, target_url: str) -> tuple[dict[str, str], str]:
        log.info("solving Cloudflare challenge via FlareSolverr for %s%s",
                 target_url, f" (proxy={redact_proxy(self.proxy_url)})" if self.proxy_url else "")
        cookies, user_agent = _solve(
            self.flaresolverr_url, target_url,
            timeout_ms=self.request_timeout_ms, proxy_url=self.proxy_url,
        )
        payload = {
            "solved_at": time.time(),
            "target_url": target_url,
            "proxy_url": redact_proxy(self.proxy_url),
            "cookies": cookies,
            "user_agent": user_agent,
        }
        tmp = self.cache_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, self.cache_path)
        log.info("stored %d cookies + UA at %s", len(cookies), self.cache_path)
        return cookies, user_agent


class FlareSolverrError(RuntimeError):
    pass


def _solve(
    flaresolverr_url: str,
    target_url: str,
    timeout_ms: int,
    proxy_url: str = "",
) -> tuple[dict[str, str], str]:
    """Ask FlareSolverr to fetch a URL through Chrome and return cookies + UA.

    Routes through `proxy_url` if given. `user:pass@host` URLs are split into
    separate FlareSolverr `username`/`password` fields (see module docstring).
    """
    endpoint = flaresolverr_url.rstrip("/") + "/v1"
    body: dict = {"cmd": "request.get", "url": target_url, "maxTimeout": timeout_ms}
    if proxy_url:
        base, user, pw = _parse_proxy(proxy_url)
        body["proxy"] = {"url": base}
        if user:
            body["proxy"]["username"] = user
        if pw:
            body["proxy"]["password"] = pw

    http_timeout = timeout_ms / 1000 + 30
    try:
        r = httpx.post(endpoint, json=body, timeout=http_timeout)
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise FlareSolverrError(f"FlareSolverr call failed: {e}") from e

    if data.get("status") != "ok":
        raise FlareSolverrError(f"FlareSolverr did not solve: {data.get('message') or data}")

    sol = data.get("solution") or {}
    if sol.get("status") and sol["status"] >= 400:
        raise FlareSolverrError(
            f"FlareSolverr fetched {target_url} but upstream returned {sol['status']}"
        )

    cookies = {c["name"]: c["value"] for c in sol.get("cookies") or [] if c.get("name")}
    user_agent = sol.get("userAgent") or ""
    if "cf_clearance" not in cookies:
        log.warning("FlareSolverr returned no cf_clearance — replay may fail if Cloudflare expects one")
    if not user_agent:
        raise FlareSolverrError("FlareSolverr response missing userAgent")
    return cookies, user_agent
