//! Deposits: the `DEPOSIT` work item's wire format and the rule that credits each deposit
//! once. Pure and host-testable; the service's accumulate runs it through [`Kv`].
//!
//! `DEPOSIT` is the Phase-2 faucet (real custody, `on_transfer` against the JAM token
//! standard, is Phase 3). It is unsigned, so it has no signature nonce to replay-protect
//! it, and it used to credit on every accumulate: a duplicated work item (the builder's
//! fail-over is at-least-once, other clients' builders promise nothing, and any client may
//! re-submit after an unknown outcome) credited again. A 5 DOT deposit was seen credited 3×
//! (jamswap#7).
//!
//! ## Wire (the one definition; server.py `deposit_payload` must match byte for byte)
//!
//! ```text
//! [tag=1] account:u32 ‖ asset:u32 ‖ amount:u64 ‖ nonce:u64      25 bytes, little-endian
//! ```
//!
//! The nonce is an idempotency key the producer picks: unique per account, roughly
//! increasing, never 0. The builder uses its wall clock in nanoseconds (strictly increasing
//! per process, so a restart never reuses one); the fixed-script producers count 1, 2, ….
//! Exact length: the old 17-byte layout is refused, so no deposit bypasses the check.
//!
//! ## The rule: a floor plus a window of recent nonces, `b"dn"‖account`
//!
//! ```text
//! b"dn"‖account → floor:u64 ‖ k × nonce:u64      k ≤ WINDOW, ascending, every nonce > floor
//! ```
//!
//! A deposit is credited iff its nonce is above the floor and not in the window; it then
//! joins the window, and once the window holds more than [`WINDOW`] nonces the smallest is
//! evicted and becomes the new floor. Every credited nonce is therefore either in the window
//! or at/below the floor for good, so a copy is refused however late it lands.
//!
//! Why not a bare monotonic floor like the seq floors (`b"sq"`, `b"sc"`): deposits race.
//! Two submitted back to back (the signed-ops e2e funds DOT and USDC this way) go to
//! different guarantors and can accumulate in either order, and a bare floor would then
//! refuse the older one: a deposit lost. The window accepts any reordering shallower than
//! [`WINDOW`] deposits on one account. A deposit reordered deeper, or one reusing a nonce
//! already credited, is refused: the rule can drop a deposit, never credit one twice.
//! State per account is bounded at `8 + 8·WINDOW` bytes.

use crate::Kv;
use alloc::vec::Vec;

/// `[tag][account][asset][amount][nonce]`.
pub const DEPOSIT_LEN: usize = 1 + 4 + 4 + 8 + 8;
/// How many recent nonces per account are remembered above the floor (the reorder depth).
pub const WINDOW: usize = 16;

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub struct Deposit {
    pub account: u32,
    pub asset: u32,
    pub amount: u64,
    pub nonce: u64,
}

fn u32_at(b: &[u8], off: usize) -> u32 {
    u32::from_le_bytes([b[off], b[off + 1], b[off + 2], b[off + 3]])
}
fn u64_at(b: &[u8], off: usize) -> u64 {
    let mut x = [0u8; 8];
    x.copy_from_slice(&b[off..off + 8]);
    u64::from_le_bytes(x)
}

/// Decode a `DEPOSIT` work item (refine echoes the payload; the caller matched the tag byte).
/// `None` unless it is exactly [`DEPOSIT_LEN`] bytes.
pub fn decode(b: &[u8]) -> Option<Deposit> {
    if b.len() != DEPOSIT_LEN {
        return None;
    }
    Some(Deposit { account: u32_at(b, 1), asset: u32_at(b, 5), amount: u64_at(b, 9), nonce: u64_at(b, 17) })
}

/// `b"dn"‖account` — the account's deposit floor and window.
pub fn key(account: u32) -> [u8; 6] {
    let a = account.to_le_bytes();
    [b'd', b'n', a[0], a[1], a[2], a[3]]
}

/// Admit deposit `nonce` for `account`: true (record it, and the caller credits the deposit)
/// iff the nonce is above the account's floor and not already in its window. Refused
/// deposits write nothing.
pub fn admit(kv: &mut impl Kv, account: u32, nonce: u64) -> bool {
    let k = key(account);
    let v = kv.get(&k).unwrap_or_default();
    let floor = if v.len() >= 8 { u64_at(&v, 0) } else { 0 };
    if nonce <= floor {
        return false; // credited already, or older than the window can tell
    }
    let mut window: Vec<u64> = v.get(8..).unwrap_or(&[]).chunks_exact(8).map(|c| u64_at(c, 0)).collect();
    if window.contains(&nonce) {
        return false; // a copy of a deposit that already landed
    }
    let at = window.iter().position(|&n| n > nonce).unwrap_or(window.len());
    window.insert(at, nonce);
    let floor = if window.len() > WINDOW { window.remove(0) } else { floor };
    let mut out = Vec::with_capacity(8 + 8 * window.len());
    out.extend_from_slice(&floor.to_le_bytes());
    for n in &window {
        out.extend_from_slice(&n.to_le_bytes());
    }
    kv.set(&k, &out);
    true
}

#[cfg(test)]
mod tests {
    use super::*;
    use alloc::collections::BTreeMap;

    #[derive(Default, Clone, PartialEq, Debug)]
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

