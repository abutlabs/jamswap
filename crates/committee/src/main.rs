//! Off-protocol committee sidecar for jamswap encrypt-until-batch (option 2).
//!
//! Simulates a k-member committee holding FRESH keys (never validator consensus keys) that
//! decrypts sealed orders at batch close. The off-chain server shells out to these commands:
//!
//!   setup                                   -> `setup <hex>` (gov-signed ENC_SETUP) + `committee <blob>`
//!   encrypt <market> <order_hex> <seed_hex> -> `ciphertext <hex>` (C1‖body) + `commit <ENC_COMMIT hex>`
//!   round <m> <b> <q> <plaintext_hex> <ct_csv> -> `round <ENC_ROUND hex>` (decrypts + proves each ct)
//!
//! Plus `scenario <gov_byte>` (drives the e2e test) and `govpub`/`govfind` (dev helpers).
//! The committee is deterministic (fixed member seeds) so setup/encrypt/round agree on keys —
//! a real deployment runs an interactive DKG across independent operators instead.

use ed25519_compact::{KeyPair, Seed};
use match_engine::wire;
use vdec::{encrypt, joint_pk, keygen, pack_committee, partial_decrypt, Member, POINT_LEN};

const MARKET_UNUSED: u32 = 0; // (documentation: market is passed per-command)
const COMMITTEE_SIZE: usize = 2;
const COMMITTEE_TAG: u8 = 0x11;

// Documented demo governance seed (32 bytes). Service bakes the matching GOV_PUBKEY; production
// replaces both with a DAO/multisig key. Only this key may set the committee or sweep the treasury.
const GOV_SEED: [u8; 32] = *b"jamswap:demo:governance:key:v1!!";

// Must match GOV_PUBKEY in service/src/lib.rs (used by `govfind`, a legacy check).
const GOV_PUBKEY: [u8; 32] = [
    0x37, 0x42, 0x87, 0x63, 0x4e, 0x12, 0x9e, 0xc1, 0xf7, 0x2c, 0x75, 0x08, 0xa1, 0x30, 0xa6, 0xf4,
    0xae, 0x2c, 0x14, 0x56, 0xc9, 0x28, 0x0f, 0xe5, 0x2e, 0xaa, 0x4f, 0x22, 0x54, 0xf7, 0xe6, 0xca,
];

fn hex(b: &[u8]) -> String {
    let mut s = String::with_capacity(b.len() * 2);
    for x in b {
        s.push_str(&format!("{:02x}", x));
    }
    s
}
fn unhex(s: &str) -> Vec<u8> {
    let s = s.trim();
    (0..s.len() / 2).map(|i| u8::from_str_radix(&s[i * 2..i * 2 + 2], 16).unwrap()).collect()
}

fn govkey() -> KeyPair {
    KeyPair::from_seed(Seed::new(GOV_SEED))
}

/// The deterministic demo committee: (members, joint key, blob = [k][pks]).
fn fixed_committee() -> (Vec<Member>, ark_bn254::G1Affine, Vec<u8>) {
    let members: Vec<Member> = (0..COMMITTEE_SIZE).map(|i| keygen(&[i as u8, COMMITTEE_TAG, 7])).collect();
    let affs: Vec<_> = members.iter().map(|m| m.pk).collect();
    let joint = joint_pk(&affs);
    let pks = pack_committee(&affs);
    let mut blob = Vec::with_capacity(1 + pks.len());
    blob.push(COMMITTEE_SIZE as u8);
    blob.extend_from_slice(&pks);
    (members, joint, blob)
}

fn canon_committee(n: u8, pks: &[u8], nonce: u64) -> Vec<u8> {
    let mut m = Vec::new();
    m.extend_from_slice(b"jamswap:v1:committee");
    m.push(n);
    m.extend_from_slice(pks);
    m.extend_from_slice(&nonce.to_le_bytes());
    m
}

/// Gov-signed ENC_SETUP payload for a committee blob at the given nonce.
fn setup_payload(blob: &[u8], nonce: u64) -> Vec<u8> {
    let n = blob[0];
    let pks = &blob[1..];
    let msg = canon_committee(n, pks, nonce);
    let sig = govkey().sk.sign(&msg, None);
    let mut out = Vec::new();
    out.push(9u8); // TAG_ENC_SETUP
    out.push(n);
    out.extend_from_slice(pks);
    out.extend_from_slice(&nonce.to_le_bytes());
    out.extend_from_slice(&*sig);
    out
}

