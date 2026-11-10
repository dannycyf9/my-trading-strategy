"""Durable local shadow ledger. Records hypothetical fills; never calls a broker."""

from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from spread_exit import SpreadPosition, evaluate_exit
from spx_vertical import ts

STATE = Path("state/shadow_book.sqlite3")
SLIPPAGE_POINTS = 0.10  # conservative net spread price adjustment on each side


class ShadowError(ValueError):
    pass


class ShadowBook:
    def __init__(self, path: Path = STATE):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout = 5000")
        self.db.execute("""CREATE TABLE IF NOT EXISTS positions (
            id TEXT PRIMARY KEY, session_date_et TEXT NOT NULL, status TEXT NOT NULL,
            short_symbol TEXT NOT NULL, long_symbol TEXT NOT NULL,
            short_strike REAL NOT NULL, long_strike REAL NOT NULL,
            credit_points REAL NOT NULL, width_points REAL NOT NULL,
            invalidation_spot REAL NOT NULL, opened_at TEXT NOT NULL,
            open_quote_at TEXT NOT NULL, closed_at TEXT, close_quote_at TEXT,
            debit_points REAL, realized_pnl_usd REAL, close_reason TEXT)""")
        self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_open_shadow ON positions(status) WHERE status = 'OPEN'")
        self.db.commit()

    def close(self):
        self.db.close()

    def open_position(self) -> dict | None:
        row = self.db.execute("SELECT * FROM positions WHERE status = 'OPEN'").fetchone()
        return dict(row) if row else None

    def day_pnl(self, session: str) -> float:
        row = self.db.execute("SELECT COALESCE(SUM(realized_pnl_usd), 0) FROM positions "
                              "WHERE session_date_et = ? AND status = 'CLOSED'", (session,)).fetchone()
        return round(float(row[0]), 2)

    def open_from_candidate(self, observation: dict, now: datetime) -> dict:
        if observation.get("status") != "RESEARCH_CANDIDATE" or not observation.get("new_observation"):
            raise ShadowError("fresh research candidate required")
        q = observation["quote"]
        session = now.astimezone(ZoneInfo("America/New_York")).date().isoformat()
        stamp = ts(q["quoted_at"])
        if not now - timedelta(seconds=15) <= stamp <= now:
            raise ShadowError("opening quote stale")
        width = float(q["short_strike"]) - float(q["long_strike"])
        credit = round(float(q["short_bid"]) - float(q["long_ask"]) - SLIPPAGE_POINTS, 4)
        if width != 5 or not (0 < credit < width) or not math.isfinite(credit):
            raise ShadowError("shadow spread price invalid")
        max_loss = round((width - credit) * 100, 2)
        if max_loss > 500 or max_loss > 5000 - 1000:
            raise ShadowError("shadow risk sleeve exceeded")
        signal_id = str(observation["bucket_at"])
        short_symbol = _osi_put(session, float(q["short_strike"]))
        long_symbol = _osi_put(session, float(q["long_strike"]))
        try:
            self.db.execute("BEGIN IMMEDIATE")
            if self.db.execute("SELECT 1 FROM positions WHERE status = 'OPEN'").fetchone():
                raise ShadowError("one shadow position already open")
            if self.db.execute("SELECT 1 FROM positions WHERE id = ?", (signal_id,)).fetchone():
                raise ShadowError("signal already simulated")
            if self.day_pnl(session) <= -250:
                raise ShadowError("daily realized loss gate reached")
            self.db.execute("""INSERT INTO positions
                (id,session_date_et,status,short_symbol,long_symbol,short_strike,long_strike,
                 credit_points,width_points,invalidation_spot,opened_at,open_quote_at)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (signal_id, session, "OPEN", short_symbol, long_symbol,
                 float(q["short_strike"]), float(q["long_strike"]), credit, width,
                 float(observation["invalidation_spot"]), now.isoformat(), stamp.isoformat()))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return {"status": "SHADOW_OPEN", "simulation_id": signal_id,
                "credit_points": credit, "max_loss_usd": max_loss,
                "short_symbol": short_symbol, "long_symbol": long_symbol,
                "note": "local hypothetical fill; no Nightwatch or broker order"}

    def evaluate_open(self, client, spot: float, now: datetime) -> dict:
        position = self.open_position()
        if position is None:
            return {"status": "NO_SHADOW_POSITION"}
        symbols = [position["short_symbol"], position["long_symbol"]]
        response = client.option_quotes(symbols)
        if not isinstance(response, dict) or response.get("_meta", {}).get("stale"):
            raise ShadowError("closing quote response stale")
        data = response.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("quotes"), list) or data.get("unavailable"):
            raise ShadowError("closing quote unavailable")
        quotes = {q.get("contract_symbol"): q for q in data["quotes"] if isinstance(q, dict)}
        if set(symbols) != set(quotes):
            raise ShadowError("closing leg quote missing")
        stamps = [ts(quotes[s]["quoted_at"]) for s in symbols]
        if any(not now - timedelta(seconds=15) <= stamp <= now for stamp in stamps):
            raise ShadowError("closing leg quote stale")
        try:
            short_ask = float(quotes[symbols[0]]["ask_usd"])
            long_bid = float(quotes[symbols[1]]["bid_usd"])
        except (KeyError, ValueError, TypeError) as exc:
            raise ShadowError("closing bid/ask invalid") from exc
        if not all(math.isfinite(v) and v >= 0 for v in (short_ask, long_bid, spot)):
            raise ShadowError("closing bid/ask or spot invalid")
        debit = round(max(0, short_ask - long_bid) + SLIPPAGE_POINTS, 4)
        spread = SpreadPosition(credit_usd=position["credit_points"],
                                width_points=position["width_points"], quantity=1,
                                side="PUT_CREDIT", invalidation_spot=position["invalidation_spot"])
        decision = evaluate_exit(spread, short_ask=debit, long_bid=0, spot=spot,
                                 quote_age_seconds=(now - min(stamps)).total_seconds(), now=now)
        if not decision.close:
            return {"status": "SHADOW_HOLD", "simulation_id": position["id"],
                    "estimated_pnl_usd": decision.estimated_pnl_usd}
        if decision.estimated_pnl_usd is None:
            raise ShadowError(decision.reason)
        try:
            self.db.execute("BEGIN IMMEDIATE")
            updated = self.db.execute("""UPDATE positions SET status='CLOSED', closed_at=?,
                close_quote_at=?, debit_points=?, realized_pnl_usd=?, close_reason=?
                WHERE id=? AND status='OPEN'""",
                (now.isoformat(), min(stamps).isoformat(), debit,
                 decision.estimated_pnl_usd, decision.reason, position["id"]))
            if updated.rowcount != 1:
                raise ShadowError("shadow position changed during close")
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return {"status": "SHADOW_CLOSE", "simulation_id": position["id"],
                "debit_points": debit, "realized_pnl_usd": decision.estimated_pnl_usd,
                "reason": decision.reason,
                "note": "local hypothetical fill; no Nightwatch or broker order"}


def _osi_put(session: str, strike: float) -> str:
    if strike * 1000 != round(strike * 1000):
        raise ShadowError("invalid strike")
    return f"SPXW{session[2:4]}{session[5:7]}{session[8:10]}P{round(strike * 1000):08d}"
