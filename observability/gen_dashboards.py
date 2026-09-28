#!/usr/bin/env python3
"""Generate jamswap's Grafana dashboards (observability/dashboards/*.json).

    python3 observability/gen_dashboards.py          # write them
    python3 observability/gen_dashboards.py --check  # exit 1 if the JSON is stale

The observability stack (the abutlabs/observability repo, next to this checkout or at
$OBS_HOME) mounts observability/dashboards/ as the Grafana folder "jamswap". This script
uses its dashboard helpers (dashgen/obsdash.py); Grafana itself only reads the JSON,
so running a net never needs this script.

  obs-dex        DEX: offered load against placed, cleared and refused orders
  obs-soak-runs  Soak runs: one soak's configuration, progress and verdict, and every
                 soak so far side by side (nets/soak.py pushes the results; soak/README.md)

Metric names come from the code that exports them: offchain/{metrics,order_telemetry,
server,loadgen}.py for the DEX, nets/soak_metrics.py for the soak results.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
OUT = os.path.join(HERE, "dashboards")
OBS_HOME = os.environ.get("OBS_HOME") or os.path.join(os.path.dirname(REPO), "observability")
sys.path.insert(0, os.path.join(OBS_HOME, "dashgen"))
sys.path.insert(0, os.path.join(REPO, "nets"))
try:
    from obsdash import (GREEN, PASS_FAIL, PROM, RED, SEL, Layout, dashboard, right_axis,  # noqa: E402
                         stat, table, text, ts, variables, write_all)
except ImportError:                          # the tests skip without it
    obsdash_missing = "dashgen/obsdash.py not found under OBS_HOME=%s" % OBS_HOME
else:
    obsdash_missing = None
import soak_metrics  # noqa: E402

SLO_TARGET = 0.9999       # nets/soak.py / offchain/soak_verdict.py --target


def zero_if_up(expr, job):
    """expr, or 0 while the run's `job` targets are scraped: counters an exporter creates
    lazily read 0, not "no data", until then; a run without that job still reads "no data"."""
    return '%s or on() (sum(up{%s,job="%s"}) * 0)' % (expr, SEL, job)


# ---- DEX ------------------------------------------------------------------------------
def dex():
    L = Layout()
    L.add(text(
        "**Is the DEX turning offered load into cleared trades?** The load generator's offered "
        "operations (per second, by op) should be met by placed and cleared orders; its errors "
        "and busy replies are load the DEX turned away. The headline is the clearing SLO, the "
        "soak's own verdict (PASS ≥ %s): the share of marketable orders that cleared." % SLO_TARGET),
          24, 3)
    turned = ("((sum(loadgen_op_errors_total{%s}) or vector(0)) + "
              "(sum(loadgen_ops_busy_total{%s}) or vector(0))) / sum(loadgen_ops_total{%s})"
              % (SEL, SEL, SEL))
    L.add(stat("Clearing SLO · PASS ≥ %s" % SLO_TARGET, "min(jamswap_order_clearing_slo{%s})" % SEL,
               "Marketable orders that cleared over all that reached a terminal state "
               "(offchain/order_telemetry.py).", ">=", SLO_TARGET, unit="percentunit",
               decimals=3), 5, 4)
    L.add(stat("Load turned away · PASS ≤ %g%%" % (100 * (1 - SLO_TARGET)), turned,
               "Errors plus busy (503) replies over operations offered, as the soak judges the "
               "offered load. Counts restart when the load generator restarts.",
               "<=", 1 - SLO_TARGET, unit="percentunit", decimals=2), 5, 4)
    L.add(stat("Settle timeouts · PASS = 0",
               zero_if_up("sum(jamswap_settle_timeouts_total{%s})" % SEL, "dex"),
               "Submitted operations whose effect never became visible on chain.", "==", 0), 5, 4)
    L.add(stat("Settlements reverted · PASS = 0",
               zero_if_up("sum(jamswap_settle_reverted_total{%s})" % SEL, "dex"),
               "Settlements seen on chain and then erased by a re-org before the hold window.",
               "==", 0), 5, 4)
    L.add(stat("Clear latency p99, 10 min (info)",
               "histogram_quantile(0.99, sum by (le) (rate(jamswap_order_clear_latency_seconds_bucket{%s}[10m])))"
               % SEL, "Placement to durable fill, 99th percentile.", unit="s", decimals=0), 4, 4)

    L.newline()
    L.add(ts("Offered load (ops/s, by op)",
             [("sum by (op) (rate(loadgen_ops_total{%s}[1m]))" % SEL, "{{op}}")],
             "Operations the load generator offered to the DEX API.", stack=True), 8, 8)
    L.add(ts("Turned away (ops/s, by op)",
             [(zero_if_up("(sum(rate(loadgen_op_errors_total{%s}[1m])) or vector(0)) + "
                          "(sum(rate(loadgen_ops_busy_total{%s}[1m])) or vector(0))" % (SEL, SEL),
                          "loadgen"), "all ops"),
              ("sum by (op) (rate(loadgen_op_errors_total{%s}[1m]))" % SEL, "{{op}} error"),
              ("sum by (op) (rate(loadgen_ops_busy_total{%s}[1m]))" % SEL, "{{op}} busy (503)")],
             "error: the DEX refused or failed the call. busy: the chain was at capacity."), 8, 8)
    L.add(ts("Orders placed and terminal (per s)",
             [("sum(rate(jamswap_order_placed_total{%s}[1m]))" % SEL, "placed"),
              ("sum by (outcome) (rate(jamswap_order_terminal_total{%s}[1m]))" % SEL, "{{outcome}}")],
             "placed: orders the DEX accepted. Terminal outcomes: filled clears; expired is the "
             "failure the SLO counts."), 8, 8)
    L.add(ts("Clearing SLO", [("jamswap_order_clearing_slo{%s}" % SEL, "clearing SLO")],
             "The soak's headline, live. Dashed: the target.", unit="percentunit",
             threshold=SLO_TARGET, min0=False, max=1, decimals=4), 8, 8)
    L.add(ts("Clearing latency (s)",
             [("histogram_quantile(%s, sum by (le) (rate(jamswap_order_clear_latency_seconds_bucket{%s}[5m])))"
               % (q, SEL), "p%s" % int(float(q) * 100)) for q in ("0.5", "0.99")] +
             [("histogram_quantile(0.5, sum by (le, op) (rate(jamswap_settle_latency_seconds_bucket{%s}[5m])))"
               % SEL, "settle p50 {{op}}")],
             "Order placement to durable fill (p50, p99), and submit to visible-on-chain per op.",
             unit="s"), 8, 8)
    L.add(ts("Refused (per min, by op)",
             [("sum by (op) (60 * rate(jamswap_refused_total{%s}[2m]))" % SEL, "{{op}} refused by every guarantor"),
              ('sum by (route) (60 * rate(jamswap_api_requests_total{%s,code=~"5.."}[2m]))' % SEL,
               "{{route}} 5xx")],
             "Submissions every guarantor refused (CE-133 queue at cap), and API server errors."), 8, 8)
    L.add(ts("Round sizes",
             [("sum by (market) (rate(jamswap_round_orders_sum{%s}[5m])) / "
               "sum by (market) (rate(jamswap_round_orders_count{%s}[5m]))" % (SEL, SEL),
               "market {{market}} orders per round"),
              ("jamswap_inflight_orders{%s}" % SEL, "market {{market}} in flight"),
              ("jamswap_mempool_orders{%s}" % SEL, "market {{market}} in mempool")],
             "Mean orders per assembled round (sealed + public), orders inside a round awaiting "
             "settlement, and orders waiting for the next round."), 8, 8)
    L.add(ts("Rounds abandoned and late-landed (per 10 min)",
             [(zero_if_up("sum(increase(jamswap_round_abandoned_total{%s}[10m]))" % SEL, "dex"),
               "abandoned (all reasons)"),
              ("sum by (reason) (increase(jamswap_round_abandoned_total{%s}[10m]))" % SEL, "abandoned: {{reason}}"),
              (zero_if_up("sum(increase(jamswap_round_late_landed_total{%s}[10m]))" % SEL, "dex"),
               "late landed")],
             "Rounds released without settling, by reason, and released rounds that settled after all.",
             decimals=0), 8, 8)
    L.add(ts("Treasury reserve (JAMKB)",
             [("jamswap_treasury_jamkb_atomic{%s} / 1e4" % SEL, "reserve"),
              ("jamswap_treasury_reserve_target_atomic{%s} / 1e4" % SEL, "target"),
              ("sum(jamswap_reserve_topups_total{%s})" % SEL, "top-ups sent (count)")],
             "The treasury's JAMKB reserve against its target (display units), and the top-ups "
             "the keeper sent.", overrides=[right_axis("top-ups.*")]), 8, 8)
    # the DEX's runs are the ones with scraped targets (the dex, the load generator)
    return dashboard("obs-dex", "DEX", L, variables_=variables('up{run_id!=""}'))


# ---- Soak runs ------------------------------------------------------------------------
RUN = 'run_id="$run_id"'
ABOUT = """**A soak** runs a real JAM network for a long time under steady trading load, then \
checks that nothing was lost and the chain stayed healthy (`soak/run NET SECS`; \
`soak/README.md`). The load generator offers 12 crossing order pairs a minute, 20% of them \
sealed; the DEX batches them into rounds, each a work-package the validators guarantee, \
assure, audit and accumulate; after SECS of load, 180 s of drain let the last rounds settle; \
then every order's fate and the chain on every node are judged.

