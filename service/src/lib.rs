#![no_std]
#![no_main]

extern crate alloc;

use alloc::vec::Vec;

use jam_pvm_common::accumulate::{accumulate_items, get_storage, set_storage};
use jam_pvm_common::jam_types::*;
use jam_pvm_common::{declare_service, Service};

use blake2::{Blake2s256, Digest};
use match_engine::{clear, resting, wire, Order, Side};

declare_service!(Marmalade);
struct Marmalade;

// payload / work-output tags
const TAG_MATCH: u8 = 0; // plaintext batch for one market: [tag][market][base][quote][orders…]
const TAG_DEPOSIT: u8 = 1; // [tag][account][asset_id][amount] — fund a balance (Phase-2 faucet)
const TAG_COMMIT: u8 = 2; // [tag][market][account][commitment(32)] — seal a hidden order
const TAG_REVEAL: u8 = 3; // [tag][market][base][quote][commits_len][commits][reveals] — reveal+match
const TAG_CANCEL: u8 = 4; // [tag][market][account][order_id] — cancel a resting order
const TAG_WITHDRAW: u8 = 5; // [tag][account][asset_id][amount] — debit balance (+ custody)
const TAG_LIST: u8 = 6; // [tag][market][base][quote] — list a market (canonical assets)

// trading fee: a flat fee on matched quote notional (FBA has no maker/taker), paid
// by both sides into the treasury account (in the market's quote asset). 30 bps.
const FEE_BPS: u32 = 30;
const FEE_ACCOUNT: u32 = u32::MAX;

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

impl Service for Marmalade {
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
            // echoes for accumulate
            TAG_DEPOSIT | TAG_COMMIT | TAG_CANCEL | TAG_WITHDRAW | TAG_LIST => data.into(),
            TAG_REVEAL if data.len() >= 17 => {
                let (market, base, quote) = (ru32(&data, 1), ru32(&data, 5), ru32(&data, 9));
                let cl = ru32(&data, 13) as usize;
                if data.len() < 17 + cl {
                    return Vec::new().into();
                }
                let commits = &data[17..17 + cl];
                let reveals = &data[17 + cl..];
                let n_commit = commits.len() / 32;
                let mut verified: Vec<Order> = Vec::new();
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
                    if ok {
                        if let Some(o) = wire::decode_orders(&r[..wire::ORDER_LEN]).into_iter().next() {
                            verified.push(o);
                        }
                    }
                }
                match_output(market, base, quote, &verified).into()
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
                // withdraw: debit balance + custody, only if funded (no overdraft)
                TAG_WITHDRAW if out.len() >= 1 + 4 + 4 + 8 => {
                    let account = ru32(&out, 1);
                    let asset = ru32(&out, 5);
                    let amount = le_u64(&out[9..17]);
                    let b = get_bal(asset, account);
                    if b >= amount {
                        set_bal(asset, account, b - amount);
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
                // [tag][market][account][order_id]
                TAG_CANCEL if out.len() >= 1 + 4 + 4 + 4 => {
                    let market = ru32(&out, 1);
                    let account = ru32(&out, 5);
                    let oid = ru32(&out, 9);
                    let book_key = mkey(b"book", market);
                    let orders = wire::decode_orders(&get_storage(&book_key).unwrap_or_default());
                    let kept: Vec<Order> =
                        orders.into_iter().filter(|o| !(o.account == account && o.id == oid)).collect();
                    set_storage(&book_key, &wire::encode_orders(&kept)).ok();
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
                // [tag][market][base][quote][settle_len][settle][book]
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
                    set_storage(&mkey(b"commits", market), &[]).ok();
                    if let Some((price, entries)) = wire::decode_settlement(settle) {
                        // conservation-checked deltas (incl. the fee to the treasury):
                        // base_delta in base asset, quote_delta in quote asset.
                        for (account, db, dq) in wire::settle_deltas(price, &entries, FEE_BPS, FEE_ACCOUNT) {
                            let nb = (get_bal(base, account) as i64 + db).max(0) as u64;
                            let nq = (get_bal(quote, account) as i64 + dq).max(0) as u64;
                            set_bal(base, account, nb);
                            set_bal(quote, account, nq);
                        }
                        let volume: u64 =
                            entries.iter().filter(|e| e.side == Side::Buy).map(|e| e.qty as u64).sum();
                        set_storage(&mkey(b"lp", market), &price.to_le_bytes()).ok();
                        let cum = get_storage(&mkey(b"cv", market)).map(|v| le_u64(&v)).unwrap_or(0) + volume;
                        set_storage(&mkey(b"cv", market), &cum.to_le_bytes()).ok();
                    }
                }
                _ => {}
            }
        }
        None
    }
}
