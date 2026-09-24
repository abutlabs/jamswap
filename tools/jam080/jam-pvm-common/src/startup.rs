pub fn load_protocol_parameters() {
	use crate::host_calls::fetch_wrappers::protocol_parameters;
	let params = protocol_parameters();
	params.apply().expect("Protocol parameters are invalid");
}
