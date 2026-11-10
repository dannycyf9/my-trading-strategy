import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from market_monitor import (DataUnavailable, Store, assert_discover,
                            normalize_bars, osi_put, run_once)
from test_strategy import ET, fixture


class FakeClient:
    def __init__(self, snapshots, bars):
        self.snapshots = iter(snapshots)
        self.bars = bars
        self.quote_calls = 0

    def heatmap_snapshot(self, ticker):
        assert ticker == "SPX"
        return next(self.snapshots)

    def spx_five_minute_bars(self):
        return {"data": {"bars": self.bars}, "_meta": {"stale": False}}

    def option_quotes(self, symbols):
        self.quote_calls += 1
        return {"data": {"quotes": [
            {"contract_symbol": symbols[0], "bid_usd": 2, "ask_usd": 2.1,
             "quoted_at": self.now.isoformat()},
            {"contract_symbol": symbols[1], "bid_usd": 0.9, "ask_usd": 1,
             "quoted_at": self.now.isoformat()}], "unavailable": []},
            "_meta": {"stale": False}}


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 11, 10, 45, tzinfo=ET)
        self.example = fixture(self.now)

    def test_quota_must_be_verified(self):
        self.assertEqual(assert_discover({"data": {"quota": {"monthly_remaining": 100},
                                                  "user": {"access_scope": ["data-api:read"]}}}), 100)
        with self.assertRaises(DataUnavailable):
            assert_discover({"data": {"quota": {"monthly_remaining": 0}}})

    def test_spx_bars_require_explicit_time_and_full_session(self):
        bars = self.example["bars"]
        self.assertEqual(len(normalize_bars({"data": {"bars": bars}}, self.now)), 15)
        bad = [dict(b) for b in bars]
        bad[0]["timestamp"] = bad[0].pop("end_at")
        with self.assertRaisesRegex(DataUnavailable, "explicit start_at/end_at"):
            normalize_bars({"data": {"bars": bad}}, self.now)

    def test_osi_spxw_put(self):
        self.assertEqual(osi_put("2026-11-11", 5990), "SPXW261111P05990000")

    def test_monitor_waits_for_old_snapshot_then_emits_research_only(self):
        old = self.example["heatmap_30m_ago"]
        new = self.example["heatmap_now"]
        client = FakeClient([old, new], self.example["bars"])
        client.now = self.now
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "monitor.sqlite3")
            try:
                self.assertEqual(run_once(client, store, self.now - timedelta(minutes=30))["status"], "WAITING")
                result = run_once(client, store, self.now)
            finally:
                store.close()
        self.assertEqual(result["status"], "RESEARCH_CANDIDATE")
        self.assertEqual(result["execution"], "DISABLED_ACCOUNT_AND_ORDER_STATE_UNVERIFIED")
        self.assertTrue(result["new_observation"])
        self.assertEqual(client.quote_calls, 1)

    def test_no_pullback_does_not_spend_quote_units(self):
        example = fixture(self.now)
        example["bars"][-1].update(close=5999, low=5995)
        client = FakeClient([example["heatmap_now"]], example["bars"])
        client.now = self.now
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "monitor.sqlite3")
            store.save_snapshot(example["heatmap_30m_ago"], self.now - timedelta(minutes=30))
            try:
                result = run_once(client, store, self.now)
            finally:
                store.close()
        self.assertEqual(result["status"], "NO_TRADE")
        self.assertEqual(client.quote_calls, 0)


if __name__ == "__main__":
    unittest.main()
