#![no_std]
#![no_main]

extern crate alloc;

use alloc::vec::Vec;

use jam_pvm_common::accumulate::{accumulate_items, get_storage, set_storage};
use jam_pvm_common::jam_types::*;
use jam_pvm_common::{declare_service, Service};

use blake2::{Blake2s256, Digest};
use match_engine::auth::{canon, order_msg, verify_signed};
use match_engine::{clear, resting, wire, Order, Side};

declare_service!(Jamswap);
struct Jamswap;

// Curve field arithmetic needs more stack than polkavm's small default — without this a
// verify traps and the whole invocation rolls back. 4 MiB covers both ed25519 (account auth)
// and BN254 G1 scalar mults (the encrypt-until-batch committee decryption in refine); the
// vdec gas spike established 4 MiB is required for BN254. PVM-target only (host CI compile gate).
#[cfg(target_arch = "riscv64")]
polkavm_derive::min_stack_size!(4 * 1024 * 1024);

// payload / work-output tags
// TAG 0 (unsigned TAG_MATCH) is RETIRED: it let the builder submit orders nobody signed.
// Public rounds now go exclusively through TAG_SMATCH, whose orders are signature-verified
// in refine — leaving the unsigned path in place would be a downgrade attack.
const TAG_DEPOSIT: u8 = 1; // [tag][account][asset_id][amount] — fund a balance (Phase-2 faucet)
const TAG_COMMIT: u8 = 2; // [tag][market][account][commitment(32)] — seal a hidden order
const TAG_REVEAL: u8 = 3; // unified sealed round — see reveal_output() for the wire layout
const TAG_CANCEL: u8 = 4; // [tag][market][account][order_id] — cancel a resting order
const TAG_WITHDRAW: u8 = 5; // [tag][account][asset][amount][nonce][sig(64)] — signed debit
const TAG_LIST: u8 = 6; // [tag][market][base][quote] — list a market (canonical assets)
const TAG_REGISTER: u8 = 7; // [tag][pubkey(32)][sig(64)] — bind an ed25519 key to an account handle
const TAG_TREASURY: u8 = 8; // [tag][asset(4)][amount(8)][dest(4)][nonce(8)][sig(64)] — governance fee sweep
// --- encrypt-until-batch (option 2): sealed orders decrypted by an off-protocol committee ---
const TAG_ENC_SETUP: u8 = 9; // gov-signed: [tag][n:u8][committee_pks(n*32)][nonce(8)][sig(64)] — commit the committee keys on-chain
const TAG_ENC_COMMIT: u8 = 10; // [tag][market(4)][C1(32)][body(ORDER_LEN)] — post an encrypted order (stored by id = H(ciphertext))
const TAG_ENC_ROUND: u8 = 11; // sealed-encrypted round — see enc_round_output() for the wire layout
// --- trustless public orders: per-order ed25519 verified IN REFINE (no builder trust) ---
const TAG_SMATCH: u8 = 12; // signed public round — see parse_public_section() / smatch_output()

// Governance key authorised to sweep the fee treasury AND commit the encrypt-until-batch
// committee. Derived from the documented demo seed b"jamswap:demo:governance:key:v1!!"
// (crates/committee, `committee govpub`); in production this is a DAO/multisig key. Only
// signatures by this key move funds out of FEE_ACCOUNT or set the committee, and dedicated
// nonces (b"govnonce" / b"comnonce") stop replay.
const GOV_PUBKEY: [u8; 32] = [
    0x37, 0x42, 0x87, 0x63, 0x4e, 0x12, 0x9e, 0xc1, 0xf7, 0x2c, 0x75, 0x08, 0xa1, 0x30, 0xa6, 0xf4,
    0xae, 0x2c, 0x14, 0x56, 0xc9, 0x28, 0x0f, 0xe5, 0x2e, 0xaa, 0x4f, 0x22, 0x54, 0xf7, 0xe6, 0xca,
];

// trading fee: a flat, cost-based fee charged per filled order in the market's BASE
// asset (FBA has no maker/taker), routed to the treasury account. 0.03 base units
// (300 atomic at SCALE 10000) — approximates the per-order execution + state cost rather
// than a size-proportional trading fee. Base-asset collection means DOT/USDC pays fees in
// DOT and JAMKB/* pays directly in JAMKB, funding the service's JAMKB state-rent reserve;
// only the surplus is withdrawable profit, via a GOV_PUBKEY-signed sweep. See docs/REVENUE.md.
const FEE_FLAT: u64 = 300;
const FEE_ACCOUNT: u32 = u32::MAX;
// Owner / beneficiary of withdrawable profit (Polkadot AssetHub). Recorded for
// documentation: JAM sweeps are authorised by GOV_PUBKEY, and cross-chain payout to
// AssetHub is deferred (no JAM<->Polkadot bridge in this MVP) — see docs/REVENUE.md.
#[allow(dead_code)]
const PROFIT_BENEFICIARY: &str = "15AWQjAZ9Ev9uhcYJdfwQzXA2VRDn2oLgZTkBzRRT7sZNDgs";

// Fixed-point price scale: on-chain prices, quantities, and balances are integer
// *atomic* units = display × SCALE, so a fractional price like 1.1050 is carried as
// the integer 11050. Settlement de-scales the quote notional by one factor of SCALE
// (see wire::settle_deltas). The off-chain layer scales on ingest and de-scales on
// read; the matching engine itself is scale-agnostic (it matches raw integers).
const SCALE: u32 = 10_000; // 4 decimal places

const NONCE_LEN: usize = 32;
const REVEAL_LEN: usize = wire::ORDER_LEN + NONCE_LEN; // order(17) ‖ nonce(32)

fn ru32(b: &[u8], off: usize) -> u32 {
    u32::from_le_bytes([b[off], b[off + 1], b[off + 2], b[off + 3]])
}
fn le_u64(b: &[u8]) -> u64 {
    let mut x = [0u8; 8];
    let n = core::cmp::min(8, b.len());
    x[..n].copy_from_slice(&b[..n]);
    u64::from_le_bytes(x)
}
fn commitment(reveal_bytes: &[u8]) -> [u8; 32] {
    let mut h = Blake2s256::new();
    h.update(reveal_bytes);
    let out = h.finalize();
    let mut a = [0u8; 32];
    a.copy_from_slice(&out);
    a
}

