use crate::analyze_chunk;
use analysis::diagnostics::*;
use analysis::purity::{Purity, PurityReason, PurityRule};
use analysis::reports::schema::PurityCompletionDisagreement;
use analysis::*;
use analysis::{AnalysisInputs, execution_evidence::SourceLocator};
use source_identity::SourceUnitOrdinal;

#[test]
fn records_native_purity_disagreement_with_analyzed_site() {
    let source = "const input = 1;\nconst output = input + 1;\n";
    let (module, source_map) = raw_js_test_support::parse_with_source_map(source);
    let lines = js_ast::SourceLineIndex::for_source_map(&source_map);
    let location = SourceLocation {
        source_path: "src/main.js".to_owned(),
        start_line: 2,
        end_line: 2,
        start_column: Some(1),
    };
    let disagreement = PurityCompletionDisagreement {
        prepared: Purity::pure(),
        completed: Purity::NotPure {
            reasons: vec![PurityReason {
                rule: PurityRule::UnknownCall,
                source_location: Some(location.clone()),
                detail: Some("injected monitoring evidence".to_owned()),
                author_guidance: None,
                cause_ref: None,
                cause: None,
                span: Default::default(),
            }],
        },
    };
    let mut facts = analyze_chunk(
        &module,
        &AnalysisInputs::default(),
        Some(SourceLocator {
            source_path: "src/main.js",
            lines: &lines,
        }),
    );
    {
        let fact = &mut facts.facts[1];
        assert_eq!(fact.ordinal, SourceUnitOrdinal(1));
        assert_eq!(fact.source_location, Some(location.clone()));
        fact.synchronous_calls
            .get_or_insert_default()
            .purity_disagreement = Some(disagreement.clone());
    }

    let diagnostics = Diagnostics::default();
    diagnostics.record_purity_completion_disagreements("main", &facts);

    assert_eq!(
        facts.facts[1].purity_completion_disagreement(),
        Some(&disagreement),
        "recording a diagnostic must preserve the native fact"
    );
    let collected = diagnostics.drain();
    assert!(collected.invocation.is_empty());
    let [
        ChunkDiagnostic::PurityCompletionDisagreement {
            ordinal,
            source_location,
            evidence,
        },
    ] = collected.chunks["main"].as_slice()
    else {
        panic!("unexpected diagnostics: {collected:?}");
    };
    assert_eq!(*ordinal, SourceUnitOrdinal(1));
    assert_eq!(source_location.as_ref(), Some(&location));
    assert_eq!(evidence.as_ref(), &disagreement);
}
