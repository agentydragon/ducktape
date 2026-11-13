//! Structured, invocation-owned diagnostics; analysis never chooses an output stream.

use crate::execution_evidence::SourceLocation;
use crate::reports::schema::PurityCompletionDisagreement;
use crate::{RedundantPureMemberHint, RedundantPurityHint};
use source_identity::SourceUnitOrdinal;
use spec::AdmissionCheck;
use std::collections::BTreeMap;
use std::sync::Mutex;

#[derive(Debug)]
pub enum ChunkDiagnostic {
    RedundantPurityHint(RedundantPurityHint),
    RedundantPureMemberHint(RedundantPureMemberHint),
    PurityCompletionDisagreement {
        ordinal: SourceUnitOrdinal,
        source_location: Option<SourceLocation>,
        evidence: Box<PurityCompletionDisagreement>,
    },
    AdmissionOverride {
        shape: AdmissionShape,
        statements: Vec<usize>,
    },
    RedundantAdmissionOverride(AdmissionCheck),
    Selector(Box<selector_outcome::SelectorOutcome>),
    UnusedCommutingStatements(Box<crate::CommutingStatementsUnused>),
    DanglingExportContract {
        contract: ExportContract,
        export: String,
    },
    OpaqueEntry {
        error: js_ast::JsParseError,
    },
}

/// Shared by parallel chunk work. A command drains it even when analysis fails.
/// Draining orders chunks and their typed diagnostic identities deterministically.
#[derive(Debug, Default)]
pub struct Diagnostics {
    collected: Mutex<CollectedDiagnostics>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum InvocationDiagnostic {
    OwnershipSubset,
}

/// Drained evidence preserves whether it describes the invocation or one chunk.
#[derive(Debug, Default)]
pub struct CollectedDiagnostics {
    pub invocation: Vec<InvocationDiagnostic>,
    pub chunks: BTreeMap<String, Vec<ChunkDiagnostic>>,
}

impl Diagnostics {
    pub fn record_purity_completion_disagreements(
        &self,
        chunk: &str,
        facts: &crate::facts::ChunkFactAnalysis,
    ) {
        for fact in &facts.facts {
            let Some(evidence) = fact.purity_completion_disagreement() else {
                continue;
            };
            self.record_chunk(
                chunk,
                ChunkDiagnostic::PurityCompletionDisagreement {
                    ordinal: fact.ordinal,
                    source_location: fact.source_location.clone(),
                    evidence: Box::new(evidence.clone()),
                },
            );
        }
    }

    pub fn record_chunk(&self, chunk: &str, diagnostic: ChunkDiagnostic) {
        self.collected
            .lock()
            .expect("diagnostics lock poisoned")
            .chunks
            .entry(chunk.to_owned())
            .or_default()
            .push(diagnostic);
    }

    pub fn record_invocation(&self, diagnostic: InvocationDiagnostic) {
        self.collected
            .lock()
            .expect("diagnostics lock poisoned")
            .invocation
            .push(diagnostic);
    }

