//! Round identity and landed-round markers: how the off-chain builder learns, EXACTLY,
//! whether a round it submitted has settled.
//!
//! Before this, the builder inferred settlement from the market's cumulative volume
//! (`cv >= cv_before + volume`), which cannot tell its round from any other round that moved
//! the same volume, and cannot see a zero-volume round at all. So a round abandoned after a
//! timeout that settled later was never noticed: its orders got no receipts and were later
//! recorded "rejected". Now refine derives a round id from the work-item payload, accumulate
//! files it under a marker once the round is accepted, and the builder (which hashes the
//! payload it submits) reads the marker: `landed(round) = marker(round_id) exists`, for the
//! live round and for abandoned ones.
//!
//! ## The id (the one definition; server.py `round_id` must match byte for byte)
//!
//! ```text
//! round_id = blake2s-256( b"jamswap:v1:round" ‖ payload )   the whole work-item payload,
//!                                                           exactly as refine receives it
//! ```
//!
//! The payload is everything the round IS: market, the signed orders, the prune list, the
//! reveals or ciphertexts and committee partials, and the input book. An id over only some of
//! it (it used to be book hash ‖ bindings ‖ consumed) let two different rounds share one id —
//! a prune-only round had the same id as a no-op anyone could submit, and a copy of a round
//! with a different prune list cleared differently under the same id. A byte-identical
//! payload is the same round. One that carries orders or reveals can be accepted only once
//! (its order floors rise, its commits are consumed); one that only prunes changes the book,
//! so it could land again only on a byte-identical book, re-filing the same marker. The
//! service refuses a round that would change nothing at all.
//!
//! Refine computes the id (~8.3k gas per 64-byte blake2s block on lasair's 0.8.0 PVM, so
//! ~1M gas for a 48-order round's ~8 KB payload: negligible against refine's budget, but a
//! real slice of the 10M a work-report's items share for accumulate) and carries it in the
//! output's auth trailer (`wire::push_auth_trailer`). Accumulate trusts it exactly as it trusts
//! the book hash beside it: it is this service's own refine output, attested by the
//! guarantors and re-run by auditors.
//!
//! ## Markers: `b"rl"‖round_id → slot(4 LE)`, expired by age only
//!
//! One key per accepted round, holding the slot it landed in. They used to be a per-market
//! ring of the newest 32 ids, and a ring can be flushed: anyone who landed 32 other rounds
//! (free no-op rounds, or cheap self-signed ones) pushed a round the builder was still
//! confirming out of it, and the builder then read a round that DID settle as reverted and
//! re-batched its filled orders. A marker is removed only once it is `LANDED_TTL_SLOTS` old,
//! by a garbage collector that walks a FIFO of (id, slot) entries:
//! `b"rlq"‖n(8 LE) → id ‖ slot` for n in `[head, tail)`, with `b"rlh" → head ‖ tail`.
//! Every accept appends one entry and reaps at most `GC_STEPS` expired ones from the head, so
//! each accept costs a bounded ~15 host calls however many rounds are live, and the backlog
//! drains (each accept reaps more than it adds). The slot also lets the builder tell how deep
//! a landing is (it can compare it with the finalized slot).

use crate::Kv;
use blake2::{Blake2s256, Digest};

pub const ROUND_ID_DOMAIN: &[u8] = b"jamswap:v1:round";
/// How long a landed-round marker lives. It has to outlast the builder's interest in a round:
/// first sighting within one 2 s poll of the landing, then at most its settle hold (150 s
/// default) — so ~600 slots (~1 h at 6 s) is a wide margin.
pub const LANDED_TTL_SLOTS: u32 = 600;
/// Expired markers reaped per accept (> 1, so the backlog shrinks while rounds keep landing).
pub const GC_STEPS: usize = 4;
const ID_LEN: usize = 32;
const Q_ENTRY_LEN: usize = ID_LEN + 4;
const HEAD_KEY: &[u8] = b"rlh";

/// The id of a round: blake2s-256 over the domain and its work-item payload (module docs).
pub fn round_id(payload: &[u8]) -> [u8; 32] {
    let mut h = Blake2s256::new();
    h.update(ROUND_ID_DOMAIN);
    h.update(payload);
    let mut id = [0u8; 32];
    id.copy_from_slice(&h.finalize());
    id
}

