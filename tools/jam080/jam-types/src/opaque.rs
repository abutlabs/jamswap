#[cfg(feature = "serde")]
#[doc(hidden)]
#[macro_export]
macro_rules! impl_serde_base64 {
	(
		$(#[$($struct_meta:meta)*])*
		$struct_vis:vis struct $t:ident ($inner_vis:vis [u8; $l:expr]);
	) => {
		$(#[$($struct_meta)*])*
		$struct_vis struct $t($inner_vis [u8; $l]);

		impl $crate::serde::Serialize for $t {
			fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
			where
				S: $crate::serde::Serializer,
			{
				use $crate::base64::{display::Base64Display, engine::general_purpose::STANDARD};
				let display = Base64Display::new(&self.0[..], &STANDARD);
				serializer.collect_str(&display)
			}
		}

		impl<'a> $crate::serde::Deserialize<'a> for $t {
			fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
			where
				D: $crate::serde::Deserializer<'a>,
			{
				struct Visitor;
				impl $crate::serde::de::Visitor<'_> for Visitor {
					type Value = $t;

					fn expecting(&self, formatter: &mut core::fmt::Formatter) -> core::fmt::Result {
						formatter.write_str("BASE64 string")
					}

					fn visit_str<E: $crate::serde::de::Error>(
						self,
						string: &str,
					) -> Result<Self::Value, E> {
						use $crate::base64::{engine::general_purpose::STANDARD, Engine};
						let mut bytes = [0_u8; $l];
						let num_bytes_written = STANDARD
							.decode_slice(string, &mut bytes[..])
							.map_err($crate::serde::de::Error::custom)?;
						if num_bytes_written != bytes.len() {
							return Err($crate::serde::de::Error::custom("Decoded only part of BASE64 string"));
						}
						Ok($t(bytes))
					}
				}
				deserializer.deserialize_str(Visitor)
			}
		}
	};
	(
		$(#[$($struct_meta:meta)*])*
		$struct_vis:vis struct $t:ident ($inner_vis:vis Vec<u8>);
	) => {
		$(#[$($struct_meta)*])*
		$struct_vis struct $t($inner_vis $crate::Vec<u8>);

		impl $crate::serde::Serialize for $t {
			fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
			where
				S: $crate::serde::Serializer,
			{
				use $crate::base64::{display::Base64Display, engine::general_purpose::STANDARD};
				let display = Base64Display::new(&self.0[..], &STANDARD);
				serializer.collect_str(&display)
			}
		}

		impl<'a> $crate::serde::Deserialize<'a> for $t {
			fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
			where
				D: $crate::serde::Deserializer<'a>,
			{
				struct Visitor;
				impl $crate::serde::de::Visitor<'_> for Visitor {
					type Value = $t;

					fn expecting(&self, formatter: &mut core::fmt::Formatter) -> core::fmt::Result {
						formatter.write_str("BASE64 string")
					}

					fn visit_str<E: $crate::serde::de::Error>(
						self,
						string: &str,
					) -> Result<Self::Value, E> {
						use $crate::base64::{engine::general_purpose::STANDARD, Engine};
						let bytes = STANDARD.decode(string).map_err($crate::serde::de::Error::custom)?;
						Ok($t(bytes))
					}
				}
				deserializer.deserialize_str(Visitor)
			}
		}
	};
	(
		$(#[$($struct_meta:meta)*])*
		$struct_vis:vis struct $t:ident ($inner_vis:vis Bytes);
	) => {
		$(#[$($struct_meta)*])*
		$struct_vis struct $t($inner_vis ::bytes::Bytes);

		impl $crate::serde::Serialize for $t {
			fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
			where
				S: $crate::serde::Serializer,
			{
				use $crate::base64::{display::Base64Display, engine::general_purpose::STANDARD};
				let display = Base64Display::new(&self.0[..], &STANDARD);
				serializer.collect_str(&display)
			}
		}

		impl<'a> $crate::serde::Deserialize<'a> for $t {
			fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
			where
				D: $crate::serde::Deserializer<'a>,
			{
				struct Visitor;
				impl $crate::serde::de::Visitor<'_> for Visitor {
					type Value = $t;

					fn expecting(&self, formatter: &mut core::fmt::Formatter) -> core::fmt::Result {
						formatter.write_str("BASE64 string")
					}

					fn visit_str<E: $crate::serde::de::Error>(
						self,
						string: &str,
					) -> Result<Self::Value, E> {
						use $crate::base64::{engine::general_purpose::STANDARD, Engine};
						let bytes = STANDARD.decode(string).map_err($crate::serde::de::Error::custom)?;
						Ok($t(bytes.into()))
					}
				}
				deserializer.deserialize_str(Visitor)
			}
		}
	};
	(
		$(#[$($struct_meta:meta)*])*
		$struct_vis:vis struct $t:ident ($inner_vis:vis FixedVec<u8, $l:ident>);
	) => {
		$(#[$($struct_meta)*])*
		$struct_vis struct $t($inner_vis $crate::FixedVec<u8, $l>);

		impl $crate::serde::Serialize for $t {
			fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
			where
				S: $crate::serde::Serializer,
			{
				use $crate::base64::{display::Base64Display, engine::general_purpose::STANDARD};
				let display = Base64Display::new(self.0.as_slice(), &STANDARD);
				serializer.collect_str(&display)
			}
		}

		impl<'a> $crate::serde::Deserialize<'a> for $t {
			fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
			where
				D: $crate::serde::Deserializer<'a>,
			{
				struct Visitor;
				impl $crate::serde::de::Visitor<'_> for Visitor {
					type Value = $t;

					fn expecting(&self, formatter: &mut core::fmt::Formatter) -> core::fmt::Result {
						formatter.write_str("BASE64 string")
					}

					fn visit_str<E: $crate::serde::de::Error>(
						self,
						string: &str,
					) -> Result<Self::Value, E> {
						use $crate::base64::{engine::general_purpose::STANDARD, Engine};
						let bytes = STANDARD
							.decode(string)
							.map_err($crate::serde::de::Error::custom)?
							.try_into()
							.map_err(|_| serde::de::Error::custom("Invalid FixedVec length"))?;
						Ok($t(bytes))
					}
				}
				deserializer.deserialize_str(Visitor)
			}
		}
	};
}

#[cfg(not(feature = "serde"))]
#[doc(hidden)]
#[macro_export]
macro_rules! impl_serde_base64 {
	(
		$(#[$($struct_meta:meta)*])*
		$struct_vis:vis struct $t:ident ($inner_vis:vis [u8; $l:expr]);
	) => {
		$(#[$($struct_meta)*])*
		$struct_vis struct $t($inner_vis [u8; $l]);
	};
	(
		$(#[$($struct_meta:meta)*])*
		$struct_vis:vis struct $t:ident ($inner_vis:vis Vec<u8>);
	) => {
		$(#[$($struct_meta)*])*
		$struct_vis struct $t($inner_vis $crate::Vec<u8>);
	};
	(
		$(#[$($struct_meta:meta)*])*
		$struct_vis:vis struct $t:ident ($inner_vis:vis Bytes);
	) => {
		$(#[$($struct_meta)*])*
		$struct_vis struct $t($inner_vis ::bytes::Bytes);
	};
	(
		$(#[$($struct_meta:meta)*])*
		$struct_vis:vis struct $t:ident ($inner_vis:vis FixedVec<u8, $l:ident>);
	) => {
		$(#[$($struct_meta)*])*
		$struct_vis struct $t($inner_vis $crate::FixedVec<u8, $l>);
	};
}

#[doc(hidden)]
#[macro_export]
macro_rules! opaque {
	() => {};
	(
		$(#[$($struct_meta:meta)*])*
		$struct_vis:vis
		struct $t:ident ($inner_vis:vis [u8; $l:expr]);
		$($rest:tt)*
	) => {
		$crate::impl_serde_base64! {
			$(#[$($struct_meta)*])*
			#[derive(
				$crate::Encode,
				$crate::Decode,
				$crate::MaxEncodedLen,
				Copy,
				Clone,
				Eq,
				PartialEq,
				Ord,
				PartialOrd,
				Hash,
			)]
			#[repr(transparent)]
			$struct_vis struct $t($inner_vis [u8; $l]);
		}

		impl codec::ConstEncodedLen for $t {}

		impl Default for $t {
			fn default() -> Self {
				Self([0; $l])
			}
		}

		impl core::ops::Deref for $t {
			type Target = [u8; $l];
			fn deref(&self) -> &Self::Target {
				&self.0
			}
		}

		impl core::ops::DerefMut for $t {
			fn deref_mut(&mut self) -> &mut Self::Target {
				&mut self.0
			}
		}

		impl From<[u8; $l]> for $t {
			fn from(value: [u8; $l]) -> Self {
				Self(value)
			}
		}
		impl AsRef<[u8; $l]> for $t {
			fn as_ref(&self) -> &[u8; $l] {
				&self.0
			}
		}
		impl AsMut<[u8; $l]> for $t {
			fn as_mut(&mut self) -> &mut [u8; $l] {
				&mut self.0
			}
		}
		impl AsMut<[u8]> for $t {
			fn as_mut(&mut self) -> &mut [u8] {
				&mut self.0[..]
			}
		}

		impl core::fmt::Debug for $t {
			fn fmt(&self, f: &mut core::fmt::Formatter) -> core::fmt::Result {
				if self.iter().all(|&x| (32..=127).contains(&x) || x == 0) {
					let i = self.0.iter().position(|x| *x == 0).unwrap_or($l);
					if let Ok(s) = core::str::from_utf8(&self.0[..i]) {
						return f.write_fmt(format_args!("\"{s}\""));
					}
				}
				if self.0.len() > 8 {
					f.write_fmt(format_args!(
						"0x{}...",
						$crate::hex::hex(&self.0[..8]),
					))
				} else {
					f.write_fmt(format_args!("0x{}", $crate::hex::hex(&self.0)))
				}
			}
		}

		impl core::fmt::Display for $t {
			fn fmt(&self, f: &mut core::fmt::Formatter) -> core::fmt::Result {
				if self.iter().all(|&x| (32..=127).contains(&x) || x == 0) {
					let i = self.0.iter().position(|x| *x == 0).unwrap_or($l);
					if let Ok(s) = core::str::from_utf8(&self.0[..i]) {
						return f.write_fmt(format_args!("\"{s}\""));
					}
				}
				if self.0.len() > 8 {
					f.write_fmt(format_args!(
						"0x{}...",
						$crate::hex::hex(&self.0[..8]),
					))
				} else {
					f.write_fmt(format_args!("0x{}", $crate::hex::hex(&self.0)))
				}
			}
		}

		impl $t {
			/// Create a new instance filled with zeroes.
			pub fn zero() -> Self {
				Self([0; $l])
			}
			/// Create a new instance from a slice.
			///
			/// The instance will be equal to the left portion of `left`, suffixed with zeroes in
			/// case `left` is too short.
			pub fn padded(left: &[u8]) -> Self {
				let mut i = [0; $l];
				i[..left.len().min($l)].copy_from_slice(&left[..left.len().min($l)]);
				Self(i)
			}
			/// Return the length of this data in bytes.
			pub const fn len() -> usize {
				$l
			}
			/// Return a [Vec]($crate::Vec) with the same data as this.
			#[allow(clippy::wrong_self_convention)]
			pub fn to_vec(&self) -> $crate::Vec<u8> {
				self.0.to_vec()
			}

			/// The length of this data.
			pub const LEN: usize = $l;
		}

		impl From<$t> for [u8; $l] {
			fn from(value: $t) -> [u8; $l] {
				value.0
			}
		}
		opaque! { $($rest)* }
	};
	(
		$(#[$($struct_meta:meta)*])*
		$struct_vis:vis
		struct $t:ident ($inner_vis:vis Vec<u8>);
		$($rest:tt)*
	) => {
		$crate::impl_serde_base64! {
			$(#[$($struct_meta)*])*
			#[derive(
				$crate::Encode, $crate::Decode, Default, Clone, Eq, PartialEq, Ord, PartialOrd,
			)]
			$struct_vis struct $t($inner_vis Vec<u8>);
		}

		impl AsRef<[u8]> for $t {
			fn as_ref(&self) -> &[u8] {
				&self.0[..]
			}
		}

		impl core::ops::Deref for $t {
			type Target = $crate::Vec<u8>;
			fn deref(&self) -> &Self::Target {
				&self.0
			}
		}

		impl core::ops::DerefMut for $t {
			fn deref_mut(&mut self) -> &mut Self::Target {
				&mut self.0
			}
		}

		impl From<$crate::Vec<u8>> for $t {
			fn from(value: $crate::Vec<u8>) -> Self {
				Self(value)
			}
		}

		impl $t {
			/// Create a new empty instance.
			pub fn new() -> Self {
				Self($crate::Vec::new())
			}
			/// Return the inner [Vec]($crate::Vec) value, consuming this instance.
			pub fn take(self) -> $crate::Vec<u8> {
				self.0
			}
		}

		impl From<$t> for $crate::Vec<u8> {
			fn from(other: $t) -> Self {
				other.0
			}
		}

		impl core::fmt::Debug for $t {
			fn fmt(&self, f: &mut core::fmt::Formatter) -> core::fmt::Result {
				core::fmt::Display::fmt(self, f)
			}
		}

		impl core::fmt::Display for $t {
			fn fmt(&self, f: &mut core::fmt::Formatter) -> core::fmt::Result {
				let suffix = self.iter().rev().take_while(|&&x| x == 0).count();
				let body = &self.0[..self.len() - suffix];
				if body.iter().all(|&x| (32..=127).contains(&x)) {
					if let Ok(s) = core::str::from_utf8(body) {
						return f.write_fmt(format_args!("\"{s}\""));
					}
				}
				if body.len() > 32 {
					f.write_fmt(format_args!(
						"0x{}...",
						$crate::hex::hex(&body[..8]),
					))?;
				} else {
					f.write_fmt(format_args!("0x{}", $crate::hex::hex(body)))?;
				}
				if suffix != 0 {
					f.write_fmt(format_args!("+{suffix}*0"))?;
				}
				Ok(())
			}
		}
		opaque! { $($rest)* }
	};
	(
		$(#[$($struct_meta:meta)*])*
		$struct_vis:vis
		struct $t:ident ($inner_vis:vis Bytes);
		$($rest:tt)*
	) => {
		$crate::impl_serde_base64! {
			$(#[$($struct_meta)*])*
			#[derive(
				$crate::Encode, $crate::Decode, Default, Clone, Eq, PartialEq, Ord, PartialOrd,
			)]
			$struct_vis struct $t($inner_vis Bytes);
		}

		impl AsRef<[u8]> for $t {
			fn as_ref(&self) -> &[u8] {
				&self.0[..]
			}
		}

		impl core::ops::Deref for $t {
			type Target = ::bytes::Bytes;
			fn deref(&self) -> &Self::Target {
				&self.0
			}
		}

		impl core::ops::DerefMut for $t {
			fn deref_mut(&mut self) -> &mut Self::Target {
				&mut self.0
			}
		}

		impl From<::bytes::Bytes> for $t {
			fn from(value: ::bytes::Bytes) -> Self {
				Self(value)
			}
		}

		impl $t {
			/// Create a new empty instance.
			pub fn new() -> Self {
				Self(::bytes::Bytes::new())
			}
			/// Return the inner [Bytes] value, consuming this instance.
			pub fn take(self) -> ::bytes::Bytes {
				self.0
			}
		}

		impl From<$t> for ::bytes::Bytes {
			fn from(other: $t) -> Self {
				other.0
			}
		}

		impl core::fmt::Debug for $t {
			fn fmt(&self, f: &mut core::fmt::Formatter) -> core::fmt::Result {
				core::fmt::Display::fmt(self, f)
			}
		}

		impl core::fmt::Display for $t {
			fn fmt(&self, f: &mut core::fmt::Formatter) -> core::fmt::Result {
				let suffix = self.iter().rev().take_while(|&&x| x == 0).count();
				let body = &self.0[..self.len() - suffix];
				if body.iter().all(|&x| (32..=127).contains(&x)) {
					if let Ok(s) = core::str::from_utf8(body) {
						return f.write_fmt(format_args!("\"{s}\""));
					}
				}
				if body.len() > 32 {
					f.write_fmt(format_args!(
						"0x{}...",
						$crate::hex::hex(&body[..8]),
					))?;
				} else {
					f.write_fmt(format_args!("0x{}", $crate::hex::hex(body)))?;
				}
				if suffix != 0 {
					f.write_fmt(format_args!("+{suffix}*0"))?;
				}
				Ok(())
			}
		}
		opaque! { $($rest)* }
	};
	(
		$(#[$($struct_meta:meta)*])*
		$struct_vis:vis
		struct $t:ident ($inner_vis:vis BoundedVec<u8, { $s:expr }>);
		$($rest:tt)*
	) => {
		$(#[$($struct_meta)*])*
		#[derive(
			$crate::Encode,
			$crate::Decode,
			$crate::MaxEncodedLen,
			Default,
			Clone,
			Eq,
			PartialEq,
			Ord,
			PartialOrd,
		)]
		$struct_vis struct $t($inner_vis $crate::BoundedVec<u8, ConstU32<{ $s as u32 }>>);

		impl core::ops::Deref for $t {
			type Target = $crate::BoundedVec<u8, ConstU32<{ $s as u32 }>>;
			fn deref(&self) -> &Self::Target {
				&self.0
			}
		}

		impl core::ops::DerefMut for $t {
			fn deref_mut(&mut self) -> &mut Self::Target {
				&mut self.0
			}
		}

		impl TryFrom<$crate::Vec<u8>> for $t {
			type Error = ();
			fn try_from(value: $crate::Vec<u8>) -> Result<Self, Self::Error> {
				Ok(Self($crate::BoundedVec::try_from(value).map_err(|_| ())?))
			}
		}

		impl From<$crate::BoundedVec<u8, ConstU32<{ $s as u32 }>>> for $t {
			fn from(value: $crate::BoundedVec<u8, ConstU32<{ $s as u32 }>>) -> Self {
				Self(value)
			}
		}

		impl core::fmt::Debug for $t {
			fn fmt(&self, f: &mut core::fmt::Formatter) -> core::fmt::Result {
				core::fmt::Display::fmt(self, f)
			}
		}

		impl core::fmt::Display for $t {
			fn fmt(&self, f: &mut core::fmt::Formatter) -> core::fmt::Result {
				if self.0.len() > 32 {
					f.write_fmt(format_args!(
						"0x{}...",
						$crate::hex::hex(&self.0[..8]),
					))
				} else {
					f.write_fmt(format_args!("0x{}", $crate::hex::hex(&self.0)))
				}
			}
		}
		opaque! { $($rest)* }
	};
	(
		$(#[$($struct_meta:meta)*])*
		$struct_vis:vis
		struct $t:ident ($inner_vis:vis FixedVec<u8, $l:ident>);
		$($rest:tt)*
	) => {
		$crate::impl_serde_base64! {
			$(#[$($struct_meta)*])*
			#[derive(
				$crate::Encode,
				$crate::Decode,
				Default,
				Clone,
				Eq,
				PartialEq,
			)]
			$struct_vis struct $t($inner_vis FixedVec<u8, $l>);
		}

		impl core::ops::Deref for $t {
			type Target = $crate::FixedVec<u8, $l>;
			fn deref(&self) -> &Self::Target {
				&self.0
			}
		}

		impl core::ops::DerefMut for $t {
			fn deref_mut(&mut self) -> &mut Self::Target {
				&mut self.0
			}
		}

		impl TryFrom<$crate::Vec<u8>> for $t {
			type Error = ();
			fn try_from(value: $crate::Vec<u8>) -> Result<Self, Self::Error> {
				Ok(Self($crate::FixedVec::try_from(value)?))
			}
		}

		impl From<$crate::FixedVec<u8, $l>> for $t {
			fn from(value: $crate::FixedVec<u8, $l>) -> Self {
				Self(value)
			}
		}

		impl From<$t> for $crate::FixedVec<u8, $l> {
			fn from(other: $t) -> Self {
				other.0
			}
		}

		impl core::fmt::Debug for $t {
			fn fmt(&self, f: &mut core::fmt::Formatter) -> core::fmt::Result {
				core::fmt::Display::fmt(self, f)
			}
		}

		impl core::fmt::Display for $t {
			fn fmt(&self, f: &mut core::fmt::Formatter) -> core::fmt::Result {
				let bytes = self.0.as_slice();
				if self.0.len() > 32 {
					f.write_fmt(format_args!(
						"0x{}...",
						$crate::hex::hex(&bytes[..8]),
					))
				} else {
					f.write_fmt(format_args!("0x{}", $crate::hex::hex(bytes)))
				}
			}
		}
		opaque! { $($rest)* }
	};
}
