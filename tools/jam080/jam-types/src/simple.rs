// MODIFIED by abutlabs (jamswap tools/jam080, 2026-09-24) for GP 0.8.0 from the
// crates.io jam-types 0.1.28 release (Apache-2.0, Parity Technologies). See ../../README.md.
#[allow(unused_imports)]
use super::Authorizer;
use crate::{chain_params, opaque, FixedVec};
use bounded_collections::{BoundedVec, Get};
use codec::{Decode, Encode, MaxEncodedLen};
use core::sync::atomic::Ordering::Relaxed;

/// Version of the Gray Paper implemented by this and dependent crates.
pub const GP_VERSION: &str = "0.7.2";

/// Beginning of the Jam "Common Era" (1200 UTC on January 1, 2025),
/// as seconds after the Unix epoch, excluding leap seconds.
pub const JAM_COMMON_ERA: u64 = 1_735_732_800;

/// Length of a transfer memo in bytes.
pub const MEMO_LEN: usize = 128;

/// Maximum length of the preimage in the encoded form.
#[doc(hidden)]
// TODO @ivan This is temporary value. Replace with the one from the GP when it's updated.
pub const MAX_PREIMAGE_LEN: usize = 4 * 1024 * 1024;

/// Maximum length of the preimage blob.
///
/// Equals `MAX_PREIMAGE_LEN` minus the overhead.
// TODO @ivan Unhide when the GP is updated.
#[doc(hidden)]
pub const MAX_PREIMAGE_BLOB_LEN: usize = MAX_PREIMAGE_LEN - 8;

/// PolkaVM page size in bytes.
pub const PAGE_SIZE: u32 = 4096;

/// The maximum number of levels in the implicit tree derived from the segment hashes in a
/// proof-page.
pub const SUFFIX_SKIP_LEN: usize = 6;

/// The maximum number of segments whose justification can be included in a single segment-sized
/// proof-page. Basically just the highest power of 2 which when multiplied by 32 (the size of a
/// hash) leaves enough room from 4104 (the size of a segment) to fit a partial Merkle proof of
/// 6 branches (6 * 32 = 192) plus the overhead for storing the actual number of hashes (should be
/// one byte for `PROVEN_PER_SEGMENT < 128`). It turns out that this is 2^6 = 64.
///
/// See `max_prefix_proof_len` for the actual formula.
pub const PROVEN_PER_SEGMENT: usize = 1 << SUFFIX_SKIP_LEN;

/// Index for an attempt at producing a ticket for the right to produce a block.
pub type TicketAttempt = u8;

const DEFAULT_PARAMS: ProtocolParameters = ProtocolParameters::full();

