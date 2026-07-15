//! Pure byte-set operations for the jamswap service's on-chain commit sets.
//!
//! The service keeps hidden sealed-order commitments in two parallel sets:
//!   * `b"commits"‖market` — `key`-wide entries (`cid(32) ‖ account(4)` = 36 B) matched
//!     byte-exact by consume_set and the off-chain reveal gate; and
//!   * `b"cage"‖market`    — an age index, one entry per live commit carrying its expiry
//!     slot (`cid(32) ‖ account(4) ‖ expiry(4)` = 40 B), so abandoned commits (never
//!     revealed) can be garbage-collected instead of growing state forever.
//!
//! Keeping the two in step — add together, purge together on consume, reap together on
//! expiry — is fiddly index arithmetic that MUST be deterministic and byte-exact across
//! every validator. These helpers are storage-free and `no_std` so the service can call
//! them in `accumulate` while they are exhaustively unit-tested on the host (the service
//! crate itself is a PVM module that can't host a std test harness).
#![no_std]
extern crate alloc;
use alloc::vec::Vec;

/// Little-endian u32 read at `off` (0 if out of range).
fn ru32(b: &[u8], off: usize) -> u32 {
    if off + 4 > b.len() {
        return 0;
    }
    u32::from_le_bytes([b[off], b[off + 1], b[off + 2], b[off + 3]])
}

/// Remove, first-match, each `key`-byte target from a set of `stride`-wide entries,
/// comparing the leading `key` bytes of each entry. Returns the compacted set.
///
/// - Works whether `stored` is the 36 B `b"commits"` set or the 40 B `b"cage"` set (the
///   comparison only ever looks at the leading `key` bytes), so one function purges both.
/// - `targets` is a flat concatenation of `key`-byte keys.
/// - First-match, one entry removed per target: duplicate entries are not all wiped by a
///   single target (mirrors consume_set, so consuming one commit removes exactly one).
pub fn remove_first_match(stored: &[u8], targets: &[u8], stride: usize, key: usize) -> Vec<u8> {
    if stride == 0 || key == 0 || key > stride {
        return stored.to_vec();
    }
    let n = stored.len() / stride;
    let mut removed = Vec::new();
    removed.resize(n, false);
    for c in 0..(targets.len() / key) {
        let t = &targets[c * key..(c + 1) * key];
        for j in 0..n {
            if !removed[j] && &stored[j * stride..j * stride + key] == t {
                removed[j] = true;
                break;
            }
        }
    }
    let mut out = Vec::with_capacity(stored.len());
    for j in 0..n {
        if !removed[j] {
            out.extend_from_slice(&stored[j * stride..(j + 1) * stride]);
        }
    }
    out
}

/// Split an age index into `(kept, reaped_keys)` at slot `now`. Each `entry`-wide index
/// entry is `key`-bytes of identity followed by a little-endian u32 expiry slot at offset
/// `key`. An entry with `expiry <= now` is reaped; `reaped_keys` is the flat concatenation
/// of the `key`-byte prefixes of every reaped entry (exactly what `remove_first_match`
/// takes to prune the parallel `b"commits"` set). Expiry is INCLUSIVE at `now` — a commit
/// is dead the slot it expires, not the slot after.
pub fn reap_expired(index: &[u8], now: u32, entry: usize, key: usize) -> (Vec<u8>, Vec<u8>) {
    if entry == 0 || key + 4 > entry {
        return (index.to_vec(), Vec::new());
    }
    let n = index.len() / entry;
    let mut kept = Vec::with_capacity(index.len());
    let mut reaped = Vec::new();
    for j in 0..n {
        let e = &index[j * entry..(j + 1) * entry];
        if ru32(e, key) <= now {
            reaped.extend_from_slice(&e[..key]);
        } else {
            kept.extend_from_slice(e);
        }
    }
    (kept, reaped)
}

#[cfg(test)]
mod tests {
    use super::*;
    use alloc::vec::Vec;

    const KEY: usize = 36; // cid(32) ‖ account(4)
    const CAGE: usize = 40; // + expiry(4)

    fn key_entry(tag: u8, account: u32) -> Vec<u8> {
        let mut e = Vec::new();
        e.extend_from_slice(&[tag; 32]);
        e.extend_from_slice(&account.to_le_bytes());
        e
    }
    fn cage_entry(tag: u8, account: u32, expiry: u32) -> Vec<u8> {
        let mut e = key_entry(tag, account);
        e.extend_from_slice(&expiry.to_le_bytes());
        e
    }

