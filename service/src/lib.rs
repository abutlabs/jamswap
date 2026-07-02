#![no_std]
#![no_main]

extern crate alloc;

use alloc::vec::Vec;

use jam_pvm_common::accumulate::{accumulate_items, get_storage, set_storage};
use jam_pvm_common::jam_types::*;
use jam_pvm_common::{declare_service, Service};

use blake2::{Blake2s256, Digest};
use match_engine::auth::{canon, verify_signed};
use match_engine::{clear, resting, wire, Order, Side};

declare_service!(Jamswap);
struct Jamswap;

// ed25519 verification (curve field arithmetic) needs more stack than polkavm's small
// default — without this the verify traps and the whole accumulate rolls back. 1 MiB is
// ample for ed25519 (the sibling zk-jam-service bumps to 4 MiB for BN254 pairing).
// PVM-target only; skipped on host builds (CI compile gate).
#[cfg(target_arch = "riscv64")]
polkavm_derive::min_stack_size!(1024 * 1024);

// payload / work-output tags
const TAG_MATCH: u8 = 0; // plaintext batch for one market: [tag][market][base][quote][orders…]
const TAG_DEPOSIT: u8 = 1; // [tag][account][asset_id][amount] — fund a balance (Phase-2 faucet)
const TAG_COMMIT: u8 = 2; // [tag][market][account][commitment(32)] — seal a hidden order
const TAG_REVEAL: u8 = 3; // unified sealed round — see reveal_output() for the wire layout
const TAG_CANCEL: u8 = 4; // [tag][market][account][order_id] — cancel a resting order
const TAG_WITHDRAW: u8 = 5; // [tag][account][asset][amount][nonce][sig(64)] — signed debit
const TAG_LIST: u8 = 6; // [tag][market][base][quote] — list a market (canonical assets)
const TAG_REGISTER: u8 = 7; // [tag][pubkey(32)][sig(64)] — bind an ed25519 key to an account handle
const TAG_TREASURY: u8 = 8; // [tag][asset(4)][amount(8)][dest(4)][nonce(8)][sig(64)] — governance fee sweep

// Governance key authorised to sweep the fee treasury. Derived from a documented seed for the
// demo (see sim/); in production this is a DAO/multisig key. Only signatures by this key can
// move funds out of FEE_ACCOUNT, and a dedicated governance nonce (b"govnonce") stops replay.
const GOV_PUBKEY: [u8; 32] = [
    0x90, 0x37, 0x37, 0x55, 0x60, 0x00, 0xf3, 0xf2, 0x64, 0x66, 0xd6, 0x30, 0x43, 0x64, 0xf1, 0xd2,
    0x22, 0x6e, 0xe8, 0x34, 0x0f, 0xfe, 0xe3, 0x66, 0x26, 0xc3, 0x15, 0xd0, 0x4b, 0xcf, 0xd5, 0x68,
];

// trading fee: a flat fee on matched quote notional (FBA has no maker/taker), paid
// by both sides into the treasury account (in the market's quote asset). 30 bps.
const FEE_BPS: u32 = 30;
const FEE_ACCOUNT: u32 = u32::MAX;

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

