#!/usr/bin/env python3
"""Generate the two provisioned Grafana dashboards (network view + node view).

Regenerate after editing:  python3 monitor/grafana/gen_dashboards.py
Panel boilerplate lives here once; the dashboards stay consistent by
construction. Colors are the CVD-validated per-entity palette — an entity
keeps its hue on every panel of every dashboard.
"""
import json
import os

OUT = os.path.join(os.path.dirname(__file__), "dashboards")

# fixed per-entity palette (validated against Grafana's dark surface)
NODE_COLOR = {
    "pj0": "#3987e5", "pj1": "#199e70", "pj2": "#c98500",   # PolkaJam: blue aqua yellow
    "lm3": "#008300", "lm4": "#9085e9", "lm5": "#e66767",   # lasair:   green violet red
}
CLIENT_COLOR = {"polkajam": "#3987e5", "lasair": "#c98500"}
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


def dashboard(uid, title, panels, templating=None):
    d = {"uid": uid, "title": title, "timezone": "browser", "refresh": "5s",
         "time": {"from": "now-30m", "to": "now"}, "editable": True,
         "panels": panels, "schemaVersion": 39, "version": 1}
    if templating:
        d["templating"] = {"list": templating}
    return d


node_overrides = [override(n, c) for n, c in NODE_COLOR.items()]
lm_overrides = [override(n, NODE_COLOR[n]) for n in ("lm3", "lm4", "lm5")]

# ═══════════════ NETWORK VIEW ═══════════════
network = dashboard("jam-mixed", "JAM mixed network", [
    stat("Current block (slot)", "max(jam_head_slot)", 0, 4),
    stat("Chain height", "max(lasair_block_height)", 4, 4),
    stat("Finality lag (slots)", "max(jam_head_slot) - max(jam_finalized_slot)", 8, 4,
         thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 6},
                     {"color": "red", "value": 24}]),
    stat("Blocks / min",
         "(sum(rate(lasair_blocks_authored_total[2m])) + sum(rate(jam_authored_total[2m]))) * 60",
         12, 4),
    stat("Validator peers (pj view)", "min(jam_pj_vals)", 16, 4,
         thresholds=[{"color": "red", "value": None}, {"color": "green", "value": 5}]),
    stat("Stalled lasair nodes (no import >30s)",
         "sum((time() - lasair_last_import_time) > bool 30)", 20, 4,
         thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 1}]),

    ts("Authoring rate per validator (blocks/min) — rotation, live",
       [{"expr": "sum by (node) (rate(lasair_blocks_authored_total[2m])) * 60",
         "legendFormat": "{{node}}"},
        {"expr": "sum by (node) (rate(jam_authored_total{client=\"polkajam\"}[2m])) * 60",
         "legendFormat": "{{node}}"}],
       0, 4, 12, overrides=node_overrides),
    ts("Chain height per lasair node — diverging lines = a fork",
       [{"expr": "lasair_block_height", "legendFormat": "{{node}}"}],
       12, 4, 12, overrides=lm_overrides),

    bargauge("Blocks authored (dashboard time range, restart-proof)",
             [{"expr": "sum by (node) (increase(lasair_blocks_authored_total[$__range]))",
               "legendFormat": "{{node}}"},
              {"expr": "sum by (node) (increase(jam_authored_total{client=\"polkajam\"}[$__range]))",
               "legendFormat": "{{node}}"}],
             0, 12, 8, overrides=node_overrides),
    ts("Blocks imported per lasair node (blocks/min)",
       [{"expr": "sum by (node) (rate(lasair_blocks_imported_total[2m])) * 60",
         "legendFormat": "{{node}}"}],
       8, 12, 8, overrides=lm_overrides),
    ts("Faults (increase, 5m)",
       [{"expr": "sum by (reason) (increase(lasair_block_rejects_total[5m]))",
         "legendFormat": "reject: {{reason}}"},
        {"expr": "sum(increase(lasair_accept_errors_total[5m]))",
         "legendFormat": "QUIC accept errors"},
        {"expr": "sum(increase(lasair_ring_key_failures_total[5m]))",
         "legendFormat": "ring-key failures"},
        {"expr": "sum(increase(lasair_authored_rejected_total[5m]))",
         "legendFormat": "own blocks rejected"}],
       16, 12, 8),

    ts("Peers connected",
       [{"expr": "lasair_peers_connected", "legendFormat": "{{node}}"},
        {"expr": "jam_pj_peers", "legendFormat": "{{node}}"}],
       0, 20, 8, overrides=node_overrides),
    ts("Safrole ticket pool per lasair node",
       [{"expr": "lasair_ticket_pool", "legendFormat": "{{node}}"}],
       8, 20, 8, overrides=lm_overrides),
    ts("CE-133 pipeline (items/min, all lasair nodes)",
       [{"expr": "sum(rate(lasair_ce133_queued_total[2m])) * 60", "legendFormat": "queued"},
        {"expr": "sum(rate(lasair_ce133_guaranteed_total[2m])) * 60", "legendFormat": "guaranteed"},
        {"expr": "sum(rate(lasair_ce133_dropped_total[2m])) * 60", "legendFormat": "dropped"}],
       16, 20, 8,
       overrides=[override("queued", ROLE["neutral"]),
                  override("guaranteed", ROLE["imported"]),
                  override("dropped", ROLE["rejected"])]),

    ts("Seconds since last import — flat climb = frozen node",
       [{"expr": "time() - lasair_last_import_time", "legendFormat": "{{node}}"}],
       0, 36, 12, unit="s", overrides=lm_overrides),
    ts("Status-thread heartbeat age (s)",
       [{"expr": "time() - lasair_status_alive_time", "legendFormat": "{{node}}"}],
       12, 36, 12, unit="s", overrides=lm_overrides),

    # ---- consensus view: GP validator statistics (pi), decoded from on-chain
    # state via pj's RPC. The SAME numbers from any node, for BOTH clients'
    # validators — what the chain credited each validator with, not what a
    # client says about itself. The apples-to-apples row. Cumulative = each
    # epoch's finals folded into a counter by the exporter (pi itself resets
    # per epoch); counts start at the exporter's start.
    bargauge("π blocks credited by consensus — cumulative",
             [{"expr": "sum by (node) (jam_pi_blocks_cumulative_total)",
               "legendFormat": "{{node}}"}],
             0, 28, 8, overrides=node_overrides),
    bargauge("π guarantees (acted as guarantor) — cumulative",
             [{"expr": "sum by (node) (jam_pi_guarantees_cumulative_total)",
               "legendFormat": "{{node}}"}],
             8, 28, 8, overrides=node_overrides),
    bargauge("π tickets landed on-chain — cumulative",
             [{"expr": "sum by (node) (jam_pi_tickets_cumulative_total)",
               "legendFormat": "{{node}}"}],
             16, 28, 8, overrides=node_overrides),
])

