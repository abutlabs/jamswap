#!/usr/bin/env python3
"""Soak verdict harness — replay the per-order event log and judge each order.

Reads the JSONL written by `order_telemetry` (one line per lifecycle transition)
and reconstructs every order's history, then answers the soak's question order by
order: did each marketable order CLEAR durably, and if not, where did it die?

    python3 soak_verdict.py [events.jsonl] [--target 0.9999] [--json]
                            [--chain samples.jsonl|verdict.json] [--parity parity.json]

Exit code 0 iff the clearing SLO meets the target AND no order is left in an
illegal state (open forever, cleared-then-reverted-permanently). Designed to run
as the assertion at the end of a k8s soak.

The optional CHAIN section (issue #15) judges the net the orders ran on, with
netwatch.py: --chain takes the samples netwatch poll/serve wrote (or a netwatch
verdict JSON) and adds one head, liveness and finality advance; --parity takes a
`netwatch.py parity --json` result and adds state agreement across the nodes. When
either is given the exit code is 0 only if the orders AND the chain pass; without
them the verdict is exactly the order verdict.

The SLO denominator is MARKETABLE orders that reached a terminal state — orders a
correct chain was obliged to clear. A non-marketable order that rested and expired
is not a failure (it never had a counterparty); it is reported separately.

"Marketable" starts as the placement-time guess (the order crossed the book or the
mempool when it arrived). Once a settled auction leaves the order `rested` on the book,
the auction has decided it: outbid, or rationed at the clearing price by price-time
priority (`crossed` records whether its limit reached that price; the engine's tie-break
can ration even orders better than it). Either way it is `resting`, not open, and never
a miss — not while it rests, nor when it expires unfilled; it clears if it later fills. A resting order still open well past its own expiry is stuck (its prune
never landed). Logs written before the `rested` event existed carry no clearing price,
so there a rested order still looks open.
"""
import argparse
import collections
import json
import sys
import time

# Terminal outcomes. A CARRIED remainder is NOT terminal — re-sealing keeps the order
# working under the same oid, so it resolves to exactly one terminal later (filled/expired).
# Only genuine end states appear as `terminal` events in the telemetry log.
CLEARED = {"filled"}
MISSED = {"expired", "lost", "cancelled", "partial-cancelled", "rejected"}


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
    rests = [e for e in events if e["event"] == "rested"]
    # a settled auction that left the order resting has decided it (outbid, or rationed at
    # the clearing price by price-time priority): it is no longer a clearing obligation
    marketable = False if rests else any(e.get("marketable") for e in events)
    crossed = any(e.get("crossed") for e in rests)
    sealed = bool(placed and placed.get("sealed"))
    deferrals = sum(1 for e in events if e["event"] == "deferred")
    retries = max((e.get("retries", 0) for e in events), default=0)
    reverts = sum(1 for e in events if e["event"] == "reverted")
    v = {"marketable": marketable, "crossed": crossed, "sealed": sealed, "deferrals": deferrals,
         "retries": retries, "reverts": reverts,
         "placed": bool(placed), "terminal": term["outcome"] if term else None,
         "latency": term.get("latency") if term else None,
         "filled": term.get("filled", 0) if term else 0,
         "expires_at": rests[-1].get("expires_at") if rests else None}
    if term is None:
        # live on the book after an auction, or never reached a terminal state
        v["class"] = "resting" if events[-1]["event"] == "rested" else "open"
    elif term["outcome"] in CLEARED:
        v["class"] = "cleared"
    elif term["outcome"] in MISSED:
        v["class"] = "missed" if marketable else "expired-nonmarketable"
    else:
        v["class"] = "other"                      # an outcome this harness doesn't know
    return v


