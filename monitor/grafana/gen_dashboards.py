#!/usr/bin/env python3
"""Generate the provisioned Grafana dashboards: network, clients, node and finality views
(the chain), and the service and accounts views (the DEX).

Regenerate after editing:  python3 monitor/grafana/gen_dashboards.py
Panel boilerplate lives here once; the dashboards stay consistent by
construction. Colors are the CVD-validated per-entity palette — an entity
keeps its hue on every panel of every dashboard.

The chain views are CLIENT-NEUTRAL (issue #15): every panel reads the jam_* series
offchain/netwatch.py exports for any node — JIP-2 nodes (hashes) and nodes read through
their metrics (lasair until lasair#68) alike — labelled {node, client}, plus the
on-chain validator statistics (jam_pi_*). So the same dashboards run unchanged on
lasair6, on an all-PolkaJam net and on a mixed one. Client-specific instrumentation
(lasair's native lasair_* gauges) lives in collapsed "lasair overlay" rows: optional
detail that is simply empty on a net without lasair.
"""
import json
import os

OUT = os.path.join(os.path.dirname(__file__), "dashboards")

# fixed per-entity palette (validated against Grafana's dark surface)
NODE_COLOR = {
    "pj0": "#3987e5", "pj1": "#199e70", "pj2": "#c98500",   # PolkaJam: blue aqua yellow
    "lm3": "#008300", "lm4": "#9085e9", "lm5": "#e66767",   # lasair:   green violet red
}
CLIENT_COLOR = {"polkajam": "#3987e5", "lasair": "#c98500", "javajam": "#199e70",
                "pbnjam": "#9085e9"}
ROLE = {"authored": "#3987e5", "imported": "#199e70", "rejected": "#e66767",
        "tickets": "#9085e9", "neutral": "#c98500"}

DS = {"type": "prometheus", "uid": "prometheus"}
_id = [0]


def nid():
    _id[0] += 1
    return _id[0]


def override(name, color):
    return {"matcher": {"id": "byName", "options": name},
            "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": color}}]}


def override_prefix(prefix, color):
    # legends read "<node> · <client>": match the series of one node whatever its client
    return {"matcher": {"id": "byRegexp", "options": "^%s( |$)" % prefix},
            "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": color}}]}


def stat(title, expr, x, w, y=0, color="text", thresholds=None, unit=None, mappings=None):
    fc = {"decimals": 0}
    if thresholds:
        fc["color"] = {"mode": "thresholds"}
        fc["thresholds"] = {"mode": "absolute", "steps": thresholds}
    else:
        fc["color"] = {"mode": "fixed", "fixedColor": color}
    if unit:
        fc["unit"] = unit
    if mappings:
        fc["mappings"] = mappings
    return {"type": "stat", "title": title, "id": nid(),
            "gridPos": {"x": x, "y": y, "w": w, "h": 4}, "datasource": DS,
            "targets": [{"expr": expr, "instant": True, "refId": "A"}],
            "fieldConfig": {"defaults": fc, "overrides": []},
            "options": {"graphMode": "none", "colorMode": "value", "textMode": "value"}}


def ts(title, targets, x, y, w, h=8, overrides=None, unit=None, fill=0, minzero=True):
    defaults = {"custom": {"lineWidth": 2, "fillOpacity": fill, "pointSize": 4,
                           "showPoints": "never"},
                "color": {"mode": "palette-classic"}}
    if unit:
        defaults["unit"] = unit
    if minzero:
        defaults["min"] = 0
    return {"type": "timeseries", "title": title, "id": nid(),
            "gridPos": {"x": x, "y": y, "w": w, "h": h}, "datasource": DS,
            "targets": [dict(t, refId=chr(65 + i)) for i, t in enumerate(targets)],
            "fieldConfig": {"defaults": defaults, "overrides": overrides or []},
            "options": {"legend": {"displayMode": "list", "placement": "bottom",
                                   "showLegend": True},
                        "tooltip": {"mode": "multi", "sort": "desc"}}}


def bargauge(title, targets, x, y, w, h=8, overrides=None):
    return {"type": "bargauge", "title": title, "id": nid(),
            "gridPos": {"x": x, "y": y, "w": w, "h": h}, "datasource": DS,
            "targets": [dict(t, refId=chr(65 + i), instant=True) for i, t in enumerate(targets)],
            "fieldConfig": {"defaults": {"decimals": 0, "min": 0,
                                         "color": {"mode": "fixed", "fixedColor": "text"}},
                            "overrides": overrides or []},
            "options": {"orientation": "horizontal", "displayMode": "basic",
                        "showUnfilled": True, "namePlacement": "left",
                        "reduceOptions": {"calcs": ["lastNotNull"]}}}


LASAIR_OVERLAY = "lasair overlay — lasair's native lasair_* metrics (empty on a net without lasair)"


