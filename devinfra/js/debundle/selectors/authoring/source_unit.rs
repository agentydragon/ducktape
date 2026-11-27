//! Source-unit selector synthesis: residual claims, anonymous statements, commuting endpoints.

use std::collections::BTreeMap;
use std::fs;
use std::path::Path;

use anyhow::{Context, Result, bail};
use selector_outcome::Outcome;
use selector_resolve::{EntityIndex, SpecModule};
use source_match::ParsedSourceMatchSelector;
use spec::annotations::{SourceMatch, SourceMatchBinding, SourceMatchClaim};
use spec::{SourceMatchBindingDetail, SourceMatchIdentifierMode};
use swc_common::DUMMY_SP;
use swc_ecma_ast::*;
use synthesis_failure::SynthesisSkipReason;

use crate::chunk_index::{ChunkSelectorIndex, SYNTHESIS_MODULE};
use crate::group_synthesis::{
    GroupSelectorOutcome, RequestedDeclGroup, synthesize_requested_declaration_groups,
    synthesize_simplest_selector_for_group,
};
use crate::name_binding_rewrite::NameBindingMember;

/// Build proven source-backed claims for bindings that have no YAML member yet.
/// This shares the same indexed declaration lookup and selector proof as the
/// name-binding codemod, without first creating unstable `binding.name` pins.
pub fn synthesize_residual_claims_for_modules(
    source_file: &Path,
    modules: &BTreeMap<String, BTreeMap<String, String>>,
) -> Result<BTreeMap<String, Vec<SourceMatchClaim>>> {
    js_ast::with_swc_globals(|| {
        let parsed = js_ast::parse_js_module_file(source_file)?;
        let index = ChunkSelectorIndex::new(&parsed.module);
        let mut result = BTreeMap::new();
        for (module, bindings) in modules {
            let mut groups: BTreeMap<usize, Vec<NameBindingMember>> = BTreeMap::new();
            for (runtime_binding, export_name) in bindings {
                let Some([decl_idx]) = index
                    .binding_to_decl
                    .get(runtime_binding)
                    .map(Vec::as_slice)
                else {
                    bail!(
                        "binding `{runtime_binding}` needs exactly one source declaration for selector synthesis"
                    );
                };
                groups
                    .entry(*decl_idx)
                    .or_default()
                    .push(NameBindingMember {
                        member_index: 0,
                        export_name: export_name.clone(),
                        binding_name: runtime_binding.clone(),
                        annotation_fields: Vec::new(),
                    });
            }
            let mut claims = Vec::new();
            for RequestedDeclGroup {
                decl_idx,
                members: _,
                outcome,
            } in synthesize_requested_declaration_groups(&index, groups, 1)?
            {
                let synthesized = match outcome? {
                    GroupSelectorOutcome::Synthesized(synthesized) => synthesized,
                    GroupSelectorOutcome::Skipped(reason) => bail!(
                        "no uniquely proven structural selector for declaration {decl_idx}: {}",
                        reason.explanation()
                    ),
                };
                let mut claim_bindings = Vec::new();
                for target in synthesized.target_bindings {
                    claim_bindings.push(SourceMatchBinding::Detailed(SourceMatchBindingDetail {
                        local: target.selector_local,
                        name: Some(target.export_name),
                    }));
                }
                claims.push(SourceMatchClaim {
                    identifiers: SourceMatchIdentifierMode::AlphaAll,
                    match_source: synthesized.match_source,
                    bindings: claim_bindings,
                    note: None,
                });
            }
            result.insert(module.clone(), claims);
        }
        Ok(result)
    })
}

