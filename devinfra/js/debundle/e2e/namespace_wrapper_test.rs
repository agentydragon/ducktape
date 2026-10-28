//! Neutral real-CLI reproducer for imported namespace wrapping.

use analysis::reports::schema::InferenceRule;
use debundle_e2e_support::*;
use std::collections::BTreeSet;

const WRAPPER: &str = r#"
const cache = new WeakMap();

export function wrap(value, includeDefault) {
  const isObject = value !== null && typeof value === "object";
  if (isObject) {
    const cached = cache.get(value);
    if (cached) return cached;
  }

  const namespace = Object.create(Object.getPrototypeOf(value));
  if (includeDefault) {
    Object.defineProperty(namespace, "default", { enumerable: true, value });
  }
  for (const name of Object.getOwnPropertyNames(value)) {
    if (!Object.prototype.hasOwnProperty.call(namespace, name)) {
      Object.defineProperty(namespace, name, {
        enumerable: true,
        get() { return value[name]; },
      });
    }
  }

  if (isObject) cache.set(value, namespace);
  return namespace;
}
"#;

#[test]
fn imported_wrapper_preserves_object_identity_and_defers_own_getters() {
    let source = r#"
import { wrap } from "./peer.js";

let getterCalls = 0;
const source = {
  label: "record",
  get delayed() { getterCalls++; return later; },
};

const first = wrap(source, true);
const second = wrap(source, true);
const getterCallsAfterWrap = getterCalls;
const later = "ready";

console.log(
  getterCallsAfterWrap,
  first === second,
  first.default === source,
  Object.getPrototypeOf(first) === Object.getPrototypeOf(source),
  Object.getOwnPropertyNames(first).sort().join(","),
  first.delayed,
  getterCalls,
);
"#;
    let files = [("static/peer.js", WRAPPER), ("static/app/peer.js", WRAPPER)];
    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            vec![
                logical_module(
                    "namespace/wrapped",
                    &[
                        Member::new("getterCalls"),
                        Member::new("source"),
                        Member::new("first"),
                        Member::new("second"),
                        Member::new("getterCallsAfterWrap"),
                    ],
                ),
                logical_module("values/later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[("static/peer", WRAPPER)])
        .with_extra_files(&files),
    );

    assert_preserved_entry_output(&fixture, "0 true true true default,delayed,label ready 1\n");
    let graph = fixture.dependency_graph();
    let first = graph
        .nodes
        .iter()
        .find(|node| {
            node.declared_bindings
                .iter()
                .any(|binding| binding.binding == "first")
        })
        .unwrap();
    let uses = &first.synchronous_execution.as_ref().unwrap().inference_uses;
    let wrapper = uses
        .iter()
        .find(|usage| usage.conclusion == InferenceRule::NamespaceWrapper)
        .unwrap();
    assert_eq!(
        wrapper.derivation_rules,
        BTreeSet::from([
            InferenceRule::NativeIntegrity,
            InferenceRule::NativeCallIdentity,
            InferenceRule::EmptyCollectionConstruction,
            InferenceRule::StableFunction,
            InferenceRule::NamespaceWrapper,
        ])
    );
    assert!(wrapper.value_input_refs.is_empty());
    let input = uses
        .iter()
        .find(|usage| usage.conclusion == InferenceRule::OwnPropertyPrefixState)
        .unwrap();
    assert_eq!(
        input.derivation_rules,
        BTreeSet::from([
            InferenceRule::OwnPropertySourceOrigin,
            InferenceRule::OwnPropertyShape,
            InferenceRule::OwnPropertyPrefixState,
        ])
    );
    assert!(input.value_input_refs.is_empty());
}

const SPLIT_SOURCE: &str = r#"
import { wrap } from "./peer.js";
const source = { get delayed() { return later; } };
const first = wrap(source, true);
const later = "ready";
console.log(first.delayed);
"#;

fn expect_split_rejected(source: &str, peer: &str) {
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            source,
            vec![
                logical_module("wrapped", &[Member::new("source"), Member::new("first")]),
                logical_module("later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[("static/peer", peer)]),
        &["wrapped", "later"],
        &[],
    );
}

#[test]
fn wrapper_source_rejects_extra_eager_getter_and_escaped_cache() {
    let eager = WRAPPER.replace(
        "  const namespace = Object.create",
        "  value.delayed;\n  const namespace = Object.create",
    );
    expect_split_rejected(SPLIT_SOURCE, &eager);
    let exposed_cache = format!("{WRAPPER}\nexport {{ cache }};\n");
    expect_split_rejected(SPLIT_SOURCE, &exposed_cache);
    let foreign_receiver = WRAPPER.replace("Object.", "foreignReceiver.");
    expect_split_rejected(SPLIT_SOURCE, &foreign_receiver);
    let replaced_before = WRAPPER.replace(
        "export function wrap",
        "cache.get = value => value.delayed;\nexport function wrap",
    );
    expect_split_rejected(SPLIT_SOURCE, &replaced_before);
    let replaced_after = format!("{WRAPPER}\ncache.get = value => value.delayed;\n");
    expect_split_rejected(SPLIT_SOURCE, &replaced_after);
}

#[test]
fn wrapper_preserves_eager_argument_reads_and_early_deferred_getters() {
    let argument_read = SPLIT_SOURCE.replace("wrap(source, true)", "wrap(source, (later, true))");
    expect_split_rejected(&argument_read, WRAPPER);
    let early_getter = SPLIT_SOURCE.replace(
        "const later = \"ready\";",
        "const observed = first.delayed;\nconst later = \"ready\";",
    );
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            &early_getter,
            vec![
                logical_module(
                    "wrapped",
                    &[
                        Member::new("source"),
                        Member::new("first"),
                        Member::new("observed"),
                    ],
                ),
                logical_module("later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[("static/peer", WRAPPER)]),
        &["wrapped", "later"],
        &[],
    );
}

#[test]
fn wrapper_rejects_proxy_input_and_mutated_native_call() {
    let proxy = SPLIT_SOURCE.replace(
        "const source = { get delayed() { return later; } };",
        "const source = new Proxy({ get delayed() { return later; } }, {});",
    );
    expect_split_rejected(&proxy, WRAPPER);
    let mutated = format!("Function.prototype.call = () => later;\n{SPLIT_SOURCE}");
    expect_split_rejected(&mutated, WRAPPER);
    let constructor_prototype = format!("Map.__proto__.call = () => later;\n{SPLIT_SOURCE}");
    expect_split_rejected(&constructor_prototype, WRAPPER);
    let aliased_prototype = format!(
        "const functionPrototype = Object.getPrototypeOf(Map);\nfunctionPrototype.call = () => later;\n{SPLIT_SOURCE}"
    );
    expect_split_rejected(&aliased_prototype, WRAPPER);
    let object_prototype = format!(
        "const objectPrototype = Object.getPrototypeOf(Math);\nobjectPrototype.hasOwnProperty = () => later;\n{SPLIT_SOURCE}"
    );
    expect_split_rejected(&object_prototype, WRAPPER);
    let dynamic_prototype =
        format!("const key = '__proto__';\nMap[key].call = () => later;\n{SPLIT_SOURCE}");
    expect_split_rejected(&dynamic_prototype, WRAPPER);
}

#[test]
fn wrapper_rejects_shadowed_object_receiver_inside_loop() {
    let shadowed = WRAPPER
        .replace("for (const name of", "for (const Object of")
        .replace("namespace, name)", "namespace, Object)")
        .replace("value[name]", "value[Object]");
    expect_split_rejected(SPLIT_SOURCE, &shadowed);
}

#[test]
fn wrapper_cached_alias_keeps_callbacks_installed_through_first_result() {
    let source = SPLIT_SOURCE.replace(
        "const later = \"ready\";",
        "first.read = () => later;\nconst second = wrap(source, true);\nconst observed = second.read();\nconst later = \"ready\";",
    );
    expect_atomic_conflict_rejection(
        FixtureOpts::new(
            &source,
            vec![
                logical_module(
                    "wrapped",
                    &[
                        Member::new("source"),
                        Member::new("first"),
                        Member::new("second"),
                        Member::new("observed"),
                    ],
                ),
                logical_module("later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[("static/peer", WRAPPER)]),
        &["wrapped", "later"],
        &[],
    );
}

#[test]
fn wrapper_call_before_provider_cache_initializes_preserves_tdz() {
    let peer = WRAPPER.replace(
        "const cache = new WeakMap();",
        "export let early;\ntry { wrap({}, true); } catch (error) { early = error.name; }\nconst cache = new WeakMap();",
    );
    let source = r#"
import { wrap, early } from "./peer.js";
const source = { get delayed() { return later; } };
const first = wrap(source, true);
const later = "ready";
console.log(early, first.delayed);
"#;
    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            vec![
                logical_module("wrapped", &[Member::new("source"), Member::new("first")]),
                logical_module("later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[("static/peer", &peer)]),
    );
    assert_preserved_entry_output(&fixture, "ReferenceError ready\n");
}

#[test]
fn imported_wrapper_called_during_provider_cycle_preserves_cache_tdz() {
    let peer = WRAPPER.replace(
        "const cache = new WeakMap();",
        "import './reenter.js';\nconst cache = new WeakMap();",
    );
    let reenter = r#"
import { wrap } from './peer.js';
try { wrap({}, true); globalThis.early = "missed"; }
catch (error) { globalThis.early = error.name; }
"#;
    let source = r#"
import { wrap } from "./peer.js";
const source = { get delayed() { return later; } };
const first = wrap(source, true);
const later = "ready";
console.log(globalThis.early, first.delayed);
"#;
    let files = [
        ("static/app/peer.js", peer.as_str()),
        ("static/app/reenter.js", reenter),
    ];
    let fixture = run_fixture(
        FixtureOpts::new(
            source,
            vec![
                logical_module("wrapped", &[Member::new("source"), Member::new("first")]),
                logical_module("later", &[Member::new("later")]),
            ],
        )
        .with_extra_chunks(&[("static/peer", &peer), ("static/reenter", reenter)])
        .with_extra_files(&files),
    );
    assert_preserved_entry_output(&fixture, "ReferenceError ready\n");
}
