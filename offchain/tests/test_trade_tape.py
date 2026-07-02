"""Trade-tape tests — the per-market recent-trades history + volume metrics.

The tape records a clearing print whenever a market's on-chain CUMULATIVE volume grows
(`server.record_trade`), and `server.api_trades` returns the recent prints + metrics.
Both read chain state via `server.mstate`; here we monkeypatch that so the logic is
tested with no node. `server.py`'s startup is `__main__`-guarded, so importing it is safe.
"""
import os
import sys
import time
import unittest
from collections import deque

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import server  # noqa: E402

S = server.SCALE


def fake_mstate(vals):
    # vals maps (prefix_bytes, market) -> atomic value; default 0
    return lambda prefix, m: vals.get((prefix, m), 0)


class TradeTape(unittest.TestCase):
    def setUp(self):
        server.TRADES_FILE = "/tmp/jamswap_test_trades.json"   # throwaway; don't touch the real tape
        server.trades.clear()
        server._last_cv.clear()

    def test_first_sight_seeds_without_emitting(self):
        # a reused node may already have cumulative volume — the first observation must
        # seed the baseline, NOT dump prior volume as one giant trade.
        server.mstate = fake_mstate({(b"cv", 1): 5 * S, (b"lp", 1): 100 * S})
        server.record_trade(1)
        self.assertEqual(server.api_trades({"market": "1"})["metrics"]["trades"], 0)

    def test_records_delta_price_and_volume(self):
        vals = {(b"cv", 1): 5 * S, (b"lp", 1): 100 * S}
        server.mstate = fake_mstate(vals)
        server.record_trade(1)              # seed at cv=5
        vals[(b"cv", 1)] = 8 * S            # +3 volume settled
        server.record_trade(1)
        r = server.api_trades({"market": "1"})
        self.assertEqual(r["metrics"]["trades"], 1)
        self.assertEqual(r["trades"][0]["price"], 100)
        self.assertEqual(r["trades"][0]["volume"], 3)

    def test_direction_and_metrics(self):
        vals = {(b"cv", 1): 0, (b"lp", 1): 0}
        server.mstate = fake_mstate(vals)
        server.record_trade(1)              # seed cv=0
        for cv, lp in [(2, 100), (3, 105), (7, 95)]:   # +2@100, +1@105 (up), +4@95 (down)
            vals[(b"cv", 1)] = cv * S
            vals[(b"lp", 1)] = lp * S
            server.record_trade(1)
        r = server.api_trades({"market": "1"})
        m = r["metrics"]
        self.assertEqual(m["trades"], 3)
        self.assertEqual(m["high"], 105)
        self.assertEqual(m["low"], 95)
        self.assertEqual(m["volume"], 7)    # 2 + 1 + 4 base traded
        self.assertEqual(m["last"], 95)
        # most-recent-first ordering + tick direction
        self.assertEqual(r["trades"][0]["price"], 95)
        self.assertEqual(r["trades"][0]["dir"], "down")
        self.assertEqual(r["trades"][1]["dir"], "up")

    def test_flat_round_records_nothing(self):
        vals = {(b"cv", 1): 5 * S, (b"lp", 1): 100 * S}
        server.mstate = fake_mstate(vals)
        server.record_trade(1)              # seed
        server.record_trade(1)              # cv unchanged -> no trade
        self.assertEqual(server.api_trades({"market": "1"})["metrics"]["trades"], 0)

    def test_prunes_prints_older_than_24h(self):
        now = 1_000_000.0
        server.trades[1] = deque([
            {"ts": now - server.TRADE_TTL - 10, "price": 1, "volume": 5, "dir": "flat"},  # >24h old
            {"ts": now - 100, "price": 2, "volume": 3, "dir": "up"},                       # recent
        ], maxlen=server.TRADE_HISTORY)
        server._prune_trades(1, now)
        self.assertEqual(len(server.trades[1]), 1)
        self.assertEqual(server.trades[1][0]["price"], 2, "only the recent print survives the 24h window")

    def test_persist_and_reload_survives_restart(self):
        server.trades[1] = deque([{"ts": time.time(), "price": 5, "volume": 2, "dir": "up"}],
                                 maxlen=server.TRADE_HISTORY)
        server._save_trades()
        server.trades.clear()                 # simulate a server restart (fresh process)
        server.load_trades()
        self.assertEqual(len(server.trades.get(1, [])), 1, "tape must survive a restart")
        self.assertEqual(server.trades[1][0]["price"], 5)

    def test_markets_are_independent(self):
        vals = {(b"cv", 1): 0, (b"cv", 2): 0, (b"lp", 1): 0, (b"lp", 2): 0}
        server.mstate = fake_mstate(vals)
        server.record_trade(1); server.record_trade(2)   # seed both
        vals[(b"cv", 1)] = 4 * S; vals[(b"lp", 1)] = 10 * S
        server.record_trade(1)              # only market 1 trades
        self.assertEqual(server.api_trades({"market": "1"})["metrics"]["trades"], 1)
        self.assertEqual(server.api_trades({"market": "2"})["metrics"]["trades"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
