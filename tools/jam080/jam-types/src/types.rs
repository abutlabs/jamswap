use core::{
	borrow::Borrow,
	ops::{Add, AddAssign},
};

use crate::simple::{Hash, MaxDependencies, MaxExtrinsics, MaxImports, MaxWorkItems};

use super::*;
use simple::OpaqueBlsPublic;

/// A `Wrap` implementation provides a type constructor which "wraps" a type.
pub trait Wrap {
	/// Wrap `T`.
	type Wrap<T>: Borrow<T>;
}

/// No-op `Wrap`.
// These traits are derived to avoid running into issues with Rust's poor handling of derive with
// generic parameters; see https://github.com/rust-lang/rust/issues/26925
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NoWrap;

impl Wrap for NoWrap {
	type Wrap<T> = T;
}

/// Plain-old-data struct of the same length and layout to `ValKeyset` struct. This does not
/// bring in any cryptography.
#[derive(Copy, Clone, Encode, Decode, Debug, Eq, PartialEq)]
pub struct OpaqueValKeyset {
	/// The opaque Bandersnatch public key.
	pub bandersnatch: OpaqueBandersnatchPublic,
	/// The opaque Ed25519 public key.
	pub ed25519: OpaqueEd25519Public,
	/// The opaque BLS public key.
	pub bls: OpaqueBlsPublic,
	/// The opaque metadata.
	pub metadata: OpaqueValidatorMetadata,
}

impl Default for OpaqueValKeyset {
	fn default() -> Self {
		Self {
			bandersnatch: OpaqueBandersnatchPublic::zero(),
			ed25519: OpaqueEd25519Public::zero(),
			bls: OpaqueBlsPublic::zero(),
			metadata: OpaqueValidatorMetadata::zero(),
		}
	}
}

/// The opaque keys for each validator.
pub type OpaqueValKeysets = FixedVec<OpaqueValKeyset, ValCount>;

/// Reference to a sequence of import segments, which when combined with an index forms a
/// commitment to a specific segment of data.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Hash)]
pub enum RootIdentifier {
	/// Direct cryptographic commitment to the export-segments tree root.
	Direct(SegmentTreeRoot),
	/// Indirect reference to the export-segments tree root via a hash of the work-package which
	/// resulted in it.
	Indirect(WorkPackageHash),
}

impl From<SegmentTreeRoot> for RootIdentifier {
	fn from(root: SegmentTreeRoot) -> Self {
		Self::Direct(root)
	}
}
impl From<WorkPackageHash> for RootIdentifier {
	fn from(hash: WorkPackageHash) -> Self {
		Self::Indirect(hash)
	}
}
impl TryFrom<RootIdentifier> for SegmentTreeRoot {
	type Error = WorkPackageHash;
	fn try_from(root: RootIdentifier) -> Result<Self, Self::Error> {
		match root {
			RootIdentifier::Direct(root) => Ok(root),
			RootIdentifier::Indirect(hash) => Err(hash),
		}
	}
}

/// Import segments specification, which identifies a single exported segment.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Hash)]
pub struct ImportSpec {
	/// The identifier of a series of exported segments.
	pub root: RootIdentifier,
	/// The index into the identified series of exported segments.
	pub index: u16,
}

impl Encode for ImportSpec {
	fn encode_to<T: codec::Output + ?Sized>(&self, dest: &mut T) {
		let off = match &self.root {
			RootIdentifier::Direct(root) => {
				root.encode_to(dest);
				0
			},
			RootIdentifier::Indirect(hash) => {
				hash.encode_to(dest);
				1 << 15
			},
		};
		(self.index + off).encode_to(dest);
	}
}

impl Decode for ImportSpec {
	fn decode<I: codec::Input>(input: &mut I) -> Result<Self, codec::Error> {
		let h = Hash::decode(input)?;
		let i = u16::decode(input)?;
		let root = if i & (1 << 15) == 0 {
			SegmentTreeRoot::from(h).into()
		} else {
			WorkPackageHash::from(h).into()
		};
		Ok(Self { root, index: i & !(1 << 15) })
	}

	fn encoded_fixed_size() -> Option<usize> {
		Some(core::mem::size_of::<Hash>() + core::mem::size_of::<u16>())
	}
}

impl MaxEncodedLen for ImportSpec {
	fn max_encoded_len() -> usize {
		Hash::max_encoded_len() + u16::max_encoded_len()
	}
}

/// Specification of a single piece of extrinsic data.
#[derive(Clone, Encode, Decode, MaxEncodedLen, Debug)]
pub struct ExtrinsicSpec {
	/// The hash of the extrinsic data.
	pub hash: ExtrinsicHash,
	/// The length of the extrinsic data.
	pub len: u32,
}

