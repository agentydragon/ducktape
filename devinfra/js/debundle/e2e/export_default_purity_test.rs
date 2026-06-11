//! Soundness regression: `classify_item` maps non-`ExportDecl`
//! module declarations to `StatementKind::Export`, and `item_purity`
//! mapped every `Export` to `Pure` — so `export default sideEffect()`
//! and `export default class C { static { ... } }` dropped out of the
//! S-chain entirely. Statements in other modules could then be
//! ordered around the default export's side effect, reordering the
//! bundle's observable init sequence.
//!
//! The fix routes `ExportDefaultExpr` through `classify_expr_purity`
//! and `ExportDefaultDecl(Class)` through
//! `class_has_static_observable`.

use debundle_e2e_support::*;

/// `export default (globalThis.seq += "D")` is impure. With the S
/// edge restored, `after` (mod_z) is sequenced after the default
/// export, which lives in entry — entry evaluates last under ESM, so
/// the spec is unrealizable and must be rejected. Before the fix the
/// default export was "pure": the spec was accepted and the emitted
/// bundle printed `SZD` instead of the original `SDZ`.
#[test]
fn impure_export_default_expr_restores_s_chain_ordering() {
    expect_rejection(
        FixtureOpts::new(
            r#"const init = (globalThis.seq = "S", "i");
export default (globalThis.seq = globalThis.seq + "D");
const after = (globalThis.seq = globalThis.seq + "Z", "z");
console.log(globalThis.seq, init, after);
export { init, after };
"#,
            vec![
                logical_module("mod_i", &[Member::new("init")]),
                logical_module("mod_z", &[Member::new("after")]),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
        &["cycle", "unrealizable"],
    );
}

/// Same shape with `export default class` carrying an observable
/// static block.
#[test]
fn export_default_class_with_static_block_restores_s_chain_ordering() {
    expect_rejection(
        FixtureOpts::new(
            r#"const init = (globalThis.seq = "S", "i");
export default class Boot {
  static {
    globalThis.seq = globalThis.seq + "C";
  }
}
const after = (globalThis.seq = globalThis.seq + "Z", "z");
console.log(globalThis.seq, init, after);
export { init, after };
"#,
            vec![
                logical_module("mod_i", &[Member::new("init")]),
                logical_module("mod_z", &[Member::new("after")]),
            ],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
        &["cycle", "unrealizable"],
    );
}

/// Green companion: with only the *earlier* impure statement peeled
/// off, the restored S edge (`default → init`) is satisfiable
/// (mod_i before entry) and the bundle preserves the original
/// `SCZ` sequence.
#[test]
fn export_default_class_static_block_ordering_is_preserved_when_realizable() {
    let fixture = run_fixture(
        FixtureOpts::new(
            r#"const init = (globalThis.seq = "S", "i");
export default class Boot {
  static {
    globalThis.seq = globalThis.seq + "C";
  }
}
const after = (globalThis.seq = globalThis.seq + "Z", "z");
console.log(globalThis.seq, init, after);
export { init, after };
"#,
            vec![logical_module("mod_i", &[Member::new("init")])],
        )
        .with_unassigned_mode(unassigned_mode_inline()),
    );
    assert_entry_output(&fixture, "SCZ i z\n");
}
