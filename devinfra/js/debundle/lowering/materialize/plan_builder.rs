//! `ChunkPlanBuilder` owns the per-chunk mutable state that
//! `materialize_logical_chunk` threads across its eight phases. Each phase is a
//! method on the builder, so the shared lookup (`bindings_catalogue` +
//! `binding_assignment`) lives behind the builder's encapsulation rather than
//! being open-coded per phase.

use super::super::ordinal::body_index_for_statement_ordinal;
use super::*;
use crate::plans::{AnonymousStatementRequest, RelationalSelector};
use analysis::StatementOrdinal;
use anyhow::anyhow;

#[derive(Debug, Clone)]
struct DuplicateClaimSite {
    module_id: String,
    export_name: Option<String>,
    claim_origin: Option<String>,
}

#[derive(Debug, Clone)]
struct DuplicateBindingClaim {
    chunk_id: String,
    binding: String,
    existing: DuplicateClaimSite,
    duplicate: DuplicateClaimSite,
}

#[derive(Debug, Clone)]
struct AnonymousStatementTargetInfo {
    target: SelectorTargetId,
    request_index: usize,
    statement: AnonymousStatementRequest,
}

impl DuplicateBindingClaim {
    fn render(&self) -> String {
        format!(
            "binding {:?} in chunk {:?}: already claimed by {}; duplicate claim by {}",
            self.binding,
            self.chunk_id,
            render_duplicate_claim_site(&self.existing),
            render_duplicate_claim_site(&self.duplicate),
        )
    }
}

// The serialized report shape is the debundler-owned JSON contract shared
// with the `debundle spec validate --keep-going` reader; it lives in the
// `selector_diagnostics` crate so writer and reader cannot drift.
use selector_diagnostics::{
    DuplicateClaimReport, DuplicateClaimSiteReport, SelectorDiagnosticEntry,
    SelectorDiagnosticsReport,
};
use selector_ir::{
    ClaimOutcome, ResolvedClaim, SelectorFact, SelectorFactStore, SelectorTargetId, SolverResult,
};
use selector_ir_lowering::{MemberSelectorLoweringContext, MemberSelectorProgramBuilder};
use selector_ortools_cpsat_backend::OrToolsCpSatBackend;

const SELECTOR_BACKEND_ENV: &str = "DUCKTAPE_DEBUNDLE_SELECTOR_BACKEND";
const ORTOOLS_CPSAT_SOLVER_ENV: &str = "DUCKTAPE_DEBUNDLE_ORTOOLS_CPSAT_SOLVER";

#[derive(Debug, Clone, PartialEq, Eq)]
enum GlobalSelectorBackendChoice {
    Ascent,
    OrToolsCpSat { solver_path: PathBuf },
}

fn global_selector_backend_from_env() -> Result<GlobalSelectorBackendChoice> {
    let backend = std::env::var(SELECTOR_BACKEND_ENV).ok();
    let solver_path = std::env::var(ORTOOLS_CPSAT_SOLVER_ENV).ok();
    parse_global_selector_backend(backend.as_deref(), solver_path.as_deref())
}

fn parse_global_selector_backend(
    backend: Option<&str>,
    solver_path: Option<&str>,
) -> Result<GlobalSelectorBackendChoice> {
    let backend = backend.unwrap_or_default().trim();
    if backend.is_empty() || backend.eq_ignore_ascii_case("ascent") {
        return Ok(GlobalSelectorBackendChoice::Ascent);
    }
    if matches!(
        backend.to_ascii_lowercase().as_str(),
        "ortools-cpsat" | "ortools_cpsat" | "cp-sat" | "cpsat"
    ) {
        let solver_path = solver_path
            .map(str::trim)
            .filter(|path| !path.is_empty())
            .with_context(|| {
                format!("{SELECTOR_BACKEND_ENV}=ortools-cpsat requires {ORTOOLS_CPSAT_SOLVER_ENV}")
            })?;
        return Ok(GlobalSelectorBackendChoice::OrToolsCpSat {
            solver_path: PathBuf::from(solver_path),
        });
    }
    bail!("unknown {SELECTOR_BACKEND_ENV} value {backend:?}; expected `ascent` or `ortools-cpsat`")
}

fn solve_global_selector_program(
    program: &selector_ir::SelectorProgram,
    facts: &SelectorFactStore,
) -> Result<SolverResult> {
    match global_selector_backend_from_env()? {
        GlobalSelectorBackendChoice::Ascent => Ok(selector_ir_solver::solve(program, facts)?),
        GlobalSelectorBackendChoice::OrToolsCpSat { solver_path } => {
            let backend = OrToolsCpSatBackend::new(solver_path);
            selector_backend_solver::solve_with_backend(program, facts, &backend)
                .context("global selector CP-SAT backend failed")
        }
    }
}

impl From<&DuplicateClaimSite> for DuplicateClaimSiteReport {
    fn from(site: &DuplicateClaimSite) -> Self {
        Self {
            module_id: site.module_id.clone(),
            export_name: site.export_name.clone(),
            claim_origin: site.claim_origin.clone(),
        }
    }
}

fn render_duplicate_claim_site(site: &DuplicateClaimSite) -> String {
    let export = site
        .export_name
        .as_deref()
        .map(|name| format!(" as `{name}`"))
        .unwrap_or_default();
    let origin = site
        .claim_origin
        .as_deref()
        .map(|origin| format!(" ({origin})"))
        .unwrap_or_default();
    format!("module {}{export}{origin}", site.module_id)
}

fn render_duplicate_binding_claims(duplicates: &[DuplicateBindingClaim]) -> String {
    let mut duplicates = duplicates.iter().collect::<Vec<_>>();
    duplicates.sort_by(|a, b| {
        (
            a.chunk_id.as_str(),
            a.binding.as_str(),
            a.duplicate.module_id.as_str(),
            a.duplicate.export_name.as_deref().unwrap_or_default(),
        )
            .cmp(&(
                b.chunk_id.as_str(),
                b.binding.as_str(),
                b.duplicate.module_id.as_str(),
                b.duplicate.export_name.as_deref().unwrap_or_default(),
            ))
    });
    let mut report = format!(
        "Duplicate binding claim report: {} duplicate claim(s) found. Each binding may belong to exactly one logical module. Different selector forms (`{{name: foo}}` vs `{{name: foo, kind: class_declaration}}`) that resolve to the same source declaration still count as duplicates. To expose a binding under multiple readable names, list all the renames in one module.",
        duplicates.len()
    );
    for duplicate in &duplicates {
        report.push_str("\n- ");
        report.push_str(&duplicate.render());
    }
    report
}

/// `export_name → minified binding` over the members already resolved by
/// `add_explicit_request` — the anchor-first handle a post-Stage-A selector pass
/// uses to resolve a `@Anchor` reference (`cross_ref`'s anchor, `reads_member`'s
/// `object:`) to a minified binding. The owner graph's `export_name` is not
/// populated at member-resolution time, so the readable→binding map is rebuilt
/// from the resolved members instead (see `materialize::cross_ref`).
///
/// Members still unresolved at this point (the post-Stage-A selectors themselves)
/// have an empty `binding` and are skipped. An export name claimed by two
/// resolved members maps to `None` so it cannot anchor ambiguously — resolution
/// stays categorical, and a missing/ambiguous anchor fails closed at the use site.
fn resolved_anchor_bindings(
    explicit_requests: &[LogicalRequest],
) -> HashMap<String, Option<String>> {
    let mut anchor_binding: HashMap<String, Option<String>> = HashMap::new();
    for request in explicit_requests {
        for member in &request.members {
            if member.resolves_after_stage_a() {
                continue;
            }
            anchor_binding
                .entry(member.export_name.clone())
                .and_modify(|slot| *slot = None)
                .or_insert_with(|| Some(member.binding.clone()));
        }
    }
    anchor_binding
}

fn relational_targets<'a, T: 'a, F>(
    explicit_requests: &'a [LogicalRequest],
    selector: F,
) -> impl Iterator<Item = (usize, &'a LogicalRequest, &'a MemberRequest, &'a T)> + 'a
where
    F: Fn(&'a MemberRequest) -> Option<&'a T> + Copy + 'a,
{
    explicit_requests
        .iter()
        .enumerate()
        .flat_map(move |(index, request)| {
            request.members.iter().filter_map(move |member| {
                selector(member).map(|target| (index, request, member, target))
            })
        })
}

/// Wording for one selector's `@Anchor` resolution diagnostics. The selector kind
/// and the anchor's role (`anchor` / `object` / `class` / `referenced_by`) shape
/// the messages a relational pass emits when its anchor is ambiguous or absent.
struct AnchorLabels {
    /// The `members[].selector.<selector>` key (e.g. `cross_ref`).
    selector: &'static str,
    /// What the anchor identifies, used in the message prefix and the
    /// "The {role} must be the readable `name:`…" missing-anchor sentence.
    role: &'static str,
    /// The thing a non-ambiguous anchor would identify (`binding` / `object` /
    /// `class` / `referencing helper`).
    noun: &'static str,
    /// Closing sentence for the ambiguous case (cross-ref phrases it specially).
    ambiguous_advice: &'static str,
}

/// Resolve a present `@Anchor` name to the anchor's minified binding, fail-closed:
/// an ambiguous (`Some(None)`) or absent (`None`) anchor bails with the
/// selector-appropriate diagnostic. Shared by every relational pass that pins on a
/// `@Anchor` (`cross_ref` / `reads_member` / `passed_to_call` / `makes_decorate_call`
/// / `intrinsic_alias`); `member_of_module` has no anchor.
fn resolve_anchor<'a>(
    anchor_binding: &'a HashMap<String, Option<String>>,
    name: &str,
    request: &LogicalRequest,
    member: &MemberRequest,
    labels: &AnchorLabels,
) -> Result<&'a str> {
    match anchor_binding.get(name) {
        Some(Some(binding)) => Ok(binding.as_str()),
        Some(None) => bail!(
            "logical_module {}: members[].selector.{} {} `@{}` (for member `{}`) is ambiguous — \
             several members resolve to it, so it cannot identify a single {}. {}",
            request.id,
            labels.selector,
            labels.role,
            name,
            member.export_name,
            labels.noun,
            labels.ambiguous_advice,
        ),
        None => bail!(
            "logical_module {}: members[].selector.{} {} `@{}` (for member `{}`) does not name a \
             resolved member in this chunk. The {} must be the readable `name:` of another member \
             whose binding resolves in the same chunk.",
            request.id,
            labels.selector,
            labels.role,
            name,
            member.export_name,
            labels.role,
        ),
    }
}

fn member_selector_spec_for_global_solver(
    member: &MemberRequest,
) -> Option<spec::MemberSelectorSpec> {
    if let Some(selector) = &member.source_match {
        return Some(spec::MemberSelectorSpec::SourceMatch(selector.clone()));
    }

    if let Some(relational) = &member.relational {
        return Some(match relational {
            RelationalSelector::CrossRef(target) => {
                spec::MemberSelectorSpec::CrossRef(target.clone())
            }
            RelationalSelector::ReadsMember(target) => {
                spec::MemberSelectorSpec::ReadsMember(target.clone())
            }
            RelationalSelector::MemberOfModule(target) => {
                spec::MemberSelectorSpec::MemberOfModule(target.clone())
            }
            RelationalSelector::PassedToCall(target) => {
                spec::MemberSelectorSpec::PassedToCall(target.clone())
            }
            RelationalSelector::MakesDecorateCall(target) => {
                spec::MemberSelectorSpec::MakesDecorateCall(target.clone())
            }
            RelationalSelector::IntrinsicAlias(target) => {
                spec::MemberSelectorSpec::IntrinsicAlias(target.clone())
            }
        });
    }

    if member.resolves_after_stage_a() || member.is_import_specifier {
        return None;
    }
    Some(spec::MemberSelectorSpec::Binding(spec::BindingSelector {
        name: member.binding.clone(),
        kind: None,
    }))
}

