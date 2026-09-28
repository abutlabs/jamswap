#!/usr/bin/env bash
# Entrypoint for the mixed-network PolkaJam image. Dispatches on ROLE:
#   ROLE=init       generate the shared genesis spec (runs nets/genesis.py, then exits)
#   ROLE=validator  run PolkaJam as validator INDEX on the shared spec
#   ROLE=node       run an ordinary PolkaJam node (no validator key) on the shared spec,
#                   JAMNP-S on PORT, JIP-2 RPC on RPC_PORT: a builder's gateway (a
#                   validator's RPC does not forward work-packages; see docs/NETS.md)
#
# PolkaJam is used BLACK-BOX; its binary is fetched from the public release at
# image-BUILD time (never committed / never pushed to our registry). See
# jamswap/README.md "Mixed-client network" and lasair docs/DISCLOSURES.md.
set -euo pipefail
ROLE="${ROLE:?set ROLE=init|validator}"
SHARED="${SHARED:-/shared}"

if [ "$ROLE" = "init" ]; then
  exec python3 /nets/genesis.py
fi

for _ in $(seq 1 "${WAIT_SPEC:-90}"); do [ -s "$SHARED/ready" ] && break; echo "waiting for shared genesis ..."; sleep 1; done
[ -s "$SHARED/ready" ] || { echo "FATAL: shared genesis never appeared"; exit 1; }

if [ "$ROLE" = "node" ]; then
  BOOT=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["bootnode"])' "$SHARED/nodes.json")
  # the net's finality mode too: its finalizedBlock is what a DEX treats as irreversible
  args=(--chain "$SHARED/spec.json" run --temp --mode ordinary
        --listen-ip 0.0.0.0 --port "${PORT:?set PORT}" --bootnode "$BOOT"
        --finality-mode "${FINALITY_MODE:-dummy}"
        --rpc --rpc-listen-ip 0.0.0.0 --rpc-port "${RPC_PORT:?set RPC_PORT}")
  [ -n "${EXTERNAL_IP:-}" ] && args+=(--external-ip "$EXTERNAL_IP")
  [ -n "${TELEMETRY:-}" ] && args+=(--telemetry "$TELEMETRY")
  echo "polkajam ordinary node: port=$PORT rpc=$RPC_PORT finality=${FINALITY_MODE:-dummy} bootnode=$BOOT"
  exec polkajam "${args[@]}"
fi

# ---- validator ----
INDEX="${INDEX:?set INDEX=<validator index>}"

# pull my parameters out of nodes.json
read -r PID PORT RPC BOOT ISBOOT < <(python3 - "$SHARED/nodes.json" "$INDEX" <<'PY'
import json,sys
topo=json.load(open(sys.argv[1])); idx=int(sys.argv[2])
me=[n for n in topo["nodes"] if n["index"]==idx][0]
boot=topo["bootnode"]; isboot="1" if boot.split("@")[0]==me["peer_id"] else "0"
print(me["peer_id"], me["port"], me.get("rpc",0), boot, isboot)
PY
)

args=(--chain "$SHARED/spec.json" run --temp --peer-id "$PID"
      --key-seed-file "$SHARED/pj_${INDEX}.seed"
      --listen-ip 0.0.0.0 --port "$PORT" --finality-mode "${FINALITY_MODE:-dummy}"
      --rpc --rpc-listen-ip 0.0.0.0 --rpc-port "$RPC")
[ "$ISBOOT" = "0" ] && args+=(--bootnode "$BOOT")
# a net with a node running natively on the host addresses every validator at the
# host's IP (nets/netgen.py): tell PolkaJam that is its external address too
[ -n "${EXTERNAL_IP:-}" ] && args+=(--external-ip "$EXTERNAL_IP")
# JIP-3 telemetry to HOST:PORT (./dex up points it at the observability stack's receiver)
[ -n "${TELEMETRY:-}" ] && args+=(--telemetry "$TELEMETRY")

echo "polkajam validator $INDEX: peer_id=$PID port=$PORT rpc=$RPC bootnode=$([ "$ISBOOT" = 1 ] && echo SELF || echo "$BOOT")"
exec polkajam "${args[@]}"
