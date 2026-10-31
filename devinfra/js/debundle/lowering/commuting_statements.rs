//! Resolve a chunk's `commuting_statements` premises (SPEC.md § Commuting
//! statements) to pairs of its post-split source units. Only dependency-graph
//! construction consumes them, so they are resolved per chunk right before it,
//! never in the cross-module pass: an edit to them must not reach analysis.

use std::collections::BTreeMap;

use analysis::analysis_inputs::{
    CommutingEndpoint, CommutingStatementsRef, ResolvedCommutingStatements,
};
use anyhow::{Context, Result, bail, ensure};
use binding_targets::binding_name_strings;
use graph_provenance::GraphProvenance;
use js_ast::{source_unit_ordinal_for_statement, split_var_declarators};
use selector_outcome::Outcome as SelectorOutcome;
use selector_resolve::{Chunk as SelectorChunk, EntityIndex};
use source_identity::{SourceUnitOrdinal, StatementId};
use spec::{CommutingStatementsContract, SourceUnitSelector};
use swc_ecma_ast::{Module, ModuleItem};

use super::cross_module::{author_input_spec_path, select_one_declared_binding};
use super::materialize::ResolvedMemberSite;

/// Resolve every `commuting_statements` pair of `chunk_name` to two distinct
/// post-split source units of `body`. Resolution failures, ambiguity, a foreign
/// provider, a self-pair and a repeated unordered pair are spec errors naming
/// the entry and endpoint.
pub(super) fn resolve_chunk_commuting_statements(
    chunk_name: &str,
    body: &[ModuleItem],
    contracts: &[CommutingStatementsContract],
    graph_provenance: Option<&GraphProvenance>,
    resolved_member_sites: &BTreeMap<(String, String), Vec<ResolvedMemberSite>>,
) -> Result<Vec<ResolvedCommutingStatements>> {
    if contracts.is_empty() {
        return Ok(Vec::new());
    }
    let prefix = format!("chunk_analysis_options.{chunk_name}.commuting_statements");
    let spec_path = author_input_spec_path(
        graph_provenance.context("commuting statements require graph provenance")?,
    )?;
    let module = Module {
        span: Default::default(),
        body: body.to_vec(),
        shebang: None,
    };
    let structural = analysis::facts::collect_chunk_source_facts(&module, None, |_| None);
    let selector_chunk = SelectorChunk::new(chunk_name, &module, structural);
    let mut seen_pairs = BTreeMap::<(SourceUnitOrdinal, SourceUnitOrdinal), usize>::new();
    let mut chunk_pairs = Vec::with_capacity(contracts.len());
    for (input_index, contract) in contracts.iter().enumerate() {
        let request_id = format!("{prefix}[{input_index}]");
        let a = resolve_source_unit_endpoint(
            &selector_chunk,
            body,
            chunk_name,
            &format!("{request_id}.a"),
            &contract.a,
            resolved_member_sites,
        )?;
        let b = resolve_source_unit_endpoint(
            &selector_chunk,
            body,
            chunk_name,
            &format!("{request_id}.b"),
            &contract.b,
            resolved_member_sites,
        )?;
        ensure!(
            a.source_unit != b.source_unit,
            "{request_id} selects the same source unit {} at both endpoints",
            a.source_unit
        );
        let key = (
            a.source_unit.min(b.source_unit),
            a.source_unit.max(b.source_unit),
        );
        if let Some(earlier) = seen_pairs.insert(key, input_index) {
            bail!(
                "{request_id} duplicates {prefix}[{earlier}]: both select source units {} and {}",
                key.0,
                key.1
            );
        }
        chunk_pairs.push(ResolvedCommutingStatements {
            reference: CommutingStatementsRef {
                defining_chunk: chunk_name.to_string(),
                input_index,
            },
            spec_location: format!("{}:{request_id}", spec_path.display()),
            a,
            b,
            note: contract.note.clone(),
        });
    }
    Ok(chunk_pairs)
}

