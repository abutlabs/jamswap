"""Prometheus instrumentation + the submit->settle ledger for the jamswap
off-chain layer (docs/OBSERVABILITY_PLAN.md phases 0-1). Stdlib only.

Two jobs, one module:

  * a tiny metrics registry (counters / gauges / one histogram family) rendered
    in Prometheus text exposition format by `render()`;
  * the PENDING LEDGER: every state-mutating payload the server relays to the
    chain is `track()`ed with a settle predicate; a single watcher thread polls
    the predicates and stamps each entry settled (observing the
    `jamswap_settle_latency_seconds{op}` histogram) or timed-out. `/api/pending`
    serves the ledger so the UI can show WHERE an order is instead of a
    dead-end "try again" toast — on a contested mixed chain settlement takes
    slots, and slow must be distinguishable from broken.

The settle predicates read the SAME on-chain state the UI reads (CE-129 via the
reader), so "settled" here means user-visible, end to end.
"""
import threading, time

_lock = threading.Lock()
_counters = {}          # (name, labels_tuple) -> float
_gauge_cbs = {}         # name -> zero-arg callable returning float (evaluated per scrape)
_help = {}              # name -> help text

# settle-latency buckets in seconds; JAM slots are 6s, so these read as
# 0.5/1/2/3/4/5/7.5/10/15/20/30/50/100 slots.
BUCKETS = [3, 6, 12, 18, 24, 30, 45, 60, 90, 120, 180, 300, 600]
_hist = {}              # (name, labels_tuple) -> [counts per bucket] + [sum, count]

def _labels_key(labels):
    return tuple(sorted((labels or {}).items()))

def describe(name, help_text):
    _help[name] = help_text

def inc(name, labels=None, by=1.0):
    with _lock:
        k = (name, _labels_key(labels))
        _counters[k] = _counters.get(k, 0.0) + by

def gauge_fn(name, help_text, fn):
    """Register a gauge evaluated lazily at scrape time (errors -> sample skipped)."""
    _help[name] = help_text
    _gauge_cbs[name] = fn

def observe(name, labels=None, value=0.0):
    with _lock:
        k = (name, _labels_key(labels))
        h = _hist.setdefault(k, [0] * len(BUCKETS) + [0.0, 0])
        for i, b in enumerate(BUCKETS):
            if value <= b:
                h[i] += 1
        h[-2] += value
        h[-1] += 1

def _fmt_labels(lt, extra=None):
    parts = ["%s=%r" % (k, str(v)) for k, v in lt] + (extra or [])
    return ("{" + ",".join(parts).replace("'", '"') + "}") if parts else ""

def render():
    out = []
    with _lock:
        counters = dict(_counters)
        hists = {k: list(v) for k, v in _hist.items()}
    seen = set()
    for (name, lt), v in sorted(counters.items()):
        if name not in seen:
            seen.add(name)
            if name in _help:
                out.append("# HELP %s %s" % (name, _help[name]))
            out.append("# TYPE %s counter" % name)
        out.append("%s%s %g" % (name, _fmt_labels(lt), v))
    for (name, lt), h in sorted(hists.items()):
        if name not in seen:
            seen.add(name)
            if name in _help:
                out.append("# HELP %s %s" % (name, _help[name]))
            out.append("# TYPE %s histogram" % name)
        cum = 0
        for i, b in enumerate(BUCKETS):
            cum = h[i]
            out.append('%s_bucket%s %d' % (name, _fmt_labels(lt, ['le="%g"' % b]), cum))
        out.append('%s_bucket%s %d' % (name, _fmt_labels(lt, ['le="+Inf"']), h[-1]))
        out.append("%s_sum%s %g" % (name, _fmt_labels(lt), h[-2]))
        out.append("%s_count%s %d" % (name, _fmt_labels(lt), h[-1]))
    for name, fn in _gauge_cbs.items():
        try:
            v = float(fn())
        except Exception:
            continue        # a failed read (node restarting) skips the sample, honestly
        out.append("# HELP %s %s" % (name, _help.get(name, "")))
        out.append("# TYPE %s gauge" % name)
        out.append("%s %g" % (name, v))
    return "\n".join(out) + "\n"

# ---- the pending ledger -----------------------------------------------------

SETTLE_TIMEOUT = 600            # 100 slots: past this an op is declared timed-out
LEDGER_KEEP = 100               # resolved entries kept for /api/pending history

_ledger_lock = threading.Lock()
_ledger = []                    # newest last: dicts (see track())
_seq = [0]

describe("jamswap_submits_total", "state-mutating payloads relayed to the chain, by op")
describe("jamswap_settled_total", "tracked ops whose on-chain effect became visible, by op")
describe("jamswap_settle_timeouts_total", "tracked ops with no visible effect within the timeout, by op")
describe("jamswap_settle_latency_seconds", "submit -> state-visible latency for tracked ops, by op")
describe("jamswap_api_requests_total", "HTTP API requests, by route and status code")
describe("jamswap_api_errors_total", "HTTP API handler exceptions, by route")

def track(op, detail, check=None):
    """Record a relayed submission. `check` is a zero-arg predicate that returns
    True once the op's effect is visible on-chain; None means the op is counted
    but not settlement-tracked (e.g. rounds, whose effect is a book rewrite)."""
    inc("jamswap_submits_total", {"op": op})
    e = {"id": _seq[0], "op": op, "detail": detail, "submitted": time.time(),
         "settled": None, "timed_out": False, "check": check}
    _seq[0] += 1
    with _ledger_lock:
        _ledger.append(e)
        # bound memory: drop oldest RESOLVED entries beyond the keep window
        resolved = [x for x in _ledger if x["settled"] or x["timed_out"] or not x["check"]]
        for x in resolved[:-LEDGER_KEEP]:
            _ledger.remove(x)
    return e["id"]

def pending_snapshot():
    now = time.time()
    with _ledger_lock:
        entries = list(_ledger)[-LEDGER_KEEP:]
    out = []
    for e in reversed(entries):         # newest first
        state = ("settled" if e["settled"] else
                 "timed_out" if e["timed_out"] else
                 "pending" if e["check"] else "submitted")
        out.append({"id": e["id"], "op": e["op"], "detail": e["detail"], "state": state,
                    "age": round(now - e["submitted"], 1),
                    "latency": round(e["settled"] - e["submitted"], 1) if e["settled"] else None})
    return out

def _watch():
    while True:
        time.sleep(2.0)
        now = time.time()
        with _ledger_lock:
            open_entries = [e for e in _ledger
                            if e["check"] and not e["settled"] and not e["timed_out"]]
        for e in open_entries:
            try:
                done = e["check"]()
            except Exception:
                continue                # reader hiccup: try again next tick
            if done:
                e["settled"] = now
                inc("jamswap_settled_total", {"op": e["op"]})
                observe("jamswap_settle_latency_seconds", {"op": e["op"]},
                        now - e["submitted"])
            elif now - e["submitted"] > SETTLE_TIMEOUT:
                e["timed_out"] = True
                inc("jamswap_settle_timeouts_total", {"op": e["op"]})

def start_watcher():
    threading.Thread(target=_watch, daemon=True).start()