def overlay(y, panels, title=LASAIR_OVERLAY):
    """A collapsed row at `y` holding client-specific panels, laid out from y=0 and placed
    under the row when it is opened."""
    for p in panels:
        p["gridPos"]["y"] += y + 1
    return {"type": "row", "title": title, "id": nid(), "collapsed": True,
            "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "panels": panels}


def dashboard(uid, title, panels, templating=None):
    d = {"uid": uid, "title": title, "timezone": "browser", "refresh": "5s",
         "time": {"from": "now-30m", "to": "now"}, "editable": True,
         "panels": panels, "schemaVersion": 39, "version": 1}
    if templating:
        d["templating"] = {"list": templating}
    return d


node_overrides = [override_prefix(n, c) for n, c in NODE_COLOR.items()]
lm_overrides = [override(n, NODE_COLOR[n]) for n in ("lm3", "lm4", "lm5")]
client_overrides = [override(c, col) for c, col in CLIENT_COLOR.items()]
NODE_LEGEND = "{{node}} · {{client}}"
ONE_HEAD_MAP = [{"type": "value", "options": {"0": {"text": "DIVERGED", "color": "red"},
                                              "1": {"text": "ONE HEAD", "color": "green"}}}]

# ═══════════════ NETWORK VIEW ═══════════════
# One row of verdicts, then per-node lag and agreement, then what consensus credited each
# validator with (GP pi). Heads are compared by hash where a node serves JIP-2.
network = dashboard("jam-mixed", "JAM network", [
    stat("Head (newest slot)", "max(jam_net_head_slot)", 0, 4),
    stat("One head (this sample)", "min(jam_net_one_head)", 4, 4,
         thresholds=[{"color": "red", "value": None}, {"color": "green", "value": 1}],
         mappings=ONE_HEAD_MAP),
    stat("Distinct heads at the common slot", "max(jam_net_heads)", 8, 4,
         thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 2}]),
    stat("Finality lag (slots)", "max(jam_net_head_slot) - max(jam_net_finalized_slot)", 12, 4,
         thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 6},
                     {"color": "red", "value": 24}]),
    stat("Nodes down", "max(jam_net_nodes) - max(jam_net_nodes_up)", 16, 4,
         thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 1}]),
    stat("Finality conflicts (same slot, two hashes)",
         "max(jam_finality_conflicts_total) or vector(0)", 20, 4,
         thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 1}]),

    ts("Head lag per node (slots behind the newest best block)",
       [{"expr": "jam_head_lag_slots", "legendFormat": NODE_LEGEND}],
       0, 4, 12, overrides=node_overrides),
    ts("Finality lag per node (best - finalized, slots)",
       [{"expr": "jam_finality_lag_slots", "legendFormat": NODE_LEGEND}],
       12, 4, 12, overrides=node_overrides),

    ts("Head agreement (1 = holds the block most nodes hold at the common slot)",
       [{"expr": "jam_head_agree", "legendFormat": NODE_LEGEND}],
       0, 12, 8, overrides=node_overrides),
    ts("Divergence: distinct heads and the current episode (slots)",
       [{"expr": "max(jam_net_heads)", "legendFormat": "heads at the common slot"},
        {"expr": "max(jam_net_final_heads)", "legendFormat": "finalized heads"},
        {"expr": "max(jam_net_divergence_slots)", "legendFormat": "divergence episode (slots)"}],
       8, 12, 8,
       overrides=[override("heads at the common slot", ROLE["authored"]),
                  override("finalized heads", ROLE["imported"]),
                  override("divergence episode (slots)", ROLE["rejected"])]),
    ts("Peers per node (JIP-2 syncState)",
       [{"expr": "jam_peers", "legendFormat": NODE_LEGEND}],
       16, 12, 8, overrides=node_overrides),

    # ---- consensus view: GP validator statistics (pi), read over JIP-2 from on-chain
    # state. The SAME numbers from any node, for EVERY client's validators — what the
    # chain credited each validator with, not what a client says about itself.
    # Cumulative = each epoch's finals folded into a counter by netwatch (pi itself
    # resets per epoch); counts start when netwatch starts.
    bargauge("π blocks credited by consensus — cumulative",
             [{"expr": "sum by (node, client) (jam_pi_blocks_cumulative_total)",
               "legendFormat": NODE_LEGEND}],
             0, 20, 8, overrides=node_overrides),
    bargauge("π guarantees (acted as guarantor) — cumulative",
             [{"expr": "sum by (node, client) (jam_pi_guarantees_cumulative_total)",
               "legendFormat": NODE_LEGEND}],
             8, 20, 8, overrides=node_overrides),
    bargauge("π tickets landed on-chain — cumulative",
             [{"expr": "sum by (node, client) (jam_pi_tickets_cumulative_total)",
               "legendFormat": NODE_LEGEND}],
             16, 20, 8, overrides=node_overrides),
    ts("π blocks per validator, last full epoch — rotation",
       [{"expr": 'max by (node, client) (jam_pi_blocks{epoch="last"})', "legendFormat": NODE_LEGEND}],
       0, 28, 12, overrides=node_overrides),
    ts("Finalized slots per minute per node (0 = finality stalled)",
       [{"expr": "rate(jam_finalized_slot[2m]) * 60", "legendFormat": NODE_LEGEND}],
       12, 28, 12, overrides=node_overrides),

    overlay(36, [
        stat("Stalled lasair nodes (no import >30s)",
             "sum((time() - lasair_last_import_time) > bool 30)", 0, 6,
             thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 1}]),
        stat("lasair blocks / min", "sum(rate(lasair_blocks_authored_total[2m])) * 60", 6, 6),
        stat("lasair chain height", "max(lasair_block_height)", 12, 6),
        ts("Authoring rate per lasair validator (blocks/min)",
           [{"expr": "sum by (node) (rate(lasair_blocks_authored_total[2m])) * 60",
             "legendFormat": "{{node}}"}],
           0, 4, 12, overrides=lm_overrides),
        ts("Chain height per lasair node — diverging lines = a fork",
           [{"expr": "lasair_block_height", "legendFormat": "{{node}}"}],
           12, 4, 12, overrides=lm_overrides),
        ts("Blocks imported per lasair node (blocks/min)",
           [{"expr": "sum by (node) (rate(lasair_blocks_imported_total[2m])) * 60",
             "legendFormat": "{{node}}"}],
           0, 12, 8, overrides=lm_overrides),
        ts("lasair faults (increase, 5m)",
           [{"expr": "sum by (reason) (increase(lasair_block_rejects_total[5m]))",
             "legendFormat": "reject: {{reason}}"},
            {"expr": "sum(increase(lasair_accept_errors_total[5m]))",
             "legendFormat": "QUIC accept errors"},
            {"expr": "sum(increase(lasair_ring_key_failures_total[5m]))",
             "legendFormat": "ring-key failures"},
            {"expr": "sum(increase(lasair_authored_rejected_total[5m]))",
             "legendFormat": "own blocks rejected"}],
           8, 12, 8),
        ts("Safrole ticket pool per lasair node",
           [{"expr": "lasair_ticket_pool", "legendFormat": "{{node}}"}],
           16, 12, 8, overrides=lm_overrides),
        ts("CE-133 pipeline (items/min, all lasair nodes)",
           [{"expr": "sum(rate(lasair_ce133_queued_total[2m])) * 60", "legendFormat": "queued"},
            {"expr": "sum(rate(lasair_ce133_guaranteed_total[2m])) * 60", "legendFormat": "guaranteed"},
            {"expr": "sum(rate(lasair_ce133_dropped_total[2m])) * 60", "legendFormat": "dropped"}],
           0, 20, 8,
           overrides=[override("queued", ROLE["neutral"]),
                      override("guaranteed", ROLE["imported"]),
                      override("dropped", ROLE["rejected"])]),
        ts("Seconds since last import — flat climb = frozen node",
           [{"expr": "time() - lasair_last_import_time", "legendFormat": "{{node}}"}],
           8, 20, 8, unit="s", overrides=lm_overrides),
        ts("Status-thread heartbeat age (s)",
           [{"expr": "time() - lasair_status_alive_time", "legendFormat": "{{node}}"}],
           16, 20, 8, unit="s", overrides=lm_overrides),
    ]),
])

