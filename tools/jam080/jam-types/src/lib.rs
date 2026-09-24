//! JAM types used within the PVM instances (service code and authorizer code).

#![cfg_attr(not(feature = "std"), no_std)]

extern crate alloc;

#[doc(hidden)]
#[cfg(not(feature = "std"))]
pub use core::{
	clone::Clone,
	cmp::{Eq, PartialEq},
	fmt,
	fmt::Debug,
	option::Option,
	prelude::rust_2021::derive,
	result::Result,
};

#[doc(hidden)]
pub use codec::{Decode, Encode, MaxEncodedLen};

#[doc(hidden)]
#[cfg(not(feature = "std"))]
pub use alloc::vec::Vec;
#[doc(hidden)]
pub use bounded_collections::BoundedVec;
#[doc(hidden)]
#[cfg(feature = "std")]
pub use std::vec::Vec;

#[doc(hidden)]
#[cfg(feature = "serde")]
pub use {base64, serde};

// To allow use of ::jam_types in macros
extern crate self as jam_types;

mod fixed_vec;
mod simple;
mod simple_result_code;
mod types;
mod vec_map;
mod vec_set;

mod opaque;
#[cfg(feature = "bytes")]
mod segment_bytes;

#[cfg(feature = "bytes")]
pub use segment_bytes::SegmentBytes;

pub use fixed_vec::{BoundedMap, FixedVec};
#[cfg(feature = "bytes")]
pub use simple::AnyBytes;
pub use simple::{
	auth_queue_len, availability_timeout, basic_piece_len, basic_piece_points, block_gas_limit,
	core_count, deposit_per_account, deposit_per_byte, deposit_per_item, epoch_period,
	epoch_tail_start, max_accumulate_gas, max_authorizer_code_size, max_dependencies,
	max_export_segments, max_exports, max_extrinsics, max_import_segments, max_imports, max_input,
	max_is_authorized_gas, max_lookup_anchor_age, max_refine_gas, max_report_elective_data,
	max_service_code_size, max_tickets_per_block, max_work_items, min_turnaround_period,
	pieces_per_segment, recent_block_count, rotation_period, segment_len, segment_slice_len,
	slot_period_sec, tickets_attempts_number, val_count, AuthConfig, AuthQueue, AuthQueueLen,
	AuthTrace, AuthWindow, Authorization, AuthorizerHash, Balance, CodeHash, CoreCount, CoreIndex,
	EpochPeriod, ExtrinsicHash, Hash, HeaderHash, MaxDependencies, MaxExportSegments,
	MaxExtrinsics, MaxImportSegments, MaxImports, MaxTicketsPerBlock, MaxWorkItems, Memo,
	OpaqueBandersnatchPublic, OpaqueBlsPublic, OpaqueEd25519Public, OpaqueValidatorMetadata,
	PayloadHash, ProtocolParameters, RecentBlockCount, Segment, SegmentHash, SegmentLen,
	SegmentSliceLen, SegmentTreeRoot, ServiceId, SignedGas, Slot, TicketAttempt,
	TicketsAttemptsNumber, ToAny, ValCount, ValIndex, WorkOutput, WorkPackageHash, WorkPayload,
	GP_VERSION, JAM_COMMON_ERA, MEMO_LEN, PAGE_SIZE, POINT_LEN, PROVEN_PER_SEGMENT, SEGMENT_LEN,
	VALS_PER_CORE,
};
pub use vec_map::{MapLike, VecMap};
pub use vec_set::{SetLike, VecSet};

pub use types::{
	AccumulateItem, Authorizer, ExtrinsicSpec, FetchKind, ImportSpec, NoWrap, OpaqueValKeyset,
	OpaqueValKeysets, PageMode, PageOperation, RefineContext, RootIdentifier, ServiceInfo,
	TransferRecord, WorkItem, WorkItemImportsVec, WorkItemRecord, WorkItemSummary, WorkPackage,
	Wrap, WrappedWorkPackage,
};

#[doc(hidden)]
pub use simple::{
	AccumulateRootHash, AnyHash, AnyVec, Code, DoubleBalance, DoubleGas, MerkleNodeHash,
	MmrPeakHash, StateRootHash, UnsignedGas, ValSuperMajority, WorkReportHash,
	MAX_PREIMAGE_BLOB_LEN, MAX_PREIMAGE_LEN,
};

// Internal use: here and `jam-node` crates.
#[doc(hidden)]
pub mod hex;

// Internal use: `jam-node` and `jam-pvm-builder` crates.
#[doc(hidden)]
pub use simple_result_code::{InvokeOutcomeCode, SimpleResult, SimpleResultCode, LOWEST_ERROR};

// Internal use: `jam-node` and/or `jam-pvm-common` crates.
// TODO: Anything only used in one or the other should be moved to the respective crate.
#[doc(hidden)]
pub use types::{
	AccumulateParams, IsAuthorizedParams, RefineLoad, RefineParams, RefineParamsRef, WorkDigest,
	WorkError, WorkItems,
};

mod pvm;
pub use pvm::*;

pub use bounded_collections::Get;