/// Assemble the ENC_ROUND payload: for each (C1, body) ciphertext, every member contributes a
/// proven partial decryption; refine will verify + decrypt + clear. `plaintext` = resting book
/// + public orders (17B each). The committee keys are embedded so accumulate can hash-check them.
fn assemble_round(
    members: &[Member],
    blob: &[u8],
    market: u32,
    base: u32,
    quote: u32,
    ciphertexts: &[(Vec<u8>, Vec<u8>)],
    plaintext: &[u8],
) -> Vec<u8> {
    let k = members.len();
    let pks = &blob[1..];
    let mut out = Vec::new();
    out.push(11u8); // TAG_ENC_ROUND
    out.extend_from_slice(&market.to_le_bytes());
    out.extend_from_slice(&base.to_le_bytes());
    out.extend_from_slice(&quote.to_le_bytes());
    out.push(k as u8);
    out.extend_from_slice(pks);
    out.extend_from_slice(&(ciphertexts.len() as u16).to_le_bytes());
    for (c1, body) in ciphertexts {
        out.extend_from_slice(c1);
        out.push(body.len() as u8);
        out.extend_from_slice(body);
    }
    // partials: per ciphertext, one proven partial per member. The proof nonce is bound to
    // (sk, C1) inside vdec, so a constant per-member seed here is safe (no nonce reuse leak).
    for (c1, _body) in ciphertexts {
        for (i, m) in members.iter().enumerate() {
            let p = partial_decrypt(c1, m, &[i as u8]).expect("partial");
            out.extend_from_slice(&p);
        }
    }
    out.extend_from_slice(plaintext);
    out
}

fn cmd_setup() {
    let (_members, _joint, blob) = fixed_committee();
    println!("setup {}", hex(&setup_payload(&blob, 0)));
    println!("committee {}", hex(&blob));
}

fn cmd_encrypt(market: u32, order_hex: &str, seed_hex: &str) {
    let (_members, joint, _blob) = fixed_committee();
    let order = unhex(order_hex);
    let seed = unhex(seed_hex);
    let (c1, body) = encrypt(&order, &joint, &seed);
    let mut ct = Vec::with_capacity(POINT_LEN + body.len());
    ct.extend_from_slice(&c1);
    ct.extend_from_slice(&body);
    let mut commit = Vec::new();
    commit.push(10u8); // TAG_ENC_COMMIT
    commit.extend_from_slice(&market.to_le_bytes());
    commit.extend_from_slice(&ct);
    println!("ciphertext {}", hex(&ct));
    println!("commit {}", hex(&commit));
}

fn cmd_round(market: u32, base: u32, quote: u32, plaintext_hex: &str, ct_csv: &str) {
    let (members, _joint, blob) = fixed_committee();
    let plaintext = if plaintext_hex.is_empty() { Vec::new() } else { unhex(plaintext_hex) };
    let mut cts: Vec<(Vec<u8>, Vec<u8>)> = Vec::new();
    for cthex in ct_csv.split(',').filter(|s| !s.trim().is_empty()) {
        let ct = unhex(cthex);
        let (c1, body) = ct.split_at(POINT_LEN);
        cts.push((c1.to_vec(), body.to_vec()));
    }
    let payload = assemble_round(&members, &blob, market, base, quote, &cts, &plaintext);
    println!("round {}", hex(&payload));
}

fn ord_bytes(account: u32, id: u32, side_buy: bool, price_disp: u32, qty_disp: u32) -> Vec<u8> {
    const SCALE: u32 = 10_000;
    let side = if side_buy { match_engine::Side::Buy } else { match_engine::Side::Sell };
    let o = match_engine::Order { account, id, side, price: price_disp * SCALE, qty: qty_disp * SCALE };
    wire::encode_orders(core::slice::from_ref(&o))
}