# ═══════════════ CLIENT AVERAGES VIEW ═══════════════
# Every metric averaged across each client's nodes — the per-CLIENT health comparison,
# by the `client` label, so it grows with the net: a new client is a new line.
clients_view = dashboard("jam-clients", "JAM clients (averages)", [
    bargauge("π blocks credited — cumulative, avg per validator",
             [{"expr": "avg by (client) (jam_pi_blocks_cumulative_total)",
               "legendFormat": "{{client}}"}], 0, 0, 8, h=6, overrides=client_overrides),
    bargauge("Head lag now — max per client (slots)",
             [{"expr": "max by (client) (jam_head_lag_slots)", "legendFormat": "{{client}}"}],
             8, 0, 8, h=6, overrides=client_overrides),
    bargauge("Nodes up per client",
             [{"expr": "sum by (client) (jam_node_up)", "legendFormat": "{{client}}"}],
             16, 0, 8, h=6, overrides=client_overrides),

    ts("Head lag — avg per client (slots behind the newest)",
       [{"expr": "avg by (client) (jam_head_lag_slots)", "legendFormat": "{{client}}"}],
       0, 6, 12, overrides=client_overrides),
    ts("Finality lag — avg per client (slots)",
       [{"expr": "avg by (client) (jam_finality_lag_slots)", "legendFormat": "{{client}}"}],
       12, 6, 12, overrides=client_overrides),

    ts("π blocks credited — cumulative, avg per validator",
       [{"expr": "avg by (client) (jam_pi_blocks_cumulative_total)",
         "legendFormat": "{{client}}"}],
       0, 14, 8, overrides=client_overrides),
    ts("π guarantees — cumulative, avg per validator",
       [{"expr": "avg by (client) (jam_pi_guarantees_cumulative_total)",
         "legendFormat": "{{client}}"}],
       8, 14, 8, overrides=client_overrides),
    ts("π tickets on-chain — cumulative, avg per validator",
       [{"expr": "avg by (client) (jam_pi_tickets_cumulative_total)",
         "legendFormat": "{{client}}"}],
       16, 14, 8, overrides=client_overrides),

    ts("Peers — avg per client (JIP-2 syncState)",
       [{"expr": "avg by (client) (jam_peers)", "legendFormat": "{{client}}"}],
       0, 22, 12, overrides=client_overrides),
    ts("Head agreement — min per client (1 = every node on the majority block)",
       [{"expr": "min by (client) (jam_head_agree)", "legendFormat": "{{client}}"}],
       12, 22, 12, overrides=client_overrides),

    overlay(30, [
        ts("lasair authoring rate — avg per validator (blocks/min)",
           [{"expr": "avg(rate(lasair_blocks_authored_total[2m])) * 60", "legendFormat": "lasair"}],
           0, 0, 12, overrides=client_overrides),
        ts("lasair blocks imported/min — avg per node",
           [{"expr": "avg(rate(lasair_blocks_imported_total[2m])) * 60", "legendFormat": "lasair"}],
           12, 0, 12, overrides=client_overrides),
    ]),
])