// clear a market's orders → work-output: [TAG_MATCH][market][base][quote][settle_len][settle][book]
fn match_output(market: u32, base: u32, quote: u32, orders: &[Order]) -> Vec<u8> {
    let c = clear(orders);
    let settle = wire::encode_settlement(c.price, orders, &c);
    let book = wire::encode_orders(&resting(orders, &c));
    let mut out = Vec::with_capacity(17 + settle.len() + book.len());
    out.push(TAG_MATCH);
    out.extend_from_slice(&market.to_le_bytes());
    out.extend_from_slice(&base.to_le_bytes());
    out.extend_from_slice(&quote.to_le_bytes());
    out.extend_from_slice(&(settle.len() as u32).to_le_bytes());
    out.extend_from_slice(&settle);
    out.extend_from_slice(&book);
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
//         [book]                              — new resting book (sealed remainder excluded)
fn reveal_output(
    market: u32,
    base: u32,
    quote: u32,
    plaintext: &[Order],
    sealed: &[Order],
    consumed: &[[u8; 32]],
) -> Vec<u8> {
    let mut all: Vec<Order> = Vec::with_capacity(plaintext.len() + sealed.len());
    all.extend_from_slice(plaintext);
    all.extend_from_slice(sealed);
    let c = clear(&all);
    let settle = wire::encode_settlement(c.price, &all, &c);
    // resting = remainder of everything EXCEPT sealed orders (IOC: sealed remainder expires)
    let rest = resting(&all, &c);
    let public_rest: Vec<Order> =
        rest.into_iter().filter(|o| !sealed.iter().any(|s| s.id == o.id)).collect();
    let book = wire::encode_orders(&public_rest);
    let mut out = Vec::with_capacity(21 + settle.len() + consumed.len() * 32 + book.len());
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
    out.extend_from_slice(&book);
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
    for (account, db, dq) in wire::settle_deltas(price, &entries, FEE_BPS, FEE_ACCOUNT, SCALE) {
        let apply = |bal: u64, d: i128| -> u64 { (bal as i128 + d).clamp(0, u64::MAX as i128) as u64 };
        set_bal(base, account, apply(get_bal(base, account), db));
        set_bal(quote, account, apply(get_bal(quote, account), dq));
    }
    let volume: u64 = entries.iter().filter(|e| e.side == Side::Buy).map(|e| e.qty as u64).sum();
    set_storage(&mkey(b"lp", market), &price.to_le_bytes()).ok();
    let cum = get_storage(&mkey(b"cv", market)).map(|v| le_u64(&v)).unwrap_or(0) + volume;
    set_storage(&mkey(b"cv", market), &cum.to_le_bytes()).ok();
}

// Remove each 32-byte hash in `consumed` from the stored commitments blob (first match per
// hash). Commitments that were NOT revealed this round are preserved — no wholesale wipe, so
// an order committed-but-not-yet-revealed (e.g. cancelled in the mempool) isn't destroyed.
fn consume_commits(market: u32, consumed: &[u8]) {
    let key = mkey(b"commits", market);
    let stored = get_storage(&key).unwrap_or_default();
    let n = stored.len() / 32;
    let mut removed = Vec::new();
    removed.resize(n, false);
    for c in 0..(consumed.len() / 32) {
        let h = &consumed[c * 32..c * 32 + 32];
        for j in 0..n {
            if !removed[j] && &stored[j * 32..j * 32 + 32] == h {
                removed[j] = true;
                break;
            }
        }
    }
    let mut out = Vec::with_capacity(stored.len());
    for j in 0..n {
        if !removed[j] {
            out.extend_from_slice(&stored[j * 32..j * 32 + 32]);
        }
    }
    set_storage(&key, &out).ok();
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
            TAG_MATCH if data.len() >= 13 => {
                let (market, base, quote) = (ru32(&data, 1), ru32(&data, 5), ru32(&data, 9));
                match_output(market, base, quote, &wire::decode_orders(&data[13..])).into()
            }
            // echoes for accumulate (auth + state changes happen there, where storage lives)
            TAG_DEPOSIT | TAG_COMMIT | TAG_CANCEL | TAG_WITHDRAW | TAG_LIST | TAG_REGISTER
            | TAG_TREASURY => data.into(),
            // Unified sealed round. Input:
            //   [tag][market][base][quote]
            //   [commits_len:u32][commits]
            //   [reveals_len:u32][reveals]        — n×(order17 ‖ nonce32)
            //   [plaintext orders]                — resting book + public orders (each 17B)
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
                let plaintext = wire::decode_orders(&data[reveals_off + rl..]);
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
                reveal_output(market, base, quote, &plaintext, &verified, &consumed).into()
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
                // [tag][market][base][quote][settle_len][settle][consumed_len][consumed][book]
                // — settle everything that crossed, consume ONLY the revealed commitments, and
                // write the new public book (sealed remainder already excluded in refine).
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
                    if out.len() < consumed_off + consumed_len {
                        continue;
                    }
                    let consumed = &out[consumed_off..consumed_off + consumed_len];
                    let book = &out[consumed_off + consumed_len..];
                    consume_commits(market, consumed);
                    set_storage(&mkey(b"book", market), book).ok();
                    apply_settlement(base, quote, market, settle);
                }
                // [tag][market][base][quote][settle_len][settle][book] — plaintext (public) round
                TAG_MATCH if out.len() >= 17 => {
                    let (market, base, quote) = (ru32(&out, 1), ru32(&out, 5), ru32(&out, 9));
                    // integrity: the market must be listed with exactly these assets
                    if market_assets(market) != Some((base, quote)) {
                        continue; // unlisted or asset-mismatched market — reject the round
                    }
                    let settle_len = ru32(&out, 13) as usize;
                    if out.len() < 17 + settle_len {
                        continue;
                    }
                    let settle = &out[17..17 + settle_len];
                    let book = &out[17 + settle_len..];
                    set_storage(&mkey(b"book", market), book).ok();
                    apply_settlement(base, quote, market, settle);
                }
                _ => {}
            }
        }
        None
    }
}
