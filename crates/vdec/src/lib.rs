#![cfg_attr(not(test), no_std)]

//! Verifiable threshold decryption for JAM services.
//!
//! A JAM service can never hold a secret (refine is a pure, publicly re-executed
//! function — see jamswap/docs/LASAIR_INTERNALS.md Q5). So confidential orders are
//! encrypted to an OFF-protocol committee key, and at batch close the committee
//! decrypts and hands refine a *proof* that it decrypted correctly. refine verifies
//! the proof and derives the plaintext ITSELF — it never sees a key, and a malicious
//! committee cannot lie about the plaintext.
//!
//! Scheme (additive n-of-n committee over BN254 G1):
//!   setup:   member i holds sk_i, publishes PK_i = sk_i·G; joint PK = Σ PK_i.
//!   encrypt: C1 = r·G, S = r·PK, k = KDF(S), body = order ⊕ k. Ciphertext = (C1, body).
//!   decrypt: member i returns partial S_i = sk_i·C1 with a Chaum-Pedersen proof that
//!            (G, PK_i, C1, S_i) is a DDH tuple (dlog_G PK_i = dlog_C1 S_i). Then
//!            S = Σ S_i = sk·C1, order = body ⊕ KDF(S).
//!   verify:  refine checks every partial's proof against the COMMITTED PK_i, sums the
//!            proven S_i, derives k, and recovers the order. No secret enters refine.
//!
//! Liveness needs all n members (n-of-n); the t-of-n upgrade replaces the additive sum
//! with Lagrange-in-the-exponent over Shamir shares — the per-partial proof is identical,
//! so refine-side cost and this verify path are unchanged. See README.

extern crate alloc;
use alloc::vec::Vec;

use ark_bn254::{Fr, G1Affine, G1Projective};
use ark_ec::{AffineRepr, CurveGroup, PrimeGroup};
use ark_ff::PrimeField;
use ark_serialize::{CanonicalDeserialize, CanonicalSerialize};
use blake2::{Blake2s256, Digest};

/// Compressed BN254 G1 point / Fr scalar are both 32 bytes.
pub const POINT_LEN: usize = 32;
pub const SCALAR_LEN: usize = 32;
/// A partial decryption on the wire: S_i ‖ e ‖ z.
pub const PARTIAL_LEN: usize = POINT_LEN + SCALAR_LEN + SCALAR_LEN;

fn g() -> G1Projective {
    G1Projective::generator()
}

fn ser_point(p: &G1Affine) -> [u8; POINT_LEN] {
    let mut buf = [0u8; POINT_LEN];
    p.serialize_compressed(&mut buf[..]).expect("G1 compresses to 32 bytes");
    buf
}

fn de_point(b: &[u8]) -> Option<G1Affine> {
    if b.len() < POINT_LEN {
        return None;
    }
    G1Affine::deserialize_compressed(&b[..POINT_LEN]).ok()
}

fn ser_scalar(s: &Fr) -> [u8; SCALAR_LEN] {
    let mut buf = [0u8; SCALAR_LEN];
    s.serialize_compressed(&mut buf[..]).expect("Fr compresses to 32 bytes");
    buf
}

fn de_scalar(b: &[u8]) -> Option<Fr> {
    if b.len() < SCALAR_LEN {
        return None;
    }
    Fr::deserialize_compressed(&b[..SCALAR_LEN]).ok()
}

/// Derive a scalar deterministically from a domain tag + seed bytes (no rand dep, so
/// prove is reproducible and testable). Rejection-free: reduce 64 hash bytes mod order.
fn scalar_from(tag: &[u8], seed: &[u8]) -> Fr {
    let mut h1 = Blake2s256::new();
    h1.update(tag);
    h1.update(b"|0|");
    h1.update(seed);
    let a = h1.finalize();
    let mut h2 = Blake2s256::new();
    h2.update(tag);
    h2.update(b"|1|");
    h2.update(seed);
    let b = h2.finalize();
    let mut wide = [0u8; 64];
    wide[..32].copy_from_slice(&a);
    wide[32..].copy_from_slice(&b);
    Fr::from_le_bytes_mod_order(&wide)
}

/// KDF keystream from a shared-secret point: Blake2s(S) expanded by a counter to `len`.
fn kdf(shared: &G1Affine, len: usize) -> Vec<u8> {
    let sp = ser_point(shared);
    let mut out = Vec::with_capacity(len);
    let mut ctr: u32 = 0;
    while out.len() < len {
        let mut h = Blake2s256::new();
        h.update(b"vdec-kdf");
        h.update(sp);
        h.update(ctr.to_le_bytes());
        let block = h.finalize();
        let take = core::cmp::min(32, len - out.len());
        out.extend_from_slice(&block[..take]);
        ctr += 1;
    }
    out
}

