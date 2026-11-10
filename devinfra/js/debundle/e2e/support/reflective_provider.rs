//! A provider chunk whose exported helper writes through an unknown receiver's
//! prototype, so the program census keeps an unknown-receiver mutation witness.

use serde_json::Value;

pub const REFLECT_SOURCE: &str = "export function reflect(receiver, key) { Object.defineProperty(Object.getPrototypeOf(receiver), key, { value: 'changed', configurable: true }); }\n";
pub const REFLECT_CHUNKS: &[(&str, &str)] = &[("static/reflect", REFLECT_SOURCE)];
pub const REFLECT_FILES: &[(&str, &str)] = &[("static/reflect.js", REFLECT_SOURCE)];

/// The serialized program graph's witness for the helper's unknown-receiver write.
pub fn unknown_receiver_witness(program_graph: &Value) -> &Value {
    program_graph["native_integrity_witnesses"]
        .as_array()
        .expect("native mutation witnesses")
        .iter()
        .find(|witness| {
            witness["operation"] == "mutation_call"
                && witness["receiver_roots"]
                    .as_array()
                    .is_some_and(|roots| roots.iter().any(|root| root == "unknown_global_value"))
                && witness["source_location"]["source_path"] == "static/reflect.js"
        })
        .unwrap_or_else(|| {
            panic!("reflective helper's unknown receiver is absent: {program_graph}")
        })
}
