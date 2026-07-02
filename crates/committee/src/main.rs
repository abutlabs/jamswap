//! Off-protocol committee sidecar for jamswap encrypt-until-batch (option 2).
//!
//! Simulates a k-member committee that holds fresh keys (NOT validator consensus keys) and
//! decrypts sealed orders at batch close. Emits the exact service payloads:
//!   ENC_SETUP  — gov-signed commitment of the committee keys on-chain
//!   ENC_COMMIT — an encrypted order (ciphertext) posted by a trader
//!   ENC_ROUND  — the batch: ciphertexts + proven partial decryptions for refine
//!
//! `scenario <gov_seed_byte>` prints every payload the e2e test drives (honest + three
//! adversarial rounds). `govfind` recovers the demo gov seed that matches the baked
//! GOV_PUBKEY (the service gates ENC_SETUP behind it, like the treasury).

use ed25519_compact::{KeyPair, Seed};
use match_engine::{wire, Order, Side};
use vdec::{encrypt, joint_pk, keygen, pack_committee, partial_decrypt, Member, PARTIAL_LEN, POINT_LEN};

const SCALE: u32 = 10_000;
const MARKET: u32 = 1;
const BASE: u32 = 10;
const QUOTE: u32 = 20;

// Must match GOV_PUBKEY in service/src/lib.rs (governance authority for committee setup).
const GOV_PUBKEY: [u8; 32] = [
    0x90, 0x37, 0x37, 0x55, 0x60, 0x00, 0xf3, 0xf2, 0x64, 0x66, 0xd6, 0x30, 0x43, 0x64, 0xf1, 0xd2,
    0x22, 0x6e, 0xe8, 0x34, 0x0f, 0xfe, 0xe3, 0x66, 0x26, 0xc3, 0x15, 0xd0, 0x4b, 0xcf, 0xd5, 0x68,
];

fn hex(b: &[u8]) -> String {
    let mut s = String::with_capacity(b.len() * 2);
    for x in b {
        s.push_str(&format!("{:02x}", x));
    }
    s
}

fn canon_committee(n: u8, pks: &[u8], nonce: u64) -> Vec<u8> {
    // must byte-match the service: canon(b"committee", &[&[n], pks, &nonce_le])
    let mut m = Vec::new();
    m.extend_from_slice(b"jamswap:v1:committee");
    m.push(n);
    m.extend_from_slice(pks);
    m.extend_from_slice(&nonce.to_le_bytes());
    m
}

/// Build a committee of `k` members from a domain seed. Returns (members, joint key, blob=[k][pks]).
fn committee(k: usize, tag: u8) -> (Vec<Member>, ark_committee_key, Vec<u8>) {
    let members: Vec<Member> = (0..k).map(|i| keygen(&[i as u8, tag, 7])).collect();
    let pk_affines: Vec<_> = members.iter().map(|m| m.pk).collect();
    let joint = joint_pk(&pk_affines);
    let pks = pack_committee(&pk_affines);
    let mut blob = Vec::with_capacity(1 + pks.len());
    blob.push(k as u8);
    blob.extend_from_slice(&pks);
    (members, joint, blob)
}
type ark_committee_key = ark_bn254::G1Affine;

fn order_bytes(o: &Order) -> Vec<u8> {
    wire::encode_orders(core::slice::from_ref(o))
}

/// ENC_COMMIT payload: [10][market:4][C1:32][body]. Returns (payload, ciphertext = C1‖body).
fn enc_commit(order: &Order, joint: &ark_committee_key, seed: &[u8]) -> (Vec<u8>, Vec<u8>) {
    let ob = order_bytes(order);
    let (c1, body) = encrypt(&ob, joint, seed);
    let mut ct = Vec::with_capacity(POINT_LEN + body.len());
    ct.extend_from_slice(&c1);
    ct.extend_from_slice(&body);
    let mut payload = Vec::new();
    payload.push(10u8); // TAG_ENC_COMMIT
    payload.extend_from_slice(&MARKET.to_le_bytes());
    payload.extend_from_slice(&ct);
    (payload, ct)
}

/// ENC_ROUND payload for refine. `sealed` = (order, seed) pairs decrypted by `members`;
/// `plaintext` = public/resting orders. `tamper` flips a byte in the first partial.
fn enc_round(
    members: &[Member],
    blob: &[u8],
    sealed: &[(Order, &[u8])],
    plaintext: &[Order],
    tamper: bool,
) -> Vec<u8> {
    let k = members.len();
    let pks = &blob[1..]; // strip the leading n byte
    let joint = {
        let affs: Vec<_> = members.iter().map(|m| m.pk).collect();
        joint_pk(&affs)
    };
    let mut out = Vec::new();
    out.push(11u8); // TAG_ENC_ROUND
    out.extend_from_slice(&MARKET.to_le_bytes());
    out.extend_from_slice(&BASE.to_le_bytes());
    out.extend_from_slice(&QUOTE.to_le_bytes());
    out.push(k as u8);
    out.extend_from_slice(pks);
    out.extend_from_slice(&(sealed.len() as u16).to_le_bytes());
    // ciphertexts
    let mut cts: Vec<Vec<u8>> = Vec::new();
    for (o, seed) in sealed {
        let ob = order_bytes(o);
        let (c1, body) = encrypt(&ob, &joint, seed);
        out.extend_from_slice(&c1);
        out.push(body.len() as u8);
        out.extend_from_slice(&body);
        let mut ct = Vec::new();
        ct.extend_from_slice(&c1);
        ct.extend_from_slice(&body);
        cts.push(ct);
    }
    // partials: per ciphertext, one proven partial per member
    let mut first = true;
    for ct in &cts {
        let c1 = &ct[..POINT_LEN];
        for (i, m) in members.iter().enumerate() {
            let mut p = partial_decrypt(c1, m, &[i as u8, 99]).expect("partial");
            if tamper && first {
                p[POINT_LEN + POINT_LEN + 1] ^= 0x01; // corrupt z of the first partial
                first = false;
            }
            out.extend_from_slice(&p);
        }
    }
    let _ = PARTIAL_LEN;
    // plaintext (resting book + public orders)
    out.extend_from_slice(&wire::encode_orders(plaintext));
    out
}

