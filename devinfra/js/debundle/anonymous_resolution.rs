//! Source-backed selector claim resolution for graph-backed CLI paths.
//!
//! Spec files claim declarations by `source_matches[]` templates and anonymous
//! top-level statements by `anonymous_statements[]` selectors. The CLI edit
//! gate and `peel` need those claims as owner-graph facts: the binding names a
//! module's `source_matches[]` claim and the anonymous owners it co-moves. They
//! get them from [`selector_resolve`], the resolve `debundle run` uses, over
//! each chunk source the owner graph's `source_location` data names.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::env;
use std::fs;
use std::path::{Path, PathBuf};

use analysis::{OwnerGraphReport, OwnerId, StatementKind};
use anyhow::{Context, Result, bail};
use selector_outcome::{Candidate, Outcome, SelectorOutcome, Severity};
use selector_resolve::{AnonymousStatement, EntityIndex, Member, MemberSelector, SpecModule};
use source_match::ParsedSourceMatchSelector;
use spec::{AnonymousStatementSelector, SourceMatchClaim};
use swc_common::{EqIgnoreSpan, SyntaxContext};

/// One module's source-backed claims.
#[derive(Debug, Clone, Copy)]
pub struct SourceClaimSet<'a> {
    pub module_path: &'a Path,
    pub source_matches: &'a [SourceMatchClaim],
    pub anonymous_selectors: &'a BTreeSet<AnonymousStatementSelector>,
}

/// What one module's source-backed claims resolve to.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ResolvedSourceClaims {
    /// Chunk-top binding names its `source_matches[]` claim.
    pub bindings: BTreeSet<String>,
    pub anonymous_owners: BTreeSet<OwnerId>,
}