    pub fn drain(&self) -> CollectedDiagnostics {
        let mut collected =
            std::mem::take(&mut *self.collected.lock().expect("diagnostics lock poisoned"));
        collected.invocation.sort();
        for records in collected.chunks.values_mut() {
            records.sort_by(|left, right| left.ordering_key().cmp(&right.ordering_key()));
        }
        collected
    }
}

// Ordering reads structured identities, never human explanations.
#[derive(PartialEq, Eq, PartialOrd, Ord)]
enum DiagnosticOrder<'a> {
    OpaqueEntry,
    DanglingExportContract(ExportContract, &'a str),
    Selector(selector_outcome::SelectorIdentity<'a>),
    RedundantPurityHint(&'a str),
    RedundantPureMemberHint(&'a str, &'a str),
    PurityCompletionDisagreement(SourceUnitOrdinal),
    AdmissionOverride(&'a AdmissionShape, &'a [usize]),
    RedundantAdmissionOverride(AdmissionCheck),
    UnusedCommutingStatements(&'a str),
}

impl ChunkDiagnostic {
    fn ordering_key(&self) -> DiagnosticOrder<'_> {
        match self {
            Self::OpaqueEntry { .. } => DiagnosticOrder::OpaqueEntry,
            Self::DanglingExportContract { contract, export } => {
                DiagnosticOrder::DanglingExportContract(*contract, export)
            }
            Self::Selector(outcome) => DiagnosticOrder::Selector(outcome.identity()),
            Self::RedundantPurityHint(hint) => {
                DiagnosticOrder::RedundantPurityHint(&hint.binding_name)
            }
            Self::RedundantPureMemberHint(hint) => {
                DiagnosticOrder::RedundantPureMemberHint(&hint.binding_name, &hint.property)
            }
            Self::PurityCompletionDisagreement { ordinal, .. } => {
                DiagnosticOrder::PurityCompletionDisagreement(*ordinal)
            }
            Self::AdmissionOverride { shape, statements } => {
                DiagnosticOrder::AdmissionOverride(shape, statements)
            }
            Self::RedundantAdmissionOverride(check) => {
                DiagnosticOrder::RedundantAdmissionOverride(*check)
            }
            Self::UnusedCommutingStatements(unused) => {
                DiagnosticOrder::UnusedCommutingStatements(&unused.spec_location)
            }
        }
    }
}

/// Authoring fields whose export names must exist in analyzed providers.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, strum::Display)]
#[strum(serialize_all = "snake_case")]
pub enum ExportContract {
    PureExports,
    CallbackFactoryExports,
    PureMembers,
    FluentExports,
    PureNewExports,
    NoReentryCallbackExports,
    RetainedCallbackConstructorExports,
    RetainedCallbackFactoryExports,
    RetainedCallbackFactoryMembers,
    InlineCallbackFactoryExports,
    NamespaceIndependentMembers,
    CallbackFactoryMembers,
    NoReentryMembers,
    NoReentryRecordMembers,
    NoReentryExports,
}

/// Source evidence for one admission condition. Payloads retain JavaScript data,
/// never composed diagnostic prose.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub enum AdmissionShape {
    DirectEvalAtTopLevel,
    IndirectEvalAtTopLevel,
    LazyDirectEval,
    InternalDynamicImport { specifier: String },
    NonliteralDynamicImport,
    ImportMetaProperty { property: String },
    ComputedImportMeta,
    BareImportMeta,
}

impl AdmissionShape {
    pub fn check(&self) -> AdmissionCheck {
        match self {
            Self::DirectEvalAtTopLevel | Self::IndirectEvalAtTopLevel | Self::LazyDirectEval => {
                AdmissionCheck::A1Eval
            }
            Self::InternalDynamicImport { .. } | Self::NonliteralDynamicImport => {
                AdmissionCheck::A3DynamicImport
            }
            Self::ImportMetaProperty { .. } | Self::ComputedImportMeta | Self::BareImportMeta => {
                AdmissionCheck::A5ImportMeta
            }
        }
    }

    pub fn explanation(&self) -> AdmissionExplanation<'_> {
        AdmissionExplanation(self)
    }
}

pub struct AdmissionExplanation<'a>(&'a AdmissionShape);

impl std::fmt::Display for AdmissionExplanation<'_> {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self.0 {
            AdmissionShape::DirectEvalAtTopLevel | AdmissionShape::IndirectEvalAtTopLevel => {
                let shape = match self.0 {
                    AdmissionShape::DirectEvalAtTopLevel => "direct `eval(...)` call",
                    _ => "indirect `(…, eval)(...)` call",
                };
                write!(f, "{shape} at module top level — the static analyzer cannot see what eval reads, so the at-init read graph `I` would be incomplete")
            }
            AdmissionShape::LazyDirectEval => f.write_str("direct `eval(...)` call in a lazy body — it can read and write any binding in scope by name, which the analysis cannot see, once the chunk is split, reordered or renamed"),
            AdmissionShape::InternalDynamicImport { specifier } => write!(f, "`import({specifier:?})` resolves into this chunk — dynamic import of a debundled internal module routes around the static import graph"),
            AdmissionShape::NonliteralDynamicImport => f.write_str("dynamic `import(...)` with a non-literal specifier at module top level — the target cannot be proven external to the debundled module set"),
            AdmissionShape::ImportMetaProperty { property } => write!(f, "`import.meta.{property}` read at module top level — only `import.meta.url` is covered by the realizability theorem"),
            AdmissionShape::ComputedImportMeta => f.write_str("computed `import.meta[...]` access at module top level — only `import.meta.url` is covered by the realizability theorem"),
            AdmissionShape::BareImportMeta => f.write_str("bare `import.meta` used as a value at module top level — namespace reflection beyond `import.meta.url`"),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::facts::{SynchronousCalls, analyze_chunk};
    use crate::purity::{Purity, PurityReason, PurityRule};
    use crate::reports::schema::PurityCompletionDisagreement;
    use crate::{AnalysisInputs, execution_evidence::SourceLocator};
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
            fact.synchronous_calls = Some(SynchronousCalls {
                purity_disagreement: Some(disagreement.clone()),
                ..SynchronousCalls::default()
            });
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
}
