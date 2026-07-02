//! Scenario tests — sequences of orders through the matching engine, checking the
//! engine's behaviour vs expected. Complements the in-crate unit + property tests
//! (`src/lib.rs`) by exercising the *continuous order book*: clear a batch, rest the
//! remainder, add new orders, clear again — the multi-round behaviour a live market
//! actually sees, and the level at which the sealed-order carry-forward bug lived.
//!
//! Run with `cargo test --release` (or `cargo test --test scenarios`).

use match_engine::{clear, resting, Order, Side};

fn buy(id: u32, price: u32, qty: u32) -> Order {
    Order { account: id, id, side: Side::Buy, price, qty }
}
fn sell(id: u32, price: u32, qty: u32) -> Order {
    Order { account: id, id, side: Side::Sell, price, qty }
}

/// Total filled quantity for an order id in a clearing.
fn filled(c: &match_engine::Clearing, id: u32) -> u64 {
    c.fills.iter().filter(|f| f.id == id).map(|f| f.qty as u64).sum()
}

// ---- single-batch scenarios ------------------------------------------------

#[test]
fn crossing_buy_and_sell_at_same_price_fully_fill() {
    // The exact shape of the user's report: a sell and a buy at the same price, in
    // the SAME batch, must cross and both fully fill.
    let c = clear(&[sell(1, 10_000, 10), buy(2, 10_000, 10)]);
    assert_eq!(c.price, 10_000);
    assert_eq!(c.volume, 10);
    assert_eq!(filled(&c, 1), 10);
    assert_eq!(filled(&c, 2), 10);
}

#[test]
fn many_sells_one_big_buy_ration_by_time_priority() {
    // Four sells of 10 @ $1 and one buy of 40 @ $1 clear the whole 40; each sell
    // fills fully. (The four-sealed-sells + buys scenario, at the engine level.)
    let book = [sell(1, 10_000, 10), sell(2, 10_000, 10), sell(3, 10_000, 10),
                sell(4, 10_000, 10), buy(5, 10_000, 40)];
    let c = clear(&book);
    assert_eq!(c.volume, 40);
    for id in 1..=4 { assert_eq!(filled(&c, id), 10, "sell {id} fully fills"); }
    assert_eq!(filled(&c, 5), 40);
    assert!(resting(&book, &c).is_empty(), "nothing rests — the batch fully cleared");
}

#[test]
fn wide_spread_does_not_cross() {
    // buys well below sells: no trade, everything rests.
    let book = [buy(1, 9_000, 10), sell(2, 11_000, 10)];
    let c = clear(&book);
    assert_eq!(c.volume, 0);
    assert!(c.fills.is_empty());
    assert_eq!(resting(&book, &c).len(), 2, "both orders rest, unfilled");
}

// ---- multi-round continuous-book scenarios ---------------------------------

/// Clear a batch, then return the resting book that carries into the next round.
fn round(book: &[Order]) -> (match_engine::Clearing, Vec<Order>) {
    let c = clear(book);
    let rest = resting(book, &c);
    (c, rest)
}

#[test]
fn sell_rests_then_a_later_buy_crosses_it() {
    // Round 1: a lone sell @ $1 — nothing to trade with, it rests.
    let (c1, book1) = round(&[sell(1, 10_000, 10)]);
    assert_eq!(c1.volume, 0);
    assert_eq!(book1.len(), 1, "the sell rests into round 2");

    // Round 2: a buy @ $1 arrives and crosses the resting sell.
    let mut book2 = book1.clone();
    book2.push(buy(2, 10_000, 10));
    let (c2, rest2) = round(&book2);
    assert_eq!(c2.volume, 10, "the later buy fills the resting sell");
    assert_eq!(c2.price, 10_000);
    assert!(rest2.is_empty(), "both fully filled — book empties");
}

#[test]
fn partial_fill_rests_and_completes_over_three_rounds() {
    // Round 1: sell 10, buy 4 -> 4 trade, 6 of the sell rests.
    let (c1, book1) = round(&[sell(1, 10_000, 10), buy(2, 10_000, 4)]);
    assert_eq!(c1.volume, 4);
    assert_eq!(book1.len(), 1);
    assert_eq!(book1[0].id, 1);
    assert_eq!(book1[0].qty, 6, "6 of the sell rests");

    // Round 2: another buy of 4 -> 4 more trade, 2 rest.
    let mut book2 = book1.clone();
    book2.push(buy(3, 10_000, 4));
    let (c2, book2r) = round(&book2);
    assert_eq!(c2.volume, 4);
    assert_eq!(book2r[0].qty, 2, "2 of the original sell still rests");

    // Round 3: a buy of 5 -> only 2 left to trade; the sell clears, 3 of the buy rests.
    let mut book3 = book2r.clone();
    book3.push(buy(4, 10_000, 5));
    let (c3, book3r) = round(&book3);
    assert_eq!(c3.volume, 2, "only the sell's last 2 units remain to trade");
    assert_eq!(book3r.len(), 1);
    assert_eq!(book3r[0].id, 4);
    assert_eq!(book3r[0].qty, 3, "the buy's unfilled 3 rests");
}

#[test]
fn resting_book_is_stable_when_no_new_crossing_order() {
    // A resting sell stays put across a round that adds only a non-crossing buy.
    let (_, book1) = round(&[sell(1, 12_000, 10)]);
    let mut book2 = book1.clone();
    book2.push(buy(2, 9_000, 10)); // far below the ask — no cross
    let (c2, book2r) = round(&book2);
    assert_eq!(c2.volume, 0);
    assert_eq!(book2r.len(), 2, "both rest; the book grows but nothing trades");
}
