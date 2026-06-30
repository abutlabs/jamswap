//! Marmalade frequent-batch-auction matching engine.
//!
//! A uniform-price sealed-bid double auction (call auction / "fixing"): every
//! order in a batch clears at a single price `p*`, removing the latency race that
//! drives CEX/AMM MEV. This is the heavy compute that runs in JAM's `refine` —
//! integer-only and fully deterministic, so every validator re-executes it
//! byte-identically (the trust model). No floating point, ever.
//!
//! Algorithm (see docs/PLAN.md §3.3):
//!   1. Candidate clearing prices = the set of submitted limit prices.
//!   2. For each price p: aggregate demand D(p) = Σ buy qty with limit ≥ p, and
//!      supply S(p) = Σ sell qty with limit ≤ p. Matched volume = min(D, S).
//!   3. p* maximizes matched volume; tie-break: minimal |D−S| imbalance, then the
//!      lowest such price (a deterministic reference).
//!   4. Fill to volume V on each side by price-time priority (best price first,
//!      then lowest order id) — the marginal order is partially filled; everyone
//!      trades at p*.

#![cfg_attr(not(test), no_std)]

extern crate alloc;
use alloc::vec::Vec;

pub mod wire;

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Side {
    Buy,
    Sell,
}

/// A limit order on an integer tick grid. `qty` is in integer base units.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub struct Order {
    pub id: u32,
    pub side: Side,
    pub price: u32,
    pub qty: u32,
}

/// A (partial or full) fill for one order, at the uniform clearing price.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub struct Fill {
    pub id: u32,
    pub qty: u32,
}

/// The result of clearing a batch: the uniform price, the matched volume, and the
/// per-order fills (only orders that received a non-zero fill are listed).
#[derive(Clone, PartialEq, Eq, Debug)]
pub struct Clearing {
    pub price: u32,
    pub volume: u64,
    pub fills: Vec<Fill>,
}

fn ration(side: &[&Order], volume: u64, out: &mut Vec<Fill>) {
    let mut rem = volume;
    for o in side {
        if rem == 0 {
            break;
        }
        let f = rem.min(o.qty as u64);
        out.push(Fill { id: o.id, qty: f as u32 });
        rem -= f;
    }
}

/// Clear a batch of orders. Pure and deterministic: same input → same output on
/// every node. Returns `volume = 0` with no fills if the book does not cross.
pub fn clear(orders: &[Order]) -> Clearing {
    // candidate clearing prices = the distinct submitted limit prices
    let mut prices: Vec<u32> = orders.iter().map(|o| o.price).collect();
    prices.sort_unstable();
    prices.dedup();

    let demand = |p: u32| -> u64 {
        orders.iter().filter(|o| o.side == Side::Buy && o.price >= p).map(|o| o.qty as u64).sum()
    };
    let supply = |p: u32| -> u64 {
        orders.iter().filter(|o| o.side == Side::Sell && o.price <= p).map(|o| o.qty as u64).sum()
    };

    let mut best_p = 0u32;
    let mut best_v = 0u64;
    let mut best_imbalance = u64::MAX;
    for &p in &prices {
        let d = demand(p);
        let s = supply(p);
        let v = d.min(s);
        let imbalance = d.abs_diff(s);
        // maximize volume; tie-break minimal imbalance; then lowest price (prices
        // ascend, so the first-seen wins → deterministic).
        if v > best_v || (v == best_v && v > 0 && imbalance < best_imbalance) {
            best_v = v;
            best_imbalance = imbalance;
            best_p = p;
        }
    }

    if best_v == 0 {
        return Clearing { price: 0, volume: 0, fills: Vec::new() };
    }
    let p = best_p;
    let v = best_v;

    // buys eligible (limit ≥ p) by price-time priority: highest price, then id.
    let mut buys: Vec<&Order> =
        orders.iter().filter(|o| o.side == Side::Buy && o.price >= p).collect();
    buys.sort_unstable_by(|a, b| b.price.cmp(&a.price).then(a.id.cmp(&b.id)));
    // sells eligible (limit ≤ p): most aggressive (lowest price) first, then id.
    let mut sells: Vec<&Order> =
        orders.iter().filter(|o| o.side == Side::Sell && o.price <= p).collect();
    sells.sort_unstable_by(|a, b| a.price.cmp(&b.price).then(a.id.cmp(&b.id)));

    let mut fills = Vec::new();
    ration(&buys, v, &mut fills);
    ration(&sells, v, &mut fills);
    Clearing { price: p, volume: v, fills }
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;

    fn buy(id: u32, price: u32, qty: u32) -> Order { Order { id, side: Side::Buy, price, qty } }
    fn sell(id: u32, price: u32, qty: u32) -> Order { Order { id, side: Side::Sell, price, qty } }

    #[test]
    fn simple_cross_full_fill() {
        let c = clear(&[buy(1, 100, 10), sell(2, 100, 10)]);
        assert_eq!(c.price, 100);
        assert_eq!(c.volume, 10);
        assert_eq!(c.fills.len(), 2);
    }

    #[test]
    fn no_cross_no_trade() {
        let c = clear(&[buy(1, 90, 10), sell(2, 100, 10)]);
        assert_eq!(c.volume, 0);
        assert!(c.fills.is_empty());
    }

    #[test]
    fn uniform_price_buyer_pays_clearing_not_limit() {
        // buyer bids 105 but clears at 100 (the price that maximizes volume)
        let c = clear(&[buy(1, 105, 5), buy(2, 100, 5), sell(3, 100, 10)]);
        assert_eq!(c.price, 100);
        assert_eq!(c.volume, 10);
    }

    #[test]
    fn marginal_rationing_is_price_time_deterministic() {
        // demand 15 @≥100, supply 10 @100 → V=10; buys rationed: id1 (5) full,
        // id2 (10) gets the remaining 5. (both at price 100 → time priority by id)
        let c = clear(&[buy(1, 100, 5), buy(2, 100, 10), sell(3, 100, 10)]);
        assert_eq!(c.volume, 10);
        let f1 = c.fills.iter().find(|f| f.id == 1).unwrap().qty;
        let f2 = c.fills.iter().find(|f| f.id == 2).unwrap().qty;
        assert_eq!(f1, 5);
        assert_eq!(f2, 5);
    }

    proptest! {
        // value conservation + determinism + per-order bound, over random books.
        #[test]
        fn invariants(orders in prop::collection::vec(
            (any::<bool>(), 1u32..50, 1u32..100), 0..40usize)) {
            let book: Vec<Order> = orders.iter().enumerate().map(|(i, &(b, price, qty))| {
                Order { id: i as u32, side: if b { Side::Buy } else { Side::Sell }, price, qty }
            }).collect();

            let c = clear(&book);
            // determinism: re-clearing yields the identical result
            prop_assert_eq!(clear(&book), c.clone());

            let id_side = |id: u32| book.iter().find(|o| o.id == id).map(|o| o.side).unwrap();
            let id_qty = |id: u32| book.iter().find(|o| o.id == id).map(|o| o.qty).unwrap();
            let buy_filled: u64 = c.fills.iter().filter(|f| id_side(f.id) == Side::Buy).map(|f| f.qty as u64).sum();
            let sell_filled: u64 = c.fills.iter().filter(|f| id_side(f.id) == Side::Sell).map(|f| f.qty as u64).sum();
            // conservation: bought volume == sold volume == cleared volume
            prop_assert_eq!(buy_filled, c.volume);
            prop_assert_eq!(sell_filled, c.volume);
            // no order fills beyond its quantity
            for f in &c.fills { prop_assert!(f.qty <= id_qty(f.id)); }
        }
    }
}