# ═══════════════ NODE VIEW ═══════════════
sel = '{node=~"$node"}'
node_var = [{"name": "node", "label": "node", "type": "query", "datasource": DS,
             "query": "label_values(jam_best_slot, node)", "refresh": 2,
             "sort": 1, "includeAll": False, "multi": False}]

node_view = dashboard("jam-node", "JAM node", [
    stat("Up", f"jam_node_up{sel}", 0, 3,
         thresholds=[{"color": "red", "value": None}, {"color": "green", "value": 1}]),
    stat("Best slot", f"jam_best_slot{sel}", 3, 4),
    stat("Finalized slot", f"jam_finalized_slot{sel}", 7, 4),
    stat("Head lag (slots)", f"jam_head_lag_slots{sel}", 11, 3,
         thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 2},
                     {"color": "red", "value": 4}]),
    stat("Finality lag (slots)", f"jam_finality_lag_slots{sel}", 14, 3,
         thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 6},
                     {"color": "red", "value": 24}]),
    stat("Head agrees", f"jam_head_agree{sel}", 17, 3,
         thresholds=[{"color": "red", "value": None}, {"color": "green", "value": 1}]),
    stat("Peers", f"jam_peers{sel}", 20, 4,
         thresholds=[{"color": "red", "value": None}, {"color": "orange", "value": 3},
                     {"color": "green", "value": 5}]),

    ts("Head and finality lag (slots)",
       [{"expr": f"jam_head_lag_slots{sel}", "legendFormat": "head lag"},
        {"expr": f"jam_finality_lag_slots{sel}", "legendFormat": "finality lag"}],
       0, 4, 12,
       overrides=[override("head lag", ROLE["authored"]), override("finality lag", ROLE["tickets"])]),
    ts("Agreement with the net (1 = majority block)",
       [{"expr": f"jam_head_agree{sel}", "legendFormat": "head"},
        {"expr": f"jam_final_agree{sel}", "legendFormat": "finalized"}],
       12, 4, 12,
       overrides=[override("head", ROLE["authored"]), override("finalized", ROLE["imported"])]),
    ts("Finalized slots per minute (0 = stalled)",
       [{"expr": f"rate(jam_finalized_slot{sel}[2m]) * 60", "legendFormat": "finalized/min"},
        {"expr": f"rate(jam_best_slot{sel}[2m]) * 60", "legendFormat": "best/min"}],
       0, 12, 12,
       overrides=[override("best/min", ROLE["authored"]), override("finalized/min", ROLE["imported"])]),
    ts("Peers", [{"expr": f"jam_peers{sel}", "legendFormat": "peers"}], 12, 12, 12),

    overlay(20, [
        stat("Height", f"lasair_block_height{sel}", 0, 4),
        stat("Since last import", f"time() - lasair_last_import_time{sel}", 4, 4, unit="s",
             thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 30},
                         {"color": "red", "value": 60}]),
        stat("Status heartbeat", f"time() - lasair_status_alive_time{sel}", 8, 4, unit="s",
             thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 15},
                         {"color": "red", "value": 60}]),
        stat("Ticket pool", f"lasair_ticket_pool{sel}", 12, 4),
        stat("Authored", f"sum(lasair_blocks_authored_total{sel})", 16, 4),
        stat("Imported", f"sum(lasair_blocks_imported_total{sel})", 20, 4),
        ts("Authoring vs import rate (blocks/min)",
           [{"expr": f"rate(lasair_blocks_authored_total{sel}[2m]) * 60", "legendFormat": "authored"},
            {"expr": f"rate(lasair_blocks_imported_total{sel}[2m]) * 60", "legendFormat": "imported"}],
           0, 4, 12,
           overrides=[override("authored", ROLE["authored"]), override("imported", ROLE["imported"])]),
        ts("Import rejects by reason (increase, 5m)",
           [{"expr": f"sum by (reason) (increase(lasair_block_rejects_total{sel}[5m]))",
             "legendFormat": "{{reason}}"}],
           12, 4, 12),
        ts("Peer dial/connection failures by peer (per 5m) — sustained high = churn",
           [{"expr": f"sum by (peer) (increase(lasair_peer_conn_failures_total{sel}[5m]))",
             "legendFormat": "{{peer}}"}],
           0, 12, 8),
        ts("QUIC accepts & errors (per 5m)",
           [{"expr": f"increase(lasair_accepts_total{sel}[5m])", "legendFormat": "accepted"},
            {"expr": f"increase(lasair_accept_errors_total{sel}[5m])", "legendFormat": "errors"}],
           8, 12, 8,
           overrides=[override("accepted", ROLE["imported"]), override("errors", ROLE["rejected"])]),
        ts("Safrole tickets",
           [{"expr": f"lasair_ticket_pool{sel}", "legendFormat": "pool size"},
            {"expr": f"rate(lasair_tickets_pooled_total{sel}[2m]) * 60",
             "legendFormat": "pooled/min"}],
           16, 12, 8,
           overrides=[override("pool size", ROLE["tickets"]),
                      override("pooled/min", ROLE["neutral"])]),
        ts("CE-133 pipeline (items/min)",
           [{"expr": f"rate(lasair_ce133_queued_total{sel}[2m]) * 60", "legendFormat": "queued"},
            {"expr": f"rate(lasair_ce133_guaranteed_total{sel}[2m]) * 60", "legendFormat": "guaranteed"},
            {"expr": f"rate(lasair_ce133_dropped_total{sel}[2m]) * 60", "legendFormat": "dropped"}],
           0, 20, 24,
           overrides=[override("queued", ROLE["neutral"]),
                      override("guaranteed", ROLE["imported"]),
                      override("dropped", ROLE["rejected"])]),
    ]),
], templating=node_var)