/// The e2e scenario driver: emits ENC_SETUP + two ENC_COMMITs + honest/tampered/wrong-committee/
/// injected ENC_ROUNDs. Kept identical in shape to the modular commands (one assembler).
fn scenario(_gov_byte: u8) {
    let market = 1u32;
    let (base, quote) = (10u32, 20u32);
    let (members, joint, blob) = fixed_committee();

    println!("setup {}", hex(&setup_payload(&blob, 0)));

    let buy = ord_bytes(8, 1, true, 100, 5);
    let sell = ord_bytes(7, 2, false, 100, 5);
    let (c1b, bb) = encrypt(&buy, &joint, b"seed-buy");
    let (c1s, bs) = encrypt(&sell, &joint, b"seed-sell");
    let ct_buy = [c1b.to_vec(), bb.clone()].concat();
    let ct_sell = [c1s.to_vec(), bs.clone()].concat();
    let mk_commit = |ct: &[u8]| {
        let mut c = Vec::new();
        c.push(10u8);
        c.extend_from_slice(&market.to_le_bytes());
        c.extend_from_slice(ct);
        c
    };
    println!("commit_buy {}", hex(&mk_commit(&ct_buy)));
    println!("commit_sell {}", hex(&mk_commit(&ct_sell)));

    let cts = vec![(c1b.to_vec(), bb.clone()), (c1s.to_vec(), bs.clone())];
    // honest
    println!("round {}", hex(&assemble_round(&members, &blob, market, base, quote, &cts, &[])));
    // tampered: flip a byte in the assembled partials region (after header+cts)
    let mut tampered = assemble_round(&members, &blob, market, base, quote, &cts, &[]);
    let tlen = tampered.len();
    tampered[tlen - 1] ^= 0x01; // corrupt the last partial's z tail
    println!("round_tampered {}", hex(&tampered));
    // wrong committee: build with a DIFFERENT committee (keys + partials) -> committee-hash mismatch
    let evil_members: Vec<Member> = (0..COMMITTEE_SIZE).map(|i| keygen(&[i as u8, 0x99, 7])).collect();
    let evil_affs: Vec<_> = evil_members.iter().map(|m| m.pk).collect();
    let evil_joint = joint_pk(&evil_affs);
    let evil_pks = pack_committee(&evil_affs);
    let mut evil_blob = vec![COMMITTEE_SIZE as u8];
    evil_blob.extend_from_slice(&evil_pks);
    // re-encrypt to the evil joint so the evil committee can actually decrypt its own ciphertexts
    let (ec1b, ebb) = encrypt(&buy, &evil_joint, b"seed-buy");
    let (ec1s, ebs) = encrypt(&sell, &evil_joint, b"seed-sell");
    let evil_cts = vec![(ec1b.to_vec(), ebb), (ec1s.to_vec(), ebs)];
    println!(
        "round_wrongcommittee {}",
        hex(&assemble_round(&evil_members, &evil_blob, market, base, quote, &evil_cts, &[]))
    );
    // injected: a third ciphertext never committed on-chain -> accumulate consume-or-reject
    let inject = ord_bytes(9, 3, true, 105, 5);
    let (c1i, bi) = encrypt(&inject, &joint, b"seed-inject-uncommitted");
    let mut cts3 = cts.clone();
    cts3.push((c1i.to_vec(), bi));
    println!("round_injected {}", hex(&assemble_round(&members, &blob, market, base, quote, &cts3, &[])));
}

fn govpub() {
    let pk = govkey().pk;
    print!("gov_pubkey ");
    for (i, b) in pk.as_ref().iter().enumerate() {
        print!("0x{:02x}, ", b);
        if i % 16 == 15 {
            print!("\n           ");
        }
    }
    println!();
    println!("gov_pubkey_hex {}", hex(pk.as_ref()));
}

fn govfind() {
    for b in 1u16..=255 {
        let kp = KeyPair::from_seed(Seed::new([b as u8; 32]));
        if kp.pk.as_ref() == &GOV_PUBKEY[..] {
            println!("gov_seed_byte {}", b);
            return;
        }
    }
    println!("gov_seed_byte NOTFOUND");
}

fn main() {
    let _ = MARKET_UNUSED;
    let a: Vec<String> = std::env::args().collect();
    let cmd = a.get(1).map(|s| s.as_str()).unwrap_or("");
    let p = |i: usize| a.get(i).cloned().unwrap_or_default();
    let pu = |i: usize| p(i).parse::<u32>().unwrap_or(0);
    match cmd {
        "setup" => cmd_setup(),
        "encrypt" => cmd_encrypt(pu(2), &p(3), &p(4)),
        "round" => cmd_round(pu(2), pu(3), pu(4), &p(5), &p(6)),
        "scenario" => scenario(pu(2) as u8),
        "govpub" => govpub(),
        "govfind" => govfind(),
        _ => {
            eprintln!("usage: committee <setup | encrypt <market> <order_hex> <seed_hex> | round <m> <b> <q> <plaintext_hex> <ct_csv> | scenario <gov> | govpub | govfind>");
            std::process::exit(2);
        }
    }
}
