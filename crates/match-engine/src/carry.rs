//! Sealed carry-forward: the state-side rules for re-sealing a partially-filled sealed
//! order's remainder. The rules live here, pure and host-testable; the service's accumulate
//! runs them against its storage through [`Kv`].
//!
//! A sealed order's unfilled remainder never rests on the public book (its terms would be
//! exposed), so the builder re-seals it into a fresh hidden commitment and posts that
//! commitment for the owner, who is offline by design (`TAG_CARRY_COMMIT` /
//! `TAG_CARRY_ENC_COMMIT`, unsigned). The post is **allowance-gated**: a settling round
//! mints one credit per partially-filled sealed order for its account ([`mint`]), and each
//! re-seal spends one ([`admit`]). Credits are counted per (market, account), not per order.
//!
//! ## A duplicate re-seal must not spend a credit (jamswap#5)
//!
//! The same re-seal can land twice: a duplicated work item from the builder's fail-over, or
//! the server re-posting a commitment whose first submit had an unknown outcome (it re-posts
//! the SAME commitment on purpose). When one account had two partial fills in a round it held
//! two credits, and remainder 1's twin spent the second one: remainder 2's carry was then
//! refused, waited as "commit-not-onchain" until it expired and never traded, and the stray
//! twin sat in the set for the commit TTL. So a re-seal whose `id ‖ account` entry is already
//! in the set is refused **before anything is written**: no credit spent, the set unchanged.
//! A legitimate re-seal never collides: a commitment is `blake2s(order ‖ fresh 32-byte
//! nonce)` and a ciphertext id covers a fresh ECIES point, so only a copy repeats an entry.
//!
//! Residual (documented in docs/SECURITY.md): a twin that lands only AFTER its original was
//! revealed and consumed (or expired) finds no entry, and spends a credit if the account
//! still holds one. The builder reveals a carried remainder only once its commit is on-chain,
//! so this needs a copy delayed past a whole later round.

use crate::Kv;

/// A commit-set / encset entry: `id(32) ‖ account(4)`. Consumption matches both halves.
pub const ENTRY_LEN: usize = 36;

/// `b"cw"‖market‖account` → the account's carry credits in `market` (u32 LE).
pub fn credit_key(market: u32, account: u32) -> [u8; 10] {
    let (m, a) = (market.to_le_bytes(), account.to_le_bytes());
    [b'c', b'w', m[0], m[1], m[2], m[3], a[0], a[1], a[2], a[3]]
}

/// The account's unspent carry credits in `market` (0 when none were ever minted).
pub fn credits(kv: &impl Kv, market: u32, account: u32) -> u32 {
    kv.get(&credit_key(market, account))
        .filter(|v| v.len() >= 4)
        .map(|v| u32::from_le_bytes([v[0], v[1], v[2], v[3]]))
        .unwrap_or(0)
}

fn set_credits(kv: &mut impl Kv, market: u32, account: u32, n: u32) {
    kv.set(&credit_key(market, account), &n.to_le_bytes());
}

/// Mint one credit per entry of a settling round's carry list (`account(4)` each, one entry
/// per genuinely partially-filled sealed order, so an account listed twice gets two).
pub fn mint(kv: &mut impl Kv, market: u32, carry: &[u8]) {
    for a in carry.chunks_exact(4) {
        let account = u32::from_le_bytes([a[0], a[1], a[2], a[3]]);
        let n = credits(kv, market, account).saturating_add(1);
        set_credits(kv, market, account, n);
    }
}

/// Admit a builder-posted re-seal `entry` (`id ‖ account`) into the set stored under
/// `set_key` (`b"commits"‖market` or `b"encset"‖market`): spend one of the account's credits
/// in `market` and append the entry. Refused, writing nothing, when the account holds no
/// credit or the entry is already in the set (a duplicate: see the module docs).
pub fn admit(kv: &mut impl Kv, set_key: &[u8], market: u32, entry: &[u8; ENTRY_LEN]) -> bool {
    let account = u32::from_le_bytes([entry[32], entry[33], entry[34], entry[35]]);
    let cw = credits(kv, market, account);
    if cw == 0 {
        return false; // no partial fill minted a credit for this account
    }
    let mut set = kv.get(set_key).unwrap_or_default();
    if set.chunks_exact(ENTRY_LEN).any(|e| e == &entry[..]) {
        return false; // a copy of a re-seal that already landed
    }
    set_credits(kv, market, account, cw - 1);
    set.extend_from_slice(entry);
    kv.set(set_key, &set);
    true
}