/// Fiat-Shamir challenge for the Chaum-Pedersen proof, bound to all four points plus
/// the prover's commitments T1, T2.
fn challenge(pk_i: &G1Affine, c1: &G1Affine, s_i: &G1Affine, t1: &G1Affine, t2: &G1Affine) -> Fr {
    let mut h = Blake2s256::new();
    h.update(b"vdec-cp");
    h.update(ser_point(&g().into_affine()));
    h.update(ser_point(pk_i));
    h.update(ser_point(c1));
    h.update(ser_point(s_i));
    h.update(ser_point(t1));
    h.update(ser_point(t2));
    let d = h.finalize();
    // fold to a scalar (mod order); a 32-byte challenge is ample soundness for a pilot.
    let mut wide = [0u8; 64];
    wide[..32].copy_from_slice(&d);
    Fr::from_le_bytes_mod_order(&wide)
}

// ---------------------------------------------------------------------------
// Committee / prover side (also compiles no_std; used by the offchain sidecar).
// ---------------------------------------------------------------------------

/// A committee member's keypair.
pub struct Member {
    pub sk: Fr,
    pub pk: G1Affine,
}

/// Deterministically derive a committee member keypair from a seed.
pub fn keygen(seed: &[u8]) -> Member {
    let sk = scalar_from(b"vdec-sk", seed);
    let pk = (g() * sk).into_affine();
    Member { sk, pk }
}

/// Joint committee public key PK = Σ PK_i.
pub fn joint_pk(members: &[G1Affine]) -> G1Affine {
    let mut acc = G1Projective::default(); // identity
    for m in members {
        acc += m.into_group();
    }
    acc.into_affine()
}

/// Encrypt `order` to the joint committee key. `seed` supplies the ephemeral randomness r.
/// Returns (C1 compressed, body = order ⊕ KDF).
pub fn encrypt(order: &[u8], joint: &G1Affine, seed: &[u8]) -> ([u8; POINT_LEN], Vec<u8>) {
    let r = scalar_from(b"vdec-r", seed);
    let c1 = (g() * r).into_affine();
    let shared = (joint.into_group() * r).into_affine();
    let ks = kdf(&shared, order.len());
    let mut body = Vec::with_capacity(order.len());
    for (i, b) in order.iter().enumerate() {
        body.push(b ^ ks[i]);
    }
    (ser_point(&c1), body)
}

/// Produce a member's partial decryption + Chaum-Pedersen proof for ciphertext point C1.
/// Wire layout of the returned bytes: S_i(32) ‖ e(32) ‖ z(32) = PARTIAL_LEN.
pub fn partial_decrypt(c1_bytes: &[u8], member: &Member, seed: &[u8]) -> Option<[u8; PARTIAL_LEN]> {
    let c1 = de_point(c1_bytes)?;
    let s_i = (c1.into_group() * member.sk).into_affine();
    let w = scalar_from(b"vdec-w", seed);
    let t1 = (g() * w).into_affine();
    let t2 = (c1.into_group() * w).into_affine();
    let e = challenge(&member.pk, &c1, &s_i, &t1, &t2);
    let z = w + e * member.sk;
    let mut out = [0u8; PARTIAL_LEN];
    out[..POINT_LEN].copy_from_slice(&ser_point(&s_i));
    out[POINT_LEN..POINT_LEN + SCALAR_LEN].copy_from_slice(&ser_scalar(&e));
    out[POINT_LEN + SCALAR_LEN..].copy_from_slice(&ser_scalar(&z));
    Some(out)
}

// ---------------------------------------------------------------------------
// Verifier / refine side (the part that runs inside the JAM PVM).
// ---------------------------------------------------------------------------

/// Verify one partial decryption against the committed member key PK_i, returning the
/// proven S_i as a point on success. Fails closed on any malformed input or bad proof.
pub fn verify_partial(c1_bytes: &[u8], pk_i_bytes: &[u8], partial: &[u8]) -> Option<G1Affine> {
    if partial.len() < PARTIAL_LEN {
        return None;
    }
    let c1 = de_point(c1_bytes)?;
    let pk_i = de_point(pk_i_bytes)?;
    let s_i = de_point(&partial[..POINT_LEN])?;
    let e = de_scalar(&partial[POINT_LEN..POINT_LEN + SCALAR_LEN])?;
    let z = de_scalar(&partial[POINT_LEN + SCALAR_LEN..])?;
    // Recompute the prover's commitments from the response: T1 = z·G - e·PK_i, T2 = z·C1 - e·S_i.
    let t1 = (g() * z - pk_i.into_group() * e).into_affine();
    let t2 = (c1.into_group() * z - s_i.into_group() * e).into_affine();
    let e_expected = challenge(&pk_i, &c1, &s_i, &t1, &t2);
    if e_expected == e {
        Some(s_i)
    } else {
        None
    }
}

