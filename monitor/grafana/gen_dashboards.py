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


def stat(title, expr, x, w, color="text", thresholds=None, unit=None, mappings=None):
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
            "gridPos": {"x": x, "y": 0, "w": w, "h": 4}, "datasource": DS,
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
    stat("Faults (last 5m)",
         "sum(increase(lasair_block_rejects_total[5m])) + sum(increase(lasair_accept_errors_total[5m]))"
         " + sum(increase(lasair_ring_key_failures_total[5m]))",
         20, 4,
         thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 1}]),

    ts("Authoring rate per validator (blocks/min) — rotation, live",
       [{"expr": "sum by (node) (rate(lasair_blocks_authored_total[2m])) * 60",
         "legendFormat": "{{node}}"},
        {"expr": "sum by (node) (rate(jam_authored_total{client=\"polkajam\"}[2m])) * 60",
         "legendFormat": "{{node}}"}],
       0, 4, 12, overrides=node_overrides),
    ts("Chain height per lasair node — diverging lines = a fork",
       [{"expr": "lasair_block_height", "legendFormat": "{{node}}"}],
       12, 4, 12, overrides=lm_overrides),

    bargauge("Blocks authored (total since node start)",
             [{"expr": "sum by (node) (lasair_blocks_authored_total)", "legendFormat": "{{node}}"},
              {"expr": "sum by (node) (jam_authored_total{client=\"polkajam\"})",
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

    # ---- consensus view: GP validator statistics (pi), decoded from on-chain
    # state via pj's RPC. The SAME numbers from any node, for BOTH clients'
    # validators — what the chain credited each validator with, not what a
    # client says about itself. The apples-to-apples row.
    bargauge("π blocks credited by consensus (current epoch)",
             [{"expr": "jam_pi_blocks{epoch=\"current\"}", "legendFormat": "{{node}}"}],
             0, 28, 12, overrides=node_overrides),
    bargauge("π tickets on-chain (current epoch) — lasair at 0 = its ticket "
             "extrinsics never land",
             [{"expr": "jam_pi_tickets{epoch=\"current\"}", "legendFormat": "{{node}}"}],
             12, 28, 12, overrides=node_overrides),
])

# ═══════════════ NODE VIEW ═══════════════
sel = '{node=~"$node"}'
node_var = [{"name": "node", "label": "lasair node", "type": "query", "datasource": DS,
             "query": "label_values(lasair_block_height, node)", "refresh": 2,
             "current": {"text": "lm3", "value": "lm3"},
             "sort": 1, "includeAll": False, "multi": False}]

node_view = dashboard("jam-node", "JAM node (lasair)", [
    stat("Height", f"lasair_block_height{sel}", 0, 4),
    stat("Slot", f"lasair_slot{sel}", 4, 4),
    stat("Peers connected", f"lasair_peers_connected{sel}", 8, 4,
         thresholds=[{"color": "red", "value": None}, {"color": "orange", "value": 3},
                     {"color": "green", "value": 5}]),
    stat("Ticket pool", f"lasair_ticket_pool{sel}", 12, 4),
    stat("Authored", f"sum(lasair_blocks_authored_total{sel})", 16, 4),
    stat("Imported", f"sum(lasair_blocks_imported_total{sel})", 20, 4),

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

for name, d in (("jam-mixed.json", network), ("jam-node.json", node_view)):
    path = os.path.join(OUT, name)
    json.dump(d, open(path, "w"), indent=2)
    print("wrote", path, "panels:", len(d["panels"]))