fn selector_fact_store_for_chunk(
    chunk_id: ChunkId,
    owner_graph: &analysis::OwnerGraph,
    module: &swc_ecma_ast::Module,
    import_sources: &HashMap<String, String>,
) -> Result<SelectorFactStore> {
    let mut store = SelectorFactStore::default();
    let ast_facts = chunk_facts::extract_facts(module).map_err(|unsupported| {
        anyhow!(
            "chunk {:?}: selector AST fact extraction failed at {}; global selector solving needs \
             a complete AST EDB",
            chunk_id,
            unsupported.context,
        )
    })?;
    store.extend_chunk_facts(chunk_id, &ast_facts);
    for node in owner_graph.iter_nodes() {
        store.push(SelectorFact::Owner {
            chunk_id,
            owner: node.id,
            statement_ordinal: node.statement_ordinal,
            statement_kind: node.kind.to_string(),
        });
        for binding in &node.declared {
            store.push(SelectorFact::DeclaredBinding {
                chunk_id,
                owner: node.id,
                binding: binding.0.as_str().to_string(),
                export_name: None,
            });
        }
    }
    for edge in owner_graph.iter_edges() {
        if let Some(binding) = edge.reason.binding() {
            store.push(SelectorFact::OwnerReferencesBinding {
                chunk_id,
                owner: edge.from,
                binding: binding.0.as_str().to_string(),
                edge_kind: edge.reason.kind().to_string(),
            });
        }
    }
    for (ordinal, reads) in chunk_facts::member_reads_by_ordinal(module) {
        for read in reads {
            store.push(SelectorFact::MemberRead {
                chunk_id,
                statement_ordinal: StatementOrdinal(ordinal),
                object: read.object,
                member: read.member,
            });
        }
    }
    for (ordinal, uses) in chunk_facts::module_member_uses_by_ordinal(module, import_sources) {
        for use_site in uses {
            store.push(SelectorFact::ModuleMemberUse {
                chunk_id,
                statement_ordinal: StatementOrdinal(ordinal),
                module: use_site.module,
                member: use_site.member,
            });
        }
    }
    for call in chunk_facts::call_argument_uses(module) {
        store.push(SelectorFact::CallArgumentUse {
            chunk_id,
            argument: call.argument,
            callee_object: call.callee_object,
            callee_member: call.callee_member,
            arg_index: call.arg_index,
        });
    }
    for call in chunk_facts::decorate_call_uses(module) {
        store.push(SelectorFact::DecorateCallUse {
            chunk_id,
            callee: call.callee,
            class_anchor: call.class_anchor,
            member: call.member,
        });
    }
    for alias in chunk_facts::intrinsic_alias_uses(module) {
        store.push(SelectorFact::IntrinsicAliasUse {
            chunk_id,
            binding: alias.binding,
            property: alias.property,
        });
    }
    Ok(store)
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

#[derive(Debug, Clone)]
struct SourceMatchDiagnostic {
    module_id: String,
    module_path: String,
    export_name: String,
    claim_origin: String,
    selector: spec::AnonymousStatementSelector,
    message: String,
    category: String,
    body_indices: Vec<usize>,
    first_mismatch: Option<String>,
}

impl SourceMatchDiagnostic {
    fn new(
        module_id: &str,
        module_path: &str,
        member: &MemberRequest,
        body_indices: Vec<usize>,
        message: String,
    ) -> Self {
        let selector = member
            .source_match
            .clone()
            .expect("source_match diagnostic requires unresolved selector");
        let first_mismatch = first_relevant_error_line(&message);
        Self {
            module_id: module_id.to_string(),
            module_path: module_path.to_string(),
            export_name: member.export_name.clone(),
            claim_origin: member.claim_origin.clone(),
            selector,
            category: classify_source_match_failure(&message).to_string(),
            body_indices,
            first_mismatch,
            message,
        }
    }

    fn render(&self) -> String {
        format!(
            "module {} as `{}` ({}): {}",
            self.module_id, self.export_name, self.claim_origin, self.message
        )
    }
}

fn render_source_match_diagnostics(diagnostics: &[SourceMatchDiagnostic]) -> String {
    let mut diagnostics = diagnostics.iter().collect::<Vec<_>>();
    diagnostics.sort_by(|a, b| {
        (
            a.module_id.as_str(),
            a.export_name.as_str(),
            a.claim_origin.as_str(),
        )
            .cmp(&(
                b.module_id.as_str(),
                b.export_name.as_str(),
                b.claim_origin.as_str(),
            ))
    });
    let mut report = format!(
        "Source-match selector diagnostic report: {} unresolved selector(s) found. \
         Under --keep-going, members with unresolved source_match selectors are skipped from \
         canonical ownership so the rest of the chunk can still be checked.",
        diagnostics.len()
    );
    for diagnostic in &diagnostics {
        report.push_str("\n- ");
        report.push_str(&diagnostic.render());
    }
    report
}

fn native_source_match_no_match_message(
    request: &LogicalRequest,
    member: &MemberRequest,
) -> Option<String> {
    let selector = member.source_match.as_ref()?;
    let target_binding_hint = selector
        .target_binding
        .as_deref()
        .map(|target| format!(" target_binding `{target}`"))
        .unwrap_or_default();
    Some(format!(
        "logical_module {}: members[].selector.source_match for export `{}`{} did not produce a \
         valid global selector assignment for any top-level declaration accepted by the global \
         selector solver. The selector did not match any top-level declaration under the joint \
         constraints; it may have no matching source declaration, or the joint constraints may \
         reject all otherwise matching declarations. Selector:\n{}",
        request.id, member.export_name, target_binding_hint, selector.match_source,
    ))
}

fn native_source_match_ambiguous_message(
    request: &LogicalRequest,
    member: &MemberRequest,
    candidates: &[ResolvedClaim],
) -> Option<String> {
    let selector = member.source_match.as_ref()?;
    let target_binding_hint = selector
        .target_binding
        .as_deref()
        .map(|target| format!(" target_binding `{target}`"))
        .unwrap_or_default();
    Some(format!(
        "logical_module {}: members[].selector.source_match for export `{}`{} is ambiguous in the \
         native selector solver -- matched {} owners at statement ordinals {:?} (bindings: {}). \
         Refine the selector. Source:\n{}",
        request.id,
        member.export_name,
        target_binding_hint,
        candidates.len(),
        candidates
            .iter()
            .map(|candidate| candidate.statement_ordinal.0)
            .collect::<Vec<_>>(),
        candidates
            .iter()
            .filter_map(|candidate| candidate.binding.as_deref())
            .collect::<Vec<_>>()
            .join(", "),
        selector.match_source,
    ))
}

fn first_relevant_error_line(message: &str) -> Option<String> {
    message
        .lines()
        .find(|line| !line.trim().is_empty())
        .map(|line| line.trim().to_string())
}

fn classify_source_match_failure(message: &str) -> &'static str {
    if message.contains(" is ambiguous") {
        "ambiguous_selector"
    } else if message.contains("valid global selector assignment")
        || message.contains("global selector solver")
    {
        "selector_resolution_error"
    } else if message.contains("did not match any") {
        "unresolved_selector"
    } else {
        "selector_resolution_error"
    }
}

fn recommended_source_match_action(category: &str) -> &'static str {
    match category {
        "ambiguous_selector" => {
            "Refine the selector, add target_binding when selecting one binding from a matched declaration, or narrow the matched source context."
        }
        "unresolved_selector" => {
            "Update the selector source to match the current chunk or inspect the logged selector context before applying a mechanical rewrite."
        }
        _ => "Inspect the selector error and update the spec syntax or selector source.",
    }
}

fn source_match_selector_kind(claim_origin: &str) -> &'static str {
    if claim_origin.starts_with("binding_groups[]") {
        "binding_groups.source_match"
    } else {
        "members.source_match"
    }
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
struct SourceMatchGroupCacheKey {
    selector: spec::AnonymousStatementSelector,
    target_bindings: Vec<String>,
}

impl SourceMatchGroupCacheKey {
    fn new(
        selector: spec::AnonymousStatementSelector,
        exports_by_target: &BTreeMap<String, String>,
    ) -> Self {
        let target_bindings = exports_by_target.keys().cloned().collect();
        Self {
            selector,
            target_bindings,
        }
    }
}

#[derive(Debug, Clone)]
struct SourceMatchGroupAssignment {
    selector: spec::AnonymousStatementSelector,
    exports_by_target: BTreeMap<String, String>,
}

fn source_match_group_assignments(
    request: &LogicalRequest,
) -> BTreeMap<usize, SourceMatchGroupAssignment> {
    let mut grouped_by_selector: BTreeMap<spec::AnonymousStatementSelector, Vec<usize>> =
        BTreeMap::new();
    for (idx, member) in request.members.iter().enumerate() {
        let Some(selector) = &member.source_match else {
            continue;
        };
        if selector.target_binding.is_none() {
            continue;
        }
        let mut group_selector = selector.clone();
        group_selector.target_binding = None;
        grouped_by_selector
            .entry(group_selector)
            .or_default()
            .push(idx);
    }

    let mut assignments = BTreeMap::new();
    for (selector, member_indices) in grouped_by_selector {
        if member_indices.len() < 2 {
            continue;
        }
        let mut exports_by_target = BTreeMap::new();
        let mut has_duplicate_target = false;
        for idx in &member_indices {
            let member = &request.members[*idx];
            let target_binding = member
                .source_match
                .as_ref()
                .and_then(|selector| selector.target_binding.clone())
                .expect("grouped selectors always have target_binding");
            if exports_by_target
                .insert(target_binding, member.export_name.clone())
                .is_some()
            {
                has_duplicate_target = true;
            }
        }
        if has_duplicate_target {
            continue;
        }
        let assignment = SourceMatchGroupAssignment {
            selector,
            exports_by_target,
        };
        for idx in member_indices {
            assignments.insert(idx, assignment.clone());
        }
    }
    assignments
}

fn module_path_from_id(module_id: &str) -> Option<String> {
    module_id.split_once("::").map(|(_, path)| path.to_string())
}

fn render_anonymous_statement_diagnostics(diagnostics: &[AnonymousStatementDiagnostic]) -> String {
    let mut diagnostics = diagnostics.iter().collect::<Vec<_>>();
    diagnostics.sort_by(|a, b| a.module_id.cmp(&b.module_id));
    let mut report = format!(
        "Anonymous statement selector diagnostic report: {} unresolved selector(s) found. \
         Under --keep-going, anonymous statements with unresolved selectors are skipped from \
         canonical ownership so the rest of the chunk can still be checked.",
        diagnostics.len()
    );
    for diagnostic in &diagnostics {
        report.push_str("\n- ");
        report.push_str(&diagnostic.render());
    }
    report
}

fn anonymous_statement_no_match_message(
    request: &LogicalRequest,
    statement: &AnonymousStatementRequest,
) -> String {
    format!(
        "logical_module {}: anonymous_statements[].match did not match any top-level statement \
         group in the chunk. Selector:\n{}",
        request.id, statement.selector.match_source,
    )
}

fn anonymous_statement_ambiguous_message(
    request: &LogicalRequest,
    statement: &AnonymousStatementRequest,
    candidate_count: usize,
    body_indices: &[usize],
) -> String {
    format!(
        "logical_module {}: anonymous_statements[].match is ambiguous -- matched {} top-level \
         statement groups at body indices {:?}. Refine the selector. Source:\n{}",
        request.id, candidate_count, body_indices, statement.selector.match_source,
    )
}

/// Output of `ChunkPlanBuilder::finalize`: everything downstream
/// `lower_chunk` + the chunk-report builder need from the plan
/// construction phase.
pub(super) struct ChunkPlan {
    pub(super) module_plans: Vec<ModulePlan>,
    pub(super) binding_assignment: HashMap<Id, usize>,
    pub(super) bindings_catalogue: HashMap<Id, BindingKind>,
    pub(super) anonymous_ordinal_assignment: BTreeMap<usize, usize>,
    pub(super) unmatched_spec_claims: Vec<crate::UnmatchedSpecClaim>,
}

/// Per-explicit-request inputs the builder reads but does not own.
pub(super) struct ExplicitRequestContext<'a> {
    pub(super) declaration_by_name: &'a HashMap<Id, usize>,
    pub(super) chunk_top_level_mark: swc_common::Mark,
    pub(super) target_dir: &'a str,
    pub(super) chunk_id: &'a str,
    pub(super) target_file: &'a str,
    pub(super) runtime_import_facts: &'a RuntimeImportFacts,
}

