use debundle_e2e_support::*;

pub(super) const PROVIDER_CHUNK: &str = "static/provider";

/// An opaque call precedes later exported state: under the conservative
/// policy the call can reenter `readLater`, binding the two modules together.
pub(super) const PROVIDER: &str = r#"const prefix = new globalThis.Date();
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return "ready";
})({});
const later = dynamic;
class Registry { static current = later; }
export function readLater() { return Registry.current; }
export function loadHelper() { return import.meta.require("./helper.js"); }
"#;
pub(super) const HELPER: &str = "exports.suffix = \"!\";\n";
pub(super) const CONSUMER: &str = r#"import { readLater } from "./provider.js";
export { readLater };
"#;

pub(super) fn provider_modules() -> Vec<LogicalModuleEntry> {
    vec![
        logical_module("values/dynamic", &binding_members(["prefix", "dynamic"])),
        logical_module(
            "values/later",
            &binding_members(["later", "Registry", "readLater", "loadHelper"]),
        ),
    ]
}

pub(super) const EXTRA_CHUNKS: [(&str, &str); 2] =
    [("static/helper", HELPER), (PROVIDER_CHUNK, PROVIDER)];