// balance key: "b" ‖ asset_id(4) ‖ account(4) — balances are global (cross-market);
// a market just names which two assets it trades.
fn bal_key(asset: u32, account: u32) -> Vec<u8> {
    let mut k = Vec::with_capacity(9);
    k.push(b'b');
    k.extend_from_slice(&asset.to_le_bytes());
    k.extend_from_slice(&account.to_le_bytes());
    k
}
fn get_bal(asset: u32, account: u32) -> u64 {
    get_storage(&bal_key(asset, account)).map(|v| le_u64(&v)).unwrap_or(0)
}
fn set_bal(asset: u32, account: u32, v: u64) {
    set_storage(&bal_key(asset, account), &v.to_le_bytes()).ok();
}
// per-market state key: prefix ‖ market(4)
fn mkey(prefix: &[u8], market: u32) -> Vec<u8> {
    let mut k = Vec::with_capacity(prefix.len() + 4);
    k.extend_from_slice(prefix);
    k.extend_from_slice(&market.to_le_bytes());
    k
}
// custodied total per asset: deposits add, withdrawals subtract. The accounting
// invariant Σ(balances of an asset) == custody[asset] holds by construction —
// deposits/withdrawals touch a balance and custody equally, and trades conserve
// (settle_deltas Σ == 0). Key: "cust" ‖ asset(4).
fn cust_key(asset: u32) -> Vec<u8> {
    let mut k = Vec::with_capacity(8);
    k.extend_from_slice(b"cust");
    k.extend_from_slice(&asset.to_le_bytes());
    k
}
fn get_cust(asset: u32) -> u64 {
    get_storage(&cust_key(asset)).map(|v| le_u64(&v)).unwrap_or(0)
}
fn set_cust(asset: u32, v: u64) {
    set_storage(&cust_key(asset), &v.to_le_bytes()).ok();
}

// ---- account registry ----------------------------------------------------
// An account is a sequential, collision-free u32 *handle* bound to an ed25519 public key by a
// signed TAG_REGISTER. The handle is the account id the engine + ledger use (so the matching
// engine and its wire format are untouched); the pubkey authenticates withdraw/cancel and the
// per-account nonce prevents replay. `b"pk"‖handle → pubkey(32)`, `b"h"‖pubkey → handle(4)`,
// `b"nc"‖handle → nonce(8)`, `b"nexthandle" → next u32 (handles start at 1; u32::MAX is the fee treasury).
fn pubkey_of(handle: u32) -> Option<[u8; 32]> {
    let v = get_storage(&mkey(b"pk", handle))?;
    if v.len() >= 32 {
        let mut a = [0u8; 32];
        a.copy_from_slice(&v[..32]);
        Some(a)
    } else {
        None
    }
}
fn hkey(pubkey: &[u8; 32]) -> Vec<u8> {
    let mut k = Vec::with_capacity(33);
    k.push(b'h');
    k.extend_from_slice(pubkey);
    k
}
fn handle_of(pubkey: &[u8; 32]) -> Option<u32> {
    get_storage(&hkey(pubkey)).filter(|v| v.len() >= 4).map(|v| ru32(&v, 0))
}
fn get_nonce(handle: u32) -> u64 {
    get_storage(&mkey(b"nc", handle)).map(|v| le_u64(&v)).unwrap_or(0)
}
fn set_nonce(handle: u32, v: u64) {
    set_storage(&mkey(b"nc", handle), &v.to_le_bytes()).ok();
}
// Per-account monotonic order-sequence floor (b"sq"‖handle → u64). Distinct from the strict
// per-op nonce above: orders are concurrent (several may be in flight), so instead of exact
// equality each signed order carries a client-chosen seq that must be STRICTLY greater than
// the account's floor; the floor rises to the round's max. A captured order signature can
// therefore never be replayed into a later batch.
fn get_seq_floor(handle: u32) -> u64 {
    get_storage(&mkey(b"sq", handle)).map(|v| le_u64(&v)).unwrap_or(0)
}
fn set_seq_floor(handle: u32, v: u64) {
    set_storage(&mkey(b"sq", handle), &v.to_le_bytes()).ok();
}
// Assign a handle to a fresh pubkey (idempotent: an already-registered key keeps its handle).
fn register_key(pubkey: &[u8; 32]) -> u32 {
    if let Some(h) = handle_of(pubkey) {
        return h;
    }
    let next = get_storage(b"nexthandle").filter(|v| v.len() >= 4).map(|v| ru32(&v, 0)).unwrap_or(1);
    set_storage(&mkey(b"pk", next), pubkey).ok();
    set_storage(&hkey(pubkey), &next.to_le_bytes()).ok();
    set_storage(b"nexthandle", &next.saturating_add(1).to_le_bytes()).ok();
    next
}
// Read a fixed 64-byte signature at `off` (caller guarantees the slice is long enough).
fn sig64(b: &[u8], off: usize) -> [u8; 64] {
    let mut s = [0u8; 64];
    s.copy_from_slice(&b[off..off + 64]);
    s
}
// the canonical (base, quote) a market was listed with, if any.
fn market_assets(market: u32) -> Option<(u32, u32)> {
    let v = get_storage(&mkey(b"mkt", market))?;
    if v.len() >= 8 { Some((ru32(&v, 0), ru32(&v, 4))) } else { None }
}

