"""A soak's configuration, progress and results as Prometheus metrics for the obs stack's
Pushgateway (the "Soak runs" dashboard, observability/dashboards/obs-soak-runs.json).

nets/soak.py pushes them with `obs push soak --group run_id=<run> --group net=<net>`: one
group per run, so every series below also carries job="soak", run_id and net. The start
pushes soak_info and the phase, every minute of load pushes the progress, the end pushes
the results (a push replaces only the metrics it names, so the three never erase each
other).

  soak_info{clients, lasair_image, data_dir, dex_backend, load_profile, load_rate,
            sealed_ratio, jamswap_commit, secs, drain, out_dir}      1
  soak_phase{phase}                  1 (starting, load, drain, parity, verdict, done)
  soak_load_seconds_left             seconds of load still to run
  soak_load_seconds, soak_drain_seconds, soak_started_timestamp_seconds,
  soak_finished_timestamp_seconds, soak_duration_seconds
  soak_pass                          1 iff every check passed
  soak_check{check, measured, threshold}   1 pass / 0 fail: the dashboard's tiles
  soak_check_value{check}, soak_check_threshold{check}   the numbers behind them
  soak_orders_{offered,refused,busy,seen,cleared,missed}, soak_sealed_{seen,stuck},
  soak_clear_latency_seconds{quantile="0.5"|"0.99"}, soak_finalized_slots,
  soak_step_exit_code{step="poll"|"parity"|"verdict"}

Stdlib only; pure functions (nets/tests/test_obs.py).
"""

# (check, tile title, what it means) in the order the dashboard shows them
CHECKS = [
    ("offered_load", "offered load",
     "Orders the DEX turned away (refused or busy) over orders offered."),
    ("clearing_slo", "clearing SLO",
     "Of the orders that could trade (met a clearing price), the share that did."),
    ("sealed_zero_loss", "SEALED zero-loss",
     "Sealed (hidden) orders stuck between commit and reveal."),
    ("one_head", "one head",
     "Every node agreed on one head at every sample; a divergence must end within an epoch."),
    ("liveness", "liveness", "Every node's best block advanced during the soak."),
    ("finality", "finality",
     "Blocks were finalized, never two at one slot, never going back, no stall of an epoch."),
    ("authoring", "authoring", "Every validator authored blocks."),
    ("state_parity", "state parity",
     "At one finalized block every node, whatever its client, holds byte-identical DEX state."),
]

PHASES = ("starting", "load", "drain", "parity", "verdict", "done")


def _esc(v):
    return str(v).replace("\\", "\\\\").replace("\n", " ").replace('"', '\\"')


def _num(v):
    v = float(v)
    return str(int(v)) if v.is_integer() and abs(v) < 1e15 else repr(v)


class Text:
    """Prometheus text, one family at a time (the Pushgateway wants each family's samples
    together, under one TYPE line)."""

    def __init__(self):
        self.lines, self.seen = [], set()

    def add(self, name, value, labels=None, kind="gauge"):
        if value is None:
            return
        if name not in self.seen:
            self.seen.add(name)
            self.lines.append("# TYPE %s %s" % (name, kind))
        lb = ",".join('%s="%s"' % (k, _esc(v)) for k, v in sorted((labels or {}).items()))
        self.lines.append("%s%s %s" % (name, "{%s}" % lb if lb else "", _num(value)))

    def text(self):
        return "\n".join(self.lines) + "\n"


def start(cfg, secs, drain, started):
    """The soak's configuration and its first phase."""
    t = Text()
    info = {k: cfg.get(k, "") for k in ("clients", "lasair_image", "data_dir", "dex_backend",
                                        "load_profile", "load_rate", "sealed_ratio",
                                        "jamswap_commit", "out_dir")}
    info.update(secs=secs, drain=drain)
    t.add("soak_info", 1, info)
    t.add("soak_load_seconds", secs)
    t.add("soak_drain_seconds", drain)
    t.add("soak_started_timestamp_seconds", started)
    t.lines += progress("starting", secs).splitlines()
    return t.text()


def progress(phase, load_left):
    t = Text()
    t.add("soak_phase", 1, {"phase": phase})
    t.add("soak_load_seconds_left", max(0, int(load_left)))
    return t.text()


def _get(d, *path):
    for p in path:
        if not isinstance(d, dict):
            return None
        d = d.get(p)
    return d