chain_params! {
	/// Total number of validators in the JAM.
	static VAL_COUNT: _ = _(DEFAULT_PARAMS.val_count);
	pub fn val_count() -> ValIndex;
	pub struct ValCount; impl Get<_> for _ {}

	/// Number of bytes in a basic EC piece.
	static BASIC_PIECE_LEN: _ = _(DEFAULT_PARAMS.basic_piece_len as _);
	pub fn basic_piece_len() -> usize;

	/// Number EC pieces in a segment.
	static SEGMENT_PIECE_COUNT: _ = _(DEFAULT_PARAMS.segment_piece_count as _);
	pub fn segment_piece_count() -> usize;

	/// Number of authorizations in a queue allocated to a core.
	static AUTH_QUEUE_LEN: _ = _(DEFAULT_PARAMS.auth_queue_len as _);
	pub fn auth_queue_len() -> usize;
	pub struct AuthQueueLen; impl Get<_> for _ {}

	/// Minimum period in blocks between going from becoming `Available` to `Zombie`, and then
	/// again from `Zombie` to non-existent.
	///
	/// This ensures firstly that any data added and referenced in a Work Report's lookup anchor
	/// block will remain on-chain right up until the latest possible time a dispute might
	/// conclude. Secondly, it ensure that we only need to record one "flip-flop" of the data's
	/// availability in order to be able to determine whether it's available or not at any block
	/// within this period.
	static MIN_TURNAROUND_PERIOD: _ = _(DEFAULT_PARAMS.min_turnaround_period);
	pub fn min_turnaround_period() -> Slot;

	/// Maximum number of Work Items in a Work Package.
	static MAX_WORK_ITEMS: _ = _(DEFAULT_PARAMS.max_work_items as _);
	pub fn max_work_items() -> usize;
	pub struct MaxWorkItems; impl Get<_> for _ {}

	/// Maximum number of imports in a Work Package.
	static MAX_IMPORTS: _ = _(DEFAULT_PARAMS.max_imports);
	pub fn max_imports() -> u32;
	pub struct MaxImports; impl Get<_> for _ {}

	/// Maximum number of exports in a Work Package.
	static MAX_EXPORTS: _ = _(DEFAULT_PARAMS.max_exports);
	pub fn max_exports() -> u32;

	/// Maximum number of extrinsics in a Work Package.
	static MAX_EXTRINSICS: _ = _(DEFAULT_PARAMS.max_extrinsics as _);
	pub fn max_extrinsics() -> u32;
	pub struct MaxExtrinsics; impl Get<_> for _ {}

	/// Maximum number of dependencies (total of prerequisites and SR lookup entries).
	static MAX_DEPENDENCIES: _ = _(DEFAULT_PARAMS.max_dependencies as _);
	pub fn max_dependencies() -> usize;
	pub struct MaxDependencies; impl Get<_> for _ {}

	/// Maximum size of a Work Package together with all extrinsic data and imported segments.
	static MAX_INPUT: _ = _(DEFAULT_PARAMS.max_input);
	pub fn max_input() -> u32;
	pub struct MaxInput; impl Get<_> for _ {}

	/// Returns the number of bytes in a segment slice.
	pub fn segment_slice_len() -> usize {
		segment_len() / basic_piece_points()
	}
	pub struct SegmentSliceLen; impl Get<_> for _ {}

	/// Number of bytes in a segment. This is fixed.
	pub const SEGMENT_LEN: _ = (DEFAULT_PARAMS.basic_piece_len * DEFAULT_PARAMS.segment_piece_count) as usize;
	pub fn segment_len() -> usize;
	pub struct SegmentLen; impl Get<_> for _ {}

	/// Number of seconds in a time slot.
	static SLOT_PERIOD_SEC : _ = _(DEFAULT_PARAMS.slot_period_sec as _);
	pub fn slot_period_sec() -> u64;

	/// The rotation period, defined in number of slots.
	static ROTATION_PERIOD: _ = _(DEFAULT_PARAMS.rotation_period as _);
	pub fn rotation_period() -> Slot;

	/// The epoch period, defined in number of slots.
	static EPOCH_PERIOD: _ = _(DEFAULT_PARAMS.epoch_period);
	pub fn epoch_period() -> Slot;
	pub struct EpochPeriod; impl Get<_> for _ {}

	/// The length of the contest, defined in number of slots.
	static EPOCH_TAIL_START: _ = _(DEFAULT_PARAMS.epoch_tail_start);
	pub fn epoch_tail_start() -> Slot;

	/// Max tickets allowed to be embedded in each block extrinsic.
	static MAX_TICKETS_PER_BLOCK: _ = _(DEFAULT_PARAMS.max_tickets_per_block as _);
	pub fn max_tickets_per_block() -> usize;
	pub struct MaxTicketsPerBlock; impl Get<_> for _ {}

	/// Maximum number of tickets which each validator may create.
	///
	/// Influences the anonymity of block producers. As all published tickets have a public
	/// attempt number less than `attempts_number` if two tickets share an attempt number
	/// then they must belong to two different validators, which reduces anonymity late as
	/// we approach the epoch tail.
	static TICKETS_ATTEMPTS_NUMBER: _ = _(DEFAULT_PARAMS.tickets_attempts_number as _);
	pub fn tickets_attempts_number() -> TicketAttempt;
	pub struct TicketsAttemptsNumber; impl Get<_> for _ {}

	static DEPOSIT_PER_ACCOUNT: _ = _(DEFAULT_PARAMS.deposit_per_account);
	pub fn deposit_per_account() -> Balance;

	static DEPOSIT_PER_ITEM: _ = _(DEFAULT_PARAMS.deposit_per_item);
	pub fn deposit_per_item() -> Balance;

	static DEPOSIT_PER_BYTE: _ = _(DEFAULT_PARAMS.deposit_per_byte);
	pub fn deposit_per_byte() -> Balance;

	/// The maximum gas which may be used in a single block. At present this is just the product of
	/// the maximum gas per work-report and the number of cores. In the future it should probably be
	/// more to account for the possibility of always-accumulate services.
	static BLOCK_GAS_LIMIT: _ = _(DEFAULT_PARAMS.block_gas_limit);
	pub fn block_gas_limit() -> UnsignedGas;

	/// Maximum gas which may be used to Authorize a single work-package.
	static MAX_IS_AUTHORIZED_GAS: _ = _(DEFAULT_PARAMS.max_is_authorized_gas);
	pub fn max_is_authorized_gas() -> UnsignedGas;

	/// Maximum gas which may be used to Refine a single work-report.
	static MAX_REFINE_GAS: _ = _(DEFAULT_PARAMS.max_refine_gas);
	pub fn max_refine_gas() -> UnsignedGas;

	/// Maximum gas which may be used to Accumulate a single work-report.
	static MAX_ACCUMULATE_GAS: _ = _(DEFAULT_PARAMS.max_accumulate_gas);
	pub fn max_accumulate_gas() -> UnsignedGas;

	static MAX_SERVICE_CODE_SIZE: _ = _(DEFAULT_PARAMS.max_service_code_size as _);
	pub fn max_service_code_size() -> usize;

	static MAX_AUTHORIZER_CODE_SIZE: _ = _(DEFAULT_PARAMS.max_authorizer_code_size as _);
	pub fn max_authorizer_code_size() -> usize;

	/// The number of blocks which are kept in the recent block cache. We use a `u8` to index into
	/// this and therefore it may be no larger than 255.
	static RECENT_BLOCK_COUNT: _ = _(DEFAULT_PARAMS.recent_block_count as _);
	pub fn recent_block_count() -> Slot;
	pub struct RecentBlockCount; impl Get<_> for _ {}

	/// The number of validators on each core.
	pub const VALS_PER_CORE: _ = 3;
	pub fn _vals_per_core() -> usize;
	pub struct _ValsPerCore; impl Get<_> for _ {}

	/// Number of cores in the JAM.
	pub fn core_count() -> CoreIndex {
		(val_count() as usize / VALS_PER_CORE) as CoreIndex
	}
	pub struct CoreCount; impl Get<_> for _ {}

	/// Maximum number of DA segments that a Work Package can produce.
	pub fn max_export_segments() -> u32 {
		max_exports() + max_exports().div_ceil(PROVEN_PER_SEGMENT as u32)
	}
	pub struct MaxExportSegments; impl Get<_> for _ {}

	/// Maximum number of DA segments that a Work Package can require as imports. This is twice the
	/// maximum number of imports because each import may require a proof segment.
	pub fn max_import_segments() -> u32 { max_imports() * 2 }
	pub struct MaxImportSegments; impl Get<_> for _ {}

	/// The period in timeslots after which reported but unavailable work may be replaced.
	static AVAILABILITY_TIMEOUT: _ = _(DEFAULT_PARAMS.availability_timeout as _);
	pub fn availability_timeout() -> Slot;

	/// Number of items in the authorization window.
	static AUTH_WINDOW: _ = _(DEFAULT_PARAMS.auth_window as _);
	pub fn auth_window() -> usize;
	pub struct AuthWindow; impl Get<_> for _ {}

	/// Maximum age, in blocks, that the lookup anchor may be, taken from the regular anchor.
	static MAX_LOOKUP_ANCHOR_AGE: _ = _(DEFAULT_PARAMS.max_lookup_anchor_age);
	pub fn max_lookup_anchor_age() -> Slot;

	/// The maximum total size of all unbounded blobs in a work-report.
	/// Maximum age, in blocks, that the lookup anchor may be, taken from the regular anchor.
	static MAX_REPORT_ELECTIVE_DATA: _ = _(DEFAULT_PARAMS.max_report_elective_data);
	pub fn max_report_elective_data() -> u32;
	pub struct MaxReportElectiveData; impl Get<_> for _ {}
}