// ---- the signed public-order section (shared by every round type) ----------
//
// Every round's public input is now:
//   [ns:u16] ns×SignedOrder(126)          — NEW orders, each carrying pubkey + ed25519 sig
//   [np:u16] np×(account:4 ‖ oid:4)       — resting entries the builder prunes (expired)
//   [resting book bytes, 17 each]         — MUST be the on-chain book, byte-exact
//
// Refine verifies each new order's signature statelessly against the CARRIED pubkey (and
// that a limit order executes at exactly the signed price); accumulate — which can read
// state — finishes the job: pubkey must equal the account's registered key, seq must beat
// the account's floor (replay), market orders must price within the band of the on-chain
// last price, and the book refine matched against must hash to the on-chain book (so the
// builder can't fabricate resting orders). Pruning is builder discretion it already had
// (expiry policy lives off-chain), but it is now EXPLICIT in the input, never silent.
// Fail-closed: any bad signature / price mismatch / malformed section rejects the round.
struct PublicSection {
    book: Vec<Order>,       // on-chain resting book minus the pruned entries
    new_orders: Vec<Order>, // signature-verified new orders, in submission order
    bindings: Vec<wire::Binding>,
    book_hash: [u8; 32],    // H(raw input book bytes) — accumulate compares vs on-chain
}
fn parse_public_section(data: &[u8], mut off: usize, market: u32) -> Option<PublicSection> {
    let ns = ru16(data.get(off..off + 2)?, 0) as usize;
    off += 2;
    let sblob = data.get(off..off + ns * wire::SIGNED_ORDER_LEN)?;
    off += ns * wire::SIGNED_ORDER_LEN;
    let signed = wire::decode_signed_orders(sblob);
    if signed.len() != ns {
        return None;
    }
    let mut new_orders = Vec::with_capacity(ns);
    let mut bindings = Vec::with_capacity(ns);
    for s in &signed {
        let is_market = s.flags & wire::FLAG_MARKET != 0;
        if s.flags & wire::FLAG_SEALED != 0 {
            return None; // sealed orders never travel in the public section
        }
        // a limit order must execute at exactly the price the trader signed
        if !is_market && s.order.price != s.signed_price {
            return None;
        }
        let side = if s.order.side == Side::Buy { 0u8 } else { 1u8 };
        let msg =
            order_msg(s.order.account, market, side, s.order.qty, is_market, false, s.signed_price, s.seq);
        if !verify_signed(&s.pubkey, &msg, &s.sig) {
            return None;
        }
        new_orders.push(s.order);
        bindings.push(wire::Binding {
            account: s.order.account,
            seq: s.seq,
            pubkey: s.pubkey,
            flags: s.flags,
            price: s.order.price,
        });
    }
    let np = ru16(data.get(off..off + 2)?, 0) as usize;
    off += 2;
    let pruned = data.get(off..off + np * 8)?;
    off += np * 8;
    let book_bytes = data.get(off..)?;
    let book_hash = commitment(book_bytes);
    let mut book = Vec::new();
    'keep: for o in wire::decode_orders(book_bytes) {
        for i in 0..np {
            if o.account == ru32(pruned, i * 8) && o.id == ru32(pruned, i * 8 + 4) {
                continue 'keep;
            }
        }
        book.push(o);
    }
    Some(PublicSection { book, new_orders, bindings, book_hash })
}

// The verification trailer every round output now ends with, so accumulate can finish
// the checks refine couldn't do statelessly: [nb:u16][bindings 49×nb][book_hash 32][book].
fn push_auth_trailer(out: &mut Vec<u8>, bindings: &[wire::Binding], book_hash: &[u8; 32], book: &[u8]) {
    out.extend_from_slice(&(bindings.len() as u16).to_le_bytes());
    out.extend_from_slice(&wire::encode_bindings(bindings));
    out.extend_from_slice(book_hash);
    out.extend_from_slice(book);
}

// clear a signed public round → work-output:
// [TAG_SMATCH][market][base][quote][settle_len][settle][nb][bindings][book_hash][book]
fn smatch_output(market: u32, base: u32, quote: u32, ps: &PublicSection) -> Vec<u8> {
    let mut all: Vec<Order> = Vec::with_capacity(ps.book.len() + ps.new_orders.len());
    all.extend_from_slice(&ps.book);
    all.extend_from_slice(&ps.new_orders);
    let c = clear(&all);
    let settle = wire::encode_settlement(c.price, &all, &c);
    let book = wire::encode_orders(&resting(&all, &c));
    let mut out = Vec::with_capacity(51 + settle.len() + ps.bindings.len() * wire::BINDING_LEN + book.len());
    out.push(TAG_SMATCH);
    out.extend_from_slice(&market.to_le_bytes());
    out.extend_from_slice(&base.to_le_bytes());
    out.extend_from_slice(&quote.to_le_bytes());
    out.extend_from_slice(&(settle.len() as u32).to_le_bytes());
    out.extend_from_slice(&settle);
    push_auth_trailer(&mut out, &ps.bindings, &ps.book_hash, &book);
    out
}

// A unified sealed round. `plaintext` = the resting book + this round's public orders;
// `sealed` = the sealed orders whose reveal hash matched a recorded commitment; `consumed`
// = those matched commitment hashes (so accumulate removes ONLY these, not every commit).
// Everything clears together at ONE uniform price, so a sealed order can cross public /
// resting liquidity. Sealed orders are immediate-or-cancel: their unfilled remainder is
// excluded from the emitted book (never rests publicly, exposing its terms).
//
// Output: [TAG_REVEAL][market][base][quote]
//         [settle_len:u32][settle]            — all fills, at the uniform price
//         [consumed_len:u32][consumed(32×k)]  — commitment hashes this round consumed
//         [nb:u16][bindings][book_hash(32)]   — auth trailer (see push_auth_trailer)
//         [book]                              — new resting book (sealed remainder excluded)
fn reveal_output(
    market: u32,
    base: u32,
    quote: u32,
    ps: &PublicSection,
    sealed: &[Order],
    consumed: &[[u8; 32]],
) -> Vec<u8> {
    let mut all: Vec<Order> = Vec::with_capacity(ps.book.len() + ps.new_orders.len() + sealed.len());
    all.extend_from_slice(&ps.book);
    all.extend_from_slice(&ps.new_orders);
    all.extend_from_slice(sealed);
    let c = clear(&all);
    let settle = wire::encode_settlement(c.price, &all, &c);
    // resting = remainder of everything EXCEPT sealed orders (IOC: sealed remainder expires)
    let rest = resting(&all, &c);
    let public_rest: Vec<Order> =
        rest.into_iter().filter(|o| !sealed.iter().any(|s| s.id == o.id)).collect();
    let book = wire::encode_orders(&public_rest);
    let mut out = Vec::with_capacity(55 + settle.len() + consumed.len() * 32 + book.len());
    out.push(TAG_REVEAL);
    out.extend_from_slice(&market.to_le_bytes());
    out.extend_from_slice(&base.to_le_bytes());
    out.extend_from_slice(&quote.to_le_bytes());
    out.extend_from_slice(&(settle.len() as u32).to_le_bytes());
    out.extend_from_slice(&settle);
    out.extend_from_slice(&((consumed.len() * 32) as u32).to_le_bytes());
    for h in consumed {
        out.extend_from_slice(h);
    }
    push_auth_trailer(&mut out, &ps.bindings, &ps.book_hash, &book);
    out
}