    #[test]
    fn reap_splits_on_expiry() {
        let mut cage = Vec::new();
        cage.extend_from_slice(&cage_entry(1, 10, 100));
        cage.extend_from_slice(&cage_entry(2, 20, 200));
        cage.extend_from_slice(&cage_entry(3, 30, 100));
        let (kept, reaped) = reap_expired(&cage, 150, CAGE, KEY);
        assert_eq!(kept.len(), CAGE, "only entry 2 survives");
        assert_eq!(&kept[..32], &[2u8; 32]);
        assert_eq!(reaped.len(), 2 * KEY, "two reaped, each a 36B key");
        assert_eq!(&reaped[..32], &[1u8; 32]);
        assert_eq!(&reaped[KEY..KEY + 32], &[3u8; 32]);
    }

    #[test]
    fn reap_expiry_is_inclusive_at_slot() {
        let cage = cage_entry(7, 1, 100);
        assert_eq!(reap_expired(&cage, 100, CAGE, KEY).1.len(), KEY, "expiry<=now reaps");
        assert_eq!(reap_expired(&cage, 99, CAGE, KEY).1.len(), 0, "not yet expired");
    }

    #[test]
    fn reap_nothing_expired_returns_input() {
        let cage = cage_entry(1, 10, 5000);
        let (kept, reaped) = reap_expired(&cage, 100, CAGE, KEY);
        assert_eq!(kept, cage);
        assert!(reaped.is_empty());
    }

    #[test]
    fn remove_prunes_commits() {
        let mut commits = Vec::new();
        commits.extend_from_slice(&key_entry(1, 10));
        commits.extend_from_slice(&key_entry(2, 20));
        commits.extend_from_slice(&key_entry(3, 30));
        let mut reaped = Vec::new();
        reaped.extend_from_slice(&key_entry(1, 10));
        reaped.extend_from_slice(&key_entry(3, 30));
        let out = remove_first_match(&commits, &reaped, KEY, KEY);
        assert_eq!(out.len(), KEY, "only entry 2 remains");
        assert_eq!(&out[..32], &[2u8; 32]);
    }

    #[test]
    fn remove_works_across_strides() {
        // purge a 40B cage using a 36B target (leading 36B compared) — the cage_purge path.
        let mut cage = Vec::new();
        cage.extend_from_slice(&cage_entry(1, 10, 100));
        cage.extend_from_slice(&cage_entry(2, 20, 200));
        let out = remove_first_match(&cage, &key_entry(1, 10), CAGE, KEY);
        assert_eq!(out.len(), CAGE, "entry 1 pruned from the 40B cage");
        assert_eq!(&out[..32], &[2u8; 32]);
    }

    #[test]
    fn remove_only_one_per_target() {
        let mut commits = Vec::new();
        commits.extend_from_slice(&key_entry(9, 1));
        commits.extend_from_slice(&key_entry(9, 1));
        let out = remove_first_match(&commits, &key_entry(9, 1), KEY, KEY);
        assert_eq!(out.len(), KEY, "only one of the duplicates removed");
    }

    #[test]
    fn account_binding_matters() {
        // same cid, different account: reaping account 10 must not remove account 20's commit.
        let mut commits = Vec::new();
        commits.extend_from_slice(&key_entry(5, 10));
        commits.extend_from_slice(&key_entry(5, 20));
        let out = remove_first_match(&commits, &key_entry(5, 10), KEY, KEY);
        assert_eq!(out.len(), KEY);
        assert_eq!(&out[32..36], &20u32.to_le_bytes(), "account 20's commit survives");
    }

    #[test]
    fn reap_then_remove_end_to_end() {
        // the full GC: an index of three commits (two expired) reaps to a 36B key list,
        // which prunes exactly those two from the parallel commit set.
        let mut cage = Vec::new();
        cage.extend_from_slice(&cage_entry(1, 10, 50));
        cage.extend_from_slice(&cage_entry(2, 20, 999));
        cage.extend_from_slice(&cage_entry(3, 30, 50));
        let mut commits = Vec::new();
        for (t, a) in [(1u8, 10u32), (2, 20), (3, 30)] {
            commits.extend_from_slice(&key_entry(t, a));
        }
        let (kept_cage, reaped) = reap_expired(&cage, 100, CAGE, KEY);
        let kept_commits = remove_first_match(&commits, &reaped, KEY, KEY);
        assert_eq!(kept_cage.len(), CAGE, "one commit survives in the index");
        assert_eq!(kept_commits.len(), KEY, "and one in the commit set");
        assert_eq!(&kept_cage[..32], &[2u8; 32]);
        assert_eq!(&kept_commits[..32], &[2u8; 32]);
    }
}
