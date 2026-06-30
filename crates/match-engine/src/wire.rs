//! Binary wire codec for batch payloads and clearing results — shared by the
//! `no_std` JAM service (decode the batch in `refine`, encode the fills) and the
//! off-chain builder/tools (encode a batch, decode the result). Fixed-width
//! little-endian; integer-only; no allocation surprises.
//!
//! Order  (13 bytes): id:u32 ‖ side:u8 (0=buy,1=sell) ‖ price:u32 ‖ qty:u32
//! Result: price:u32 ‖ volume:u64 ‖ n_fills:u32 ‖ n×(id:u32 ‖ qty:u32)

use crate::{Clearing, Fill, Order, Side};
use alloc::vec::Vec;

pub const ORDER_LEN: usize = 13;

pub fn encode_orders(orders: &[Order]) -> Vec<u8> {
    let mut b = Vec::with_capacity(orders.len() * ORDER_LEN);
    for o in orders {
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
            id: u32::from_le_bytes([o[0], o[1], o[2], o[3]]),
            side: if o[4] == 0 { Side::Buy } else { Side::Sell },
            price: u32::from_le_bytes([o[5], o[6], o[7], o[8]]),
            qty: u32::from_le_bytes([o[9], o[10], o[11], o[12]]),
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::clear;

    #[test]
    fn order_roundtrip() {
        let orders = [
            Order { id: 1, side: Side::Buy, price: 100, qty: 10 },
            Order { id: 2, side: Side::Sell, price: 99, qty: 7 },
        ];
        assert_eq!(decode_orders(&encode_orders(&orders)), orders.to_vec());
    }

    #[test]
    fn clearing_roundtrip() {
        let c = clear(&[
            Order { id: 1, side: Side::Buy, price: 100, qty: 10 },
            Order { id: 2, side: Side::Sell, price: 100, qty: 10 },
        ]);
        assert_eq!(decode_clearing(&encode_clearing(&c)), Some(c));
    }
}
