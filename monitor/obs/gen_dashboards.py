#!/usr/bin/env python3
"""Generate the obs stack's provisioned Grafana dashboards (monitor/obs/dashboards/*.json).

    python3 monitor/obs/gen_dashboards.py          # write them
    python3 monitor/obs/gen_dashboards.py --check  # exit 1 if the JSON is stale

Four dashboards, each scoped by the variables net and run_id (every series carries both,
from `obs register`), each opening with a text panel that says what it answers, then
pass/fail stats whose titles carry their threshold, then the time series:

  obs-chain    Chain health: one chain, growing and finalizing, on every node
  obs-lasair   lasair validator duties: authoring, CE-133/134/135, assurances, audits
  obs-dex      DEX: offered load against placed, cleared and refused orders
  obs-memory   Memory: lasair's RSS and OCaml heap against finalized height

Metric names come from the code that exports them: lasair's bin/*.ml and jamnp/*.ml,
jamswap's offchain/{metrics,order_telemetry,server,loadgen,netwatch}.py. Runs appear as
annotations: `obs annotate` tags them with the run id, and pass/fail tags draw green/red.
"""
import json
import os
import sys

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboards")
DS = {"type": "prometheus", "uid": "prometheus"}
SEL = 'net="$net",run_id="$run_id"'
GREEN, RED, BLUE, GREY = "green", "red", "#5794F2", "text"

EPOCH_SLOTS = 12          # tiny; netwatch's DEFAULT_EPOCH_SLOTS, the one-epoch stall bound
MAX_LAG_SLOTS = 3         # netwatch's DEFAULT_MAX_LAG: a best block this far behind is one head
SLO_TARGET = 0.9999       # nets/soak.py / offchain/soak_verdict.py --target
MEM_KB_PER_BLOCK = 8      # lasair scripts/memory-soak.sh MAX_KB_PER_BLOCK


class Layout:
    """Panels placed left to right, wrapping at 24 columns; ids in order."""

    def __init__(self):
        self.panels, self.x, self.y, self.row_h, self.id = [], 0, 0, 0, 0

    def add(self, panel, w, h):
        if self.x + w > 24:
            self.x, self.y, self.row_h = 0, self.y + self.row_h, 0
        self.id += 1
        panel.update(id=self.id, gridPos={"x": self.x, "y": self.y, "w": w, "h": h})
        self.panels.append(panel)
        self.x += w
        self.row_h = max(self.row_h, h)

    def newline(self):
        if self.x:
            self.x, self.y, self.row_h = 0, self.y + self.row_h, 0

    def row(self, title):
        self.newline()
        self.add({"type": "row", "title": title, "collapsed": False, "panels": []}, 24, 1)
        self.newline()


def text(md):
    return {"type": "text", "title": "", "transparent": True,
            "options": {"mode": "markdown", "content": md}}


def _steps(pass_if, threshold):
    """Threshold steps coloring a value green when it passes, red when it fails."""
    if pass_if == "<=":
        return [{"color": GREEN, "value": None}, {"color": RED, "value": threshold + 1e-9}]
    if pass_if == ">=":
        return [{"color": RED, "value": None}, {"color": GREEN, "value": threshold}]
    if pass_if == ">":
        return [{"color": RED, "value": None}, {"color": GREEN, "value": threshold + 1e-9}]
    if pass_if == "==":            # == 0
        return [{"color": GREEN, "value": None}, {"color": RED, "value": threshold + 1e-9}]
    raise ValueError(pass_if)


def stat(title, expr, desc, pass_if=None, threshold=None, unit=None, decimals=0,
         calc="lastNotNull", mappings=None):
    """A stat over the dashboard's range (so a finished run still shows its last value).
    pass_if/threshold color it green/red; the title states the threshold."""
    fc = {"decimals": decimals, "noValue": "no data"}
    if pass_if:
        fc["color"] = {"mode": "thresholds"}
        fc["thresholds"] = {"mode": "absolute", "steps": _steps(pass_if, threshold)}
    else:
        fc["color"] = {"mode": "fixed", "fixedColor": GREY}
    if unit:
        fc["unit"] = unit
    if mappings:
        fc["mappings"] = mappings
    return {"type": "stat", "title": title, "description": desc, "datasource": DS,
            "targets": [{"expr": expr, "refId": "A", "range": True, "instant": False}],
            "fieldConfig": {"defaults": fc, "overrides": []},
            "options": {"graphMode": "area", "colorMode": "background", "textMode": "value",
                        "justifyMode": "center",
                        "reduceOptions": {"calcs": [calc], "fields": "", "values": False}}}