#[cfg(test)]
mod tests {
    use super::*;
    use alloc::collections::BTreeMap;
    use alloc::vec::Vec;

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

    const M: u32 = 1;
    const SET: &[u8] = b"commits\x01\x00\x00\x00";
    fn entry(tag: u8, account: u32) -> [u8; ENTRY_LEN] {
        let mut e = [tag; ENTRY_LEN];
        e[32..].copy_from_slice(&account.to_le_bytes());
        e
    }
    fn set_entries(kv: &Mem) -> Vec<[u8; ENTRY_LEN]> {
        kv.get(SET).unwrap_or_default().chunks_exact(ENTRY_LEN).map(|c| c.try_into().unwrap()).collect()
    }

    #[test]
    fn key_is_prefixed_market_then_account() {
        assert_eq!(credit_key(1, 7), *b"cw\x01\x00\x00\x00\x07\x00\x00\x00");
    }

    #[test]
    fn a_duplicated_carry_does_not_spend_another_orders_credit() {
        // jamswap#5: account 7's two sealed orders partially fill in one round → 2 credits.
        let mut kv = Mem::default();
        mint(&mut kv, M, &[7u32.to_le_bytes(), 7u32.to_le_bytes()].concat());
        assert_eq!(credits(&kv, M, 7), 2);
        let (r1, r2) = (entry(0xA1, 7), entry(0xA2, 7));
        assert!(admit(&mut kv, SET, M, &r1), "remainder 1's carry lands");
        assert_eq!(credits(&kv, M, 7), 1);
        // the same re-seal lands again (builder fail-over, or the server's retry)
        let before = kv.clone();
        assert!(!admit(&mut kv, SET, M, &r1), "the copy is refused");
        assert_eq!(kv, before, "refused cleanly: no credit spent, the set unchanged");
        assert_eq!(credits(&kv, M, 7), 1, "remainder 2's credit survives");
        assert!(admit(&mut kv, SET, M, &r2), "remainder 2's carry lands");
        assert_eq!(credits(&kv, M, 7), 0);
        assert_eq!(set_entries(&kv), [r1, r2], "each remainder once, no stray twin");
    }

    #[test]
    fn no_credit_no_carry() {
        let mut kv = Mem::default();
        assert!(!admit(&mut kv, SET, M, &entry(1, 7)));
        assert_eq!(kv, Mem::default(), "nothing written");
        mint(&mut kv, M, &8u32.to_le_bytes());
        assert!(!admit(&mut kv, SET, M, &entry(1, 7)), "another account's credit is not 7's");
        assert!(!admit(&mut kv, SET, 2, &entry(1, 8)), "credits are per market");
        assert!(admit(&mut kv, SET, M, &entry(1, 8)));
        assert!(!admit(&mut kv, SET, M, &entry(2, 8)), "one credit, one carry");
    }

    #[test]
    fn the_duplicate_check_covers_the_whole_entry() {
        // an owner-signed commit (or another account's carry) already in the set: the same id
        // under a different account is a different entry, so it is not mistaken for a copy
        let mut kv = Mem::default();
        kv.set(SET, &entry(5, 8));
        mint(&mut kv, M, &7u32.to_le_bytes());
        assert!(admit(&mut kv, SET, M, &entry(5, 7)));
        assert_eq!(set_entries(&kv), [entry(5, 8), entry(5, 7)]);
        // and a copy of an entry anywhere in the set is refused, not only the last one
        mint(&mut kv, M, &8u32.to_le_bytes());
        assert!(!admit(&mut kv, SET, M, &entry(5, 8)));
        assert_eq!(credits(&kv, M, 8), 1);
    }

    #[test]
    fn minting_accumulates_per_account() {
        let mut kv = Mem::default();
        mint(&mut kv, M, &[7u32.to_le_bytes(), 8u32.to_le_bytes(), 7u32.to_le_bytes()].concat());
        mint(&mut kv, M, &7u32.to_le_bytes());
        assert_eq!((credits(&kv, M, 7), credits(&kv, M, 8)), (3, 1));
        mint(&mut kv, M, &[]);
        assert_eq!(credits(&kv, M, 7), 3, "an empty carry list mints nothing");
    }
}
