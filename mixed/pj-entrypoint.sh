#!/usr/bin/env bash
# Entrypoint for the mixed-network PolkaJam image. Dispatches on ROLE:
#   ROLE=init       generate the shared genesis spec (runs gen-spec.py, then exits)
#   ROLE=validator  run PolkaJam as validator INDEX on the shared spec
#
# PolkaJam is used BLACK-BOX; its binary is fetched from the public release at
# image-BUILD time (never committed / never pushed to our registry). See
# jamswap/README.md "Mixed-client network" and lasair docs/DISCLOSURES.md.
set -euo pipefail
ROLE="${ROLE:?set ROLE=init|validator}"
SHARED="${SHARED:-/shared}"

if [ "$ROLE" = "init" ]; then
  exec python3 /gen-spec.py
fi

# ---- validator ----
INDEX="${INDEX:?set INDEX=<validator index>}"
for _ in $(seq 1 "${WAIT_SPEC:-90}"); do [ -s "$SHARED/ready" ] && break; echo "waiting for shared genesis ..."; sleep 1; done
[ -s "$SHARED/ready" ] || { echo "FATAL: shared genesis never appeared"; exit 1; }

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
      --listen-ip 0.0.0.0 --port "$PORT" --finality-mode dummy
      --rpc --rpc-listen-ip 0.0.0.0 --rpc-port "$RPC")
[ "$ISBOOT" = "0" ] && args+=(--bootnode "$BOOT")

echo "polkajam validator $INDEX: peer_id=$PID port=$PORT rpc=$RPC bootnode=$([ "$ISBOOT" = 1 ] && echo SELF || echo "$BOOT")"
exec polkajam "${args[@]}"
