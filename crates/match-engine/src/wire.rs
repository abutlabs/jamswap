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

// ---- trustless order authentication (verified in refine) -------------------
//
// A NEW public order travels with everything refine needs to verify it statelessly:
// the trader's pubkey and signature over auth::order_msg(...). Refine verifies the
// signature (and that the executed price equals the signed price for limit orders);
// accumulate — which CAN read state — then binds the pubkey to the account's
// registered key, enforces the per-account monotonic seq (replay), and band-checks
// market orders against the on-chain last price. Split verification: expensive
// crypto where gas is abundant, cheap state checks where state lives.

/// Order type / sealing flags carried beside a signed order (they're part of the
/// signed message, so refine can reconstruct exactly what the trader signed).
pub const FLAG_MARKET: u8 = 1;
pub const FLAG_SEALED: u8 = 2;

/// order(17) ‖ flags(1) ‖ signed_price(4) ‖ seq(8) ‖ pubkey(32) ‖ sig(64)
pub const SIGNED_ORDER_LEN: usize = ORDER_LEN + 1 + 4 + 8 + 32 + 64;

#[derive(Clone, PartialEq, Eq, Debug)]
pub struct SignedOrder {
    pub order: Order,
    pub flags: u8,
    pub signed_price: u32,
    pub seq: u64,
    pub pubkey: [u8; 32],
    pub sig: [u8; 64],
}

pub fn encode_signed_orders(orders: &[SignedOrder]) -> Vec<u8> {
    let mut b = Vec::with_capacity(orders.len() * SIGNED_ORDER_LEN);
    for s in orders {
        b.extend_from_slice(&encode_orders(core::slice::from_ref(&s.order)));
        b.push(s.flags);
        b.extend_from_slice(&s.signed_price.to_le_bytes());
        b.extend_from_slice(&s.seq.to_le_bytes());
        b.extend_from_slice(&s.pubkey);
        b.extend_from_slice(&s.sig);
    }
    b
}

pub fn decode_signed_orders(data: &[u8]) -> Vec<SignedOrder> {
    let mut out = Vec::new();
    let n = data.len() / SIGNED_ORDER_LEN;
    for i in 0..n {
        let s = &data[i * SIGNED_ORDER_LEN..(i + 1) * SIGNED_ORDER_LEN];
        let order = match decode_orders(&s[..ORDER_LEN]).into_iter().next() {
            Some(o) => o,
            None => continue,
        };
        let mut pubkey = [0u8; 32];
        pubkey.copy_from_slice(&s[30..62]);
        let mut sig = [0u8; 64];
        sig.copy_from_slice(&s[62..126]);
        out.push(SignedOrder {
            order,
            flags: s[17],
            signed_price: u32::from_le_bytes([s[18], s[19], s[20], s[21]]),
            seq: u64::from_le_bytes([s[22], s[23], s[24], s[25], s[26], s[27], s[28], s[29]]),
            pubkey,
            sig,
        });
    }
    out
}

/// What refine reports to accumulate about each admitted NEW order, so accumulate can
/// finish the verification refine couldn't do statelessly:
/// account(4) ‖ seq(8) ‖ pubkey(32) ‖ flags(1) ‖ price(4).
pub const BINDING_LEN: usize = 4 + 8 + 32 + 1 + 4;

#[derive(Clone, PartialEq, Eq, Debug)]
pub struct Binding {
    pub account: u32,
    pub seq: u64,
    pub pubkey: [u8; 32],
    pub flags: u8,
    pub price: u32,
}

pub fn encode_bindings(bindings: &[Binding]) -> Vec<u8> {
    let mut b = Vec::with_capacity(bindings.len() * BINDING_LEN);
    for x in bindings {
        b.extend_from_slice(&x.account.to_le_bytes());
        b.extend_from_slice(&x.seq.to_le_bytes());
        b.extend_from_slice(&x.pubkey);
        b.push(x.flags);
        b.extend_from_slice(&x.price.to_le_bytes());
    }
    b
}

