use jam_types::{AuthTrace, CoreIndex};

/// Declare that this crate is a JAM authorizer characterized by `$auth_impl` and create necessary
/// entry points.
///
/// - `$auth_impl` must implement the [Authorizer] trait.
#[macro_export]
macro_rules! declare_authorizer {
	($auth_impl: ty) => {
		#[polkavm_derive::polkavm_export]
		extern "C" fn is_authorized_ext(ptr: u32, size: u32) -> (u64, u64) {
			$crate::startup::load_protocol_parameters();
			let $crate::jam_types::IsAuthorizedParams { core } = $crate::mem::decode_buf(ptr, size);
			let result = <$auth_impl as $crate::Authorizer>::is_authorized(core);
			let result = result.leak();
			(result.as_ptr() as u64, result.len() as u64)
		}
	};
}

/// The invocation trait for a JAM authorizer.
///
/// The [declare_authorizer] macro requires that its parameter implement this trait.
pub trait Authorizer {
	/// The single entry-point of this PVM module, this determines whether a given Work Package
	/// should be authorized to run on a given core.
	///
	/// - `core_index`: The index of the core on which the Work Package will be executed.
	///
	/// Returns the authorization output, an opaque blob which will be passed into both Refine and
	/// Accumulate for all Work Items in the `package` assigned to `core_index`. If `package` is not
	/// authorized, then this should panic instead.
	fn is_authorized(core_index: CoreIndex) -> AuthTrace;
}
