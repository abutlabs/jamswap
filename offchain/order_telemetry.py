"""Per-order lifecycle telemetry — the instrument the 99.99%-clearing soak reads.

The op-level pending ledger in `metrics.py` answers "did this submission settle?".
This answers the finer question the soak actually needs: "of every order a trader
placed, what fraction that COULD have cleared durably DID — and where did the rest
die?" That is the production SLO, and it is measured per order, not per round.

Each order is followed through a small state machine:

    placed ─┬─► rested ────────────────► (terminal: rested, never crossed)
            └─► rounded ─┬─► settled ───► (terminal: filled / partial-carried)
                         ├─► reverted ──► back to rounded (a re-org ate it; retry)
                         ├─► requeued ──► back to placed (round never landed; retry)
                         └─► expired ───► (terminal: expired — the failure the SLO counts)

An order tagged `marketable` at placement (it crossed the resting book / opposing
mempool, so it SHOULD trade) that ends `expired` or `lost` is an SLO miss. One that
ends `filled`/`partial-carried` is an SLO hit. A `rested` order that never crossed is
neither — it correctly sat on the book.

Two outputs:
  * Prometheus: `jamswap_order_placed_total`, `jamswap_order_terminal_total{outcome}`,
    `jamswap_order_retries_total{kind}`, `jamswap_order_clear_latency_seconds`
    (placement→durable fill), and `jamswap_order_clearing_slo` (the headline gauge).
  * a JSONL event log (ORDER_EVENTS_FILE) — one line per transition, so a soak's
    verdict harness can replay and audit every order offline.

Stdlib only; thread-safe; import `metrics` for the shared Prometheus registry.
"""
import json
import os
import threading
import time

import metrics

ORDER_EVENTS_FILE = os.environ.get("ORDER_EVENTS_FILE", "/tmp/jamswap_order_events.jsonl")

# outcomes that END an order's life (it will not transition again). A CARRIED remainder
# is NOT here: re-sealing keeps the order working under the same oid, so it resolves to
# exactly one terminal later (filled / expired). Only genuine end states appear.
TERMINAL = {"filled", "partial-cancelled", "rested",
            "cancelled", "expired", "rejected", "lost"}
# terminal outcomes that count as the order having CLEARED (made durable progress)
CLEARED = {"filled"}

metrics.describe("jamswap_order_placed_total",
                 "orders accepted at the door, by whether they were marketable at placement")
metrics.describe("jamswap_order_terminal_total",
                 "orders that reached a terminal state, by outcome and marketable flag")
metrics.describe("jamswap_order_retries_total",
                 "order retry transitions (a round reverted or never landed), by kind")
metrics.describe("jamswap_order_clear_latency_seconds",
                 "placement -> durable fill latency for orders that cleared")
metrics.describe("jamswap_order_clearing_slo",
                 "cleared / (cleared + missed) among MARKETABLE orders — the headline reliability SLO")
metrics.describe("jamswap_order_open",
                 "orders currently live (placed but not yet terminal), by phase")

_lock = threading.Lock()
_orders = {}            # (market, account, oid) -> order lifecycle record
# rolling SLO tallies over marketable orders that reached a terminal state
_slo = {"cleared": 0, "missed": 0}


def _log(rec, event, **extra):
    line = {"ts": round(time.time(), 3), "event": event,
            "market": rec["market"], "account": rec["account"], "oid": rec["oid"],
            "marketable": rec["marketable"]}
    line.update(extra)
    try:
        with open(ORDER_EVENTS_FILE, "a") as fh:
            fh.write(json.dumps(line) + "\n")
    except OSError:
        pass        # a full/unwritable log must never break trading


def placed(market, account, oid, side, price, qty, sealed, marketable):
    """An order was accepted. `marketable` = it crossed the book/opposing mempool at
    placement, so a correct chain MUST clear it — that is what the SLO measures."""
    key = (int(market), int(account), int(oid))
    rec = {"market": int(market), "account": int(account), "oid": int(oid),
           "side": int(side), "price": int(price), "qty": int(qty),
           "sealed": bool(sealed), "marketable": bool(marketable),
           "placed_at": time.time(), "phase": "placed", "retries": 0,
           "filled": 0, "terminal": None}
    with _lock:
        _orders[key] = rec
    metrics.inc("jamswap_order_placed_total",
                {"marketable": str(bool(marketable)).lower()})
    _log(rec, "placed", side=int(side), price=int(price), qty=int(qty), sealed=bool(sealed))