fn ord(account: u32, id: u32, side: Side, price_disp: u32, qty_disp: u32) -> Order {
    Order { account, id, side, price: price_disp * SCALE, qty: qty_disp * SCALE }
}

fn scenario(gov_byte: u8) {
    // the real committee (2 members) that the chain commits to
    let (members, joint, blob) = committee(2, 0x11);
    let n = blob[0];
    let pks = &blob[1..];

    // gov-signed ENC_SETUP (nonce 0), signed by the documented demo governance key
    let _ = gov_byte; // (legacy arg; the demo gov key is the documented GOV_SEED)
    let msg = canon_committee(n, pks, 0);
    let gov = govkey();
    let sig = gov.sk.sign(&msg, None);
    let mut setup = Vec::new();
    setup.push(9u8); // TAG_ENC_SETUP
    setup.push(n);
    setup.extend_from_slice(pks);
    setup.extend_from_slice(&0u64.to_le_bytes());
    setup.extend_from_slice(&*sig);
    println!("setup {}", hex(&setup));

    // two crossing sealed orders: buy 5 @100 (acct 8) and sell 5 @100 (acct 7)
    let buy = ord(8, 1, Side::Buy, 100, 5);
    let sell = ord(7, 2, Side::Sell, 100, 5);
    let (c_buy, _) = enc_commit(&buy, &joint, b"seed-buy");
    let (c_sell, _) = enc_commit(&sell, &joint, b"seed-sell");
    println!("commit_buy {}", hex(&c_buy));
    println!("commit_sell {}", hex(&c_sell));

    let sealed: [(Order, &[u8]); 2] = [(buy, b"seed-buy"), (sell, b"seed-sell")];

    // honest round
    println!("round {}", hex(&enc_round(&members, &blob, &sealed, &[], false)));
    // tampered proof -> refine rejects (empty output)
    println!("round_tampered {}", hex(&enc_round(&members, &blob, &sealed, &[], true)));
    // wrong committee -> refine decrypts self-consistently but committee_hash mismatches
    // the on-chain committee -> accumulate rejects
    let (evil_members, _evil_joint, evil_blob) = committee(2, 0x99);
    // re-encrypt to the evil joint so the evil committee can actually decrypt
    let evil_joint = {
        let affs: Vec<_> = evil_members.iter().map(|m| m.pk).collect();
        joint_pk(&affs)
    };
    let _ = evil_joint;
    println!(
        "round_wrongcommittee {}",
        hex(&enc_round(&evil_members, &evil_blob, &sealed, &[], false))
    );
    // injected ciphertext never committed on-chain -> accumulate consume-or-reject fails
    let inject = ord(9, 3, Side::Buy, 105, 5);
    let sealed3: [(Order, &[u8]); 3] =
        [(buy, b"seed-buy"), (sell, b"seed-sell"), (inject, b"seed-inject-uncommitted")];
    println!("round_injected {}", hex(&enc_round(&members, &blob, &sealed3, &[], false)));
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

/// Documented demo governance seed (exactly 32 bytes). The service bakes the matching
/// GOV_PUBKEY; production replaces both with a DAO/multisig key. Printing the pubkey lets us
/// bake it and lets the demo/tests actually sign governance ops (treasury + committee setup).
const GOV_SEED: [u8; 32] = *b"jamswap:demo:governance:key:v1!!";

fn govkey() -> KeyPair {
    KeyPair::from_seed(Seed::new(GOV_SEED))
}

fn govpub() {
    let kp = govkey();
    // print as a Rust byte-array literal ready to paste into GOV_PUBKEY
    let pk = kp.pk;
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

fn main() {
    let cmd = std::env::args().nth(1).unwrap_or_default();
    match cmd.as_str() {
        "govfind" => govfind(),
        "govpub" => govpub(),
        "scenario" => {
            let g: u8 = std::env::args().nth(2).and_then(|s| s.parse().ok()).unwrap_or(0);
            scenario(g);
        }
        _ => {
            eprintln!("usage: committee <govfind|scenario <gov_seed_byte>>");
            std::process::exit(2);
        }
    }
}