// Encrypt-until-batch round output. `sealed` = orders the committee decrypted this round
// (each proven, see refine), `consumed` = the ciphertext ids (H(C1‖body)) to remove from the
// on-chain encset, `committee_h` = hash of the committee keys refine used (accumulate checks
// it against the on-chain committee). Same IOC/uniform-price semantics as reveal_output:
// sealed orders clear against the public book at one price and their remainder never rests.
//
// Output: [TAG_ENC_ROUND][market][base][quote]
//         [settle_len:u32][settle]
//         [consumed_len:u32][consumed(32×k)]   — ciphertext ids consumed
//         [committee_hash(32)]                 — committee refine used (accumulate verifies)
//         [nb:u16][bindings][book_hash(32)]    — auth trailer (see push_auth_trailer)
//         [book]                               — new resting book (sealed remainder excluded)
fn enc_round_output(
    market: u32,
    base: u32,
    quote: u32,
    ps: &PublicSection,
    sealed: &[Order],
    consumed: &[[u8; 32]],
    committee_h: &[u8; 32],
) -> Vec<u8> {
    let mut all: Vec<Order> = Vec::with_capacity(ps.book.len() + ps.new_orders.len() + sealed.len());
    all.extend_from_slice(&ps.book);
    all.extend_from_slice(&ps.new_orders);
    all.extend_from_slice(sealed);
    let c = clear(&all);
    let settle = wire::encode_settlement(c.price, &all, &c);
    let rest = resting(&all, &c);
    let public_rest: Vec<Order> =
        rest.into_iter().filter(|o| !sealed.iter().any(|s| s.id == o.id)).collect();
    let book = wire::encode_orders(&public_rest);
    let mut out = Vec::with_capacity(55 + settle.len() + consumed.len() * 32 + 32 + book.len());
    out.push(TAG_ENC_ROUND);
    out.extend_from_slice(&market.to_le_bytes());
    out.extend_from_slice(&base.to_le_bytes());
    out.extend_from_slice(&quote.to_le_bytes());
    out.extend_from_slice(&(settle.len() as u32).to_le_bytes());
    out.extend_from_slice(&settle);
    out.extend_from_slice(&((consumed.len() * 32) as u32).to_le_bytes());
    for h in consumed {
        out.extend_from_slice(h);
    }
    out.extend_from_slice(committee_h);
    push_auth_trailer(&mut out, &ps.bindings, &ps.book_hash, &book);
    out
}

// Apply conservation-checked settlement deltas (incl. the treasury fee) to balances at the
// uniform price, and update the market's last price + cumulative volume. Shared by the
// plaintext (TAG_MATCH) and sealed (TAG_REVEAL) settlement paths.
fn apply_settlement(base: u32, quote: u32, market: u32, settle: &[u8]) {
    let Some((price, entries)) = wire::decode_settlement(settle) else { return };
    if entries.is_empty() {
        return;
    }
    for (account, db, dq) in wire::settle_deltas(price, &entries, FEE_FLAT, FEE_ACCOUNT, SCALE) {
        let apply = |bal: u64, d: i128| -> u64 { (bal as i128 + d).clamp(0, u64::MAX as i128) as u64 };
        set_bal(base, account, apply(get_bal(base, account), db));
        set_bal(quote, account, apply(get_bal(quote, account), dq));
    }
    let volume: u64 = entries.iter().filter(|e| e.side == Side::Buy).map(|e| e.qty as u64).sum();
    set_storage(&mkey(b"lp", market), &price.to_le_bytes()).ok();
    let cum = get_storage(&mkey(b"cv", market)).map(|v| le_u64(&v)).unwrap_or(0) + volume;
    set_storage(&mkey(b"cv", market), &cum.to_le_bytes()).ok();
}

// Verify each 32-byte hash in `consumed` against a stored id-set blob (each consumed hash
// must claim a DISTINCT stored entry), then remove the matched entries. Entries NOT touched
// this round are preserved — no wholesale wipe, so an order committed-but-not-yet-settled
// (e.g. cancelled in the mempool) isn't destroyed. Returns false — leaving storage untouched
// — if any consumed hash has no stored match: refine only checks against the BUILDER-supplied
// set, so a hash missing on-chain means the builder injected an order/ciphertext that was
// never committed, and the whole round must be rejected rather than settled. Shared by the
// commit–reveal (b"commits") and encrypt-until-batch (b"encset") paths.
fn consume_set(key: &[u8], consumed: &[u8]) -> bool {
    let stored = get_storage(key).unwrap_or_default();
    let n = stored.len() / 32;
    let mut removed = Vec::new();
    removed.resize(n, false);
    for c in 0..(consumed.len() / 32) {
        let h = &consumed[c * 32..c * 32 + 32];
        let mut found = false;
        for j in 0..n {
            if !removed[j] && &stored[j * 32..j * 32 + 32] == h {
                removed[j] = true;
                found = true;
                break;
            }
        }
        if !found {
            return false;
        }
    }
    let mut out = Vec::with_capacity(stored.len());
    for j in 0..n {
        if !removed[j] {
            out.extend_from_slice(&stored[j * 32..j * 32 + 32]);
        }
    }
    set_storage(key, &out).ok();
    true
}

// Read a big-endian... no: little-endian u16 (all jamswap wire ints are LE).
fn ru16(b: &[u8], off: usize) -> u16 {
    u16::from_le_bytes([b[off], b[off + 1]])
}

// Market orders sign price 0 (the executed price is builder-derived), so accumulate holds the
// executed price to ±10% of the on-chain last clearing price. Must match MARKET_BAND in
// server.py — the builder derives inside the same band the service enforces.
const MARKET_BAND_PCT: u128 = 10;