/// Builds a `ChunkPlan` from spec requests and chunk AST analysis.
///
/// Owns the five mutable maps (`binding_assignment`,
/// `bindings_catalogue`, `anonymous_ordinal_assignment`, `module_plans`,
/// `residual_plan_index`) that the previous shape passed through every
/// helper as `&mut` arguments. All duplicate-claim / cross-claim
/// invariants on the canonical state live behind the builder's
/// methods.
pub(super) struct ChunkPlanBuilder {
    /// Per-binding-`Id` index into `module_plans`. Authoritative
    /// source of "which logical module owns this binding".
    binding_assignment: HashMap<Id, usize>,
    /// Per-source-body-index → `module_plans` index, for anonymous
    /// (non-declared) top-level statements claimed by spec
    /// `anonymous_statements` entries.
    anonymous_ordinal_assignment: BTreeMap<usize, usize>,
    /// The plans being constructed, in append order. Final indices
    /// stable: `binding_assignment` and `anonymous_ordinal_assignment`
    /// hold positional references.
    module_plans: Vec<ModulePlan>,
    /// `BindingKind` view of every claimed binding (Owned vs Imported).
    /// Owned entries duplicate `binding_assignment`'s mapping in a
    /// different shape; Imported entries are exclusive to this map.
    bindings_catalogue: HashMap<Id, BindingKind>,
    /// Index into `module_plans` of the "catchall" plan that
    /// unclaimed bindings sweep into, when one exists. `None` when
    /// the chunk has no residual landing site (default
    /// `InlineInEntry` with no fallback request, or `MiniFactors`).
    residual_plan_index: Option<usize>,
    /// Spec claims that named a binding for which no top-level
    /// declaration exists in this chunk. Materialization keeps
    /// running with the missing claim treated as if absent; the
    /// caller fails the pipeline at the end with the rolled-up list.
    unmatched_spec_claims: Vec<crate::UnmatchedSpecClaim>,
    /// Name-keyed index into `bindings_catalogue` for the
    /// duplicate-claim check inside `add_explicit_request`. The
    /// previous shape — a linear scan of the entire `bindings_catalogue`
    /// HashMap on every member of every request — was the dominant
    /// cost in `build_module_plans` on chunks with thousands of spec
    /// modules (O(N^2) over the growing catalogue). Every catalogue
    /// key is constructed via `top_level_id(name,
    /// chunk_top_level_mark)`, so the `name` alone uniquely
    /// identifies the catalogue entry within a chunk; we mirror
    /// inserts into this index and look up by `&str` to keep
    /// duplicate detection O(1) per member.
    ///
    /// Only consulted during `add_explicit_request`; later phases
    /// (destructure siblings, residual sweep) append without
    /// name-collision checks. The map is dropped by
    /// `drop_explicit_request_scratch`.
    catalogue_index_by_name: HashMap<String, BindingKind>,
    /// Duplicate binding claims found while processing explicit
    /// requests. We keep scanning later requests after recording a
    /// duplicate so one run can report all duplicate claim sites in
    /// this chunk.
    duplicate_binding_claims: Vec<DuplicateBindingClaim>,
    /// Member-form `source_match` selectors that did not resolve.
    /// In keep-going mode, unresolved members are omitted from the
    /// canonical plan so later modules in the chunk can still be
    /// checked for independent selector and duplicate-claim failures.
    source_match_diagnostics: Vec<SourceMatchDiagnostic>,
    /// Anonymous statement selectors that did not resolve. In
    /// keep-going mode, unresolved anonymous statements are omitted
    /// from canonical ownership so later modules in the chunk can
    /// still be checked for independent selector and duplicate-claim
    /// failures.
    anonymous_statement_diagnostics: Vec<AnonymousStatementDiagnostic>,
    /// Opt-in diagnostics mode. When false, duplicate binding claims
    /// keep the historical fail-fast behavior. When true, duplicate
    /// members are skipped from canonical ownership state so later
    /// requests in this chunk can still be checked and reported.
    keep_going: bool,
}

#[allow(dead_code)]
impl ChunkPlanBuilder {
    pub(super) fn new(keep_going: bool) -> Self {
        Self {
            binding_assignment: HashMap::new(),
            anonymous_ordinal_assignment: BTreeMap::new(),
            module_plans: Vec::new(),
            bindings_catalogue: HashMap::new(),
            residual_plan_index: None,
            unmatched_spec_claims: Vec::new(),
            catalogue_index_by_name: HashMap::new(),
            duplicate_binding_claims: Vec::new(),
            source_match_diagnostics: Vec::new(),
            anonymous_statement_diagnostics: Vec::new(),
            keep_going,
        }
    }

    /// Process one explicit (non-residual) logical-module request:
    /// claim each pre-Stage-A named member, and append a `ModulePlan`.
    /// Solver-resolved members and anonymous statement selectors are left
    /// unclaimed until `resolve_and_claim_global_selectors` runs over Stage A
    /// facts. Duplicate-claim detection across all explicit requests is keyed
    /// by binding-name via the builder's `catalogue_index_by_name` scratch
    /// index.
    pub(super) fn add_explicit_request(
        &mut self,
        index: usize,
        request: &mut LogicalRequest,
        ctx: &ExplicitRequestContext<'_>,
        imported_binding_resolver: &mut ArtifactSourceImportResolutionCache<'_>,
        imported_from_by_src: &mut BTreeMap<String, String>,
    ) -> Result<()> {
        reject_duplicate_member_bindings("logical_module", &request.id, &request.members)?;
        let mut bindings = HashMap::<String, String>::new();
        let dest_target_file = target_file_for_request(ctx.target_dir, &request.target_path)?;
        let module_id = ModuleId(LogicalModuleIndex(index));
        let mut duplicate_bindings = BTreeSet::<String>::new();
        for member in &request.members {
            // Deferred selector members resolve in the global selector pass after
            // Stage A has produced the owner graph and selector fact store. Until
            // then their `binding` is empty, so they contribute nothing here.
            if member.resolves_after_stage_a() {
                continue;
            }
            if let Some(existing_kind) = self.catalogue_index_by_name.get(member.binding.as_str()) {
                let existing = match existing_kind {
                    BindingKind::Owned {
                        module: ModuleId(LogicalModuleIndex(owner_index)),
                    } => {
                        let plan = self.module_plans.get(*owner_index);
                        DuplicateClaimSite {
                            module_id: plan
                                .map(|plan| plan.id.clone())
                                .unwrap_or_else(|| format!("<plan#{owner_index}>")),
                            export_name: plan
                                .and_then(|plan| plan.bindings.get(member.binding.as_str()))
                                .cloned(),
                            claim_origin: plan
                                .and_then(|plan| {
                                    plan.binding_claim_origins.get(member.binding.as_str())
                                })
                                .cloned(),
                        }
                    }
                    BindingKind::Imported {
                        re_exporter: ModuleId(LogicalModuleIndex(re_index)),
                        public_name,
                        ..
                    } => {
                        let plan = self.module_plans.get(*re_index);
                        DuplicateClaimSite {
                            module_id: plan
                                .map(|plan| plan.id.clone())
                                .unwrap_or_else(|| format!("<plan#{re_index}>")),
                            export_name: Some(public_name.to_string()),
                            claim_origin: plan
                                .and_then(|plan| {
                                    plan.binding_claim_origins.get(member.binding.as_str())
                                })
                                .cloned(),
                        }
                    }
                };
                let duplicate = DuplicateBindingClaim {
                    chunk_id: ctx.chunk_id.to_string(),
                    binding: member.binding.clone(),
                    existing,
                    duplicate: DuplicateClaimSite {
                        module_id: request.id.clone(),
                        export_name: Some(member.export_name.clone()),
                        claim_origin: Some(member.claim_origin.clone()),
                    },
                };
                self.duplicate_binding_claims.push(duplicate);
                duplicate_bindings.insert(member.binding.clone());
                continue;
            }
            if member.is_import_specifier {
                let (imported_name, imported_from) = resolve_imported_binding(
                    imported_binding_resolver,
                    ctx.runtime_import_facts,
                    ctx.chunk_id,
                    ctx.target_file,
                    &member.binding,
                    imported_from_by_src,
                )?;
                let kind = BindingKind::Imported {
                    imported_name: imported_name.into(),
                    imported_from,
                    re_exporter: module_id,
                    public_name: member.export_name.as_str().into(),
                };
                self.catalogue_index_by_name
                    .insert(member.binding.clone(), kind.clone());
                self.bindings_catalogue.insert(
                    top_level_id(&member.binding, ctx.chunk_top_level_mark),
                    kind,
                );
            } else {
                bindings.insert(member.binding.clone(), member.export_name.clone());
            }
        }
        for (binding, export_name) in &bindings {
            let binding_id = top_level_id(binding, ctx.chunk_top_level_mark);
            if ctx.declaration_by_name.contains_key(&binding_id) {
                self.binding_assignment.insert(binding_id.clone(), index);
                let kind = BindingKind::Owned { module: module_id };
                self.catalogue_index_by_name
                    .insert(binding.clone(), kind.clone());
                self.bindings_catalogue.insert(binding_id, kind);
            } else {
                // The spec claimed a binding name that does not
                // appear as a top-level declaration in this chunk —
                // the previous behavior silently dropped the claim,
                // leaving the destination module short one export
                // and the binding falling into the residual sweep.
                // Record it so the pipeline can fail at the end
                // with the full list across every chunk; meanwhile
                // keep lowering as if the spec had not claimed the
                // name (lower_chunk only touches binding ids it can
                // resolve, so the missing claim is a no-op here).
                self.unmatched_spec_claims.push(crate::UnmatchedSpecClaim {
                    chunk_id: ctx.chunk_id.to_string(),
                    module_path: spec::ModulePath::parse(&request.target_path, "")
                        .expect("request target_path is a canonical module path"),
                    binding_name: binding.clone(),
                    export_name: export_name.clone(),
                });
            }
        }
        let binding_comments: BTreeMap<String, String> = request
            .members
            .iter()
            .filter(|member| !member.resolves_after_stage_a())
            .filter(|member| !duplicate_bindings.contains(member.binding.as_str()))
            .filter_map(|member| {
                member
                    .comment
                    .as_ref()
                    .map(|c| (member.binding.clone(), c.clone()))
            })
            .collect();
        let binding_claim_origins: BTreeMap<String, String> = request
            .members
            .iter()
            .filter(|member| !member.resolves_after_stage_a())
            .filter(|member| !duplicate_bindings.contains(member.binding.as_str()))
            .map(|member| (member.binding.clone(), member.claim_origin.clone()))
            .collect();
        self.module_plans.push(ModulePlan {
            id: request.id.clone(),
            target_file: dest_target_file,
            target_path: request.target_path.clone(),
            explicit: true,
            bindings,
            anonymous_statement_ordinals: Vec::new(),
            anonymous_statement_comments: BTreeMap::new(),
            comment: request.comment.clone(),
            binding_comments,
            binding_claim_origins,
        });
        Ok(())
    }

    fn claim_anonymous_statement(
        &mut self,
        module_index: usize,
        request_id: &str,
        claim: &ResolvedAnonymousStatement,
    ) -> Result<()> {
        if let Some(existing) = self
            .anonymous_ordinal_assignment
            .get(&claim.ordinal)
            .copied()
        {
            let existing_id: String = self
                .module_plans
                .get(existing)
                .map(|plan: &ModulePlan| plan.id.clone())
                .unwrap_or_else(|| format!("<plan#{existing}>"));
            bail!(
                "anonymous_statements[].match in module {} also matches the top-level \
                 statement at body index {} already claimed by module {}; each anonymous \
                 statement may belong to at most one logical module.",
                request_id,
                claim.ordinal,
                existing_id,
            );
        }
        self.anonymous_ordinal_assignment
            .insert(claim.ordinal, module_index);
        let plan = self.module_plans.get_mut(module_index).with_context(|| {
            format!(
                "logical_module {request_id}: anonymous statement resolved before its module plan existed"
            )
        })?;
        plan.anonymous_statement_ordinals.push(claim.ordinal);
        plan.anonymous_statement_ordinals.sort_unstable();
        plan.anonymous_statement_ordinals.dedup();
        if let Some(comment) = &claim.comment {
            plan.anonymous_statement_comments
                .insert(claim.ordinal, comment.clone());
        }
        Ok(())
    }

    fn claim_anonymous_statement_from_solver(
        &mut self,
        module: &swc_ecma_ast::Module,
        module_index: usize,
        request_id: &str,
        statement: &AnonymousStatementRequest,
        claim: &ResolvedClaim,
    ) -> Result<()> {
        let body_index = body_index_for_statement_ordinal(&module.body, claim.statement_ordinal.0)
            .with_context(|| {
                format!(
                    "logical_module {request_id}: global selector solver resolved anonymous \
                     statement to post-split ordinal {} which has no source body item",
                    claim.statement_ordinal.0,
                )
            })?;
        self.claim_anonymous_statement(
            module_index,
            request_id,
            &ResolvedAnonymousStatement {
                ordinal: body_index,
                comment: statement.comment.clone(),
            },
        )
    }

    fn record_anonymous_statement_failure_or_bail(
        &mut self,
        request: &LogicalRequest,
        statement: &AnonymousStatementRequest,
        message: String,
    ) -> Result<()> {
        if self.keep_going {
            self.anonymous_statement_diagnostics
                .push(AnonymousStatementDiagnostic {
                    module_id: request.id.clone(),
                    selector: statement.selector.clone(),
                    message,
                });
            return Ok(());
        }
        bail!("{message}")
    }