/// Number of points in a piece.
pub fn basic_piece_points() -> usize {
	basic_piece_len() / POINT_LEN
}

/// Returns the number of pieces in a segment.
pub fn pieces_per_segment() -> usize {
	SEGMENT_LEN / basic_piece_len()
}

/// Baseline parameters for the JAM protocol.
#[derive(Copy, Clone, Eq, PartialEq, Debug, Encode, Decode, MaxEncodedLen)]
#[cfg_attr(feature = "serde", derive(serde::Serialize, serde::Deserialize))]
pub struct ProtocolParameters {
	/// (Bi) The additional deposit required for each preimage or storage item in an account.
	pub deposit_per_item: Balance,
	/// (Bl) The additional deposit required for each byte of each storage item in an account and
	/// preimage of an account.
	pub deposit_per_byte: Balance,
	/// (Bs) The base deposit required to retain an account.
	pub deposit_per_account: Balance,
	/// (C) Number of JAM cores.
	pub core_count: CoreIndex,
	/// (D) The period in timeslots after which an unreferenced preimage may be expunged.
	pub min_turnaround_period: Slot,
	/// (E) The epoch period, defined in number of slots.
	pub epoch_period: Slot,
	/// (Ga) Maximum gas which may be used to Accumulate a single work-report.
	pub max_accumulate_gas: UnsignedGas,
	/// (Gi) Maximum gas which may be used to Authorize a single work-package.
	pub max_is_authorized_gas: UnsignedGas,
	/// (Gr) Maximum gas which may be used to Refine a single work-report.
	pub max_refine_gas: UnsignedGas,
	/// (Gt) Maximum gas which can be processed in a single block.
	pub block_gas_limit: UnsignedGas,
	/// (H) The number of blocks which are kept in the recent block cache.
	pub recent_block_count: u16,
	/// (I) Maximum number of Work Items in a Work Package.
	pub max_work_items: u16,
	/// (J) Maximum number of dependencies (total of prerequisites and SR lookup entries).
	pub max_dependencies: u16,
	/// (K) Max tickets allowed to be embedded in each block extrinsic.
	pub max_tickets_per_block: u16,
	/// (L) Maximum age, in blocks, that the lookup anchor may be, taken from the regular anchor.
	pub max_lookup_anchor_age: Slot,
	/// (N) The number of distinct tickets which may be created and submitted by each validator on
	/// each epoch.
	// GP 0.8.0: no longer a protocol parameter (not in fetch(0) / JIP-1 0.8.0); kept
	// as a field for API compatibility, never encoded or decoded.
	#[codec(skip)]
	pub tickets_attempts_number: u16,
	/// (O) Number of items in the authorization window.
	pub auth_window: u16,
	/// (P) The period for each JAM time slot, defined in seconds.
	pub slot_period_sec: u16,
	/// (Q) Number of authorizations in a queue allocated to a core.
	pub auth_queue_len: u16,
	/// (R) The rotation period, defined in number of slots.
	pub rotation_period: u16,
	/// (T) Maximum number of extrinsics in a Work Package.
	pub max_extrinsics: u16,
	/// (U) The period in timeslots after which reported but unavailable work may be replaced.
	pub availability_timeout: u16,
	/// (V) Total number of validators. Must by divisible by guarantor group size (3).
	// GP 0.8.0: no longer a protocol parameter (not in fetch(0) / JIP-1 0.8.0); kept
	// as a field for API compatibility, never encoded or decoded.
	#[codec(skip)]
	pub val_count: ValIndex,
	/// (Wa) The maximum size of authorizer code (used by Is-Authorized procedure).
	pub max_authorizer_code_size: u32,
	/// (Wb) Maximum size of a Work Package together with all extrinsic data and imported segments.
	pub max_input: u32,
	/// (Wc) The maximum size of service code (used by Refine/Accumulate/OnTransfer procedures).
	pub max_service_code_size: u32,
	/// (We) Number of octets in a basic piece. Must be even and divide into segment length
	/// (4,104).
	// GP 0.8.0: no longer a protocol parameter (not in fetch(0) / JIP-1 0.8.0); kept
	// as a field for API compatibility, never encoded or decoded.
	#[codec(skip)]
	pub basic_piece_len: u32,
	/// (Wm) Maximum number of imports in a Work Package.
	pub max_imports: u32,
	/// (Wp) The number of erasure-coded pieces in a segment.
	// GP 0.8.0: no longer a protocol parameter (not in fetch(0) / JIP-1 0.8.0); kept
	// as a field for API compatibility, never encoded or decoded.
	#[codec(skip)]
	pub segment_piece_count: u32,
	/// (Wr) The maximum total size of all unbounded blobs in a work-report, in octets.
	pub max_report_elective_data: u32,
	/// (Wt) The size of a transfer memo in octets. The size of a transfer memo in octets.
	pub transfer_memo_size: u32,
	/// (Wx) Maximum number of exports in a Work Package.
	pub max_exports: u32,
	/// (Y) The length of the contest, defined in number of slots.
	pub epoch_tail_start: Slot,
}