# ═══════════════ CLIENT AVERAGES VIEW ═══════════════
# Every metric averaged across each client's nodes — the per-CLIENT health
# comparison, built to stay meaningful as more client implementations join.
client_overrides = [override(c, col) for c, col in CLIENT_COLOR.items()]

clients_view = dashboard("jam-clients", "JAM clients (averages)", [
    stat("lasair — blocks/min (avg per validator)",
         "avg(rate(lasair_blocks_authored_total[5m])) * 60", 0, 6,
         color="#c98500"),
    stat("polkajam — blocks/min (avg per validator)",
         "avg(rate(jam_authored_total{client=\"polkajam\"}[5m])) * 60", 6, 6,
         color="#3987e5"),
    stat("lasair — π blocks cumulative (avg)",
         "avg(jam_pi_blocks_cumulative_total{client=\"lasair\"})", 12, 6,
         color="#c98500"),
    stat("polkajam — π blocks cumulative (avg)",
         "avg(jam_pi_blocks_cumulative_total{client=\"polkajam\"})", 18, 6,
         color="#3987e5"),

    ts("Authoring rate — avg per validator (blocks/min)",
       [{"expr": "avg(rate(lasair_blocks_authored_total[2m])) * 60", "legendFormat": "lasair"},
        {"expr": "avg(rate(jam_authored_total{client=\"polkajam\"}[2m])) * 60",
         "legendFormat": "polkajam"}],
       0, 4, 12, overrides=client_overrides),
    ts("π blocks credited — cumulative, avg per validator",
       [{"expr": "avg by (client) (jam_pi_blocks_cumulative_total)",
         "legendFormat": "{{client}}"}],
       12, 4, 12, overrides=client_overrides),

    ts("π guarantees — cumulative, avg per validator",
       [{"expr": "avg by (client) (jam_pi_guarantees_cumulative_total)",
         "legendFormat": "{{client}}"}],
       0, 12, 12, overrides=client_overrides),
    ts("π tickets on-chain — cumulative, avg per validator",
       [{"expr": "avg by (client) (jam_pi_tickets_cumulative_total)",
         "legendFormat": "{{client}}"}],
       12, 12, 12, overrides=client_overrides),

    ts("Peers connected — avg per node",
       [{"expr": "avg(lasair_peers_connected)", "legendFormat": "lasair"},
        {"expr": "avg(jam_pj_peers)", "legendFormat": "polkajam"}],
       0, 20, 12, overrides=client_overrides),
    ts("Blocks imported/min — avg per node (native-instrumented clients only)",
       [{"expr": "avg(rate(lasair_blocks_imported_total[2m])) * 60",
         "legendFormat": "lasair"}],
       12, 20, 12, overrides=client_overrides),
])

