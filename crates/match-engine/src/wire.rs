//! Binary wire codec for batch payloads and clearing results — shared by the
//! `no_std` JAM service (decode the batch in `refine`, encode the fills) and the
//! off-chain builder/tools (encode a batch, decode the result). Fixed-width
//! little-endian; integer-only; no allocation surprises.
//!
//! Order (17 bytes): account:u32 ‖ id:u32 ‖ side:u8 (0=buy,1=sell) ‖ price:u32 ‖ qty:u32
//! Result: price:u32 ‖ volume:u64 ‖ n_fills:u32 ‖ n×(id:u32 ‖ qty:u32)

use crate::{Clearing, Fill, Order, Side};
use alloc::vec::Vec;

pub const ORDER_LEN: usize = 17;

pub fn encode_orders(orders: &[Order]) -> Vec<u8> {
    let mut b = Vec::with_capacity(orders.len() * ORDER_LEN);
    for o in orders {
        b.extend_from_slice(&o.account.to_le_bytes());
        b.extend_from_slice(&o.id.to_le_bytes());
        b.push(if o.side == Side::Buy { 0 } else { 1 });
        b.extend_from_slice(&o.price.to_le_bytes());
        b.extend_from_slice(&o.qty.to_le_bytes());
    }
    b
}

pub fn decode_orders(data: &[u8]) -> Vec<Order> {
    let mut out = Vec::new();
    let n = data.len() / ORDER_LEN;
    for i in 0..n {
        let o = &data[i * ORDER_LEN..i * ORDER_LEN + ORDER_LEN];
        out.push(Order {
            account: u32::from_le_bytes([o[0], o[1], o[2], o[3]]),
            id: u32::from_le_bytes([o[4], o[5], o[6], o[7]]),
            side: if o[8] == 0 { Side::Buy } else { Side::Sell },
            price: u32::from_le_bytes([o[9], o[10], o[11], o[12]]),
            qty: u32::from_le_bytes([o[13], o[14], o[15], o[16]]),
        });
    }
    out
}

pub fn encode_clearing(c: &Clearing) -> Vec<u8> {
    let mut b = Vec::with_capacity(16 + c.fills.len() * 8);
    b.extend_from_slice(&c.price.to_le_bytes());
    b.extend_from_slice(&c.volume.to_le_bytes());
    b.extend_from_slice(&(c.fills.len() as u32).to_le_bytes());
    for f in &c.fills {
        b.extend_from_slice(&f.id.to_le_bytes());
        b.extend_from_slice(&f.qty.to_le_bytes());
    }
    b
}

pub fn decode_clearing(data: &[u8]) -> Option<Clearing> {
    if data.len() < 16 {
        return None;
    }
    let price = u32::from_le_bytes([data[0], data[1], data[2], data[3]]);
    let mut vb = [0u8; 8];
    vb.copy_from_slice(&data[4..12]);
    let volume = u64::from_le_bytes(vb);
    let n = u32::from_le_bytes([data[12], data[13], data[14], data[15]]) as usize;
    if data.len() < 16 + n * 8 {
        return None;
    }
    let mut fills = Vec::with_capacity(n);
    for i in 0..n {
        let o = &data[16 + i * 8..16 + i * 8 + 8];
        fills.push(Fill {
            id: u32::from_le_bytes([o[0], o[1], o[2], o[3]]),
            qty: u32::from_le_bytes([o[4], o[5], o[6], o[7]]),
        });
    }
    Some(Clearing { price, volume, fills })
}

/// One fill resolved to its trader + side, at the uniform clearing price — what
/// settlement (`accumulate`) needs to debit/credit balances.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub struct SettleEntry {
    pub account: u32,
    pub side: Side,
    pub qty: u32,
}