impl ProtocolParameters {
	pub const fn full() -> Self {
		Self {
			val_count: 1023,
			core_count: 341,
			basic_piece_len: 684,
			auth_queue_len: 80,
			min_turnaround_period: 19_200,
			max_work_items: 16,
			max_imports: 3072,
			max_exports: 3072,
			max_extrinsics: 128,
			max_dependencies: 8,
			max_input: 13_791_360,
			slot_period_sec: 6,
			epoch_period: 600,
			epoch_tail_start: 500,
			rotation_period: 10,
			block_gas_limit: 3_500_000_000,
			recent_block_count: 8,
			max_tickets_per_block: 16,
			tickets_attempts_number: 2,
			deposit_per_account: 100,
			deposit_per_item: 10,
			deposit_per_byte: 1,
			max_is_authorized_gas: 50_000_000,
			max_refine_gas: 5_000_000_000,
			max_accumulate_gas: 10_000_000,
			max_service_code_size: 4_000_000,
			max_authorizer_code_size: 64_000,
			availability_timeout: 5,
			auth_window: 8,
			max_lookup_anchor_age: 24 * 600,
			max_report_elective_data: 48 * 1024,
			segment_piece_count: 6,
			transfer_memo_size: 128,
		}
	}
	pub const fn tiny() -> Self {
		let mut params = Self::full();
		params.val_count = 6;
		params.core_count = 2;
		params.basic_piece_len = 4;
		params.epoch_period = 12;
		params.epoch_tail_start = 10;
		params.rotation_period = 4;
		params.block_gas_limit = 20_000_000;
		params.max_tickets_per_block = 3;
		params.tickets_attempts_number = 3;
		params.max_refine_gas = 1_000_000_000;
		params.segment_piece_count = 1026;
		// Remove unreferenced preimages after 32 slots.
		// The short retention period simplifies testing with test vectors and fuzzing.
		params.min_turnaround_period = 32;
		// Same reasoning as `min_turnaround_period`
		params.max_lookup_anchor_age = 24;
		params
	}