/// Resolve every module's claims together against each chunk source the
/// owner graph names. An entity must resolve in exactly one source and match
/// nowhere else; anything else is an error, since the caller (the CLI edit
/// gate) must never silently treat a claimed owner as residual. As in `run`, a
/// claim unique only because other claims took its alternatives resolves, with
/// a warning on stderr.
pub fn resolve_source_claims(
    graph: &OwnerGraphReport,
    owner_graph_path: &Path,
    modules_root: &Path,
    source_root: Option<&Path>,
    claim_sets: &[SourceClaimSet<'_>],
) -> Result<Vec<ResolvedSourceClaims>> {
    resolve_claim_sets(
        graph,
        owner_graph_path,
        modules_root,
        source_root,
        claim_sets,
    )?
    .into_iter()
    .map(|resolved| resolved.map(ResolvedClaimSet::warn))
    .collect()
}

/// [`resolve_source_claims`] for `claim_sets[index]` alone, resolved together
/// with the others: an error in another set does not fail it.
pub fn resolve_source_claims_of(
    graph: &OwnerGraphReport,
    owner_graph_path: &Path,
    modules_root: &Path,
    source_root: Option<&Path>,
    claim_sets: &[SourceClaimSet<'_>],
    index: usize,
) -> Result<ResolvedSourceClaims> {
    resolve_claim_sets(
        graph,
        owner_graph_path,
        modules_root,
        source_root,
        claim_sets,
    )?
    .swap_remove(index)
    .map(ResolvedClaimSet::warn)
}

/// A claim set's resolution and the warnings it carries.
#[derive(Default)]
struct ResolvedClaimSet {
    claims: ResolvedSourceClaims,
    warnings: Vec<String>,
}

impl ResolvedClaimSet {
    fn warn(self) -> ResolvedSourceClaims {
        for warning in &self.warnings {
            eprintln!("{warning}");
        }
        self.claims
    }
}

fn resolve_claim_sets(
    graph: &OwnerGraphReport,
    owner_graph_path: &Path,
    modules_root: &Path,
    source_root: Option<&Path>,
    claim_sets: &[SourceClaimSet<'_>],
) -> Result<Vec<Result<ResolvedClaimSet>>> {
    let mut resolved = claim_sets
        .iter()
        .map(|_| Ok(ResolvedClaimSet::default()))
        .collect::<Vec<Result<ResolvedClaimSet>>>();
    let has_anonymous = claim_sets
        .iter()
        .any(|claims| !claims.anonymous_selectors.is_empty());
    if !has_anonymous
        && claim_sets
            .iter()
            .all(|claims| claims.source_matches.is_empty())
    {
        return Ok(resolved);
    }
    js_ast::with_swc_globals(|| {
        let modules = claim_sets
            .iter()
            .map(spec_module)
            .collect::<Result<Vec<_>>>()?;
        let mut anonymous_owner_by_source_ordinal = HashMap::<(String, usize), OwnerId>::new();
        for (idx, node) in graph.nodes.iter().enumerate() {
            if let (true, Some(location)) =
                (node.declared_bindings.is_empty(), &node.source_location)
            {
                anonymous_owner_by_source_ordinal.insert(
                    (location.source_path.clone(), node.statement_ordinal.0),
                    OwnerId(idx),
                );
            }
        }
        let no_sources = if has_anonymous {
            "spec contains anonymous_statements, but owner_graph.json has no source_location \
             data; cannot resolve anonymous selectors"
        } else {
            "spec contains source_matches[] binding claims, but owner_graph.json has no \
             source_location data; cannot resolve source_match selectors"
        };
        with_source_modules(
            graph,
            owner_graph_path,
            modules_root,
            source_root,
            no_sources,
            |parsed_by_source| {
                // Every source is a chunk every module is scoped to.
                let chunks = parsed_by_source
                    .iter()
                    .map(|(source_path, parsed)| {
                        selector_resolve::Chunk::analyze(source_path, &parsed.module)
                    })
                    .collect::<Vec<_>>();
                let resolutions = selector_resolve::resolve(
                    &chunks
                        .iter()
                        .map(|chunk| (chunk, modules.as_slice()))
                        .collect::<Vec<_>>(),
                )?;
                // Per entity, its outcome in every source.
                let mut per_source = BTreeMap::<(usize, EntityIndex), Vec<SourceOutcome>>::new();
                for ((source_path, parsed), resolution) in parsed_by_source.iter().zip(resolutions)
                {
                    for entity in resolution.outcomes {
                        per_source
                            .entry((entity.module, entity.entity))
                            .or_default()
                            .push(SourceOutcome {
                                source_path,
                                module: &parsed.module,
                                outcome: entity.outcome,
                            });
                    }
                }
                // Anonymous statements first, then members, in module order.
                let mut entities = per_source.into_iter().collect::<Vec<_>>();
                entities.sort_by_key(|((module, entity), _)| {
                    (matches!(entity, EntityIndex::Member(_)), *module, *entity)
                });
                for ((module_index, entity), outcomes) in entities {
                    let Ok(set) = &mut resolved[module_index] else {
                        continue;
                    };
                    if let Err(error) = claim_entity(
                        &claim_sets[module_index],
                        &modules[module_index],
                        entity,
                        &outcomes,
                        &anonymous_owner_by_source_ordinal,
                        set,
                    ) {
                        resolved[module_index] = Err(error);
                    }
                }
                Ok(())
            },
        )
    })?;
    Ok(resolved)
}

/// Adds what `entity` claims, given its outcome in every source, to `set`.
fn claim_entity(
    claims: &SourceClaimSet<'_>,
    module: &SpecModule,
    entity: EntityIndex,
    outcomes: &[SourceOutcome<'_>],
    anonymous_owner_by_source_ordinal: &HashMap<(String, usize), OwnerId>,
    set: &mut ResolvedClaimSet,
) -> Result<()> {
    let (place, source) = one_place(claims, module, entity, outcomes)?;
    if source.outcome.severity() != Severity::Ok {
        set.warnings.push(source.outcome.render_line());
    }
    match entity {
        EntityIndex::AnonymousStatement(_) => {
            let ordinal = js_ast::statement_ordinal_for_body_index(&source.module.body, place);
            let owner = anonymous_owner_by_source_ordinal
                .get(&(source.source_path.clone(), ordinal))
                .copied()
                .with_context(|| {
                    format!(
                        "module {} anonymous statement selector matched source {} statement \
                         ordinal {ordinal}, but owner_graph.json has no anonymous owner at that \
                         source position",
                        claims.module_path.display(),
                        source.source_path,
                    )
                })?;
            set.claims.anonymous_owners.insert(owner);
        }
        EntityIndex::Member(_) => {
            let Outcome::Resolved {
                binding: Some(binding),
                ..
            } = &source.outcome.outcome
            else {
                unreachable!("a resolved member names its binding");
            };
            set.claims.bindings.insert(binding.clone());
        }
    }
    Ok(())
}

/// One claim set as the entities it claims.
fn spec_module(claims: &SourceClaimSet<'_>) -> Result<SpecModule> {
    let request_id = claims.module_path.to_string_lossy().to_string();
    let mut members = Vec::new();
    for claim in claims.source_matches {
        for expanded in source_match::source_match_claim_member_selectors(&request_id, claim)? {
            members.push(Member {
                export_name: expanded.export_name,
                selector: MemberSelector::SourceMatch(expanded.parsed_selector),
            });
        }
    }
    let anonymous_statements = claims
        .anonymous_selectors
        .iter()
        .enumerate()
        .map(|(index, selector)| {
            Ok(AnonymousStatement {
                index,
                selector: ParsedSourceMatchSelector::parse(
                    &request_id,
                    "source_match",
                    format!("<source_match needle in {request_id}>"),
                    selector,
                    "source_match",
                )?,
            })
        })
        .collect::<Result<_>>()?;
    Ok(SpecModule {
        path: request_id,
        members,
        anonymous_statements,
    })
}

struct SourceOutcome<'s> {
    source_path: &'s String,
    module: &'s swc_ecma_ast::Module,
    outcome: SelectorOutcome,
}

/// The one body index `entity` resolves to across the chunk sources, and the
/// source it is in: it resolves in exactly one source and matches in no other.
fn one_place<'o, 's>(
    claims: &SourceClaimSet<'_>,
    module: &SpecModule,
    entity: EntityIndex,
    outcomes: &'o [SourceOutcome<'s>],
) -> Result<(usize, &'o SourceOutcome<'s>)> {
    let (what, selector) = match entity {
        EntityIndex::Member(index) => (
            "source_matches[]",
            module.members[index]
                .selector
                .source_match()
                .expect("claim members are source_match members"),
        ),
        EntityIndex::AnonymousStatement(index) => (
            "anonymous statement selector",
            module.anonymous_statements[index].selector.selector(),
        ),
    };
    let module_path = claims.module_path.display();
    let match_source = &selector.match_source;
    let mut places = Vec::<(Candidate, &'o SourceOutcome<'s>)>::new();
    let mut truncated = false;
    for source in outcomes {
        match &source.outcome.outcome {
            Outcome::NoMatch { .. } => {}
            Outcome::Resolved { owner, binding, .. } => {
                places.push((
                    Candidate {
                        owner: *owner,
                        binding: binding.clone(),
                    },
                    source,
                ));
            }
            Outcome::Ambiguous {
                candidates,
                truncated: more,
            } => {
                truncated |= more;
                places.extend(
                    candidates
                        .iter()
                        .map(|candidate| (candidate.clone(), source)),
                );
            }
            _ => bail!(
                "module {module_path} {what} did not resolve:\n{}",
                source.outcome.render_line()
            ),
        }
    }
    let at_least = if truncated { "at least " } else { "" };
    match (places.as_slice(), entity) {
        ([(place, source)], _) if !truncated => Ok((place.owner, source)),
        ([], EntityIndex::Member(_)) => bail!(
            "module {module_path} source_matches[] did not match any top-level declaration in the \
             chunk sources:\n{match_source}"
        ),
        ([], EntityIndex::AnonymousStatement(_)) => bail!(
            "module {module_path} anonymous statement selector did not match any source \
             statement:\n{match_source}"
        ),
        (multiple, EntityIndex::Member(_)) => bail!(
            "module {module_path} source_matches[] is ambiguous — matched {at_least}{} \
             declarations ({}); refine the selector:\n{match_source}",
            multiple.len(),
            multiple
                .iter()
                .filter_map(|(place, _)| place.binding.as_deref())
                .collect::<Vec<_>>()
                .join(", "),
        ),
        (multiple, EntityIndex::AnonymousStatement(_)) => bail!(
            "module {module_path} anonymous statement selector matched {at_least}{} source \
             statements; refine the selector:\n{match_source}",
            multiple.len(),
        ),
    }
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