# ═══════════════ NODE VIEW ═══════════════
sel = '{node=~"$node"}'
node_var = [{"name": "node", "label": "lasair node", "type": "query", "datasource": DS,
             "query": "label_values(lasair_block_height, node)", "refresh": 2,
             "current": {"text": "lm3", "value": "lm3"},
             "sort": 1, "includeAll": False, "multi": False}]

node_view = dashboard("jam-node", "JAM node (lasair)", [
    stat("Height", f"lasair_block_height{sel}", 0, 3),
    stat("Slot", f"lasair_slot{sel}", 3, 3),
    stat("Since last import", f"time() - lasair_last_import_time{sel}", 6, 3, unit="s",
         thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 30},
                     {"color": "red", "value": 60}]),
    stat("Status heartbeat", f"time() - lasair_status_alive_time{sel}", 9, 3, unit="s",
         thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 15},
                     {"color": "red", "value": 60}]),
    stat("Peers", f"lasair_peers_connected{sel}", 12, 3,
         thresholds=[{"color": "red", "value": None}, {"color": "orange", "value": 3},
                     {"color": "green", "value": 5}]),
    stat("Ticket pool", f"lasair_ticket_pool{sel}", 15, 3),
    stat("Authored", f"sum(lasair_blocks_authored_total{sel})", 18, 3),
    stat("Imported", f"sum(lasair_blocks_imported_total{sel})", 21, 3),

    ts("Block height",
       [{"expr": f"lasair_block_height{sel}", "legendFormat": "height"}],
       0, 4, 12, overrides=[override("height", ROLE["imported"])]),
    ts("Authoring vs import rate (blocks/min)",
       [{"expr": f"rate(lasair_blocks_authored_total{sel}[2m]) * 60", "legendFormat": "authored"},
        {"expr": f"rate(lasair_blocks_imported_total{sel}[2m]) * 60", "legendFormat": "imported"}],
       12, 4, 12,
       overrides=[override("authored", ROLE["authored"]), override("imported", ROLE["imported"])]),

    ts("Import rejects by reason (increase, 5m)",
       [{"expr": f"sum by (reason) (increase(lasair_block_rejects_total{sel}[5m]))",
         "legendFormat": "{{reason}}"}],
       0, 12, 8),
    ts("Peer dial/connection failures by peer (per 5m) — sustained high = churn",
       [{"expr": f"sum by (peer) (increase(lasair_peer_conn_failures_total{sel}[5m]))",
         "legendFormat": "{{peer}}"}],
       8, 12, 8),
    ts("QUIC accepts & errors (per 5m)",
       [{"expr": f"increase(lasair_accepts_total{sel}[5m])", "legendFormat": "accepted"},
        {"expr": f"increase(lasair_accept_errors_total{sel}[5m])", "legendFormat": "errors"}],
       16, 12, 8,
       overrides=[override("accepted", ROLE["imported"]), override("errors", ROLE["rejected"])]),

    ts("Safrole tickets",
       [{"expr": f"lasair_ticket_pool{sel}", "legendFormat": "pool size"},
        {"expr": f"rate(lasair_tickets_pooled_total{sel}[2m]) * 60",
         "legendFormat": "pooled/min"}],
       0, 20, 12,
       overrides=[override("pool size", ROLE["tickets"]),
                  override("pooled/min", ROLE["neutral"])]),
    ts("CE-133 pipeline (items/min)",
       [{"expr": f"rate(lasair_ce133_queued_total{sel}[2m]) * 60", "legendFormat": "queued"},
        {"expr": f"rate(lasair_ce133_guaranteed_total{sel}[2m]) * 60", "legendFormat": "guaranteed"},
        {"expr": f"rate(lasair_ce133_dropped_total{sel}[2m]) * 60", "legendFormat": "dropped"}],
       12, 20, 12,
       overrides=[override("queued", ROLE["neutral"]),
                  override("guaranteed", ROLE["imported"]),
                  override("dropped", ROLE["rejected"])]),
], templating=node_var)