# ═══════════════ FINALITY VIEW ═══════════════
# A2 on one page: the finalized head advances on every node with the same hash.
finality_view = dashboard("jam-finality", "JAM finality", [
    stat("Finality conflicts (same slot, two hashes)",
         "max(jam_finality_conflicts_total) or vector(0)", 0, 6,
         thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 1}]),
    stat("Distinct finalized heads at the common slot", "max(jam_net_final_heads)", 6, 6,
         thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 2}]),
    stat("Max finality lag (slots)", "max(jam_finality_lag_slots)", 12, 6,
         thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 6},
                     {"color": "red", "value": 24}]),
    stat("Finalized everywhere (lowest finalized slot)", "max(jam_net_finalized_slot)", 18, 6),

    ts("Finalized slots per minute per node (0 = stalled)",
       [{"expr": "rate(jam_finalized_slot[2m]) * 60", "legendFormat": NODE_LEGEND}],
       0, 4, 12, overrides=node_overrides),
    ts("Finality lag per node (best - finalized, slots)",
       [{"expr": "jam_finality_lag_slots", "legendFormat": NODE_LEGEND}],
       12, 4, 12, overrides=node_overrides),
    ts("Finalized-head agreement (1 = the block most nodes finalized)",
       [{"expr": "jam_final_agree", "legendFormat": NODE_LEGEND}],
       0, 12, 12, overrides=node_overrides),
    ts("Finalized slot behind the net's newest finalized slot (0 = in step)",
       [{"expr": "scalar(max(jam_finalized_slot)) - jam_finalized_slot", "legendFormat": NODE_LEGEND}],
       12, 12, 12, overrides=node_overrides),

    overlay(20, [
        ts("lasair finalized height — all nodes (should track together)",
           [{"expr": "lasair_finalized_height", "legendFormat": "{{node}}"}],
           0, 0, 12, overrides=lm_overrides),
        ts("lasair finality lag (blocks) — all nodes",
           [{"expr": "lasair_block_height - lasair_finalized_height", "legendFormat": "{{node}}"}],
           12, 0, 12, overrides=lm_overrides),
        ts("lasair active commit tally",
           [{"expr": "lasair_finality_commits", "legendFormat": "{{node}}"}],
           0, 8, 12, overrides=lm_overrides),
        ts("lasair GRANDPA round",
           [{"expr": "lasair_grandpa_round", "legendFormat": "{{node}}"}],
           12, 8, 12, overrides=lm_overrides),
    ]),
])

# ═══════════════ SERVICE VIEW (jamswap) ═══════════════
# The flagship-service page (docs/OBSERVABILITY_PLAN.md phase 2): the order
# funnel from API submit to on-chain accumulate, settle latency, the tier-1
# settlement mechanics live, service state, and the end-to-end canary.
# Sources: dex + builder + canary /metrics (jamswap job), client-neutral. The
# lasair nodes' native ce133 counters (the guarantor pipeline as lasair sees it)
# are an overlay: the funnel's node-side series and a collapsed row at the bottom,
# empty on a net without lasair. NOTE the fan-out asymmetry: the dex submits
# each op ONCE but the builder fans it to all 3 lm nodes, so queued/
# guaranteed/accumulated count node-side events, ~3x/1x/1x per op.
OP_COLOR = {"register": "#3987e5", "deposit": "#199e70",
            "withdraw": "#c98500", "cancel": "#9085e9"}
op_overrides = [override(o, c) for o, c in OP_COLOR.items()]
TRACKED = 'op=~"register|deposit|withdraw|cancel"'

