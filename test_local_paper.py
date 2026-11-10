import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from discord_notifier import build_message
from local_paper import ShadowBook, ShadowError

ET = ZoneInfo("America/New_York")


class Quotes:
    def __init__(self, now, short_ask=2.4, long_bid=0.9, stale=False):
        self.now = now
        self.short_ask = short_ask
        self.long_bid = long_bid
        self.stale = stale

    def option_quotes(self, symbols):
        stamp = self.now - timedelta(seconds=30) if self.stale else self.now
        return {"data": {"quotes": [
            {"contract_symbol": symbols[0], "ask_usd": self.short_ask, "quoted_at": stamp.isoformat()},
            {"contract_symbol": symbols[1], "bid_usd": self.long_bid, "quoted_at": stamp.isoformat()}],
            "unavailable": []}}


class ShadowTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 11, 10, 45, tzinfo=ET)
        self.obs = {"status": "RESEARCH_CANDIDATE", "new_observation": True,
                    "bucket_at": "2026-11-11T10:40:00-05:00", "invalidation_spot": 5990,
                    "quote": {"short_strike": 5990, "long_strike": 5985,
                              "short_bid": 2, "long_ask": 1, "quoted_at": self.now.isoformat()}}
        self.temp = tempfile.TemporaryDirectory()
        self.book = ShadowBook(Path(self.temp.name) / "shadow.sqlite3")

    def tearDown(self):
        self.book.close()
        self.temp.cleanup()

    def test_open_is_durable_one_contract_and_idempotent(self):
        opened = self.book.open_from_candidate(self.obs, self.now)
        self.assertEqual(opened["credit_points"], 0.9)
        self.assertEqual(opened["max_loss_usd"], 410)
        with self.assertRaises(ShadowError):
            self.book.open_from_candidate(self.obs, self.now)
        self.book.close()
        self.book = ShadowBook(Path(self.temp.name) / "shadow.sqlite3")
        self.assertEqual(self.book.open_position()["id"], self.obs["bucket_at"])

    def test_stale_exit_keeps_position_open(self):
        self.book.open_from_candidate(self.obs, self.now)
        with self.assertRaisesRegex(ShadowError, "stale"):
            self.book.evaluate_open(Quotes(self.now, stale=True), 5980, self.now)
        self.assertIsNotNone(self.book.open_position())

    def test_exit_records_pnl_once_and_daily_loss_blocks_reentry(self):
        self.book.open_from_candidate(self.obs, self.now)
        later = self.now + timedelta(minutes=5)
        result = self.book.evaluate_open(Quotes(later, short_ask=4.9, long_bid=0), 5980, later)
        self.assertEqual(result["status"], "SHADOW_CLOSE")
        self.assertEqual(result["realized_pnl_usd"], -410)
        self.assertEqual(self.book.evaluate_open(Quotes(later), 5980, later)["status"], "NO_SHADOW_POSITION")
        newer = dict(self.obs, bucket_at="2026-11-11T10:45:00-05:00")
        newer["quote"] = dict(self.obs["quote"], quoted_at=later.isoformat())
        with self.assertRaisesRegex(ShadowError, "daily realized loss"):
            self.book.open_from_candidate(newer, later)

    def test_stale_open_rejected_and_discord_label_explicit(self):
        stale = dict(self.obs, quote=dict(self.obs["quote"], quoted_at=(self.now - timedelta(seconds=16)).isoformat()))
        with self.assertRaisesRegex(ShadowError, "stale"):
            self.book.open_from_candidate(stale, self.now)
        msg = build_message({"event_type": "shadow_open", "signal_id": "x",
                             "session_date_et": "2026-11-11"})
        self.assertIn("假设成交", msg)


if __name__ == "__main__":
    unittest.main()
