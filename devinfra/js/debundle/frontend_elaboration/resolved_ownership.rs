//! Read exact source units from the canonical solve without planning a split.

use super::FrontendChunkInput;
use super::chunk_preparation::solve_selectors;
use anyhow::{Context, Result, ensure};
use artifact::ChunkBundle;
use source_identity::SourceUnitOrdinal;
use spec::{ModulePath, TransformSpec};
use std::collections::{BTreeMap, BTreeSet};
use swc_ecma_ast::{ModuleDecl, ModuleItem};

pub enum ClaimSelector {
    Member { binding: String, readable: String },
    Anonymous(usize),
}

pub struct OwnershipClaim {
    pub ordinal: SourceUnitOrdinal,
    pub module: ModulePath,
    pub selector: ClaimSelector,
}

pub struct CanonicalOwnership {
    pub unit_count: usize,
    pub claims: Vec<OwnershipClaim>,
}

/// Solve before selecting any owners: other modules and chunks are anchors.
pub fn resolve_ownership(
    artifact: &ChunkBundle,
    spec: &TransformSpec,
    diagnostics: &analysis::diagnostics::Diagnostics,
) -> Result<BTreeMap<String, CanonicalOwnership>> {
    let chunks = spec.materialize_chunk_ids();
    let inputs = chunks
        .iter()
        .map(|id| FrontendChunkInput {
            artifact,
            chunk_id: id,
            file: spec.materialize_logical_modules.file.as_deref(),
        })
        .collect();
    let mut result = BTreeMap::new();
    for (chunk, resolved) in solve_selectors(
        inputs,
        &spec.logical_modules,
        &spec.unassigned_mode,
        &BTreeSet::new(),
        true,
    )? {
        let claims = chunk.claims(
            &resolved.resolution,
            &resolved.resolved_members,
            true,
            diagnostics,
        )?;
        let facts = &resolved.structural_analysis.per_statement;
        let locations = &chunk.ast_analysis.source_unit_syntax_locations;
        ensure!(
            locations.len() == facts.len(),
            "inconsistent source-unit census in {}",
            chunk.chunk_id
        );
        let mut bindings = BTreeMap::<String, BTreeSet<SourceUnitOrdinal>>::new();
        let mut statements = BTreeMap::<_, Vec<_>>::new();
        for (i, (unit, location)) in facts.iter().zip(locations).enumerate() {
            ensure!(
                unit.ordinal.0 == i && unit.ordinal == location.ordinal,
                "inconsistent source-unit ordinal"
            );
            statements
                .entry(location.statement)
                .or_default()
                .push(unit.ordinal);
            for binding in &unit.declared {
                bindings
                    .entry(binding.0.to_string())
                    .or_default()
                    .insert(unit.ordinal);
            }
            if let ModuleItem::ModuleDecl(ModuleDecl::Import(import)) =
                &chunk.runtime_ast.module.body[location.statement.0]
            {
                for specifier in &import.specifiers {
                    bindings
                        .entry(specifier.local().sym.to_string())
                        .or_default()
                        .insert(unit.ordinal);
                }
            }
        }
        let mut owners = BTreeMap::new();
        let mut canonical = CanonicalOwnership {
            unit_count: facts.len(),
            claims: Vec::new(),
        };
        let mut module_paths = BTreeSet::new();
        for (r, request) in chunk.explicit_requests.iter().enumerate() {
            let module = ModulePath::parse(&request.target_path, "")?;
            ensure!(
                module_paths.insert(module.clone()),
                "{}: duplicate canonical module path {module}",
                chunk.chunk_id
            );
            let mut record = |ordinal: SourceUnitOrdinal, selector: ClaimSelector| -> Result<()> {
                // Distinct bindings can name the very same destructuring declarator.
                // A unit still has just one owner, even before filtering.
                let named = matches!(selector, ClaimSelector::Member { .. });
                if let Some((previous, previous_named)) =
                    owners.insert(ordinal, (module.clone(), named))
                {
                    ensure!(
                        previous == module && named && previous_named,
                        "{}: inconsistent claims on unit {} by {} and {}",
                        chunk.chunk_id,
                        ordinal.0,
                        previous,
                        module
                    );
                }
                canonical.claims.push(OwnershipClaim {
                    ordinal,
                    module: module.clone(),
                    selector,
                });
                Ok(())
            };
            for (m, member) in request.members.iter().enumerate() {
                let binding = claims.claimed_members.get(&(r, m)).with_context(|| {
                    format!("{}: unresolved member {}", request.id, member.export_name)
                })?;
                let units = bindings
                    .get(binding)
                    .with_context(|| format!("{}: no source unit for {binding}", request.id))?;
                ensure!(
                    units.len() == 1,
                    "{}: binding {binding} has {} source units",
                    request.id,
                    units.len()
                );
                record(
                    *units.first().unwrap(),
                    ClaimSelector::Member {
                        binding: binding.clone(),
                        readable: member.export_name.clone(),
                    },
                )?;
            }
            for i in 0..request.anonymous_statements.len() {
                let statement = claims
                    .claimed_anonymous_statements
                    .get(&(r, i))
                    .with_context(|| {
                        format!("{}: unresolved anonymous statement {i}", request.id)
                    })?;
                let units = statements
                    .get(statement)
                    .context("anonymous claim has no source unit")?;
                ensure!(
                    units.len() == 1,
                    "{}: anonymous selector {i} spans multiple source units",
                    request.id
                );
                record(units[0], ClaimSelector::Anonymous(i))?;
            }
        }
        result.insert(chunk.chunk_id, canonical);
    }
    Ok(result)
}