// The state-side half of order verification (refine already did the crypto). For a round to
// settle, ALL of: the book refine matched against hashes to the on-chain book (no fabricated
// resting orders); every new order's carried pubkey IS the account's registered key; every
// seq strictly beats the account's floor (no replayed signatures — floors are per account and
// only ever rise); a market order's executed price sits within the band of the on-chain last
// price. Returns the floors to commit, or None to reject the round untouched (fail-closed —
// consistent with consume_set: a builder that includes one bad order forfeits the round).
fn check_round_auth(market: u32, bindings_blob: &[u8], book_hash: &[u8]) -> Option<Vec<(u32, u64)>> {
    if commitment(&get_storage(&mkey(b"book", market)).unwrap_or_default())[..] != *book_hash {
        return None; // refine was fed a book that isn't the on-chain book
    }
    let bindings = wire::decode_bindings(bindings_blob);
    let lp = get_storage(&mkey(b"lp", market)).map(|v| le_u64(&v)).unwrap_or(0);
    let mut floors: Vec<(u32, u64)> = Vec::new();
    for b in &bindings {
        if pubkey_of(b.account)? != b.pubkey {
            return None; // unregistered account or a key that isn't the registered one
        }
        let i = match floors.iter().position(|(a, _)| *a == b.account) {
            Some(i) => i,
            None => {
                floors.push((b.account, get_seq_floor(b.account)));
                floors.len() - 1
            }
        };
        if b.seq <= floors[i].1 {
            return None; // replayed (or intra-round duplicate) order signature
        }
        floors[i].1 = b.seq;
        if b.flags & wire::FLAG_MARKET != 0 {
            if lp == 0 {
                return None; // no reference price — a market order can't be bounded
            }
            let (p, l) = (b.price as u128, lp as u128);
            if p * 100 < l * (100 - MARKET_BAND_PCT) || p * 100 > l * (100 + MARKET_BAND_PCT) {
                return None; // builder-derived price outside the band the trader accepted
            }
        }
    }
    Some(floors)
}
fn commit_seq_floors(floors: &[(u32, u64)]) {
    for (a, s) in floors {
        set_seq_floor(*a, *s);
    }
}

// Hash the committee blob (exactly the bytes stored under b"committee": [n:u8][pks n*32]).
// refine outputs this hash; accumulate re-hashes the ON-CHAIN committee and compares, so a
// builder cannot swap in its own committee keys to steer the decryption to a forged order.
fn committee_hash(blob: &[u8]) -> [u8; 32] {
    commitment(blob)
}

// refine side of the sealed-encrypted round. Builder-supplied input:
//   [tag][market:4][base:4][quote:4]
//   [n:u8][committee_pks: n*32]                 — the committee keys (accumulate re-checks these)
//   [m:u16]                                      — number of sealed ciphertexts
//   m × ( [C1:32][body_len:u8][body] )           — the encrypted orders (bodies = ORDER_LEN)
//   m × [partials: n*PARTIAL_LEN]                — one proven partial per member per ciphertext
//   [signed public section]                      — see parse_public_section()
// Verifies every partial against the committee keys, recovers each order, verifies every
// public order's signature, and clears. Returns None (⇒ empty output ⇒ round dropped) on any
// malformed input, failed decryption proof, or bad order signature.
fn refine_enc_round(data: &[u8]) -> Option<Vec<u8>> {
    let n_off = 13usize;
    let n = *data.get(n_off)? as usize;
    if n == 0 {
        return None;
    }
    let (market, base, quote) = (ru32(data, 1), ru32(data, 5), ru32(data, 9));
    let mut off = n_off + 1;
    // committee keys
    let pks_blob = data.get(off..off + n * vdec::POINT_LEN)?;
    let mut pks: Vec<[u8; vdec::POINT_LEN]> = Vec::with_capacity(n);
    for i in 0..n {
        let mut a = [0u8; vdec::POINT_LEN];
        a.copy_from_slice(&pks_blob[i * vdec::POINT_LEN..(i + 1) * vdec::POINT_LEN]);
        pks.push(a);
    }
    off += n * vdec::POINT_LEN;
    let m = ru16(data.get(off..off + 2)?, 0) as usize;
    off += 2;
    // ciphertexts: m × (C1 ‖ body_len ‖ body)
    let mut ciphertexts: Vec<(&[u8], &[u8])> = Vec::with_capacity(m);
    for _ in 0..m {
        let c1 = data.get(off..off + vdec::POINT_LEN)?;
        off += vdec::POINT_LEN;
        let body_len = *data.get(off)? as usize;
        off += 1;
        let body = data.get(off..off + body_len)?;
        off += body_len;
        ciphertexts.push((c1, body));
    }
    // partials: m × (n × PARTIAL_LEN)
    let partials_blob = data.get(off..off + m * n * vdec::PARTIAL_LEN)?;
    off += m * n * vdec::PARTIAL_LEN;
    // remaining bytes = the signed public section (new orders verified here, in refine)
    let ps = parse_public_section(data, off, market)?;

    let mut sealed: Vec<Order> = Vec::with_capacity(m);
    let mut consumed: Vec<[u8; 32]> = Vec::with_capacity(m);
    for i in 0..m {
        let (c1, body) = ciphertexts[i];
        let parts = &partials_blob[i * n * vdec::PARTIAL_LEN..(i + 1) * n * vdec::PARTIAL_LEN];
        // verifiable decryption: every partial must prove out against the committed keys, or
        // the whole round is rejected (fail-closed). This is the MEV-resistance property —
        // nobody, not even the builder, can substitute a plaintext for a committed ciphertext.
        let order_bytes = vdec::verify_and_decrypt(c1, body, &pks, parts)?;
        let o = wire::decode_orders(&order_bytes).into_iter().next()?;
        sealed.push(o);
        // ciphertext id = H(C1 ‖ body): accumulate consumes exactly these from the encset.
        let mut idbuf = Vec::with_capacity(c1.len() + body.len());
        idbuf.extend_from_slice(c1);
        idbuf.extend_from_slice(body);
        consumed.push(commitment(&idbuf));
    }
    // committee hash over exactly [n][pks] — the bytes accumulate stores/checks.
    let mut blob = Vec::with_capacity(1 + n * vdec::POINT_LEN);
    blob.push(n as u8);
    blob.extend_from_slice(pks_blob);
    let ch = committee_hash(&blob);

    Some(enc_round_output(market, base, quote, &ps, &sealed, &consumed, &ch))
}

