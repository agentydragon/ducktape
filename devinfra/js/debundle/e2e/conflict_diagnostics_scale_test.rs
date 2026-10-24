//! Independent atomic conflicts must not each scan every unrelated graph edge.

use debundle_e2e_support::*;
use spec::{BindingSelector, Member as SpecMember, MemberSelector};
use std::fmt::Write as _;

#[test]
fn many_independent_conflicts_keep_their_diagnostics() {
    let mut source = String::new();
    let mut modules = [logical_module("left", &[]), logical_module("right", &[])];
    for index in 0..6656 {
        writeln!(
            source,
            "let left{index} = 0; let right{index} = 0; \
             function factory{index}() {{ left{index}++; right{index}++; }}"
        )
        .unwrap();
        for (module, prefix) in modules.iter_mut().zip(["left", "right"]) {
            let name = format!("{prefix}{index}");
            module.1.members.push(SpecMember {
                name: Some(name.clone()),
                selector: MemberSelector {
                    binding: Some(BindingSelector { name, kind: None }),
                    ..Default::default()
                },
            });
        }
    }
    for index in 0..128 {
        write!(source, "function reader{index}() {{ return [").unwrap();
        for binding in 0..6656 {
            write!(source, "left{binding},").unwrap();
        }
        source.push_str("]; }\n");
    }
    source.push_str("console.log(left0);\n");
    expect_atomic_conflict_rejection(
        FixtureOpts::new(&source, modules.into()).with_unassigned_mode(unassigned_mode_inline()),
        &["left", "right"],
        &[],
    );
}