/// Sequence of [WorkItem]s, each wrapped by `W`, within a [WrappedWorkPackage] and thus limited in
/// length to [max_work_items()].
pub type WrappedWorkItems<W> = BoundedVec<<W as Wrap>::Wrap<WorkItem>, MaxWorkItems>;

/// Sequence of [WorkItem]s within a [WorkPackage] and thus limited in length to
/// [max_work_items()].
pub type WorkItems = WrappedWorkItems<NoWrap>;

/// A definition of work to be done by the Refinement logic of a service and transformed into a
/// [WorkOutput] for its Accumulation logic.
#[derive(Clone, Encode, Decode, MaxEncodedLen, Debug)]
pub struct WorkItem {
	/// Service identifier to which this work item relates.
	pub service: ServiceId,
	/// The service's code hash at the time of reporting. This must be available in-core at the
	/// time of the lookup-anchor block.
	pub code_hash: CodeHash,
	/// Gas limit with which to execute this work item's Refine logic.
	pub refine_gas_limit: UnsignedGas,
	/// Gas limit with which to execute this work item's Accumulate logic.
	pub accumulate_gas_limit: UnsignedGas,
	/// Number of segments exported by this work item.
	pub export_count: u16,
	/// Opaque data passed in to the service's Refinement logic to describe its workload.
	pub payload: WorkPayload,
	/// Sequence of imported data segments.
	pub import_segments: WorkItemImportsVec,
	/// Additional data available to the service's Refinement logic while doing its workload.
	pub extrinsics: BoundedVec<ExtrinsicSpec, MaxExtrinsics>,
}

impl WorkItem {
	/// Returns the sum of the lengths of the item's extrinsics.
	///
	/// Returns `u32::MAX` on overflow.
	pub fn extrinsic_size(&self) -> u32 {
		self.extrinsics
			.iter()
			.map(|xt| xt.len)
			.fold(0u32, |sum, len| sum.saturating_add(len))
	}
}

/// A sequence of import specifications.
pub type WorkItemImportsVec = BoundedVec<ImportSpec, MaxImports>;

/// Various pieces of information helpful to contextualize the Refinement process.
#[derive(Clone, Encode, Decode, Debug, Eq, PartialEq)]
pub struct RefineContext {
	/// The most recent header hash of the chain when building. This must be no more than
	/// `RECENT_BLOCKS` blocks old when reported.
	pub anchor: HeaderHash,
	/// Must be state root of block `anchor`. This is checked on-chain when reported.
	pub state_root: StateRootHash,
	/// Must be Beefy root of block `anchor`. This is checked on-chain when reported.
	pub beefy_root: MmrPeakHash,
	/// The hash of a header of a block which is final. Availability will not succeed unless a
	/// super-majority of validators have attested to this.
	/// Preimage `lookup`s will be judged according to this block.
	pub lookup_anchor: HeaderHash,
	/// The slot of `lookup_anchor` on the chain. This is checked in availability and the
	/// report's package will not be made available without it being correct.
	/// This value must be at least `anchor_slot + 14400`.
	pub lookup_anchor_slot: Slot,
	/// Hashes of Work Packages, the reports of which must be reported prior to this one.
	/// This is checked on-chain when reported.
	pub prerequisites: VecSet<WorkPackageHash>,
}

impl MaxEncodedLen for RefineContext {
	fn max_encoded_len() -> usize {
		HeaderHash::max_encoded_len() + // anchor
			StateRootHash::max_encoded_len() + // state_root
			MmrPeakHash::max_encoded_len() + // beefy_root
			HeaderHash::max_encoded_len() + // lookup_anchor
			Slot::max_encoded_len() + // lookup_anchor_slot
			BoundedVec::<WorkPackageHash, MaxDependencies>::max_encoded_len() // prerequisites
	}
}

impl RefineContext {
	#[doc(hidden)]
	pub fn largest() -> Self {
		Self {
			anchor: Default::default(),
			state_root: Default::default(),
			beefy_root: Default::default(),
			lookup_anchor: Default::default(),
			lookup_anchor_slot: Slot::MAX,
			prerequisites: (0..max_dependencies()).map(|i| [i as u8; 32].into()).collect(),
		}
	}
}

/// A work-package, a collection of work-items together with authorization and contextual
/// information. This is processed _in-core_ with Is-Authorized and Refine logic to produce a
/// work-report.
///
/// The `context` and `items` fields are wrapped by `W`. Use [`WorkPackage`] if you want a plain
/// work-package struct.
#[derive(Clone, Debug)]
pub struct WrappedWorkPackage<W: Wrap> {
	/// Authorization token.
	pub authorization: Authorization,
	/// Service identifier.
	pub auth_code_host: ServiceId,
	/// Authorizer.
	pub authorizer: Authorizer,
	/// Refinement context.
	pub context: W::Wrap<RefineContext>,
	/// Sequence of work items.
	pub items: W::Wrap<WrappedWorkItems<W>>,
}

