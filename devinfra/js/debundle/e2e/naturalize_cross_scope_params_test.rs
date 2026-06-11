//! Regression fixture for #2045: selected-module lowering must keep
//! naturalizing function params/locals into readable names even when
//! several sibling scopes reuse the same minified spelling.
//!
//! The heuristic naturalizer aggregates per-module candidate renames
//! into one flat string-keyed map. Two independent scopes that derive
//! renames for the same minified source name (here `e` → `value` in
//! one function, `e` → `registry` in a constructor, `e` → `payload`
//! in a third function) overwrite each other during collection, so
//! only the last contributor's scope keeps its readable name and the
//! others silently fall back to the minified spelling. The renames
//! are scope-local by construction — each applies only inside the
//! deriving function/constructor — so per-scope application is safe
//! and all three names should survive.
//!
//! The fixture also pins the behavior that must NOT change:
//! - a free (import-alias) return-object rename still flows through
//!   the runtime-reimport bridge (`import { t as options }`), and
//! - a shadowed binding reusing the same minified spelling (`s(t)`)
//!   is not rewritten by that import-alias rename.

use debundle_e2e_support::*;

const PROVIDER_SOURCE: &str = r#"export const e = (x) => x + 1;
export const t = { kind: "reg" };
export const r = (x) => ({ size: x });
"#;

const CHUNK_SOURCE: &str = r#"import { e, t, r } from "./provider.js";
function a({ value: e }) {
  return e + 1;
}
function b(e) {
  const n = r(e);
  return { item: n };
}
class C {
  constructor(e) {
    this.registry = e;
  }
}
function d() {
  return { options: t };
}
function s(t) {
  return t * 2;
}
function g({ payload: e }) {
  return e.length;
}
console.log(a({ value: 1 }), b(2).item.size, new C(3).registry, d().options.kind, s(4), g({ payload: "xy" }));
export { a, b, C, d, s, g };
"#;

#[test]
fn naturalizes_params_per_scope_despite_shared_minified_spellings() {
    let mut opts = FixtureOpts::new(
        CHUNK_SOURCE,
        vec![logical_module(
            "helpers/shapes",
            &[
                Member::renamed("pair", "a"),
                Member::renamed("makeItem", "b"),
                Member::renamed("Registry", "C"),
                Member::renamed("makeOptions", "d"),
                Member::renamed("scale", "s"),
                Member::renamed("unwrapPayload", "g"),
            ],
        )],
    );
    opts.extra_files = &[("static/app/provider.js", PROVIDER_SOURCE)];
    let fixture = run_fixture(opts);

    // The emitted tree must still run with unchanged behavior.
    assert_entry_output(&fixture, "2 2 3 reg 8 2\n");

    let module_path = "static/app/modules/helpers/shapes.js";

    // Scope-local naturalization: every deriving scope keeps its own
    // readable name even though all three derive from the spelling `e`.
    assert_module_source(
        &fixture.out_root,
        module_path,
        &[
            "pair({ value })",
            "return value + 1",
            "constructor(registry)",
            "this.registry = registry",
            "unwrapPayload({ payload })",
            "return payload.length",
            "const item = ",
        ],
        &[
            // No scope may regress to the minified spelling.
            "value: e",
            "registry: e",
            "payload: e",
            "constructor(e)",
            "item: n",
        ],
    );

    // The free import-alias rename still flows through the
    // runtime-reimport bridge: the moved body reads `options`, so the
    // module re-imports the original `t` under the readable alias.
    assert_module_source(
        &fixture.out_root,
        module_path,
        &["t as options", "options", "provider.js"],
        &["options: t"],
    );

    // Negative case: `s`'s parameter re-binds the import alias's
    // minified spelling `t`; the import-alias rename (`t` → `options`)
    // must not rewrite the shadowed binding or its references.
    assert_module_source(
        &fixture.out_root,
        module_path,
        &["scale(t)", "return t * 2"],
        &["scale(options)", "return options * 2"],
    );
}