pub trait ToAtomic {
	type Atomic: atomic_traits::Atomic;
}
macro_rules! impl_to_atomic {
	($t:ty, $atomic:ty) => {
		impl ToAtomic for $t {
			type Atomic = $atomic;
		}
	};
}
impl_to_atomic!(u8, core::sync::atomic::AtomicU8);
impl_to_atomic!(u16, core::sync::atomic::AtomicU16);
impl_to_atomic!(u32, core::sync::atomic::AtomicU32);
impl_to_atomic!(u64, core::sync::atomic::AtomicU64);
impl_to_atomic!(usize, core::sync::atomic::AtomicUsize);

#[macro_export]
macro_rules! chain_params {
	(atomic $atom:ident ; $init:expr ; $t:ty) => {
		static $atom: <$t as $crate::ToAtomic>::Atomic =
			<$t as $crate::ToAtomic>::Atomic::new($init);
	};
	(basic $atom:ident ; $init:expr ; $fn_vis:vis , $f:ident ; $t:tt ; $st:tt ; $(#[$($meta:meta)*])*) => {
		chain_params! { atomic $atom; $init; $st }
		$(#[$($meta)*])* $fn_vis fn $f() -> $t {
			$atom.load(core::sync::atomic::Ordering::Relaxed) as $t
		}
	};
	(get $struct_vis:vis , $struct_name:ident ; $f:ident ; $(#[$($meta:meta)*])*) => {
		$(#[$($meta)*])*
		#[derive(Copy, Clone, Eq, PartialEq, Default, Debug)]
		$struct_vis struct $struct_name;
		impl $crate::Get<u16> for $struct_name { fn get() -> u16 { $f() as u16 } }
		impl $crate::Get<u32> for $struct_name { fn get() -> u32 { $f() as u32 } }
		impl $crate::Get<u64> for $struct_name { fn get() -> u64 { $f() as u64 } }
		impl $crate::Get<u128> for $struct_name { fn get() -> u128 { $f() as u128 } }
		impl $crate::Get<usize> for $struct_name { fn get() -> usize { $f() as usize } }
	};
	(
		$(#[$($meta:meta)*])* static $atom:ident : _ = _($init:expr);
		$fn_vis:vis fn $f:ident() -> $t:tt;
		$struct_vis:vis struct $struct_name:ident;
		impl Get<_> for _ {}
		$($rest:tt)*
	) => {
		chain_params! { basic $atom ; $init ; $fn_vis , $f ; $t ; $t ; $(#[$($meta)*])* }
		chain_params! { get $struct_vis , $struct_name ; $f ; $(#[$($meta)*])* }
		chain_params! { $($rest)* }
	};
	(
		$(#[$($meta:meta)*])* static $atom:ident : $st:tt = _($init:expr);
		$fn_vis:vis fn $f:ident() -> $t:tt;
		$struct_vis:vis struct $struct_name:ident;
		impl Get<_> for _ {}
		$($rest:tt)*
	) => {
		chain_params! { basic $atom ; $init ; $fn_vis , $f ; $t ; $st ; $(#[$($meta)*])* }
		chain_params! { get $struct_vis , $struct_name ; $f ; $(#[$($meta)*])* }
		chain_params! { $($rest)* }
	};
	(
		$(#[$($meta:meta)*])* static $atom:ident : _ = _($init:expr);
		$fn_vis:vis fn $f:ident() -> $t:tt;
		$($rest:tt)*
	) => {
		chain_params! { basic $atom ; $init ; $fn_vis , $f ; $t ; $t ; $(#[$($meta)*])* }
		chain_params! { $($rest)* }
	};
	(
		$(#[$($meta:meta)*])* static $atom:ident : $st:tt = _($init:expr);
		$fn_vis:vis fn $f:ident() -> $t:tt;
		$($rest:tt)*
	) => {
		chain_params! { basic $atom ; $init ; $fn_vis , $f ; $t ; $st ; $(#[$($meta)*])* }
		chain_params! { $($rest)* }
	};
	(
		$(#[$($meta:meta)*])* $fn_vis:vis fn $f:ident() -> $t:tt { $fx:expr }
		$struct_vis:vis struct $struct_name:ident;
		impl Get<_> for _ {}
		$($rest:tt)*
	) => {
		$(#[$($meta)*])* $fn_vis fn $f() -> $t { $fx }
		chain_params! { get $struct_vis , $struct_name ; $f ; $(#[$($meta)*])* }
		chain_params! { $($rest)* }
	};
	(
		$(#[$($meta:meta)*])* $const_vis:vis const $const_name:ident: _ = $cx:expr;
		$fn_vis:vis fn $f:ident() -> $t:tt;
		$struct_vis:vis struct $struct_name:ident;
		impl Get<_> for _ {}
		$($rest:tt)*
	) => {
		$(#[$($meta)*])* $const_vis const $const_name: $t = $cx;
		$(#[$($meta)*])* $fn_vis fn $f() -> $t { $const_name }
		chain_params! { get $struct_vis , $struct_name ; $f ; $(#[$($meta)*])* }
		chain_params! { $($rest)* }
	};
	() => {}
}