/// `b"rl"‖round_id` — the round's landed marker (value: the slot it landed in).
pub fn landed_key(rid: &[u8; 32]) -> [u8; 34] {
    let mut k = [0u8; 34];
    k[..2].copy_from_slice(b"rl");
    k[2..].copy_from_slice(rid);
    k
}

fn queue_key(n: u64) -> [u8; 11] {
    let mut k = [0u8; 11];
    k[..3].copy_from_slice(b"rlq");
    k[3..].copy_from_slice(&n.to_le_bytes());
    k
}

fn u32_at(b: &[u8], off: usize) -> u32 {
    u32::from_le_bytes([b[off], b[off + 1], b[off + 2], b[off + 3]])
}
fn u64_at(b: &[u8], off: usize) -> u64 {
    let mut x = [0u8; 8];
    x.copy_from_slice(&b[off..off + 8]);
    u64::from_le_bytes(x)
}

/// The slot `rid` landed in, if it is marked.
pub fn landed_slot(kv: &impl Kv, rid: &[u8; 32]) -> Option<u32> {
    kv.get(&landed_key(rid)).filter(|v| v.len() >= 4).map(|v| u32_at(&v, 0))
}

/// File an accepted round as landed at `slot`, first reaping up to `GC_STEPS` markers older
/// than `LANDED_TTL_SLOTS`. Accumulate runs in slot order along a chain, so the FIFO is in
/// slot order and reaping stops at the first live entry.
pub fn mark_landed(kv: &mut impl Kv, rid: &[u8; 32], slot: u32) {
    let (mut head, tail) = match kv.get(HEAD_KEY) {
        Some(v) if v.len() >= 16 => (u64_at(&v, 0), u64_at(&v, 8)),
        _ => (0, 0),
    };
    let mut steps = 0;
    while head < tail && steps < GC_STEPS {
        let qk = queue_key(head);
        if let Some(e) = kv.get(&qk).filter(|e| e.len() >= Q_ENTRY_LEN) {
            let landed = u32_at(&e, ID_LEN);
            if landed.saturating_add(LANDED_TTL_SLOTS) > slot {
                break; // the oldest entry is still live, so every later one is too
            }
            let mut old = [0u8; 32];
            old.copy_from_slice(&e[..ID_LEN]);
            // only the marker THIS entry wrote: an id filed again later keeps its newer mark
            if landed_slot(kv, &old) == Some(landed) {
                kv.remove(&landed_key(&old));
            }
        }
        kv.remove(&qk);
        head += 1;
        steps += 1;
    }
    kv.set(&landed_key(rid), &slot.to_le_bytes());
    let mut e = [0u8; Q_ENTRY_LEN];
    e[..ID_LEN].copy_from_slice(rid);
    e[ID_LEN..].copy_from_slice(&slot.to_le_bytes());
    kv.set(&queue_key(tail), &e);
    let mut ht = [0u8; 16];
    ht[..8].copy_from_slice(&head.to_le_bytes());
    ht[8..].copy_from_slice(&(tail + 1).to_le_bytes());
    kv.set(HEAD_KEY, &ht);
}

#[cfg(test)]
mod tests {
    use super::*;
    use alloc::collections::BTreeMap;

    fn hex(b: &[u8]) -> alloc::string::String {
        use core::fmt::Write;
        let mut s = alloc::string::String::new();
        for x in b {
            write!(s, "{:02x}", x).unwrap();
        }
        s
    }

    #[derive(Default)]
    struct Mem(BTreeMap<Vec<u8>, Vec<u8>>);
    impl Kv for Mem {
        fn get(&self, key: &[u8]) -> Option<Vec<u8>> {
            self.0.get(key).cloned()
        }
        fn set(&mut self, key: &[u8], value: &[u8]) {
            self.0.insert(key.to_vec(), value.to_vec());
        }
        fn remove(&mut self, key: &[u8]) {
            self.0.remove(key);
        }
    }
    fn id(i: u32) -> [u8; 32] {
        round_id(&i.to_le_bytes())
    }

    // The shared fixture: offchain/tests/test_round_poison.py hashes the same payload with
    // server.round_id and must get this exact id.
    const FIXTURE_PAYLOAD: &[u8] = b"\x0c\x01\x00\x00\x00jamswap round fixture";