PASS_FAIL = [{"type": "value", "options": {"0": {"text": "FAIL", "color": RED},
                                           "1": {"text": "PASS", "color": GREEN}}}]


def ts(title, targets, desc="", unit=None, threshold=None, decimals=None, stack=False,
       overrides=None, min0=True, max=None):
    """targets: [(expr, legend), ...]. threshold: draws a dashed red line at that value."""
    d = {"custom": {"lineWidth": 2, "fillOpacity": 10 if stack else 0, "showPoints": "never",
                    "spanNulls": False,
                    "stacking": {"mode": "normal" if stack else "none", "group": "A"}},
         "color": {"mode": "palette-classic"}}
    if unit:
        d["unit"] = unit
    if decimals is not None:
        d["decimals"] = decimals
    if min0:
        d["min"] = 0
    if max is not None:
        d["max"] = max
    if threshold is not None:
        d["custom"]["thresholdsStyle"] = {"mode": "dashed"}
        d["thresholds"] = {"mode": "absolute", "steps": [{"color": "transparent", "value": None},
                                                         {"color": RED, "value": threshold}]}
    return {"type": "timeseries", "title": title, "description": desc, "datasource": DS,
            "targets": [{"expr": e, "legendFormat": lg, "refId": chr(65 + i), "range": True}
                        for i, (e, lg) in enumerate(targets)],
            "fieldConfig": {"defaults": d, "overrides": overrides or []},
            "options": {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                        "tooltip": {"mode": "multi", "sort": "desc"}}}


