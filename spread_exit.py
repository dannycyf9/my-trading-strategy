"""Pure exit decision for one SPXW credit spread, using executable net debit."""

from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class SpreadPosition:
    credit_usd: float  # per share, actual opening fill
    width_points: float
    quantity: int
    side: str  # PUT_CREDIT or CALL_CREDIT
    invalidation_spot: float


@dataclass(frozen=True)
class ExitDecision:
    close: bool
    reason: str
    estimated_pnl_usd: float | None


def evaluate_exit(pos: SpreadPosition, *, short_ask: float | None,
                  long_bid: float | None, spot: float, quote_age_seconds: float,
                  now: datetime, data_healthy: bool = True) -> ExitDecision:
    if pos.side not in ("PUT_CREDIT", "CALL_CREDIT") or pos.quantity < 1:
        raise ValueError("invalid position")
    if pos.credit_usd <= 0 or pos.credit_usd >= pos.width_points:
        raise ValueError("invalid opening credit")
    et_time = now.astimezone(ET).time()
    expired = et_time >= time(13, 0)
    crossed = spot <= pos.invalidation_spot if pos.side == "PUT_CREDIT" else spot >= pos.invalidation_spot
    if not data_healthy or quote_age_seconds > 15 or short_ask is None or long_bid is None:
        # A broker order is never inferred from stale data; operator must resolve a live position.
        return ExitDecision(True, "URGENT_MANUAL_REVIEW_DATA_FAILURE", None)
    if short_ask < 0 or long_bid < 0:
        return ExitDecision(True, "URGENT_MANUAL_REVIEW_BAD_QUOTE", None)
    debit = max(0.0, short_ask - long_bid)
    pnl = round((pos.credit_usd - debit) * 100 * pos.quantity, 2)
    if expired:
        return ExitDecision(True, "TIME_EXIT_13_ET", pnl)
    if crossed:
        return ExitDecision(True, "SPOT_INVALIDATION", pnl)
    if debit >= pos.credit_usd + 1.25:
        return ExitDecision(True, "LOSS_LIMIT_125_PER_CONTRACT", pnl)
    if debit <= pos.credit_usd * 0.5:
        return ExitDecision(True, "TAKE_PROFIT_50_PERCENT_CREDIT", pnl)
    return ExitDecision(False, "HOLD", pnl)
