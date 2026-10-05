//! Regression test for the namespace-aggregator TDZ hole.
//!
//! ## Shape
//!
//! A "namespace aggregator" is a module-top object built from
//! object-spread of submodule namespace objects:
//!
//! ```js
//! export const ids = { ...sub1, ...sub2 };
//! ```
//!
//! When the spec splits this into three modules (one for the
//! aggregator and one each per sub), the aggregator's body issues an
//! at-init read of `sub1` and `sub2`. ESM Phase-2 evaluates `sub1`
//! and `sub2` before `aggregator` because they are imports.
//!
//! The hazard: when a sub-module ALSO reads a binding from the residual
//! at-init, and the residual reads the aggregator's `ids` at-init,
//! the resulting cycle is:
//!
//! ```text
//! residual --EagerUse--> aggregator (ids)
//! aggregator --EagerUse--> sub1     (...sub1)
//! sub1 --EagerUse--> residual       (helperFromResidual)
//! ```
//!
//! ESM DFS from residual:
//!
//! 1. residual on stack, recurse into aggregator.
//! 2. aggregator on stack, recurse into sub1.
//! 3. sub1 on stack, recurse into residual — already on stack;
//!    don't recurse.
//! 4. Done with sub1 deps → evaluate sub1 body. Sub1 reads
//!    `helperFromResidual` — residual has NOT yet run its
//!    `const helperFromResidual = ...` line. TDZ on the residual
//!    binding.
//!
//! For the aggregator-specific shape (this test), the same fix
//! family catches a closely related TDZ: a back-edge from a sub to
//! the aggregator's destination module. The cycle the gate must see:
//!
//! ```text
//! aggregator-module --EagerUse--> sub1-module
//! sub1-module --EagerUse--> aggregator-module (via residual)
//! ```
//!
//! ## Expected outcome
//!
//! The realizability gate sees the aggregator's eager read of the
//! sub-modules as a constraining edge that participates in the cycle, and
//! rejects the spec (accepting it would emit a bundle that throws
//! `ReferenceError: Cannot access 'ids' before initialization`).

use debundle_e2e_support::*;

// `helperFromResidual` lives in the residual chunk (entry) and is
// read at-init by both `sub1`'s and `sub2`'s initializers. The
// aggregator `ids` is read at-init from the residual's
// `const consumed = ids.foo;` line. The fixture below produces the
// cycle: residual ↔ aggregator (via sub1 → helperFromResidual).
const NAMESPACE_AGGREGATOR_SOURCE: &str = r#"const helperFromResidual = "H";
const sub1 = { foo: helperFromResidual + "1" };
const sub2 = { bar: helperFromResidual + "2" };
const ids = { ...sub1, ...sub2 };
const consumed = ids.foo + "|" + ids.bar;
console.log(consumed);
export { ids, sub1, sub2, helperFromResidual };
"#;

fn opts_for_fixture() -> FixtureOpts<'static> {
    let mut opts = FixtureOpts::new(
        NAMESPACE_AGGREGATOR_SOURCE,
        vec![
            logical_module("ids/index", &[Member::new("ids")]),
            logical_module("ids/sub1", &[Member::new("sub1")]),
            logical_module("ids/sub2", &[Member::new("sub2")]),
        ],
    );
    opts.unassigned_mode = unassigned_mode_inline();
    opts
}

/// The gate rejects the split and the diagnostic does binding-pair blame, not
/// just module-list rendering: `cycles.json` carries the cut edges and the
/// `render_cycle_summary` rows name their `from_binding` and `binding`, so
/// spec authors can act directly: "move {X, Y} into one module."
///
/// The cut is a minimum feedback arc set over the constraining-edge
/// view, so only the bindings on the edges FAS picks as back-edges
/// are guaranteed to appear; for this fixture FAS picks the
/// `consumed → ids` back-edge from residual into the aggregator
/// (it breaks both `…→sub1→residual` and `…→sub2→residual`
/// cycles with a single arc). Assert the `ids` aggregator name; the
/// smaller cut is by design.
#[test]
fn namespace_aggregator_diagnostic_blames_binding_pairs() {
    let rejected = run_rejection_fixture(opts_for_fixture());
    // The aggregator binding is the target of the cut's back-edge.
    let cycles = rejected.cycles();
    let edge = cycles
        .iter()
        .flat_map(|scc| scc["cut"].as_array().expect("SCC cut"))
        .find(|edge| edge["binding"] == "ids")
        .unwrap_or_else(|| panic!("no cut edge targets `ids`: {cycles:#?}"));
    // The summary renders the edge as a binding pair.
    for end in ["from_binding", "binding"] {
        let binding = edge[end].as_str().expect("edge binding");
        assert!(
            rejected.stderr.contains(binding),
            "summary does not name `{binding}`:\n{}",
            rejected.stderr
        );
    }
}
