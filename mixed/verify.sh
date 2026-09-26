#!/usr/bin/env bash
# Health check for a RUNNING mixed network (docker-compose.mixed.yml).
# Run via: make verify-mixed
#
# Client-neutral (issue #15): instead of grepping each client's container logs, this runs
# offchain/netwatch.py against every node's PUBLIC interface — JIP-2 for PolkaJam (heads
# compared by hash), the Prometheus gauges for lasair (by slot and height, until lasair
# serves JIP-2, lasair#68) — for VERIFY_SECS and asserts:
#
#   one head    every node's best block agrees (by hash where it can) within 3 slots, and
#               no fork, lag or outage lasts longer than one epoch;
#   liveness    every node's best block advances;
#   authoring   every validator is credited blocks by consensus (the on-chain validator
#               statistics, GP pi, read over JIP-2) — its blocks landed on the canonical
#               chain. This is what the old greps approximated: stale genesis keys
#               (bad_seal, ring-key failures) and unreachable peers (QUIC accept errors,
#               a loopback bind) all end as a validator whose blocks never land;
#   peers       every JIP-2 node reports at least MIN_PEERS peers.
#
# Finality is reported, not judged (VERIFY_FINALITY=report): the clients of an
# equal-split mixed net do not share finality (docs/NETS.md), so PolkaJam's may advance
# while lasair's cannot. VERIFY_FINALITY=require (or auto) on a net that should finalize.
#
# The node list follows LAYOUT (the same variable the compose file's spec-init takes):
# validator i runs client LAYOUT[i] at 172.28.0.(10+i) as pj<i> (JIP-2 on 19890+i) or
# lm<i> (metrics on :9615). NETWATCH_NODES overrides it entirely. netwatch runs inside
# RUNNER (default: the dex container, which is on the net and mounts ./offchain at /app).
set -euo pipefail

PROJECT="${COMPOSE_PROJECT:-jamswap}"
RUNNER="${RUNNER:-$PROJECT-dex-1}"
APP="${NETWATCH_APP:-/app}"
SECS="${VERIFY_SECS:-360}"       # ~5 tiny epochs: every validator gets its turn to author
LAYOUT="${LAYOUT:-polkajam,polkajam,polkajam,lasair,lasair,lasair}"
IP_BASE="${IP_BASE:-172.28.0}"
RPC_BASE="${RPC_BASE:-19890}"
MIN_PEERS="${MIN_PEERS:-5}"

nodes=() validators=()
IFS=, read -r -a clients <<<"$LAYOUT"
for i in "${!clients[@]}"; do
  c="${clients[$i]}"
  ip="$IP_BASE.$((10 + i))"
  case "$c" in
    polkajam) n="pj$i"; nodes+=("$n,$c,ws://$ip:$((RPC_BASE + i))") ;;
    lasair)   n="lm$i"; nodes+=("$n,$c,http://$ip:9615/metrics") ;;
    *)        echo "verify: no public interface known for client '$c' (index $i)" >&2; exit 2 ;;
  esac
  validators+=("$n:$c")
done
NODES="${NETWATCH_NODES:-${nodes[*]}}"
VALIDATORS="$(IFS=,; echo "${validators[*]}")"

echo "verify: watching ${NODES// /  } for ${SECS}s (via $RUNNER)"
# shellcheck disable=SC2086  # VERIFY_ARGS is a list of extra flags
if docker exec -e NETWATCH_NODES="$NODES" "$RUNNER" python3 "$APP/netwatch.py" poll \
     --duration "$SECS" --interval 6 --validators "$VALIDATORS" \
     --require-authoring --min-peers "$MIN_PEERS" --finality "${VERIFY_FINALITY:-report}" \
     ${VERIFY_ARGS:-}; then
  echo "ALL PASS: one head, every node live, every validator's blocks canonical"
else
  echo "FAIL: see the verdict above (per-sample lines name the node and the problem)"
  exit 1
fi