    #[allow(clippy::too_many_arguments)]
    pub(super) fn resolve_and_claim_global_selectors(
        &mut self,
        explicit_requests: &[LogicalRequest],
        owner_graph: &analysis::OwnerGraph,
        module: &swc_ecma_ast::Module,
        import_sources: &HashMap<String, String>,
        runtime_import_facts: &RuntimeImportFacts,
        imported_binding_resolver: &mut ArtifactSourceImportResolutionCache<'_>,
        imported_from_by_src: &mut BTreeMap<String, String>,
        chunk_top_level_mark: swc_common::Mark,
        chunk_id: &str,
        target_file: &str,
        chunk_id_interned: ChunkId,
        declaration_by_name: &HashMap<Id, usize>,
    ) -> Result<()> {
        let has_deferred_members = explicit_requests
            .iter()
            .flat_map(|request| &request.members)
            .any(MemberRequest::resolves_after_stage_a);
        let has_anonymous_statements = explicit_requests
            .iter()
            .any(|request| !request.anonymous_statements.is_empty());
        if !has_deferred_members && !has_anonymous_statements {
            return Ok(());
        }

        let mut builder = MemberSelectorProgramBuilder::new(MemberSelectorLoweringContext::new(
            chunk_id_interned,
            chunk_id,
        ));
        let mut deferred_targets = BTreeMap::<SelectorTargetId, (usize, MemberRequest)>::new();
        let mut anonymous_statement_targets = Vec::<AnonymousStatementTargetInfo>::new();
        let mut pending_constraints = Vec::<(String, String, spec::MemberSelectorSpec)>::new();
        let mut pending_source_match_groups = Vec::<(String, SourceMatchGroupAssignment)>::new();
        let mut pending_source_match_group_keys =
            BTreeSet::<(String, SourceMatchGroupCacheKey)>::new();
        if has_deferred_members {
            for (index, request) in explicit_requests.iter().enumerate() {
                let group_assignments = source_match_group_assignments(request);
                for (member_index, member) in request.members.iter().enumerate() {
                    let Some(selector) = member_selector_spec_for_global_solver(member) else {
                        continue;
                    };
                    let target = builder.declare_member_target_in_module(
                        &request.id,
                        &member.export_name,
                        &selector,
                    )?;
                    let group_assignment = group_assignments.get(&member_index).cloned();
                    if let Some(group) = &group_assignment {
                        let key = (
                            request.id.clone(),
                            SourceMatchGroupCacheKey::new(
                                group.selector.clone(),
                                &group.exports_by_target,
                            ),
                        );
                        if pending_source_match_group_keys.insert(key) {
                            pending_source_match_groups.push((request.id.clone(), group.clone()));
                        }
                    } else {
                        pending_constraints.push((
                            request.id.clone(),
                            member.export_name.clone(),
                            selector,
                        ));
                    }
                    if member.resolves_after_stage_a() {
                        deferred_targets.insert(target, (index, member.clone()));
                    }
                }
            }
        }
        for (request_index, request) in explicit_requests.iter().enumerate() {
            for (statement_index, statement) in request.anonymous_statements.iter().enumerate() {
                match builder.declare_native_anonymous_statement_target_in_module(
                    &request.id,
                    statement_index,
                    &statement.selector,
                ) {
                    Ok(target) => anonymous_statement_targets.push(AnonymousStatementTargetInfo {
                        target,
                        request_index,
                        statement: statement.clone(),
                    }),
                    Err(error) => {
                        let message = format!(
                            "logical_module {}: anonymous_statements[].match cannot be lowered \
                             into native selector IR: {error}",
                            request.id,
                        );
                        self.record_anonymous_statement_failure_or_bail(
                            request, statement, message,
                        )?;
                    }
                }
            }
        }
        for (logical_module, group) in pending_source_match_groups {
            if builder.try_lower_native_source_match_group(
                &logical_module,
                &group.selector,
                &group.exports_by_target,
            )? {
                continue;
            }
            return Err(selector_ir_lowering::SelectorIrLoweringError::Unsupported {
                selector_kind: "binding_group.source_match",
                reason: "selector shape is not yet supported by native selector IR",
            }
            .into());
        }
        for (logical_module, export_name, selector) in pending_constraints {
            builder.lower_member_constraints_in_module(&logical_module, &export_name, &selector)?;
        }
        let program = builder.into_program()?;
        if program.targets.is_empty() {
            return Ok(());
        }
        let facts =
            selector_fact_store_for_chunk(chunk_id_interned, owner_graph, module, import_sources)?;
        let result = solve_global_selector_program(&program, &facts)?;

        for (target, (index, member)) in deferred_targets {
            let request = &explicit_requests[index];
            match result.outcome_for(target) {
                Some(ClaimOutcome::Unique { claim }) => {
                    let binding = claim.binding.as_deref().with_context(|| {
                        format!(
                            "logical_module {}: global selector solver resolved member `{}` to \
                             owner {:?} without a single declared binding",
                            request.id, member.export_name, claim.owner,
                        )
                    })?;
                    let native_selects_import = member.source_match.is_some()
                        && solver_claim_is_import_specifier(&facts, claim);
                    if native_selects_import {
                        self.claim_post_stage_a_imported_binding(
                            request,
                            &member,
                            binding,
                            index,
                            chunk_top_level_mark,
                            chunk_id,
                            target_file,
                            runtime_import_facts,
                            imported_binding_resolver,
                            imported_from_by_src,
                        )?;
                        continue;
                    }
                    self.claim_post_stage_a_binding(
                        request,
                        &member,
                        binding,
                        index,
                        chunk_top_level_mark,
                        chunk_id,
                        declaration_by_name,
                    )?;
                }
                Some(ClaimOutcome::NoMatch) => {
                    if let Some(message) = native_source_match_no_match_message(request, &member) {
                        if self.keep_going {
                            self.source_match_diagnostics
                                .push(SourceMatchDiagnostic::new(
                                    &request.id,
                                    &request.target_path,
                                    &member,
                                    Vec::new(),
                                    message,
                                ));
                            continue;
                        }
                        bail!("{message}");
                    }
                    bail!(
                        "logical_module {}: global selector solver found no match for selector \
                         member `{}` ({}): {:?}",
                        request.id,
                        member.export_name,
                        member.claim_origin,
                        member.relational,
                    );
                }
                Some(ClaimOutcome::Ambiguous { candidates }) => {
                    let message =
                        native_source_match_ambiguous_message(request, &member, candidates);
                    if let Some(message) = message {
                        if self.keep_going {
                            let body_indices = candidates
                                .iter()
                                .map(|candidate| candidate.statement_ordinal.0)
                                .collect::<Vec<_>>();
                            self.source_match_diagnostics
                                .push(SourceMatchDiagnostic::new(
                                    &request.id,
                                    &request.target_path,
                                    &member,
                                    body_indices,
                                    message,
                                ));
                            continue;
                        }
                        bail!("{message}");
                    }
                    bail!(
                        "logical_module {}: global selector solver found {} candidates for \
                         selector member `{}` ({}): {:?}",
                        request.id,
                        candidates.len(),
                        member.export_name,
                        member.claim_origin,
                        member.relational,
                    );
                }
                Some(ClaimOutcome::Duplicate {
                    owner,
                    conflicting_targets,
                }) => {
                    bail!(
                        "logical_module {}: global selector solver assigned selector member `{}` \
                         to duplicate owner {:?} shared by targets {:?}",
                        request.id,
                        member.export_name,
                        owner,
                        conflicting_targets,
                    );
                }
                Some(ClaimOutcome::Unsupported { message }) => {
                    bail!(
                        "logical_module {}: global selector solver does not support selector \
                         member `{}` ({}): {}",
                        request.id,
                        member.export_name,
                        member.claim_origin,
                        message,
                    );
                }
                None => {
                    bail!(
                        "logical_module {}: global selector solver returned no outcome for \
                         selector member `{}` ({})",
                        request.id,
                        member.export_name,
                        member.claim_origin,
                    );
                }
            }
        }
        for info in anonymous_statement_targets {
            let request = &explicit_requests[info.request_index];
            match result.outcome_for(info.target) {
                Some(ClaimOutcome::Unique { claim }) => {
                    self.claim_anonymous_statement_from_solver(
                        module,
                        info.request_index,
                        &request.id,
                        &info.statement,
                        claim,
                    )?;
                }
                Some(ClaimOutcome::NoMatch) => {
                    self.record_anonymous_statement_failure_or_bail(
                        request,
                        &info.statement,
                        anonymous_statement_no_match_message(request, &info.statement),
                    )?;
                    continue;
                }
                Some(ClaimOutcome::Ambiguous { candidates }) => {
                    let body_indices = candidates
                        .iter()
                        .filter_map(|candidate| {
                            body_index_for_statement_ordinal(
                                &module.body,
                                candidate.statement_ordinal.0,
                            )
                        })
                        .collect::<Vec<_>>();
                    self.record_anonymous_statement_failure_or_bail(
                        request,
                        &info.statement,
                        anonymous_statement_ambiguous_message(
                            request,
                            &info.statement,
                            candidates.len(),
                            &body_indices,
                        ),
                    )?;
                    continue;
                }
                Some(ClaimOutcome::Duplicate {
                    owner,
                    conflicting_targets,
                }) => {
                    bail!(
                        "logical_module {}: global selector solver assigned anonymous statement \
                         to duplicate owner {:?} shared by targets {:?}",
                        request.id,
                        owner,
                        conflicting_targets,
                    );
                }
                Some(ClaimOutcome::Unsupported { message }) => {
                    bail!(
                        "logical_module {}: global selector solver does not support anonymous \
                         statement selector: {}",
                        request.id,
                        message,
                    );
                }
                None => {
                    bail!(
                        "logical_module {}: global selector solver returned no outcome for \
                         anonymous statement selector",
                        request.id,
                    );
                }
            }
        }
        Ok(())
    }

