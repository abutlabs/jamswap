#![no_std]
#![no_main]

extern crate alloc;

use jam_pvm_common::accumulate::{accumulate_items, get_storage, set_storage};
use jam_pvm_common::jam_types::*;
use jam_pvm_common::{declare_service, Service};

use match_engine::{clear, wire};

declare_service!(Marmalade);
struct Marmalade;

fn le_u64(b: &[u8]) -> u64 {
    let mut x = [0u8; 8];
    let n = core::cmp::min(8, b.len());
    x[..n].copy_from_slice(&b[..n]);
    u64::from_le_bytes(x)
}

impl Service for Marmalade {
    /// refine = THE MATCHING ENGINE. Decode a sealed batch of orders from the
    /// work payload, run the frequent-batch-auction uniform-price clearing
    /// (integer-only, deterministic — every validator re-executes identically),
    /// and emit the clearing result (price ‖ volume ‖ fills).
    fn refine(
        _core_index: CoreIndex,
        _item_index: usize,
        _service_id: ServiceId,
        payload: WorkPayload,
        _package_hash: WorkPackageHash,
    ) -> WorkOutput {
        let orders = wire::decode_orders(&payload.take());
        let clearing = clear(&orders);
        wire::encode_clearing(&clearing).into()
    }

    /// accumulate = SETTLEMENT (settlement-lite for the MVP): read the matching
    /// result and commit the round's outcome to on-chain service state — last
    /// clearing price/volume, round count, cumulative matched volume. (Full
    /// balance-ledger settlement of individual fills is Phase 2.)
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
            let c = match wire::decode_clearing(&out) {
                Some(c) => c,
                None => continue,
            };
            set_storage(b"last_price", &c.price.to_le_bytes()).ok();
            set_storage(b"last_volume", &c.volume.to_le_bytes()).ok();
            let rounds = get_storage(b"rounds").map(|v| le_u64(&v)).unwrap_or(0) + 1;
            set_storage(b"rounds", &rounds.to_le_bytes()).ok();
            let cum = get_storage(b"cum_volume").map(|v| le_u64(&v)).unwrap_or(0) + c.volume;
            set_storage(b"cum_volume", &cum.to_le_bytes()).ok();
        }
        None
    }
}