impl<W: Wrap> WrappedWorkPackage<W> {
	/// Returns the total number of extrinsics.
	pub fn extrinsic_count(&self) -> u32 {
		self.items
			.borrow()
			.iter()
			.map(|item| item.borrow().extrinsics.len() as u32)
			.sum()
	}

	/// Returns the sum of the lengths of the package's extrinsics.
	///
	/// Returns `u32::MAX` on overflow.
	pub fn saturated_extrinsic_size(&self) -> u32 {
		self.items
			.borrow()
			.iter()
			.map(|item| item.borrow().extrinsic_size())
			.fold(0u32, |sum, size| sum.saturating_add(size))
	}

	/// Returns an iterator over all of the import specs.
	pub fn import_specs(&self) -> impl Iterator<Item = &ImportSpec> {
		self.items.borrow().iter().flat_map(|item| item.borrow().import_segments.iter())
	}

	/// Returns the total number of imports.
	pub fn import_count(&self) -> u32 {
		self.items
			.borrow()
			.iter()
			.map(|item| item.borrow().import_segments.len() as u32)
			.sum()
	}

	/// Returns the total number of exports.
	pub fn export_count(&self) -> u32 {
		self.items.borrow().iter().map(|item| item.borrow().export_count as u32).sum()
	}

	/// Returns the number of dependencies, including dependencies implied by indirect imports.
	pub fn dependency_count(&self) -> u32 {
		let mut indirect = VecSet::new();
		for spec in self.import_specs() {
			if let RootIdentifier::Indirect(wph) = &spec.root {
				indirect.insert(wph);
			}
		}
		(self.context.borrow().prerequisites.len() + indirect.len()) as u32
	}
}

impl<W: Wrap> Encode for WrappedWorkPackage<W>
where
	W::Wrap<RefineContext>: Encode,
	W::Wrap<WrappedWorkItems<W>>: Encode,
{
	fn encode_to<T: codec::Output + ?Sized>(&self, dest: &mut T) {
		self.auth_code_host.encode_to(dest);
		self.authorizer.code_hash.encode_to(dest);
		self.context.encode_to(dest);
		self.authorization.encode_to(dest);
		self.authorizer.config.encode_to(dest);
		self.items.encode_to(dest);
	}
}

impl<W: Wrap> Decode for WrappedWorkPackage<W>
where
	W::Wrap<RefineContext>: Decode,
	W::Wrap<WrappedWorkItems<W>>: Decode,
{
	fn decode<I: codec::Input>(input: &mut I) -> Result<Self, codec::Error> {
		let auth_code_host = ServiceId::decode(input)?;
		let auth_code_hash = CodeHash::decode(input)?;
		let context = W::Wrap::<RefineContext>::decode(input)?;
		let authorization = Authorization::decode(input)?;
		let auth_config = AuthConfig::decode(input)?;
		let items = W::Wrap::<WrappedWorkItems<W>>::decode(input)?;
		Ok(Self {
			authorization,
			auth_code_host,
			authorizer: Authorizer { code_hash: auth_code_hash, config: auth_config },
			context,
			items,
		})
	}
}

impl<W: Wrap> MaxEncodedLen for WrappedWorkPackage<W>
where
	Self: Encode,
{
	fn max_encoded_len() -> usize {
		// This implementation is somewhat naive. It doesn't account for eg the fact that if there
		// are many import specs, some of the max_input() allowance will be used up by the imported
		// segments. The implementation of max_bundle_size in jam-std-common relies on this
		// naivety. If you change this function, ensure you don't break that!

		let mut max = Authorization::max_encoded_len() +
			ServiceId::max_encoded_len() + // auth_code_host
			Authorizer::max_encoded_len() +
			RefineContext::max_encoded_len() +
			WorkItems::max_encoded_len();

		// In the max expression above, the max_input() bound is effectively applied separately to
		// authorization, authorizer.config, and item payloads. It actually applies to the combined
		// size of these (plus imports and extrinsics) -- they cannot _all_ have maximum length.
		// Account for this.
		let max_inputs = 2 + max_work_items();
		max -= (max_inputs - 1) * (max_input() as usize);

		// Similarly, the max_imports() bound has effectively been applied separately to each item,
		// but it applies to the total number of imports
		max -= (max_work_items() - 1) * (max_imports() as usize) * ImportSpec::max_encoded_len();

		// And the same for max_extrinsics()
		max -=
			(max_work_items() - 1) * (max_extrinsics() as usize) * ExtrinsicSpec::max_encoded_len();

		max
	}
}