    /// Resolve and claim every `cross_ref` member across all explicit requests,
    /// using the chunk's owner-graph `Resolution`. Runs after Stage A (the owner
    /// graph carries the reference/alias edges cross-refs ride) but before
    /// `finalize`. Cross-ref members were left unclaimed by `add_explicit_request`
    /// (their target binding isn't known until here).
    ///
    /// **Anchor handle (the settled ordering decision).** A `@Anchor` names another
    /// member by its readable `name:`; the kernel needs the anchor's *minified*
    /// binding to ride the relational edge. The owner graph's `export_name` (the
    /// readable→binding handle `owner_for_export` would use) is **not** populated at
    /// this point — it is filled from the factorization, built later — so the anchor
    /// binding comes from the **already-resolved members**: `export_name → binding`
    /// over every binding/source_match member resolved by `add_explicit_request`.
    /// See `materialize::cross_ref` and the `cross_ref_anchor_ordering` e2e test.
    ///
    /// Resolution is categorical / fail-closed: a cross-ref whose anchor is unknown,
    /// or whose relational edge does not pick out exactly one declaring owner,
    /// errors (or, under keep-going, is recorded as an unresolved-selector
    /// diagnostic and skipped) — it never guesses.
    pub(super) fn resolve_and_claim_cross_refs(
        &mut self,
        explicit_requests: &[LogicalRequest],
        owner_graph: &analysis::OwnerGraph,
        chunk_top_level_mark: swc_common::Mark,
        chunk_id: &str,
        declaration_by_name: &HashMap<Id, usize>,
    ) -> Result<()> {
        // No-op (and no owner-graph solve) for chunks without any cross-ref member.
        if relational_targets(explicit_requests, MemberRequest::cross_ref)
            .next()
            .is_none()
        {
            return Ok(());
        }
        // The owner-graph solve carrying the reference/alias edges a cross-ref rides.
        // Built once per chunk, only when a cross-ref member exists.
        let resolution = cross_ref::build_resolution(owner_graph);

        let anchor_binding = resolved_anchor_bindings(explicit_requests);

        // Resolution itself is fail-closed in both modes: an unresolvable
        // cross-ref (unknown/ambiguous anchor, or a relational edge that doesn't
        // pick out exactly one declaring owner) errors rather than guessing.
        // Duplicate-claim clashes still follow the chunk's keep-going policy
        // inside `resolve_cross_ref_member`.
        for (index, request, member, target) in
            relational_targets(explicit_requests, MemberRequest::cross_ref)
        {
            self.resolve_cross_ref_member(
                request,
                member,
                target,
                &resolution,
                &anchor_binding,
                index,
                chunk_top_level_mark,
                chunk_id,
                declaration_by_name,
            )?;
        }
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    fn resolve_cross_ref_member(
        &mut self,
        request: &LogicalRequest,
        member: &MemberRequest,
        target: &spec::CrossRefTarget,
        resolution: &selector_solve::Resolution,
        anchor_binding: &HashMap<String, Option<String>>,
        index: usize,
        chunk_top_level_mark: swc_common::Mark,
        chunk_id: &str,
        declaration_by_name: &HashMap<Id, usize>,
    ) -> Result<()> {
        let anchor = resolve_anchor(
            anchor_binding,
            &target.anchor,
            request,
            member,
            &AnchorLabels {
                selector: "cross_ref",
                role: "anchor",
                noun: "binding",
                ambiguous_advice: "Anchor a cross-ref on a uniquely-named member.",
            },
        )?;
        let binding =
            cross_ref::resolve_cross_ref(resolution, target, anchor).with_context(|| {
                format!(
                    "logical_module {}: members[].selector.cross_ref for member `{}` did not \
                 resolve to exactly one binding via `{}` of anchor `@{anchor_readable}` \
                 (resolved binding `{anchor}`). The relational edge must pick out exactly one \
                 declaring owner; add or refine `kind:` if several owners stand in the relation.",
                    request.id,
                    member.export_name,
                    match target.relation {
                        spec::CrossRefRelation::References => "references",
                        spec::CrossRefRelation::Aliases => "aliases",
                    },
                    anchor_readable = target.anchor,
                )
            })?;
        // The cross-ref residual-move citation: the binding was parked at the
        // residual plan by the pre-Stage-A sweep, exactly as `apply_rebind_folds`
        // / `synthesize_mini_factors` handle their late claims; the shared tail
        // moves it out.
        self.claim_post_stage_a_binding(
            request,
            member,
            binding,
            index,
            chunk_top_level_mark,
            chunk_id,
            declaration_by_name,
        )
    }

    /// Build a `DuplicateBindingClaim` describing a clash between an
    /// already-claimed `binding` and the cross-ref member now resolving to it.
    /// Shares the existing-site projection with the named-member path.
    fn duplicate_claim_for(
        &self,
        existing_kind: &BindingKind,
        binding: &str,
        chunk_id: &str,
        request: &LogicalRequest,
        member: &MemberRequest,
    ) -> DuplicateBindingClaim {
        let existing = match existing_kind {
            BindingKind::Owned {
                module: ModuleId(LogicalModuleIndex(owner_index)),
            } => {
                let plan = self.module_plans.get(*owner_index);
                DuplicateClaimSite {
                    module_id: plan
                        .map(|plan| plan.id.clone())
                        .unwrap_or_else(|| format!("<plan#{owner_index}>")),
                    export_name: plan.and_then(|plan| plan.bindings.get(binding)).cloned(),
                    claim_origin: plan
                        .and_then(|plan| plan.binding_claim_origins.get(binding))
                        .cloned(),
                }
            }
            BindingKind::Imported {
                re_exporter: ModuleId(LogicalModuleIndex(re_index)),
                public_name,
                ..
            } => {
                let plan = self.module_plans.get(*re_index);
                DuplicateClaimSite {
                    module_id: plan
                        .map(|plan| plan.id.clone())
                        .unwrap_or_else(|| format!("<plan#{re_index}>")),
                    export_name: Some(public_name.to_string()),
                    claim_origin: plan
                        .and_then(|plan| plan.binding_claim_origins.get(binding))
                        .cloned(),
                }
            }
        };
        DuplicateBindingClaim {
            chunk_id: chunk_id.to_string(),
            binding: binding.to_string(),
            existing,
            duplicate: DuplicateClaimSite {
                module_id: request.id.clone(),
                export_name: Some(member.export_name.clone()),
                claim_origin: Some(member.claim_origin.clone()),
            },
        }
    }

    /// Resolve and claim every `reads_member` member across all explicit
    /// requests, using the chunk's owner-graph `Resolution` (with the chunk's AST
    /// member-read facts joined in). Runs after Stage A (the owner graph exists
    /// and the AST is in scope) but before `finalize`. Reads-member members were
    /// left unclaimed by `add_explicit_request` (their target binding isn't known
    /// until here).
    ///
    /// **Object anchor (the settled ordering decision, shared with `cross_ref`).**
    /// A `reads_member` selector may constrain the object the member is read off
    /// (`object: @Anchor`), naming another member by its readable `name:`. The
    /// kernel needs the object's *minified* binding to match the AST member-read.
    /// The owner graph's `export_name` is **not** populated at this point, so the
    /// object binding comes from the **already-resolved members**: `export_name →
    /// binding` over every binding/source_match member resolved by
    /// `add_explicit_request`. See `materialize::reads_member` and `cross_ref`.
    ///
    /// Resolution is categorical / fail-closed: a reads-member whose object anchor
    /// is unknown, or whose member relation does not pick out exactly one
    /// declaring owner, errors (or, under keep-going, is recorded as an
    /// unmatched-claim diagnostic and skipped) — it never guesses.
    pub(super) fn resolve_and_claim_reads_members(
        &mut self,
        explicit_requests: &[LogicalRequest],
        owner_graph: &analysis::OwnerGraph,
        module: &swc_ecma_ast::Module,
        chunk_top_level_mark: swc_common::Mark,
        chunk_id: &str,
        declaration_by_name: &HashMap<Id, usize>,
    ) -> Result<()> {
        // No-op (and no owner-graph solve / AST member-read scan) for chunks
        // without any reads-member member.
        if relational_targets(explicit_requests, MemberRequest::reads_member)
            .next()
            .is_none()
        {
            return Ok(());
        }
        // The owner-graph solve carrying the `reads_member` EDB (member-read facts
        // derived from the chunk AST and joined to owners). Built once per chunk,
        // only when a reads-member member exists.
        let resolution = reads_member::build_resolution(owner_graph, module);

        let anchor_binding = resolved_anchor_bindings(explicit_requests);

        for (index, request, member, target) in
            relational_targets(explicit_requests, MemberRequest::reads_member)
        {
            self.resolve_reads_member_member(
                request,
                member,
                target,
                &resolution,
                &anchor_binding,
                index,
                chunk_top_level_mark,
                chunk_id,
                declaration_by_name,
            )?;
        }
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    fn resolve_reads_member_member(
        &mut self,
        request: &LogicalRequest,
        member: &MemberRequest,
        target: &spec::ReadsMemberTarget,
        resolution: &selector_solve::Resolution,
        anchor_binding: &HashMap<String, Option<String>>,
        index: usize,
        chunk_top_level_mark: swc_common::Mark,
        chunk_id: &str,
        declaration_by_name: &HashMap<Id, usize>,
    ) -> Result<()> {
        // Resolve the optional `object: @Anchor` to the anchor's minified binding.
        let object_binding = match &target.object {
            None => None,
            Some(object) => Some(resolve_anchor(
                anchor_binding,
                object,
                request,
                member,
                &AnchorLabels {
                    selector: "reads_member",
                    role: "object",
                    noun: "object",
                    ambiguous_advice: "Anchor on a uniquely-named member.",
                },
            )?),
        };
        let binding = reads_member::resolve_reads_member(resolution, target, object_binding)
            .with_context(|| {
                let object_clause = match &target.object {
                    Some(object) => format!(" off object `@{object}`"),
                    None => String::new(),
                };
                format!(
                    "logical_module {}: members[].selector.reads_member for member `{}` did not \
                     resolve to exactly one binding via reading member `.{}`{object_clause}. The \
                     relation must pick out exactly one declaring owner; add or refine `object:` \
                     / `kind:` if several owners read the member.",
                    request.id, member.export_name, target.member,
                )
            })?;
        self.claim_post_stage_a_binding(
            request,
            member,
            binding,
            index,
            chunk_top_level_mark,
            chunk_id,
            declaration_by_name,
        )
    }

    /// Resolve and claim every `passed_to_call` member across all explicit
    /// requests, using the chunk's owner-graph `Resolution` (with the chunk's AST
    /// call-argument facts joined in). Runs after Stage A (the owner graph exists
    /// and the AST is in scope) but before `finalize`. Passed-to-call members were
    /// left unclaimed by `add_explicit_request` (their target binding isn't known
    /// until here).
    ///
    /// **Object anchor (the settled ordering decision, shared with `reads_member`).**
    /// A `passed_to_call` selector may constrain the callee's receiver
    /// (`object: @Anchor`, the registry singleton), naming another member by its
    /// readable `name:`. The kernel needs the object's *minified* binding to match
    /// the AST callee. The owner graph's `export_name` is **not** populated at this
    /// point, so the object binding comes from the **already-resolved members**:
    /// `export_name → binding` over every binding/source_match member resolved by
    /// `add_explicit_request`. See `materialize::passed_to_call` and `cross_ref`.
    ///
    /// Resolution is categorical / fail-closed: a passed-to-call whose object anchor
    /// is unknown, or whose call-argument relation does not pick out exactly one
    /// declaring owner, errors (or, under keep-going, is recorded as an
    /// unmatched-claim diagnostic and skipped) — it never guesses.
    pub(super) fn resolve_and_claim_passed_to_calls(
        &mut self,
        explicit_requests: &[LogicalRequest],
        owner_graph: &analysis::OwnerGraph,
        module: &swc_ecma_ast::Module,
        chunk_top_level_mark: swc_common::Mark,
        chunk_id: &str,
        declaration_by_name: &HashMap<Id, usize>,
    ) -> Result<()> {
        // No-op (and no owner-graph solve / AST call-argument scan) for chunks
        // without any passed-to-call member.
        if relational_targets(explicit_requests, MemberRequest::passed_to_call)
            .next()
            .is_none()
        {
            return Ok(());
        }
        // The owner-graph solve carrying the `passed_to_call` EDB (call-argument
        // facts derived from the chunk AST and joined to each argument's declaring
        // owner). Built once per chunk, only when a passed-to-call member exists.
        let resolution = passed_to_call::build_resolution(owner_graph, module);

        let anchor_binding = resolved_anchor_bindings(explicit_requests);

        for (index, request, member, target) in
            relational_targets(explicit_requests, MemberRequest::passed_to_call)
        {
            self.resolve_passed_to_call_member(
                request,
                member,
                target,
                &resolution,
                &anchor_binding,
                index,
                chunk_top_level_mark,
                chunk_id,
                declaration_by_name,
            )?;
        }
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    fn resolve_passed_to_call_member(
        &mut self,
        request: &LogicalRequest,
        member: &MemberRequest,
        target: &spec::PassedToCallTarget,
        resolution: &selector_solve::Resolution,
        anchor_binding: &HashMap<String, Option<String>>,
        index: usize,
        chunk_top_level_mark: swc_common::Mark,
        chunk_id: &str,
        declaration_by_name: &HashMap<Id, usize>,
    ) -> Result<()> {
        // Resolve the optional `object: @Anchor` to the anchor's minified binding.
        let object_binding = match &target.object {
            None => None,
            Some(object) => Some(resolve_anchor(
                anchor_binding,
                object,
                request,
                member,
                &AnchorLabels {
                    selector: "passed_to_call",
                    role: "object",
                    noun: "object",
                    ambiguous_advice: "Anchor on a uniquely-named member.",
                },
            )?),
        };
        let binding = passed_to_call::resolve_passed_to_call(resolution, target, object_binding)
            .with_context(|| {
                let object_clause = match &target.object {
                    Some(object) => format!(" off object `@{object}`"),
                    None => String::new(),
                };
                let index_clause = match target.arg_index {
                    Some(index) => format!(" at argument index {index}"),
                    None => String::new(),
                };
                format!(
                    "logical_module {}: members[].selector.passed_to_call for member `{}` did not \
                     resolve to exactly one binding via being passed to a call of \
                     `.{}`{object_clause}{index_clause}. The relation must pick out exactly one \
                     declaring owner; add or refine `object:` / `arg_index:` / `kind:` if several \
                     owners are passed to the callee.",
                    request.id, member.export_name, target.callee_member,
                )
            })?;
        self.claim_post_stage_a_binding(
            request,
            member,
            binding,
            index,
            chunk_top_level_mark,
            chunk_id,
            declaration_by_name,
        )
    }

    /// Resolve and claim every `makes_decorate_call` member across all explicit
    /// requests, using the chunk's owner-graph `Resolution` (with the chunk's AST
    /// decorator-application facts joined in). The inverse-direction sibling of
    /// `resolve_and_claim_passed_to_calls`, with the same post-Stage-A timing and
    /// no-op-when-absent contract. Makes-decorate-call members were left unclaimed
    /// by `add_explicit_request` (their target binding isn't known until here).
    ///
    /// **Class anchor (the settled ordering decision, shared with `passed_to_call`).**
    /// A `makes_decorate_call` selector pins by `class: @Anchor` (the decorated
    /// class), naming another member by its readable `name:`. The kernel needs the
    /// class's *minified* binding to match the AST decorate-call's class base. The
    /// owner graph's `export_name` is not populated at this point, so the class
    /// binding comes from the already-resolved members (`export_name → binding` over
    /// every binding/source_match member resolved by `add_explicit_request`).
    ///
    /// Resolution is categorical / fail-closed: a makes-decorate-call whose class
    /// anchor is unknown, or whose decorate-call relation does not pick out exactly
    /// one declaring owner, errors (or, under keep-going, is recorded as an
    /// unmatched-claim diagnostic and skipped) — it never guesses.
    pub(super) fn resolve_and_claim_makes_decorate_calls(
        &mut self,
        explicit_requests: &[LogicalRequest],
        owner_graph: &analysis::OwnerGraph,
        module: &swc_ecma_ast::Module,
        chunk_top_level_mark: swc_common::Mark,
        chunk_id: &str,
        declaration_by_name: &HashMap<Id, usize>,
    ) -> Result<()> {
        // No-op (and no owner-graph solve / AST decorate-call scan) for chunks
        // without any makes-decorate-call member.
        if relational_targets(explicit_requests, MemberRequest::makes_decorate_call)
            .next()
            .is_none()
        {
            return Ok(());
        }
        // The owner-graph solve carrying the `makes_decorate_call` EDB (decorator
        // applications derived from the chunk AST and joined to each helper callee's
        // declaring owner). Built once per chunk, only when such a member exists.
        let resolution = makes_decorate_call::build_resolution(owner_graph, module);

        let anchor_binding = resolved_anchor_bindings(explicit_requests);

        for (index, request, member, target) in
            relational_targets(explicit_requests, MemberRequest::makes_decorate_call)
        {
            self.resolve_makes_decorate_call_member(
                request,
                member,
                target,
                &resolution,
                &anchor_binding,
                index,
                chunk_top_level_mark,
                chunk_id,
                declaration_by_name,
            )?;
        }
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    fn resolve_makes_decorate_call_member(
        &mut self,
        request: &LogicalRequest,
        member: &MemberRequest,
        target: &spec::MakesDecorateCallTarget,
        resolution: &selector_solve::Resolution,
        anchor_binding: &HashMap<String, Option<String>>,
        index: usize,
        chunk_top_level_mark: swc_common::Mark,
        chunk_id: &str,
        declaration_by_name: &HashMap<Id, usize>,
    ) -> Result<()> {
        // Resolve the required `class: @Anchor` to the decorated class's minified
        // binding (the anchor-first handle the kernel matches against the AST).
        let class_binding = resolve_anchor(
            anchor_binding,
            &target.class,
            request,
            member,
            &AnchorLabels {
                selector: "makes_decorate_call",
                role: "class",
                noun: "class",
                ambiguous_advice: "Anchor on a uniquely-named member.",
            },
        )?;
        let binding =
            makes_decorate_call::resolve_makes_decorate_call(resolution, target, class_binding)
                .with_context(|| {
                    let member_clause = match &target.member {
                        Some(member) => format!(" decorating `{member}`"),
                        None => String::new(),
                    };
                    format!(
                        "logical_module {}: members[].selector.makes_decorate_call for member `{}` \
                         did not resolve to exactly one binding via making a decorator application \
                         on `@{}`{member_clause}. The relation must pick out exactly one declaring \
                         owner; add or refine `member:` / `kind:` if several helpers decorate the \
                         class.",
                        request.id, member.export_name, target.class,
                    )
                })?;
        self.claim_post_stage_a_binding(
            request,
            member,
            binding,
            index,
            chunk_top_level_mark,
            chunk_id,
            declaration_by_name,
        )
    }

    /// Resolve and claim every `intrinsic_alias` member across all explicit
    /// requests, using the chunk's owner-graph `Resolution` (with the chunk's AST
    /// intrinsic-alias facts joined in). The follow-on companion of
    /// `resolve_and_claim_makes_decorate_calls`, with the same post-Stage-A timing
    /// and no-op-when-absent contract. Intrinsic-alias members were left unclaimed by
    /// `add_explicit_request` (their target binding isn't known until here).
    ///
    /// **Helper anchor (the settled ordering decision, shared with
    /// `makes_decorate_call`).** An `intrinsic_alias` selector pins by
    /// `referenced_by: @Helper` (the trio's `__decorate` helper), naming another
    /// member by its readable `name:`. The kernel needs the helper's *owner* to match
    /// the AST `references` edge's source. The owner graph's `export_name` is not
    /// populated at this point, so the helper binding comes from the already-resolved
    /// members (`export_name → binding`) and is resolved to its owner inside the
    /// bridge. The anchor map is built **per companion module**
    /// (`claimed_member_bindings_in_module`): esbuild co-locates each `__decorate`
    /// helper with its companions, so the helper is unambiguous within that module
    /// even when its readable `name:` (e.g. a generic `applyDecorators`) repeats
    /// across modules.
    ///
    /// Resolution is categorical / fail-closed: an intrinsic-alias whose helper
    /// anchor is unknown, or whose `Object.<property>` relation does not pick out
    /// exactly one declaring owner (including the shadowed-`Object` case, which yields
    /// no EDB rows), errors (or, under keep-going, is recorded as an unmatched-claim
    /// diagnostic and skipped) — it never guesses.
    pub(super) fn resolve_and_claim_intrinsic_aliases(
        &mut self,
        explicit_requests: &[LogicalRequest],
        owner_graph: &analysis::OwnerGraph,
        module: &swc_ecma_ast::Module,
        chunk_top_level_mark: swc_common::Mark,
        chunk_id: &str,
        declaration_by_name: &HashMap<Id, usize>,
    ) -> Result<()> {
        // No-op (and no owner-graph solve / AST intrinsic-alias scan) for chunks
        // without any intrinsic-alias member.
        if relational_targets(explicit_requests, MemberRequest::intrinsic_alias)
            .next()
            .is_none()
        {
            return Ok(());
        }
        // The owner-graph solve carrying the `intrinsic_alias` EDB (intrinsic-alias
        // declarations derived from the chunk AST and joined to each alias's declaring
        // owner). Built once per chunk, only when such a member exists.
        let resolution = intrinsic_alias::build_resolution(owner_graph, module);

        for (index, request, member, target) in
            relational_targets(explicit_requests, MemberRequest::intrinsic_alias)
        {
            // The `referenced_by` helper is pinned by `makes_decorate_call` (run
            // earlier), which co-locates it in the companion's own module, so its
            // binding lives in `module_plans[index]` rather than in
            // `resolved_anchor_bindings` (which sees only
            // `add_explicit_request`-resolved members). Resolve the anchor within
            // that module so a generic helper `name:` shared across modules stays
            // unambiguous per module.
            let anchor_binding = self.claimed_member_bindings_in_module(index);
            self.resolve_intrinsic_alias_member(
                request,
                member,
                target,
                &resolution,
                &anchor_binding,
                index,
                chunk_top_level_mark,
                chunk_id,
                declaration_by_name,
            )?;
        }
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    fn resolve_intrinsic_alias_member(
        &mut self,
        request: &LogicalRequest,
        member: &MemberRequest,
        target: &spec::IntrinsicAliasTarget,
        resolution: &selector_solve::Resolution,
        anchor_binding: &HashMap<String, Option<String>>,
        index: usize,
        chunk_top_level_mark: swc_common::Mark,
        chunk_id: &str,
        declaration_by_name: &HashMap<Id, usize>,
    ) -> Result<()> {
        // Resolve the required `referenced_by: @Helper` to the helper's minified
        // binding (the anchor-first handle the bridge resolves to the referencer
        // owner).
        let referenced_by_binding = resolve_anchor(
            anchor_binding,
            &target.referenced_by,
            request,
            member,
            &AnchorLabels {
                selector: "intrinsic_alias",
                role: "referenced_by",
                noun: "referencing helper",
                ambiguous_advice: "Anchor on a uniquely-named member.",
            },
        )?;
        let binding =
            intrinsic_alias::resolve_intrinsic_alias(resolution, target, referenced_by_binding)
                .with_context(|| {
                    format!(
                        "logical_module {}: members[].selector.intrinsic_alias for member `{}` did \
                         not resolve to exactly one binding via being an `Object.{}` alias \
                         referenced by `@{}`. The relation must pick out exactly one declaring \
                         owner; confirm the property name and that the global `Object` is not \
                         shadowed (a shadowed/reassigned/imported `Object` fails closed).",
                        request.id, member.export_name, target.property, target.referenced_by,
                    )
                })?;
        self.claim_post_stage_a_binding(
            request,
            member,
            binding,
            index,
            chunk_top_level_mark,
            chunk_id,
            declaration_by_name,
        )
    }

    /// Resolve and claim every `member_of_module` member across all explicit
    /// requests, using the chunk's owner-graph `Resolution` with the
    /// `member_of_module` use-site EDB (member accesses joined to the chunk's
    /// import table). Runs after Stage A, mirroring
    /// [`Self::resolve_and_claim_reads_members`]; `member_of_module` members were
    /// left unclaimed by `add_explicit_request` (their target binding isn't known
    /// until here).
    ///
    /// Unlike `reads_member`, the selector carries **no object anchor** — both its
    /// labels (`module`, `member`) are re-minify-invariant and resolved directly
    /// against the use-site EDB — so there is no anchor-first map to build.
    /// Resolution is categorical / fail-closed: a use-site relation that does not
    /// pick out exactly one declaring owner errors (or, under keep-going, is
    /// recorded as an unmatched-claim diagnostic) — it never guesses.
    #[allow(clippy::too_many_arguments)]
    pub(super) fn resolve_and_claim_member_of_modules(
        &mut self,
        explicit_requests: &[LogicalRequest],
        owner_graph: &analysis::OwnerGraph,
        module: &swc_ecma_ast::Module,
        import_sources: &HashMap<String, String>,
        chunk_top_level_mark: swc_common::Mark,
        chunk_id: &str,
        declaration_by_name: &HashMap<Id, usize>,
    ) -> Result<()> {
        // No-op (and no owner-graph solve / AST scan) for chunks without any
        // member-of-module member.
        if relational_targets(explicit_requests, MemberRequest::member_of_module)
            .next()
            .is_none()
        {
            return Ok(());
        }
        // The owner-graph solve carrying the `member_of_module` use-site EDB
        // (member accesses joined to the import table). Built once per chunk, only
        // when a member-of-module member exists.
        let resolution = member_of_module::build_resolution(owner_graph, module, import_sources);

        for (index, request, member, target) in
            relational_targets(explicit_requests, MemberRequest::member_of_module)
        {
            let binding = member_of_module::resolve_member_of_module(&resolution, target)
                .with_context(|| {
                    format!(
                        "logical_module {}: members[].selector.member_of_module for member \
                             `{}` did not resolve to exactly one binding via consuming \
                             `{}.{}`. The use-site relation must pick out exactly one declaring \
                             owner; add or refine `kind:` if several owners consume the module \
                             member.",
                        request.id, member.export_name, target.module, target.member,
                    )
                })?;
            self.claim_post_stage_a_binding(
                request,
                member,
                binding,
                index,
                chunk_top_level_mark,
                chunk_id,
                declaration_by_name,
            )?;
        }
        Ok(())
    }

    /// `export_name → resolved minified binding` over the already-claimed members
    /// of **one logical module** (`module_plans[index]`), including those resolved
    /// by earlier *post-Stage-A* passes (`reads_member` / `member_of_module` /
    /// `passed_to_call` / `makes_decorate_call`). Unlike [`resolved_anchor_bindings`]
    /// — which sees only `add_explicit_request`-resolved `binding`/`source_match`
    /// members — this reads the claimed bindings back out of the module plan, so an
    /// anchor pinned by a prior post-Stage-A pass is resolvable. An export name
    /// claimed by two distinct bindings *within this module* maps to `None`
    /// (ambiguous → fail-closed), matching `resolved_anchor_bindings`' semantics.
    ///
    /// Module-scoped because `intrinsic_alias`'s `referenced_by` rides the trio's
    /// `__decorate` helper, which esbuild co-locates in the companion's own module:
    /// `makes_decorate_call` (run earlier) claims the helper into `module_plans[index]`,
    /// the same module the companion resolves into. Scoping to that module is the
    /// correct resolution — a generic helper `name:` repeated across modules (e.g.
    /// `applyDecorators`) would collapse to ambiguous under a chunk-global view, even
    /// though each companion's helper is unambiguous in its own module.
    fn claimed_member_bindings_in_module(&self, index: usize) -> HashMap<String, Option<String>> {
        let mut by_export: HashMap<String, Option<String>> = HashMap::new();
        for (binding, export_name) in &self.module_plans[index].bindings {
            by_export
                .entry(export_name.clone())
                .and_modify(|slot| {
                    // A second distinct binding under one export name is
                    // ambiguous; identical re-claims keep the single binding.
                    if slot.as_deref() != Some(binding.as_str()) {
                        *slot = None;
                    }
                })
                .or_insert_with(|| Some(binding.clone()));
        }
        by_export
    }

    /// Claim a binding a post-Stage-A selector pass resolved to (the shared tail
    /// of `reads_member` and `member_of_module`): record an unmatched-claim if the
    /// binding has no top-level declaration; error / record a duplicate if already
    /// claimed; otherwise move it out of the residual sweep and into module
    /// `index`, registering its export name, claim origin, and comment. The
    /// residual move mirrors the cross-ref pass — the binding was parked at the
    /// residual plan when the pre-Stage-A sweep ran, before this pass knew its
    /// identity.
    #[allow(clippy::too_many_arguments)]
    fn claim_post_stage_a_imported_binding(
        &mut self,
        request: &LogicalRequest,
        member: &MemberRequest,
        binding: &str,
        index: usize,
        chunk_top_level_mark: swc_common::Mark,
        chunk_id: &str,
        target_file: &str,
        runtime_import_facts: &RuntimeImportFacts,
        imported_binding_resolver: &mut ArtifactSourceImportResolutionCache<'_>,
        imported_from_by_src: &mut BTreeMap<String, String>,
    ) -> Result<()> {
        if let Some(existing_kind) = self.catalogue_index_by_name.get(binding) {
            let duplicate =
                self.duplicate_claim_for(existing_kind, binding, chunk_id, request, member);
            if !self.keep_going {
                bail!("{}", render_duplicate_binding_claims(&[duplicate]));
            }
            self.duplicate_binding_claims.push(duplicate);
            return Ok(());
        }
        let (imported_name, imported_from) = resolve_imported_binding(
            imported_binding_resolver,
            runtime_import_facts,
            chunk_id,
            target_file,
            binding,
            imported_from_by_src,
        )?;
        let kind = BindingKind::Imported {
            imported_name: imported_name.into(),
            imported_from,
            re_exporter: ModuleId(LogicalModuleIndex(index)),
            public_name: member.export_name.as_str().into(),
        };
        self.catalogue_index_by_name
            .insert(binding.to_string(), kind.clone());
        self.bindings_catalogue
            .insert(top_level_id(binding, chunk_top_level_mark), kind);
        Ok(())
    }

    fn same_module_duplicate_source_binding_report(
        &self,
        existing_kind: &BindingKind,
        binding: &str,
        index: usize,
        request: &LogicalRequest,
        member: &MemberRequest,
    ) -> Option<String> {
        member.source_match.as_ref()?;
        let BindingKind::Owned {
            module: ModuleId(LogicalModuleIndex(owner_index)),
        } = existing_kind
        else {
            return None;
        };
        if *owner_index != index {
            return None;
        }
        let plan = self.module_plans.get(index)?;
        let existing_export = plan.bindings.get(binding)?;
        let existing_origin = plan
            .binding_claim_origins
            .get(binding)
            .map(String::as_str)
            .unwrap_or("<unknown origin>");
        Some(format!(
            "logical_module {} has duplicate source binding claims:\n- source binding `{}` \
             claimed 2 times:\n  - export `{}` ({})\n  - export `{}` ({})",
            request.id,
            binding,
            existing_export,
            existing_origin,
            member.export_name,
            member.claim_origin,
        ))
    }

    #[allow(clippy::too_many_arguments)]
    fn claim_post_stage_a_binding(
        &mut self,
        request: &LogicalRequest,
        member: &MemberRequest,
        binding: &str,
        index: usize,
        chunk_top_level_mark: swc_common::Mark,
        chunk_id: &str,
        declaration_by_name: &HashMap<Id, usize>,
    ) -> Result<()> {
        let binding_id = top_level_id(binding, chunk_top_level_mark);
        if !declaration_by_name.contains_key(&binding_id) {
            // Mirror the named-member path: a resolved binding with no top-level
            // declaration in this chunk is recorded and the pipeline fails at the
            // end with the full list, rather than half-claiming it here.
            self.unmatched_spec_claims.push(crate::UnmatchedSpecClaim {
                chunk_id: chunk_id.to_string(),
                module_path: spec::ModulePath::parse(&request.target_path, "")
                    .expect("request target_path is a canonical module path"),
                binding_name: binding.to_string(),
                export_name: member.export_name.clone(),
            });
            return Ok(());
        }
        let module_id = ModuleId(LogicalModuleIndex(index));
        if let Some(existing_kind) = self.catalogue_index_by_name.get(binding) {
            if let Some(report) = self.same_module_duplicate_source_binding_report(
                existing_kind,
                binding,
                index,
                request,
                member,
            ) {
                bail!("{report}");
            }
            let duplicate =
                self.duplicate_claim_for(existing_kind, binding, chunk_id, request, member);
            if !self.keep_going {
                bail!("{}", render_duplicate_binding_claims(&[duplicate]));
            }
            self.duplicate_binding_claims.push(duplicate);
            return Ok(());
        }
        // The residual sweep ran before Stage A (before this pass), so the target
        // binding — unclaimed at sweep time — was parked at the residual plan. Move
        // it out: overwrite its assignment and prune the residual plan's export
        // list, so the residual module doesn't keep `export { <binding> }` for a
        // declaration that now lives in the explicit module.
        if let Some(prev) = self.binding_assignment.get(&binding_id).copied()
            && Some(prev) == self.residual_plan_index
            && let Some(residual_idx) = self.residual_plan_index
        {
            self.module_plans[residual_idx].bindings.remove(binding);
        }
        self.binding_assignment.insert(binding_id.clone(), index);
        let kind = BindingKind::Owned { module: module_id };
        self.catalogue_index_by_name
            .insert(binding.to_string(), kind.clone());
        self.bindings_catalogue.insert(binding_id, kind);
        let plan = &mut self.module_plans[index];
        plan.bindings
            .insert(binding.to_string(), member.export_name.clone());
        plan.binding_claim_origins
            .insert(binding.to_string(), member.claim_origin.clone());
        if let Some(comment) = &member.comment {
            plan.binding_comments
                .insert(binding.to_string(), comment.clone());
        }
        Ok(())
    }

    /// Drop the name-keyed catalogue scratch index now that the
    /// explicit-requests loop is finished. Destructure siblings and
    /// the residual sweep don't consult this index.
    pub(super) fn drop_explicit_request_scratch(&mut self) {
        self.catalogue_index_by_name = HashMap::new();
    }

    /// Destructure-atomicity: a destructuring declarator like
    /// `const { x, y } = obj` binds multiple names from a single
    /// pattern that the lowerer's `split_var_decl` moves as one
    /// unit. If the spec claims any one binding from such a pattern,
    /// every sibling binding must travel to the same module —
    /// otherwise the residual's export list would list a name whose
    /// declarator has already moved away, and `node` would reject the
    /// resulting module with `SyntaxError: Export 'y' is not defined
    /// in module`.
    ///
    /// Implicitly-pulled siblings join the claimed module with their
    /// own binding name as the export name. They aren't separately
    /// spec'd, but the destructure pattern must keep its full name
    /// set together regardless. Conflicting claims (two siblings
    /// claimed by different modules) are rejected.
    pub(super) fn pull_destructure_siblings(
        &mut self,
        destructure_siblings: &BTreeMap<String, BTreeSet<String>>,
        chunk_top_level_mark: swc_common::Mark,
    ) -> Result<()> {
        for (claimed_name, sibling_set) in destructure_siblings {
            let claimed_id = top_level_id(claimed_name, chunk_top_level_mark);
            let Some(&owner_index) = self.binding_assignment.get(&claimed_id) else {
                continue;
            };
            let owner_id = ModuleId(LogicalModuleIndex(owner_index));
            for sibling in sibling_set {
                if sibling == claimed_name {
                    continue;
                }
                let sibling_id = top_level_id(sibling, chunk_top_level_mark);
                match self.binding_assignment.get(&sibling_id).copied() {
                    None => {
                        self.binding_assignment
                            .insert(sibling_id.clone(), owner_index);
                        self.bindings_catalogue
                            .insert(sibling_id, BindingKind::Owned { module: owner_id });
                        let plan = &mut self.module_plans[owner_index];
                        plan.bindings.insert(sibling.clone(), sibling.clone());
                    }
                    Some(other_index) if other_index != owner_index => {
                        let owner_plan_id = self.module_plans[owner_index].id.clone();
                        let other_plan_id = self.module_plans[other_index].id.clone();
                        bail!(
                            "destructure declarator binds {claimed_name} (claimed by module \
                             {owner_plan_id}) and {sibling} (claimed by module {other_plan_id}); \
                             destructuring declarators must move atomically — claim both \
                             bindings from the same module or claim neither.",
                        );
                    }
                    Some(_) => {}
                }
            }
        }
        Ok(())
    }

    /// Bindings declared by an anonymously-claimed statement (e.g. a
    /// block-hoisted `var` inside a claimed `try` statement) belong
    /// to the module that claims the statement: the declaration is
    /// emitted there, so binding ownership, exports, and
    /// cross-module import wiring must follow it. Runs after global
    /// selector resolution; bindings already swept into residual are
    /// moved out so the residual file does not export declarations
    /// emitted in another module.
    pub(super) fn adopt_bindings_of_claimed_anonymous_statements(
        &mut self,
        declarations: &[TopLevelDecl],
    ) {
        for decl in declarations {
            let Some(&plan_index) = self.anonymous_ordinal_assignment.get(&decl.ordinal) else {
                continue;
            };
            let module = ModuleId(LogicalModuleIndex(plan_index));
            for (name, id) in &decl.bindings {
                if let Some(existing) = self.binding_assignment.get(id).copied() {
                    if Some(existing) != self.residual_plan_index {
                        continue;
                    }
                    if let Some(residual_index) = self.residual_plan_index {
                        self.module_plans[residual_index].bindings.remove(name);
                    }
                }
                self.binding_assignment.insert(id.clone(), plan_index);
                self.module_plans[plan_index]
                    .bindings
                    .entry(name.clone())
                    .or_insert_with(|| name.clone());
                self.bindings_catalogue
                    .insert(id.clone(), BindingKind::Owned { module });
            }
        }
    }

    /// Residual sweep: route every chunk top-level binding the spec
    /// did not claim to the chunk's catchall destination.
    ///
    /// Two shapes:
    ///
    /// 1. A memberless residual request was synthesized (or supplied
    ///    by the spec) — build a new residual plan, append it, and
    ///    point `residual_plan_index` at it.
    /// 2. An explicit `logical_modules` entry already pins itself at
    ///    the catchall target — repurpose that plan: flip its
    ///    `explicit` flag and append unclaimed bindings to its
    ///    members.
    ///
    /// `None` for both `residual_request` and `catchall_target`
    /// leaves `residual_plan_index` unset, which is the
    /// `InlineInEntry` / `MiniFactors` shape.
    pub(super) fn add_residual_sweep(
        &mut self,
        residual_request: Option<&LogicalRequest>,
        catchall_target_for_overflow: Option<&str>,
        declarations: &[TopLevelDecl],
        target_dir: &str,
    ) -> Result<()> {
        if let Some(residual) = residual_request {
            let residual_index = self.module_plans.len();
            let residual_module_id = ModuleId(LogicalModuleIndex(residual_index));
            let mut residual_bindings = HashMap::<String, String>::new();
            for decl in declarations {
                for (name, id) in &decl.bindings {
                    if !self.binding_assignment.contains_key(id) {
                        self.binding_assignment.insert(id.clone(), residual_index);
                        residual_bindings.insert(name.clone(), name.clone());
                        self.bindings_catalogue.insert(
                            id.clone(),
                            BindingKind::Owned {
                                module: residual_module_id,
                            },
                        );
                    }
                }
            }
            if !residual_bindings.is_empty() {
                self.module_plans.push(ModulePlan {
                    id: residual.id.clone(),
                    target_file: target_file_for_request(target_dir, &residual.target_path)?,
                    target_path: residual.target_path.clone(),
                    explicit: false,
                    bindings: residual_bindings,
                    anonymous_statement_ordinals: Vec::new(),
                    anonymous_statement_comments: BTreeMap::new(),
                    comment: None,
                    binding_comments: BTreeMap::new(),
                    binding_claim_origins: BTreeMap::new(),
                });
                self.residual_plan_index = Some(residual_index);
            }
        } else if let Some(catchall_target) = catchall_target_for_overflow {
            // No memberless residual request was synthesized — an
            // explicit `logical_modules` entry already pinned itself at
            // the catchall target. Append unclaimed bindings to that
            // plan so the residual sweep still has a home, and flip
            // its `explicit` flag so downstream consumers see it as
            // the residual destination (residual flag on the factorization
            // module, OutputRole::ResidualModule in artifact metadata, and
            // `residual: true` in modules.json).
            let owner_index = self
                .module_plans
                .iter()
                .position(|plan| plan.target_path == catchall_target);
            if let Some(owner_index) = owner_index {
                let owner_id = ModuleId(LogicalModuleIndex(owner_index));
                let owner_plan = &mut self.module_plans[owner_index];
                owner_plan.explicit = false;
                for decl in declarations {
                    for (name, id) in &decl.bindings {
                        if !self.binding_assignment.contains_key(id) {
                            self.binding_assignment.insert(id.clone(), owner_index);
                            owner_plan
                                .bindings
                                .entry(name.clone())
                                .or_insert_with(|| name.clone());
                            self.bindings_catalogue
                                .insert(id.clone(), BindingKind::Owned { module: owner_id });
                        }
                    }
                }
                self.residual_plan_index = Some(owner_index);
            }
        }
        Ok(())
    }

    /// `MiniFactors` mode: every unclaimed atomic factor unit gets
    /// its own synthesized plan at `__auto/mini/NNNN`. Run after
    /// Stage A.5 (`apply_rebind_folds`) so the residual sweep +
    /// rebind folding have already settled which units are still
    /// unclaimed. Bindings
    /// previously parked at the residual plan are moved out into the
    /// synthesized plan; the residual plan's `bindings` map is
    /// pruned to match.
    pub(super) fn synthesize_mini_factors(
        &mut self,
        precomputed: &OwnerGraphAndUnits,
        body: &[ModuleItem],
        target_dir: &str,
    ) -> Result<()> {
        let owner_graph = &precomputed.owner_graph;
        let atomic_units = &precomputed.atomic_units;
        let mut owner_declared_names: HashMap<OwnerId, Vec<Id>> = HashMap::new();
        let mut owner_statement_ordinal: HashMap<OwnerId, usize> = HashMap::new();
        for node in owner_graph.iter_nodes() {
            let ids: Vec<Id> = node.declared.iter().cloned().collect();
            owner_declared_names.insert(node.id, ids);
            owner_statement_ordinal.insert(node.id, node.statement_ordinal.0);
        }

        let residual_plan_index = self.residual_plan_index;
        let binding_assignment = &self.binding_assignment;
        let anonymous_ordinal_assignment = &self.anonymous_ordinal_assignment;
        // A unit member counts as unclaimed iff every declared binding
        // is either absent from `binding_assignment` or assigned to
        // the residual plan (if any); anonymous owners must similarly
        // be unassigned or routed via residual. If any member is
        // claimed by an explicit (non-residual) plan, the spec author
        // already named the unit's destination — leave the existing
        // claim intact (and let downstream validation flag an
        // atomic-unit conflict if the claims disagree).
        let is_owner_unclaimed = |owner: OwnerId| -> bool {
            let names = owner_declared_names
                .get(&owner)
                .map(Vec::as_slice)
                .unwrap_or(&[]);
            for id in names {
                match binding_assignment.get(id).copied() {
                    None => continue,
                    Some(idx) if Some(idx) == residual_plan_index => continue,
                    Some(_) => return false,
                }
            }
            if names.is_empty() {
                let Some(stmt_ord) = owner_statement_ordinal.get(&owner).copied() else {
                    return true;
                };
                let Some(body_idx) = body_index_for_statement_ordinal(body, stmt_ord) else {
                    return true;
                };
                match anonymous_ordinal_assignment.get(&body_idx).copied() {
                    None => return true,
                    Some(idx) if Some(idx) == residual_plan_index => return true,
                    Some(_) => return false,
                }
            }
            true
        };

        let mut unclaimed_units: Vec<&BTreeSet<OwnerId>> = atomic_units
            .iter()
            .filter(|unit| unit.members.iter().copied().all(is_owner_unclaimed))
            .map(|unit| &unit.members)
            .collect();
        // Stable iteration order: smallest OwnerId first.
        unclaimed_units.sort_by_key(|members| members.iter().next().copied());

        for (idx, members) in unclaimed_units.into_iter().enumerate() {
            let synthetic_idx = self.module_plans.len();
            let synthetic_module_id = ModuleId(LogicalModuleIndex(synthetic_idx));
            let target_path = format!("__auto/mini/{idx:04}");
            let target_file = target_file_for_request(target_dir, &target_path)?;
            let mut bindings = HashMap::<String, String>::new();
            let mut anonymous_statement_ordinals = Vec::<usize>::new();
            for owner in members {
                let names = owner_declared_names
                    .get(owner)
                    .map(Vec::as_slice)
                    .unwrap_or(&[]);
                if names.is_empty() {
                    let Some(stmt_ord) = owner_statement_ordinal.get(owner).copied() else {
                        continue;
                    };
                    let Some(body_idx) = body_index_for_statement_ordinal(body, stmt_ord) else {
                        continue;
                    };
                    self.anonymous_ordinal_assignment
                        .insert(body_idx, synthetic_idx);
                    anonymous_statement_ordinals.push(body_idx);
                    continue;
                }
                for name in names {
                    let name_str = name.0.to_string();
                    bindings.insert(name_str.clone(), name_str.clone());
                    // Move the binding out of the residual plan (if it
                    // was staged there by the sweep above) into the
                    // synthesized plan. The residual plan's
                    // bindings/anonymous-ordinal maps are pruned so it
                    // doesn't double-claim members.
                    if let Some(prev) = self.binding_assignment.get(name).copied()
                        && Some(prev) == residual_plan_index
                        && let Some(residual_idx) = residual_plan_index
                    {
                        self.module_plans[residual_idx].bindings.remove(&name_str);
                    }
                    self.binding_assignment.insert(name.clone(), synthetic_idx);
                    self.bindings_catalogue.insert(
                        name.clone(),
                        BindingKind::Owned {
                            module: synthetic_module_id,
                        },
                    );
                }
            }
            anonymous_statement_ordinals.sort_unstable();
            self.module_plans.push(ModulePlan {
                id: target_path.clone(),
                target_file,
                target_path,
                explicit: false,
                bindings,
                anonymous_statement_ordinals,
                anonymous_statement_comments: BTreeMap::new(),
                comment: None,
                binding_comments: BTreeMap::new(),
                binding_claim_origins: BTreeMap::new(),
            });
        }
        Ok(())
    }

    /// Apply a batch of rebind-fold decisions produced by the
    /// Stage A.5 composer (`stage_one::compute_rebind_folds`).
    ///
    /// Each fold reroutes a single binding from its previous plan
    /// (if any) to the cycle's explicit destination. The
    /// `bindings_catalogue` mirrors the new owner, and bindings
    /// that were previously parked at the residual plan are pruned
    /// from that plan's binding list so the residual doesn't
    /// double-claim them.
    ///
    /// Folds are produced in atomic-unit iteration order; we do not
    /// reorder them here. The mutations are idempotent in the sense
    /// that `module_plans[dest].bindings.entry(name).or_insert_with`
    /// preserves any existing entry under that name.
    pub(super) fn apply_rebind_folds(&mut self, folds: Vec<RebindFold>) {
        let residual_plan_index = self.residual_plan_index;
        for fold in folds {
            let RebindFold {
                binding,
                name,
                dest,
                owned_kind,
                previous,
            } = fold;
            self.binding_assignment.insert(binding.clone(), dest);
            self.bindings_catalogue.insert(binding, owned_kind);
            self.module_plans[dest]
                .bindings
                .entry(name.clone())
                .or_insert_with(|| name.clone());
            if let Some(prev_idx) = previous
                && Some(prev_idx) == residual_plan_index
            {
                self.module_plans[prev_idx].bindings.remove(&name);
            }
        }
    }

    /// Borrow access to the current binding assignment so the
    /// Stage A.5 composer can compute folds against it without
    /// mutating the builder.
    pub(super) fn binding_assignment(&self) -> &HashMap<Id, usize> {
        &self.binding_assignment
    }

    /// The residual landing-site plan index, if one was created by
    /// the residual sweep. Needed by Stage A.5 to know which
    /// existing claims count as "swept" (and hence still foldable).
    pub(super) fn residual_plan_index(&self) -> Option<usize> {
        self.residual_plan_index
    }

    pub(super) fn selector_diagnostics_report(
        &self,
        chunk_id: &str,
    ) -> Option<SelectorDiagnosticsReport> {
        let mut diagnostics = Vec::new();
        for diagnostic in &self.source_match_diagnostics {
            diagnostics.push(SelectorDiagnosticEntry {
                category: diagnostic.category.clone(),
                module_id: diagnostic.module_id.clone(),
                module_path: Some(diagnostic.module_path.clone()),
                export_name: Some(diagnostic.export_name.clone()),
                selector_kind: source_match_selector_kind(&diagnostic.claim_origin).to_string(),
                target_binding: diagnostic.selector.target_binding.clone(),
                claim_origin: Some(diagnostic.claim_origin.clone()),
                body_indices: diagnostic.body_indices.clone(),
                first_mismatch: diagnostic.first_mismatch.clone(),
                source_match_preview: Some(source_match::source_match_preview(
                    &diagnostic.selector.match_source,
                )),
                source_match_hash: Some(source_match::selector_key(&diagnostic.selector)),
                source_match_body_hash: Some(source_match::selector_body_key(&diagnostic.selector)),
                duplicate_claim: None,
                message: diagnostic.message.clone(),
                recommended_next_action: recommended_source_match_action(&diagnostic.category)
                    .to_string(),
            });
        }
        for diagnostic in &self.anonymous_statement_diagnostics {
            let category = classify_source_match_failure(&diagnostic.message);
            diagnostics.push(SelectorDiagnosticEntry {
                category: category.to_string(),
                module_id: diagnostic.module_id.clone(),
                module_path: module_path_from_id(&diagnostic.module_id),
                export_name: None,
                selector_kind: "anonymous_statements.source_match".to_string(),
                target_binding: None,
                claim_origin: None,
                body_indices: Vec::new(),
                first_mismatch: first_relevant_error_line(&diagnostic.message),
                source_match_preview: Some(source_match::source_match_preview(
                    &diagnostic.selector.match_source,
                )),
                source_match_hash: None,
                source_match_body_hash: None,
                duplicate_claim: None,
                message: diagnostic.message.clone(),
                recommended_next_action: recommended_source_match_action(category).to_string(),
            });
        }
        for duplicate in &self.duplicate_binding_claims {
            diagnostics.push(SelectorDiagnosticEntry {
                category: "duplicate_claim".to_string(),
                module_id: duplicate.duplicate.module_id.clone(),
                module_path: module_path_from_id(&duplicate.duplicate.module_id),
                export_name: duplicate.duplicate.export_name.clone(),
                selector_kind: "duplicate_claim".to_string(),
                target_binding: None,
                claim_origin: duplicate.duplicate.claim_origin.clone(),
                body_indices: Vec::new(),
                first_mismatch: None,
                source_match_preview: None,
                source_match_hash: None,
                source_match_body_hash: None,
                duplicate_claim: Some(DuplicateClaimReport {
                    chunk_id: duplicate.chunk_id.clone(),
                    binding: duplicate.binding.clone(),
                    existing: DuplicateClaimSiteReport::from(&duplicate.existing),
                    duplicate: DuplicateClaimSiteReport::from(&duplicate.duplicate),
                }),
                message: duplicate.render(),
                recommended_next_action: "Move duplicate claims into one logical module, remove the duplicate member, or expose aliases from the same module."
                    .to_string(),
            });
        }
        if diagnostics.is_empty() {
            return None;
        }
        diagnostics.sort_by(|a, b| {
            (
                a.category.as_str(),
                a.module_id.as_str(),
                a.export_name.as_deref().unwrap_or_default(),
                a.selector_kind.as_str(),
            )
                .cmp(&(
                    b.category.as_str(),
                    b.module_id.as_str(),
                    b.export_name.as_deref().unwrap_or_default(),
                    b.selector_kind.as_str(),
                ))
        });
        let mut counts = BTreeMap::new();
        for diagnostic in &diagnostics {
            *counts.entry(diagnostic.category.clone()).or_insert(0) += 1;
        }
        Some(SelectorDiagnosticsReport {
            chunk_id: chunk_id.to_string(),
            counts,
            diagnostics,
            coverage_notes: vec![
                "Name-pin debt annotated with note: is not yet surfaced as structured entries (note: is not plumbed through MemberRequest)."
                    .to_string(),
                "Free-readable-identifier failures (alpha_all readable names used as free references rather than local binders) are not yet classified — see TODO.md P1.5."
                    .to_string(),
            ],
        })
    }

    pub(super) fn finalize(self) -> Result<ChunkPlan> {
        let mut reports = Vec::new();
        if !self.source_match_diagnostics.is_empty() {
            reports.push(render_source_match_diagnostics(
                &self.source_match_diagnostics,
            ));
        }
        if !self.anonymous_statement_diagnostics.is_empty() {
            reports.push(render_anonymous_statement_diagnostics(
                &self.anonymous_statement_diagnostics,
            ));
        }
        if !self.duplicate_binding_claims.is_empty() {
            reports.push(render_duplicate_binding_claims(
                &self.duplicate_binding_claims,
            ));
        }
        if !reports.is_empty() {
            bail!("{}", reports.join("\n\n"));
        }
        Ok(ChunkPlan {
            module_plans: self.module_plans,
            binding_assignment: self.binding_assignment,
            bindings_catalogue: self.bindings_catalogue,
            anonymous_ordinal_assignment: self.anonymous_ordinal_assignment,
            unmatched_spec_claims: self.unmatched_spec_claims,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn selector_backend_choice_defaults_to_ascent() {
        assert_eq!(
            parse_global_selector_backend(None, None).unwrap(),
            GlobalSelectorBackendChoice::Ascent
        );
        assert_eq!(
            parse_global_selector_backend(Some(""), None).unwrap(),
            GlobalSelectorBackendChoice::Ascent
        );
        assert_eq!(
            parse_global_selector_backend(Some("ascent"), None).unwrap(),
            GlobalSelectorBackendChoice::Ascent
        );
    }

    #[test]
    fn selector_backend_choice_accepts_ortools_cpsat_aliases() {
        for backend in ["ortools-cpsat", "ortools_cpsat", "cp-sat", "cpsat"] {
            assert_eq!(
                parse_global_selector_backend(Some(backend), Some(" /tmp/solver ")).unwrap(),
                GlobalSelectorBackendChoice::OrToolsCpSat {
                    solver_path: PathBuf::from("/tmp/solver")
                }
            );
        }
    }

    #[test]
    fn selector_backend_choice_requires_sidecar_path() {
        let error = parse_global_selector_backend(Some("ortools-cpsat"), None)
            .expect_err("missing sidecar path should fail");
        assert!(
            error
                .to_string()
                .contains("DUCKTAPE_DEBUNDLE_ORTOOLS_CPSAT_SOLVER"),
            "{error}"
        );
    }

    #[test]
    fn selector_backend_choice_rejects_unknown_backend() {
        let error = parse_global_selector_backend(Some("manual-csp"), None)
            .expect_err("unknown backend should fail");
        assert!(
            error
                .to_string()
                .contains("unknown DUCKTAPE_DEBUNDLE_SELECTOR_BACKEND"),
            "{error}"
        );
    }
}
