//! Source-backed anonymous statement selector resolution.
//!
//! Spec files identify anonymous top-level statements by JS selector
//! snippets (`anonymous_statements[].match`). Graph-backed CLI paths
//! that need owner ids use this module as the single implementation
//! for mapping matched selectors back to owner-graph nodes. The
//! selector parser and AST equality live in `source_match`.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::env;
use std::fs;
use std::path::{Path, PathBuf};

use analysis::{OwnerGraphReport, OwnerId, StatementKind};
use anyhow::{Context, Result, bail};
use spec::AnonymousStatementSelector;
use swc_common::{EqIgnoreSpan, SyntaxContext};

#[derive(Debug, Clone, Copy)]
pub struct AnonymousStatementClaimSet<'a> {
    pub module_path: &'a Path,
    pub selectors: &'a BTreeSet<AnonymousStatementSelector>,
}

pub fn resolve_anonymous_statement_claims(
    graph: &OwnerGraphReport,
    owner_graph_path: &Path,
    modules_root: &Path,
    source_root: Option<&Path>,
    claims_by_module: &[AnonymousStatementClaimSet<'_>],
) -> Result<Vec<BTreeSet<OwnerId>>> {
    js_ast::with_swc_globals(|| {
        resolve_anonymous_statement_claims_in_globals(
            graph,
            owner_graph_path,
            modules_root,
            source_root,
            claims_by_module,
        )
    })
}

pub fn addressable_anonymous_statement_owner_ids(
    graph: &OwnerGraphReport,
    owner_graph_path: &Path,
    modules_root: &Path,
    source_root: Option<&Path>,
) -> Result<BTreeSet<String>> {
    js_ast::with_swc_globals(|| {
        addressable_anonymous_statement_owner_ids_in_globals(
            graph,
            owner_graph_path,
            modules_root,
            source_root,
        )
    })
}

fn addressable_anonymous_statement_owner_ids_in_globals(
    graph: &OwnerGraphReport,
    owner_graph_path: &Path,
    modules_root: &Path,
    source_root: Option<&Path>,
) -> Result<BTreeSet<String>> {
    let mut owner_by_source_ordinal = HashMap::<(String, usize), &str>::new();
    let mut source_paths = BTreeSet::<String>::new();
    for node in &graph.nodes {
        if !node.declared_bindings.is_empty()
            || matches!(
                node.statement_kind,
                StatementKind::Import | StatementKind::Export
            )
        {
            continue;
        }
        if let Some(location) = &node.source_location {
            source_paths.insert(location.source_path.clone());
            owner_by_source_ordinal.insert(
                (location.source_path.clone(), node.statement_ordinal.0),
                &node.id,
            );
        }
    }

    let mut out = BTreeSet::<String>::new();
    for source_path in &source_paths {
        let resolved =
            resolve_source_file(source_path, source_root, owner_graph_path, modules_root)?;
        let source = fs::read_to_string(&resolved)
            .with_context(|| format!("reading source file {}", resolved.display()))?;
        let parsed = js_ast::parse_js_module(source_path, &source)
            .with_context(|| format!("parsing source file {}", resolved.display()))?;
        let unique_body_indices: BTreeSet<usize> = SyntaxContext::within_ignored_ctxt(|| {
            parsed
                .module
                .body
                .iter()
                .enumerate()
                .filter_map(|(body_idx, item)| {
                    let match_count = parsed
                        .module
                        .body
                        .iter()
                        .filter(|candidate| item.eq_ignore_span(candidate))
                        .take(2)
                        .count();
                    (match_count == 1).then_some(body_idx)
                })
                .collect()
        });
        for body_idx in unique_body_indices {
            let statement_ordinal =
                js_ast::statement_ordinal_for_body_index(&parsed.module.body, body_idx);
            if let Some(owner_id) =
                owner_by_source_ordinal.get(&(source_path.clone(), statement_ordinal))
            {
                out.insert((*owner_id).to_string());
            }
        }
    }
    Ok(out)
}

fn resolve_anonymous_statement_claims_in_globals(
    graph: &OwnerGraphReport,
    owner_graph_path: &Path,
    modules_root: &Path,
    source_root: Option<&Path>,
    claims_by_module: &[AnonymousStatementClaimSet<'_>],
) -> Result<Vec<BTreeSet<OwnerId>>> {
    let mut out = vec![BTreeSet::<OwnerId>::new(); claims_by_module.len()];
    if claims_by_module
        .iter()
        .all(|claims| claims.selectors.is_empty())
    {
        return Ok(out);
    }

    let source_paths: BTreeSet<String> = graph
        .nodes
        .iter()
        .filter_map(|node| node.source_location.as_ref())
        .map(|location| location.source_path.clone())
        .collect();
    if source_paths.is_empty() {
        bail!(
            "spec contains anonymous_statements, but owner_graph.json has no source_location \
             data; cannot resolve anonymous selectors"
        );
    }

    let mut anonymous_owner_by_source_ordinal = HashMap::<(String, usize), OwnerId>::new();
    for (idx, node) in graph.nodes.iter().enumerate() {
        if !node.declared_bindings.is_empty() {
            continue;
        }
        if let Some(location) = &node.source_location {
            anonymous_owner_by_source_ordinal.insert(
                (location.source_path.clone(), node.statement_ordinal.0),
                OwnerId(idx),
            );
        }
    }

    let mut parsed_by_source = BTreeMap::new();
    for source_path in &source_paths {
        let resolved =
            resolve_source_file(source_path, source_root, owner_graph_path, modules_root)?;
        let source = fs::read_to_string(&resolved)
            .with_context(|| format!("reading source file {}", resolved.display()))?;
        let parsed = js_ast::parse_js_module(source_path, &source)
            .with_context(|| format!("parsing source file {}", resolved.display()))?;
        parsed_by_source.insert(source_path.clone(), parsed);
    }

    for (module_idx, claims) in claims_by_module.iter().enumerate() {
        for selector in claims.selectors {
            let mut matches = Vec::<(String, OwnerId)>::new();
            for (source_path, parsed) in &parsed_by_source {
                let body_indices = source_match::find_anonymous_statement_body_indices(
                    &parsed.module,
                    &claims.module_path.to_string_lossy(),
                    selector,
                )?;
                for body_idx in body_indices {
                    let statement_ordinal =
                        js_ast::statement_ordinal_for_body_index(&parsed.module.body, body_idx);
                    let Some(&owner) = anonymous_owner_by_source_ordinal
                        .get(&(source_path.clone(), statement_ordinal))
                    else {
                        bail!(
                            "module {} anonymous statement selector matched source {} body index \
                             {} / statement ordinal {}, but owner_graph.json has no anonymous \
                             owner at that source position",
                            claims.module_path.display(),
                            source_path,
                            body_idx,
                            statement_ordinal,
                        );
                    };
                    matches.push((source_path.clone(), owner));
                }
            }
            match matches.as_slice() {
                [(_, owner)] => {
                    out[module_idx].insert(*owner);
                }
                [] => bail!(
                    "module {} anonymous statement selector did not match any source statement:\n{}",
                    claims.module_path.display(),
                    selector.match_source,
                ),
                multiple => bail!(
                    "module {} anonymous statement selector matched {} source statements; refine \
                     the selector:\n{}",
                    claims.module_path.display(),
                    multiple.len(),
                    selector.match_source,
                ),
            }
        }
    }

    Ok(out)
}

fn resolve_source_file(
    source_path: &str,
    source_root: Option<&Path>,
    owner_graph_path: &Path,
    modules_root: &Path,
) -> Result<PathBuf> {
    let mut candidates = Vec::new();
    let source = PathBuf::from(source_path);
    if source.is_absolute() {
        candidates.push(source);
    } else {
        if let Some(root) = source_root {
            candidates.push(root.join(source_path));
        }
        if let Ok(cwd) = env::current_dir() {
            candidates.push(cwd.join(source_path));
        }
        push_relative_candidate(&mut candidates, owner_graph_path.parent(), source_path);
        push_relative_candidate(
            &mut candidates,
            owner_graph_path.parent().and_then(Path::parent),
            source_path,
        );
        push_relative_candidate(&mut candidates, modules_root.parent(), source_path);
        push_relative_candidate(
            &mut candidates,
            modules_root.parent().and_then(Path::parent),
            source_path,
        );
    }
    dedup_paths(&mut candidates);
    for candidate in &candidates {
        if candidate.is_file() {
            return Ok(candidate.clone());
        }
    }
    bail!(
        "could not resolve source path {source_path:?}; pass --source-root. Tried: {}",
        candidates
            .iter()
            .map(|path| path.display().to_string())
            .collect::<Vec<_>>()
            .join(", ")
    )
}

fn push_relative_candidate(candidates: &mut Vec<PathBuf>, root: Option<&Path>, source_path: &str) {
    if let Some(root) = root {
        candidates.push(root.join(source_path));
    }
}

fn dedup_paths(paths: &mut Vec<PathBuf>) {
    let mut seen = BTreeSet::new();
    paths.retain(|path| seen.insert(path.display().to_string()));
}
