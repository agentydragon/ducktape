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
use source_match::SelectorResolver;
use spec::AnonymousStatementSelector;
use swc_common::{EqIgnoreSpan, SyntaxContext};

#[derive(Debug, Clone, Copy)]
pub struct AnonymousStatementClaimSet<'a> {
    pub module_path: &'a Path,
    pub selectors: &'a BTreeSet<AnonymousStatementSelector>,
}

/// Member-form `selector.source_match` claims (including expanded
/// `binding_groups:` entries) for one module. Resolved by
/// [`resolve_member_selector_claims`] into the chunk-top binding
/// names they claim.
#[derive(Debug, Clone, Copy)]
pub struct MemberSelectorClaimSet<'a> {
    pub module_path: &'a Path,
    pub selectors: &'a BTreeSet<AnonymousStatementSelector>,
}

/// Resolve member-form `source_match` selectors to the chunk-top
/// binding names they claim, by matching each selector against the
/// chunk sources referenced by `graph`'s `source_location` data —
/// the same source-backed matching (`source_match`) the run
/// pipeline's member materialization applies. Each selector must
/// match exactly one declared binding across all chunk sources;
/// zero or multiple matches are hard errors, as is unresolvable
/// chunk source — the caller (the CLI edit gate) must never
/// silently treat a source_match-claimed owner as residual.
///
/// Bindings resolved to import specifiers are skipped: they refer
/// to upstream symbols, not chunk-top owners (same exclusion as
/// `spec_modules::load_active_claims`).
pub fn resolve_member_selector_claims(
    graph: &OwnerGraphReport,
    owner_graph_path: &Path,
    modules_root: &Path,
    source_root: Option<&Path>,
    claims_by_module: &[MemberSelectorClaimSet<'_>],
) -> Result<Vec<BTreeSet<String>>> {
    js_ast::with_swc_globals(|| {
        resolve_member_selector_claims_in_globals(
            graph,
            owner_graph_path,
            modules_root,
            source_root,
            claims_by_module,
        )
    })
}

fn resolve_member_selector_claims_in_globals(
    graph: &OwnerGraphReport,
    owner_graph_path: &Path,
    modules_root: &Path,
    source_root: Option<&Path>,
    claims_by_module: &[MemberSelectorClaimSet<'_>],
) -> Result<Vec<BTreeSet<String>>> {
    if claims_by_module
        .iter()
        .all(|claims| claims.selectors.is_empty())
    {
        return Ok(vec![BTreeSet::new(); claims_by_module.len()]);
    }

    with_source_resolvers(
        graph,
        owner_graph_path,
        modules_root,
        source_root,
        "spec contains members[].selector.source_match claims, but owner_graph.json has no \
         source_location data; cannot resolve source_match selectors",
        |parsed_by_source, resolvers_by_source| {
            let mut out = vec![BTreeSet::<String>::new(); claims_by_module.len()];
            for (module_idx, claims) in claims_by_module.iter().enumerate() {
                let request_id = claims.module_path.to_string_lossy();
                for selector in claims.selectors {
                    let mut matches = Vec::new();
                    for source_path in parsed_by_source.keys() {
                        matches.extend(
                            resolvers_by_source[source_path]
                                .member_candidates(&request_id, selector)?,
                        );
                    }
                    match matches.as_slice() {
                        [single] => {
                            if !matches!(
                                single.binding.kind,
                                Some(spec::BindingSourceKind::ImportSpecifier)
                            ) {
                                out[module_idx].insert(single.binding.binding_name.clone());
                            }
                        }
                        [] => bail!(
                            "module {} members[].selector.source_match did not match any top-level \
                     declaration in the chunk sources:\n{}",
                            claims.module_path.display(),
                            selector.match_source,
                        ),
                        multiple => bail!(
                            "module {} members[].selector.source_match is ambiguous — matched {} \
                     declarations ({}); refine the selector:\n{}",
                            claims.module_path.display(),
                            multiple.len(),
                            multiple
                                .iter()
                                .map(|matched| matched.binding.binding_name.as_str())
                                .collect::<Vec<_>>()
                                .join(", "),
                            selector.match_source,
                        ),
                    }
                }
            }

            Ok(out)
        },
    )
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
        let parsed =
            read_and_parse_source(source_path, source_root, owner_graph_path, modules_root)?;
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
    if claims_by_module
        .iter()
        .all(|claims| claims.selectors.is_empty())
    {
        return Ok(vec![BTreeSet::new(); claims_by_module.len()]);
    }

    with_source_resolvers(
        graph,
        owner_graph_path,
        modules_root,
        source_root,
        "spec contains anonymous_statements, but owner_graph.json has no source_location \
         data; cannot resolve anonymous selectors",
        |parsed_by_source, resolvers_by_source| {
            let mut out = vec![BTreeSet::<OwnerId>::new(); claims_by_module.len()];
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

            for (module_idx, claims) in claims_by_module.iter().enumerate() {
                for selector in claims.selectors {
                    let mut match_groups = Vec::<Vec<(String, OwnerId)>>::new();
                    for (source_path, parsed) in parsed_by_source {
                        let body_index_groups = resolvers_by_source[source_path]
                            .resolve_anonymous_groups(
                                &claims.module_path.to_string_lossy(),
                                selector,
                            )?;
                        for body_indices in body_index_groups {
                            let mut owners = Vec::with_capacity(body_indices.len());
                            for body_idx in body_indices {
                                let statement_ordinal = js_ast::statement_ordinal_for_body_index(
                                    &parsed.module.body,
                                    body_idx,
                                );
                                let Some(&owner) = anonymous_owner_by_source_ordinal
                                    .get(&(source_path.clone(), statement_ordinal))
                                else {
                                    bail!(
                                        "module {} anonymous statement selector matched source {} body \
                                 index {} / statement ordinal {}, but owner_graph.json has no \
                                 anonymous owner at that source position",
                                        claims.module_path.display(),
                                        source_path,
                                        body_idx,
                                        statement_ordinal,
                                    );
                                };
                                owners.push((source_path.clone(), owner));
                            }
                            match_groups.push(owners);
                        }
                    }
                    match match_groups.as_slice() {
                        [owners] => {
                            out[module_idx].extend(owners.iter().map(|(_, owner)| *owner));
                        }
                        [] => bail!(
                            "module {} anonymous statement selector did not match any source statement:\n{}",
                            claims.module_path.display(),
                            selector.match_source,
                        ),
                        multiple => bail!(
                            "module {} anonymous statement selector matched {} source statement groups; \
                     refine the selector:\n{}",
                            claims.module_path.display(),
                            multiple.len(),
                            selector.match_source,
                        ),
                    }
                }
            }

            Ok(out)
        },
    )
}

/// Parse every distinct `source_location.source_path` in `graph` and hand
/// `body` a per-source `ChunkResolver` map alongside the parsed modules.
/// Resolvers are built once and borrow the parsed modules (the build-once seam
/// contract; the fact resolver would otherwise rebuild a per-source EDB on every
/// selector), which is why the parsed map and its resolvers are produced
/// together and lent through the closure rather than returned. `no_sources` is
/// the caller-specific error raised when the graph carries no `source_location`
/// data.
fn with_source_resolvers<R>(
    graph: &OwnerGraphReport,
    owner_graph_path: &Path,
    modules_root: &Path,
    source_root: Option<&Path>,
    no_sources: &str,
    body: impl for<'m> FnOnce(
        &'m BTreeMap<String, js_ast::ParsedJsModule>,
        &BTreeMap<String, source_match::ChunkResolver<'m>>,
    ) -> Result<R>,
) -> Result<R> {
    let source_paths: BTreeSet<String> = graph
        .nodes
        .iter()
        .filter_map(|node| node.source_location.as_ref())
        .map(|location| location.source_path.clone())
        .collect();
    if source_paths.is_empty() {
        bail!("{no_sources}");
    }

    let parsed_by_source: BTreeMap<String, js_ast::ParsedJsModule> = source_paths
        .iter()
        .map(|source_path| {
            Ok((
                source_path.clone(),
                read_and_parse_source(source_path, source_root, owner_graph_path, modules_root)?,
            ))
        })
        .collect::<Result<_>>()?;
    let resolvers_by_source: BTreeMap<_, _> = parsed_by_source
        .iter()
        .map(|(source_path, parsed)| {
            (
                source_path.clone(),
                source_match::ChunkResolver::new(&parsed.module),
            )
        })
        .collect();
    body(&parsed_by_source, &resolvers_by_source)
}

/// Resolve `source_path` to a file on disk, read it, and parse it as a JS
/// module. The resolve/read/parse trio every selector-claim resolver in this
/// module repeats; the read and parse error contexts name the resolved file.
fn read_and_parse_source(
    source_path: &str,
    source_root: Option<&Path>,
    owner_graph_path: &Path,
    modules_root: &Path,
) -> Result<js_ast::ParsedJsModule> {
    let resolved = resolve_source_file(source_path, source_root, owner_graph_path, modules_root)?;
    let source = fs::read_to_string(&resolved)
        .with_context(|| format!("reading source file {}", resolved.display()))?;
    js_ast::parse_js_module(source_path, &source)
        .with_context(|| format!("parsing source file {}", resolved.display()))
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