	pub fn get() -> Self {
		Self {
			deposit_per_item: deposit_per_item(),
			deposit_per_byte: deposit_per_byte(),
			deposit_per_account: deposit_per_account(),
			core_count: core_count(),
			min_turnaround_period: min_turnaround_period(),
			epoch_period: epoch_period(),
			max_accumulate_gas: max_accumulate_gas(),
			max_is_authorized_gas: max_is_authorized_gas(),
			max_refine_gas: max_refine_gas(),
			block_gas_limit: block_gas_limit(),
			recent_block_count: recent_block_count() as _,
			max_work_items: max_work_items() as _,
			max_dependencies: max_dependencies() as _,
			max_tickets_per_block: max_tickets_per_block() as _,
			max_lookup_anchor_age: max_lookup_anchor_age(),
			tickets_attempts_number: tickets_attempts_number() as _,
			auth_window: auth_window() as _,
			slot_period_sec: slot_period_sec() as _,
			auth_queue_len: auth_queue_len() as _,
			rotation_period: rotation_period() as _,
			max_extrinsics: max_extrinsics() as _,
			availability_timeout: availability_timeout() as _,
			val_count: val_count(),
			max_authorizer_code_size: max_authorizer_code_size() as _,
			max_input: max_input(),
			max_service_code_size: max_service_code_size() as _,
			basic_piece_len: basic_piece_len() as _,
			max_imports: max_imports() as _,
			segment_piece_count: segment_piece_count() as _,
			max_report_elective_data: max_report_elective_data(),
			transfer_memo_size: MEMO_LEN as _,
			max_exports: max_exports(),
			epoch_tail_start: epoch_tail_start(),
		}
	}
	pub fn validate(&self) -> Result<(), &'static str> {
		// GP 0.8.0: basic_piece_len / val_count are absent from the wire (decode as 0);
		// validate them only when a caller set them explicitly.
		if self.basic_piece_len != 0 && !self.basic_piece_len.is_multiple_of(2) {
			return Err("`basic_piece_len` is not even")
		}
		if self.basic_piece_len != 0 && !SEGMENT_LEN.is_multiple_of(self.basic_piece_len as usize) {
			return Err("`basic_piece_len` does not divide into `SEGMENT_LEN` (4,104)")
		}
		if !self.epoch_period.is_multiple_of(self.rotation_period as Slot) {
			return Err("`rotation_period` does not divide into `epoch_period`")
		}
		if self.epoch_tail_start >= self.epoch_period {
			return Err("`epoch_tail_start` must be less than `epoch_period`")
		}
		if self.val_count != 0 && !(self.val_count as usize).is_multiple_of(VALS_PER_CORE) {
			return Err("`val_count` does not divide by `VALS_PER_CORE` (3)")
		}
		if self.val_count != 0 && self.val_count != self.core_count * VALS_PER_CORE as u16 {
			return Err("`val_count != core_count * VALS_PER_CORE` (3)")
		}
		if self.transfer_memo_size != MEMO_LEN as u32 {
			return Err("`transfer_memo_size` must be equal tor `MEMO_LEN` (128)")
		}
		if recent_block_count() > 255 {
			return Err("`recent_block_count` may be no larger than `BlockIndex::MAX` (255)")
		}
		Ok(())
	}
	pub fn apply(self) -> Result<(), &'static str> {
		self.validate()?;
		DEPOSIT_PER_ITEM.store(self.deposit_per_item, Relaxed);
		DEPOSIT_PER_BYTE.store(self.deposit_per_byte, Relaxed);
		DEPOSIT_PER_ACCOUNT.store(self.deposit_per_account, Relaxed);
		MIN_TURNAROUND_PERIOD.store(self.min_turnaround_period, Relaxed);
		EPOCH_PERIOD.store(self.epoch_period, Relaxed);
		MAX_ACCUMULATE_GAS.store(self.max_accumulate_gas, Relaxed);
		MAX_IS_AUTHORIZED_GAS.store(self.max_is_authorized_gas, Relaxed);
		MAX_REFINE_GAS.store(self.max_refine_gas, Relaxed);
		BLOCK_GAS_LIMIT.store(self.block_gas_limit, Relaxed);
		RECENT_BLOCK_COUNT.store(self.recent_block_count as _, Relaxed);
		MAX_WORK_ITEMS.store(self.max_work_items as _, Relaxed);
		MAX_DEPENDENCIES.store(self.max_dependencies as _, Relaxed);
		MAX_TICKETS_PER_BLOCK.store(self.max_tickets_per_block as _, Relaxed);
		MAX_LOOKUP_ANCHOR_AGE.store(self.max_lookup_anchor_age as _, Relaxed);
		if self.tickets_attempts_number != 0 {
			TICKETS_ATTEMPTS_NUMBER.store(self.tickets_attempts_number as _, Relaxed);
		}
		AUTH_WINDOW.store(self.auth_window as _, Relaxed);
		SLOT_PERIOD_SEC.store(self.slot_period_sec as _, Relaxed);
		AUTH_QUEUE_LEN.store(self.auth_queue_len as _, Relaxed);
		ROTATION_PERIOD.store(self.rotation_period as _, Relaxed);
		MAX_EXTRINSICS.store(self.max_extrinsics as _, Relaxed);
		AVAILABILITY_TIMEOUT.store(self.availability_timeout as _, Relaxed);
		if self.val_count != 0 {
			VAL_COUNT.store(self.val_count, Relaxed);
		}
		MAX_AUTHORIZER_CODE_SIZE.store(self.max_authorizer_code_size as _, Relaxed);
		MAX_INPUT.store(self.max_input, Relaxed);
		MAX_SERVICE_CODE_SIZE.store(self.max_service_code_size as _, Relaxed);
		if self.basic_piece_len != 0 {
			BASIC_PIECE_LEN.store(self.basic_piece_len as _, Relaxed);
		}
		MAX_IMPORTS.store(self.max_imports, Relaxed);
		if self.segment_piece_count != 0 {
			SEGMENT_PIECE_COUNT.store(self.segment_piece_count as _, Relaxed);
		}
		MAX_REPORT_ELECTIVE_DATA.store(self.max_report_elective_data, Relaxed);
		MAX_EXPORTS.store(self.max_exports, Relaxed);
		EPOCH_TAIL_START.store(self.epoch_tail_start, Relaxed);
		Ok(())
	}
}

