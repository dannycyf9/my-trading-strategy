import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from spx_vertical import evaluate
from spread_exit import SpreadPosition, evaluate_exit

ET = ZoneInfo("America/New_York")


def fixture(now):
    bars = []
    for i in range(15):
        end = now - timedelta(minutes=5 * (14 - i))
        bars.append({"end_at": end.isoformat(), "open": 6000, "high": 6005,
                     "low": 5995, "close": 6001})
    bars[-2].update(open=5999, close=6004)
    bars[-1].update(open=6002, close=6004, low=6000)

    def snap(stamp, values):
        return {"data": {"ticker": "SPX", "generated_at": stamp.isoformat(),
                         "session_date_et": now.date().isoformat(), "spot_usd": 6004,
                         "expirations": [now.date().isoformat()],
                         "cells": [{"expiration": now.date().isoformat(), "strike_usd": k,
                                    "net_dealer_gex_usd": v * 1000000} for k, v in values.items()]}}

    return {"bars": bars,
            "heatmap_30m_ago": snap(now - timedelta(minutes=35), {6015: 15, 5990: 45, 6025: -20, 5980: -20}),
            "heatmap_now": snap(now - timedelta(minutes=5), {6015: 40, 5990: 20, 6025: -20, 5980: -20}),
            "side": "PUT_CREDIT", "sleeve_equity_usd": 5000, "session_high_usd": 6005,
            "quote": {"root": "SPXW", "right": "P", "short_strike": 5980,
                      "long_strike": 5975, "short_bid": 2.5, "long_ask": 1.5,
                      "quoted_at": now.isoformat()}}


class StrategyTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 11, 10, 30, tzinfo=ET)

    def test_bullish_structure_does_not_authorize_call_credit(self):
        p = fixture(self.now)
        p["side"] = "CALL_CREDIT"
        out = evaluate(p, self.now)
        self.assertIn("A_gamma_forward_breakout", out.structures)
        self.assertEqual(out.status, "NO_TRADE")

    def test_missing_visible_node_fails_closed(self):
        p = fixture(self.now)
        p["heatmap_now"]["data"]["cells"].pop()
        self.assertEqual(evaluate(p, self.now).status, "NO_TRADE")

    def test_stale_data_fails_closed(self):
        p = fixture(self.now)
        p["heatmap_now"]["data"]["generated_at"] = (self.now - timedelta(minutes=15)).isoformat()
        self.assertEqual(evaluate(p, self.now).status, "NO_TRADE")

    def test_quote_and_short_strike_guard(self):
        p = fixture(self.now)
        p["quote"]["long_strike"] = 5985
        self.assertEqual(evaluate(p, self.now).status, "NO_TRADE")

    def test_valid_candidate_is_one_contract(self):
        p = fixture(self.now)
        out = evaluate(p, self.now)
        self.assertEqual(out.status, "CANDIDATE_PUT_CREDIT")
        self.assertEqual(out.max_contracts, 1)

    def test_exit_loss_and_time(self):
        pos = SpreadPosition(1, 5, 1, "PUT_CREDIT", 5980)
        x = evaluate_exit(pos, short_ask=3, long_bid=0.5, spot=6000,
                          quote_age_seconds=2, now=self.now)
        self.assertEqual(x.reason, "LOSS_LIMIT_125_PER_CONTRACT")
        x = evaluate_exit(pos, short_ask=0.7, long_bid=0.3, spot=6000,
                          quote_age_seconds=2, now=self.now.replace(hour=13))
        self.assertEqual(x.reason, "TIME_EXIT_13_ET")


if __name__ == "__main__":
    unittest.main()
