// MODIFIED by abutlabs (jamswap tools/jam080, 2026-09-24) for GP 0.8.0 from the
// crates.io jam-pvm-common 0.1.28 release (Apache-2.0, Parity Technologies). See ../../README.md.
#[polkavm_derive::polkavm_import]
extern "C" {
	// NOTE: This is NOT part of the GP.
	#[polkavm_import(index = 100)]
	pub fn log(
		level: u64,
		target_ptr: *const u8,
		target_len: u64,
		text_ptr: *const u8,
		text_len: u64,
	);

	#[polkavm_import(index = 0)]
	pub fn gas() -> u64;

	// GP 0.8.0 (#508): grow the RW heap to end at page `page` (index + 1 of the last
	// writable page); returns the current end page either way. Replaces the removed
	// `sbrk` instruction. Every later host-call id shifted by one vs GP 0.7.2.
	#[polkavm_import(index = 1)]
	pub fn grow_heap(page: u64) -> u64;

	#[polkavm_import(index = 2)]
	pub fn fetch(buffer: *mut u8, offset: u64, buffer_len: u64, kind: u64, a: u64, b: u64) -> u64;

	// If `service == u64::MAX`, then use caller service's storage.
	// Copies up to out_len bytes.
	// Returns `u64::MAX` if the preimage is unknown. Otherwise the preimage's length.
	#[polkavm_import(index = 3)]
	pub fn lookup(
		service: u64,
		hash_ptr: *const u8,
		out: *mut u8,
		offset: u64,
		out_len: u64,
	) -> u64;

	// If `service == u64::MAX`, then use caller service's storage.
	// Copies up to out_len bytes.
	// Returns `u64::MAX` if the key is non-existent. Otherwise the value's length.
	#[polkavm_import(index = 4)]
	pub fn read(
		service: u64,
		key_ptr: *const u8,
		key_len: u64,
		out: *mut u8,
		offset: u64,
		out_len: u64,
	) -> u64;

	// Returns the length of the *old* value or u64::MAX if there wasn't one.
	#[polkavm_import(index = 5)]
	pub fn write(key_ptr: *const u8, key_len: u64, value: *const u8, value_len: u64) -> u64;

	#[polkavm_import(index = 6)]
	pub fn info(service: u64, service_info_ptr: *mut u8, offset: u64, len: u64) -> u64;

	#[polkavm_import(index = 7)]
	pub fn historical_lookup(
		service_id: u64,
		ho: *const u8,
		bo: *mut u8,
		offset: u64,
		bz: u64,
	) -> u64;

	#[polkavm_import(index = 8)]
	pub fn export(buffer: *const u8, buffer_len: u64) -> u64;

	#[polkavm_import(index = 9)]
	pub fn machine(code_ptr: *const u8, code_len: u64, program_counter: u64) -> u64;

	#[polkavm_import(index = 10)]
	pub fn peek(vm_handle: u64, outer_dst: *mut u8, inner_src: u64, length: u64) -> u64;

	#[polkavm_import(index = 11)]
	pub fn poke(vm_handle: u64, outer_src: *const u8, inner_dst: u64, length: u64) -> u64;

	#[polkavm_import(index = 12)]
	pub fn pages(vm_handle: u64, page: u64, count: u64, operation: u64) -> u64;

	// When this crate is compiled natively Rust will complain that the tuple
	// here cannot be used in FFI. This happens because for non-RISC-V targets
	// the `polkavm_import` macro just passes through the `extern C` block as-is.
	//
	// Compiling this natively is nonsense since calling anything would result
	// in a segfault anyway, so just silence the warning.
	#[cfg_attr(not(target_env = "polkavm"), allow(improper_ctypes))]
	#[polkavm_import(index = 13)]
	pub fn invoke(vm_handle: u64, args: *mut core::ffi::c_void) -> (u64, u64);

	#[polkavm_import(index = 14)]
	pub fn expunge(vm_handle: u64) -> u64;

	#[polkavm_import(index = 15)]
	pub fn bless(
		manager: u64,
		assigners: *const u8,
		designate: u64,
		register: u64,
		always_acc: *const u8,
		always_acc_count: u64,
	) -> u64;

	#[polkavm_import(index = 16)]
	pub fn assign(core: u64, auth_ptr: *const u8, assigner: u64) -> u64;

	#[polkavm_import(index = 17)]
	pub fn designate(validator_keys_ptr: *const u8) -> u64;

	#[polkavm_import(index = 18)]
	pub fn checkpoint() -> u64;

	#[polkavm_import(index = 19)]
	pub fn new(
		code_hash_ptr: *const u8,
		code_len: u64,
		min_item_gas: u64,
		min_memo_gas: u64,
		deposit_offset: u64,
		new_service_id: u64,
	) -> u64;

	#[polkavm_import(index = 20)]
	pub fn upgrade(code_hash_ptr: *const u8, min_item_gas: u64, min_memo_gas: u64) -> u64;

	#[polkavm_import(index = 21)]
	pub fn transfer(dest: u64, amount: u64, gas_limit: u64, memo_ptr: *const u8) -> u64;

	#[polkavm_import(index = 22)]
	pub fn eject(target: u64, code_hash: *const u8) -> u64;

	#[cfg_attr(not(target_arch = "riscv64"), allow(improper_ctypes))]
	#[polkavm_import(index = 23)]
	pub fn query(hash_ptr: *const u8, preimage_len: u64) -> (u64, u64);

	#[polkavm_import(index = 24)]
	pub fn solicit(hash_ptr: *const u8, preimage_len: u64) -> u64;

	#[polkavm_import(index = 25)]
	pub fn forget(hash_ptr: *const u8, preimage_len: u64) -> u64;

	#[polkavm_import(index = 26)]
	pub fn yield_hash(hash_ptr: *const u8) -> u64;

	#[polkavm_import(index = 27)]
	pub fn provide(service_id: u64, preimage_ptr: *const u8, preimage_len: u64) -> u64;
}