def rounded(market, account, oid):
    """The order was pulled into an in-flight round (submitted to the chain)."""
    key = (int(market), int(account), int(oid))
    with _lock:
        rec = _orders.get(key)
        if rec and rec["phase"] not in TERMINAL:
            rec["phase"] = "rounded"
            rec.setdefault("rounded_at", time.time())
    if rec:
        _log(rec, "rounded")


def reverted(market, account, oid):
    """The round this order settled on lost fork choice — a re-org ate it; it retries."""
    key = (int(market), int(account), int(oid))
    with _lock:
        rec = _orders.get(key)
        if rec and rec["phase"] not in TERMINAL:
            rec["retries"] += 1
            rec["phase"] = "rounded"
    if rec:
        metrics.inc("jamswap_order_retries_total", {"kind": "reverted"})
        _log(rec, "reverted", retries=rec["retries"])


def requeued(market, account, oid):
    """The round never landed within the gate; the order returns to the mempool."""
    key = (int(market), int(account), int(oid))
    with _lock:
        rec = _orders.get(key)
        if rec and rec["phase"] not in TERMINAL:
            rec["retries"] += 1
            rec["phase"] = "placed"
    if rec:
        metrics.inc("jamswap_order_retries_total", {"kind": "requeued"})
        _log(rec, "requeued", retries=rec["retries"])


def deferred(market, account, oid, reason):
    """Non-terminal: a sealed order is waiting for its commit to land on-chain before it
    can reveal. Recorded (phase + JSONL) for observability so a soak can see WHY an order
    is idle, but it stays live — it will reveal, carry, or expire in a later round."""
    key = (int(market), int(account), int(oid))
    with _lock:
        rec = _orders.get(key)
        if rec and rec["phase"] not in TERMINAL:
            rec["phase"] = "deferred"
    if rec:
        _log(rec, "deferred", reason=reason)


def terminal(market, account, oid, outcome, filled=0):
    """The order reached a terminal state. `outcome` in TERMINAL; `filled` is the
    durably-settled quantity (atomic). Updates the SLO for marketable orders."""
    key = (int(market), int(account), int(oid))
    with _lock:
        rec = _orders.pop(key, None)
        if rec is None:
            # a terminal for an order we never saw placed (e.g. resting book order
            # from before this process started): synthesize a minimal record so the
            # counters and log stay complete rather than silently dropping it.
            rec = {"market": int(market), "account": int(account), "oid": int(oid),
                   "marketable": False, "placed_at": time.time(), "retries": 0}
        rec["terminal"] = outcome
        rec["filled"] = filled
        cleared = outcome in CLEARED
        latency = time.time() - rec["placed_at"]
        if rec.get("marketable"):
            if cleared:
                _slo["cleared"] += 1
            elif outcome in ("expired", "lost", "partial-cancelled", "rejected"):
                _slo["missed"] += 1
        c, mi = _slo["cleared"], _slo["missed"]
    metrics.inc("jamswap_order_terminal_total",
                {"outcome": outcome, "marketable": str(bool(rec.get("marketable"))).lower()})
    if outcome in CLEARED:
        metrics.observe("jamswap_order_clear_latency_seconds", None, latency)
    total = c + mi
    metrics.set_gauge("jamswap_order_clearing_slo", None, (c / total) if total else 1.0)
    _log(rec, "terminal", outcome=outcome, filled=int(filled),
         retries=rec.get("retries", 0), latency=round(latency, 2))


def snapshot():
    """Live SLO + open-order counts for /api/orders_slo and the phase gauges."""
    with _lock:
        c, mi = _slo["cleared"], _slo["missed"]
        phases = {}
        for rec in _orders.values():
            phases[rec["phase"]] = phases.get(rec["phase"], 0) + 1
        open_n = len(_orders)
    for ph in ("placed", "rounded", "deferred"):
        metrics.set_gauge("jamswap_order_open", {"phase": ph}, phases.get(ph, 0))
    total = c + mi
    return {"cleared": c, "missed": mi, "open": open_n, "phases": phases,
            "slo": (c / total) if total else 1.0,
            "marketable_terminal": total}
