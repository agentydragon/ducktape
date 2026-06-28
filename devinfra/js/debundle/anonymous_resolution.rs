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

use analysis::{
    AnalysisHints, ChunkId, OwnerGraphReport, OwnerId, StatementKind, analyze_chunk,
    build_owner_graph,
};
use anyhow::{Context, Result, bail};
use selector_ir::{ClaimOutcome, ResolvedClaim, SelectorFact, SelectorFactStore, SelectorTargetId};
use selector_ir_lowering::{
    MemberSelectorLoweringContext, MemberSelectorProgramBuilder, lower_member_selector,
};
use selector_runtime::solve_global_selector_program;
use spec::{AnonymousStatementSelector, MemberSelectorSpec};
use swc_common::{EqIgnoreSpan, SyntaxContext};
use swc_ecma_ast::Module;

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
/// chunk sources referenced by `graph`'s `source_location` data through
/// the same selector-IR/CP-SAT path the run pipeline applies. Each selector must
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

    with_source_modules(
        graph,
        owner_graph_path,
        modules_root,
        source_root,
        "spec contains members[].selector.source_match claims, but owner_graph.json has no \
         source_location data; cannot resolve source_match selectors",
        |parsed_by_source| {
            let facts_by_source: BTreeMap<String, SelectorFactStore> = parsed_by_source
                .iter()
                .map(|(source_path, parsed)| {
                    Ok((
                        source_path.clone(),
                        selector_fact_store_for_module(&parsed.module).with_context(|| {
                            format!("building selector facts for source {source_path}")
                        })?,
                    ))
                })
                .collect::<Result<_>>()?;
            let mut out = vec![BTreeSet::<String>::new(); claims_by_module.len()];
            for (module_idx, claims) in claims_by_module.iter().enumerate() {
                let request_id = claims.module_path.to_string_lossy();
                for selector in claims.selectors {
                    let mut matches = Vec::<MemberSelectorMatch>::new();
                    for (source_path, facts) in &facts_by_source {
                        for claim in
                            resolve_member_source_match_claims(facts, &request_id, selector)?
                        {
                            let binding_name = claim.binding.clone().with_context(|| {
                                format!(
                                    "module {} members[].selector.source_match matched source {} \
                                     owner {:?} without a single declared binding; use \
                                     target_binding or a single-binding selector",
                                    claims.module_path.display(),
                                    source_path,
                                    claim.owner,
                                )
                            })?;
                            matches.push(MemberSelectorMatch {
                                binding_name,
                                is_import_specifier: solver_claim_is_import_specifier(
                                    facts, &claim,
                                ),
                            });
                        }
                    }
                    match matches.as_slice() {
                        [single] => {
                            if !single.is_import_specifier {
                                out[module_idx].insert(single.binding_name.clone());
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
                                .map(|matched| matched.binding_name.as_str())
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

#[derive(Debug, Clone)]
struct MemberSelectorMatch {
    binding_name: String,
    is_import_specifier: bool,
}

fn resolve_member_source_match_claims(
    facts: &SelectorFactStore,
    logical_module: &str,
    selector: &AnonymousStatementSelector,
) -> Result<Vec<ResolvedClaim>> {
    let selector = MemberSelectorSpec::SourceMatch(selector.clone());
    let lowered = lower_member_selector(
        &MemberSelectorLoweringContext::new(ChunkId(0), logical_module),
        "candidate",
        &selector,
    )
    .with_context(|| "lowering members[].selector.source_match to selector IR")?;
    let result = solve_global_selector_program(&lowered.program, facts)
        .with_context(|| "solving members[].selector.source_match selector IR")?;
    let outcome = result
        .outcome_for(lowered.target)
        .with_context(|| "selector solver did not return the member source_match target")?;
    match outcome {
        ClaimOutcome::Unique { claim } => Ok(vec![claim.clone()]),
        ClaimOutcome::Ambiguous { candidates } => Ok(candidates.clone()),
        ClaimOutcome::NoMatch => Ok(Vec::new()),
        ClaimOutcome::Unsupported { message } => {
            bail!("members[].selector.source_match is unsupported by selector IR solver: {message}")
        }
        ClaimOutcome::Duplicate {
            owner,
            conflicting_targets,
        } => bail!(
            "members[].selector.source_match produced a duplicate claim for owner {owner:?} \
             across {conflicting_targets:?}",
        ),
    }
}

fn selector_fact_store_for_module(module: &Module) -> Result<SelectorFactStore> {
    let analysis = analyze_chunk(module, &AnalysisHints::default(), None, |_| None);
    let owner_graph = build_owner_graph(&analysis.facts)?;
    let chunk_id = ChunkId(0);
    let mut facts = SelectorFactStore::default();
    facts.extend_chunk_facts(
        chunk_id,
        &chunk_facts::extract_facts(module).map_err(|unsupported| {
            anyhow::anyhow!(
                "selector AST fact extraction failed at {}; edit-gate source_match resolution \
                 needs a complete AST EDB",
                unsupported.context
            )
        })?,
    );
    facts.extend_owner_graph_facts(chunk_id, &owner_graph);
    Ok(facts)
}

fn solver_claim_is_import_specifier(facts: &SelectorFactStore, claim: &ResolvedClaim) -> bool {
    facts.facts.iter().any(|fact| {
        matches!(
            fact,
            SelectorFact::Owner {
                owner,
                statement_ordinal,
                statement_kind,
                ..
            } if *owner == claim.owner
                && *statement_ordinal == claim.statement_ordinal
                && statement_kind == "import"
        )
    })
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

    with_source_modules(
        graph,
        owner_graph_path,
        modules_root,
        source_root,
        "spec contains anonymous_statements, but owner_graph.json has no source_location \
         data; cannot resolve anonymous selectors",
        |parsed_by_source| {
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

            let anonymous_claims =
                claims_by_module
                    .iter()
                    .enumerate()
                    .flat_map(|(module_idx, claims)| {
                        let request_id = claims.module_path.to_string_lossy().to_string();
                        claims.selectors.iter().enumerate().map(
                            move |(selector_index, selector)| AnonymousSelectorClaimInfo {
                                module_idx,
                                selector_index,
                                module_path: claims.module_path,
                                request_id: request_id.clone(),
                                selector,
                            },
                        )
                    })
                    .collect::<Vec<_>>();
            let mut match_groups_by_claim =
                vec![Vec::<Vec<(String, OwnerId)>>::new(); anonymous_claims.len()];

            for (source_path, parsed) in parsed_by_source {
                let mut builder = MemberSelectorProgramBuilder::new(
                    MemberSelectorLoweringContext::new(ChunkId(0), source_path),
                );
                let mut targets = Vec::<AnonymousSelectorTargetInfo>::new();
                for (claim_idx, claim) in anonymous_claims.iter().enumerate() {
                    let target = builder
                        .declare_native_anonymous_statement_target_in_module(
                            &claim.request_id,
                            claim.selector_index,
                            claim.selector,
                        )
                        .with_context(|| {
                            format!(
                                "module {} anonymous statement selector cannot be lowered into \
                                 native selector IR",
                                claim.module_path.display(),
                            )
                        })?;
                    targets.push(AnonymousSelectorTargetInfo { claim_idx, target });
                }
                if targets.is_empty() {
                    continue;
                }
                let program = builder.into_program()?;
                let facts = selector_fact_store_for_module(&parsed.module)
                    .with_context(|| format!("building selector facts for source {source_path}"))?;
                let result =
                    solve_global_selector_program(&program, &facts).with_context(|| {
                        format!("solving anonymous selectors for source {source_path}")
                    })?;
                for target in targets {
                    let claim = &anonymous_claims[target.claim_idx];
                    let outcome = result.outcome_for(target.target).with_context(|| {
                        format!(
                            "selector solver did not return anonymous statement target for module {}",
                            claim.module_path.display(),
                        )
                    })?;
                    let groups = anonymous_match_groups_for_outcome(
                        source_path,
                        claim,
                        outcome,
                        &anonymous_owner_by_source_ordinal,
                    )?;
                    match_groups_by_claim[target.claim_idx].extend(groups);
                }
            }

            for (claim, match_groups) in anonymous_claims.iter().zip(match_groups_by_claim) {
                match match_groups.as_slice() {
                    [owners] => {
                        out[claim.module_idx].extend(owners.iter().map(|(_, owner)| *owner));
                    }
                    [] => bail!(
                        "module {} anonymous statement selector did not match any source statement:\n{}",
                        claim.module_path.display(),
                        claim.selector.match_source,
                    ),
                    multiple => bail!(
                        "module {} anonymous statement selector matched {} source statement groups; \
                 refine the selector:\n{}",
                        claim.module_path.display(),
                        multiple.len(),
                        claim.selector.match_source,
                    ),
                }
            }
            Ok(out)
        },
    )
}

#[derive(Debug, Clone)]
struct AnonymousSelectorClaimInfo<'a> {
    module_idx: usize,
    selector_index: usize,
    module_path: &'a Path,
    request_id: String,
    selector: &'a AnonymousStatementSelector,
}

#[derive(Debug, Clone, Copy)]
struct AnonymousSelectorTargetInfo {
    claim_idx: usize,
    target: SelectorTargetId,
}

fn anonymous_match_groups_for_outcome(
    source_path: &str,
    claim: &AnonymousSelectorClaimInfo<'_>,
    outcome: &ClaimOutcome,
    anonymous_owner_by_source_ordinal: &HashMap<(String, usize), OwnerId>,
) -> Result<Vec<Vec<(String, OwnerId)>>> {
    match outcome {
        ClaimOutcome::Unique { claim: resolved } => Ok(vec![vec![anonymous_owner_for_claim(
            source_path,
            claim,
            resolved,
            anonymous_owner_by_source_ordinal,
        )?]]),
        ClaimOutcome::Ambiguous { candidates } => candidates
            .iter()
            .map(|resolved| {
                Ok(vec![anonymous_owner_for_claim(
                    source_path,
                    claim,
                    resolved,
                    anonymous_owner_by_source_ordinal,
                )?])
            })
            .collect(),
        ClaimOutcome::NoMatch => Ok(Vec::new()),
        ClaimOutcome::Unsupported { message } => {
            bail!(
                "module {} anonymous statement selector is unsupported by selector IR solver: {message}",
                claim.module_path.display(),
            )
        }
        ClaimOutcome::Duplicate {
            owner,
            conflicting_targets,
        } => bail!(
            "module {} anonymous statement selector produced a duplicate claim for owner {owner:?} \
             across {conflicting_targets:?}",
            claim.module_path.display(),
        ),
    }
}

fn anonymous_owner_for_claim(
    source_path: &str,
    claim: &AnonymousSelectorClaimInfo<'_>,
    resolved: &ResolvedClaim,
    anonymous_owner_by_source_ordinal: &HashMap<(String, usize), OwnerId>,
) -> Result<(String, OwnerId)> {
    let statement_ordinal = resolved.statement_ordinal.0;
    let Some(&owner) =
        anonymous_owner_by_source_ordinal.get(&(source_path.to_string(), statement_ordinal))
    else {
        bail!(
            "module {} anonymous statement selector matched source {} statement ordinal {}, \
             but owner_graph.json has no anonymous owner at that source position",
            claim.module_path.display(),
            source_path,
            statement_ordinal,
        );
    };
    Ok((source_path.to_string(), owner))
}

/// Parse every distinct `source_location.source_path` in `graph` and hand
/// `body` the parsed modules. `no_sources` is the caller-specific error raised
/// when the graph carries no `source_location` data.
fn with_source_modules<R>(
    graph: &OwnerGraphReport,
    owner_graph_path: &Path,
    modules_root: &Path,
    source_root: Option<&Path>,
    no_sources: &str,
    body: impl FnOnce(&BTreeMap<String, js_ast::ParsedJsModule>) -> Result<R>,
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
    body(&parsed_by_source)
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
