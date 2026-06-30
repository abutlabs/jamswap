#![no_std]
#![no_main]

extern crate alloc;

use alloc::vec::Vec;

use jam_pvm_common::accumulate::{accumulate_items, get_storage, set_storage};
use jam_pvm_common::jam_types::*;
use jam_pvm_common::{declare_service, Service};

use match_engine::{clear, resting, wire, Side};

declare_service!(Marmalade);
struct Marmalade;

// payload / work-output tags
const TAG_MATCH: u8 = 0; // a sealed batch of orders to clear
const TAG_DEPOSIT: u8 = 1; // fund a trader's balance (Phase-2 faucet; real custody = Phase 3)

const ASSET_BASE: u8 = 0;
const ASSET_QUOTE: u8 = 1;

fn le_u64(b: &[u8]) -> u64 {
    let mut x = [0u8; 8];
    let n = core::cmp::min(8, b.len());
    x[..n].copy_from_slice(&b[..n]);
    u64::from_le_bytes(x)
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

impl Service for Marmalade {
    /// refine = THE MATCHING ENGINE (+ a passthrough for deposits). A MATCH batch
    /// is cleared (deterministic uniform-price FBA) and resolved to settlement
    /// instructions (per fill: account, side, qty @ clearing price). A DEPOSIT is
    /// echoed for accumulate to apply. Heavy work off the state path; trustless.
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
            TAG_MATCH => {
                let orders = wire::decode_orders(&data[1..]);
                let c = clear(&orders);
                // [TAG][settle_len:u32][settlement][resting book] — the book is the
                // partially/un-filled orders that carry into the next round.
                let settle = wire::encode_settlement(c.price, &orders, &c);
                let book = wire::encode_orders(&resting(&orders, &c));
                let mut out = Vec::with_capacity(5 + settle.len() + book.len());
                out.push(TAG_MATCH);
                out.extend_from_slice(&(settle.len() as u32).to_le_bytes());
                out.extend_from_slice(&settle);
                out.extend_from_slice(&book);
                out.into()
            }
            TAG_DEPOSIT => {
                // echo [TAG_DEPOSIT] ++ account(4) ++ asset(1) ++ amount(8)
                data.into()
            }
            _ => Vec::new().into(),
        }
    }

    /// accumulate = SETTLEMENT. Apply each work result to on-chain state: credit
    /// deposits; for a cleared batch, move base/quote between traders at the
    /// uniform price (buyer pays qty×price quote, receives qty base; seller the
    /// reverse). Also tracks last price, round count, cumulative volume.
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
                TAG_MATCH if out.len() >= 5 => {
                    let settle_len =
                        u32::from_le_bytes([out[1], out[2], out[3], out[4]]) as usize;
                    if out.len() < 5 + settle_len {
                        continue;
                    }
                    let settle = &out[5..5 + settle_len];
                    let book = &out[5 + settle_len..];
                    // persist the resting order book (read by the next round's builder)
                    set_storage(b"book", book).ok();
                    if let Some((price, entries)) = wire::decode_settlement(settle) {
                        let p = price as u64;
                        let mut volume = 0u64;
                        for e in &entries {
                            let q = e.qty as u64;
                            let notional = q.saturating_mul(p);
                            match e.side {
                                Side::Buy => {
                                    // buyer: + base, − quote
                                    set_bal(ASSET_BASE, e.account, get_bal(ASSET_BASE, e.account).saturating_add(q));
                                    set_bal(ASSET_QUOTE, e.account, get_bal(ASSET_QUOTE, e.account).saturating_sub(notional));
                                    volume += q;
                                }
                                Side::Sell => {
                                    // seller: − base, + quote
                                    set_bal(ASSET_BASE, e.account, get_bal(ASSET_BASE, e.account).saturating_sub(q));
                                    set_bal(ASSET_QUOTE, e.account, get_bal(ASSET_QUOTE, e.account).saturating_add(notional));
                                }
                            }
                        }
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
