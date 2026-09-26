//! Replay floors: the state-side half of signed-op replay protection. Refine verifies every
//! signature statelessly (see `auth.rs`); accumulate, which can read state, then refuses a
//! signature it has already consumed. The rules live here, pure and host-testable, and the
//! service's accumulate runs them against its storage through [`FloorStore`].
//!
//! Two per-account monotonic floors, each a little-endian u64 in service storage:
//!   * `b"sq"‖handle` — the ORDER floor. Every signed public order's seq must strictly beat
//!     it; a settling round raises it to that round's highest seq per account.
//!   * `b"sc"‖handle` — the COMMIT floor. Every owner-signed sealed commit (TAG_COMMIT /
//!     TAG_ENC_COMMIT) must strictly beat it, and raises it.
//!
//! Why two floors: they used to be ONE (`b"sq"`), and the UI and loadgen draw order seqs and
//! commit seqs from one per-account counter. A sealed commit is a small standalone work-item
//! submitted at placement, so it usually lands before that trader's OLDER public orders,
//! which wait for an auction round. The commit raised the shared floor past them, the round
//! carrying them then failed `check_bindings` WHOLE (every other trader's orders with it),
//! and the stale orders could never settle. Measured in the 2026-09-24 lasair6 soak: every
//! abandoned round had a same-account commit that landed first (jamswap-late-settlement
//! analysis, 26/26 rejection groups).
//!
//! Why it stays replay-proof: an order signs `canon("order", …)` and a commit signs
//! `canon("commit", …)`, so neither signature verifies as the other kind; each kind's replay
//! is refused by its own floor. An order and a commit may carry the same seq value.
//!
//! Migration note: a fresh genesis starts both floors at 0. The service has no in-place
//! upgrade path today; one that kept existing state would have to seed `b"sc"` from `b"sq"`,
//! or pre-upgrade commit signatures could be replayed once.

use crate::wire::{Binding, FLAG_MARKET};
use alloc::vec::Vec;

/// Storage the floors are read from and written to (the service implements it over
/// get_storage / set_storage; tests over a map). A missing key reads as floor 0.
pub trait FloorStore {
    fn floor(&self, key: &[u8]) -> u64;
    fn set_floor(&mut self, key: &[u8], v: u64);
}

fn floor_key(prefix: &[u8; 2], handle: u32) -> [u8; 6] {
    let h = handle.to_le_bytes();
    [prefix[0], prefix[1], h[0], h[1], h[2], h[3]]
}

/// `b"sq"‖handle` — the public-order seq floor (the server's round builder reads it too).
pub fn order_floor_key(handle: u32) -> [u8; 6] {
    floor_key(b"sq", handle)
}

/// `b"sc"‖handle` — the sealed-commit seq floor.
pub fn commit_floor_key(handle: u32) -> [u8; 6] {
    floor_key(b"sc", handle)
}

/// Admit an owner-signed sealed commit at `seq` for `account`: true (and the commit floor
/// rises to `seq`) iff `seq` strictly beats the account's COMMIT floor. The caller checks the
/// signer is the account's registered key first. The ORDER floor is neither read nor written.
pub fn admit_commit(store: &mut impl FloorStore, account: u32, seq: u64) -> bool {
    let key = commit_floor_key(account);
    if seq <= store.floor(&key) {
        return false; // replayed (or superseded) commit signature
    }
    store.set_floor(&key, seq);
    true
}

/// The state-side check of a round's public-order bindings (refine already verified every
/// signature, and that a limit order executes at its signed price). ALL of: each binding's
/// carried pubkey is the account's registered key; each seq strictly beats the account's
/// running ORDER floor (it starts at the stored floor and rises to each admitted seq, in round
/// order, so a replayed or intra-round duplicate seq fails); a market order's builder-derived
/// price sits within `band_pct` percent of the on-chain last price, which must exist.
///
/// Returns the per-account floors to commit once the round is otherwise accepted, or None to
/// reject the whole round. Fail-closed on purpose: refine cleared every order together, so
/// dropping one would still let it move the uniform price for everyone else.
pub fn check_bindings(
    bindings: &[Binding],
    store: &impl FloorStore,
    registered_key: impl Fn(u32) -> Option<[u8; 32]>,
    last_price: u64,
    band_pct: u128,
) -> Option<Vec<(u32, u64)>> {
    let mut floors: Vec<(u32, u64)> = Vec::new();
    for b in bindings {
        if registered_key(b.account)? != b.pubkey {
            return None; // unregistered account or a key that isn't the registered one
        }
        let i = match floors.iter().position(|(a, _)| *a == b.account) {
            Some(i) => i,
            None => {
                floors.push((b.account, store.floor(&order_floor_key(b.account))));
                floors.len() - 1
            }
        };
        if b.seq <= floors[i].1 {
            return None; // replayed (or intra-round duplicate) order signature
        }
        floors[i].1 = b.seq;
        if b.flags & FLAG_MARKET != 0 {
            if last_price == 0 {
                return None; // no reference price — a market order can't be bounded
            }
            let (p, l) = (b.price as u128, last_price as u128);
            if p * 100 < l * (100 - band_pct) || p * 100 > l * (100 + band_pct) {
                return None; // builder-derived price outside the band the trader accepted
            }
        }
    }
    Some(floors)
}