    #[test]
    fn fixture_id_matches_the_server() {
        assert_eq!(
            hex(&round_id(FIXTURE_PAYLOAD)),
            "097a0379d26b1553e50fcb50e9e29024d65bf520be3ee4243f69c8660a3f5e3d"
        );
    }

    #[test]
    fn the_id_binds_every_payload_byte() {
        let base = round_id(FIXTURE_PAYLOAD);
        assert_eq!(base, round_id(FIXTURE_PAYLOAD), "deterministic");
        for i in 0..FIXTURE_PAYLOAD.len() {
            let mut p = FIXTURE_PAYLOAD.to_vec();
            p[i] ^= 1;
            assert_ne!(base, round_id(&p), "byte {i}");
        }
        // a prune-only round and the no-op on the same book used to share an id
        let noop = b"\x0c\x01\x00\x00\x00\x00\x00\x00\x00";
        let prune = b"\x0c\x01\x00\x00\x00\x00\x00\x01\x00\x07\x00\x00\x00\x01\x00\x00\x00";
        assert_ne!(round_id(noop), round_id(prune));
        // the domain separates it from a bare hash of the payload
        let mut h = Blake2s256::new();
        h.update(FIXTURE_PAYLOAD);
        assert_ne!(&base[..], &h.finalize()[..]);
    }

    #[test]
    fn a_marked_round_reads_back_its_slot() {
        let mut kv = Mem::default();
        assert_eq!(landed_slot(&kv, &id(1)), None);
        mark_landed(&mut kv, &id(1), 77);
        assert_eq!(landed_slot(&kv, &id(1)), Some(77));
        assert_eq!(landed_key(&id(1))[..2], *b"rl");
    }

    #[test]
    fn no_number_of_later_rounds_evicts_a_live_marker() {
        // the ring this replaces kept the newest 32: 32 more landings flushed a round the
        // builder was still confirming. Markers only expire by age.
        let mut kv = Mem::default();
        mark_landed(&mut kv, &id(0), 100);
        for i in 1..=1000 {
            mark_landed(&mut kv, &id(i), 100 + i / 10); // 1000 rounds within ~100 slots
        }
        assert_eq!(landed_slot(&kv, &id(0)), Some(100), "still marked after 1000 later rounds");
    }

    #[test]
    fn markers_expire_by_age_and_the_backlog_drains() {
        let mut kv = Mem::default();
        for i in 0..20 {
            mark_landed(&mut kv, &id(i), 10);
        }
        // not yet expired: nothing reaped
        mark_landed(&mut kv, &id(100), 10 + LANDED_TTL_SLOTS - 1);
        assert_eq!(landed_slot(&kv, &id(0)), Some(10));
        // expired: each accept reaps up to GC_STEPS from the head, oldest first
        mark_landed(&mut kv, &id(101), 10 + LANDED_TTL_SLOTS);
        for i in 0..GC_STEPS as u32 {
            assert_eq!(landed_slot(&kv, &id(i)), None, "reaped {i}");
        }
        assert_eq!(landed_slot(&kv, &id(GC_STEPS as u32)), Some(10), "bounded work per accept");
        for j in 0..10 {
            mark_landed(&mut kv, &id(200 + j), 10 + LANDED_TTL_SLOTS);
        }
        for i in 0..20 {
            assert_eq!(landed_slot(&kv, &id(i)), None, "the backlog drained");
        }
        assert_eq!(landed_slot(&kv, &id(100)), Some(10 + LANDED_TTL_SLOTS - 1), "live one kept");
        // the reaped queue entries are gone too: storage is bounded by what is live
        let live_q = kv.0.keys().filter(|k| k.starts_with(b"rlq")).count();
        let live_m = kv.0.keys().filter(|k| k.len() == 34 && k.starts_with(b"rl")).count();
        assert_eq!(live_q, live_m);
        assert_eq!(live_m, 1 + 1 + 10);
    }

    #[test]
    fn an_id_filed_again_keeps_its_newer_mark_when_the_old_entry_expires() {
        let mut kv = Mem::default();
        mark_landed(&mut kv, &id(5), 10);
        mark_landed(&mut kv, &id(5), 10 + LANDED_TTL_SLOTS - 1);
        mark_landed(&mut kv, &id(6), 10 + LANDED_TTL_SLOTS); // reaps the first entry
        assert_eq!(landed_slot(&kv, &id(5)), Some(10 + LANDED_TTL_SLOTS - 1));
    }
}
