# Diagnosis: permanent forking on the Lasair 6-validator testnet

**Status:** diagnosed, not yet fixed.
**Fix location:** the `lasair` repo (`submodules/lasair`), NOT jamswap. Jamswap only
consumes the prebuilt image `ghcr.io/abutlabs/lasair-testnet-node` via
`docker-compose.testnet.yml`; once lasair ships a fixed image, jamswap needs at most a
`LASAIR_NODE_TAG` bump.
**Diagnosed:** 2026-07-02, from live testnet logs + reading lasair source at commit
current on that date.

---

## Symptom

Running `docker compose -f docker-compose.testnet.yml up` (6 validators, tiny spec,
6 s slots, 12-slot epochs), the logs degrade within minutes to almost entirely:

```
node1  ✗ rejected slot 7878934 from node3: bad_parent_state_root
node5  ⏳ slot 7878929  no block from node3 (leader missed?)
```

Late-stage signatures observed (all from one ~5-minute capture):

1. **Every authored block is rejected by every other node** with
   `bad_parent_state_root` (~5 reject lines per block).
2. **Heights diverge per node**: at the same wall time node5 was at height #19,
   node1 #18, node2 #17, node0 #17, node3 #18, node4 #16 — six independent chains.
3. **Nodes disagree on who the slot leader is**: for slot 7878937, node3/node0/node2
   waited for node1 while node1/node5 waited for node4.
4. **Multiple nodes author the SAME slot** on different heads: slot 7878962 was
   authored by node4 (`0x3dfccf..`), node5 (`0xe2ed90..`), node3 (`0x08ab0d..`) and
   node1 (`0xbc78fe..`).
5. **Stall-then-burst timing**: node1 and node5 were silent 15:33:58 → 15:34:06, then
   processed slots 7878929–7878934 within ~5 ms. Both woke at the same instant
   (suggests a host/Docker-VM scheduling stall, but any single missed block triggers
   the same cascade).

## Root cause

The distributed driver is `lasair/bin/lasair_testnet_node.ml`. Its own header comment
states the known limitation: *"there is no fork choice / block recovery yet (an
honest, reliable local network is assumed)"*.

The slot loop (`lasair_testnet_node.ml:254-286`) advances `parent`/`db` ONLY when a
block for the **current** slot is authored or imported successfully. Consequences:

- **One miss = permanent fork.** If a node misses a slot's block (leader missed, block
  arrived late, node stalled), it keeps its old head. The next slot's block from the
  healthy chain then fails the importer's `parent_state_root` check forever after.
  There is no mechanism to fetch a missed block later. The importer
  (`Conformance.Trace_runner.import_block`, reached via `Chain.import` in
  `lasair/jamnp/chain.ml:213-217`) is behaving CORRECTLY — do not loosen the
  `bad_parent_state_root` check; it is the STF doing its job.