/// A work-package, a collection of work-items together with authorization and contextual
/// information. This is processed _in-core_ with Is-Authorized and Refine logic to produce a
/// work-report.
pub type WorkPackage = WrappedWorkPackage<NoWrap>;

/// The authorizer tuple which together identifies a means of determining whether a Work Package is
/// acceptable to execute on a core.
#[derive(Clone, Encode, Decode, MaxEncodedLen, Debug)]
pub struct Authorizer {
	/// Authorization code hash.
	pub code_hash: CodeHash,
	/// Configuration blob for the auth logic.
	pub config: AuthConfig,
}

impl Authorizer {
	pub fn any() -> Self {
		Self { code_hash: CodeHash::zero(), config: Default::default() }
	}

	pub fn with_concat<R>(&self, f: impl Fn(&[u8]) -> R) -> R {
		f(&[&self.code_hash.0[..], &self.config[..]].concat()[..])
	}

	pub fn hash(&self, hasher: impl Fn(&[u8]) -> Hash) -> AuthorizerHash {
		self.with_concat(hasher).into()
	}
}

/// Potential errors encountered during the refinement of a [`WorkItem`].
///
/// Although additional errors may be generated internally by the PVM engine,
/// these are the specific errors designated by the GP for the [`WorkResult`]
/// and that are eligible to be forwarded to the accumulate process as part
/// of the [`AccumulateItem`].
#[derive(Clone, Encode, Decode, MaxEncodedLen, Debug, Eq, PartialEq)]
#[doc(hidden)]
pub enum WorkError {
	/// Gas exhausted (∞).
	OutOfGas = 1,
	/// Unexpected termination (☇).
	Panic = 2,
	/// Invalid amount of segments exported (⊚).
	BadExports = 3,
	/// Work output is too big (⊖).
	OutputOversize = 4,
	/// Bad code for the service (`BAD`).
	///
	/// This may occur due to an unknown service identifier or unavailable code preimage.
	BadCode = 5,
	/// Out of bounds code size (`BIG`).
	CodeOversize = 6,
}

/// Fields describing the level of activity imposed on the core to construct the `WorkResult`
/// output.
#[derive(Copy, Clone, Encode, Decode, MaxEncodedLen, Debug, Eq, PartialEq, Default)]
#[doc(hidden)]
pub struct RefineLoad {
	/// The amount of gas actually used for this refinement.
	#[codec(compact)]
	pub gas_used: UnsignedGas,
	/// The number of imports made.
	#[codec(compact)]
	pub imports: u16,
	/// The number of extrinsics referenced.
	#[codec(compact)]
	pub extrinsic_count: u16,
	/// The amount of data used in extrinsics.
	#[codec(compact)]
	pub extrinsic_size: u32,
	/// The number of exports made.
	#[codec(compact)]
	pub exports: u16,
}

impl Add for RefineLoad {
	type Output = Self;
	fn add(self, rhs: Self) -> Self {
		Self {
			gas_used: self.gas_used + rhs.gas_used,
			imports: self.imports + rhs.imports,
			extrinsic_count: self.extrinsic_count + rhs.extrinsic_count,
			extrinsic_size: self.extrinsic_size + rhs.extrinsic_size,
			exports: self.exports + rhs.exports,
		}
	}
}

impl AddAssign for RefineLoad {
	fn add_assign(&mut self, rhs: Self) {
		self.gas_used += rhs.gas_used;
		self.imports += rhs.imports;
		self.extrinsic_count += rhs.extrinsic_count;
		self.extrinsic_size += rhs.extrinsic_size;
		self.exports += rhs.exports;
	}
}

/// The result and surrounding context of a single Refinement operation passed as part of a Work
/// Report.
#[derive(Clone, Encode, Decode, MaxEncodedLen, Debug, Eq, PartialEq)]
#[doc(hidden)]
pub struct WorkDigest {
	/// The service whose Refinement gave this result.
	pub service: ServiceId,
	/// The service's code hash at the time of reporting. This must be available in-core at the
	/// time of the lookup-anchor block.
	pub code_hash: CodeHash,
	/// The hash of the payload data passed into Refinement which gave this result.
	pub payload_hash: PayloadHash,
	/// The amount of gas to be used for the accumulation of this result.
	pub accumulate_gas: UnsignedGas,
	/// The result of the Refinement operation itself.
	#[codec(encoded_as = "CompactRefineResult")]
	pub result: Result<WorkOutput, WorkError>,
	/// Information the how much resources the refinement consumed.
	pub refine_load: RefineLoad,
}

