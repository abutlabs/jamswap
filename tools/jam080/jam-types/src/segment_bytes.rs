use crate::{AnyBytes, Segment, SEGMENT_LEN};
use alloc::{vec, vec::Vec};
use bytes::Bytes;

/// Zero-copy segment wrapper.
///
/// The segment is immutable and its length is fixed.
#[derive(Clone, PartialEq, Eq)]
pub struct SegmentBytes(Bytes);

impl SegmentBytes {
	pub fn as_array(&self) -> &[u8; SEGMENT_LEN] {
		(&self.0[..]).try_into().expect("Segment length is fixed")
	}

	pub fn as_slice(&self) -> &[u8] {
		&self.0[..]
	}

	pub fn as_bytes(&self) -> &Bytes {
		&self.0
	}

	pub fn into_inner(self) -> Bytes {
		self.0
	}
}

impl Default for SegmentBytes {
	fn default() -> Self {
		Self(vec![0_u8; SEGMENT_LEN].into())
	}
}

impl core::ops::Deref for SegmentBytes {
	type Target = [u8];

	fn deref(&self) -> &Self::Target {
		&self.0[..]
	}
}

impl AsRef<[u8; SEGMENT_LEN]> for SegmentBytes {
	fn as_ref(&self) -> &[u8; SEGMENT_LEN] {
		self.as_array()
	}
}

impl AsRef<[u8]> for SegmentBytes {
	fn as_ref(&self) -> &[u8] {
		self.as_slice()
	}
}

impl From<Segment> for SegmentBytes {
	fn from(other: Segment) -> Self {
		Self(other.into_vec().into())
	}
}

impl From<&[u8; SEGMENT_LEN]> for SegmentBytes {
	fn from(other: &[u8; SEGMENT_LEN]) -> Self {
		Self(other.to_vec().into())
	}
}

impl TryFrom<Vec<u8>> for SegmentBytes {
	type Error = ();

	fn try_from(other: Vec<u8>) -> Result<Self, Self::Error> {
		if other.len() != SEGMENT_LEN {
			return Err(());
		}
		Ok(Self(other.into()))
	}
}

impl TryFrom<Bytes> for SegmentBytes {
	type Error = ();

	fn try_from(other: Bytes) -> Result<Self, Self::Error> {
		if other.len() != SEGMENT_LEN {
			return Err(());
		}
		Ok(Self(other))
	}
}

impl From<SegmentBytes> for Bytes {
	fn from(other: SegmentBytes) -> Self {
		other.0
	}
}

impl core::fmt::Debug for SegmentBytes {
	fn fmt(&self, f: &mut core::fmt::Formatter) -> core::fmt::Result {
		core::fmt::Debug::fmt(&AnyBytes(self.0.clone()), f)
	}
}

impl core::fmt::Display for SegmentBytes {
	fn fmt(&self, f: &mut core::fmt::Formatter) -> core::fmt::Result {
		core::fmt::Debug::fmt(self, f)
	}
}

#[cfg(feature = "serde")]
impl serde::Serialize for SegmentBytes {
	fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
	where
		S: serde::Serializer,
	{
		AnyBytes(self.0.clone()).serialize(serializer)
	}
}

#[cfg(feature = "serde")]
impl<'a> serde::Deserialize<'a> for SegmentBytes {
	fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
	where
		D: serde::Deserializer<'a>,
	{
		let AnyBytes(bytes) = AnyBytes::deserialize(deserializer)?;
		bytes
			.try_into()
			.map_err(|_| serde::de::Error::custom("Invalid SegmentBytes length"))
	}
}

#[cfg(all(feature = "serde", test))]
mod tests {
	use super::*;

	#[test]
	fn segment_base64_works() {
		let segment = SegmentBytes::from(&[123_u8; SEGMENT_LEN]);
		let string = serde_json::to_string(&segment).unwrap();
		let actual: SegmentBytes = serde_json::from_str(&string).unwrap();
		assert_eq!(segment, actual);
	}
}
