//! The global properties a `predefined_globals: standard` host may define
//! before the program runs (SPEC.md § Predefined globals): the `globals`
//! package's lists for ECMAScript, browser windows and workers and Node, plus
//! `%Object.prototype%`'s own properties, which every global object inherits.

use std::collections::BTreeSet;
use std::sync::LazyLock;

/// The `globals` environments a standard host draws from; every ECMAScript
/// edition list (`es*`) is included too.
const ENVIRONMENTS: &[&str] = &[
    "builtin",
    "browser",
    "worker",
    "serviceworker",
    "sharedWorker",
    "audioWorklet",
    "paintWorklet",
    "node",
    "nodeBuiltin",
    "commonjs",
    "shared-node-browser",
];

/// ECMA-262 § Properties of the Object Prototype Object, with Annex B.
const OBJECT_PROTOTYPE: &[&str] = &[
    "constructor",
    "hasOwnProperty",
    "isPrototypeOf",
    "propertyIsEnumerable",
    "toLocaleString",
    "toString",
    "valueOf",
    "__proto__",
    "__defineGetter__",
    "__defineSetter__",
    "__lookupGetter__",
    "__lookupSetter__",
];

static NAMES: LazyLock<BTreeSet<String>> = LazyLock::new(|| {
    let lists: serde_json::Map<String, serde_json::Value> =
        serde_json::from_str(include_str!(env!("STANDARD_GLOBALS_JSON")))
            .expect("the globals package's globals.json is an object of environments");
    let mut names: BTreeSet<String> = OBJECT_PROTOTYPE
        .iter()
        .map(|name| (*name).to_owned())
        .collect();
    for (environment, globals) in &lists {
        if !(ENVIRONMENTS.contains(&environment.as_str()) || environment.starts_with("es")) {
            continue;
        }
        let globals = globals
            .as_object()
            .unwrap_or_else(|| panic!("globals environment {environment} is an object"));
        names.extend(globals.keys().cloned());
    }
    names
});

/// Whether a standard host may predefine a global property named `name`.
pub fn is_standard_global(name: &str) -> bool {
    NAMES.contains(name)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_environment_is_listed_and_names_are_host_globals() {
        let lists: serde_json::Map<String, serde_json::Value> =
            serde_json::from_str(include_str!(env!("STANDARD_GLOBALS_JSON"))).unwrap();
        for environment in ENVIRONMENTS {
            assert!(lists.contains_key(*environment), "{environment}");
        }
        for name in [
            "Object",
            "window",
            "self",
            "global",
            "location",
            "process",
            "hasOwnProperty",
        ] {
            assert!(is_standard_global(name), "{name}");
        }
        for name in [
            "_sentryDebugIds",
            "analyticsConnectorInstances",
            "experimentInstances",
        ] {
            assert!(!is_standard_global(name), "{name}");
        }
    }
}