def score(orders, target=0.9999, open_grace=600, now=None):
    """Judge every order of a loaded event log (see `load`); returns the report dict."""
    now = time.time() if now is None else now
    tally = collections.Counter()
    sealed_tally = collections.Counter()
    latencies, retried, stuck_open, missed_orders = [], 0, [], []
    sealed_seen = sealed_terminal = sealed_stuck = 0
    sealed_lost = []                              # sealed orders with NO terminal, open past grace
    resting_crossed = 0                           # resting, but a clearing price reached them
    for key, events in orders.items():
        v = judge(events)
        tally[v["class"]] += 1
        if v["sealed"]:
            sealed_seen += 1
            sealed_tally[v["class"]] += 1
            if v["terminal"] is not None:
                sealed_terminal += 1
        if v["retries"]:
            retried += 1
        if v["class"] == "cleared" and v["latency"] is not None:
            latencies.append(v["latency"])
        if v["class"] == "open":
            last = max(e["ts"] for e in events)
            if now - last > open_grace:           # open past the grace window = a real miss
                stuck_open.append((key, round(now - last, 1)))
                if v["sealed"]:
                    sealed_stuck += 1
                    sealed_lost.append((key, round(now - last, 1)))
        if v["class"] == "resting":
            # legitimately live on the book — until its own expiry: still open well past it,
            # the prune never landed (the order leaked)
            resting_crossed += v["crossed"]
            if v["expires_at"] and now - v["expires_at"] > open_grace:
                stuck_open.append((key, round(now - v["expires_at"], 1)))
        if v["class"] == "missed":
            missed_orders.append((key, v["terminal"], v["retries"]))

    cleared = tally["cleared"]
    missed = tally["missed"] + len(stuck_open)    # stuck open = missed
    denom = cleared + missed
    slo = (cleared / denom) if denom else 1.0
    p50 = p99 = None
    if latencies:
        s = sorted(latencies)
        p50 = s[len(s) // 2]
        p99 = s[min(len(s) - 1, int(len(s) * 0.99))]

    # Sealed zero-loss: every accepted sealed order must reach EXACTLY ONE terminal state
    # (or still be legitimately live within grace). A sealed order stuck open past grace is a
    # SILENT DROP — the failure the robustness redesign exists to eliminate. This is the
    # headline invariant of the sealed-order soak.
    sealed_zero_loss = (sealed_stuck == 0)
    ok = slo >= target and not stuck_open and sealed_zero_loss
    return {
        "orders_seen": len(orders),
        "slo": round(slo, 6), "target": target, "pass": ok,
        "cleared": cleared, "missed": missed,
        "missed_terminal": tally["missed"],
        "breakdown": dict(tally),
        "resting": tally["resting"], "resting_crossed": resting_crossed,
        "orders_with_retries": retried,
        "clear_latency_p50_s": p50, "clear_latency_p99_s": p99,
        "stuck_open_count": len(stuck_open), "stuck_open": stuck_open[:20],
        "sample_missed": missed_orders[:20],
        "sealed": {
            "seen": sealed_seen, "terminal": sealed_terminal,
            "stuck_open": sealed_stuck, "zero_loss": sealed_zero_loss,
            "breakdown": dict(sealed_tally),
            "sample_lost": sealed_lost[:20],
        },
    }


def chain_section(chain_path=None, parity_path=None, **opts):
    """The chain half of the verdict: netwatch's one head / liveness / finality verdict
    over `chain_path` (samples JSONL or a verdict JSON), and the parity result at
    `parity_path`. `opts` are netwatch.verdict's thresholds."""
    sec = {"pass": True, "verdict": None, "parity": None}
    if chain_path:
        import netwatch                           # only a chain section needs the chain adapter
        v = netwatch.load_chain_report(chain_path, **opts)
        sec["verdict"] = v
        sec["pass"] = sec["pass"] and bool(v.get("pass"))
    if parity_path:
        with open(parity_path) as fh:
            p = json.load(fh)
        sec["parity"] = p
        sec["pass"] = sec["pass"] and bool(p.get("pass"))
    return sec


def with_chain(report, sec):
    """Fold a chain section into an order report: `pass` becomes orders AND chain."""
    report["orders_pass"] = report["pass"]
    report["chain"] = sec
    report["pass"] = report["pass"] and sec["pass"]
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("events", nargs="?", default="/tmp/jamswap_order_events.jsonl")
    ap.add_argument("--target", type=float, default=0.9999,
                    help="minimum clearing SLO to PASS (default 0.9999)")
    ap.add_argument("--open-grace", type=float, default=600,
                    help="seconds an order may stay open before it counts as a failure")
    ap.add_argument("--json", action="store_true")
    ch = ap.add_argument_group("chain section (optional; netwatch.py)")
    ch.add_argument("--chain", default=None, metavar="FILE",
                    help="netwatch samples (JSONL) or verdict (JSON): adds one head, liveness "
                         "and finality advance to the verdict")
    ch.add_argument("--parity", default=None, metavar="FILE",
                    help="a `netwatch.py parity --json` result: adds state agreement")
    ch.add_argument("--max-lag", type=int, default=None,
                    help="slots a node's best may trail the newest (netwatch default 3)")
    ch.add_argument("--epoch-slots", type=int, default=None,
                    help="epoch length in slots (default: as the samples recorded it)")
    ch.add_argument("--require-finality", action="store_true",
                    help="fail the chain section if the finalized head does not advance")
    ch.add_argument("--final-stall-slots", type=int, default=None,
                    help="longest the finalized head may stand still (default one epoch)")
    args = ap.parse_args()

    try:
        orders = load(args.events)
    except FileNotFoundError:
        print(f"no event log at {args.events} — nothing to judge", file=sys.stderr)
        return 2

    report = score(orders, target=args.target, open_grace=args.open_grace)
    if args.chain or args.parity:
        opts = {"epoch_slots": args.epoch_slots,
                "finality": "require" if args.require_finality else "auto",
                "final_stall_slots": args.final_stall_slots}
        if args.max_lag is not None:
            opts["max_lag"] = args.max_lag
        try:
            sec = chain_section(args.chain, args.parity, **opts)
        except (OSError, ValueError, KeyError, TypeError) as e:
            print(f"cannot read the chain section: {e}", file=sys.stderr)
            return 2
        report = with_chain(report, sec)
    # the order verdict alone (the combined one is report["pass"] when a chain section is in)
    ok = report.get("orders_pass", report["pass"])
    slo, stuck_open = report["slo"], report["stuck_open"]
    p50, p99 = report["clear_latency_p50_s"], report["clear_latency_p99_s"]
    missed_orders, n_stuck = report["sample_missed"], report["stuck_open_count"]
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"orders seen         : {report['orders_seen']}")
        print(f"clearing SLO        : {slo:.6f}  (target {args.target})  "
              f"{'PASS' if ok else 'FAIL'}")
        print(f"  cleared           : {report['cleared']}")
        print(f"  missed            : {report['missed']}  "
              f"(expired/lost {report['missed_terminal']}, stuck-open {n_stuck})")
        print(f"breakdown           : {report['breakdown']}")
        print(f"resting on book     : {report['resting']}  "
              f"({report['resting_crossed']} met a clearing price and were rationed — "
              f"judged when they end)")
        print(f"orders w/ retries   : {report['orders_with_retries']}")
        sl = report["sealed"]
        print(f"SEALED zero-loss    : {'PASS' if sl['zero_loss'] else 'FAIL'}  "
              f"(seen {sl['seen']}, terminal {sl['terminal']}, stuck-open {sl['stuck_open']})")
        print(f"  sealed breakdown  : {sl['breakdown']}")
        if sl["sample_lost"]:
            print(f"  SEALED LOST       : {sl['sample_lost'][:5]}")
        if p50 is not None:
            print(f"clear latency       : p50 {p50:.1f}s  p99 {p99:.1f}s")
        if stuck_open:
            print(f"STUCK OPEN (>{args.open_grace:.0f}s): "
                  f"{n_stuck} — e.g. {stuck_open[:5]}")
        if missed_orders:
            print(f"sample missed       : {missed_orders[:5]}")
        if "chain" in report:
            import netwatch
            sec = report["chain"]
            print(f"orders              : {'PASS' if report['orders_pass'] else 'FAIL'}")
            if sec["verdict"] is not None:
                netwatch.print_verdict(sec["verdict"])
            if sec["parity"] is not None:
                netwatch.print_parity(sec["parity"])
            print(f"VERDICT (orders + chain): {'PASS' if report['pass'] else 'FAIL'}")
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