service_view = dashboard("jam-service", "JAMswap service", [
    stat("Settle success (15m)",
         f"sum(increase(jamswap_settled_total[15m])) / "
         f"sum(increase(jamswap_submits_total{{{TRACKED}}}[15m]))", 0, 4,
         unit="percentunit",
         thresholds=[{"color": "red", "value": None}, {"color": "orange", "value": 0.5},
                     {"color": "green", "value": 0.99}]),
    stat("Settle p95 (15m)",
         "histogram_quantile(0.95, sum by (le) "
         "(rate(jamswap_settle_latency_seconds_bucket[15m])))", 4, 4, unit="s",
         thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 60},
                     {"color": "red", "value": 120}]),
    stat("Ops in flight",
         f"clamp_min(sum(jamswap_submits_total{{{TRACKED}}}) - sum(jamswap_settled_total)"
         " - sum(jamswap_settle_timeouts_total), 0)", 8, 4,
         thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 5},
                     {"color": "red", "value": 20}]),
    stat("Settle timeouts (1h)",
         "sum(increase(jamswap_settle_timeouts_total[1h])) or vector(0)", 12, 4,
         thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 1}]),
    stat("Canary last pass age",
         "jamswap_canary_last_pass_age_seconds", 16, 4, unit="s",
         thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 600},
                     {"color": "red", "value": 900}]),
    stat("Accounts on-chain", "jamswap_accounts_registered", 20, 4),

    ts("Order funnel (per 5m) — dex submits/settles; queued/guaranteed/accumulated: lasair overlay",
       [{"expr": f"sum(increase(jamswap_submits_total{{{TRACKED}}}[5m]))",
         "legendFormat": "submitted (dex)"},
        {"expr": "sum(increase(lasair_ce133_queued_total[5m]))", "legendFormat": "queued (3x)"},
        {"expr": "sum(increase(lasair_ce133_guaranteed_total[5m]))", "legendFormat": "guaranteed"},
        {"expr": "sum(increase(lasair_ce133_accumulated_total[5m]))", "legendFormat": "accumulated"},
        {"expr": "sum(increase(jamswap_settled_total[5m]))", "legendFormat": "settled (dex)"}],
       0, 4, 12,
       overrides=[override("submitted (dex)", ROLE["neutral"]),
                  override("queued (3x)", "#9085e9"),
                  override("guaranteed", ROLE["authored"]),
                  override("accumulated", ROLE["imported"]),
                  override("settled (dex)", "#008300")]),
    ts("Settle latency — percentiles (10m) + canary e2e",
       [{"expr": "histogram_quantile(0.50, sum by (le) "
                 "(rate(jamswap_settle_latency_seconds_bucket[10m])))",
         "legendFormat": "p50"},
        {"expr": "histogram_quantile(0.95, sum by (le) "
                 "(rate(jamswap_settle_latency_seconds_bucket[10m])))",
         "legendFormat": "p95"},
        {"expr": "rate(jamswap_canary_duration_seconds_sum[30m]) / "
                 "rate(jamswap_canary_duration_seconds_count[30m])",
         "legendFormat": "canary full cycle (avg 30m)"}],
       12, 4, 12, unit="s",
       overrides=[override("p50", ROLE["imported"]), override("p95", ROLE["neutral"]),
                  override("canary full cycle (avg 30m)", ROLE["tickets"])]),

    ts("Settle latency by op (avg, 10m)",
       [{"expr": "sum by (op) (rate(jamswap_settle_latency_seconds_sum[10m])) / "
                 "sum by (op) (rate(jamswap_settle_latency_seconds_count[10m]))",
         "legendFormat": "{{op}}"}],
       0, 12, 12, unit="s", overrides=op_overrides),
    ts("Service state: accounts + resting orders",
       [{"expr": "jamswap_accounts_registered", "legendFormat": "accounts"},
        {"expr": "jamswap_resting_orders", "legendFormat": "resting orders"}],
       12, 12, 6,
       overrides=[override("accounts", ROLE["authored"]),
                  override("resting orders", ROLE["neutral"])]),
    ts("Treasury JAMKB: held vs reserve target (atomic)",
       [{"expr": "jamswap_treasury_jamkb_atomic", "legendFormat": "held"},
        {"expr": "jamswap_treasury_reserve_target_atomic", "legendFormat": "target"}],
       18, 12, 6,
       overrides=[override("held", ROLE["imported"]), override("target", ROLE["rejected"])]),

    ts("API requests by route (per 5m)",
       [{"expr": "sum by (route) (increase(jamswap_api_requests_total[5m]))",
         "legendFormat": "{{route}}"}],
       0, 20, 8),
    ts("Errors: API handlers + builder per-target submit failures (per 5m)",
       [{"expr": "sum by (route) (increase(jamswap_api_errors_total[5m]))",
         "legendFormat": "api {{route}}"},
        {"expr": "sum by (target) (increase(builder_submit_failures_total[5m]))",
         "legendFormat": "builder -> {{target}}"}],
       8, 20, 8),
    ts("Canary cycles (per 30m)",
       [{"expr": "sum(increase(jamswap_canary_pass_total[30m]))", "legendFormat": "pass"},
        {"expr": "sum by (stage) (increase(jamswap_canary_fail_total[30m]))",
         "legendFormat": "fail: {{stage}}"}],
       16, 20, 8,
       overrides=[override("pass", ROLE["imported"])]),

    overlay(28, [
    ts("Guarantee outcomes, all lm nodes (per 5m) — requeued = lost fork race or timeout",
       [{"expr": "sum(increase(lasair_ce133_guaranteed_total[5m]))", "legendFormat": "guaranteed"},
        {"expr": "sum(increase(lasair_ce133_requeued_total[5m]))", "legendFormat": "requeued"},
        {"expr": "sum(increase(lasair_ce133_dropped_total[5m]))",
         "legendFormat": "dropped (duplicate)"}],
       0, 0, 8,
       overrides=[override("guaranteed", ROLE["imported"]),
                  override("requeued", ROLE["neutral"]),
                  override("dropped (duplicate)", ROLE["rejected"])]),
    ts("CE-133 queue depth per lm node — sustained growth = settlement can't keep up",
       [{"expr": "lasair_ce133_queue_depth", "legendFormat": "{{node}}"}],
       8, 0, 8, overrides=lm_overrides),
    ts("Availability work (per 5m): cores assured / items accumulated",
       [{"expr": "sum(increase(lasair_ce133_assured_cores_total[5m]))",
         "legendFormat": "cores assured"},
        {"expr": "sum(increase(lasair_ce133_accumulated_total[5m]))",
         "legendFormat": "accumulated"}],
       16, 0, 8,
       overrides=[override("cores assured", ROLE["tickets"]),
                  override("accumulated", ROLE["imported"])]),
    ]),
])
service_view["links"] = [
    {"title": "JAM network", "type": "link", "url": "/d/jam-mixed", "targetBlank": False},
    {"title": "JAM node", "type": "link", "url": "/d/jam-node", "targetBlank": False},
]