    /// accumulate's DEPOSIT arm in miniature: admit, then credit
    #[derive(Default)]
    struct Ledger {
        kv: Mem,
        bal: BTreeMap<u32, u64>,
    }
    impl Ledger {
        fn deposit(&mut self, account: u32, amount: u64, nonce: u64) -> bool {
            if !admit(&mut self.kv, account, nonce) {
                return false;
            }
            *self.bal.entry(account).or_default() += amount;
            true
        }
        fn bal(&self, account: u32) -> u64 {
            self.bal.get(&account).copied().unwrap_or(0)
        }
    }

    // The shared fixture: offchain/tests/test_deposit.py builds this exact payload with
    // server.deposit_payload(7, 1, 50_000, nonce=0x0102030405060708).
    const FIXTURE_HEX: &str = "01070000000100000050c30000000000000807060504030201";

    fn unhex(s: &str) -> Vec<u8> {
        (0..s.len()).step_by(2).map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap()).collect()
    }

    #[test]
    fn fixture_decodes_as_the_server_encodes_it() {
        let b = unhex(FIXTURE_HEX);
        assert_eq!(b.len(), DEPOSIT_LEN);
        assert_eq!(b[0], 1, "TAG_DEPOSIT");
        assert_eq!(decode(&b), Some(Deposit { account: 7, asset: 1, amount: 50_000, nonce: 0x0102030405060708 }));
    }

    #[test]
    fn only_the_exact_length_decodes() {
        let b = unhex(FIXTURE_HEX);
        assert_eq!(decode(&b[..17]), None, "the old nonce-less deposit");
        assert_eq!(decode(&b[..DEPOSIT_LEN - 1]), None);
        assert_eq!(decode(&[b.clone(), alloc::vec![0]].concat()), None);
    }

    #[test]
    fn key_is_prefixed_handle() {
        assert_eq!(key(7), *b"dn\x07\x00\x00\x00");
    }

    #[test]
    fn a_duplicated_deposit_is_credited_once() {
        // jamswap#7: one deposit accumulated three times (builder fan-out) credited 3×
        let mut l = Ledger::default();
        assert!(l.deposit(7, 5, 100));
        let after_first = l.kv.clone();
        assert!(!l.deposit(7, 5, 100));
        assert!(!l.deposit(7, 5, 100));
        assert_eq!(l.bal(7), 5);
        assert_eq!(l.kv, after_first, "a refused copy writes nothing");
    }

    #[test]
    fn a_replayed_older_nonce_is_refused() {
        let mut l = Ledger::default();
        for n in [10, 11, 12] {
            assert!(l.deposit(7, 1, n));
        }
        assert!(!l.deposit(7, 1, 11), "a credited nonce, replayed after newer ones");
        assert!(!l.deposit(7, 1, 10));
        assert!(!l.deposit(7, 1, 0), "0 is never a nonce (the floor starts at 0)");
        assert_eq!(l.bal(7), 3);
    }

    #[test]
    fn strictly_increasing_nonces_are_each_credited() {
        let mut l = Ledger::default();
        for n in 1..=100u64 {
            assert!(l.deposit(7, 2, n), "nonce {n}");
        }
        assert_eq!(l.bal(7), 200);
        // and the state stays bounded: floor + a full window
        assert_eq!(l.kv.get(&key(7)).unwrap().len(), 8 + 8 * WINDOW);
        assert_eq!(u64_at(&l.kv.get(&key(7)).unwrap(), 0), 100 - WINDOW as u64, "the floor");
    }

    #[test]
    fn deposits_that_land_out_of_order_are_each_credited() {
        // two deposits submitted back to back land in the opposite order (different cores):
        // a bare floor would refuse the older one
        let mut l = Ledger::default();
        assert!(l.deposit(7, 5, 2_000));
        assert!(l.deposit(7, 5, 1_000), "the older one still lands");
        assert!(!l.deposit(7, 5, 1_000) && !l.deposit(7, 5, 2_000), "and neither twice");
        assert_eq!(l.bal(7), 10);
        // any reordering shallower than the window is tolerated
        let mut l = Ledger::default();
        let late = 1u64;
        for n in 2..=(WINDOW as u64) {
            assert!(l.deposit(7, 1, n));
        }
        assert!(l.deposit(7, 1, late), "{} newer deposits landed first", WINDOW - 1);
        assert_eq!(l.bal(7), WINDOW as u64);
    }

    #[test]
    fn a_deposit_reordered_deeper_than_the_window_is_refused_not_double_credited() {
        let mut l = Ledger::default();
        for n in 2..=(WINDOW as u64 + 2) {
            assert!(l.deposit(7, 1, n)); // WINDOW + 1 newer deposits: the floor rises to 2
        }
        assert!(!l.deposit(7, 1, 1), "older than the floor: refused (lost, never doubled)");
        assert!(!l.deposit(7, 1, 2), "the evicted nonce is at the floor: still refused");
        assert!(l.deposit(7, 1, 3 + WINDOW as u64), "newer ones keep landing");
    }

    #[test]
    fn nonces_are_per_account() {
        let mut l = Ledger::default();
        assert!(l.deposit(7, 1, 5));
        assert!(l.deposit(8, 1, 5), "the same nonce on another account is another deposit");
        assert!(l.deposit(u32::MAX, 1, 5), "the treasury account too (reserve top-ups)");
        assert_eq!((l.bal(7), l.bal(8), l.bal(u32::MAX)), (1, 1, 1));
    }
}
