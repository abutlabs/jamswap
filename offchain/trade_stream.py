#!/usr/bin/env python3
"""Continuous, backpressured cross-account trade stream for the accounts dashboard.

Unlike loadgen.py (fire-and-forget, overwhelms a finality-light net) this places a
small BALANCED batch, waits for it to fully clear, then places the next — so trades
settle across the six dev accounts forever without ever piling up an unsettleable
mempool. It reuses the fuzzer's proven signing + settlement-wait primitives.

    docker exec -e DEX_URL=http://localhost:8080 -e PYTHONUNBUFFERED=1 \
        lasair6-dex-1 python3 -u trade_stream.py [--pairs N] [--think S]

Stop it with `docker exec lasair6-dex-1 pkill -f trade_stream` or Ctrl-C.
"""
import argparse, time
import dex_fuzz as F


def run():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", type=int, default=8, help="crossing pairs per round")
    ap.add_argument("--think", type=float, default=3.0, help="pause between cleared rounds (s)")
    ap.add_argument("--settle-timeout", type=float, default=120.0)
    args = ap.parse_args()

    import random
    F._init_seqs()
    rnd = 0
    print(f"trade_stream: {args.pairs} pairs/round, backpressured, dex={F.L.DEX}")
    while True:
        rnd += 1
        rng = random.Random(1_000_000 + rnd)          # a different balanced batch each round
        orders, _dot, exp_vol, _gb, _gs, _n = F.gen_batch(rng, args.pairs)
        before = F.chain_state()
        cv_target = before["cv"] + exp_vol * F.SC
        placed = 0
        for (h, side, q, px) in orders:
            try:
                r = F._place(F.L.KEYS[h - 1], h, side, q, px)
                placed += 1 if r.get("ok") else 0
            except Exception:
                pass
        ok, cv, waited = F.wait_quiescent(cv_target, args.settle_timeout)
        got = (F.chain_state()["cv"] - before["cv"]) // F.SC
        status = "settled" if ok else "TIMEOUT"
        print(f"  round {rnd}: placed {placed}/{len(orders)}, {status} "
              f"+{got} DOT vol in {round(waited)}s", flush=True)
        if not ok:
            print("  (settlement stalled — net may be saturated/wedged; backing off)", flush=True)
            time.sleep(10)
        time.sleep(args.think)


if __name__ == "__main__":
    raise SystemExit(run())