/// Synthesize proven anonymous selectors for graph source-unit ordinals. Raw
/// body indices are derived from the parsed source, never treated as graph IDs.
pub fn synthesize_anonymous_claims_for_modules(
    source_file: &Path,
    modules: &BTreeMap<String, Vec<source_identity::SourceUnitOrdinal>>,
) -> Result<BTreeMap<String, Vec<spec::annotations::AnonymousStatement>>> {
    js_ast::with_swc_globals(|| {
        let source = fs::read_to_string(source_file)?;
        let parsed = js_ast::parse_js_module(&source_file.display().to_string(), source)?;
        with_source_unit_selector_synthesizer(&parsed.module, |synthesizer| {
            let mut result = BTreeMap::new();
            for (module, ordinals) in modules {
                let mut statements = Vec::new();
                for ordinal in ordinals {
                    let body_index = (0..parsed.module.body.len())
                        .find(|&index| {
                            js_ast::source_unit_ordinal_for_statement(
                                &parsed.module.body,
                                source_identity::StatementId(index),
                            ) == *ordinal
                        })
                        .with_context(|| format!("source unit ordinal {ordinal:?} is absent"))?;
                    let request = SourceUnitSelectorRequest::AnonymousStatement { body_index };
                    let SourceUnitSelectorSynthesis::Synthesized(selector) =
                        synthesizer.synthesize(&request)?
                    else {
                        bail!(
                            "anonymous source unit {ordinal:?} has no unique structural selector"
                        );
                    };
                    statements.push(spec::annotations::AnonymousStatement {
                        match_source: None,
                        source_match: Some(SourceMatch {
                            identifiers: selector.claim.identifiers,
                            match_source: selector.claim.match_source,
                            target_binding: None,
                        }),
                        no_reentry_call: None,
                        note: None,
                        comment: None,
                    });
                }
                result.insert(module.clone(), statements);
            }
            Ok(result)
        })
    })
}

/// One source unit a `commuting_statements` endpoint must select
/// (SPEC.md § Commuting statements).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SourceUnitSelectorRequest {
    /// `binding`'s declaration: a `source_match` claiming that one binding.
    DeclaredBinding { binding: String },
    /// The anonymous (non-declaring) statement at raw body index
    /// `body_index`: a `source_match` without bindings.
    AnonymousStatement { body_index: usize },
}

/// A proven `source_match` endpoint selector and the body item it selects.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SynthesizedSourceUnitSelector {
    pub claim: SourceMatchClaim,
    pub body_index: usize,
    pub rewritten_holes: Vec<String>,
}

/// Outcome of one [`SourceUnitSelectorRequest`].
#[derive(Debug, Clone)]
pub enum SourceUnitSelectorSynthesis {
    Synthesized(SynthesizedSourceUnitSelector),
    /// No uniquely proven selector; `reason` is the nearest miss (the
    /// minimizer's rejection, or the shared resolve's non-resolved outcome).
    Skipped {
        reason: SynthesisSkipReason,
    },
}

/// Run `body` with a [`SourceUnitSelectorSynthesizer`] over the caller's parsed
/// chunk. The chunk is indexed once and reused for every request `body` makes.
/// The caller must parse it inside an SWC `GLOBALS` scope.
pub fn with_source_unit_selector_synthesizer<T>(
    module: &Module,
    body: impl FnOnce(&SourceUnitSelectorSynthesizer<'_>) -> Result<T>,
) -> Result<T> {
    let index = ChunkSelectorIndex::new(module);
    body(&SourceUnitSelectorSynthesizer { index: &index })
}

/// Synthesizes one proven `source_match` per request, with the name-binding
/// codemod's indexed synthesis for declared bindings and the exact statement
/// for anonymous ones; every selector is proven with the shared resolve on
/// its own.
pub struct SourceUnitSelectorSynthesizer<'a> {
    index: &'a ChunkSelectorIndex<'a>,
}

impl SourceUnitSelectorSynthesizer<'_> {
    pub fn synthesize(
        &self,
        request: &SourceUnitSelectorRequest,
    ) -> Result<SourceUnitSelectorSynthesis> {
        match request {
            SourceUnitSelectorRequest::DeclaredBinding { binding } => {
                synthesize_declared_binding_selector(self.index, binding)
            }
            SourceUnitSelectorRequest::AnonymousStatement { body_index } => {
                synthesize_anonymous_statement_selector(self.index, *body_index)
            }
        }
    }
}