/// Commit the ORDER floors [`check_bindings`] returned, once the round is accepted.
pub fn raise_order_floors(store: &mut impl FloorStore, floors: &[(u32, u64)]) {
    for (a, s) in floors {
        store.set_floor(&order_floor_key(*a), *s);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use alloc::collections::BTreeMap;

    #[derive(Default)]
    struct Mem(BTreeMap<Vec<u8>, u64>);
    impl FloorStore for Mem {
        fn floor(&self, key: &[u8]) -> u64 {
            self.0.get(key).copied().unwrap_or(0)
        }
        fn set_floor(&mut self, key: &[u8], v: u64) {
            self.0.insert(key.to_vec(), v);
        }
    }

    const PK: [u8; 32] = [7; 32];
    fn registered(a: u32) -> Option<[u8; 32]> {
        if a == 7 || a == 8 { Some(PK) } else { None }
    }
    fn order(account: u32, seq: u64) -> Binding {
        Binding { account, seq, pubkey: PK, flags: 0, price: 100 }
    }
    // accumulate's sequence for a round: check, then (accepted) raise the order floors
    fn settle(s: &mut Mem, bindings: &[Binding]) -> bool {
        match check_bindings(bindings, s, registered, 100, 10) {
            Some(f) => { raise_order_floors(s, &f); true }
            None => false,
        }
    }

    #[test]
    fn keys_are_distinct_prefixed_handles() {
        assert_eq!(order_floor_key(7), *b"sq\x07\x00\x00\x00");
        assert_eq!(commit_floor_key(7), *b"sc\x07\x00\x00\x00");
    }

    #[test]
    fn a_commit_at_seq_20_does_not_reject_an_order_at_seq_15() {
        // the soak's failure, in miniature: the trader signed order seq 15, then a sealed
        // order whose commit (seq 20) landed first. With one shared floor the round carrying
        // order 15 failed whole; with separate floors it settles.
        let mut s = Mem::default();
        assert!(admit_commit(&mut s, 7, 20));
        assert_eq!(s.floor(&order_floor_key(7)), 0, "a commit never touches the order floor");
        assert!(settle(&mut s, &[order(7, 15), order(8, 3)]), "order 15 still settles");
        assert_eq!(s.floor(&order_floor_key(7)), 15);
        assert_eq!(s.floor(&commit_floor_key(7)), 20);
    }

    #[test]
    fn an_order_floor_does_not_reject_a_commit() {
        // the reverse race: a round raising the order floor to 30 leaves commit 21 valid
        let mut s = Mem::default();
        assert!(settle(&mut s, &[order(7, 30)]));
        assert!(admit_commit(&mut s, 7, 21));
    }

    #[test]
    fn commit_replay_is_refused() {
        let mut s = Mem::default();
        assert!(admit_commit(&mut s, 7, 20));
        assert!(!admit_commit(&mut s, 7, 20), "the same commit signature twice");
        assert!(!admit_commit(&mut s, 7, 19), "an older commit signature");
        assert_eq!(s.floor(&commit_floor_key(7)), 20, "a refused commit leaves the floor alone");
        assert!(admit_commit(&mut s, 7, 21));
        assert!(admit_commit(&mut s, 8, 1), "floors are per account");
    }

    #[test]
    fn order_replay_is_refused() {
        let mut s = Mem::default();
        assert!(settle(&mut s, &[order(7, 5), order(7, 6)]));
        assert!(!settle(&mut s, &[order(7, 6)]), "a settled seq replayed");
        assert!(!settle(&mut s, &[order(7, 4)]), "an older seq");
        assert!(!settle(&mut s, &[order(7, 9), order(7, 9)]), "an intra-round duplicate");
        assert!(!settle(&mut s, &[order(7, 9), order(7, 8)]), "seqs out of order in a round");
        assert_eq!(s.floor(&order_floor_key(7)), 6, "a rejected round leaves the floor alone");
        assert!(settle(&mut s, &[order(7, 7)]));
    }

    #[test]
    fn one_stale_binding_rejects_the_whole_round() {
        // fail-closed stays: a round mixing a fresh order with a stale one is refused whole
        let mut s = Mem::default();
        assert!(settle(&mut s, &[order(7, 10)]));
        assert!(!settle(&mut s, &[order(8, 1), order(7, 10)]));
        assert_eq!(s.floor(&order_floor_key(8)), 0, "the fresh order's floor didn't move");
    }

    #[test]
    fn wrong_or_unregistered_key_is_refused() {
        let s = Mem::default();
        let mut b = order(7, 1);
        b.pubkey = [9; 32];
        assert!(check_bindings(&[b], &s, registered, 100, 10).is_none(), "not the registered key");
        assert!(check_bindings(&[order(99, 1)], &s, registered, 100, 10).is_none(), "unregistered");
    }

    #[test]
    fn market_orders_are_band_checked_against_the_last_price() {
        let s = Mem::default();
        let mkt = |price| Binding { account: 7, seq: 1, pubkey: PK, flags: FLAG_MARKET, price };
        assert!(check_bindings(&[mkt(100)], &s, registered, 0, 10).is_none(), "no last price");
        assert!(check_bindings(&[mkt(90)], &s, registered, 100, 10).is_some(), "lower edge");
        assert!(check_bindings(&[mkt(110)], &s, registered, 100, 10).is_some(), "upper edge");
        assert!(check_bindings(&[mkt(89)], &s, registered, 100, 10).is_none());
        assert!(check_bindings(&[mkt(111)], &s, registered, 100, 10).is_none());
        // a limit order isn't band-checked (refine pinned it to the signed price)
        assert!(check_bindings(&[order(7, 1)], &s, registered, 0, 10).is_some());
    }
}
