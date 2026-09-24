// MODIFIED by abutlabs (jamswap tools/jam080, 2026-09-24) for GP 0.8.0 from the
// crates.io jam-pvm-common 0.1.28 release (Apache-2.0, Parity Technologies). See ../../README.md.
use codec::{Decode, Encode, Output};
pub use core::alloc::Layout;

// GP 0.8.0: the `sbrk` instruction is gone (JamV1 ISA), so the upstream allocator
// (picoalloc, which emits it) cannot link. This bump allocator grows the RW heap with
// the `grow_heap` host call instead. Invocations are short-lived, so freeing is a
// no-op; realloc of the most recent block grows in place.
#[cfg(target_env = "polkavm")]
mod grow_heap_alloc {
	use core::alloc::{GlobalAlloc, Layout};

	const PAGE: usize = 4096;

	struct GrowHeap;
	static mut NEXT: usize = 0; // next free byte (0 = not initialised)
	static mut END: usize = 0; // end of the writable heap region
	static mut LAST: usize = 0; // start of the most recent block (for in-place realloc)

	unsafe fn ensure(end: usize) -> bool {
		if end <= END {
			return true;
		}
		let want = end.div_ceil(PAGE) as u64;
		let got = crate::imports::grow_heap(want) as usize;
		END = got * PAGE;
		end <= END
	}

	unsafe impl GlobalAlloc for GrowHeap {
		unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
			if NEXT == 0 {
				NEXT = polkavm_derive::heap_base() as usize;
				END = crate::imports::grow_heap(0) as usize * PAGE;
			}
			let start = NEXT.next_multiple_of(layout.align());
			let end = match start.checked_add(layout.size()) {
				Some(e) => e,
				None => return core::ptr::null_mut(),
			};
			if !ensure(end) {
				return core::ptr::null_mut();
			}
			NEXT = end;
			LAST = start;
			start as *mut u8
		}

		unsafe fn dealloc(&self, _ptr: *mut u8, _layout: Layout) {}

		unsafe fn realloc(&self, ptr: *mut u8, layout: Layout, new_size: usize) -> *mut u8 {
			let p = ptr as usize;
			if p == LAST && p + layout.size() == NEXT {
				if let Some(end) = p.checked_add(new_size) {
					if ensure(end) {
						NEXT = end;
						return ptr;
					}
				}
				return core::ptr::null_mut();
			}
			let new_layout = Layout::from_size_align_unchecked(new_size, layout.align());
			let q = self.alloc(new_layout);
			if !q.is_null() {
				core::ptr::copy_nonoverlapping(ptr, q, layout.size().min(new_size));
			}
			q
		}
	}

	#[global_allocator]
	static ALLOCATOR: GrowHeap = GrowHeap;
}

pub fn alloc(size: u32) -> u32 {
	let ptr = unsafe { alloc::alloc::alloc(Layout::from_size_align(size as usize, 4).unwrap()) };
	ptr as u32
}

pub fn dealloc(ptr: u32, size: u32) {
	unsafe {
		alloc::alloc::dealloc(ptr as *mut u8, Layout::from_size_align(size as usize, 4).unwrap())
	};
}

pub struct BufferOutput<'a>(&'a mut [u8], usize);
impl Output for BufferOutput<'_> {
	/// Write to the output.
	fn write(&mut self, bytes: &[u8]) {
		let (_, rest) = self.0.split_at_mut(self.1);
		let len = bytes.len().min(rest.len());
		rest[..len].copy_from_slice(&bytes[..len]);
		self.1 += len;
	}
}

pub fn decode_buf<T: Decode>(ptr: u32, size: u32) -> T {
	let slice = unsafe { core::slice::from_raw_parts(ptr as *const u8, size as usize) };
	let params = T::decode(&mut &slice[..]);
	params.unwrap()
}

pub fn encode_to_buf<T: Encode>(value: T) -> (u32, u32) {
	// TODO: @gav wish avoid extra copy
	let size = value.encoded_size();
	let ptr = alloc(size as u32);
	let slice = unsafe { core::slice::from_raw_parts_mut(ptr as *mut u8, size) };
	value.encode_to(&mut BufferOutput(&mut slice[..], 0));
	(ptr, size as u32)
}