pub fn decode_bindings(data: &[u8]) -> Vec<Binding> {
    let mut out = Vec::new();
    let n = data.len() / BINDING_LEN;
    for i in 0..n {
        let s = &data[i * BINDING_LEN..(i + 1) * BINDING_LEN];
        let mut pubkey = [0u8; 32];
        pubkey.copy_from_slice(&s[12..44]);
        out.push(Binding {
            account: u32::from_le_bytes([s[0], s[1], s[2], s[3]]),
            seq: u64::from_le_bytes([s[4], s[5], s[6], s[7], s[8], s[9], s[10], s[11]]),
            pubkey,
            flags: s[44],
            price: u32::from_le_bytes([s[45], s[46], s[47], s[48]]),
        });
    }
    out
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
/// is +qty base / −(qty·price/scale) quote, a sell the reverse. Each filled order also
/// pays a **flat fee** `fee_flat` (atomic units of the BASE asset), routed to `treasury`
/// — a cost-based fee that approximates the per-order execution + state cost, rather than
/// a size-proportional trading fee. Collecting it in the base asset means a DOT/USDC
/// market pays fees in DOT and a JAMKB/* market pays fees directly in JAMKB (which funds
/// the service's JAMKB state-rent reserve). The buyer receives `qty − fee` base, the
/// seller delivers `qty + fee` base, and the treasury accrues `fee` per filled order; the
/// fee is capped at the fill (`min(fee, qty)`) so a buyer's base can never go negative.
///
/// `scale` is the fixed-point price scale: prices and quantities are integer *atomic*
/// units (display × scale), so a fractional display price like 1.1 is the integer
/// 11000 at scale 10000. Quote notional therefore de-scales by one factor of `scale`
/// (`qty·price/scale`). Buyers round the notional **up**, sellers **down**, so the
/// rounding residual is always ≥ 0 and flows to the treasury — it can never overdraw
/// it. When qty is a multiple of `scale` (the production invariant: whole-unit
/// quantities are submitted as `qty·scale`) the division is exact and ceil == floor,
/// so every fill is penny-perfect. `scale = 1` reproduces the un-scaled integer market.
///
/// Aggregated per account. **Invariant:** Σ base deltas == 0 and Σ quote deltas == 0
/// *including the treasury* — a batch moves value (incl. fees + rounding dust) between
/// accounts, it never creates or destroys it (settlement's safety property; property-tested).
// i128 throughout: qty·price can exceed i64 for large orders (a real overflow that
// wraps silently in release) — i128 holds any u32·u32 with room to spare.
pub fn settle_deltas(price: u32, entries: &[SettleEntry], fee_flat: u64, treasury: u32, scale: u32) -> Vec<(u32, i128, i128)> {
    let p = price as i128;
    let s = scale.max(1) as i128;
    let f = fee_flat as i128;
    let mut out: Vec<(u32, i128, i128)> = Vec::new();
    let add = |out: &mut Vec<(u32, i128, i128)>, acct: u32, db: i128, dq: i128| {
        match out.iter_mut().find(|(a, _, _)| *a == acct) {
            Some(slot) => { slot.1 += db; slot.2 += dq; }
            None => out.push((acct, db, dq)),
        }
    };
    for e in entries {
        let q = e.qty as i128;
        let gross = q * p;
        // buyers round up, sellers round down → quote residual ≥ 0 (to treasury); exact
        // when gross is a multiple of scale (whole-unit quantities), so ceil == floor.
        let notional = match e.side {
            Side::Buy => (gross + s - 1) / s,
            Side::Sell => gross / s,
        };
        // flat cost-based fee in the BASE asset, capped at the fill so a buyer's base
        // can never go negative on a tiny fill.
        let fee = f.min(q);
        let (db, dq) = match e.side {
            Side::Buy => (q - fee, -notional),      // buyer receives qty − fee base, pays notional
            Side::Sell => (-(q + fee), notional),   // seller delivers qty + fee base, receives notional
        };
        add(&mut out, e.account, db, dq);
        if fee > 0 {
            add(&mut out, treasury, fee, 0);        // treasury accrues the flat fee in BASE
        }
    }
    // Treasury also absorbs any quote rounding residual so Σ quote deltas == 0 exactly.
    // (Σ base deltas == 0 already: each order's base fee is added to the treasury and
    // removed from that order, and matched buy qty == sell qty.)
    let resid: i128 = out.iter().map(|(_, _, dq)| *dq).sum();
    if resid != 0 {
        add(&mut out, treasury, 0, -resid);
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
    fn signed_order_and_binding_roundtrip() {
        let s = SignedOrder {
            order: Order { account: 7, id: 42, side: Side::Buy, price: 100, qty: 10 },
            flags: FLAG_MARKET,
            signed_price: 0,
            seq: 1_720_000_000_000,
            pubkey: [0xAB; 32],
            sig: [0xCD; 64],
        };
        let enc = encode_signed_orders(core::slice::from_ref(&s));
        assert_eq!(enc.len(), SIGNED_ORDER_LEN);
        assert_eq!(decode_signed_orders(&enc), alloc::vec![s]);

        let b = Binding { account: 7, seq: 9, pubkey: [0xEF; 32], flags: 0, price: 100 };
        let enc = encode_bindings(core::slice::from_ref(&b));
        assert_eq!(enc.len(), BINDING_LEN);
        assert_eq!(decode_bindings(&enc), alloc::vec![b]);
    }

    #[test]
    fn clearing_roundtrip() {
        let c = clear(&[
            Order { account: 1, id: 1, side: Side::Buy, price: 100, qty: 10 },
            Order { account: 2, id: 2, side: Side::Sell, price: 100, qty: 10 },
        ]);
        assert_eq!(decode_clearing(&encode_clearing(&c)), Some(c));
    }

    #[test]
    fn scaled_settlement_is_exact_for_whole_quantities() {
        // scale 10000; a fractional display price 1.1 = 11000 atomic. Whole-unit
        // quantities are atomic multiples of scale (5 units = 50000), so the notional
        // divides exactly: 50000 · 11000 / 10000 = 55000 atomic = 5.5 display. Buyer
        // and seller settle at the identical notional — no residual beyond the (zero) fee.
        let scale = 10_000u32;
        let entries = [
            SettleEntry { account: 1, side: Side::Buy, qty: 5 * scale },
            SettleEntry { account: 2, side: Side::Sell, qty: 5 * scale },
        ];
        let d = settle_deltas(11_000, &entries, 0, u32::MAX, scale);
        let buyer = d.iter().find(|(a, _, _)| *a == 1).unwrap();
        let seller = d.iter().find(|(a, _, _)| *a == 2).unwrap();
        assert_eq!(buyer.1, (5 * scale) as i128);   // +5 units base (atomic)
        assert_eq!(buyer.2, -55_000);               // −5.5 quote (atomic)
        assert_eq!(seller.2, 55_000);               // +5.5 quote (atomic) — exact, no dust
        assert!(d.iter().find(|(a, _, _)| *a == u32::MAX).is_none()); // no residual/fee
        // conservation
        assert_eq!(d.iter().map(|x| x.1).sum::<i128>(), 0);
        assert_eq!(d.iter().map(|x| x.2).sum::<i128>(), 0);
    }

    #[test]
    fn scaled_settlement_conserves_with_rounding_dust() {
        // qty NOT a multiple of scale → the ceil/floor split leaves a quote residual, and
        // a flat base fee is charged per order; the treasury absorbs both so value is still
        // conserved exactly (Σ base == 0 and Σ quote == 0, treasury never overdrawn).
        let entries = [
            SettleEntry { account: 1, side: Side::Buy, qty: 3 },
            SettleEntry { account: 2, side: Side::Sell, qty: 1 },
            SettleEntry { account: 3, side: Side::Sell, qty: 2 },
        ];
        let d = settle_deltas(7, &entries, 1, u32::MAX, 10_000);   // flat fee 1 (atomic base) per order
        assert_eq!(d.iter().map(|x| x.1).sum::<i128>(), 0);
        assert_eq!(d.iter().map(|x| x.2).sum::<i128>(), 0);
        // treasury only ever receives (never overdrawn), in both base and quote
        if let Some(t) = d.iter().find(|(a, _, _)| *a == u32::MAX) {
            assert!(t.1 >= 0);
            assert!(t.2 >= 0);
        }
    }

    #[test]
    fn flat_fee_charged_in_base_and_conserves() {
        // buy 10 vs sell 10 @ price 1.0 (scale 10000), flat fee 300 atomic base (0.03 units)
        // per filled order. Buyer receives 10−0.03 base, seller delivers 10+0.03, treasury
        // accrues 0.03 + 0.03 = 0.06 base. No quote fee. Conservation holds in both assets.
        let scale = 10_000u32;
        let entries = [
            SettleEntry { account: 1, side: Side::Buy, qty: 10 * scale },
            SettleEntry { account: 2, side: Side::Sell, qty: 10 * scale },
        ];
        let d = settle_deltas(1 * scale, &entries, 300, u32::MAX, scale);
        let buyer = d.iter().find(|(a, _, _)| *a == 1).unwrap();
        let seller = d.iter().find(|(a, _, _)| *a == 2).unwrap();
        let treasury = d.iter().find(|(a, _, _)| *a == u32::MAX).unwrap();
        assert_eq!(buyer.1, 10 * scale as i128 - 300);      // +9.97 base
        assert_eq!(seller.1, -(10 * scale as i128 + 300));  // −10.03 base
        assert_eq!(treasury.1, 600);                        // +0.06 base fee
        assert_eq!(treasury.2, 0);                          // no quote fee, no rounding dust
        assert_eq!(d.iter().map(|x| x.1).sum::<i128>(), 0); // Σ base conserved
        assert_eq!(d.iter().map(|x| x.2).sum::<i128>(), 0); // Σ quote conserved
    }
}