fn synthesize_declared_binding_selector(
    index: &ChunkSelectorIndex<'_>,
    binding: &str,
) -> Result<SourceUnitSelectorSynthesis> {
    let decl_indices = index
        .binding_to_decl
        .get(binding)
        .map(Vec::as_slice)
        .unwrap_or_default();
    let [decl_idx] = decl_indices else {
        return Ok(SourceUnitSelectorSynthesis::Skipped {
            reason: SynthesisSkipReason::BindingDeclarationCount {
                binding: binding.to_owned(),
                count: decl_indices.len(),
            },
        });
    };
    let member = NameBindingMember {
        member_index: 0,
        export_name: binding.to_owned(),
        binding_name: binding.to_owned(),
        annotation_fields: Vec::new(),
    };
    let synthesized = match synthesize_simplest_selector_for_group(index, *decl_idx, &[member], 1)?
    {
        GroupSelectorOutcome::Synthesized(synthesized) => synthesized,
        GroupSelectorOutcome::Skipped(reason) => {
            return Ok(SourceUnitSelectorSynthesis::Skipped { reason });
        }
    };
    let [target] = synthesized.target_bindings.as_slice() else {
        bail!(
            "one requested binding synthesized {} targets",
            synthesized.target_bindings.len()
        );
    };
    Ok(SourceUnitSelectorSynthesis::Synthesized(
        SynthesizedSourceUnitSelector {
            claim: SourceMatchClaim {
                identifiers: SourceMatchIdentifierMode::AlphaAll,
                match_source: synthesized.match_source,
                bindings: vec![SourceMatchBinding::Local(target.selector_local.clone())],
                note: None,
            },
            body_index: synthesized.body_idx,
            rewritten_holes: synthesized.rewritten_holes,
        },
    ))
}

/// The exact statement is the only anonymous-statement form the minimizer
/// knows (`anonymous_statements[].match`); the shared resolve decides whether
/// it is unique.
fn synthesize_anonymous_statement_selector(
    index: &ChunkSelectorIndex<'_>,
    body_index: usize,
) -> Result<SourceUnitSelectorSynthesis> {
    let item = index
        .module
        .body
        .get(body_index)
        .with_context(|| format!("body index {body_index} is outside the parsed source"))?;
    let match_source = js_ast::emit_module_source(&Module {
        span: DUMMY_SP,
        body: vec![item.clone()],
        shebang: None,
    })?;
    let selector = spec::annotations::AnonymousStatementSelector {
        match_source: match_source.clone(),
        identifiers: SourceMatchIdentifierMode::AlphaAll,
        target_binding: None,
    };
    let parsed = ParsedSourceMatchSelector::parse(
        SYNTHESIS_MODULE,
        format!("<anonymous source_match in {SYNTHESIS_MODULE}>"),
        &selector,
        source_match::SelectorField::SourceMatch,
    )?;
    let resolution = index.chunk.resolve(&[SpecModule {
        path: SYNTHESIS_MODULE.to_string(),
        members: Vec::new(),
        anonymous_statements: vec![selector_resolve::AnonymousStatement {
            index: 0,
            selector: parsed,
        }],
    }])?;
    let outcome = resolution
        .outcome(0, EntityIndex::AnonymousStatement(0))
        .context("an anonymous statement selector has an outcome")?;
    match &outcome.outcome {
        Outcome::Resolved {
            body_index: resolved,
            ..
        } if *resolved == body_index => Ok(SourceUnitSelectorSynthesis::Synthesized(
            SynthesizedSourceUnitSelector {
                claim: SourceMatchClaim {
                    identifiers: SourceMatchIdentifierMode::AlphaAll,
                    match_source,
                    bindings: Vec::new(),
                    note: None,
                },
                body_index,
                rewritten_holes: Vec::new(),
            },
        )),
        _ => Ok(SourceUnitSelectorSynthesis::Skipped {
            reason: SynthesisSkipReason::AnonymousStatementNotUnique {
                body_index,
                outcome: Box::new(outcome.clone()),
            },
        }),
    }
}
