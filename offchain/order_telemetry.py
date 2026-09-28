"""Per-order lifecycle telemetry — the instrument the 99.99%-clearing soak reads.

The op-level pending ledger in `metrics.py` answers "did this submission settle?".
This answers the finer question the soak actually needs: "of every order a trader
placed, what fraction that COULD have cleared durably DID — and where did the rest
die?" That is the production SLO, and it is measured per order, not per round.

Each order is followed through a small state machine:

    placed ──► rounded ─┬─► settled ──► (terminal: filled)
                        ├─► rested ───► live on the book ─┬─► (terminal: filled)
                        │                                 └─► (terminal: expired / cancelled)
                        ├─► reverted ─► back to rounded (a re-org ate it; retry)
                        ├─► requeued ─► back to placed (round never landed; retry)
                        └─► expired ──► (terminal: expired — the failure the SLO counts)

An order tagged `marketable` at placement (it crossed the resting book / opposing
mempool, so it SHOULD trade) that ends `expired` or `lost` is an SLO miss. One that
ends `filled` is an SLO hit. Placement is only a guess, though: a settled auction that
leaves a public order `rested` on the book (non-terminal) also says whether the order's
limit reached that auction's uniform clearing price (`crossed`, recorded for reporting).
From then on the auction, not the placement guess, has decided the order: outbid, or
rationed at the clearing price by price-time priority (the engine's tie-break can ration
even orders better than the clearing price), it correctly sits on the book. A rested
order is therefore never an SLO miss; one that later fills still counts as cleared, and
one that stays live long past its own expiry is caught as a leak by the verdict.

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
TERMINAL = {"filled", "partial-cancelled",
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
metrics.describe("jamswap_order_open_stale",
                 "live orders the soak verdict would call stuck open: no event for OPEN_GRACE s "
                 "(resting ones: OPEN_GRACE s past their own expiry), by phase")
metrics.describe("jamswap_order_open_oldest_seconds",
                 "the longest any live order has gone without an event (resting ones: time past "
                 "their expiry)")

# an order silent this long is stuck open: soak_verdict.score's open_grace default, so the
# live gauge and the verdict count the same orders
OPEN_GRACE = 600

_lock = threading.Lock()
_orders = {}            # (market, account, oid) -> order lifecycle record
# rolling SLO tallies over marketable orders that reached a terminal state
_slo = {"cleared": 0, "missed": 0}


def _log(rec, event, **extra):
    """Append one transition to the JSONL log; also the order's last event (and a resting
    order's expiry), which is what the verdict judges an open order by."""
    line = {"ts": round(time.time(), 3), "event": event,
            "market": rec["market"], "account": rec["account"], "oid": rec["oid"],
            "marketable": rec["marketable"]}
    line.update(extra)
    rec["last"] = line["ts"]
    if extra.get("expires_at"):
        rec["expires_at"] = extra["expires_at"]
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


def rested(market, account, oid, clearing, crossed, filled=0, expires_at=None):
    """Non-terminal: a settled auction left this public order (or what remains of it) resting
    on the on-chain book. `clearing` is that auction's uniform price (0 = nothing traded);
    `crossed` = the order's limit reached it (a buy at or above, a sell at or below): it was
    eligible to trade, and rationing left it (part-)unfilled; `filled` = what it traded here.
    The auction supersedes the placement-time guess: the order is no longer counted as
    marketable (an auction outcome is not a system miss). It stays live until it fills, is cancelled,
    or expires (`expires_at`, unix time, lets the verdict flag a prune that never lands)."""
    key = (int(market), int(account), int(oid))
    with _lock:
        rec = _orders.get(key)
        if rec and rec["phase"] not in TERMINAL:
            rec["phase"] = "rested"
            rec["crossed"] = rec.get("crossed", False) or bool(crossed)
            rec["marketable"] = False     # the auction decided it (see the module docstring)
    if rec:
        _log(rec, "rested", clearing=int(clearing), crossed=bool(crossed), filled=int(filled),
             expires_at=round(expires_at, 3) if expires_at else None)


def terminal(market, account, oid, outcome, filled=0, reason=None):
    """The order reached a terminal state. `outcome` in TERMINAL; `filled` is the
    durably-settled quantity (atomic); `reason` (optional) says why, in the event log —
    e.g. a "rejected" order superseded by its account's seq floor. Updates the SLO for
    marketable orders."""
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
    extra = {"reason": reason} if reason else {}
    _log(rec, "terminal", outcome=outcome, filled=int(filled),
         retries=rec.get("retries", 0), latency=round(latency, 2), **extra)


def is_open(market, account, oid):
    """Is this order live (placed in this process and not yet terminal)? Lets a caller end
    an order only if nothing else already has — e.g. a signed cancel that lands after a
    round already filled the order must not record a second terminal."""
    with _lock:
        return (int(market), int(account), int(oid)) in _orders


def snapshot(now=None):
    """Live SLO + open-order counts for /api/orders_slo and the phase gauges, and the open
    orders the soak verdict would call stuck (see OPEN_GRACE)."""
    now = time.time() if now is None else now
    with _lock:
        c, mi = _slo["cleared"], _slo["missed"]
        phases, stale, oldest = {}, {}, 0.0
        for rec in _orders.values():
            ph = rec["phase"]
            phases[ph] = phases.get(ph, 0) + 1
            if ph == "rested":        # legitimately live on the book until its own expiry
                late = now - rec["expires_at"] if rec.get("expires_at") else 0.0
            else:
                late = now - rec.get("last", rec["placed_at"])
            oldest = max(oldest, late)
            if late > OPEN_GRACE:
                stale[ph] = stale.get(ph, 0) + 1
        open_n = len(_orders)
    for ph in ("placed", "rounded", "deferred", "rested"):
        metrics.set_gauge("jamswap_order_open", {"phase": ph}, phases.get(ph, 0))
        metrics.set_gauge("jamswap_order_open_stale", {"phase": ph}, stale.get(ph, 0))
    metrics.set_gauge("jamswap_order_open_oldest_seconds", None, oldest)
    total = c + mi
    return {"cleared": c, "missed": mi, "open": open_n, "phases": phases,
            "stale": sum(stale.values()), "slo": (c / total) if total else 1.0,
            "marketable_terminal": total}
