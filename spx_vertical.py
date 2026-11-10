"""SPX 0DTE vertical research engine. No broker orders are sent by this module.

Input is a JSON object containing completed five-minute bars, two Nightwatch
Heatmap snapshots, and optionally a two-leg executable quote. All timestamps
must be ISO-8601 with timezone offsets. This is deliberately fail-closed.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, time
from statistics import median
from math import isfinite
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


class InvalidData(ValueError):
    pass


def ts(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise InvalidData("timezone required")
    return result


def f(value: object) -> float:
    if isinstance(value, bool) or value is None:
        raise InvalidData("missing numeric field")
    result = float(value)
    if not 0 < result < float("inf"):
        raise InvalidData("invalid positive number")
    return result


@dataclass(frozen=True)
class Verdict:
    status: str
    structures: tuple[str, ...]
    reasons: tuple[str, ...]
    atr_points: float | None = None
    upper_positive_change_pp: float | None = None
    lower_positive_change_pp: float | None = None
    upper_negative_change_pp: float | None = None
    near_net_change_pp: float | None = None
    max_contracts: int = 0
    max_loss_usd: float | None = None


def _snapshot(obj: dict, now: datetime, max_age_minutes: int = 11) -> tuple[datetime, dict[float, float], float, str]:
    data = obj.get("data", obj)
    if data.get("ticker") not in ("SPX", "SPXW"):
        raise InvalidData("SPX Heatmap snapshot required; SPY proxy is not accepted")
    stamp = ts(data["generated_at"])
    session = now.astimezone(ET).date().isoformat()
    if data.get("session_date_et") != session:
        raise InvalidData("snapshot is from another session")
    # Nightwatch labels a completed 5m bucket with its beginning, not availability.
    if now < stamp + timedelta(minutes=5) or now - stamp > timedelta(minutes=max_age_minutes):
        raise InvalidData("snapshot is unavailable or stale")
    expiry = session
    if expiry not in data.get("expirations", []):
        raise InvalidData("no confirmed 0DTE Heatmap expiration")
    cells: dict[float, float] = {}
    for cell in data.get("cells", []):
        if cell.get("expiration") == expiry:
            strike = f(cell["strike_usd"])
            gex = float(cell["net_dealer_gex_usd"])
            if strike in cells or gex == 0 or not isfinite(gex):
                raise InvalidData("duplicate or invalid Heatmap cell")
            cells[strike] = gex
    if len(cells) < 4:
        raise InvalidData("insufficient visible 0DTE nodes")
    return stamp, cells, f(data["spot_usd"]), expiry


def _bars(raw: list[dict], now: datetime) -> tuple[list[dict], float]:
    if len(raw) < 15:
        raise InvalidData("need 15 completed five-minute bars")
    bars = raw[-15:]
    stamps = [ts(b["end_at"]) for b in bars]
    for i in range(1, len(stamps)):
        if stamps[i] - stamps[i - 1] != timedelta(minutes=5):
            raise InvalidData("bars must be consecutive completed 5m candles")
    if stamps[-1] > now or now - stamps[-1] > timedelta(minutes=7):
        raise InvalidData("price bar is not current")
    for b in bars:
        high, low, close, opening = f(b["high"]), f(b["low"]), f(b["close"]), f(b["open"])
        if not low <= close <= high or not low <= opening <= high:
            raise InvalidData("bad OHLC")
    # User's source defines ATR as median 5m candle range, not Wilder ATR.
    atr = median(f(b["high"]) - f(b["low"]) for b in bars[-14:])
    if atr <= 0:
        raise InvalidData("zero 5m range")
    return bars, atr


def _shares(cells: dict[float, float], spot: float, atr: float) -> dict[str, float]:
    total = sum(abs(v) for v in cells.values())
    if total <= 0:
        raise InvalidData("zero observable GEX")
    return {
        "up_pos": 100 * sum(v for k, v in cells.items() if k > spot and v > 0) / total,
        "down_pos": 100 * sum(v for k, v in cells.items() if k < spot and v > 0) / total,
        "up_neg": 100 * sum(-v for k, v in cells.items() if k > spot and v < 0) / total,
        "near_net": 100 * sum(v for k, v in cells.items() if abs(k - spot) <= 3 * atr) / total,
    }


def _quote_plan(q: dict, equity: float, day_pnl: float, now: datetime) -> tuple[int, float]:
    if q.get("right") != "P" or q.get("root") != "SPXW":
        raise InvalidData("only SPXW put credit spreads are eligible")
    width = f(q["short_strike"]) - f(q["long_strike"])
    if width not in (5.0, 10.0):
        raise InvalidData("spread width must be 5 or 10")
    credit = f(q["short_bid"]) - f(q["long_ask"])
    if credit <= 0 or credit >= width:
        raise InvalidData("no valid executable credit")
    if not now - timedelta(seconds=15) <= ts(q["quoted_at"]) <= now:
        raise InvalidData("spread quote older than 15 seconds")
    if day_pnl <= -250:
        raise InvalidData("daily loss gate reached")
    max_loss = 100 * (width - credit)
    # Initial rollout: one position and one contract; leave $1,000 untouched.
    sleeve = min(5000.0, equity)
    count = min(1, int(min(500.0, sleeve - 1000.0) // max_loss))
    if count < 1:
        raise InvalidData("insufficient risk sleeve")
    return count, max_loss * count


def evaluate(payload: dict, now: datetime | None = None) -> Verdict:
    now = now or datetime.now(tz=ET)
    try:
        bars, atr = _bars(payload["bars"], now)
        old_at, old, _, _ = _snapshot(payload["heatmap_30m_ago"], now, 41)
        new_at, new, spot, _ = _snapshot(payload["heatmap_now"], now)
        if new_at - old_at != timedelta(minutes=30):
            raise InvalidData("snapshots must be exactly 30 minutes apart")
        if ts(bars[-1]["end_at"]) != new_at + timedelta(minutes=5):
            raise InvalidData("price bar and Heatmap bucket are not aligned")
        if abs(f(bars[-1]["close"]) - spot) > atr:
            raise InvalidData("Heatmap spot and price bar disagree")
        # The old point can be 30m stale by design; verify its causal availability.
    except (KeyError, TypeError, ValueError) as exc:
        return Verdict("NO_TRADE", (), (str(exc),))
    try:
        return _evaluate_valid(payload, now, bars, atr, old, new, spot)
    except (KeyError, TypeError, ValueError) as exc:
        return Verdict("NO_TRADE", (), (str(exc),))


def _evaluate_valid(payload: dict, now: datetime, bars: list[dict], atr: float,
                    old: dict[float, float], new: dict[float, float], spot: float) -> Verdict:
    reasons: list[str] = []
    # A share shift is evaluated only on common nodes. Missing Top-10 rows are unknown.
    common = old.keys() & new.keys()
    old_cov = sum(abs(old[k]) for k in common) / sum(abs(v) for v in old.values())
    new_cov = sum(abs(new[k]) for k in common) / sum(abs(v) for v in new.values())
    if min(old_cov, new_cov) < 0.8 or len(common) < 4:
        return Verdict("NO_TRADE", (), ("Top-node coverage below 80%; change is not measurable",), atr)
    o, n = _shares({k: old[k] for k in common}, spot, atr), _shares({k: new[k] for k in common}, spot, atr)
    up_pos = n["up_pos"] - o["up_pos"]
    down_pos = n["down_pos"] - o["down_pos"]
    up_neg = n["up_neg"] - o["up_neg"]
    near_net = n["near_net"] - o["near_net"]
    high = f(payload["session_high_usd"])
    if high < max(f(b["high"]) for b in bars):
        return Verdict("NO_TRADE", (), ("session high below observed bar high",), atr)
    near_high = 0 <= high - spot < 0.5 * atr
    king = max(new, key=lambda k: abs(new[k]))
    strong_target = any(2 * atr <= k - spot <= 10 * atr and v > 0 and
                        100 * v / sum(abs(x) for x in new.values()) >= 20
                        for k, v in new.items())
    neg_accel = any(0 < k - spot <= 4 * atr and v < 0 and
                    100 * v / sum(abs(x) for x in new.values()) <= -10
                    for k, v in new.items())
    structures: list[str] = []
    if near_high and up_pos > 5 and down_pos < -5:
        structures.append("A_gamma_forward_breakout")
    if near_high and strong_target and neg_accel:
        structures.append("B_target_and_negative_acceleration")
    if near_high and new[king] > 0 and king - spot > atr and up_neg > 5:
        structures.append("C_positive_king_negative_expansion")
    if spot - f(bars[-7]["close"]) > 1.5 * atr and new[king] < 0 and king - spot > atr and near_net < -10:
        structures.append("D_negative_gamma_momentum")
    clock = now.astimezone(ET).time()
    positive = [(k, v) for k, v in new.items() if v > 0]
    old_positive = [(k, v) for k, v in old.items() if v > 0]
    if positive and old_positive:
        centroid = sum(k * v for k, v in positive) / sum(v for _, v in positive)
        old_centroid = sum(k * v for k, v in old_positive) / sum(v for _, v in old_positive)
        if time(9, 35) <= clock <= time(10, 30) and centroid - spot > 2 * atr and centroid - old_centroid > 0.5 * atr:
            structures.append("E_opening_centroid_forward")
    if not structures:
        reasons.append("none of SPX structures A-E passed")
    if clock < time(10, 0) or clock > time(11, 30):
        reasons.append("entry window is 10:00-11:30 ET")
    if payload.get("side") != "PUT_CREDIT":
        reasons.append("bullish A-E permits only evaluated put credit candidates")
    # A-E describe bullish movement, not a standalone permission to sell puts.
    # Require an observed reclaim and a completed pullback above that reclaim.
    b2, b1 = bars[-2], bars[-1]
    pullback = f(b2["close"]) > f(b2["open"]) and f(b1["low"]) <= f(b2["high"]) and f(b1["close"]) >= f(b2["close"])
    if not pullback:
        reasons.append("no completed pullback confirmation")
    plan = None
    if not reasons:
        try:
            q = payload["quote"]
            if f(q["short_strike"]) >= min(f(b1["low"]), spot - atr):
                raise InvalidData("short put is not below pullback low and one ATR")
            plan = _quote_plan(q, f(payload["sleeve_equity_usd"]), float(payload.get("day_pnl_usd", 0)), now)
        except (KeyError, TypeError, ValueError) as exc:
            reasons.append(str(exc))
    status = "CANDIDATE_PUT_CREDIT" if plan and structures else "NO_TRADE"
    return Verdict(status, tuple(structures), tuple(reasons), atr, up_pos, down_pos, up_neg,
                   near_net, plan[0] if plan else 0, plan[1] if plan else None)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fail-closed SPX vertical signal evaluator")
    parser.add_argument("input_json", help="fixture of completed bars, snapshots, quote, sleeve")
    parser.add_argument("--as-of", help="ISO-8601 evaluation time for offline replay only")
    args = parser.parse_args()
    with open(args.input_json, encoding="utf-8") as handle:
        payload = json.load(handle)
    print(json.dumps(asdict(evaluate(payload, ts(args.as_of) if args.as_of else None)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
