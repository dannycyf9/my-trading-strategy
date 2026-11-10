"""Timed, read-only SPX research monitor. It never submits an order.

One cycle reads a completed Nightwatch SPX Heatmap bucket and SPX 5m bars.
Snapshots are stored locally so a 30-minute comparison cannot use future data.
The option quote is fetched only after an A-E structure appears. All outputs
remain research observations because Nightwatch has no public paper-order API.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import sqlite3
import sys
import time as wall_time
from datetime import datetime, timedelta, time, timezone
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

from discord_notifier import DeliveryError, deliver
from local_paper import ShadowBook, ShadowError
from nightwatch_client import NightwatchClient, NightwatchError
from spx_vertical import evaluate, ts

ET = ZoneInfo("America/New_York")
STATE = Path("state/market_monitor.sqlite3")


class DataUnavailable(ValueError):
    pass


def assert_discover(response: dict) -> int:
    data = response.get("data", {})
    remaining = data.get("quota", {}).get("monthly_remaining")
    if isinstance(remaining, bool) or not isinstance(remaining, int) or remaining < 10:
        raise DataUnavailable("Nightwatch quota unavailable or below 10 units")
    scope = data.get("user", {}).get("access_scope")
    if not isinstance(scope, list) or "data-api:read" not in scope:
        raise DataUnavailable("Nightwatch data-api:read permission missing")
    return remaining


def _market_data(response: dict) -> object:
    if not isinstance(response, dict) or response.get("_meta", {}).get("stale"):
        raise DataUnavailable("Nightwatch returned stale or invalid data")
    if "data" not in response or response["data"] is None:
        raise DataUnavailable("Nightwatch returned no data")
    return response["data"]


def normalize_bars(response: dict, now: datetime) -> list[dict]:
    """Accept only bars with explicit start_at/end_at; an ambiguous timestamp fails.

    Nightwatch's generic OHLC endpoint has no public response schema. When the
    actual feed uses a different timestamp field, adapt this after inspecting
    a real authenticated response; never guess whether a time marks bar start.
    """
    data = _market_data(response)
    raw = data if isinstance(data, list) else next(
        (data[k] for k in ("bars", "candles") if isinstance(data, dict) and isinstance(data.get(k), list)), None)
    if not isinstance(raw, list):
        raise DataUnavailable("SPX OHLC bar shape is unverified")
    session = now.astimezone(ET).date()
    bars = []
    for item in raw:
        if not isinstance(item, dict):
            raise DataUnavailable("bad OHLC row")
        if "end_at" in item:
            end_at = ts(item["end_at"])
        elif "start_at" in item:
            end_at = ts(item["start_at"]) + timedelta(minutes=5)
        else:
            raise DataUnavailable("OHLC bar lacks explicit start_at/end_at")
        et_end = end_at.astimezone(ET)
        if (et_end.date() != session or end_at > now or
                not time(9, 35) <= et_end.time() <= time(16, 0)):
            continue
        try:
            bar = {k: float(item[k]) for k in ("open", "high", "low", "close")}
        except (KeyError, TypeError, ValueError) as exc:
            raise DataUnavailable("bad OHLC values") from exc
        if not all(math.isfinite(v) and v > 0 for v in bar.values()):
            raise DataUnavailable("nonpositive OHLC values")
        if not bar["low"] <= min(bar["open"], bar["close"]) <= max(bar["open"], bar["close"]) <= bar["high"]:
            raise DataUnavailable("inconsistent OHLC row")
        bar["end_at"] = end_at.isoformat()
        bars.append(bar)
    bars.sort(key=lambda b: b["end_at"])
    if len(bars) < 15 or len({b["end_at"] for b in bars}) != len(bars):
        raise DataUnavailable("missing or duplicate SPX 5m bars")
    if ts(bars[0]["end_at"]).astimezone(ET).time() != time(9, 35):
        raise DataUnavailable("SPX bars do not cover session open")
    for a, b in zip(bars, bars[1:]):
        if ts(b["end_at"]) - ts(a["end_at"]) != timedelta(minutes=5):
            raise DataUnavailable("gap in SPX 5m bars")
    return bars


def snapshot_bucket(response: dict, now: datetime) -> str:
    data = _market_data(response)
    if not isinstance(data, dict) or data.get("ticker") != "SPX":
        raise DataUnavailable("snapshot is not SPX")
    bucket = ts(data["generated_at"])
    if data.get("session_date_et") != now.astimezone(ET).date().isoformat():
        raise DataUnavailable("snapshot session date mismatch")
    if now < bucket + timedelta(minutes=5) or now - bucket > timedelta(minutes=11):
        raise DataUnavailable("snapshot bucket unavailable or stale")
    return bucket.isoformat()


class Store:
    def __init__(self, path: Path = STATE):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS snapshots "
                        "(bucket_at TEXT PRIMARY KEY, session_date_et TEXT NOT NULL, payload TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS observations "
                        "(bucket_at TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT NOT NULL)")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def save_snapshot(self, response: dict, now: datetime) -> str:
        bucket = snapshot_bucket(response, now)
        prior = self.snapshot(bucket)
        if prior and prior.get("data") != response.get("data"):
            raise DataUnavailable("completed Heatmap bucket changed after storage")
        self.db.execute("INSERT OR IGNORE INTO snapshots VALUES (?, ?, ?)",
                        (bucket, now.astimezone(ET).date().isoformat(), json.dumps(response)))
        self.db.commit()
        return bucket

    def snapshot(self, bucket_at: str) -> dict | None:
        row = self.db.execute("SELECT payload FROM snapshots WHERE bucket_at = ?", (bucket_at,)).fetchone()
        return json.loads(row[0]) if row else None

    def record(self, bucket_at: str, output: dict) -> bool:
        cursor = self.db.execute("INSERT OR IGNORE INTO observations VALUES (?, ?, ?)",
                                 (bucket_at, output["status"], json.dumps(output)))
        self.db.commit()
        return cursor.rowcount == 1


def osi_put(expiration: str, strike: float) -> str:
    if strike <= 0 or strike * 1000 != round(strike * 1000):
        raise DataUnavailable("invalid option strike")
    return f"SPXW{expiration[2:4]}{expiration[5:7]}{expiration[8:10]}P{round(strike * 1000):08d}"


def indicative_quote(client: NightwatchClient, bars: list[dict], spot: float,
                     atr: float, session: str, now: datetime) -> dict:
    """Read two exact contracts; never interpret a quote as a fill or order."""
    safe_upper = min(bars[-1]["low"], spot - atr)
    short = math.floor((safe_upper - 0.001) / 5) * 5
    long = short - 5
    symbols = [osi_put(session, short), osi_put(session, long)]
    data = _market_data(client.option_quotes(symbols))
    if not isinstance(data, dict) or not isinstance(data.get("quotes"), list):
        raise DataUnavailable("option quote response shape unverified")
    quotes = {q.get("contract_symbol"): q for q in data["quotes"] if isinstance(q, dict)}
    if set(symbols) != set(quotes) or data.get("unavailable"):
        raise DataUnavailable("required SPXW leg quote unavailable")
    qshort, qlong = quotes[symbols[0]], quotes[symbols[1]]
    for q in (qshort, qlong):
        stamp = ts(q["quoted_at"])
        if not now - timedelta(seconds=15) <= stamp <= now:
            raise DataUnavailable("SPXW quote older than 15 seconds")
    try:
        short_bid, long_ask = float(qshort["bid_usd"]), float(qlong["ask_usd"])
    except (TypeError, KeyError, ValueError) as exc:
        raise DataUnavailable("invalid SPXW bid/ask") from exc
    if not all(math.isfinite(v) and v > 0 for v in (short_bid, long_ask)):
        raise DataUnavailable("invalid SPXW bid/ask")
    credit = short_bid - long_ask
    if not 0 < credit < 5:
        raise DataUnavailable("no positive executable spread credit")
    return {"root": "SPXW", "right": "P", "short_strike": short,
            "long_strike": long, "short_bid": short_bid, "long_ask": long_ask,
            "quoted_at": min(ts(qshort["quoted_at"]), ts(qlong["quoted_at"])).isoformat(),
            "indicative_credit_usd": round(credit * 100, 2),
            "indicative_max_loss_usd": round((5 - credit) * 100, 2)}


def run_once(client: NightwatchClient, store: Store, now: datetime,
             *, allow_candidate_quotes: bool = True) -> dict:
    session = now.astimezone(ET).date().isoformat()
    raw = client.heatmap_snapshot("SPX")
    bucket = store.save_snapshot(raw, now)
    old = store.snapshot((ts(bucket) - timedelta(minutes=30)).isoformat())
    if old is None:
        return {"status": "WAITING", "bucket_at": bucket,
                "spot_usd": float(raw["data"]["spot_usd"]),
                "reason": "need an observed snapshot from 30 minutes earlier"}
    bars = normalize_bars(client.spx_five_minute_bars(), now)
    spot = float(raw["data"]["spot_usd"])
    if ts(bars[-1]["end_at"]) != ts(bucket) + timedelta(minutes=5):
        raise DataUnavailable("SPX bars and Heatmap bucket are not aligned")
    atr = median(b["high"] - b["low"] for b in bars[-14:])
    payload = {"bars": bars, "heatmap_30m_ago": old, "heatmap_now": raw,
               "session_high_usd": max(b["high"] for b in bars),
               "side": "PUT_CREDIT", "sleeve_equity_usd": 5000,
               "day_pnl_usd": 0}
    first = evaluate(payload, now)
    output = {"status": "NO_TRADE", "bucket_at": bucket,
              "spot_usd": spot,
              "structures": list(first.structures), "reasons": list(first.reasons),
              "execution": "DISABLED_ACCOUNT_AND_ORDER_STATE_UNVERIFIED"}
    # The first pass lacks a quote by design. Spend quote units only when that
    # is the sole remaining gate after structure, timing and pullback checks.
    if allow_candidate_quotes and first.structures and first.reasons == ("'quote'",):
        q = indicative_quote(client, bars, spot, atr, session, now)
        payload["quote"] = {k: v for k, v in q.items() if not k.startswith("indicative_")}
        result = evaluate(payload, now)
        output["status"] = "RESEARCH_CANDIDATE" if result.status == "CANDIDATE_PUT_CREDIT" else "NO_TRADE"
        output["reasons"] = list(result.reasons)
        output["quote"] = {"short_strike": q["short_strike"], "long_strike": q["long_strike"],
                           "short_bid": q["short_bid"], "long_ask": q["long_ask"],
                           "quoted_at": q["quoted_at"],
                           "indicative_credit_usd": q["indicative_credit_usd"],
                           "indicative_max_loss_usd": q["indicative_max_loss_usd"]}
        output["invalidation_spot"] = bars[-1]["low"]
    output["new_observation"] = store.record(bucket, output)
    return output


def publish_observation(output: dict, now: datetime) -> str | None:
    if output.get("status") != "RESEARCH_CANDIDATE" or not output.get("new_observation"):
        return None
    q = output["quote"]
    event = {"event_type": "signal", "signal_id": output["bucket_at"],
             "session_date_et": now.astimezone(ET).date().isoformat(),
             "data_at": output["bucket_at"], "direction": "PUT_CREDIT_RESEARCH_ONLY",
             "expiration": now.astimezone(ET).date().isoformat(),
             "short_leg": f"SPXW {q['short_strike']} Put",
             "long_leg": f"SPXW {q['long_strike']} Put",
             "quantity": 1, "credit_usd": q["indicative_credit_usd"],
             "max_loss_usd": q["indicative_max_loss_usd"],
             "reason": "研究候选；模拟账户持仓和日盈亏未核对；未下单"}
    return deliver(event)


def next_tick(now: datetime) -> datetime:
    et = now.astimezone(ET)
    start = et.replace(hour=9, minute=35, second=10, microsecond=0)
    if et < start:
        return start
    elapsed = (et - start).total_seconds()
    return start + timedelta(minutes=5 * (math.floor(elapsed / 300) + 1))


def main() -> int:
    parser = argparse.ArgumentParser(description="SPX research monitor with optional local shadow ledger; never submits orders")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--once", action="store_true", help="read one completed bucket")
    group.add_argument("--watch", action="store_true", help="poll every completed 5m bucket until 13:00 ET")
    parser.add_argument("--discord", action="store_true", help="send research candidates to Discord")
    parser.add_argument("--shadow", action="store_true", help="record hypothetical local spread opens and closes")
    args = parser.parse_args()
    lock = Path("state/market_monitor.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("another market monitor is already running", file=sys.stderr)
            return 1
        try:
            client = NightwatchClient()
            remaining = assert_discover(client.discover())
            print(json.dumps({"status": "READY_LOCAL_SHADOW" if args.shadow else "READY_READ_ONLY",
                              "quota_monthly_remaining": remaining}))
            store = Store()
            book = ShadowBook() if args.shadow else None
            try:
                while True:
                    now = datetime.now(timezone.utc)
                    et = now.astimezone(ET)
                    if args.watch and et.time() >= time(13, 0, 10):
                        break
                    if args.watch:
                        due = next_tick(now)
                        if due.time() > time(13, 0, 10):
                            break
                        wall_time.sleep(max(0, (due - et).total_seconds()))
                        now = datetime.now(timezone.utc)
                    try:
                        had_open = bool(book and book.open_position())
                        result = run_once(client, store, now, allow_candidate_quotes=not had_open)
                    except (NightwatchError, DataUnavailable, KeyError, TypeError, ValueError) as exc:
                        result = {"status": "DATA_FAILURE", "reason": str(exc),
                                  "action": "NO_NEW_TRADE; URGENT_REVIEW_ANY_OPEN_SHADOW_POSITION"}
                    print(json.dumps(result, ensure_ascii=False), flush=True)
                    shadow_result = None
                    if book:
                        try:
                            if result["status"] == "DATA_FAILURE":
                                if book.open_position():
                                    shadow_result = {"status": "SHADOW_URGENT_REVIEW",
                                                     "reason": "market data unavailable; local position remains open"}
                            elif had_open:
                                shadow_result = book.evaluate_open(client, float(result["spot_usd"]), now)
                            elif result["status"] == "RESEARCH_CANDIDATE":
                                shadow_result = book.open_from_candidate(result, now)
                        except (NightwatchError, ShadowError, KeyError, TypeError, ValueError) as exc:
                            shadow_result = {"status": "SHADOW_URGENT_REVIEW" if book.open_position() else "SHADOW_REJECTED",
                                             "reason": str(exc)}
                        if shadow_result:
                            print(json.dumps(shadow_result, ensure_ascii=False), flush=True)
                    if args.discord:
                        try:
                            delivery = publish_observation(result, now)
                            if delivery:
                                print(json.dumps({"discord": delivery}), flush=True)
                            if shadow_result and shadow_result["status"] in ("SHADOW_OPEN", "SHADOW_CLOSE", "SHADOW_URGENT_REVIEW"):
                                event = {"event_type": "shadow_open" if shadow_result["status"] == "SHADOW_OPEN" else
                                         "shadow_close" if shadow_result["status"] == "SHADOW_CLOSE" else "fault",
                                         "signal_id": shadow_result.get("simulation_id", "shadow-data-failure-" + str(result.get("bucket_at", now.isoformat()))),
                                         "session_date_et": now.astimezone(ET).date().isoformat(),
                                         "reason": shadow_result.get("reason", shadow_result.get("note", "local hypothetical fill")),
                                         "credit_usd": shadow_result.get("credit_points"),
                                         "debit_usd": shadow_result.get("debit_points"),
                                         "max_loss_usd": shadow_result.get("max_loss_usd"),
                                         "realized_pnl_usd": shadow_result.get("realized_pnl_usd")}
                                print(json.dumps({"discord": deliver(event)}), flush=True)
                        except (DeliveryError, ValueError) as exc:
                            print(json.dumps({"discord": "FAILED", "reason": str(exc)}), file=sys.stderr)
                    if args.once:
                        return 0 if result["status"] != "DATA_FAILURE" else 2
            finally:
                store.close()
                if book:
                    book.close()
        except (NightwatchError, DataUnavailable) as exc:
            print(json.dumps({"status": "STARTUP_FAILURE", "reason": str(exc)}), file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