/// Full refine-side verifiable decryption of one ciphertext against the committed
/// committee keys. `partials` must be one PARTIAL_LEN blob per committee member, in the
/// SAME ORDER as `committee_pks`. Returns the recovered plaintext order, or None if any
/// proof fails (⇒ the round must be rejected).
pub fn verify_and_decrypt(
    c1_bytes: &[u8],
    body: &[u8],
    committee_pks: &[[u8; POINT_LEN]],
    partials: &[u8],
) -> Option<Vec<u8>> {
    let n = committee_pks.len();
    if n == 0 || partials.len() != n * PARTIAL_LEN {
        return None;
    }
    let mut s_sum = G1Projective::default(); // identity
    for i in 0..n {
        let p = &partials[i * PARTIAL_LEN..(i + 1) * PARTIAL_LEN];
        let s_i = verify_partial(c1_bytes, &committee_pks[i], p)?;
        s_sum += s_i.into_group();
    }
    let shared = s_sum.into_affine();
    let ks = kdf(&shared, body.len());
    let mut order = Vec::with_capacity(body.len());
    for (i, b) in body.iter().enumerate() {
        order.push(b ^ ks[i]);
    }
    Some(order)
}

/// Serialize a committee's public keys to a flat blob (n × 32 bytes) for embedding/commit.
pub fn pack_committee(members: &[G1Affine]) -> Vec<u8> {
    let mut out = Vec::with_capacity(members.len() * POINT_LEN);
    for m in members {
        out.extend_from_slice(&ser_point(m));
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn committee(n: usize) -> (Vec<Member>, Vec<[u8; POINT_LEN]>, G1Affine) {
        let mut members = Vec::new();
        let mut pks = Vec::new();
        let mut pk_affines = Vec::new();
        for i in 0..n {
            let m = keygen(&[i as u8, 42, 7]);
            pks.push(ser_point(&m.pk));
            pk_affines.push(m.pk);
            members.push(m);
        }
        let joint = joint_pk(&pk_affines);
        (members, pks, joint)
    }

    fn round(order: &[u8], members: &[Member], joint: &G1Affine) -> ([u8; POINT_LEN], Vec<u8>, Vec<u8>) {
        let (c1, body) = encrypt(order, joint, b"ephemeral-seed");
        let mut partials = Vec::new();
        for (i, m) in members.iter().enumerate() {
            let p = partial_decrypt(&c1, m, &[i as u8, 99]).unwrap();
            partials.extend_from_slice(&p);
        }
        (c1, body, partials)
    }

    #[test]
    fn honest_roundtrip_recovers_order() {
        for n in 1..=3 {
            let (members, pks, joint) = committee(n);
            let order = b"account17bytes:!!"; // 17 bytes, like a jamswap order
            let (c1, body, partials) = round(order, &members, &joint);
            let got = verify_and_decrypt(&c1, &body, &pks, &partials).expect("honest decrypt");
            assert_eq!(&got, order, "n={n} must recover the plaintext");
        }
    }

    #[test]
    fn tampered_response_is_rejected() {
        let (members, pks, joint) = committee(2);
        let order = b"account17bytes:!!";
        let (c1, body, mut partials) = round(order, &members, &joint);
        // flip a byte in the first partial's z scalar
        let z_off = POINT_LEN + SCALAR_LEN + 1;
        partials[z_off] ^= 0x01;
        assert!(verify_and_decrypt(&c1, &body, &pks, &partials).is_none(),
            "a bad Chaum-Pedersen response must fail the round");
    }

    #[test]
    fn wrong_share_is_rejected() {
        // A malicious member submits S_i for a DIFFERENT secret (with a self-consistent
        // proof for THAT secret) — but it won't match the committed PK_i, so verify fails.
        let (members, pks, joint) = committee(2);
        let order = b"account17bytes:!!";
        let (c1, body, _good) = round(order, &members, &joint);
        let evil = keygen(&[255, 1]); // not the committed member 0
        let mut partials = Vec::new();
        partials.extend_from_slice(&partial_decrypt(&c1, &evil, &[0, 99]).unwrap());
        partials.extend_from_slice(&partial_decrypt(&c1, &members[1], &[1, 99]).unwrap());
        let _ = &pks; // committed keys unchanged
        assert!(verify_and_decrypt(&c1, &body, &pks, &partials).is_none(),
            "a share not matching the committed PK_i must be rejected");
    }

    #[test]
    fn missing_member_cannot_decrypt() {
        // n-of-n liveness: drop a member's partial -> length mismatch -> reject.
        let (members, pks, joint) = committee(3);
        let order = b"account17bytes:!!";
        let (c1, body, partials) = round(order, &members, &joint);
        let short = &partials[..2 * PARTIAL_LEN];
        assert!(verify_and_decrypt(&c1, &body, &pks, short).is_none(),
            "fewer than n partials must not decrypt (n-of-n)");
    }
}
