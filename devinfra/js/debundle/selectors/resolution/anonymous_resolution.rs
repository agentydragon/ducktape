//! Source-backed selector claim resolution for graph-backed CLI paths.
//!
//! Resolve source-backed and relational module claims against the same chunk
//! sources and selector solver as `debundle run`.

use source_identity::StatementId;
use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::env;
use std::fs;
use std::hash::{DefaultHasher, Hash, Hasher};
use std::path::{Path, PathBuf};

use analysis::{DependencyGraphReport, SourceUnitClass, SourceUnitId, StatementKind};
use anyhow::{Context, Result, bail};
use selector_outcome::{Candidate, Outcome, SelectorOutcome, Severity};
pub use selector_resolve::ModuleNeutralReferenceMeaning;
use selector_resolve::{EntityIndex, SourceMatchReferenceOrigin, SpecModule};
use spec::{AnonymousStatementSelector, MemberSelectorSpec, SourceMatchClaim};
use spec_resolution::{
    anonymous_statement, member_from_source_match, member_from_spec, parse_anonymous_selector,
    resolve_chunks, spec_module as build_spec_module,
};
use swc_common::{EqIgnoreSpan, SyntaxContext};
use swc_ecma_ast::{Ident, ModuleItem, Number, Str};
use swc_ecma_visit::{Visit, VisitWith};

/// One module's claims that need the joint selector solve. Name pins are
/// included as anchors, although their ownership is accounted for statically.
#[derive(Debug, Clone, Copy)]
pub struct SourceClaimSet<'a> {
    pub module_path: &'a Path,
    pub member_selectors: &'a [(String, MemberSelectorSpec)],
    pub source_matches: &'a [SourceMatchClaim],
    pub anonymous_selectors: &'a BTreeSet<AnonymousStatementSelector>,
}