/// The result and surrounding context of a single Refinement operation passed in to the
/// Accumulation logic.
#[derive(Clone, Debug, Encode, Decode)]
pub struct WorkItemRecord {
	/// The hash of the work-package in which the work-item which gave this result was placed.
	pub package: WorkPackageHash,
	/// The root of the segment tree which was generated by the work-package in which the work-item
	/// which gave this result was placed.
	pub exports_root: SegmentTreeRoot,
	/// The hash of the authorizer which authorized the execution of the work-package
	/// which generated this result.
	pub authorizer_hash: AuthorizerHash,
	/// The hash of the payload data passed into Refinement which gave this result.
	pub payload: PayloadHash,
	/// The amount of gas provided to Accumulate by the work-item behind this result.
	#[codec(compact)]
	pub gas_limit: UnsignedGas,
	/// The result of the Refinement operation itself.
	#[codec(encoded_as = "CompactRefineResult")]
	pub result: Result<WorkOutput, WorkError>,
	/// The output of the Is-Authorized logic which authorized the execution of the work-package
	/// which generated this result.
	pub auth_output: AuthTrace,
}

/// A single deferred transfer of balance and/or data, passed in to the Accumulation logic.
#[derive(Debug, Clone, Encode, Decode, Default)]
pub struct TransferRecord {
	/// The index of the service from which the transfer was made.
	pub source: ServiceId,
	/// The index of the service which is the target of the transfer.
	pub destination: ServiceId,
	/// The balance passed from the `source` service to the `destination`.
	pub amount: Balance,
	/// The information passed from the `source` service to the `destination`.
	pub memo: Memo,
	/// The gas limit with which the `destination` On Transfer logic may execute in order to
	/// process this transfer.
	pub gas_limit: UnsignedGas,
}

/// Accumulate item.
#[derive(Debug, Encode, Decode)]
pub enum AccumulateItem {
	/// Work item record
	WorkItem(WorkItemRecord),
	/// Incoming transfer record
	Transfer(TransferRecord),
}

impl From<WorkItemRecord> for AccumulateItem {
	fn from(w: WorkItemRecord) -> Self {
		AccumulateItem::WorkItem(w)
	}
}

impl From<TransferRecord> for AccumulateItem {
	fn from(t: TransferRecord) -> Self {
		AccumulateItem::Transfer(t)
	}
}

/// Parameters for the invocation of Accumulate.
#[derive(Debug, Encode, Decode)]
#[doc(hidden)]
pub struct AccumulateParams {
	/// The current time slot.
	#[codec(compact)]
	pub slot: Slot,
	/// The index of the service being accumulated.
	#[codec(compact)]
	pub service_id: ServiceId,
	/// Number of work-results or transfers to accumulate.
	#[codec(compact)]
	pub item_count: u32,
}

/// Parameters for the invocation of Refine.
#[derive(Debug, Encode, Decode)]
#[doc(hidden)]
pub struct IsAuthorizedParams {
	/// Core index.
	#[codec(compact)]
	pub core: u16,
}

/// Parameters for the invocation of Refine.
#[derive(Debug, Encode, Decode)]
#[doc(hidden)]
pub struct RefineParams {
	/// Core index.
	#[codec(compact)]
	pub core_index: CoreIndex,
	/// Work-item index.
	#[codec(compact)]
	pub item_index: u32, // u32?
	/// The index of the service being refined.
	#[codec(compact)]
	pub service_id: ServiceId,
	/// The payload data to process.
	pub payload: WorkPayload,
	/// The hash of the Work Package.
	pub package_hash: WorkPackageHash,
}

// TODO: @gav Consider moving to jam-node.
/// Parameters for the invocation of Refine, reference variant.
#[derive(Debug, Encode)]
#[doc(hidden)]
pub struct RefineParamsRef<'a> {
	/// Core index.
	#[codec(compact)]
	pub core_index: CoreIndex,
	/// Work-item index.
	#[codec(compact)]
	pub item_index: u32,
	/// The index of the service being refined.
	#[codec(compact)]
	pub service_id: ServiceId,
	/// The payload data to process.
	pub payload: &'a WorkPayload,
	/// The hash of the Work Package.
	pub package_hash: &'a WorkPackageHash,
}

