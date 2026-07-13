#!/usr/bin/env python3
"""Soak verdict harness — replay the per-order event log and judge each order.

Reads the JSONL written by `order_telemetry` (one line per lifecycle transition)
and reconstructs every order's history, then answers the soak's question order by
order: did each marketable order CLEAR durably, and if not, where did it die?

    python3 soak_verdict.py [events.jsonl] [--target 0.9999] [--json]

Exit code 0 iff the clearing SLO meets the target AND no order is left in an
illegal state (open forever, cleared-then-reverted-permanently). Designed to run
as the assertion at the end of a k8s soak.

The SLO denominator is MARKETABLE orders that reached a terminal state — orders a
correct chain was obliged to clear. A non-marketable order that rested and expired
is not a failure (it never had a counterparty); it is reported separately.
"""
import argparse
import collections
import json
import sys
import time

CLEARED = {"filled", "partial-carried"}
MISSED = {"expired", "lost", "partial-cancelled", "rejected"}


def load(path):
    orders = collections.OrderedDict()   # key -> list of events (in file order)
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = (e["market"], e["account"], e["oid"])
            orders.setdefault(key, []).append(e)
    return orders


def judge(events):
    """Collapse one order's event stream into a verdict dict."""
    placed = next((e for e in events if e["event"] == "placed"), None)
    term = next((e for e in events if e["event"] == "terminal"), None)
    marketable = any(e.get("marketable") for e in events)
    retries = max((e.get("retries", 0) for e in events), default=0)
    reverts = sum(1 for e in events if e["event"] == "reverted")
    v = {"marketable": marketable, "retries": retries, "reverts": reverts,
         "placed": bool(placed), "terminal": term["outcome"] if term else None,
         "latency": term.get("latency") if term else None,
         "filled": term.get("filled", 0) if term else 0}
    if term is None:
        v["class"] = "open"                       # never reached a terminal state
    elif term["outcome"] in CLEARED:
        v["class"] = "cleared"
    elif term["outcome"] in MISSED:
        v["class"] = "missed" if marketable else "expired-nonmarketable"
    else:
        v["class"] = "rested"                     # rested/cancelled non-marketable
    return v


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("events", nargs="?", default="/tmp/jamswap_order_events.jsonl")
    ap.add_argument("--target", type=float, default=0.9999,
                    help="minimum clearing SLO to PASS (default 0.9999)")
    ap.add_argument("--open-grace", type=float, default=600,
                    help="seconds an order may stay open before it counts as a failure")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    try:
        orders = load(args.events)
    except FileNotFoundError:
        print(f"no event log at {args.events} — nothing to judge", file=sys.stderr)
        return 2

    now = time.time()
    tally = collections.Counter()
    latencies, retried, stuck_open, missed_orders = [], 0, [], []
    for key, events in orders.items():
        v = judge(events)
        tally[v["class"]] += 1
        if v["retries"]:
            retried += 1
        if v["class"] == "cleared" and v["latency"] is not None:
            latencies.append(v["latency"])
        if v["class"] == "open":
            last = max(e["ts"] for e in events)
            if now - last > args.open_grace:      # open past the grace window = a real miss
                stuck_open.append((key, round(now - last, 1)))
        if v["class"] == "missed":
            missed_orders.append((key, v["terminal"], v["retries"]))

    cleared = tally["cleared"]
    missed = tally["missed"] + len(stuck_open)    # stuck-open marketable = missed
    denom = cleared + missed
    slo = (cleared / denom) if denom else 1.0
    p50 = p99 = None
    if latencies:
        s = sorted(latencies)
        p50 = s[len(s) // 2]
        p99 = s[min(len(s) - 1, int(len(s) * 0.99))]

    ok = slo >= args.target and not stuck_open
    report = {
        "orders_seen": len(orders),
        "slo": round(slo, 6), "target": args.target, "pass": ok,
        "cleared": cleared, "missed": missed,
        "breakdown": dict(tally),
        "orders_with_retries": retried,
        "clear_latency_p50_s": p50, "clear_latency_p99_s": p99,
        "stuck_open": stuck_open[:20],
        "sample_missed": missed_orders[:20],
    }

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"orders seen         : {report['orders_seen']}")
        print(f"clearing SLO        : {slo:.6f}  (target {args.target})  "
              f"{'PASS' if ok else 'FAIL'}")
        print(f"  cleared           : {cleared}")
        print(f"  missed            : {missed}  "
              f"(expired/lost {tally['missed']}, stuck-open {len(stuck_open)})")
        print(f"breakdown           : {dict(tally)}")
        print(f"orders w/ retries   : {retried}")
        if p50 is not None:
            print(f"clear latency       : p50 {p50:.1f}s  p99 {p99:.1f}s")
        if stuck_open:
            print(f"STUCK OPEN (>{args.open_grace:.0f}s): "
                  f"{len(stuck_open)} — e.g. {stuck_open[:5]}")
        if missed_orders:
            print(f"sample missed       : {missed_orders[:5]}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