- **The blocking wait makes misses self-amplifying.** `inbox_wait !slot ~timeout:(2 *
  slot_seconds)` (`lasair_testnet_node.ml:271`) blocks up to **12 s — two full
  slots** — waiting for a block that may never come. A node that times out is now ≥2
  slots behind; `wait_for_slot` returns immediately for past slots so it bursts
  through the backlog (observed signature #5), but any block it missed during the
  stall has already forked it. Worse, if that node was the leader of an intervening
  slot, it authors late or not at all, propagating misses to everyone else.

- **Forks then disagree on the leader schedule** (signatures #3, #4). Leadership is
  derived purely from local on-chain state (`Chain.plan_slot`,
  `lasair/jamnp/chain.ml:162-203`). Forks that diverged within the current epoch
  still share the schedule (gamma_s was fixed at the epoch's start), but at each
  epoch boundary the new schedule/leader depends on the chain's accumulated entropy
  (eta), which differs per fork once histories diverge — every author contributes a
  distinct VRF `entropy_source`, so eta separates on the first divergent block.
  Tiny spec = 12-slot epochs, so a boundary lands every 72 s and the divergence
  compounds fast: soon several nodes each believe they lead the same slot.

- **The inbox drops competing blocks** (aggravator). `inbox` is keyed by slot with
  `Hashtbl.replace` (`lasair_testnet_node.ml:108-111`), so when multiple forks author
  the same slot, the last block to arrive silently overwrites earlier ones. A node
  that would have accepted the correct block can have it clobbered by a fork's block.

Bootstrap note: each node builds its OWN genesis locally from the same deterministic
inputs (`build_freerun_genesis`, seeds = `Bytes.make 32 (chr (i+5))`, tau anchored to
`(now_slot / E) * E` — `lasair_testnet_node.ml:215-218`). If two nodes boot in
different epochs of wall-clock time they'd have different tau0 and fork from genesis;
the compose healthchecks make this unlikely but a fixed implementation should not rely
on it.

## Fix specification (all in `lasair`)

In priority order; 1+2 are the minimum to make the testnet self-healing.

### 1. Cap the block wait at the slot boundary (one-liner)

`lasair_testnet_node.ml:271`: replace the relative `2 × slot_seconds` timeout with an
absolute deadline = the wall-clock start of slot `!slot + 1` (see `wait_for_slot` /
`Lasair.Overview.unix_of_timeslot` for the conversion). A node then never falls behind
by more than the slot it is already in, and a leader is never still stuck waiting when
its own authoring slot arrives.

### 2. Block store + ancestor back-fill (the real fix)

Make a missed/late block recoverable instead of a permanent fork:

- Every node keeps a bounded store of blocks it has seen (its own authored blocks and
  gossiped ones), indexed by header hash AND by slot.
- Add a request/response message to the gossip protocol (currently the wire framing
  in `lasair_testnet_node.ml` `frame`/`reader`/`broadcast`, block codec in
  `Jamnp_core.Protocol` — today it carries exactly one message type, a serialized
  block, so the framing needs a message tag): "give me the block whose header hash is
  H" (and/or "blocks for slots [a..b]").
- On `bad_parent_state_root` or a missed slot: request the missing parent(s) by the
  rejected block's `header.parent` hash, import the chain of ancestors in order, then
  re-import the tip. Loop until caught up (bound the walk-back depth).
- Do NOT change the importer; recovery is entirely a driver/network concern.
- Transport hazard to fix while in here: `broadcast` writes with `output_bytes` +
  `flush` while holding the per-link mutex (`lasair_testnet_node.ml:129-134`), on the
  LEADER'S MAIN SLOT-LOOP THREAD. A stalled peer whose TCP window fills would block
  the leader's authoring loop. Harmless today (empty blocks are tiny), but a
  request/response protocol serves larger payloads — move sends to per-peer writer
  threads or make the sockets non-blocking with a drop-on-full policy.

### 3. Inbox must hold multiple candidates per slot

Change `inbox` from `slot -> block` (replace-on-write) to `slot -> block list` keyed
also by header hash, and try importing each candidate. Once fork choice exists this
becomes "store all, choose by fork rule".

### 4. Fork choice (longer term, already named as planned work in the file header)

Track competing heads (height, hash), import blocks that extend ANY known block (not
just the current head at the current slot), and re-org to the best chain. With 1–3 in
place the network self-heals from transient stalls; 4 is what makes it robust to
sustained partitions.

## Acceptance test

Run the 6-node compose (`docker-compose.testnet.yml` in jamswap, or the equivalent in
lasair). Then `docker pause` one validator and unpause. **The pause must span at
least one of the paused node's own leadership slots** — pause ≥ V slots (≥ 36 s at
6 validators × 6 s; ≥ 2 epochs is even better). Reason: `docker pause` only freezes
the process; TCP keeps queueing in kernel buffers, so on unpause the reader thread
drains every missed block into the inbox and the burst loop imports them in order —
a paused node that never held a leadership slot can recover cleanly even WITHOUT the
fix, making a short pause a flaky "before" demonstration. The guaranteed fork
trigger is the paused node reaching its own past leadership slot during the burst:
it authors and self-imports a block every other node has already moved past, then
rejects the canonical next block with `bad_parent_state_root` forever. Before the
fix: that node forks permanently and the log fills with `bad_parent_state_root`
from all nodes. After the fix: the node back-fills the missed
blocks, rejoins the canonical chain, and steady state returns to one 🚀 authored +
five ⬇ imported lines per slot with no ✗ lines. Also verify heights converge (all
nodes report the same head via `GET /v1/head`).

## Log excerpt (evidence)

```
15:33:58.336  node4  🚀 authored slot 7878934  head 0xebdab48a9b444717..  (#7)
15:34:06.526  node1  ⏳ slot 7878929  no block from node3 (leader missed?)   <- node1 wakes 8s late, 5 slots behind
15:34:06.531  node1  ✗ rejected slot 7878934 from node3: bad_parent_state_root  <- forked from here on
15:34:18.622  node3  ⏳ slot 7878937  no block from node1 (leader missed?)   <- node3 thinks node1 leads...
15:34:18.621  node1  ⏳ slot 7878937  no block from node4 (leader missed?)   <- ...node1 thinks node4 leads
15:36:18.025  node4  🚀 authored slot 7878962  head 0x3dfccf4dfa9fb73a..  (#14)  <- four authors,
15:36:18.028  node5  🚀 authored slot 7878962  head 0xe2ed903bc705823f..  (#16)     same slot,
15:36:18.092  node3  🚀 authored slot 7878962  head 0x08ab0d94bdac7c44..  (#14)     four heads
15:36:18.100  node1  🚀 authored slot 7878962  head 0xbc78feda4a5b10a9..  (#11)
```