/// Encode the settlement instructions: price ‖ n ‖ n×(account ‖ side ‖ qty).
/// Resolves each fill (by order id) back to its trader and side.
pub fn encode_settlement(price: u32, orders: &[Order], c: &Clearing) -> Vec<u8> {
    let mut b = Vec::with_capacity(8 + c.fills.len() * 9);
    b.extend_from_slice(&price.to_le_bytes());
    b.extend_from_slice(&(c.fills.len() as u32).to_le_bytes());
    for f in &c.fills {
        if let Some(o) = orders.iter().find(|o| o.id == f.id) {
            b.extend_from_slice(&o.account.to_le_bytes());
            b.push(if o.side == Side::Buy { 0 } else { 1 });
            b.extend_from_slice(&f.qty.to_le_bytes());
        }
    }
    b
}

pub fn decode_settlement(data: &[u8]) -> Option<(u32, Vec<SettleEntry>)> {
    if data.len() < 8 {
        return None;
    }
    let price = u32::from_le_bytes([data[0], data[1], data[2], data[3]]);
    let n = u32::from_le_bytes([data[4], data[5], data[6], data[7]]) as usize;
    if data.len() < 8 + n * 9 {
        return None;
    }
    let mut out = Vec::with_capacity(n);
    for i in 0..n {
        let e = &data[8 + i * 9..8 + i * 9 + 9];
        out.push(SettleEntry {
            account: u32::from_le_bytes([e[0], e[1], e[2], e[3]]),
            side: if e[4] == 0 { Side::Buy } else { Side::Sell },
            qty: u32::from_le_bytes([e[5], e[6], e[7], e[8]]),
        });
    }
    Some((price, out))
}

/// Per-account balance deltas from a cleared batch, at the uniform price: a buy
/// is +qty base / −qty·price quote, a sell the reverse. Each side also pays a flat
/// fee of `fee_bps` basis points on its quote notional, routed to `treasury`.
/// Aggregated per account. **Invariant:** Σ base deltas == 0 and Σ quote deltas == 0
/// *including the treasury* — a batch moves value (incl. fees) between accounts, it
/// never creates or destroys it (settlement's safety property; property-tested).
// i128 throughout: qty·price can exceed i64 for large orders (a real overflow that
// wraps silently in release) — i128 holds any u32·u32 with room to spare.
pub fn settle_deltas(price: u32, entries: &[SettleEntry], fee_bps: u32, treasury: u32) -> Vec<(u32, i128, i128)> {
    let p = price as i128;
    let mut out: Vec<(u32, i128, i128)> = Vec::new();
    let add = |out: &mut Vec<(u32, i128, i128)>, acct: u32, db: i128, dq: i128| {
        match out.iter_mut().find(|(a, _, _)| *a == acct) {
            Some(slot) => { slot.1 += db; slot.2 += dq; }
            None => out.push((acct, db, dq)),
        }
    };
    let mut fee_total = 0i128;
    for e in entries {
        let q = e.qty as i128;
        let notional = q * p;
        let fee = notional * fee_bps as i128 / 10_000;
        fee_total += fee;
        let (db, dq) = match e.side {
            Side::Buy => (q, -(notional + fee)),   // buyer pays notional + fee
            Side::Sell => (-q, notional - fee),    // seller receives notional − fee
        };
        add(&mut out, e.account, db, dq);
    }
    if fee_total != 0 {
        add(&mut out, treasury, 0, fee_total);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::clear;

    #[test]
    fn order_roundtrip() {
        let orders = [
            Order { account: 7, id: 1, side: Side::Buy, price: 100, qty: 10 },
            Order { account: 9, id: 2, side: Side::Sell, price: 99, qty: 7 },
        ];
        assert_eq!(decode_orders(&encode_orders(&orders)), orders.to_vec());
    }

    #[test]
    fn clearing_roundtrip() {
        let c = clear(&[
            Order { account: 1, id: 1, side: Side::Buy, price: 100, qty: 10 },
            Order { account: 2, id: 2, side: Side::Sell, price: 100, qty: 10 },
        ]);
        assert_eq!(decode_clearing(&encode_clearing(&c)), Some(c));
    }
}