/// Number of bytes in an erasure-coding point.
pub const POINT_LEN: usize = 2;

/// Validators super-majority.
#[doc(hidden)]
#[derive(Copy, Clone, Eq, PartialEq, Default, Debug)]
pub struct ValSuperMajority;
impl Get<u32> for ValSuperMajority {
	fn get() -> u32 {
		val_count() as u32 / 3 * 2 + 1
	}
}

/// Type that represents a time slot of six seconds.
///
/// This can be either in a relative sense or as a period which has elapsed from the Polkadot
/// Common Era, beginning 1200 UTC, 1 January 2025.
pub type Slot = u32;
/// Type to represent the index of a validator.
pub type ValIndex = u16;
/// Type to represent the index of a compute core.
pub type CoreIndex = u16;
/// Type to represent the index of a service.
pub type ServiceId = u32;
/// Type to represent a balance.
pub type Balance = u64;
/// Type which is double the length of Balance, for non-overflowing multiplies.
pub type DoubleBalance = u128;
/// Type to represent some gas which may be below zero. This is used primarily for the `invoke`
/// hostcall API which must be able to return a negative gas amount in case of a gas overrun.
pub type SignedGas = i64;
/// Type to represent some gas which must be at least zero.
pub type UnsignedGas = u64;
/// Type which is double the length of Gas, for non-overflowing multiplies.
pub type DoubleGas = u128;