/// Resolve one endpoint selector to exactly one post-split source unit of
/// `chunk_name`, through the shared selector engine or an already resolved
/// member site.
fn resolve_source_unit_endpoint(
    selector_chunk: &SelectorChunk<'_>,
    body: &[ModuleItem],
    chunk_name: &str,
    endpoint_id: &str,
    selector: &SourceUnitSelector,
    resolved_member_sites: &BTreeMap<(String, String), Vec<ResolvedMemberSite>>,
) -> Result<CommutingEndpoint> {
    let (body_index, selected_binding) = match selector {
        SourceUnitSelector::SourceMatch(claim) if claim.bindings.is_empty() => {
            let parsed = spec_resolution::parse_anonymous_selector(
                endpoint_id,
                format!("<anonymous source_match in {endpoint_id}>"),
                &spec::AnonymousStatementSelector {
                    match_source: claim.match_source.clone(),
                    identifiers: claim.identifiers,
                    target_binding: None,
                },
            )?;
            let selector_module = spec_resolution::spec_module(
                endpoint_id.to_owned(),
                Vec::new(),
                vec![spec_resolution::anonymous_statement(0, parsed)],
            );
            let resolution = selector_chunk
                .resolve(&[selector_module])
                .with_context(|| format!("{endpoint_id}.source_match"))?;
            let outcome = resolution
                .outcome(0, EntityIndex::AnonymousStatement(0))
                .with_context(|| {
                    format!("{endpoint_id}.source_match produced no selector outcome")
                })?;
            match &outcome.outcome {
                SelectorOutcome::Resolved { body_index, .. } => (*body_index, None),
                other => bail!(
                    "{endpoint_id}.source_match must select exactly one anonymous statement; selector outcome: {} {other:?}",
                    other.kind().as_str()
                ),
            }
        }
        SourceUnitSelector::SourceMatch(claim) => {
            ensure!(
                claim.bindings.len() == 1,
                "{endpoint_id}.source_match must name at most one declared binding"
            );
            let (body_index, binding) =
                select_one_declared_binding(selector_chunk, endpoint_id, claim)?;
            (body_index, Some(binding))
        }
        SourceUnitSelector::ResolvedMember(member) => {
            let sites = resolved_member_sites
                .get(&(member.module.clone(), member.name.clone()))
                .with_context(|| {
                    format!("{endpoint_id}.resolved_member names no logical module member")
                })?;
            let mut provider_sites = sites
                .iter()
                .filter(|site| site.provider_chunk == chunk_name);
            let site = provider_sites.next().with_context(|| {
                format!(
                    "{endpoint_id}.resolved_member belongs to another provider, not {chunk_name}"
                )
            })?;
            ensure!(
                provider_sites.next().is_none(),
                "{endpoint_id}.resolved_member is ambiguous in provider {chunk_name}"
            );
            let declaration = site.declaration.as_ref().with_context(|| {
                format!("{endpoint_id}.resolved_member has no unique original local declaration")
            })?;
            (declaration.body_index, Some(declaration.binding.clone()))
        }
    };
    let item = body
        .get(body_index)
        .with_context(|| format!("{endpoint_id} selected a missing module item"))?;
    let first_ordinal = source_unit_ordinal_for_statement(body, StatementId(body_index));
    let declarator_offset = match (&selected_binding, split_var_declarators(item)) {
        (Some(binding), Some(declarators)) => declarators
            .iter()
            .position(|declarator| {
                binding_name_strings(&declarator.name)
                    .iter()
                    .any(|name| name == binding)
            })
            .with_context(|| {
                format!("{endpoint_id} selected binding `{binding}` outside its declaration")
            })?,
        _ => 0,
    };
    Ok(CommutingEndpoint {
        selector: selector.clone(),
        source_unit: SourceUnitOrdinal(first_ordinal.0 + declarator_offset),
        selected_binding,
    })
}