/// Information concerning a particular service's state.
///
/// This is used in the `service_info` host-call.
#[derive(Debug, Clone, Encode, Decode, MaxEncodedLen)]
pub struct ServiceInfo {
	/// The hash of the code of the service.
	pub code_hash: CodeHash,
	/// The existing balance of the service.
	pub balance: Balance,
	/// The minimum balance which the service must satisfy.
	pub threshold: Balance,
	/// The minimum amount of gas which must be provided to this service's `accumulate` for each
	/// work item it must process.
	pub min_item_gas: UnsignedGas,
	/// The minimum amount of gas which must be provided to this service's `accumulate` for each
	/// incoming transfer it must process.
	pub min_memo_gas: UnsignedGas,
	/// The total number of bytes used for data electively held for this service on-chain.
	pub bytes: u64,
	/// The total number of items of data electively held for this service on-chain.
	pub items: u32,
	/// Offset of storage footprint only above which a minimum deposit is needed.
	pub deposit_offset: Balance,
	/// Creation time slot.
	pub creation_slot: Slot,
	/// Most recent accumulation time slot.
	pub last_accumulation_slot: Slot,
	/// Parent service identifier.
	pub parent_service: ServiceId,
}

impl ServiceInfo {
	/// Field offsets in the encoded representation
	pub const CODE_HASH_OFFSET: usize = 0;
	pub const BALANCE_OFFSET: usize = 32;
	pub const THRESHOLD_OFFSET: usize = 40;
	pub const MIN_ITEM_GAS_OFFSET: usize = 48;
	pub const MIN_MEMO_GAS_OFFSET: usize = 56;
	pub const BYTES_OFFSET: usize = 64;
	pub const ITEMS_OFFSET: usize = 72;
	pub const DEPOSIT_OFFSET_OFFSET: usize = 76;
	pub const CREATION_SLOT_OFFSET: usize = 84;
	pub const LAST_ACCUMULATION_SLOT_OFFSET: usize = 88;
	pub const PARENT_SERVICE_OFFSET: usize = 92;
	/// Encoded length in bytes.
	pub const ENCODED_LEN: usize = 96;
}

/// Refine result used for compact encoding of work result as prescribed by GP.
struct CompactRefineResult(Result<WorkOutput, WorkError>);
struct CompactRefineResultRef<'a>(&'a Result<WorkOutput, WorkError>);

impl From<CompactRefineResult> for Result<WorkOutput, WorkError> {
	fn from(value: CompactRefineResult) -> Self {
		value.0
	}
}

impl<'a> From<&'a Result<WorkOutput, WorkError>> for CompactRefineResultRef<'a> {
	fn from(value: &'a Result<WorkOutput, WorkError>) -> Self {
		CompactRefineResultRef(value)
	}
}

impl<'a> codec::EncodeAsRef<'a, Result<WorkOutput, WorkError>> for CompactRefineResult {
	type RefType = CompactRefineResultRef<'a>;
}

impl Encode for CompactRefineResult {
	fn encode_to<T: codec::Output + ?Sized>(&self, dest: &mut T) {
		CompactRefineResultRef(&self.0).encode_to(dest)
	}
}

impl MaxEncodedLen for CompactRefineResult {
	fn max_encoded_len() -> usize {
		(1 + WorkOutput::max_encoded_len()).max(WorkError::max_encoded_len())
	}
}

impl Encode for CompactRefineResultRef<'_> {
	fn encode_to<T: codec::Output + ?Sized>(&self, dest: &mut T) {
		match &self.0 {
			Ok(o) => {
				dest.push_byte(0);
				o.encode_to(dest)
			},
			Err(e) => e.encode_to(dest),
		}
	}
}

impl Decode for CompactRefineResult {
	fn decode<I: codec::Input>(input: &mut I) -> Result<Self, codec::Error> {
		match input.read_byte()? {
			0 => Ok(Self(Ok(WorkOutput::decode(input)?))),
			e => Ok(Self(Err(WorkError::decode(&mut &[e][..])?))),
		}
	}
}

