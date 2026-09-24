/// A stack writer to be used when panicking to print out the panic message.
struct StackWriter {
	buffer: core::mem::MaybeUninit<[u8; 256]>,
	length: usize,
}

impl StackWriter {
	#[inline(always)]
	fn new() -> Self {
		Self { buffer: core::mem::MaybeUninit::uninit(), length: 0 }
	}

	fn write_bytes(&mut self, bytes: &[u8]) {
		let max_length = core::mem::size_of_val(&self.buffer);
		for &byte in bytes {
			if self.length >= max_length {
				break;
			}

			// SAFETY: We've just checked that `length` is less than `max_length`.
			unsafe {
				let pointer = self.buffer.as_mut_ptr().cast::<u8>().add(self.length);
				pointer.write(byte);
			}

			self.length += 1;
		}
	}

	fn finish(&mut self) -> &[u8] {
		// SAFETY: `length` is guaranteed to always be less than the size of the `buffer`.
		unsafe { core::slice::from_raw_parts(self.buffer.as_ptr().cast::<u8>(), self.length) }
	}
}

impl core::fmt::Write for StackWriter {
	fn write_str(&mut self, s: &str) -> core::fmt::Result {
		self.write_bytes(s.as_bytes());
		Ok(())
	}
}

#[panic_handler]
fn panic(info: &core::panic::PanicInfo) -> ! {
	unsafe {
		// In case formatting the message overflows the stack just print out that we've panicked.
		let initial_message = "Panic handler called!";
		crate::imports::log(
			1,
			core::ptr::null(),
			0,
			initial_message.as_ptr(),
			initial_message.len() as u64,
		);

		// Now we can try to print out the full panic message.
		// Do not use the heap as that also could be borked.
		use core::fmt::Write;
		let mut writer = StackWriter::new();
		let _ = writer.write_str("Panic message: ");
		let _ = write!(&mut writer, "{}", info);
		let msg = writer.finish();
		crate::imports::log(1, core::ptr::null(), 0, msg.as_ptr(), msg.len() as u64);

		core::arch::asm!("unimp", options(noreturn));
	}
}
