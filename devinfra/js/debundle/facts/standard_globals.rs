//! The global properties a `predefined_globals` host may define before the
//! program runs (SPEC.md § Predefined globals): the `globals` package's lists
//! for ECMAScript, browser windows and workers and Node, plus
//! `%Object.prototype%`'s own properties, which every global object inherits,
//! and for `bun` Bun's own globals.

use std::collections::BTreeSet;
use std::sync::LazyLock;

use spec::PredefinedGlobals;

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

/// Bun's own global properties outside the standard lists: every string key
/// on Bun 1.x's `globalThis` and its prototype chain not in `NAMES`, as probed
/// on the pinned Claude Code executable (BuildBuddy invocation
/// e9adf31f-abf0-4cf0-94dd-b3af48eab689; gaffer-private
/// claude/cli/re/2.1.292/debug/2026-10-31-native-census-opaque-writes-audit.md).
const BUN: &[&str] = &[
    "Bun",
    "HTMLRewriter",
    "BuildError",
    "BuildMessage",
    "ResolveError",
    "ResolveMessage",
    "ShadowRealm",
];

/// Whether a `host` claiming its predefined globals may predefine a global
/// property named `name`.
pub(crate) fn is_predefined_global(name: &str, host: PredefinedGlobals) -> bool {
    is_standard_global(name) || host == PredefinedGlobals::Bun && BUN.contains(&name)
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
            assert!(
                is_predefined_global(name, PredefinedGlobals::Standard),
                "{name}"
            );
        }
        for name in [
            "_sentryDebugIds",
            "analyticsConnectorInstances",
            "experimentInstances",
        ] {
            assert!(
                !is_predefined_global(name, PredefinedGlobals::Standard),
                "{name}"
            );
        }
    }

    #[test]
    fn bun_globals_are_outside_the_standard_lists() {
        for name in BUN {
            assert!(!NAMES.contains(*name), "{name}");
        }
    }
}