impl Service for Jamswap {
    fn refine(
        _core_index: CoreIndex,
        _item_index: usize,
        _service_id: ServiceId,
        payload: WorkPayload,
        _package_hash: WorkPackageHash,
    ) -> WorkOutput {
        let data = payload.take();
        if data.is_empty() {
            return Vec::new().into();
        }
        match data[0] {
            // signed public round: [tag][market][base][quote][signed public section]
            // — every NEW order's ed25519 signature is verified HERE, in refine, where gas is
            // abundant. Nobody (builder included) can inject an order a trader didn't sign.
            TAG_SMATCH if data.len() >= 13 => {
                let (market, base, quote) = (ru32(&data, 1), ru32(&data, 5), ru32(&data, 9));
                match parse_public_section(&data, 13, market) {
                    Some(ps) => smatch_output(market, base, quote, &ps).into(),
                    None => Vec::new().into(), // bad signature / malformed — round dropped
                }
            }
            // echoes for accumulate (auth + state changes happen there, where storage lives)
            TAG_DEPOSIT | TAG_COMMIT | TAG_CANCEL | TAG_WITHDRAW | TAG_LIST | TAG_REGISTER
            | TAG_TREASURY | TAG_ENC_SETUP | TAG_ENC_COMMIT => data.into(),
            // Sealed-encrypted round (encrypt-until-batch): the committee has decrypted, so
            // refine verifies each partial against the builder-supplied committee keys,
            // recovers each order, and clears — no reveal round, no owner liveness. accumulate
            // re-checks the committee keys and ciphertext ids against on-chain state.
            TAG_ENC_ROUND => refine_enc_round(&data).map(Into::into).unwrap_or_else(|| Vec::new().into()),
            // Unified sealed round. Input:
            //   [tag][market][base][quote]
            //   [commits_len:u32][commits]
            //   [reveals_len:u32][reveals]        — n×(order17 ‖ nonce32)
            //   [signed public section]           — see parse_public_section()
            TAG_REVEAL if data.len() >= 17 => {
                let cl = ru32(&data, 13) as usize;
                if data.len() < 21 + cl {
                    return Vec::new().into();
                }
                let (market, base, quote) = (ru32(&data, 1), ru32(&data, 5), ru32(&data, 9));
                let commits = &data[17..17 + cl];
                let rl = ru32(&data, 17 + cl) as usize;
                let reveals_off = 21 + cl;
                if data.len() < reveals_off + rl {
                    return Vec::new().into();
                }
                let reveals = &data[reveals_off..reveals_off + rl];
                let Some(ps) = parse_public_section(&data, reveals_off + rl, market) else {
                    return Vec::new().into(); // bad public-order signature — round dropped
                };
                let n_commit = commits.len() / 32;
                let mut verified: Vec<Order> = Vec::new();
                let mut consumed: Vec<[u8; 32]> = Vec::new();
                for i in 0..(reveals.len() / REVEAL_LEN) {
                    let r = &reveals[i * REVEAL_LEN..(i + 1) * REVEAL_LEN];
                    let h = commitment(r);
                    let mut ok = false;
                    for j in 0..n_commit {
                        if commits[j * 32..j * 32 + 32] == h[..] {
                            ok = true;
                            break;
                        }
                    }
                    // admit only orders whose hash matches an on-chain commitment (uncommitted
                    // orders are rejected — the MEV-resistance property), and record the hash so
                    // accumulate consumes exactly this commitment.
                    if ok {
                        if let Some(o) = wire::decode_orders(&r[..wire::ORDER_LEN]).into_iter().next() {
                            verified.push(o);
                            consumed.push(h);
                        }
                    }
                }
                reveal_output(market, base, quote, &ps, &verified, &consumed).into()
            }
            _ => Vec::new().into(),
        }
    }

