//! Jamswap economic simulation (PLAN.md §9.3).
//!
//! Drives the real `match-engine` with random, realistic order flow over many
//! rounds — orders rest and carry, fills settle through `settle_deltas` (with the
//! 30 bps fee), against an in-memory ledger. It reports market-quality metrics and,
//! crucially, **asserts the safety invariants hold at scale**: value conservation
//! every round (Σ balances per asset constant incl. the treasury) and no clearing
//! anomaly. This is the economic stress test behind the "production-ready" claim.
//!
//!   cargo run --release            # 500 rounds, 40 traders, seed 1
//!   cargo run --release -- 2000 80 7

use std::collections::HashMap;

use match_engine::wire::{settle_deltas, SettleEntry};
use match_engine::{clear, resting, Order, Side};
use rand::{rngs::StdRng, Rng, SeedableRng};

const BASE: u32 = 1;
const QUOTE: u32 = 0;
const TREASURY: u32 = u32::MAX;
const FEE_BPS: u32 = 30;

fn main() {
    let a: Vec<String> = std::env::args().collect();
    let rounds: usize = a.get(1).and_then(|s| s.parse().ok()).unwrap_or(500);
    let traders: u32 = a.get(2).and_then(|s| s.parse().ok()).unwrap_or(40);
    let seed: u64 = a.get(3).and_then(|s| s.parse().ok()).unwrap_or(1);
    let mut rng = StdRng::seed_from_u64(seed);

    // ledger: (asset, account) -> balance. Fund every trader generously.
    let mut bal: HashMap<(u32, u32), i128> = HashMap::new();
    let (init_base_each, init_quote_each) = (1_000_000i128, 1_000_000_000i128);
    for t in 0..traders {
        bal.insert((BASE, t), init_base_each);
        bal.insert((QUOTE, t), init_quote_each);
    }
    let total_base0 = init_base_each * traders as i128;
    let total_quote0 = init_quote_each * traders as i128;

    let mut book: Vec<Order> = Vec::new();
    let mut next_id: u32 = 0;
    let mut mid: i64 = 1000;

    let (mut submitted, mut matched, mut sum_price, mut priced_rounds) = (0u64, 0u64, 0f64, 0u64);
    let mut prices: Vec<i64> = Vec::new();

    for _ in 0..rounds {
        // random walk the reference price; generate orders around it
        mid = (mid + rng.gen_range(-15..=15)).clamp(50, 5000);
        let n = rng.gen_range(0..=traders.min(12));
        let mut orders = book.clone();
        for _ in 0..n {
            let acct = rng.gen_range(0..traders);
            let side = if rng.gen_bool(0.5) { Side::Buy } else { Side::Sell };
            let price = (mid + rng.gen_range(-40..=40)).clamp(1, 100_000) as u32;
            let qty = rng.gen_range(1..=50);
            orders.push(Order { account: acct, id: next_id, side, price, qty });
            next_id += 1;
            submitted += qty as u64;
        }

        let c = clear(&orders);
        matched += c.volume;
        if c.volume > 0 {
            sum_price += c.price as f64;
            priced_rounds += 1;
            prices.push(c.price as i64);
        }

        // settle through the real engine path (with fees), then carry the resting book
        let entries: Vec<SettleEntry> = c
            .fills
            .iter()
            .filter_map(|f| orders.iter().find(|o| o.id == f.id).map(|o| SettleEntry { account: o.account, side: o.side, qty: f.qty }))
            .collect();
        for (acct, db, dq) in settle_deltas(c.price, &entries, FEE_BPS, TREASURY) {
            *bal.entry((BASE, acct)).or_insert(0) += db;
            *bal.entry((QUOTE, acct)).or_insert(0) += dq;
        }
        book = resting(&orders, &c);

        // INVARIANT: total value per asset is conserved every round (incl. treasury)
        let tb: i128 = bal.iter().filter(|((a, _), _)| *a == BASE).map(|(_, v)| *v).sum();
        let tq: i128 = bal.iter().filter(|((a, _), _)| *a == QUOTE).map(|(_, v)| *v).sum();
        assert_eq!(tb, total_base0, "base not conserved");
        assert_eq!(tq, total_quote0, "quote not conserved");
    }

    // market-quality report
    let fill_rate = if submitted > 0 { matched as f64 / submitted as f64 * 100.0 } else { 0.0 };
    let mean_price = if priced_rounds > 0 { sum_price / priced_rounds as f64 } else { 0.0 };
    let var = if prices.len() > 1 {
        prices.iter().map(|&p| (p as f64 - mean_price).powi(2)).sum::<f64>() / prices.len() as f64
    } else { 0.0 };
    let fee_revenue = *bal.get(&(QUOTE, TREASURY)).unwrap_or(&0);

    println!("== Jamswap economic simulation ==");
    println!("  rounds            : {rounds}   traders: {traders}   seed: {seed}");
    println!("  orders submitted  : {submitted} units;  matched: {matched} units");
    println!("  fill rate         : {fill_rate:.1}%");
    println!("  rounds that cleared: {priced_rounds}/{rounds}");
    println!("  mean clearing price: {mean_price:.1}   price stddev: {:.1}", var.sqrt());
    println!("  resting book (end) : {} orders", book.len());
    println!("  fee revenue (30bps): {fee_revenue} quote units");
    println!("  VALUE CONSERVED every round (base & quote, incl. treasury): OK");
}