def checks(done, verdict, target):
    """[(check, pass, value, threshold value, measured text, threshold text)] for every check
    the soak could judge (a soak that ended early has fewer)."""
    out = []
    load = done.get("load") or {}
    if load:
        offered = load.get("offered", 0)
        turned = load.get("turned_away", load.get("refused", 0) + load.get("busy", 0))
        out.append(("offered_load", bool(load.get("pass")), turned / offered if offered else 1.0,
                    1 - target, "%d of %d turned away" % (turned, offered),
                    "≤ %g%%" % round(100 * (1 - target), 6)))
    v = verdict or {}
    if "slo" in v:
        out.append(("clearing_slo", v["slo"] >= v.get("target", target), v["slo"],
                    v.get("target", target), "%.4f" % v["slo"], "≥ %s" % v.get("target", target)))
    sealed = v.get("sealed") or {}
    if "zero_loss" in sealed:
        stuck = sealed.get("stuck_open", 0)
        out.append(("sealed_zero_loss", bool(sealed["zero_loss"]), stuck, 0,
                    "%d stuck of %d" % (stuck, sealed.get("seen", 0)), "0 stuck"))
    cv = _get(v, "chain", "verdict") or {}
    oh = cv.get("one_head") or {}
    if "pass" in oh:
        out.append(("one_head", bool(oh["pass"]), oh.get("longest_episode_slots", 0),
                    oh.get("epoch_slots", 0),
                    "%s samples ok, %s divergence(s)" % (oh.get("ok_samples", "?"), oh.get("episodes", "?")),
                    "divergence < %s slots" % oh.get("epoch_slots", "?")))
    lv = cv.get("liveness") or {}
    if "pass" in lv:
        adv = list((lv.get("advance_slots") or {}).values())
        out.append(("liveness", bool(lv["pass"]), min(adv) if adv else 0, 0,
                    "min %s slots advanced" % (min(adv) if adv else 0), "> 0 on every node"))
    fin = cv.get("finality") or {}
    if "pass" in fin:
        stall = max([n.get("longest_stall_slots", 0) for n in (fin.get("nodes") or {}).values()] or [0])
        out.append(("finality", bool(fin["pass"]), fin.get("hash_checked_slots", 0),
                    fin.get("stall_limit_slots", 0),
                    "%s finalized, %d conflicts, stall %s" % (fin.get("hash_checked_slots", 0),
                                                              len(fin.get("conflicts") or []), stall),
                    "0 conflicts, stall < %s" % fin.get("stall_limit_slots", "?")))
    au = cv.get("authoring") or {}
    if "pass" in au:
        blocks, idle = au.get("blocks") or {}, au.get("idle") or []
        out.append(("authoring", bool(au["pass"]), len(blocks) - len(idle), len(blocks),
                    "%d of %d validators" % (len(blocks) - len(idle), len(blocks)), "every validator"))
    par = _get(v, "chain", "parity") or {}
    if "pass" in par:
        nodes = par.get("nodes") or {}
        digests = {n.get("digest") for n in nodes.values() if n.get("digest")}
        out.append(("state_parity", bool(par["pass"]), len(digests), 1,
                    "%d nodes, %d digest(s)" % (len(nodes), len(digests)), "one digest"))
    return out


def result(done, verdict, target, finished):
    """Everything the end of the soak knows."""
    t = Text()
    t.add("soak_phase", 1, {"phase": "done"})
    t.add("soak_load_seconds_left", 0)
    t.add("soak_finished_timestamp_seconds", finished)
    if done.get("started_ts"):
        t.add("soak_duration_seconds", round(finished - done["started_ts"], 1))
    t.add("soak_pass", 1 if done.get("pass") else 0)
    rows = checks(done, verdict, target)
    for c, ok, value, thr, measured, thr_text in rows:
        t.add("soak_check", 1 if ok else 0, {"check": c, "measured": measured, "threshold": thr_text})
    for c, _, value, _, _, _ in rows:
        t.add("soak_check_value", value, {"check": c})
    for c, _, _, thr, _, _ in rows:
        t.add("soak_check_threshold", thr, {"check": c})
    load = done.get("load") or {}
    for k in ("offered", "refused", "busy"):
        t.add("soak_orders_" + k, load.get(k))
    v = verdict or {}
    t.add("soak_orders_seen", v.get("orders_seen"))
    t.add("soak_orders_cleared", v.get("cleared"))
    t.add("soak_orders_missed", v.get("missed"))
    t.add("soak_sealed_seen", _get(v, "sealed", "seen"))
    t.add("soak_sealed_stuck", _get(v, "sealed", "stuck_open"))
    for q, key in (("0.5", "clear_latency_p50_s"), ("0.99", "clear_latency_p99_s")):
        t.add("soak_clear_latency_seconds", v.get(key), {"quantile": q})
    t.add("soak_finalized_slots", _get(v, "chain", "verdict", "finality", "hash_checked_slots"))
    for step in ("poll", "parity", "verdict"):
        rc = done.get(step)
        t.add("soak_step_exit_code", rc if isinstance(rc, int) else -1 if rc is not None else None,
              {"step": step})
    return t.text()
