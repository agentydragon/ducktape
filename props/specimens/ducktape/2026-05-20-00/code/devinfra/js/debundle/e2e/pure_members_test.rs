//! End-to-end pinning for the per-member `pure_members:
//! [<prop>, …]` spec annotation.
//!
//! The annotation extends `purity: pure` from "calls of the bound
//! Ident classify pure" to "calls of `<binding>.<prop>(...)` for
//! each listed property classify pure". Targets the
//! vendor-namespace shape — a star-import or renamed binding
//! standing in for a vendor module (React, etc.), where member
//! calls (`React.forwardRef`, `React.memo`, `React.lazy`,
//! `React.createContext`) are author-asserted side-effect-free
//! when their arguments classify pure.
//!
//! Same author-trust contract as `purity: pure`: the validator does
//! not re-verify; soundness shifts to the spec author. See
//! AGENTS.md "Declared purity".

use debundle_e2e_support::*;
use serde_json::json;

/// Vendor-namespace fixture: a star-import binding `ns` whose
/// `ns.makePure(...)` call site would force a side-effect-order
/// edge under default classification (`ns.makePure` is a member call
/// on an unknown receiver). With `pure_members: [makePure]` on the
/// `ns` member, the analyzer admits the call as pure, drops the
/// `S` edge, and the cycle-forcing fixture accepts.
///
/// Cycle-forcing layout:
///   1. `const a = (() => 1)();` — SE; stays in residual
///   2. `const b = ns.makePure({ value: 1 });` — would be SE
///      without the rule; peeled to b_module
///   3. `const c = b.value + a;` — reads b at init
///   4. `console.log(c);`
///
/// Without the rule: `ns.makePure(...)` is Unknown → `b` is SE →
/// `b → a` s-edge → after peeling `b` to b_module, that crosses
/// `b_module → residual`. Combined with `residual → b_module`
/// (`c` reads `b` at init), the spec is unrealizable.
///
/// With the rule: `ns.makePure(...)` is Pure → `b` is not SE →
/// no `b → a` s-edge. Only `residual → b_module` remains. DAG.
#[test]
fn pure_members_admits_namespace_member_call() {
    let opts = FixtureOpts {
        source: r#"import * as ns from "./vendor.js";
const a = (() => 1)();
const b = ns.makePure({ value: 1 });
const c = b.value + a;
console.log(c);
export { a, b, c };
"#,
        logical_modules: vec![logical_module("b_module", &[Member::new("b")])],
        chunk_renames: Some(json!({
            "id": "chunk_renames__static_app",
            "members": [
                {
                    "name": "React",
                    "selector": {
                        "binding": {
                            "name": "ns",
                            "kind": "import_specifier",
                        },
                    },
                    "pure_members": ["makePure"],
                },
            ],
        })),
        chunk_id: "static/app",
        unassigned_mode: unassigned_mode_catchall_file(None),
        dataflow_aware_s_chain: false,
        extra_files: &[(
            "static/app/vendor.js",
            "export function makePure(arg) { return arg; }\n",
        )],
    };
    let fixture = run_fixture(opts);

    // Peel succeeded: b is in b_module without dragging a.
    // The fact that the build didn't error on a cycle proves
    // that `ns.makePure(...)` was classified Pure.
    assert_module_source(
        &fixture.out_root,
        "static/app/modules/b_module.js",
        &["const b = "],
        &["const a", "(()=>1)"],
    );

    // Behaviour preserved: c == b.value + a == 1 + 1 == 2.
    assert_entry_output(&fixture, "2\n");
}

/// Companion negative: the same fixture shape but the spec lists
/// a different property name. The actual `ns.makePure(...)` call
/// stays Unknown (the spec didn't annotate `makePure`), the
/// side-effect cycle closes, and the validator rejects. Pins that
/// the annotation doesn't bleed onto sibling properties.
#[test]
fn pure_members_does_not_bleed_to_other_props() {
    expect_rejection_containing_all(
        FixtureOpts {
            source: r#"import * as ns from "./vendor.js";
const a = (() => 1)();
const b = ns.makePure({ value: 1 });
const c = b.value + a;
console.log(c);
export { a, b, c };
"#,
            logical_modules: vec![logical_module("b_module", &[Member::new("b")])],
            chunk_renames: Some(json!({
                "id": "chunk_renames__static_app",
                "members": [
                    {
                        "name": "React",
                        "selector": {
                            "binding": {
                                "name": "ns",
                                "kind": "import_specifier",
                            },
                        },
                        // Annotated property is not the one called.
                        "pure_members": ["somethingElse"],
                    },
                ],
            })),
            chunk_id: "static/app",
            unassigned_mode: unassigned_mode_catchall_file(None),
            dataflow_aware_s_chain: false,
            extra_files: &[(
                "static/app/vendor.js",
                "export function makePure(arg) { return arg; }\n",
            )],
        },
        &["cycle", "b_module", "residual"],
    );
}

/// Args are still classified independently of the spec hint — the
/// declared-purity contract covers the function value, not the
/// arguments. An impure arg (an IIFE call here) keeps the call
/// classified impure, the cycle closes, and the validator rejects.
#[test]
fn pure_members_call_with_impure_arg_does_not_admit() {
    expect_rejection_containing_all(
        FixtureOpts {
            source: r#"import * as ns from "./vendor.js";
const a = (() => 1)();
const b = ns.makePure((() => globalThis.touched = 1)());
const c = (b ? 1 : 0) + a;
console.log(c);
export { a, b, c };
"#,
            logical_modules: vec![logical_module("b_module", &[Member::new("b")])],
            chunk_renames: Some(json!({
                "id": "chunk_renames__static_app",
                "members": [
                    {
                        "name": "React",
                        "selector": {
                            "binding": {
                                "name": "ns",
                                "kind": "import_specifier",
                            },
                        },
                        "pure_members": ["makePure"],
                    },
                ],
            })),
            chunk_id: "static/app",
            unassigned_mode: unassigned_mode_catchall_file(None),
            dataflow_aware_s_chain: false,
            extra_files: &[(
                "static/app/vendor.js",
                "export function makePure(arg) { return arg; }\n",
            )],
        },
        &["cycle", "b_module", "residual"],
    );
}
