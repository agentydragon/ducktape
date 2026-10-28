use analysis::purity::{RedundantPurityHint, RedundantPurityProofBasis};
use debundle_e2e_support::{
    ChunkExportPurityBuilder, FixtureOpts, Member, MemberPurity, assert_entry_output,
    logical_module, read_json, run_fixture,
};
use serde::Deserialize;

#[derive(Deserialize)]
struct HintReport {
    redundant_purity_hints: Vec<RedundantPurityHint>,
}

#[test]
fn hint_free_function_chain_is_reported_as_independent() {
    let fixture = run_fixture(FixtureOpts::new(
        "function leaf() { return 7; }\nfunction wrapper() { return leaf(); }\nconsole.log(wrapper());\nexport { leaf, wrapper };",
        vec![logical_module(
            "functions",
            &[
                Member::new("leaf").with_purity(MemberPurity::Pure),
                Member::new("wrapper").with_purity(MemberPurity::Pure),
            ],
        )],
    ));
    assert_entry_output(&fixture, "7\n");
    let report: HintReport = read_json(
        &fixture
            .report_root
            .join(&fixture.chunk_id)
            .join("modules.json"),
    );
    assert_eq!(report.redundant_purity_hints.len(), 2);
    for hint in report.redundant_purity_hints {
        assert_eq!(
            hint.proof_basis,
            RedundantPurityProofBasis::WithoutPurityHints
        );
    }
}

#[test]
fn hint_depending_on_another_assertion_is_explicit() {
    // The leaf hint represents an author-audited callsite contract whose body
    // the analyzer cannot prove. The wrapper's removal depends on retaining it.
    let fixture = run_fixture(FixtureOpts::new(
        "function leaf(value) { return value.key; }\nfunction wrapper() { return leaf({ key: 7 }); }\nconsole.log(wrapper());\nexport { leaf, wrapper };",
        vec![logical_module(
            "functions",
            &[
                Member::new("leaf").with_purity(MemberPurity::Pure),
                Member::new("wrapper").with_purity(MemberPurity::Pure),
            ],
        )],
    ));
    assert_entry_output(&fixture, "7\n");
    let report: HintReport = read_json(
        &fixture
            .report_root
            .join(&fixture.chunk_id)
            .join("modules.json"),
    );
    let [hint] = report.redundant_purity_hints.as_slice() else {
        panic!(
            "only the wrapper hint is removable: {:?}",
            report.redundant_purity_hints
        );
    };
    assert_eq!(hint.binding_name, "wrapper");
    assert_eq!(
        hint.proof_basis,
        RedundantPurityProofBasis::WithOtherPurityHints
    );
}

#[test]
fn imported_builtin_name_does_not_make_member_hint_redundant() {
    let chunks = [(
        "static/vendor",
        "export const namespace = { isArray(value) { return value === 'sample'; } };",
    )];
    let contracts = [ChunkExportPurityBuilder::new("static/vendor")
        .with_pure_members("namespace", &["isArray"])
        .build()];
    let fixture = run_fixture(
        FixtureOpts::new(
            "import { namespace as Array } from './vendor.js';\nconst result = Array.isArray('sample');\nconsole.log(result);\nexport { result };",
            vec![logical_module("result", &[Member::new("result")])],
        )
        .with_extra_chunks(&chunks)
        .with_chunk_export_purity(&contracts),
    );
    assert_entry_output(&fixture, "true\n");
    assert!(
        !fixture.stderr.contains("`pure_members: [isArray]`"),
        "shadowed builtin's member hint is load-bearing: {}",
        fixture.stderr,
    );
}
