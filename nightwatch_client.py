"""Read-only Nightwatch REST client; environment-based credentials only."""

import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class NightwatchError(RuntimeError):
    pass


class NightwatchClient:
    def __init__(self, key: str | None = None, opener=urlopen):
        self._key = key or os.environ.get("YEHANGSHE_API_KEY")
        if not self._key:
            raise NightwatchError("YEHANGSHE_API_KEY is not set")
        self._opener = opener
        self._last_request = 0.0
        self.monthly_remaining = None

    def get(self, path: str, max_attempts: int = 2) -> dict:
        if not path.startswith("/v1/"):
            raise ValueError("unexpected API path")
        for attempt in range(max_attempts):
            # Account is shared; this client stays below 50 RPM and <=1 snapshot/sec.
            delay = 1.2 - (time.monotonic() - self._last_request)
            if delay > 0:
                time.sleep(delay)
            self._last_request = time.monotonic()
            req = Request("https://api.yehangshe.com" + path,
                          headers={"Authorization": "Bearer " + self._key,
                                   "Accept": "application/json"})
            try:
                with self._opener(req, timeout=15) as response:
                    remaining = response.headers.get("X-Quota-Remaining")
                    if remaining is not None:
                        self.monthly_remaining = int(remaining)
                    body = json.load(response)
                    if path == "/v1/discover":
                        self.monthly_remaining = body.get("data", {}).get("quota", {}).get("monthly_remaining")
                    return body
            except HTTPError as exc:
                if exc.code == 429:
                    try:
                        body = json.load(exc)
                        exhausted = body.get("error", {}).get("code") == "QUOTA_EXHAUSTED"
                    except (ValueError, AttributeError):
                        exhausted = False
                    if exhausted:
                        raise NightwatchError("monthly quota exhausted") from None
                    if attempt + 1 < max_attempts:
                        time.sleep(max(1.0, min(60.0, float(exc.headers.get("Retry-After", "5")))))
                        continue
                raise NightwatchError(f"Nightwatch HTTP {exc.code}") from None
            except (URLError, TimeoutError):
                raise NightwatchError("Nightwatch network error") from None
        raise NightwatchError("Nightwatch retry limit reached")

    def discover(self) -> dict:
        return self.get("/v1/discover")

    def heatmap_snapshot(self, ticker: str = "SPX") -> dict:
        if ticker != "SPX":
            raise ValueError("this strategy requires SPX")
        return self.get("/v1/derived/heatmap/" + quote(ticker) + "/snapshot")
