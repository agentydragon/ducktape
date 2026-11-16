//! Relocation depends on the ordinary array transfer for both local and
//! provider-owned receivers and callbacks. The negative moves a real provider
//! mutation before a snapshot and must remain refused.

use debundle_e2e_graph_support::effect_validation::binding_execution_report;
use debundle_e2e_support::*;

const PROVIDER: &str = "export const values = ['a'];\n";

#[test]
fn local_and_imported_iteration_calls_relocate_past_an_independent_iteration() {
    for (prefix, callback, members) in [
        (
            "const values = ['a'];",
            "value => value",
            vec![Member::new("values"), Member::new("mapped")],
        ),
        (
            "import { values } from './provider.js';",
            "value => value",
            vec![Member::new("mapped")],
        ),
        (
            "import { values, keep } from './provider.js';",
            "keep",
            vec![Member::new("mapped")],
        ),
        (
            "import { values } from './relay.js';\nimport { keep } from './provider.js';",
            "keep",
            vec![Member::new("mapped")],
        ),
    ] {
        let source = format!(
            "{prefix}\nconst first = values.map(value => value + '?');\nconst mapped = values.map({callback});\nconsole.log(first.join(','), mapped.join(','));\n"
        );
        let fixture = ProgramPlacement::new(&source)
            .with_dataflow_aware_s_chain()
            .with_unassigned_mode(unassigned_mode_inline())
            .place_in_entry(logical_module("mapped", &members))
            .add_chunk(
                "static/provider",
                "export const values = ['a'];\nexport const keep = value => value;\n",
                vec![],
            )
            .add_chunk(
                "static/relay",
                "export { values } from './provider.js';\n",
                vec![],
            )
            .run_with_graphs();
        assert_preserved_entry_output(&fixture, "a? a\n");
        assert_module_exports(
            &fixture.out_root,
            "static/app/modules/mapped.js",
            &["mapped"],
            &["first", "keep"],
        );
        assert_eq!(
            binding_execution_report(&fixture, "mapped")["binding_effect_authority"]["authority"],
            "operation_receipt",
            "relocation must use the checked iteration transfer: {prefix}"
        );
    }
}

#[test]
fn imported_array_iteration_cannot_move_a_later_provider_array_write_ahead() {
    let source = "import { values } from './provider.js';\n\
                  const mapped = values.map((value) => value);\n\
                  const changed = values.push('b');\n\
                  console.log(mapped.join(','), values.join(','));\n";
    let rejected = ProgramPlacement::new(source)
        .with_dataflow_aware_s_chain()
        .with_unassigned_mode(unassigned_mode_inline())
        .place_in_entry(logical_module("change", &[Member::new("changed")]))
        .add_chunk("static/provider", PROVIDER, vec![])
        .emit_on_rejection();

    assert_node_output(&rejected.source_root.join("static/app.js"), "a a,b\n", "");
    assert_node_output(&rejected.entry_path(), "a,b a,b\n", "");
    let assessment = rejected.candidate_relocation_assessment();
    assert_eq!(assessment["legacy_gate_accepts"], false, "{assessment:#}");
    assert_eq!(assessment["status"], "incomplete", "{assessment:#}");
}

#[test]
fn unsupported_iteration_callbacks_keep_their_own_source_during_fallback() {
    let provider = "class Counter { static count = 0; }\nfunction next() { return ++Counter.count; }\nfunction peek() { return Counter.count; }\n";
    for imported in [false, true] {
        let prefix = if imported {
            "import { next, peek } from './provider.js';"
        } else {
            provider
        };
        let source = format!(
            "{prefix}\nconst first = [0].map(next)[0];\nconst second = [0].map(peek)[0];\nconsole.log(first, second);\n"
        );
        let exported = format!("{provider}\nexport {{ next, peek }};\n");
        let placement = || {
            let placement = ProgramPlacement::new(&source)
                .with_dataflow_aware_s_chain()
                .with_unassigned_mode(unassigned_mode_inline());
            if imported {
                placement.add_chunk("static/provider", &exported, vec![])
            } else {
                placement.place_in_entry(logical_module(
                    "state",
                    &[
                        Member::new("Counter"),
                        Member::new("next"),
                        Member::new("peek"),
                    ],
                ))
            }
        };
        let baseline = placement().run();
        assert_preserved_entry_output(&baseline, "1 1\n");

        let rejected = placement()
            .place_in_entry(logical_module("second", &[Member::new("second")]))
            .emit_on_rejection();
        assert_node_output(&rejected.source_root.join("static/app.js"), "1 1\n", "");
        assert_node_output(&rejected.entry_path(), "1 0\n", "");
        let assessment = rejected.candidate_relocation_assessment();
        assert_eq!(
            assessment["legacy_gate_accepts"], false,
            "imported={imported}: {assessment:#}"
        );
    }
}
