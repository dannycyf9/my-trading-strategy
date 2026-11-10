"""Read-only Nightwatch REST client; environment-based credentials only."""

import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
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
                    body = json.load(response)
                    if getattr(response, "status", 200) == 202:
                        if attempt + 1 < max_attempts:
                            wait = body.get("_meta", {}).get("retry_after_seconds", 5)
                            time.sleep(max(1.0, min(30.0, float(wait))))
                            continue
                        raise NightwatchError("Nightwatch data still materializing")
                    remaining = response.headers.get("X-Quota-Remaining")
                    if remaining is not None:
                        self.monthly_remaining = int(remaining)
                    if path == "/v1/discover":
                        self.monthly_remaining = body.get("data", {}).get("quota", {}).get("monthly_remaining")
                    return body
            except HTTPError as exc:
                if exc.code == 503:
                    try:
                        body = json.load(exc)
                        wait = body.get("error", {}).get("retry_after_seconds", 5)
                    except (ValueError, AttributeError):
                        wait = 5
                    if attempt + 1 < max_attempts:
                        time.sleep(max(1.0, min(30.0, float(wait))))
                        continue
                    raise NightwatchError("Nightwatch read model unavailable") from None
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

    def spx_five_minute_bars(self) -> dict:
        return self.get("/v1/stocks/ohlc/SPX?candle_size=5m")

    def option_quotes(self, contracts: list[str]) -> dict:
        if not contracts or len(contracts) > 50:
            raise ValueError("request 1-50 option contracts")
        if any(not c.startswith("SPXW") or not c.isalnum() for c in contracts):
            raise ValueError("only SPXW option contracts are accepted")
        return self.get("/v1/options/quotes?" + urlencode({"contracts": ",".join(contracts)}))
