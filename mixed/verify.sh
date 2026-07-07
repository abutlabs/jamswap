#!/usr/bin/env bash
# Health check for a RUNNING mixed lasair+PolkaJam network (docker-compose.mixed.yml).
# Run via: make verify-mixed
#
# Asserts the flagship claim actually holds: every lasair validator authors blocks
# that land canonically, seals verify, tickets gossip, and PolkaJam recognises all
# validator peers. These are exactly the failure signatures of the two historical
# faults (stale genesis keys -> bad_seal/cert rejections; loopback QUIC bind).
set -euo pipefail

fail=0
say() { printf '%s\n' "$*"; }

# Give every validator a fair shot before judging: Safrole slot assignment is random,
# so a young chain can legitimately have a validator with zero authored blocks yet.
# Wait until the watcher has seen enough slots (~5 epochs) or every lm has authored.
need=60
for _ in $(seq 1 60); do
  slots=$(docker logs jamswap-watch-1 2>&1 | grep -c 'chain: slot' || true)
  all=1
  for n in lm3 lm4 lm5; do
    a=$(docker logs "jamswap-$n-1" 2>&1 | grep -ac 'authored slot' || true)
    [ "$a" -ge 1 ] || all=0
  done
  [ "$slots" -ge "$need" ] || [ "$all" -eq 1 ] && break
  say "waiting for slots ($slots/$need seen)…"; sleep 10
done

for n in lm3 lm4 lm5; do
  c="jamswap-$n-1"
  bad=$(docker logs "$c" 2>&1 | grep -ac bad_seal || true)
  acc=$(docker logs "$c" 2>&1 | grep -ac 'accept error' || true)
  ring=$(docker logs "$c" 2>&1 | grep -ac 'does not verify' || true)
  authored=$(docker logs "$c" 2>&1 | grep -ac 'authored slot' || true)
  say "$n: authored=$authored bad_seal=$bad accept_errors=$acc ring_failures=$ring"
  [ "$bad" -eq 0 ]  || { say "FAIL $n: bad_seal rejections — genesis keys mismatch?"; fail=1; }
  [ "$ring" -eq 0 ] || { say "FAIL $n: ring secret vs gamma_z — genesis keys mismatch?"; fail=1; }
  [ "$acc" -lt 5 ]  || { say "FAIL $n: QUIC accept errors — peer certs not recognised?"; fail=1; }
done

# every lasair validator must have authored blocks that became canonical: of its last
# few authored heads, at least one must have been imported by a PolkaJam node. (A
# single authored block can legitimately lose a wall-clock fork race — that's not a
# failure; ALL of them losing is.)
pjlog=$(mktemp)
trap 'rm -f "$pjlog"' EXIT
docker logs jamswap-pj0-1 >"$pjlog" 2>&1   # snapshot once: grep -q on a live pipe
                                           # SIGPIPEs docker logs and pipefail eats the match
for n in lm3 lm4 lm5; do
  c="jamswap-$n-1"
  heads=$(docker logs "$c" 2>&1 | grep -a 'authored slot' | tail -5 \
          | grep -oE 'head 0x[0-9a-f]+' | sed 's/head 0x//' || true)
  if [ -z "$heads" ]; then say "FAIL $n: never authored a block"; fail=1; continue; fi
  hit=""
  for h in $heads; do
    if grep -q -- "0x$h" "$pjlog"; then hit="$h"; break; fi
  done
  if [ -n "$hit" ]; then
    say "$n: authored block 0x$hit... imported by PolkaJam"
  else
    say "FAIL $n: none of the last 5 authored blocks reached PolkaJam"; fail=1
  fi
done

vals=$(docker logs jamswap-pj0-1 2>&1 | grep -oE '\([0-9]+ vals\)' | tail -1 | grep -oE '[0-9]+')
say "pj0 sees $vals validator peers (want 5)"
[ "${vals:-0}" -eq 5 ] || { say "FAIL: PolkaJam does not recognise all validators"; fail=1; }

[ "$fail" -eq 0 ] && say "ALL PASS: cross-client rotation healthy" || exit 1
