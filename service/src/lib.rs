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
const TAG_MATCH: u8 = 0; // a sealed batch of PLAINTEXT orders to clear + settle
const TAG_DEPOSIT: u8 = 1; // fund a trader's balance (Phase-2 faucet; real custody = Phase 3)
const TAG_COMMIT: u8 = 2; // commit a hidden order: H(order ‖ nonce) — front-running resistance
const TAG_REVEAL: u8 = 3; // reveal+match a batch: verify each order against its commitment

const ASSET_BASE: u8 = 0;
const ASSET_QUOTE: u8 = 1;
const NONCE_LEN: usize = 32;
const REVEAL_LEN: usize = wire::ORDER_LEN + NONCE_LEN; // order(17) ‖ nonce(32)

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

// balance storage key: "B"|"Q" ++ account(4 LE)
fn bal_key(asset: u8, account: u32) -> Vec<u8> {
    let mut k = Vec::with_capacity(5);
    k.push(if asset == ASSET_BASE { b'B' } else { b'Q' });
    k.extend_from_slice(&account.to_le_bytes());
    k
}
fn get_bal(asset: u8, account: u32) -> u64 {
    get_storage(&bal_key(asset, account)).map(|v| le_u64(&v)).unwrap_or(0)
}
fn set_bal(asset: u8, account: u32, v: u64) {
    set_storage(&bal_key(asset, account), &v.to_le_bytes()).ok();
}

// clear a set of orders and produce the settlement+book work-output (the form
// accumulate settles). Shared by the plaintext (TAG_MATCH) and sealed (TAG_REVEAL)
// paths — only how the order set is obtained differs.
fn match_output(orders: &[Order]) -> Vec<u8> {
    let c = clear(orders);
    let settle = wire::encode_settlement(c.price, orders, &c);
    let book = wire::encode_orders(&resting(orders, &c));
    let mut out = Vec::with_capacity(5 + settle.len() + book.len());
    out.push(TAG_MATCH);
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
            // plaintext batch → clear + settle
            TAG_MATCH => match_output(&wire::decode_orders(&data[1..])).into(),

            // fund a balance (echo for accumulate)
            TAG_DEPOSIT => data.into(),

            // commit a hidden order (echo the [tag][account][commitment] for accumulate)
            TAG_COMMIT => data.into(),

            // reveal + match: only orders whose H(order‖nonce) matches a recorded
            // commitment are admitted to the auction → you cannot inject an order
            // you didn't commit before the batch sealed. Layout:
            //   [TAG][commits_len:u32][commits (32B each)][reveals (49B each)]
            TAG_REVEAL => {
                if data.len() < 5 {
                    return Vec::new().into();
                }
                let cl = u32::from_le_bytes([data[1], data[2], data[3], data[4]]) as usize;
                if data.len() < 5 + cl {
                    return Vec::new().into();
                }
                let commits = &data[5..5 + cl];
                let reveals = &data[5 + cl..];
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
                match_output(&verified).into()
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
                TAG_DEPOSIT if out.len() >= 1 + 4 + 1 + 8 => {
                    let account = u32::from_le_bytes([out[1], out[2], out[3], out[4]]);
                    let asset = out[5];
                    let amount = le_u64(&out[6..14]);
                    set_bal(asset, account, get_bal(asset, account).saturating_add(amount));
                }
                // record a commitment (append its 32 bytes to the pending set)
                TAG_COMMIT if out.len() >= 1 + 4 + 32 => {
                    let mut commits = get_storage(b"commits").unwrap_or_default();
                    commits.extend_from_slice(&out[5..5 + 32]);
                    set_storage(b"commits", &commits).ok();
                }
                TAG_MATCH if out.len() >= 5 => {
                    let settle_len = u32::from_le_bytes([out[1], out[2], out[3], out[4]]) as usize;
                    if out.len() < 5 + settle_len {
                        continue;
                    }
                    let settle = &out[5..5 + settle_len];
                    let book = &out[5 + settle_len..];
                    set_storage(b"book", book).ok();
                    // a clearing consumes the pending commitments (the sealed round closed)
                    set_storage(b"commits", &[]).ok();
                    if let Some((price, entries)) = wire::decode_settlement(settle) {
                        // apply conservation-checked per-account deltas (Σ=0 per asset)
                        for (account, db, dq) in wire::settle_deltas(price, &entries) {
                            let base = (get_bal(ASSET_BASE, account) as i64 + db).max(0) as u64;
                            let quote = (get_bal(ASSET_QUOTE, account) as i64 + dq).max(0) as u64;
                            set_bal(ASSET_BASE, account, base);
                            set_bal(ASSET_QUOTE, account, quote);
                        }
                        let volume: u64 =
                            entries.iter().filter(|e| e.side == Side::Buy).map(|e| e.qty as u64).sum();
                        set_storage(b"last_price", &price.to_le_bytes()).ok();
                        let rounds = get_storage(b"rounds").map(|v| le_u64(&v)).unwrap_or(0) + 1;
                        set_storage(b"rounds", &rounds.to_le_bytes()).ok();
                        let cum = get_storage(b"cum_volume").map(|v| le_u64(&v)).unwrap_or(0) + volume;
                        set_storage(b"cum_volume", &cum.to_le_bytes()).ok();
                    }
                }
                _ => {}
            }
        }
        None
    }
}
