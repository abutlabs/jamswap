//! Operation authentication for the Jamswap service: canonical signed-message construction
//! and ed25519 signature verification. Kept here (a pure, host-testable crate) so the crypto
//! path has unit-test coverage in CI, separate from the service's PVM host-call code.
//!
//! An account is an ed25519 key; every mutating op (withdraw, cancel, register, treasury) is
//! authorised by a signature over `canon(action, fields…)`. The verifier accepts both a raw
//! signature and one over the `<Bytes>…</Bytes>` framing that injected wallets' `signRaw` adds.

use alloc::vec::Vec;
use ed25519_compact::{PublicKey, Signature};

/// Domain-separated canonical message a client signs. Versioned action tag (`jamswap:v1:<action>`)
/// followed by the raw little-endian fields, so one action/version's signature can't be replayed
/// for another. Must stay byte-identical to the constructions in server.py / the UI.
pub fn canon(action: &[u8], parts: &[&[u8]]) -> Vec<u8> {
    let mut m = Vec::new();
    m.extend_from_slice(b"jamswap:v1:");
    m.extend_from_slice(action);
    for p in parts {
        m.extend_from_slice(p);
    }
    m
}

/// Canonical message a trader signs to place an order. Covers everything the trader chose —
/// account, market, side, quantity, order type, sealing, limit price — plus a per-account
/// monotonic `seq` so a captured signature can't be replayed into a later batch. The order id
/// is NOT signed (it's builder-assigned after signing); replay protection comes from `seq`.
/// Market orders sign price 0 (the executed price is builder-derived within the band, which
/// accumulate re-checks against the on-chain last price).
/// Must stay byte-identical to the constructions in server.py / the UI.
#[allow(clippy::too_many_arguments)]
pub fn order_msg(
    account: u32,
    market: u32,
    side: u8,
    qty: u32,
    is_market: bool,
    sealed: bool,
    signed_price: u32,
    seq: u64,
) -> Vec<u8> {
    canon(
        b"order",
        &[
            &account.to_le_bytes(),
            &market.to_le_bytes(),
            &[side],
            &qty.to_le_bytes(),
            &[is_market as u8],
            &[sealed as u8],
            &signed_price.to_le_bytes(),
            &seq.to_le_bytes(),
        ],
    )
}

/// Verify an ed25519 signature over `msg` by `pubkey`. Rejects malformed keys/signatures, and
/// (like any JAM guarantor or auditor re-running this) is deterministic. Accepts the `<Bytes>`
/// framing so a message signed via `signRaw` verifies against the same canonical `msg`.
pub fn verify_signed(pubkey: &[u8; 32], msg: &[u8], sig: &[u8; 64]) -> bool {
    let Ok(pk) = PublicKey::from_slice(pubkey) else { return false };
    let Ok(s) = Signature::from_slice(sig) else { return false };
    if pk.verify(msg, &s).is_ok() {
        return true;
    }
    let mut wrapped = Vec::with_capacity(msg.len() + 15);
    wrapped.extend_from_slice(b"<Bytes>");
    wrapped.extend_from_slice(msg);
    wrapped.extend_from_slice(b"</Bytes>");
    pk.verify(&wrapped, &s).is_ok()
}

#[cfg(test)]
mod tests {
    use super::*;
    use ed25519_compact::{KeyPair, Seed};

    fn kp(seed: u8) -> KeyPair {
        KeyPair::from_seed(Seed::new([seed; 32]))
    }

    #[test]
    fn canon_is_domain_separated_and_deterministic() {
        let a = canon(b"withdraw", &[&1u32.to_le_bytes(), &500u64.to_le_bytes()]);
        let b = canon(b"withdraw", &[&1u32.to_le_bytes(), &500u64.to_le_bytes()]);
        assert_eq!(a, b);
        assert!(a.starts_with(b"jamswap:v1:withdraw"));
        // a different action yields a different message (no cross-action replay)
        assert_ne!(a, canon(b"cancel", &[&1u32.to_le_bytes(), &500u64.to_le_bytes()]));
    }

    #[test]
    fn accepts_valid_and_rejects_tampered() {
        let k = kp(1);
        let pk: [u8; 32] = (*k.pk).into();
        let msg = canon(b"withdraw", &[&7u32.to_le_bytes(), &100u64.to_le_bytes()]);
        let sig: [u8; 64] = (*k.sk.sign(&msg, None)).into();

        assert!(verify_signed(&pk, &msg, &sig), "valid signature must verify");

        // tampered message (different amount) must fail
        let msg2 = canon(b"withdraw", &[&7u32.to_le_bytes(), &101u64.to_le_bytes()]);
        assert!(!verify_signed(&pk, &msg2, &sig), "tampered message must not verify");

        // wrong key must fail
        let other: [u8; 32] = (*kp(2).pk).into();
        assert!(!verify_signed(&other, &msg, &sig), "wrong key must not verify");

        // mangled signature must fail (not panic)
        let mut bad = sig;
        bad[0] ^= 0xff;
        assert!(!verify_signed(&pk, &msg, &bad), "corrupt signature must not verify");
    }

    #[test]
    fn order_msg_binds_every_field() {
        let base = order_msg(7, 1, 0, 100, false, false, 250, 5);
        assert!(base.starts_with(b"jamswap:v1:order"));
        // flipping any field must change the message (no cross-field/replay collisions)
        assert_ne!(base, order_msg(8, 1, 0, 100, false, false, 250, 5)); // account
        assert_ne!(base, order_msg(7, 2, 0, 100, false, false, 250, 5)); // market
        assert_ne!(base, order_msg(7, 1, 1, 100, false, false, 250, 5)); // side
        assert_ne!(base, order_msg(7, 1, 0, 101, false, false, 250, 5)); // qty
        assert_ne!(base, order_msg(7, 1, 0, 100, true, false, 250, 5)); // market-order flag
        assert_ne!(base, order_msg(7, 1, 0, 100, false, true, 250, 5)); // sealed flag
        assert_ne!(base, order_msg(7, 1, 0, 100, false, false, 251, 5)); // price
        assert_ne!(base, order_msg(7, 1, 0, 100, false, false, 250, 6)); // seq
    }

    #[test]
    fn accepts_bytes_wrapped_signature() {
        // a wallet's signRaw signs "<Bytes>msg</Bytes>"; verify must accept it against `msg`
        let k = kp(3);
        let pk: [u8; 32] = (*k.pk).into();
        let msg = canon(b"register", &[&pk]);
        let mut wrapped = Vec::new();
        wrapped.extend_from_slice(b"<Bytes>");
        wrapped.extend_from_slice(&msg);
        wrapped.extend_from_slice(b"</Bytes>");
        let sig: [u8; 64] = (*k.sk.sign(&wrapped, None)).into();
        assert!(verify_signed(&pk, &msg, &sig), "<Bytes>-wrapped signature must verify against msg");
    }
}