macro_rules! fetch_kind {
	($($(#[$attr:meta])* $variant:ident = $value:expr),* $(,)?) => {
		/// Fetch host calls variants identifiers.
		#[derive(Copy, Clone, Debug)]
		pub enum FetchKind {
			$(
				$(#[$attr])*
				$variant = $value,
			)*
		}

		impl TryFrom<u64> for FetchKind {
			type Error = ();
			fn try_from(value: u64) -> Result<Self, Self::Error> {
				match value {
					$(
						$value => Ok(FetchKind::$variant),
					)*
					_ => Err(()),
				}
			}
		}
	};
}

fetch_kind! {
	/// Protocol stateless parameters.
	ProtocolParameters = 0,
	/// Entropy.
	Entropy = 1,
	/// Output from the parameterized authorizer code.
	AuthTrace = 2,
	/// A particular extrinsic of a given work-item.
	AnyExtrinsic = 3,
	/// A particular extrinsic of the executing work-item.
	OurExtrinsic = 4,
	/// A particular import-segment of a given work-item.
	AnyImport = 5,
	/// A particular import-segment of the executing work-item.
	OurImport = 6,
	/// Current work-package.
	WorkPackage = 7,
	/// Work package authorization config blob.
	AuthConfig = 8,
	/// Input provided to the parameterized authorizer code.
	AuthToken = 9,
	/// Refine context.
	RefineContext = 10,
	/// All work items information summary.
	ItemsSummary = 11,
	/// A particular work item information summary.
	AnyItemSummary = 12,
	/// A particular work item payload.
	AnyPayload = 13,
	/// All accumulate items.
	AccumulateItems = 14,
	/// A particular accumulate item.
	AnyAccumulateItem = 15,
}

/// Work item summary information used by fetch host call.
#[derive(Clone, Encode, Decode, Debug)]
pub struct WorkItemSummary {
	pub service: ServiceId,
	pub code_hash: CodeHash,
	pub refine_gas_limit: UnsignedGas,
	pub accumulate_gas_limit: UnsignedGas,
	pub export_count: u16,
	pub import_count: u16,
	pub extrinsics_count: u16,
	pub payload_len: u32,
}

impl From<&WorkItem> for WorkItemSummary {
	fn from(w: &WorkItem) -> Self {
		WorkItemSummary {
			service: w.service,
			code_hash: w.code_hash,
			refine_gas_limit: w.refine_gas_limit,
			accumulate_gas_limit: w.accumulate_gas_limit,
			export_count: w.export_count,
			import_count: w.import_segments.len() as u16,
			extrinsics_count: w.extrinsics.len() as u16,
			payload_len: w.payload.len() as u32,
		}
	}
}

/// Page access mode.
#[derive(Copy, Clone, Debug)]
pub enum PageMode {
	ReadOnly = 0,
	ReadWrite = 1,
}

/// Page operation.
#[derive(Copy, Clone, Debug)]
pub enum PageOperation {
	Free,
	Alloc(PageMode),
	SetMode(PageMode),
}

impl From<PageOperation> for u64 {
	fn from(value: PageOperation) -> Self {
		match value {
			PageOperation::Free => 0,
			PageOperation::Alloc(mode) => 1 + mode as u64,
			PageOperation::SetMode(mode) => 3 + mode as u64,
		}
	}
}

impl TryFrom<u64> for PageOperation {
	type Error = ();
	fn try_from(operation: u64) -> Result<Self, Self::Error> {
		match operation {
			0 => Ok(Self::Free),
			1 => Ok(Self::Alloc(PageMode::ReadOnly)),
			2 => Ok(Self::Alloc(PageMode::ReadWrite)),
			3 => Ok(Self::SetMode(PageMode::ReadOnly)),
			4 => Ok(Self::SetMode(PageMode::ReadWrite)),
			_ => Err(()),
		}
	}
}

#[cfg(test)]
mod tests {
	use super::*;
	use bounded_collections::{bounded_vec, TryCollect};
	use codec::DecodeAll;

	#[test]
	fn compact_refine_result_codec() {
		let enc_dec = |exp_res, exp_buf: &[u8]| {
			let buf = CompactRefineResultRef(&exp_res).encode();
			assert_eq!(buf, exp_buf);
			let res = CompactRefineResult::decode(&mut &buf[..]).unwrap();
			assert_eq!(res.0, exp_res);
		};

		enc_dec(Ok(vec![1, 2, 3].into()), &[0, 3, 1, 2, 3]);
		enc_dec(Err(WorkError::OutOfGas), &[1]);
		enc_dec(Err(WorkError::Panic), &[2]);
		enc_dec(Err(WorkError::BadExports), &[3]);
		enc_dec(Err(WorkError::OutputOversize), &[4]);
		enc_dec(Err(WorkError::BadCode), &[5]);
		enc_dec(Err(WorkError::CodeOversize), &[6]);
	}

	#[test]
	fn service_info_encoded_len_is_correct() {
		assert_eq!(ServiceInfo::max_encoded_len(), ServiceInfo::ENCODED_LEN);
	}

	#[test]
	fn service_info_item_offset_works() {
		let default = ServiceInfo {
			code_hash: Default::default(),
			balance: Default::default(),
			threshold: Default::default(),
			min_item_gas: Default::default(),
			min_memo_gas: Default::default(),
			bytes: Default::default(),
			items: Default::default(),
			deposit_offset: Default::default(),
			creation_slot: Default::default(),
			last_accumulation_slot: Default::default(),
			parent_service: Default::default(),
		};

		macro_rules! sanity_check {
			($field:ident, $type:ident, $offset:expr, $value:expr) => {{
				// Verify the type has a fixed encoded size
				let type_size = core::mem::size_of::<$type>();
				assert_eq!(type_size, $type::max_encoded_len());
				let expected = $type::from($value);
				let test_struct = ServiceInfo { $field: expected, ..default };
				let buffer = test_struct.encode();
				// Decode the field from the known offset
				let actual: $type =
					DecodeAll::decode_all(&mut &buffer[$offset..$offset + type_size]).unwrap();
				assert_eq!(expected, actual);
			}};
			($field:ident, $type:ident, $offset:expr) => {{
				sanity_check!($field, $type, $offset, <$type>::MAX);
			}};
		}

		sanity_check!(code_hash, CodeHash, ServiceInfo::CODE_HASH_OFFSET, [0xff_u8; 32]);
		sanity_check!(balance, Balance, ServiceInfo::BALANCE_OFFSET);
		sanity_check!(threshold, Balance, ServiceInfo::THRESHOLD_OFFSET);
		sanity_check!(min_item_gas, UnsignedGas, ServiceInfo::MIN_ITEM_GAS_OFFSET);
		sanity_check!(min_memo_gas, UnsignedGas, ServiceInfo::MIN_MEMO_GAS_OFFSET);
		sanity_check!(bytes, u64, ServiceInfo::BYTES_OFFSET);
		sanity_check!(items, u32, ServiceInfo::ITEMS_OFFSET);
		sanity_check!(deposit_offset, Balance, ServiceInfo::DEPOSIT_OFFSET_OFFSET);
		sanity_check!(creation_slot, Slot, ServiceInfo::CREATION_SLOT_OFFSET);
		sanity_check!(last_accumulation_slot, Slot, ServiceInfo::LAST_ACCUMULATION_SLOT_OFFSET);
		sanity_check!(parent_service, ServiceId, ServiceInfo::PARENT_SERVICE_OFFSET);
	}

	#[test]
	fn context_max_encoded_len() {
		assert_eq!(RefineContext::largest().encoded_size(), RefineContext::max_encoded_len());
	}

	#[test]
	fn package_max_encoded_len() {
		let portion = |total, i, num| (total / num) + if i == 0 { total % num } else { 0 };

		let max_inputs = 2 + max_work_items();
		let input = |i| vec![0; portion(max_input() as usize, i, max_inputs)];

		let imports = |i| {
			bounded_vec![
				ImportSpec { root: RootIdentifier::Direct(Default::default()), index: u16::MAX };
				portion(max_imports() as usize, i, max_work_items())
			]
		};

		let extrinsics = |i| {
			bounded_vec![
				ExtrinsicSpec { hash: Default::default(), len: u32::MAX };
				portion(max_extrinsics() as usize, i, max_work_items())
			]
		};

		// The largest possible work-package, or at least very close -- it might be possible to get
		// a few bytes more by eg rebalancing the input Vec lengths, as Vec lengths use compact
		// encoding.
		//
		// "Largest possible work-package" is actually a bit of a lie -- it isn't really possible
		// for a work-package to be this large as the total input size would exceed max_input()
		// after accounting for the import segments. WorkPackage::max_encoded_len() does _not_
		// account for these though, and we currently assume this accounting is lacking in the
		// max_bundle_size() implementation in jam-std-common. Hence the construction of an
		// invalidly large work-package here.
		let largest_package = WorkPackage {
			authorization: input(0).into(),
			auth_code_host: ServiceId::MAX,
			authorizer: Authorizer { code_hash: Default::default(), config: input(1).into() },
			context: RefineContext::largest(),
			items: (0..max_work_items())
				.map(|i| WorkItem {
					service: ServiceId::MAX,
					code_hash: Default::default(),
					payload: input(2 + i).into(),
					refine_gas_limit: UnsignedGas::MAX,
					accumulate_gas_limit: UnsignedGas::MAX,
					import_segments: imports(i),
					extrinsics: extrinsics(i),
					export_count: u16::MAX,
				})
				.try_collect()
				.unwrap(),
		};
		assert_eq!(largest_package.import_count(), max_imports());
		assert_eq!(largest_package.extrinsic_count(), max_extrinsics());
		let largest_package_size = largest_package.encoded_size();

		// WorkPackage::max_encoded_len() assumes each input/import-spec/extrinsic-spec Vec can be
		// the maximum length when calculating how many bytes are needed for encoding Vec lengths
		// in the worst case. The Vecs cannot all be the maximum length though due to the shared
		// nature of max_input()/max_imports()/etc. To account for this discrepancy, we allow
		// WorkPackage::max_encoded_len() to overshoot largest_package_size by 1 for each such Vec.
		let max = WorkPackage::max_encoded_len();
		assert!(largest_package_size <= max);
		assert!((largest_package_size + max_inputs + (2 * max_work_items())) >= max);
	}
}