def right_axis(regex, unit=None):
    props = [{"id": "custom.axisPlacement", "value": "right"},
             {"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [6, 4]}}]
    if unit:
        props.append({"id": "unit", "value": unit})
    return {"matcher": {"id": "byRegexp", "options": regex}, "properties": props}


def zero_if_up(expr, job):
    """expr, or 0 while the run's `job` targets are scraped: counters an exporter creates
    lazily (at the first timeout, the first compaction) read 0, not "no data", until then;
    a run without that job still reads "no data"."""
    return '%s or on() (sum(up{%s,job="%s"}) * 0)' % (expr, SEL, job)


def per_min(metric, extra=""):
    return "60 * rate(%s{%s%s}[2m])" % (metric, SEL, extra)


def variables():
    def var(name, query, sort):
        return {"name": name, "label": name, "type": "query", "datasource": DS,
                "query": query, "definition": query, "refresh": 2, "sort": sort,
                "includeAll": False, "multi": False, "current": {}, "options": [],
                "hide": 0}
    # runs of one net sort newest first: run ids end in their UTC start time
    return [var("net", 'label_values(up{run_id!=""}, net)', 1),
            var("run_id", 'label_values(up{net="$net"}, run_id)', 2)]


def annotations():
    def ann(name, tags, color):
        return {"name": name, "enable": True, "hide": False, "iconColor": color,
                "datasource": {"type": "grafana", "uid": "-- Grafana --"},
                "target": {"type": "tags", "tags": tags, "matchAny": False, "limit": 500}}
    return {"list": [
        {"builtIn": 1, "name": "Annotations & Alerts", "enable": True, "hide": True,
         "iconColor": "rgba(0, 211, 255, 1)", "type": "dashboard",
         "datasource": {"type": "grafana", "uid": "-- Grafana --"}},
        ann("run events", ["$run_id", "event"], BLUE),
        ann("PASS", ["$run_id", "pass"], "green"),
        ann("FAIL", ["$run_id", "fail"], "red"),
    ]}


def dashboard(uid, title, layout):
    return {"uid": uid, "title": title, "tags": ["obs"], "timezone": "browser",
            "refresh": "10s", "time": {"from": "now-1h", "to": "now"}, "editable": False,
            "graphTooltip": 1, "schemaVersion": 39, "version": 1,
            "templating": {"list": variables()}, "annotations": annotations(),
            "links": [{"type": "dashboards", "tags": ["obs"], "asDropdown": False,
                       "includeVars": True, "keepTime": True, "title": "obs"}],
            "panels": layout.panels}


# ---- Chain health ----------------------------------------------------------------------
# finality lag per node, once the node has finalized past genesis (before that its
# finalized slot is 0 and the "lag" is the whole slot number): netwatch's, else lasair's
FIN_LAG = ("(jam_finality_lag_slots{%s} and on(node) (jam_finalized_slot{%s} > 0)) or on(node) "
           "(lasair_slot{%s} - (lasair_finalized_slot{%s} > 0))" % (SEL, SEL, SEL, SEL))

def chain():
    L = Layout()
    L.add(text(
        "**Is the net one chain that keeps growing and finalizing, on every node?** "
        "Each line is one node: its best and finalized blocks should climb together, "
        "finality lag should stay under one epoch (%d slots), and head agreement should read "
        "one head. PolkaJam nodes are read through netwatch's JIP-2 view, which counts in "
        "slots, not heights; lasair nodes also report their own heights, so a lasair-only run "
        "(no netwatch) fills these panels from lasair's metrics." % EPOCH_SLOTS), 24, 3)

    # netwatch where it runs, else lasair's own gauges ("A or on() B": B only when A is empty)
    one_head = ("min(jam_net_one_head{%s}) or on() "
                "((max(lasair_block_height{%s}) - min(lasair_block_height{%s})) <= bool %d)"
                % (SEL, SEL, SEL, MAX_LAG_SLOTS))
    fin_lag = "max(%s)" % FIN_LAG
    def moved(m):
        # a node's genesis 0 is not progress; a node with nothing else reads 0 (FAIL)
        return ("min((max_over_time(%s{%s}[5m]) - min_over_time((%s{%s} > 0)[5m:15s])) "
                "or (%s{%s} * 0))" % (m, SEL, m, SEL, m, SEL))
    fin_moved = "%s or on() %s" % (moved("jam_finalized_slot"), moved("lasair_finalized_slot"))
    L.add(stat("Targets down (not loadgen) · PASS = 0",
               'sum(1 - up{%s,job!="loadgen"})' % SEL,
               "Registered scrape targets that do not answer. The load generator is left out: "
               "it is stopped whenever load is off.", "==", 0), 5, 4)
    L.add(stat("One head · PASS = all nodes on one block (≤ %d slots behind)" % MAX_LAG_SLOTS,
               one_head,
               "netwatch's jam_net_one_head: every node up, one block at the common slot, none "
               "more than %d slots behind the newest. Without netwatch: lasair best heights "
               "within %d." % (MAX_LAG_SLOTS, MAX_LAG_SLOTS), ">=", 1, mappings=PASS_FAIL), 5, 4)
    L.add(stat("Finality lag, worst node · PASS ≤ %d slots" % EPOCH_SLOTS, fin_lag,
               "Best slot minus finalized slot on the node that lags most. netwatch's soak "
               "verdict fails a finality stall longer than one epoch (%d slots)." % EPOCH_SLOTS,
               "<=", EPOCH_SLOTS), 5, 4)
    L.add(stat("Finalized in 5 min, slowest node · PASS > 0 slots", fin_moved,
               "How far the slowest node's finalized block moved in the last 5 minutes. "
               "0 = finality stalled on some node.", ">", 0), 5, 4)
    L.add(stat("Finality conflicts · PASS = 0",
               "max(jam_finality_conflicts_total{%s})" % SEL,
               "Finalized slots seen with two different hashes (netwatch; a safety failure). "
               "No data without netwatch.", "==", 0), 4, 4)

    L.newline()
    L.add(ts("Best block per node (slot)",
             [("jam_best_slot{%s} or on(node) lasair_slot{%s}" % (SEL, SEL), "{{node}} · {{client}}")],
             "Slot of each node's best block. Lines climb together; one that flattens stopped "
             "importing.", decimals=0, min0=False), 12, 8)
    L.add(ts("Finalized block per node (slot)",
             [("(jam_finalized_slot{%s} or on(node) lasair_finalized_slot{%s}) > 0" % (SEL, SEL),
               "{{node}} · {{client}}")],
             "Slot of each node's finalized block (from its first finality on). Lines climb in "
             "steps; one that flattens stopped finalizing.", decimals=0, min0=False), 12, 8)
    L.add(ts("Finality lag per node (slots) · PASS ≤ %d" % EPOCH_SLOTS,
             [(FIN_LAG, "{{node}} · {{client}}")],
             "Best slot minus finalized slot per node, from its first finality on; the dashed "
             "line is one epoch.",
             threshold=EPOCH_SLOTS, decimals=0), 12, 8)
    L.add(ts("Head agreement",
             [("jam_net_heads{%s}" % SEL, "distinct heads at the common slot (1 = one head)"),
              ("jam_net_final_heads{%s}" % SEL, "distinct finalized blocks (1 = agree)"),
              ("max(jam_head_lag_slots{%s})" % SEL, "worst head lag (slots)"),
              ("(max(lasair_block_height{%s}) - min(lasair_block_height{%s})) and on() "
               "absent(jam_net_heads{%s})" % (SEL, SEL, SEL), "lasair best-height spread (no netwatch)")],
             "netwatch compares block hashes at the common slot across every node. Without "
             "netwatch, the spread of lasair best heights stands in.", threshold=MAX_LAG_SLOTS,
             decimals=0), 12, 8)
    L.add(ts("Height per node (lasair: best solid, finalized dashed)",
             [("lasair_block_height{%s}" % SEL, "{{node}} best"),
              ("lasair_finalized_height{%s}" % SEL, "{{node}} finalized")],
             "Blocks since genesis, as each lasair node counts them. JIP-2 (PolkaJam) reports "
             "no heights.", decimals=0,
             overrides=[{"matcher": {"id": "byRegexp", "options": ".* finalized$"},
                         "properties": [{"id": "custom.lineStyle",
                                         "value": {"fill": "dash", "dash": [6, 4]}}]}]), 12, 8)
    L.add(ts("Peers per node",
             [("jam_peers{%s}" % SEL, "{{node}} · {{client}} (JIP-2)"),
              ("lasair_peers_connected{%s}" % SEL, "{{node}} · lasair"),
              ("sum(jam_node_up{%s})" % SEL, "nodes answering netwatch")],
             "Connected peers each node reports, and how many nodes answer netwatch.",
             decimals=0), 12, 8)
    return dashboard("obs-chain", "Chain health", L)


# ---- lasair validator duties -----------------------------------------------------------
def lasair():
    L = Layout()
    L.add(text(
        "**Is every lasair validator doing its whole job?** Each line is one lasair node, in "
        "events per minute. Authoring, co-signing (CE-134), distributing guarantees (CE-135), "
        "guaranteeing (CE-133), assuring and auditing should all move while work flows; the "
        "panels titled *refusals* and *failures* should stay at zero."), 24, 3)
    silent = ("count(lasair_blocks_authored_total{%s}) - "
              "(count(increase(lasair_blocks_authored_total{%s}[5m]) > 0) or vector(0))" % (SEL, SEL))
    L.add(stat("Silent authors (none in 5 min) · PASS = 0", silent,
               "lasair nodes that authored no block in the last 5 minutes (~50 slots).", "==", 0), 4, 4)
    L.add(stat("CE-134 mismatched · PASS = 0", zero_if_up("sum(lasair_ce134_mismatched_total{%s})" % SEL, "lasair"),
               "Co-signers that refined the same package to a different report: non-determinism.",
               "==", 0), 4, 4)
    L.add(stat("Refine failures · PASS = 0", zero_if_up("sum(lasair_guarantor_refine_failed_total{%s})" % SEL, "lasair"),
               "Work-packages the guarantor pipeline failed to refine.", "==", 0), 4, 4)
    L.add(stat("Own blocks rejected · PASS = 0", zero_if_up("sum(lasair_authored_rejected_total{%s})" % SEL, "lasair"),
               "Blocks a lasair node authored and then rejected on its own import.", "==", 0), 4, 4)
    L.add(stat("Audit judgments rejected · PASS = 0",
               zero_if_up("sum(lasair_audit_judgments_rejected_total{%s})" % SEL, "lasair"),
               "Audit judgments received from other validators that failed validation.", "==", 0), 4, 4)
    L.add(stat("Work guaranteed (CE-133) · PASS > 0", "sum(lasair_ce133_guaranteed_total{%s})" % SEL,
               "Work-packages lasair guaranteed this run. 0 means no work reached a lasair "
               "guarantor (or none was sent).", ">", 0), 4, 4)

    L.newline()
    L.add(ts("Blocks authored per minute", [(per_min("lasair_blocks_authored_total"), "{{node}}")],
             "Blocks each lasair node sealed and published."), 8, 8)
    L.add(ts("CE-134 co-signing per minute",
             [(per_min("lasair_ce134_cosigned_total"), "{{node}} co-signed"),
              (per_min("lasair_ce134_answers_total"), "{{node}} answers received")],
             "co-signed: shares this node signed for another guarantor. answers: co-signatures "
             "other validators returned to it."), 8, 8)
    L.add(ts("CE-134 refusals per minute (should stay 0)",
             [("sum by (node, reason) (%s)" % per_min("lasair_ce134_refused_total"),
               "{{node}} refused: {{reason}}"),
              (per_min("lasair_ce134_answers_refused_total"), "{{node}} answers refused")],
             "refused: co-sign requests this node turned down, by reason. answers refused: "
             "requests it sent that others turned down."), 8, 8)
    L.add(ts("CE-133 guaranteed and CE-135 included per minute",
             [(per_min("lasair_ce133_guaranteed_total"), "{{node}} CE-133 guaranteed"),
              (per_min("lasair_ce135_included_total"), "{{node}} CE-135 included")],
             "CE-133: work-packages this node guaranteed. CE-135: guarantees it received that a "
             "block it authored included."), 8, 8)
    L.add(ts("Assurances per minute",
             [(per_min("lasair_assurances_signed_total"), "{{node}} signed"),
              (per_min("lasair_assurances_included_total"), "{{node}} included")],
             "signed: availability assurances this node made. included: assurances carried by "
             "blocks it authored."), 8, 8)
    L.add(ts("Audits per minute",
             [(per_min("lasair_audit_reports_audited_total"), "{{node}} reports audited")] +
             [("sum(%s)" % per_min("lasair_audit_judgments_%s_total" % k), "judgments %s (all nodes)" % k)
              for k in ("sent", "received", "rejected", "duplicate")],
             "Work-reports each node audited, and the audit judgments all lasair nodes sent and "
             "received (rejected and duplicate should stay near 0)."), 8, 8)
    L.add(ts("Guarantor pipeline",
             [("lasair_guarantor_ready{%s}" % SEL, "{{node}} ready"),
              ("lasair_guarantor_held{%s}" % SEL, "{{node}} held"),
              ("lasair_guarantor_busy{%s}" % SEL, "{{node}} busy")],
             "ready: packages refined and waiting to be guaranteed. held: packages held back. "
             "busy: 1 while a refine runs.", decimals=0), 12, 8)
    L.add(ts("Refine seconds (average per package)",
             [("rate(lasair_guarantor_refine_seconds_sum{%s}[5m]) / "
               "rate(lasair_guarantor_refine_seconds_count{%s}[5m])" % (SEL, SEL), "{{node}}")],
             "Mean refine time over 5 minutes: rate of the sum over rate of the count.",
             unit="s"), 12, 8)
    return dashboard("obs-lasair", "lasair validator duties", L)


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
    return dashboard("obs-dex", "DEX", L)


# ---- Memory ---------------------------------------------------------------------------
def memory():
    L = Layout()
    slope = ("deriv(lasair_mem_rss_bytes{%s}[30m]) / 1024 / deriv(lasair_finalized_height{%s}[30m])"
             % (SEL, SEL))
    L.add(text(
        "**Does lasair's memory stay bounded as the chain grows?** RSS and the OCaml heap "
        "should flatten while finalized height keeps climbing. The per-block slope is the "
        "memory soak's own verdict (scripts/memory-soak.sh: PASS ≤ %d KB of RSS per finalized "
        "block); tables that keep growing (tree entries, GRANDPA stores) point at what "
        "leaks." % MEM_KB_PER_BLOCK), 24, 3)
    L.add(stat("RSS per finalized block, 30 min slope, worst node · PASS ≤ %d KB (after warm-up)"
               % MEM_KB_PER_BLOCK,
               "max(%s)" % slope,
               "Least-squares slope of RSS over the last 30 minutes divided by the slope of "
               "finalized height. Early in a run it includes warm-up growth; the soak judges the "
               "second half.", "<=", MEM_KB_PER_BLOCK, unit="kbytes", decimals=1), 8, 4)
    L.add(stat("Peak RSS, any node (info)", "max(lasair_mem_rss_bytes{%s})" % SEL,
               "Highest RSS a lasair node reported in the time range.", unit="bytes", decimals=1,
               calc="max"), 5, 4)
    L.add(stat("OCaml heap, largest node (info)", "max(lasair_mem_ocaml_heap_bytes{%s})" % SEL,
               "Current major heap of the largest node.", unit="bytes", decimals=1), 5, 4)
    L.add(stat("GC compactions this run (info)",
               zero_if_up("sum(lasair_mem_gc_compact_seconds_count{%s})" % SEL, "lasair"),
               "Heap compactions all lasair nodes ran.", decimals=0), 6, 4)

    L.newline()
    L.add(ts("RSS per node", [("lasair_mem_rss_bytes{%s}" % SEL, "{{node}}")],
             "Resident set size (Linux /proc; absent on macOS).", unit="bytes"), 12, 8)
    L.add(ts("RSS against finalized height",
             [("lasair_mem_rss_bytes{%s}" % SEL, "{{node}} RSS"),
              ("lasair_finalized_height{%s}" % SEL, "{{node}} finalized height")],
             "RSS (left) and finalized height (right, dashed): bounded memory keeps the left "
             "flat while the right climbs.", unit="bytes",
             overrides=[right_axis(".* finalized height$", "none")]), 12, 8)
    L.add(ts("RSS per finalized block (KB, 30 min slope) · PASS ≤ %d" % MEM_KB_PER_BLOCK,
             [(slope, "{{node}}")], "The memory soak's measure, rolling.", unit="kbytes",
             threshold=MEM_KB_PER_BLOCK, min0=False), 12, 8)
    L.add(ts("OCaml heap and top heap",
             [("lasair_mem_ocaml_heap_bytes{%s}" % SEL, "{{node}} heap"),
              ("lasair_mem_ocaml_top_heap_bytes{%s}" % SEL, "{{node}} top heap")],
             "Major heap size now, and the largest it has been.", unit="bytes"), 12, 8)
    L.add(ts("OCaml live bytes", [("lasair_mem_ocaml_live_bytes{%s}" % SEL, "{{node}}")],
             "Live data after the last major collection.", unit="bytes"), 12, 8)
    L.add(ts("GC compaction (seconds each, compactions per 10 min)",
             [("rate(lasair_mem_gc_compact_seconds_sum{%s}[10m]) / rate(lasair_mem_gc_compact_seconds_count{%s}[10m])"
               % (SEL, SEL), "{{node}} seconds"),
              ("increase(lasair_mem_gc_compact_seconds_count{%s}[10m])" % SEL, "{{node}} compactions")],
             "Mean pause per heap compaction and how often it runs.", unit="s",
             overrides=[right_axis(".* compactions$", "none")]), 12, 8)
    L.add(ts("Tree entries (by node and table)", [("lasair_mem_tree_entries{%s}" % SEL, "{{node}} {{table}}")],
             "Entries in each in-memory block-tree table; one that keeps growing past "
             "finality leaks.", decimals=0), 12, 8)
    L.add(ts("GRANDPA stores (entries left, bytes right)",
             [('lasair_mem_grandpa{%s,store!~".*_bytes"}' % SEL, "{{node}} {{store}}"),
              ('lasair_mem_grandpa{%s,store=~".*_bytes"}' % SEL, "{{node}} {{store}}")],
             "Entries (left) and bytes (right, dashed) each GRANDPA store holds: "
             "justifications and warp fragments.", decimals=0,
             overrides=[right_axis(".*_bytes$", "bytes")]), 12, 8)
    return dashboard("obs-memory", "Memory", L)


def render_all():
    return {"obs-chain.json": chain(), "obs-lasair.json": lasair(), "obs-dex.json": dex(),
            "obs-memory.json": memory()}


def main(argv):
    out = {k: json.dumps(v, indent=1) + "\n" for k, v in render_all().items()}
    if "--check" in argv:
        stale = [k for k, v in out.items()
                 if not os.path.exists(os.path.join(OUT, k)) or open(os.path.join(OUT, k)).read() != v]
        if stale:
            sys.exit("stale dashboards: %s (run python3 monitor/obs/gen_dashboards.py)" % ", ".join(stale))
        return
    os.makedirs(OUT, exist_ok=True)
    for k, v in out.items():
        with open(os.path.join(OUT, k), "w") as fh:
            fh.write(v)
    print("wrote %s" % ", ".join(sorted(out)))


if __name__ == "__main__":
    main(sys.argv)