# ═══════════════ ACCOUNTS & TRADING VIEW ═══════════════
# Per-dev-account on-chain balances + the invariants that make settlement
# trustworthy: conservation (per-asset dev supply only moves on faucet mints)
# and durability (cum volume must never DROP; reverts are counted).
ACCOUNT_COLOR = {
    "Alice": "#3987e5", "Bob": "#199e70", "Carol": "#c98500",
    "David": "#008300", "Eve": "#9085e9", "Fergie": "#e66767",
}
acct_overrides = [override(n, c) for n, c in ACCOUNT_COLOR.items()]
ASSET_COLOR = {"USDC": "#3987e5", "DOT": "#e66767", "JAMKB": "#c98500"}
asset_overrides = [override(n, c) for n, c in ASSET_COLOR.items()]

def bal_panel(asset, x):
    return ts(f"{asset} balance per account", [
        {"expr": f'jamswap_balance{{asset="{asset}"}}',
         "legendFormat": "{{account}}", "refId": "A"}],
        x, 4, 8, overrides=acct_overrides, minzero=False)

accounts_view = dashboard("jamswap-accounts", "JAMswap accounts & trading", [
    stat("Last price (DOT/USDC)", 'jamswap_last_price{market="1"}', 0, 4, unit="none"),
    stat("Cumulative volume", 'jamswap_cum_volume{market="1"}', 4, 4),
    stat("Rounds settled", 'sum(jamswap_settled_total{op=~"round|reveal"})', 8, 4,
         thresholds=[{"color": "red", "value": None}, {"color": "green", "value": 1}]),
    stat("Settlements REVERTED by re-org", "sum(jamswap_settle_reverted_total) or vector(0)", 12, 4,
         thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 1},
                     {"color": "red", "value": 5}]),
    stat("Mempool + in-auction", 'sum(jamswap_mempool_orders) + sum(jamswap_inflight_orders)', 16, 4),
    stat("Settle p50 (s)",
         'histogram_quantile(0.5, sum(rate(jamswap_settle_latency_seconds_bucket[10m])) by (le))',
         20, 4, unit="s"),

    bal_panel("USDC", 0), bal_panel("DOT", 8), bal_panel("JAMKB", 16),

    ts("P&L vs genesis (DOT, per account)", [
        {"expr": 'jamswap_balance{asset="DOT"} - 1000000',
         "legendFormat": "{{account}}", "refId": "A"}],
       0, 12, 12, overrides=acct_overrides, minzero=False),
    ts("CONSERVATION: dev supply per asset (flat = sound; steps = faucet mint or bug)", [
        {"expr": "jamswap_dev_supply", "legendFormat": "{{asset}}", "refId": "A"}],
       12, 12, 12, overrides=asset_overrides, minzero=False),

    ts("Cumulative on-chain volume (a DROP = re-org erased settlements)", [
        {"expr": "jamswap_cum_volume", "legendFormat": "market {{market}}", "refId": "A"}],
       0, 20, 8),
    ts("Last clearing price", [
        {"expr": "jamswap_last_price", "legendFormat": "market {{market}}", "refId": "A"}],
       8, 20, 8, minzero=False),
    ts("Order funnel (per min)", [
        {"expr": 'sum(rate(jamswap_submits_total[5m])) * 60', "legendFormat": "submitted", "refId": "A"},
        {"expr": 'sum(rate(jamswap_settled_total[5m])) * 60', "legendFormat": "settled", "refId": "B"},
        {"expr": 'sum(rate(jamswap_refused_total[5m])) * 60', "legendFormat": "refused (backpressure)", "refId": "C"},
        {"expr": 'sum(rate(jamswap_settle_timeouts_total[5m])) * 60', "legendFormat": "timed out", "refId": "D"},
        {"expr": 'sum(rate(jamswap_settle_reverted_total[5m])) * 60', "legendFormat": "REVERTED (re-org)", "refId": "E"}],
       16, 20, 8, overrides=[override("settled", ROLE["imported"]),
                             override("REVERTED (re-org)", ROLE["rejected"])]),

    ts("Resting book depth", [
        {"expr": "jamswap_book_depth", "legendFormat": "m{{market}} {{side}}", "refId": "A"}],
       0, 28, 8),
    ts("Mempool vs in-auction orders", [
        {"expr": "jamswap_mempool_orders", "legendFormat": "m{{market}} mempool", "refId": "A"},
        {"expr": "jamswap_inflight_orders", "legendFormat": "m{{market}} in auction", "refId": "B"}],
       8, 28, 8),
    ts("Chain under the DEX (netwatch): finality lag and divergence (slots)", [
        {"expr": "max(jam_net_head_slot) - max(jam_net_finalized_slot)", "legendFormat": "finality lag", "refId": "A"},
        {"expr": "max(jam_net_divergence_slots)", "legendFormat": "divergence episode", "refId": "B"},
        {"expr": "max(jam_head_lag_slots)", "legendFormat": "worst head lag", "refId": "C"}],
       16, 28, 8, overrides=[override("finality lag", ROLE["tickets"]),
                             override("divergence episode", ROLE["rejected"])]),

    # ── per-order clearing SLO (order_telemetry): the soak's headline reliability ──
    stat("ORDER CLEARING SLO", "jamswap_order_clearing_slo", 0, 6, y=36, unit="percentunit",
         thresholds=[{"color": "red", "value": None}, {"color": "orange", "value": 0.99},
                     {"color": "green", "value": 0.9999}]),
    stat("Marketable cleared", 'sum(jamswap_order_terminal_total{outcome=~"filled|partial-carried",marketable="true"})',
         6, 5, y=36, color="green"),
    stat("Marketable MISSED", 'sum(jamswap_order_terminal_total{outcome=~"expired|lost|partial-cancelled|rejected",marketable="true"}) or vector(0)',
         11, 5, y=36, thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 1}]),
    stat("Orders open now", "sum(jamswap_order_open) or vector(0)", 16, 4, y=36),
    stat("Clear latency p50 (s)",
         "histogram_quantile(0.5, sum(rate(jamswap_order_clear_latency_seconds_bucket[10m])) by (le))",
         20, 4, y=36, unit="s"),

    ts("Order clearing SLO over time (target 0.9999)", [
        {"expr": "jamswap_order_clearing_slo", "legendFormat": "SLO", "refId": "A"}],
       0, 42, 8, unit="percentunit", minzero=False),
    ts("Order outcomes (per min, by disposition)", [
        {"expr": 'sum(rate(jamswap_order_terminal_total[5m])) by (outcome) * 60',
         "legendFormat": "{{outcome}}", "refId": "A"}],
       8, 42, 8, overrides=[override("filled", ROLE["imported"]),
                            override("expired", ROLE["rejected"])]),
    ts("Order clear latency (placement -> durable fill)", [
        {"expr": "histogram_quantile(0.5, sum(rate(jamswap_order_clear_latency_seconds_bucket[10m])) by (le))",
         "legendFormat": "p50", "refId": "A"},
        {"expr": "histogram_quantile(0.99, sum(rate(jamswap_order_clear_latency_seconds_bucket[10m])) by (le))",
         "legendFormat": "p99", "refId": "B"}],
       16, 42, 8, unit="s"),

    ts("Open orders by phase", [
        {"expr": "jamswap_order_open", "legendFormat": "{{phase}}", "refId": "A"}],
       0, 50, 8),
    ts("Order retries (per min: re-org reverts + round timeouts)", [
        {"expr": 'sum(rate(jamswap_order_retries_total[5m])) by (kind) * 60',
         "legendFormat": "{{kind}}", "refId": "A"}],
       8, 50, 8),
    ts("Orders placed (per min, by marketable)", [
        {"expr": 'sum(rate(jamswap_order_placed_total[5m])) by (marketable) * 60',
         "legendFormat": "marketable={{marketable}}", "refId": "A"}],
       16, 50, 8),

    overlay(58, [
    ts("CE-133 pipeline (items/min, fleet)", [
        {"expr": "sum(rate(lasair_ce133_queued_total[5m])) * 60", "legendFormat": "queued", "refId": "A"},
        {"expr": "sum(rate(lasair_ce133_guaranteed_total[5m])) * 60", "legendFormat": "guaranteed", "refId": "B"},
        {"expr": "sum(rate(lasair_ce133_accumulated_total[5m])) * 60", "legendFormat": "accumulated", "refId": "C"}],
       0, 0, 24, overrides=[override("accumulated", ROLE["imported"])]),
    ]),
])
accounts_view["links"] = [
    {"title": "JAMswap service", "type": "link", "url": "/d/jam-service", "targetBlank": False},
    {"title": "JAM network", "type": "link", "url": "/d/jam-mixed", "targetBlank": False},
]

for name, d in (("jam-mixed.json", network), ("jam-node.json", node_view),
                ("jam-clients.json", clients_view), ("jam-finality.json", finality_view),
                ("jam-service.json", service_view), ("jamswap-accounts.json", accounts_view)):
    path = os.path.join(OUT, name)
    with open(path, "w") as fh:
        json.dump(d, fh, indent=2)
    print("wrote", path, "panels:", len(d["panels"]))
