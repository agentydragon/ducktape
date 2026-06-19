//! The `@Name` cross-reference primitives (`selector_solve`'s `referencer_for`
//! / `alias_owner_for`) must resolve the metaNode debt shapes — a shapeless
//! delegator pinned by what it references, a re-export pinned by what it
//! aliases — on an `owner_graph.json` the real pipeline emits, not a synthetic
//! EDB. The real graph is the discriminating case the synthetic unit tests
//! miss: the pipeline models `export { ... }` and side-effect statements as
//! owners that reference every binding they touch, so the cross-ref primitive
//! resolves categorically only because it counts references from *declaring*
//! owners (the `declares` conjunct in the `references` rule). See
//! plans/selector_constraint_model.md.

use std::fs;

use debundle_e2e_support::*;
use selector_solve::solve_str;

#[test]
fn cross_reference_primitives_resolve_metanode_shapes_on_real_graph() {
    // EBt is a leaf; UBt is a shapeless delegator that calls it; UJ is a class;
    // HI re-exports UJ via `const HI = UJ`. All four are exported (so the export
    // owner references all four) and exercised (so the side-effect statement
    // references them) — exactly the consumer noise the primitive must see past.
    let fixture = run_fixture(FixtureOpts::new(
        r#"function EBt(x) { return x + 1; }
function UBt(x) { return EBt(x); }
class UJ { tag() { return "uj"; } }
const HI = UJ;
console.log(UBt(41), new HI().tag());
export { EBt, UBt, UJ, HI };
"#,
        vec![logical_module(
            "shapes",
            &[
                Member::new("EBt"),
                Member::new("UBt"),
                Member::new("UJ"),
                Member::new("HI"),
            ],
        )],
    ));
    assert_entry_output(&fixture, "42 uj\n");

    let text = fs::read_to_string(fixture.report_root.join("static/app/owner_graph.json"))
        .expect("emitted owner_graph.json");
    let r = solve_str(&text).expect("solve owner graph");

    let owner = |name: &str| -> u32 {
        match r.name_to_owners.get(name).map(Vec::as_slice) {
            Some([o]) => *o,
            other => panic!("expected {name} to resolve to one owner, got {other:?}"),
        }
    };

    // The delegator is pinned by what it references, not by its own minified
    // name: among *declaring* owners, only UBt references EBt — the export and
    // side-effect owners reference EBt too but declare nothing, so the
    // categoricity gate would otherwise see two referencers and resolve nothing.
    assert_eq!(r.referencer_for("EBt"), Some(owner("UBt")));

    // The same pin disambiguated by kind, as a real selector would write it
    // ("the *function* that calls @EBt"); a wrong kind resolves nothing.
    assert_eq!(r.referencer_of_kind("EBt", "fn_decl"), Some(owner("UBt")));
    assert_eq!(r.referencer_of_kind("EBt", "class_decl"), None);

    // The re-export is pinned by what it aliases: HI is the unique var-decl
    // aliasing UJ.
    assert_eq!(r.alias_owner_for("UJ"), Some(owner("HI")));

    // Consumer-exclusion: UBt is referenced only by owners that declare nothing
    // (the export statement), so no *entity* references it — it does not resolve
    // as a cross-ref target. This is the property the `declares` conjunct buys.
    assert_eq!(r.referencer_for("UBt"), None);
}