/// What one module's resolved claims contribute to the source unit assignment.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ResolvedSourceClaims {
    /// Chunk-top binding names claimed by source or relational selectors.
    pub bindings: BTreeSet<String>,
    /// The resolved source binding for each non-name-pin member's readable name.
    pub member_bindings: BTreeMap<String, String>,
    /// Resolved free-name references from one source-backed member claim to
    /// another member in the same module. `module_neutral_meaning` is from the
    /// canonical export classifier, not names re-parsed from selector text.
    pub local_member_references: BTreeSet<ResolvedLocalMemberReference>,
    pub anonymous_source_units: BTreeSet<SourceUnitId>,
    /// The source unit selected by each anonymous source selector. The key keeps
    /// authored selector entries addressable when a CLI edit moves claims.
    pub anonymous_selector_source_units: BTreeMap<AnonymousStatementSelector, SourceUnitId>,
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub struct ResolvedLocalMemberReference {
    pub source: ResolvedLocalMemberOrigin,
    pub referenced_by: String,
    pub module_neutral_meaning: ModuleNeutralReferenceMeaning,
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub enum ResolvedLocalMemberOrigin {
    Member(String),
    AnonymousStatement {
        index: usize,
        selector: AnonymousStatementSelector,
    },
}

/// Resolve every module's claims together against each chunk source the
/// dependency graph names. An entity must resolve in exactly one source and match
/// nowhere else; anything else is an error, since the caller (the CLI edit
/// gate) must never silently treat a claimed source unit as residual. As in `run`, a
/// claim unique only because other claims took its alternatives resolves, with
/// a warning on stderr.
pub fn resolve_source_claims(
    graph: &DependencyGraphReport,
    dependency_graph_path: &Path,
    modules_root: &Path,
    source_root: Option<&Path>,
    claim_sets: &[SourceClaimSet<'_>],
) -> Result<Vec<ResolvedSourceClaims>> {
    resolve_claim_sets(
        graph,
        dependency_graph_path,
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
    graph: &DependencyGraphReport,
    dependency_graph_path: &Path,
    modules_root: &Path,
    source_root: Option<&Path>,
    claim_sets: &[SourceClaimSet<'_>],
    index: usize,
) -> Result<ResolvedSourceClaims> {
    resolve_claim_sets(
        graph,
        dependency_graph_path,
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
    graph: &DependencyGraphReport,
    dependency_graph_path: &Path,
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
        && claim_sets.iter().all(|claims| {
            claims.source_matches.is_empty()
                && claims
                    .member_selectors
                    .iter()
                    .all(|(_, selector)| matches!(selector, MemberSelectorSpec::Binding(_)))
        })
    {
        return Ok(resolved);
    }
    js_ast::with_swc_globals(|| {
        let modules = claim_sets
            .iter()
            .map(spec_module)
            .collect::<Result<Vec<_>>>()?;
        let mut anonymous_source_unit_by_source_ordinal =
            HashMap::<(String, usize), SourceUnitId>::new();
        for (idx, node) in graph.nodes.iter().enumerate() {
            if let (SourceUnitClass::AnonymousStatement, Some(location)) =
                (node.class(), &node.source_location)
            {
                anonymous_source_unit_by_source_ordinal.insert(
                    (location.source_path.clone(), node.statement_ordinal.0),
                    SourceUnitId(idx),
                );
            }
        }
        let no_sources = if has_anonymous {
            "spec contains anonymous_statements, but dependency_graph.json has no source_location \
             data; cannot resolve anonymous selectors"
        } else {
            "spec contains source-backed or relational binding claims, but dependency_graph.json has \
             no source_location data; cannot resolve selectors"
        };
        with_source_modules(
            graph,
            dependency_graph_path,
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
                let resolutions = resolve_chunks(
                    &chunks
                        .iter()
                        .map(|chunk| (chunk, modules.as_slice()))
                        .collect::<Vec<_>>(),
                )?;
                // Per entity, its outcome in every source.
                let mut per_source = BTreeMap::<(usize, EntityIndex), Vec<SourceOutcome>>::new();
                for ((source_path, parsed), resolution) in parsed_by_source.iter().zip(resolutions)
                {
                    for reference in resolution.member_references {
                        if reference.source_module != reference.referenced_module {
                            continue;
                        }
                        let Ok(set) = &mut resolved[reference.source_module] else {
                            continue;
                        };
                        let source = match reference.source {
                            SourceMatchReferenceOrigin::Member(member) => {
                                ResolvedLocalMemberOrigin::Member(member)
                            }
                            SourceMatchReferenceOrigin::AnonymousStatement(index) => {
                                let selector = modules[reference.source_module]
                                    .anonymous_statements
                                    .iter()
                                    .find(|statement| statement.index == index)
                                    .with_context(|| {
                                        format!(
                                            "resolved anonymous source_match reference has no \
                                             authored selector index {index} in module {}",
                                            claim_sets[reference.source_module]
                                                .module_path
                                                .display()
                                        )
                                    })?
                                    .selector
                                    .selector()
                                    .clone();
                                ResolvedLocalMemberOrigin::AnonymousStatement { index, selector }
                            }
                        };
                        set.claims
                            .local_member_references
                            .insert(ResolvedLocalMemberReference {
                                source,
                                referenced_by: reference.referenced_member,
                                module_neutral_meaning: reference.module_neutral_meaning,
                            });
                    }
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
                    // Name pins are already accounted for as static claims.
                    // Keep them in the joint solve as relational anchors.
                    if matches!(entity, EntityIndex::Member(index)
                        if index < claim_sets[module_index].member_selectors.len()
                            && matches!(claim_sets[module_index].member_selectors[index].1,
                                MemberSelectorSpec::Binding(_)))
                    {
                        continue;
                    }
                    let Ok(set) = &mut resolved[module_index] else {
                        continue;
                    };
                    if let Err(error) = claim_entity(
                        &claim_sets[module_index],
                        &modules[module_index],
                        entity,
                        &outcomes,
                        &anonymous_source_unit_by_source_ordinal,
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
    anonymous_source_unit_by_source_ordinal: &HashMap<(String, usize), SourceUnitId>,
    set: &mut ResolvedClaimSet,
) -> Result<()> {
    let (place, source) = one_place(claims, module, entity, outcomes)?;
    if source.outcome.severity() != Severity::Ok {
        set.warnings.push(source.outcome.render_line());
    }
    match entity {
        EntityIndex::AnonymousStatement(index) => {
            let ordinal =
                js_ast::source_unit_ordinal_for_statement(&source.module.body, StatementId(place))
                    .0;
            let source_unit = anonymous_source_unit_by_source_ordinal
                .get(&(source.source_path.clone(), ordinal))
                .copied()
                .with_context(|| {
                    format!(
                        "module {} anonymous statement selector matched source {} statement \
                         ordinal {ordinal}, but dependency_graph.json has no anonymous source_unit at that \
                         source position",
                        claims.module_path.display(),
                        source.source_path,
                    )
                })?;
            set.claims.anonymous_source_units.insert(source_unit);
            let selector = module
                .anonymous_statements
                .get(index)
                .context("resolved anonymous selector index is missing")?
                .selector
                .selector()
                .clone();
            set.claims
                .anonymous_selector_source_units
                .insert(selector, source_unit);
        }
        EntityIndex::Member(index) => {
            let Outcome::Resolved {
                binding: Some(binding),
                ..
            } = &source.outcome.outcome
            else {
                unreachable!("a resolved member names its binding");
            };
            set.claims.bindings.insert(binding.clone());
            set.claims
                .member_bindings
                .insert(module.members[index].export_name.clone(), binding.clone());
        }
    }
    Ok(())
}

/// One claim set as the entities it claims.
fn spec_module(claims: &SourceClaimSet<'_>) -> Result<SpecModule> {
    let request_id = claims.module_path.to_string_lossy().to_string();
    let mut members = claims
        .member_selectors
        .iter()
        .map(|(name, selector)| member_from_spec(name.clone(), selector.clone()))
        .collect::<Vec<_>>();
    for claim in claims.source_matches {
        for expanded in source_match::source_match_claim_member_selectors(&request_id, claim)? {
            members.push(member_from_source_match(
                expanded.export_name,
                expanded.parsed_selector,
            ));
        }
    }
    let anonymous_statements = claims
        .anonymous_selectors
        .iter()
        .enumerate()
        .map(|(index, selector)| {
            Ok(anonymous_statement(
                index,
                parse_anonymous_selector(
                    &request_id,
                    format!("<source_match needle in {request_id}>"),
                    selector,
                )?,
            ))
        })
        .collect::<Result<_>>()?;
    Ok(build_spec_module(request_id, members, anonymous_statements))
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
    let (what, match_source) = match entity {
        EntityIndex::Member(index) => {
            let selector = &module.members[index].selector;
            match selector.source_match() {
                Some(selector) => (
                    "source_matches[]".to_string(),
                    selector.match_source.clone(),
                ),
                None => (
                    format!("member selector {:?}", selector.kind()),
                    format!("{selector:?}"),
                ),
            }
        }
        EntityIndex::AnonymousStatement(index) => (
            "anonymous statement selector".to_string(),
            module.anonymous_statements[index]
                .selector
                .selector()
                .match_source
                .clone(),
        ),
    };
    let module_path = claims.module_path.display();
    let mut places = Vec::<(Candidate, &'o SourceOutcome<'s>)>::new();
    let mut truncated = false;
    for source in outcomes {
        match &source.outcome.outcome {
            Outcome::NoMatch { .. } => {}
            Outcome::Resolved {
                body_index,
                binding,
                ..
            } => {
                places.push((
                    Candidate {
                        body_index: *body_index,
                        binding: binding.clone(),
                    },
                    source,
                ));
            }
            Outcome::Ambiguous {
                candidates,
                truncated: more,
                ..
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
        ([(place, source)], _) if !truncated => Ok((place.body_index, source)),
        ([], EntityIndex::Member(_)) => bail!(
            "module {module_path} {what} did not match any top-level declaration in the \
             chunk sources:\n{match_source}"
        ),
        ([], EntityIndex::AnonymousStatement(_)) => bail!(
            "module {module_path} anonymous statement selector did not match any source \
             statement:\n{match_source}"
        ),
        (multiple, EntityIndex::Member(_)) => bail!(
            "module {module_path} {what} is ambiguous — matched {at_least}{} \
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

pub fn addressable_anonymous_statement_source_unit_ids(
    graph: &DependencyGraphReport,
    dependency_graph_path: &Path,
    modules_root: &Path,
    source_root: Option<&Path>,
) -> Result<BTreeSet<String>> {
    js_ast::with_swc_globals(|| {
        addressable_anonymous_statement_source_unit_ids_in_globals(
            graph,
            dependency_graph_path,
            modules_root,
            source_root,
        )
    })
}

fn addressable_anonymous_statement_source_unit_ids_in_globals(
    graph: &DependencyGraphReport,
    dependency_graph_path: &Path,
    modules_root: &Path,
    source_root: Option<&Path>,
) -> Result<BTreeSet<String>> {
    let mut source_unit_by_source_ordinal = HashMap::<(String, usize), &str>::new();
    let mut source_paths = BTreeSet::<String>::new();
    for node in &graph.nodes {
        if node.class() == SourceUnitClass::BindingDeclaration
            || matches!(
                node.statement_kind,
                StatementKind::Import | StatementKind::Export
            )
        {
            continue;
        }
        if let Some(location) = &node.source_location {
            source_paths.insert(location.source_path.clone());
            source_unit_by_source_ordinal.insert(
                (location.source_path.clone(), node.statement_ordinal.0),
                &node.id,
            );
        }
    }

    let mut out = BTreeSet::<String>::new();
    for source_path in &source_paths {
        let parsed = read_and_parse_source(
            source_path,
            source_root,
            dependency_graph_path,
            modules_root,
        )?;
        let unique_body_indices = unique_body_indices(&parsed.module.body);
        for body_idx in unique_body_indices {
            let statement_ordinal = js_ast::source_unit_ordinal_for_statement(
                &parsed.module.body,
                StatementId(body_idx),
            )
            .0;
            if let Some(source_unit_id) =
                source_unit_by_source_ordinal.get(&(source_path.clone(), statement_ordinal))
            {
                out.insert((*source_unit_id).to_string());
            }
        }
    }
    Ok(out)
}

/// Coarse semantic-token fingerprint, not an equality oracle. Deliberately
/// excludes spans, hygiene contexts and literal raw spelling, all ignored by
/// our EqIgnoreSpan comparison. Collisions (including different operators or
/// node shapes with the same tokens) always fall back to exact AST equality.
#[derive(Default)]
struct StatementFingerprint(DefaultHasher);

impl Visit for StatementFingerprint {
    fn visit_ident(&mut self, ident: &Ident) {
        ident.sym.hash(&mut self.0);
    }

    fn visit_str(&mut self, string: &Str) {
        string.value.hash(&mut self.0);
    }

    fn visit_number(&mut self, number: &Number) {
        // Merging signed zeros in the coarse bucket is safe regardless of the
        // AST equality policy: only the exact fallback can declare a duplicate.
        let value = if number.value == 0.0 {
            0.0
        } else {
            number.value
        };
        value.to_bits().hash(&mut self.0);
    }
}

fn unique_body_indices(body: &[ModuleItem]) -> BTreeSet<usize> {
    SyntaxContext::within_ignored_ctxt(|| {
        // Store one representative and uniqueness bit per equality class, not
        // every duplicate. Even a large run of identical statements is linear.
        let mut buckets: HashMap<u64, Vec<(usize, bool)>> = HashMap::new();
        for (index, item) in body.iter().enumerate() {
            let mut fingerprint = StatementFingerprint::default();
            item.visit_with(&mut fingerprint);
            let bucket = buckets.entry(fingerprint.0.finish()).or_default();
            if let Some((_, unique)) = bucket
                .iter_mut()
                .find(|(representative, _)| item.eq_ignore_span(&body[*representative]))
            {
                *unique = false;
            } else {
                bucket.push((index, true));
            }
        }
        buckets
            .into_values()
            .flatten()
            .filter_map(|(index, unique)| unique.then_some(index))
            .collect()
    })
}

/// Parse every distinct `source_location.source_path` in `graph` and hand
/// `body` the parsed modules. `no_sources` is the caller-specific error raised
/// when the graph carries no `source_location` data.
fn with_source_modules<R>(
    graph: &DependencyGraphReport,
    dependency_graph_path: &Path,
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
                read_and_parse_source(
                    source_path,
                    source_root,
                    dependency_graph_path,
                    modules_root,
                )?,
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
    dependency_graph_path: &Path,
    modules_root: &Path,
) -> Result<js_ast::ParsedJsModule> {
    let resolved = resolve_source_file(
        source_path,
        source_root,
        dependency_graph_path,
        modules_root,
    )?;
    let source = fs::read_to_string(&resolved)
        .with_context(|| format!("reading source file {}", resolved.display()))?;
    js_ast::parse_js_module(source_path, &source)
        .with_context(|| format!("parsing source file {}", resolved.display()))
}

/// Locate a graph-referenced source file using the same fallback order for
/// selector claims and source-slice queries.
pub fn resolve_source_file(
    source_path: &str,
    source_root: Option<&Path>,
    dependency_graph_path: &Path,
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
        push_relative_candidate(&mut candidates, dependency_graph_path.parent(), source_path);
        push_relative_candidate(
            &mut candidates,
            dependency_graph_path.parent().and_then(Path::parent),
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

#[cfg(test)]
mod tests {
    use super::*;

    fn check_against_pairwise_oracle(source: &str) -> BTreeSet<usize> {
        js_ast::with_swc_globals(|| {
            let module = js_ast::parse_js_module_ast("test.js", source).unwrap();
            let expected = SyntaxContext::within_ignored_ctxt(|| {
                module
                    .body
                    .iter()
                    .enumerate()
                    .filter_map(|(i, item)| {
                        (module
                            .body
                            .iter()
                            .filter(|other| item.eq_ignore_span(other))
                            .take(2)
                            .count()
                            == 1)
                            .then_some(i)
                    })
                    .collect::<BTreeSet<_>>()
            });
            let actual = unique_body_indices(&module.body);
            assert_eq!(actual, expected);
            actual
        })
    }

    #[test]
    fn token_collisions_and_ignored_literal_spelling_keep_exact_semantics() {
        // +/- deliberately have identical fingerprints, but are distinct ASTs.
        assert_eq!(
            check_against_pairwise_oracle(
                "log(16); log(0x10); log('x'); log(\"x\"); a + b; a - b; f(x => x); f(x => x);"
            ),
            BTreeSet::from([4, 5])
        );
    }

    #[test]
    fn many_unique_and_repeated_statements_preserve_original_indices() {
        let source: String = (0..1024).map(|i| format!("log({i});")).collect();
        let source = format!("{source}{}", "log(0);".repeat(1024));
        assert_eq!(check_against_pairwise_oracle(&source), (1..1024).collect());
    }
}