# ═══════════════ SERVICE VIEW (jamswap) ═══════════════
# The flagship-service page (docs/OBSERVABILITY_PLAN.md phase 2): the order
# funnel from API submit to on-chain accumulate, settle latency, the tier-1
# settlement mechanics live, service state, and the end-to-end canary.
# Sources: dex + builder + canary /metrics (jamswap job) and the lasair
# nodes' native ce133 counters. NOTE the fan-out asymmetry: the dex submits
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

    ts("Order funnel (per 5m) — dex submits once; node-side stages count all 3 lm nodes",
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

    ts("Guarantee outcomes, all lm nodes (per 5m) — requeued = lost fork race or timeout",
       [{"expr": "sum(increase(lasair_ce133_guaranteed_total[5m]))", "legendFormat": "guaranteed"},
        {"expr": "sum(increase(lasair_ce133_requeued_total[5m]))", "legendFormat": "requeued"},
        {"expr": "sum(increase(lasair_ce133_dropped_total[5m]))",
         "legendFormat": "dropped (duplicate)"}],
       0, 12, 8,
       overrides=[override("guaranteed", ROLE["imported"]),
                  override("requeued", ROLE["neutral"]),
                  override("dropped (duplicate)", ROLE["rejected"])]),
    ts("CE-133 queue depth per lm node — sustained growth = settlement can't keep up",
       [{"expr": "lasair_ce133_queue_depth", "legendFormat": "{{node}}"}],
       8, 12, 8, overrides=lm_overrides),
    ts("Availability work (per 5m): cores assured / items accumulated",
       [{"expr": "sum(increase(lasair_ce133_assured_cores_total[5m]))",
         "legendFormat": "cores assured"},
        {"expr": "sum(increase(lasair_ce133_accumulated_total[5m]))",
         "legendFormat": "accumulated"}],
       16, 12, 8,
       overrides=[override("cores assured", ROLE["tickets"]),
                  override("accumulated", ROLE["imported"])]),

    ts("Settle latency by op (avg, 10m)",
       [{"expr": "sum by (op) (rate(jamswap_settle_latency_seconds_sum[10m])) / "
                 "sum by (op) (rate(jamswap_settle_latency_seconds_count[10m]))",
         "legendFormat": "{{op}}"}],
       0, 20, 12, unit="s", overrides=op_overrides),
    ts("Service state: accounts + resting orders",
       [{"expr": "jamswap_accounts_registered", "legendFormat": "accounts"},
        {"expr": "jamswap_resting_orders", "legendFormat": "resting orders"}],
       12, 20, 6,
       overrides=[override("accounts", ROLE["authored"]),
                  override("resting orders", ROLE["neutral"])]),
    ts("Treasury JAMKB: held vs reserve target (atomic)",
       [{"expr": "jamswap_treasury_jamkb_atomic", "legendFormat": "held"},
        {"expr": "jamswap_treasury_reserve_target_atomic", "legendFormat": "target"}],
       18, 20, 6,
       overrides=[override("held", ROLE["imported"]), override("target", ROLE["rejected"])]),

    ts("API requests by route (per 5m)",
       [{"expr": "sum by (route) (increase(jamswap_api_requests_total[5m]))",
         "legendFormat": "{{route}}"}],
       0, 28, 8),
    ts("Errors: API handlers + builder per-target submit failures (per 5m)",
       [{"expr": "sum by (route) (increase(jamswap_api_errors_total[5m]))",
         "legendFormat": "api {{route}}"},
        {"expr": "sum by (target) (increase(builder_submit_failures_total[5m]))",
         "legendFormat": "builder -> {{target}}"}],
       8, 28, 8),
    ts("Canary cycles (per 30m)",
       [{"expr": "sum(increase(jamswap_canary_pass_total[30m]))", "legendFormat": "pass"},
        {"expr": "sum by (stage) (increase(jamswap_canary_fail_total[30m]))",
         "legendFormat": "fail: {{stage}}"}],
       16, 28, 8,
       overrides=[override("pass", ROLE["imported"])]),
])
service_view["links"] = [
    {"title": "JAM mixed network", "type": "link", "url": "/d/jam-mixed", "targetBlank": False},
    {"title": "JAM node (lasair)", "type": "link", "url": "/d/jam-node", "targetBlank": False},
]

for name, d in (("jam-mixed.json", network), ("jam-node.json", node_view),
                ("jam-clients.json", clients_view), ("jam-service.json", service_view)):
    path = os.path.join(OUT, name)
    json.dump(d, open(path, "w"), indent=2)
    print("wrote", path, "panels:", len(d["panels"]))
