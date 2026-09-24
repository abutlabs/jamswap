use jam_types::{CoreIndex, Hash, ServiceId, Slot, WorkOutput, WorkPackageHash, WorkPayload};

/// Declare that this crate is a JAM service characterized by `$service_impl` and create necessary
/// entry points.
///
/// - `$service_impl` must implement the [Service] trait.
#[macro_export]
macro_rules! declare_service {
	($service_impl: ty) => {
		#[polkavm_derive::polkavm_export]
		extern "C" fn refine_ext(ptr: u32, size: u32) -> (u64, u64) {
			$crate::startup::load_protocol_parameters();
			let $crate::jam_types::RefineParams {
				core_index,
				item_index,
				service_id,
				payload,
				package_hash,
			} = $crate::mem::decode_buf(ptr, size);
			let result = <$service_impl as $crate::Service>::refine(
				core_index,
				item_index as _,
				service_id,
				payload,
				package_hash,
			);
			let result = result.leak();
			(result.as_ptr() as u64, result.len() as u64)
		}
		#[polkavm_derive::polkavm_export]
		extern "C" fn accumulate_ext(ptr: u32, size: u32) -> (u64, u64) {
			$crate::startup::load_protocol_parameters();
			let $crate::jam_types::AccumulateParams { slot, service_id, item_count } =
				$crate::mem::decode_buf(ptr, size);
			let maybe_hash =
				<$service_impl as $crate::Service>::accumulate(slot, service_id, item_count as _);
			if let Some(hash) = maybe_hash {
				let hash = ::alloc::boxed::Box::leak(::alloc::boxed::Box::new(hash));
				(hash.as_ptr() as u64, hash.len() as u64)
			} else {
				(0, 0)
			}
		}
	};
}

/// The invocation trait for a JAM service.
///
/// The [declare_service] macro requires that its parameter implement this trait.
pub trait Service {
	/// The Refine entry-point, used in-core on a single Work Item.
	///
	/// - `core_index`: The index of the core processing the work item.
	/// - `item_index`: The index of the current work item.
	/// - `service_id`: The index of the service being refined.
	/// - `payload`: The payload data to process.
	/// - `package_hash`: The hash of the Work Package.
	///
	/// Returns the Work Output, which will be passed into [Self::accumulate] in the on-chain
	/// (stateful) context.
	fn refine(
		core_index: CoreIndex,
		item_index: usize,
		service_id: ServiceId,
		payload: WorkPayload,
		package_hash: WorkPackageHash,
	) -> WorkOutput;

	/// The Accumulate entry-point, used on-chain on one or more Work Item Outputs or Transfers,
	/// or possibly none in the case of an always-accumulate service.
	///
	/// - `slot`: The current time slot.
	/// - `service_id`: The service ID being accumulated.
	/// - `item_count`: The number of items to accumulate. May be Work Outputs or Transfers.
	fn accumulate(slot: Slot, service_id: ServiceId, item_count: usize) -> Option<Hash>;
}