**Read it top down.** Pick a net and a run: its verdict, phase and configuration first, then \
one tile per check, green when it passed, each showing what was measured and what it had to \
reach. A run passes only if every check passes. The links open the chain, lasair, DEX, memory \
and log dashboards for the same run. At the bottom, every soak so far, newest first: click a \
run id to open it here with its time range.

| Check | Passes when |
|---|---|
| offered load | ≤ 0.01% of the offered orders turned away (refused or busy) |
| clearing SLO | ≥ 0.9999 of the orders that could trade did |
| SEALED zero-loss | no sealed order stuck between commit and reveal |
| one head | every node agrees on the head, at every sample |
| liveness | every node's best block advances |
| finality | blocks finalized, no conflict, no stall of an epoch or more |
| authoring | every validator authored blocks |
| state parity | every node holds byte-identical DEX state at one finalized block |"""


def check_tile(check, title, desc):
    p = stat(title, 'max by (check, measured, threshold) (soak_check{%s,check="%s"})' % (RUN, check),
             desc, mappings=PASS_FAIL, instant=True, legend="{{measured}} · need {{threshold}}",
             text_mode="value_and_name")
    p["fieldConfig"]["defaults"]["color"] = {"mode": "thresholds"}
    p["fieldConfig"]["defaults"]["thresholds"] = {"mode": "absolute", "steps": [
        {"color": RED, "value": None}, {"color": GREEN, "value": 1}]}
    p["options"]["text"] = {"titleSize": 13, "valueSize": 28}
    return p


def soak_runs():
    def var(name, query, sort):
        return {"name": name, "label": name, "type": "query", "datasource": PROM,
                "query": query, "definition": query, "refresh": 2, "sort": sort,
                "includeAll": False, "multi": False, "current": {}, "options": [], "hide": 0}
    variables_ = [var("net", "label_values(soak_info, net)", 1),
                  var("run_id", 'label_values(soak_info{net="$net"}, run_id)', 2)]
    L = Layout()
    L.add(text(ABOUT), 24, 13)

    L.row("The selected run")
    verdict = stat("Verdict", "max(soak_pass{%s})" % RUN,
                   "PASS only if every check below passed. Empty while the soak runs.",
                   mappings=PASS_FAIL, instant=True)
    verdict["fieldConfig"]["defaults"]["noValue"] = "running"
    L.add(verdict, 4, 4)
    L.add(stat("Phase", "max by (phase) (soak_phase{%s})" % RUN,
               "starting, load, drain, parity, verdict, done.", instant=True, legend="{{phase}}",
               text_mode="name"), 4, 4)
    L.add(stat("Load left", "max(soak_load_seconds_left{%s})" % RUN,
               "Seconds of load still to run (0 once draining).", unit="s", instant=True), 4, 4)
    L.add(stat("Duration", "max(soak_duration_seconds{%s}) or (time() - max(soak_started_timestamp_seconds{%s}))"
               % (RUN, RUN), "The whole soak, start to verdict (running: so far).", unit="s",
               instant=True), 4, 4)
    L.add(stat("Last report", "time() - max(push_time_seconds{job=\"soak\",%s})" % RUN,
               "Seconds since the soak last pushed. A running soak reports every minute; a "
               "large value on an unfinished run means it stopped.", unit="s", instant=True), 4, 4)
    L.add(stat("Orders offered", "max(soak_orders_offered{%s})" % RUN,
               "Operations the load generator offered.", instant=True), 4, 4)

    L.add(table("Configuration", [("max by (clients, lasair_image, data_dir, dex_backend, load_profile, "
                                   "load_rate, sealed_ratio, jamswap_commit, secs, drain) (soak_info{%s})" % RUN, "A")],
                "What ran: soak_info, pushed when the soak starts.",
                transformations=[{"id": "organize", "options": {
                    "excludeByName": {"Time": True, "Value": True},
                    "renameByName": {"lasair_image": "lasair image", "data_dir": "data dir",
                                     "dex_backend": "DEX backend", "load_profile": "load profile",
                                     "load_rate": "pairs/min", "sealed_ratio": "sealed",
                                     "jamswap_commit": "jamswap", "secs": "load s",
                                     "drain": "drain s"}}}]), 24, 4)

    L.row("Checks: green passed, red failed (measured · what it had to reach)")
    for name, title, desc in soak_metrics.CHECKS:
        L.add(check_tile(name, title, desc), 6, 4)
    L.add(stat("Clear latency p50", 'max(soak_clear_latency_seconds{%s,quantile="0.5"})' % RUN,
               "Placement to settlement, median (information).", unit="s", instant=True), 6, 4)
    L.add(stat("Clear latency p99", 'max(soak_clear_latency_seconds{%s,quantile="0.99"})' % RUN,
               "Placement to settlement, 99th percentile (information).", unit="s", instant=True), 6, 4)
    L.add(stat("Refused / busy", "max(soak_orders_refused{%s})" % RUN,
               "Orders the DEX refused (busy replies in the offered-load tile).", instant=True), 6, 4)
    L.add(stat("Finalized slots checked", "max(soak_finalized_slots{%s})" % RUN,
               "Finalized slots netwatch hash-checked across every node.", instant=True), 6, 4)

    L.add(text(
        "**This run's dashboards** (same net, run and time range): "
        "[Chain health](/d/obs-chain?orgId=1&var-net=$net&var-run_id=$run_id&${__url_time_range}) · "
        "[Network overview](/d/obs-overview?orgId=1&var-net=$net&var-run_id=$run_id&${__url_time_range}) · "
        "[lasair validator duties](/d/obs-lasair?orgId=1&var-net=$net&var-run_id=$run_id&${__url_time_range}) · "
        "[DEX](/d/obs-dex?orgId=1&var-net=$net&var-run_id=$run_id&${__url_time_range}) · "
        "[Memory](/d/obs-memory?orgId=1&var-net=$net&var-run_id=$run_id&${__url_time_range}) · "
        "[Logs](/d/obs-logs?orgId=1&var-net=$net&var-run_id=$run_id&${__url_time_range})",
        "Open"), 24, 3)

    L.row("Every soak so far")
    by = "max by (run_id, net)"
    hist = table("Soak runs, newest first", [
        ("max by (run_id, net, clients, lasair_image) (soak_info)", "A"),
        ("%s (soak_started_timestamp_seconds) * 1000" % by, "B"),
        ("%s (soak_duration_seconds)" % by, "C"),
        ('%s (soak_check_value{check="clearing_slo"})' % by, "D"),
        ("%s (soak_orders_refused)" % by, "E"),
        ("%s (soak_sealed_stuck)" % by, "F"),
        ('%s (soak_clear_latency_seconds{quantile="0.5"})' % by, "G"),
        ('%s (soak_clear_latency_seconds{quantile="0.99"})' % by, "H"),
        ("%s (soak_pass)" % by, "I"),
        ("%s (soak_started_timestamp_seconds) * 1000 - 60000" % by, "J"),
        ("%s ((soak_finished_timestamp_seconds or (soak_started_timestamp_seconds + soak_load_seconds "
         "+ soak_drain_seconds + 900)) * 1000 + 60000)" % by, "K")],
        "Every soak the Pushgateway holds (nets/soak.py pushes one group per run). Click a run "
        "id to open it above with its time range.",
        transformations=[
            {"id": "merge", "options": {}},
            {"id": "organize", "options": {
                "excludeByName": {"Time": True, "Value #A": True},
                "indexByName": {"Value #B": 0, "run_id": 1, "net": 2, "clients": 3, "lasair_image": 4,
                                "Value #C": 5, "Value #D": 6, "Value #E": 7, "Value #F": 8,
                                "Value #G": 9, "Value #H": 10, "Value #I": 11, "Value #J": 12,
                                "Value #K": 13},
                "renameByName": {"Value #B": "started", "lasair_image": "lasair image",
                                 "Value #C": "duration", "Value #D": "clearing SLO",
                                 "Value #E": "refused", "Value #F": "sealed stuck",
                                 "Value #G": "latency p50", "Value #H": "latency p99",
                                 "Value #I": "result", "Value #J": "from", "Value #K": "to"}}}],
        overrides=[
            {"matcher": {"id": "byName", "options": "started"},
             "properties": [{"id": "unit", "value": "dateTimeAsIso"}, {"id": "custom.width", "value": 165}]},
            {"matcher": {"id": "byName", "options": "run_id"},
             "properties": [{"id": "custom.width", "value": 270}]},
            {"matcher": {"id": "byName", "options": "net"},
             "properties": [{"id": "custom.width", "value": 90}]},
            {"matcher": {"id": "byRegexp", "options": "^(clients|lasair image)$"},
             "properties": [{"id": "custom.width", "value": 150}]},
            {"matcher": {"id": "byRegexp",
                         "options": "^(duration|clearing SLO|refused|sealed stuck|latency p50|latency p99|result)$"},
             "properties": [{"id": "custom.width", "value": 106}]},
            {"matcher": {"id": "byName", "options": "duration"},
             "properties": [{"id": "unit", "value": "s"}]},
            {"matcher": {"id": "byRegexp", "options": "latency.*"},
             "properties": [{"id": "unit", "value": "s"}, {"id": "decimals", "value": 0}]},
            {"matcher": {"id": "byName", "options": "clearing SLO"},
             "properties": [{"id": "decimals", "value": 4},
                            {"id": "custom.cellOptions", "value": {"type": "color-text"}},
                            {"id": "thresholds", "value": {"mode": "absolute", "steps": [
                                {"color": RED, "value": None}, {"color": GREEN, "value": SLO_TARGET}]}}]},
            {"matcher": {"id": "byName", "options": "result"},
             "properties": [{"id": "mappings", "value": PASS_FAIL},
                            {"id": "custom.cellOptions", "value": {"type": "color-background"}}]},
            {"matcher": {"id": "byRegexp", "options": "^(from|to)$"},
             "properties": [{"id": "custom.hidden", "value": True}]},
            {"matcher": {"id": "byName", "options": "run_id"},
             "properties": [{"id": "links", "value": [{
                 "title": "Open this run",
                 "url": "/d/obs-soak-runs?orgId=1&var-net=${__data.fields.net}&var-run_id=${__value.raw}"
                        "&from=${__data.fields.from}&to=${__data.fields.to}"}]}]}],
        sort=("started", True))
    L.add(hist, 24, 10)
    return dashboard("obs-soak-runs", "Soak runs", L, variables_=variables_, refresh="30s",
                     time_from="now-7d")


def render_all():
    return {"obs-dex.json": dex(), "obs-soak-runs.json": soak_runs()}


if __name__ == "__main__":
    if obsdash_missing:
        sys.exit(obsdash_missing + ": check out abutlabs/observability beside jamswap, or set OBS_HOME")
    write_all(OUT, render_all(), sys.argv)