/// A basic 256-bit data value.
///
/// This should generally not be used directly in the rich data types, but instead one of the
/// rich opaque hash types to avoid accidental misuse and provide pretty-print facilities.
pub type Hash = [u8; 32];

opaque! {
	/// Hash of an encoded block header.
	pub struct HeaderHash(pub [u8; 32]);

	/// Hash of PVM program code.
	pub struct CodeHash(pub [u8; 32]);

	/// Hash of an encoded Work Package.
	pub struct WorkPackageHash(pub [u8; 32]);

	/// Hash of an encoded Work Report.
	pub struct WorkReportHash(pub [u8; 32]);

	/// Hash of a Work Item's [WorkPayload].
	pub struct PayloadHash(pub [u8; 32]);

	/// Hash of the JAM state root.
	pub struct StateRootHash(pub [u8; 32]);

	/// Hash of an MMR peak.
	pub struct MmrPeakHash(pub [u8; 32]);

	/// Hash of an accumulation tree root node.
	pub struct AccumulateRootHash(pub [u8; 32]);

	/// Hash of a piece of extrinsic data.
	pub struct ExtrinsicHash(pub [u8; 32]);

	/// Hash of an encoded [Authorizer] value.
	pub struct AuthorizerHash(pub [u8; 32]);

	/// Hash of a segment tree root node.
	pub struct SegmentTreeRoot(pub [u8; 32]);

	/// Hash of a [Segment] value.
	pub struct SegmentHash(pub [u8; 32]);

	/// Hash of a Merkle tree node.
	pub struct MerkleNodeHash(pub [u8; 32]);

	/// Non usage-specific hash.
	///
	/// This can be useful for pretty-printing [type@Hash] values.
	pub struct AnyHash(pub [u8; 32]);

	/// Transfer memo data, included with balance transfers between services.
	pub struct Memo(pub [u8; MEMO_LEN]);

	/// Data constituting the Authorization Token in a Work Package.
	pub struct Authorization(pub Vec<u8>);

	/// PVM Program code.
	pub struct Code(pub Vec<u8>);

	/// Payload data defining a Work Item.
	pub struct WorkPayload(pub Vec<u8>);

	/// Authorization parameter.
	pub struct AuthConfig(pub Vec<u8>);

	/// Non usage-specific data.
	///
	/// This can be useful for pretty-printing `Vec<u8>` values.
	pub struct AnyVec(pub Vec<u8>);

	/// Output data of Refinement operation, passed into Accumulation.
	pub struct WorkOutput(pub Vec<u8>);

	/// Output data of Is Authorized operation, passed into both Refinement and Accumulation.
	pub struct AuthTrace(pub Vec<u8>);

	/// Plain-old-data struct of the same length as an encoded Ed25519 public key.
	///
	/// This has no cryptographic functionality or dependencies.
	pub struct OpaqueEd25519Public(pub [u8; 32]);

	/// Plain-old-data struct of the same length as an encoded Bandersnatch public key.
	///
	/// This has no cryptographic functionality or dependencies.
	pub struct OpaqueBandersnatchPublic(pub [u8; 32]);

	/// Plain-old-data struct of the same length as an encoded BLS public key.
	///
	/// This has no cryptographic functionality or dependencies.
	pub struct OpaqueBlsPublic(pub [u8; 144]);

	/// Additional information on a validator, opaque to the actual usage.
	pub struct OpaqueValidatorMetadata(pub [u8; 128]);
}

