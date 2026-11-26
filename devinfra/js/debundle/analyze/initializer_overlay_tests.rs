use crate::analyze_chunk;
use analysis::facts::binding_effect_coverage::{BindingEffectAuthority, BindingEffectCoverage};
use analysis::facts::*;
use analysis::*;
use raw_js_test_support::parse;
use std::collections::BTreeSet;

#[test]
fn initializer_overlay_preserves_raw_receipts_and_other_effect_domains() {
    // The author marker hides the source-owned receipt in graph reports;
    // inspect that independent authority directly after real source analysis.
    for (source, has_receipt) in [
        (
            "const seed = 41; function factory() { return seed; } var result = (seed, factory(), factory());",
            true,
        ),
        (
            "const seed = 41; function factory() { globalThis.host?.(() => { const value = later; later = 3; return value; }); return seed; } var result = (seed, factory(), factory()); var later = 0;",
            false,
        ),
    ] {
        let module = parse(source);
        let mut analysis = analyze_chunk(&module, &AnalysisInputs::default(), None);
        let result_index = analysis
            .facts
            .iter()
            .position(|fact| fact.declared.iter().any(|binding| binding.0 == "result"))
            .unwrap();
        let before = analysis.facts[result_index]
            .synchronous_calls
            .as_ref()
            .unwrap()
            .clone();
        let BindingEffectCoverage::OrderedRootInitializer { receipt, .. } =
            &before.binding_effect_coverage
        else {
            panic!("the fixture must exercise ordered root receipt sealing: {before:#?}");
        };
        assert_eq!(receipt.is_some(), has_receipt, "{before:#?}");
        if !has_receipt {
            assert!(
                !before.fallback_only.reads.is_empty(),
                "raw fallback must block a source receipt"
            );
        }
        let coverage = serde_json::to_value(&before.binding_effect_coverage).unwrap();
        let selected = BTreeSet::from(["result".to_string()]);
        analysis.apply_initializer_binding_effects(&selected);
        analysis
            .validate_initializer_binding_effects(&selected)
            .unwrap();
        let fact = &analysis.facts[result_index];
        let after = fact.synchronous_calls.as_ref().unwrap();
        assert_eq!(
            serde_json::to_value(&after.binding_effect_coverage).unwrap(),
            coverage,
            "an author filter cannot mint, upgrade or reseal a source receipt"
        );
        assert_eq!(
            after.binding_effect_authority(),
            BindingEffectAuthority::AuthorSourceOnly
        );
        assert_eq!(after.complete, before.complete);
        assert_eq!(after.heap_contents, before.heap_contents);
        assert_eq!(
            after.non_assumed_heap_accesses,
            before.non_assumed_heap_accesses
        );
        assert_eq!(
            serde_json::to_value(&after.completed_initialization).unwrap(),
            serde_json::to_value(&before.completed_initialization).unwrap()
        );
        assert_eq!(after.precision_losses, before.precision_losses);
        let effects = fact.initialization_effects();
        assert!(
            effects
                .reads
                .keys()
                .any(|cell| matches!(cell, EffectCell::Binding(binding) if binding.0 == "seed")),
            "the syntax-derived footprint remains in initialization effects"
        );
        if !has_receipt {
            assert!(before.reads.iter().any(|(binding, _)| binding.0 == "later"));
            assert!(
                before
                    .rebinds
                    .iter()
                    .any(|(binding, _)| binding.0 == "later")
            );
            for cells in [&effects.reads, &effects.writes] {
                assert!(
                    !cells.keys().any(
                        |cell| matches!(cell, EffectCell::Binding(binding) if binding.0 == "later")
                    ),
                    "promoted fallback effects must be narrowed before effect translation validation"
                );
            }
        }
    }
}

#[test]
fn initializer_overlay_retains_duplicate_and_consumption_guards() {
    for (source, binding, diagnostic) in [
        (
            "var result = 1; var result = 2;",
            "result",
            "multiple source units",
        ),
        ("var result = 1;", "missing", "consumption mismatch"),
    ] {
        let module = parse(source);
        let mut facts = analyze_chunk(&module, &AnalysisInputs::default(), None);
        let configured = BTreeSet::from([binding.to_string()]);
        facts.apply_initializer_binding_effects(&configured);
        let error = facts
            .validate_initializer_binding_effects(&configured)
            .unwrap_err();
        assert!(error.to_string().contains(diagnostic), "{error:#}");
    }
}