    fn accumulate(_slot: Slot, _service_id: ServiceId, _item_count: usize) -> Option<Hash> {
        for item in accumulate_items() {
            let rec = match item {
                AccumulateItem::WorkItem(r) => r,
                _ => continue,
            };
            let out = match rec.result {
                Ok(o) => o.0,
                Err(_) => continue,
            };
            if out.is_empty() {
                continue;
            }
            match out[0] {
                // [tag][account][asset_id][amount]
                TAG_DEPOSIT if out.len() >= 1 + 4 + 4 + 8 => {
                    let account = ru32(&out, 1);
                    let asset = ru32(&out, 5);
                    let amount = le_u64(&out[9..17]);
                    set_bal(asset, account, get_bal(asset, account).saturating_add(amount));
                    set_cust(asset, get_cust(asset).saturating_add(amount));
                }
                // signed withdraw: [tag][handle(4)][asset(4)][amount(8)][nonce(8)][sig(64)]
                // — verify the account key + a matching nonce (replay-proof), THEN debit if funded.
                TAG_WITHDRAW if out.len() >= 1 + 4 + 4 + 8 + 8 + 64 => {
                    let handle = ru32(&out, 1);
                    let asset = ru32(&out, 5);
                    let amount = le_u64(&out[9..17]);
                    let nonce = le_u64(&out[17..25]);
                    let sig = sig64(&out, 25);
                    let Some(pk) = pubkey_of(handle) else { continue };
                    let msg = canon(
                        b"withdraw",
                        &[&handle.to_le_bytes(), &asset.to_le_bytes(), &amount.to_le_bytes(), &nonce.to_le_bytes()],
                    );
                    if nonce != get_nonce(handle) || !verify_signed(&pk, &msg, &sig) {
                        continue; // wrong key, tampered, or replayed
                    }
                    set_nonce(handle, nonce + 1); // consume the nonce whenever auth passes
                    let b = get_bal(asset, handle);
                    if b >= amount {
                        set_bal(asset, handle, b - amount);
                        set_cust(asset, get_cust(asset).saturating_sub(amount));
                    }
                }
                // [tag][market][account][commitment(32)]
                TAG_COMMIT if out.len() >= 1 + 4 + 4 + 32 => {
                    let market = ru32(&out, 1);
                    let key = mkey(b"commits", market);
                    let mut commits = get_storage(&key).unwrap_or_default();
                    commits.extend_from_slice(&out[9..9 + 32]);
                    set_storage(&key, &commits).ok();
                }
                // signed cancel: [tag][handle(4)][market(4)][order_id(4)][nonce(8)][sig(64)]
                // — only the account that owns the resting order can remove it.
                TAG_CANCEL if out.len() >= 1 + 4 + 4 + 4 + 8 + 64 => {
                    let handle = ru32(&out, 1);
                    let market = ru32(&out, 5);
                    let oid = ru32(&out, 9);
                    let nonce = le_u64(&out[13..21]);
                    let sig = sig64(&out, 21);
                    let Some(pk) = pubkey_of(handle) else { continue };
                    let msg = canon(
                        b"cancel",
                        &[&handle.to_le_bytes(), &market.to_le_bytes(), &oid.to_le_bytes(), &nonce.to_le_bytes()],
                    );
                    if nonce != get_nonce(handle) || !verify_signed(&pk, &msg, &sig) {
                        continue;
                    }
                    set_nonce(handle, nonce + 1);
                    let book_key = mkey(b"book", market);
                    let orders = wire::decode_orders(&get_storage(&book_key).unwrap_or_default());
                    let kept: Vec<Order> =
                        orders.into_iter().filter(|o| !(o.account == handle && o.id == oid)).collect();
                    set_storage(&book_key, &wire::encode_orders(&kept)).ok();
                }
                // signed register: [tag][pubkey(32)][sig(64)] — bind an ed25519 key to a handle
                TAG_REGISTER if out.len() >= 1 + 32 + 64 => {
                    let mut pk = [0u8; 32];
                    pk.copy_from_slice(&out[1..33]);
                    let sig = sig64(&out, 33);
                    let msg = canon(b"register", &[&pk]);
                    if verify_signed(&pk, &msg, &sig) {
                        register_key(&pk);
                    }
                }
                // governance treasury sweep: [tag][asset(4)][amount(8)][dest(4)][nonce(8)][sig(64)]
                // — only the baked GOV_PUBKEY can move accrued fees out of FEE_ACCOUNT.
                TAG_TREASURY if out.len() >= 1 + 4 + 8 + 4 + 8 + 64 => {
                    let asset = ru32(&out, 1);
                    let amount = le_u64(&out[5..13]);
                    let dest = ru32(&out, 13);
                    let nonce = le_u64(&out[17..25]);
                    let sig = sig64(&out, 25);
                    let gov_nonce = get_storage(b"govnonce").map(|v| le_u64(&v)).unwrap_or(0);
                    let msg = canon(
                        b"treasury",
                        &[&asset.to_le_bytes(), &amount.to_le_bytes(), &dest.to_le_bytes(), &nonce.to_le_bytes()],
                    );
                    if nonce != gov_nonce || !verify_signed(&GOV_PUBKEY, &msg, &sig) {
                        continue;
                    }
                    set_storage(b"govnonce", &(nonce + 1).to_le_bytes()).ok();
                    // internal transfer treasury -> dest (custody per asset is unchanged)
                    let t = get_bal(asset, FEE_ACCOUNT);
                    let moved = amount.min(t);
                    if moved > 0 {
                        set_bal(asset, FEE_ACCOUNT, t - moved);
                        set_bal(asset, dest, get_bal(asset, dest).saturating_add(moved));
                    }
                }
                // [tag][market][base][quote] — list a market with canonical assets
                TAG_LIST if out.len() >= 1 + 4 + 4 + 4 => {
                    let (market, base, quote) = (ru32(&out, 1), ru32(&out, 5), ru32(&out, 9));
                    if market_assets(market).is_none() {
                        let mut v = Vec::with_capacity(8);
                        v.extend_from_slice(&base.to_le_bytes());
                        v.extend_from_slice(&quote.to_le_bytes());
                        set_storage(&mkey(b"mkt", market), &v).ok();
                        // append to the discoverable market index
                        let mut idx = get_storage(b"markets").unwrap_or_default();
                        idx.extend_from_slice(&market.to_le_bytes());
                        set_storage(b"markets", &idx).ok();
                    }
                }
                // unified sealed round (immediate-or-cancel for sealed orders):
                // [tag][m][b][q][settle_len][settle][consumed_len][consumed][nb][bindings][book_hash][book]
                // — verify the public-order bindings + input-book hash, consume ONLY the revealed
                // commitments, then settle and write the new public book (sealed remainder
                // already excluded in refine). ALL checks precede ANY state change.
                TAG_REVEAL if out.len() >= 21 => {
                    let (market, base, quote) = (ru32(&out, 1), ru32(&out, 5), ru32(&out, 9));
                    if market_assets(market) != Some((base, quote)) {
                        continue;
                    }
                    let settle_len = ru32(&out, 13) as usize;
                    if out.len() < 21 + settle_len {
                        continue;
                    }
                    let settle = &out[17..17 + settle_len];
                    let consumed_len = ru32(&out, 17 + settle_len) as usize;
                    let consumed_off = 21 + settle_len;
                    if out.len() < consumed_off + consumed_len + 2 {
                        continue;
                    }
                    let consumed = &out[consumed_off..consumed_off + consumed_len];
                    let nb = ru16(&out, consumed_off + consumed_len) as usize;
                    let b_off = consumed_off + consumed_len + 2;
                    if out.len() < b_off + nb * wire::BINDING_LEN + 32 {
                        continue;
                    }
                    let bindings = &out[b_off..b_off + nb * wire::BINDING_LEN];
                    let h_off = b_off + nb * wire::BINDING_LEN;
                    let book_hash = &out[h_off..h_off + 32];
                    let book = &out[h_off + 32..];
                    let Some(floors) = check_round_auth(market, bindings, book_hash) else {
                        continue; // forged pubkey binding / replayed seq / fabricated book
                    };
                    // consume-or-reject BEFORE settling: every revealed commitment must exist
                    // on-chain, else the builder smuggled in an uncommitted order.
                    if !consume_set(&mkey(b"commits", market), consumed) {
                        continue;
                    }
                    commit_seq_floors(&floors);
                    set_storage(&mkey(b"book", market), book).ok();
                    apply_settlement(base, quote, market, settle);
                }
                // signed public round:
                // [tag][m][b][q][settle_len][settle][nb][bindings][book_hash][book]
                TAG_SMATCH if out.len() >= 19 => {
                    let (market, base, quote) = (ru32(&out, 1), ru32(&out, 5), ru32(&out, 9));
                    // integrity: the market must be listed with exactly these assets
                    if market_assets(market) != Some((base, quote)) {
                        continue; // unlisted or asset-mismatched market — reject the round
                    }
                    let settle_len = ru32(&out, 13) as usize;
                    if out.len() < 19 + settle_len {
                        continue;
                    }
                    let settle = &out[17..17 + settle_len];
                    let nb = ru16(&out, 17 + settle_len) as usize;
                    let b_off = 19 + settle_len;
                    if out.len() < b_off + nb * wire::BINDING_LEN + 32 {
                        continue;
                    }
                    let bindings = &out[b_off..b_off + nb * wire::BINDING_LEN];
                    let h_off = b_off + nb * wire::BINDING_LEN;
                    let book_hash = &out[h_off..h_off + 32];
                    let book = &out[h_off + 32..];
                    let Some(floors) = check_round_auth(market, bindings, book_hash) else {
                        continue; // forged pubkey binding / replayed seq / fabricated book
                    };
                    commit_seq_floors(&floors);
                    set_storage(&mkey(b"book", market), book).ok();
                    apply_settlement(base, quote, market, settle);
                }
                // gov-signed committee setup: [tag][n:u8][pks n*32][nonce(8)][sig(64)]
                // — commits the encrypt-until-batch committee keys on-chain. Only GOV_PUBKEY
                // can set/rotate them (same authority as the treasury), nonce-protected.
                TAG_ENC_SETUP if out.len() >= 1 + 1 + 8 + 64 => {
                    let n = out[1] as usize;
                    let pks_end = 2 + n * 32;
                    if out.len() != pks_end + 8 + 64 {
                        continue; // exact-length: n keys + nonce + sig
                    }
                    let pks = &out[2..pks_end];
                    let nonce = le_u64(&out[pks_end..pks_end + 8]);
                    let sig = sig64(&out, pks_end + 8);
                    let com_nonce = get_storage(b"comnonce").map(|v| le_u64(&v)).unwrap_or(0);
                    let msg = canon(b"committee", &[&[n as u8], pks, &nonce.to_le_bytes()]);
                    if nonce != com_nonce || !verify_signed(&GOV_PUBKEY, &msg, &sig) {
                        continue;
                    }
                    set_storage(b"comnonce", &(nonce + 1).to_le_bytes()).ok();
                    // store exactly [n][pks] — the bytes committee_hash() runs over.
                    let mut blob = Vec::with_capacity(1 + pks.len());
                    blob.push(n as u8);
                    blob.extend_from_slice(pks);
                    set_storage(b"committee", &blob).ok();
                }
                // encrypted-order commit: [tag][market(4)][C1(32)][body(ORDER_LEN)]
                // — record id = H(C1‖body) in the market's encset. The ciphertext reveals
                // nothing until the committee decrypts it in a round.
                TAG_ENC_COMMIT if out.len() >= 1 + 4 + vdec::POINT_LEN + wire::ORDER_LEN => {
                    let market = ru32(&out, 1);
                    let id = commitment(&out[5..5 + vdec::POINT_LEN + wire::ORDER_LEN]);
                    let key = mkey(b"encset", market);
                    let mut set = get_storage(&key).unwrap_or_default();
                    set.extend_from_slice(&id);
                    set_storage(&key, &set).ok();
                }
                // sealed-encrypted round: [tag][market][base][quote][settle_len][settle]
                //   [consumed_len][consumed][committee_hash(32)][nb][bindings][book_hash][book]
                // — verify the round used the ON-CHAIN committee, verify the public-order
                // bindings + input-book hash, consume-or-reject the ciphertext ids, then
                // settle. All builder-defence checks fail-closed, before any state change.
                TAG_ENC_ROUND if out.len() >= 23 + 32 => {
                    let (market, base, quote) = (ru32(&out, 1), ru32(&out, 5), ru32(&out, 9));
                    if market_assets(market) != Some((base, quote)) {
                        continue;
                    }
                    let settle_len = ru32(&out, 13) as usize;
                    if out.len() < 21 + settle_len {
                        continue;
                    }
                    let settle = &out[17..17 + settle_len];
                    let consumed_len = ru32(&out, 17 + settle_len) as usize;
                    let consumed_off = 21 + settle_len;
                    if out.len() < consumed_off + consumed_len + 32 + 2 {
                        continue;
                    }
                    let consumed = &out[consumed_off..consumed_off + consumed_len];
                    let ch = &out[consumed_off + consumed_len..consumed_off + consumed_len + 32];
                    let nb = ru16(&out, consumed_off + consumed_len + 32) as usize;
                    let b_off = consumed_off + consumed_len + 34;
                    if out.len() < b_off + nb * wire::BINDING_LEN + 32 {
                        continue;
                    }
                    let bindings = &out[b_off..b_off + nb * wire::BINDING_LEN];
                    let h_off = b_off + nb * wire::BINDING_LEN;
                    let book_hash = &out[h_off..h_off + 32];
                    let book = &out[h_off + 32..];
                    // (1) the round MUST have used the committed committee keys, else a builder
                    // could supply its own committee + partials and decrypt to a forged order.
                    let committee = get_storage(b"committee").unwrap_or_default();
                    if committee.is_empty() || committee_hash(&committee)[..] != *ch {
                        continue;
                    }
                    // (2) the public-order bindings + the input book must check out.
                    let Some(floors) = check_round_auth(market, bindings, book_hash) else {
                        continue;
                    };
                    // (3) consume-or-reject: every ciphertext id must exist in the encset.
                    if !consume_set(&mkey(b"encset", market), consumed) {
                        continue;
                    }
                    commit_seq_floors(&floors);
                    set_storage(&mkey(b"book", market), book).ok();
                    apply_settlement(base, quote, market, settle);
                }
                _ => {}
            }
        }
        None
    }
}