#[cfg(feature = "bytes")]
opaque! {
	/// Non usage-specific data.
	///
	/// This can be useful for pretty-printing `Bytes` values.
	pub struct AnyBytes(pub Bytes);
}

/// A queue of [AuthorizerHash]s, each of which will be rotated into the authorizer pool for a core.
pub type AuthQueue = FixedVec<AuthorizerHash, AuthQueueLen>;

/// A segment of data.
pub type Segment = FixedVec<u8, SegmentLen>;
// TODO: ^^^ Measure performance penalty for this not being 4096.

impl Segment {
	pub fn as_array(&self) -> &[u8; SEGMENT_LEN] {
		self.as_slice().try_into().expect("Segment length is fixed")
	}
}

impl AsRef<[u8; SEGMENT_LEN]> for Segment {
	fn as_ref(&self) -> &[u8; SEGMENT_LEN] {
		self.as_array()
	}
}

pub trait ToAny {
	type Any;
	fn any(&self) -> Self::Any;
	fn into_any(self) -> Self::Any;
}

impl ToAny for [u8; 32] {
	type Any = AnyHash;
	fn any(&self) -> Self::Any {
		AnyHash(*self)
	}
	fn into_any(self) -> Self::Any {
		AnyHash(self)
	}
}

impl ToAny for alloc::vec::Vec<u8> {
	type Any = AnyVec;
	fn any(&self) -> Self::Any {
		AnyVec(self.clone())
	}
	fn into_any(self) -> Self::Any {
		AnyVec(self)
	}
}

impl ToAny for &[u8] {
	type Any = AnyVec;
	fn any(&self) -> Self::Any {
		AnyVec(self.to_vec())
	}
	fn into_any(self) -> Self::Any {
		AnyVec(self.to_vec())
	}
}

impl MaxEncodedLen for Authorization {
	fn max_encoded_len() -> usize {
		BoundedVec::<u8, MaxInput>::max_encoded_len()
	}
}

impl MaxEncodedLen for WorkPayload {
	fn max_encoded_len() -> usize {
		BoundedVec::<u8, MaxInput>::max_encoded_len()
	}
}

impl MaxEncodedLen for AuthConfig {
	fn max_encoded_len() -> usize {
		BoundedVec::<u8, MaxInput>::max_encoded_len()
	}
}

impl MaxEncodedLen for AuthTrace {
	fn max_encoded_len() -> usize {
		BoundedVec::<u8, MaxReportElectiveData>::max_encoded_len()
	}
}

impl AuthTrace {
	pub fn leak(self) -> &'static [u8] {
		self.0.leak()
	}
}

impl MaxEncodedLen for WorkOutput {
	fn max_encoded_len() -> usize {
		BoundedVec::<u8, MaxReportElectiveData>::max_encoded_len()
	}
}

impl WorkOutput {
	pub fn leak(self) -> &'static [u8] {
		self.0.leak()
	}
}
