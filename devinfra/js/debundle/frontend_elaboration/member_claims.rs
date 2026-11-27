//! Projects resolved selector claims onto owned bindings, members and
//! anonymous statements.

use super::MemberClaimProjection;
use crate::chunk_ast::{ChunkAstAnalysis, binding_declaration};
use anyhow::{Context, Result, bail, ensure};
use selector_outcome::{Entity, EntityRef, Outcome, SelectorOutcome, Severity};
use source_identity::StatementId;
use spec_resolution::LogicalRequest;
use std::collections::BTreeMap;
use swc_ecma_ast::ModuleItem;

pub struct MemberClaimInput<'a> {
    pub chunk_id: &'a str,
    pub requests: &'a [LogicalRequest],
    pub resolution: &'a selector_resolve::Resolution,
    pub resolved_members: &'a [Vec<usize>],
    pub ast: &'a ChunkAstAnalysis,
    pub top_level_mark: swc_common::Mark,
    pub body: &'a [ModuleItem],
    pub fail_fast: bool,
    pub diagnostics: &'a analysis::diagnostics::Diagnostics<'a>,
}

pub fn project_member_claims(input: MemberClaimInput<'_>) -> Result<MemberClaimProjection> {
    let MemberClaimInput {
        chunk_id,
        requests,
        resolution,
        resolved_members,
        ast,
        top_level_mark,
        body,
        fail_fast,
        diagnostics,
    } = input;
    struct ExistingClaim {
        request_index: usize,
        export_name: String,
        claim_origin: String,
        imported: bool,
    }

    let mut owners = BTreeMap::<String, ExistingClaim>::new();
    let mut anonymous_owners = BTreeMap::<StatementId, (usize, String)>::new();
    let mut claimed_anonymous_statements = BTreeMap::new();
    let mut claimed_members = BTreeMap::new();
    for (request_index, request) in requests.iter().enumerate() {
        for (member_index, member) in request.members.iter().enumerate() {
            if member.selector.is_import_specifier()
                && !member.binding.is_empty()
                && let std::collections::btree_map::Entry::Vacant(entry) =
                    owners.entry(member.binding.clone())
            {
                entry.insert(ExistingClaim {
                    request_index,
                    export_name: member.export_name.clone(),
                    claim_origin: member.claim_origin.clone(),
                    imported: true,
                });
                claimed_members.insert((request_index, member_index), member.binding.clone());
            }
        }
    }
    for outcome in &resolution.outcomes {
        record_frontend_outcome(&outcome.outcome, fail_fast, diagnostics)?;
        let request = requests.get(outcome.module).with_context(|| {
            format!(
                "selector outcome names missing logical module {}",
                outcome.module
            )
        })?;
        if !matches!(outcome.outcome.outcome, Outcome::Resolved { .. }) {
            continue;
        }
        if let selector_resolve::EntityIndex::AnonymousStatement(statement_index) = outcome.entity
            && let Outcome::Resolved { body_index, .. } = outcome.outcome.outcome
        {
            let statement = StatementId(body_index);
            ensure!(
                request.anonymous_statements.get(statement_index).is_some(),
                "selector outcome names missing anonymous statement {} in logical module {}",
                statement_index,
                request.id,
            );
            if let Some((_, existing_module)) = anonymous_owners.get(&statement) {
                bail!(
                    "anonymous_statements[].match in module {} also matches the top-level statement at body index {} already claimed by module {}; each anonymous statement may belong to at most one logical module.",
                    request.id,
                    statement.0,
                    existing_module,
                );
            }
            anonymous_owners.insert(statement, (outcome.module, request.id.clone()));
            claimed_anonymous_statements.insert((outcome.module, statement_index), statement);
            continue;
        }
        let selector_resolve::EntityIndex::Member(projected_index) = outcome.entity else {
            continue;
        };
        let Some(member_index) = resolved_members
            .get(outcome.module)
            .and_then(|indices| indices.get(projected_index))
            .copied()
        else {
            bail!(
                "selector outcome names missing projected member {} in logical module {}",
                projected_index,
                request.id
            );
        };
        let member = request.members.get(member_index).with_context(|| {
            format!(
                "selector outcome names missing member {} in logical module {}",
                member_index, request.id
            )
        })?;
        let Outcome::Resolved { binding, .. } = &outcome.outcome.outcome else {
            unreachable!("non-resolved selector outcomes were handled above")
        };
        let binding = binding.as_ref().with_context(|| {
            format!(
                "logical_module {}: global selector solver resolved member `{}` to a body item without a single declared binding",
                request.id, member.export_name
            )
        })?;
        let binding_id = analysis::top_level_id(binding, top_level_mark);
        if !ast.declaration_by_name.contains_key(&binding_id) {
            continue;
        }
        if let Some(existing) = owners.get(binding) {
            if existing.request_index == outcome.module
                && !existing.imported
                && member.selector.source_match().is_some()
            {
                bail!(
                    "logical_module {} has duplicate source binding claims:\n- source binding `{}` claimed 2 times:\n  - export `{}` ({})\n  - export `{}` ({})",
                    request.id,
                    binding,
                    existing.export_name,
                    existing.claim_origin,
                    member.export_name,
                    member.claim_origin,
                );
            }
            if fail_fast {
                let declaration = binding_declaration(body, &ast.declaration_by_name, &binding_id)?;
                let duplicate = selector_resolve::member_outcome(
                    chunk_id,
                    &request.target_path,
                    &member.export_name,
                    &member.selector,
                    Outcome::DuplicateClaim {
                        binding: binding.clone(),
                        declaration,
                        claimed_by: EntityRef {
                            logical_module: requests[existing.request_index].target_path.clone(),
                            entity: Some(Entity::Export(existing.export_name.clone())),
                        },
                    },
                );
                bail!(selector_outcome::SelectorFailure::Single(Box::new(
                    duplicate
                )));
            }
            continue;
        }
        owners.insert(
            binding.clone(),
            ExistingClaim {
                request_index: outcome.module,
                export_name: member.export_name.clone(),
                claim_origin: member.claim_origin.clone(),
                imported: false,
            },
        );
        claimed_members.insert((outcome.module, member_index), binding.clone());
    }
    Ok(MemberClaimProjection {
        owned_bindings: owners.into_keys().collect(),
        claimed_members,
        claimed_anonymous_statements,
    })
}

fn record_frontend_outcome(
    outcome: &SelectorOutcome,
    fail_fast: bool,
    diagnostics: &analysis::diagnostics::Diagnostics,
) -> Result<()> {
    match outcome.severity() {
        Severity::Error if fail_fast => bail!(selector_outcome::SelectorFailure::Single(Box::new(
            outcome.clone()
        ))),
        Severity::Warning => diagnostics.record_chunk(
            &outcome.chunk,
            analysis::diagnostics::ChunkDiagnostic::Selector(Box::new(outcome.clone())),
        ),
        Severity::Error => {}
        Severity::Ok => {}
    }
    Ok(())
}
