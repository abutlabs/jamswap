#!/usr/bin/env python3
"""Write a soak run's report (REPORT.md) from its result directory.

    python3 soak/report.py DIR [--config config.json] [--run-id RUN_ID] > DIR/REPORT.md

DIR is what `./dex soak` (nets/soak.py) leaves in ~/.cache/jamswap/soak/<net>-<time>:
DONE (each step's result), verdict.txt (the order and chain checks), loadgen.txt (what
the load generator offered), config.json (what ran, with the run id); without it the
report says which settings it could not see. The report is plain markdown: what was
tested, each check with its threshold and what it means, and links to the run's
dashboards (the observability stack's `obs link`, the "Soak runs" dashboard first).
"""
import argparse, json, os, re, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "nets"))
import obsnet  # noqa: E402

# Every check the soak makes: (label in verdict.txt, threshold, what it tells you).
CHECKS = [
    ("clearing SLO", "≥ 0.9999",
     "Share of orders that could trade (met a clearing price) and did. The DEX's main promise."),
    ("SEALED zero-loss", "0 stuck",
     "Every sealed (hidden) order reached an end state: none lost in the commit/reveal path."),
    ("one head", "every sample",
     "All nodes agree on one chain head (sampled every few seconds)."),
    ("liveness", "blocks advance",
     "Every node kept producing or importing blocks for the whole run."),
    ("finality", "0 conflicts, stall < 1 epoch",
     "Blocks were finalized, never two different ones at the same height, never going backwards."),
    ("authoring (pi)", "every validator",
     "Every validator authored blocks (lasair nodes are real block producers)."),
    ("state parity", "all digests agree",
     "At one finalized block, every node holds byte-identical DEX state."),
]


def line(verdict, label):
    for ln in verdict.splitlines():
        if ln.startswith(label):
            return ln.split(":", 1)[1].strip()
    return None


def verdict_word(text):
    if text is None:
        return "—"
    return "PASS" if "PASS" in text else ("FAIL" if "FAIL" in text else "—")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("--config", default=None)
    ap.add_argument("--run-id", default=None)
    a = ap.parse_args()
    d = a.dir.rstrip("/")
    done = json.load(open(os.path.join(d, "DONE"))) if os.path.exists(os.path.join(d, "DONE")) else {}
    verdict = open(os.path.join(d, "verdict.txt")).read() if os.path.exists(os.path.join(d, "verdict.txt")) else ""
    cfg_path = a.config or os.path.join(d, "config.json")
    cfg = json.load(open(cfg_path)) if os.path.exists(cfg_path) else {}
    run_id = a.run_id or cfg.get("run_id")
    load = done.get("load", {})
    overall = done.get("pass")
    net = done.get("net") or cfg.get("net", "?")

    out = []
    w = out.append
    w("# Soak report: %s, %s" % (net, done.get("started", os.path.basename(d))))
    w("")
    w("**Result: %s**" % ("PASS" if overall else ("FAIL" if overall is False else "unknown (no DONE file)")))
    if load:
        w("  ·  %d orders offered, %d turned away" % (load.get("offered", 0), load.get("turned_away", 0)))
    w("")
    w("## What was tested")
    w("")
    w("| | |")
    w("|---|---|")
    rows = [
        ("Net", "%s (%s)" % (net, cfg.get("clients", "see nets/profiles.py"))),
        ("Validators", cfg.get("validators", "—")),
        ("lasair image", cfg.get("lasair_image", "not recorded")),
        ("Durable storage", cfg.get("data_dir") or "off (memory only)"),
        ("DEX backend", cfg.get("dex_backend", "not recorded")),
        ("Load", cfg.get("load", "PROFILE=trading, RATE=12 pairs/min, SEALED_RATIO=0.2 (defaults)")),
        ("Duration", "%s s of load, then %s s to drain" % (done.get("secs", "?"), done.get("drain", "?"))),
        ("Started / finished (UTC)", "%s / %s" % (done.get("started", "?"), done.get("finished", "?"))),
        ("jamswap commit", cfg.get("jamswap_commit", "not recorded")),
        ("Run ID (dashboards)", run_id or "not recorded"),
    ]
    for k, v in rows:
        w("| %s | %s |" % (k, v))
    w("")
    w("## Results")
    w("")
    w("| Check | Threshold | Result | Detail | What it means |")
    w("|---|---|---|---|---|")
    off = "offered load"
    if load:
        det = "%d offered, %d refused, %d busy" % (load.get("offered", 0), load.get("refused", 0), load.get("busy", 0))
        w("| %s | ≤ 0.01%% turned away | %s | %s | Orders the DEX refused or had to shed. |"
          % (off, "PASS" if load.get("pass") else "FAIL", det))
    for label, thr, meaning in CHECKS:
        t = line(verdict, label)
        if t is None:
            continue
        detail = re.sub(r"\s*(PASS|FAIL)\s*", " ", t).strip()
        detail = detail.replace("|", "/")[:110]
        w("| %s | %s | %s | %s | %s |" % (label, thr, verdict_word(t), detail, meaning))
    lat = line(verdict, "clear latency")
    if lat:
        w("| clear latency | (information) | — | %s | Time from placing an order to its settlement. |" % lat)
    w("")
    reasons = []
    lg = os.path.join(d, "loadgen.log")
    if os.path.exists(lg):
        counts = {}
        for ln in open(lg, errors="replace"):
            m = re.search(r"op (\w+) failed: (.*)", ln)
            if m:
                why = re.sub(r"\d+", "N", m.group(2).split(": ", 1)[-1])[:100]
                counts[why] = counts.get(why, 0) + 1
        reasons = sorted(counts.items(), key=lambda x: -x[1])[:5]
    if reasons:
        w("**Why orders were refused** (from the load generator's log):")
        w("")
        for why, n in reasons:
            w("- %d × %s" % (n, why))
        w("")
    w("## Dashboards")
    w("")
    links = obsnet.links(run_id) if run_id else []
    if links:
        # the soak's own dashboard first, then the others in the stack's order
        for name, u in sorted(links, key=lambda x: not x[0].endswith("Soak runs")):
            w("- [%s](%s)" % (name, u))
        w("")
        w("The links open the observability stack's Grafana on this machine (`./dex obs up`).")
    elif run_id:
        w("Run ID %s; no observability stack found (%s), so no links." % (run_id, obsnet.OBS_HOME))
    else:
        w("No run ID was recorded, so there are no dashboard links (runs before the obs stack).")
    w("")
    w("## Reproduce")
    w("")
    env = []
    if cfg.get("lasair_image"):
        env.append("LASAIR_IMAGE=%s" % cfg["lasair_image"])
    if cfg.get("data_dir"):
        env.append("LASAIR_DATA_DIR=%s" % cfg["data_dir"])
    w("```")
    w("%ssoak/run %s %s" % ((" ".join(env) + " ") if env else "", net, done.get("secs", 600)))
    w("```")
    w("")
    w("Raw files: `%s` (verdict.txt, DONE, loadgen.log, dex.log, chain.jsonl, parity.json)." % d)
    print("\n".join(out))


if __name__ == "__main__":
    main()
