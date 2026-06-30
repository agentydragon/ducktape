//! `ChunkPlanBuilder` owns the per-chunk mutable state that
//! `materialize_logical_chunk` threads across its eight phases. Each phase is a
//! method on the builder, so the shared lookup (`bindings_catalogue` +
//! `binding_assignment`) lives behind the builder's encapsulation rather than
//! being open-coded per phase.

use super::super::ordinal::body_index_for_statement_ordinal;
use super::*;
use crate::plans::{AnonymousStatementRequest, RelationalSelector};
use analysis::{OwnerId, StatementOrdinal};
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

struct SelectorFactCoverage<'a> {
    owner_kind_by_owner: BTreeMap<OwnerId, &'a str>,
    owners_by_binding: BTreeMap<&'a str, BTreeSet<OwnerId>>,
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
    ClaimOutcome, ResolvedClaim, SelectorAtom, SelectorFact, SelectorFactStore, SelectorProgram,
    SelectorTargetId,
};
use selector_ir_lowering::{
    MemberSelectorLoweringContext, MemberSelectorProgramBuilder, MemberSelectorSpecRef,
};
use selector_runtime::solve_global_selector_program;
use source_match::legacy_resolver::SelectorResolver;

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

fn member_selector_ref_for_global_solver(
    member: &MemberRequest,
) -> Option<MemberSelectorSpecRef<'_>> {
    if let Some(binding) = &member.binding_selector
        && !member.is_import_specifier
    {
        return Some(MemberSelectorSpecRef::Binding(binding));
    }

    if let Some(selector) = &member.source_match {
        return Some(MemberSelectorSpecRef::SourceMatch(selector));
    }

    if let Some(relational) = &member.relational {
        return Some(match relational {
            RelationalSelector::CrossRef(target) => MemberSelectorSpecRef::CrossRef(target),
            RelationalSelector::ReadsMember(target) => MemberSelectorSpecRef::ReadsMember(target),
            RelationalSelector::MemberOfModule(target) => {
                MemberSelectorSpecRef::MemberOfModule(target)
            }
            RelationalSelector::PassedToCall(target) => MemberSelectorSpecRef::PassedToCall(target),
            RelationalSelector::MakesDecorateCall(target) => {
                MemberSelectorSpecRef::MakesDecorateCall(target)
            }
            RelationalSelector::IntrinsicAlias(target) => {
                MemberSelectorSpecRef::IntrinsicAlias(target)
            }
        });
    }

    if member.resolves_after_chunk_analysis() || member.is_import_specifier {
        return None;
    }
    member
        .binding_selector
        .as_ref()
        .map(MemberSelectorSpecRef::Binding)
}

fn selector_fact_coverage(facts: &SelectorFactStore) -> SelectorFactCoverage<'_> {
    let mut owner_kind_by_owner = BTreeMap::new();
    let mut owners_by_binding = BTreeMap::<&str, BTreeSet<OwnerId>>::new();
    for fact in &facts.facts {
        match fact {
            SelectorFact::Owner {
                owner,
                statement_kind,
                ..
            } => {
                owner_kind_by_owner.insert(*owner, statement_kind.as_str());
            }
            SelectorFact::DeclaredBinding { owner, binding, .. } => {
                owners_by_binding
                    .entry(binding.as_str())
                    .or_default()
                    .insert(*owner);
            }
            _ => {}
        }
    }
    SelectorFactCoverage {
        owner_kind_by_owner,
        owners_by_binding,
    }
}

fn binding_source_kind_statement_kind(kind: spec::BindingSourceKind) -> &'static str {
    match kind {
        spec::BindingSourceKind::ImportSpecifier => "import",
        spec::BindingSourceKind::VariableDeclarator => "var_decl",
        spec::BindingSourceKind::FunctionDeclaration => "fn_decl",
        spec::BindingSourceKind::ClassDeclaration => "class_decl",
    }
}

fn binding_selector_has_fact_candidate(
    coverage: &SelectorFactCoverage<'_>,
    member: &MemberRequest,
) -> bool {
    let Some(selector) = &member.binding_selector else {
        return true;
    };
    if member.is_import_specifier {
        return true;
    }
    let Some(owners) = coverage.owners_by_binding.get(selector.name.as_str()) else {
        return false;
    };
    let Some(kind) = selector.kind else {
        return !owners.is_empty();
    };
    let expected = binding_source_kind_statement_kind(kind);
    owners.iter().any(|owner| {
        coverage
            .owner_kind_by_owner
            .get(owner)
            .is_some_and(|actual| *actual == expected)
    })
}

fn selector_fact_store_for_chunk(
    program: &SelectorProgram,
    chunk_id: ChunkId,
    owner_graph: &analysis::OwnerGraph,
    module: &swc_ecma_ast::Module,
    import_sources: &HashMap<String, String>,
) -> Result<SelectorFactStore> {
    let mut store = SelectorFactStore::default();
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

    if selector_program_needs_ast_edb(program) {
        let ast_facts = chunk_facts::extract_facts(module).map_err(|unsupported| {
            anyhow!(
                "chunk {:?}: selector AST fact extraction failed at {}; global selector solving \
                 needs a complete AST EDB for this selector program",
                chunk_id,
                unsupported.context,
            )
        })?;
        store.extend_chunk_facts(chunk_id, &ast_facts);
    }
    if selector_program_needs_member_reads(program) {
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
    }
    if selector_program_needs_module_member_uses(program) {
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
    }
    if selector_program_needs_call_argument_uses(program) {
        for call in chunk_facts::call_argument_uses(module) {
            store.push(SelectorFact::CallArgumentUse {
                chunk_id,
                argument: call.argument,
                callee_object: call.callee_object,
                callee_member: call.callee_member,
                arg_index: call.arg_index,
            });
        }
    }
    if selector_program_needs_decorate_call_uses(program) {
        for call in chunk_facts::decorate_call_uses(module) {
            store.push(SelectorFact::DecorateCallUse {
                chunk_id,
                callee: call.callee,
                class_anchor: call.class_anchor,
                member: call.member,
            });
        }
    }
    if selector_program_needs_intrinsic_alias_uses(program) {
        for alias in chunk_facts::intrinsic_alias_uses(module) {
            store.push(SelectorFact::IntrinsicAliasUse {
                chunk_id,
                binding: alias.binding,
                property: alias.property,
            });
        }
    }
    Ok(store)
}

fn selector_program_needs_ast_edb(program: &SelectorProgram) -> bool {
    program.atoms.iter().any(|atom| {
        matches!(
            atom,
            SelectorAtom::OwnerTopLevelRoot { .. }
                | SelectorAtom::AstKind { .. }
                | SelectorAtom::AstChild { .. }
                | SelectorAtom::AstChildListPattern { .. }
                | SelectorAtom::AstSuperClass { .. }
                | SelectorAtom::AstChildCount { .. }
                | SelectorAtom::AstStringLiteral { .. }
                | SelectorAtom::AstStringLiteralMatchingRegex { .. }
                | SelectorAtom::AstNumberLiteral { .. }
                | SelectorAtom::AstBoolLiteral { .. }
                | SelectorAtom::AstIdentifierName { .. }
                | SelectorAtom::AstPropertyName { .. }
                | SelectorAtom::AstBareProperty { .. }
                | SelectorAtom::AstOperator { .. }
                | SelectorAtom::AstRegexLiteral { .. }
                | SelectorAtom::AstTopLevel { .. }
        )
    })
}

fn selector_program_needs_member_reads(program: &SelectorProgram) -> bool {
    program.atoms.iter().any(|atom| {
        matches!(
            atom,
            SelectorAtom::ReadsMember { .. } | SelectorAtom::ReadsMemberOfOwner { .. }
        )
    })
}

fn selector_program_needs_module_member_uses(program: &SelectorProgram) -> bool {
    program
        .atoms
        .iter()
        .any(|atom| matches!(atom, SelectorAtom::ConsumesModuleMember { .. }))
}

fn selector_program_needs_call_argument_uses(program: &SelectorProgram) -> bool {
    program.atoms.iter().any(|atom| {
        matches!(
            atom,
            SelectorAtom::PassedToCall { .. } | SelectorAtom::PassedToCallOfOwner { .. }
        )
    })
}

fn selector_program_needs_decorate_call_uses(program: &SelectorProgram) -> bool {
    program.atoms.iter().any(|atom| {
        matches!(
            atom,
            SelectorAtom::MakesDecorateCall { .. } | SelectorAtom::MakesDecorateCallForOwner { .. }
        )
    })
}

fn selector_program_needs_intrinsic_alias_uses(program: &SelectorProgram) -> bool {
    program
        .atoms
        .iter()
        .any(|atom| matches!(atom, SelectorAtom::IntrinsicAlias { .. }))
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

fn owner_by_body_index_and_binding(
    owner_graph: &analysis::OwnerGraph,
    module: &swc_ecma_ast::Module,
) -> BTreeMap<(usize, String), OwnerId> {
    let mut owners = BTreeMap::new();
    for node in owner_graph.iter_nodes() {
        let Some(body_idx) =
            body_index_for_statement_ordinal(&module.body, node.statement_ordinal.0)
        else {
            continue;
        };
        for binding in &node.declared {
            owners.insert((body_idx, binding.0.as_str().to_string()), node.id);
        }
    }
    owners
}

fn projected_source_match_candidate(
    owner_by_binding: &BTreeMap<(usize, String), OwnerId>,
    candidate: &source_match::MemberBindingMatch,
) -> Result<(OwnerId, String)> {
    let binding = candidate.binding.binding_name.clone();
    let owner = owner_by_binding
        .get(&(candidate.body_idx, binding.clone()))
        .copied()
        .with_context(|| {
            format!(
                "source_match candidate at body index {} binding `{}` \
                 does not map to an owner-graph node",
                candidate.body_idx, binding
            )
        })?;
    Ok((owner, binding))
}

fn projected_source_match_candidate_rows(
    owner_by_binding: &BTreeMap<(usize, String), OwnerId>,
    candidates: Vec<source_match::MemberBindingMatch>,
) -> Result<Vec<(OwnerId, String)>> {
    candidates
        .iter()
        .map(|candidate| projected_source_match_candidate(owner_by_binding, candidate))
        .collect()
}

fn projected_source_match_group_candidate_rows(
    owner_by_binding: &BTreeMap<(usize, String), OwnerId>,
    candidates: Vec<source_match::MemberBindingGroupMatch>,
) -> Result<Vec<BTreeMap<String, (OwnerId, String)>>> {
    candidates
        .into_iter()
        .map(|candidate| {
            candidate
                .bindings
                .iter()
                .map(|(target_binding, binding_match)| {
                    projected_source_match_candidate(owner_by_binding, binding_match)
                        .map(|row| (target_binding.clone(), row))
                })
                .collect()
        })
        .collect()
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
    /// Name-keyed duplicate-check scratch for binding selectors that have a
    /// known source binding spelling but whose ownership is claimed after chunk
    /// analysis through the global solver.
    deferred_binding_claims_by_name: HashMap<String, DuplicateClaimSite>,
    /// Deferred binding selector names that were duplicate claims during
    /// request construction. Every member for such a binding stays out of the
    /// solver program so the existing duplicate-claim report remains the
    /// primary diagnostic.
    duplicate_deferred_binding_names: BTreeSet<String>,
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
            deferred_binding_claims_by_name: HashMap::new(),
            duplicate_deferred_binding_names: BTreeSet::new(),
            duplicate_binding_claims: Vec::new(),
            source_match_diagnostics: Vec::new(),
            anonymous_statement_diagnostics: Vec::new(),
            keep_going,
        }
    }

    /// Process one explicit (non-residual) logical-module request:
    /// claim each member that does not need chunk-analysis facts, and append a
    /// `ModulePlan`.
    /// Solver-resolved members and anonymous statement selectors are left
    /// unclaimed until `resolve_and_claim_global_selectors` runs over chunk
    /// analysis facts. Duplicate-claim detection across all explicit requests is
    /// keyed by binding-name via the builder's scratch indexes.
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
            if !member.binding.is_empty() {
                if let Some(existing_kind) =
                    self.catalogue_index_by_name.get(member.binding.as_str())
                {
                    let duplicate = self.duplicate_claim_for(
                        existing_kind,
                        &member.binding,
                        ctx.chunk_id,
                        request,
                        member,
                    );
                    self.duplicate_binding_claims.push(duplicate);
                    duplicate_bindings.insert(member.binding.clone());
                    if member.resolves_after_chunk_analysis() {
                        self.duplicate_deferred_binding_names
                            .insert(member.binding.clone());
                    }
                    continue;
                }
                if let Some(existing) = self
                    .deferred_binding_claims_by_name
                    .get(member.binding.as_str())
                {
                    self.duplicate_binding_claims.push(DuplicateBindingClaim {
                        chunk_id: ctx.chunk_id.to_string(),
                        binding: member.binding.clone(),
                        existing: existing.clone(),
                        duplicate: DuplicateClaimSite {
                            module_id: request.id.clone(),
                            export_name: Some(member.export_name.clone()),
                            claim_origin: Some(member.claim_origin.clone()),
                        },
                    });
                    duplicate_bindings.insert(member.binding.clone());
                    self.duplicate_deferred_binding_names
                        .insert(member.binding.clone());
                    continue;
                }
            }
            if member.resolves_after_chunk_analysis() {
                if member.binding_selector.is_some() && !member.is_import_specifier {
                    let binding_id = top_level_id(&member.binding, ctx.chunk_top_level_mark);
                    if !ctx.declaration_by_name.contains_key(&binding_id) {
                        self.unmatched_spec_claims.push(crate::UnmatchedSpecClaim {
                            chunk_id: ctx.chunk_id.to_string(),
                            module_path: spec::ModulePath::parse(&request.target_path, "")
                                .expect("request target_path is a canonical module path"),
                            binding_name: member.binding.clone(),
                            export_name: member.export_name.clone(),
                        });
                        continue;
                    }
                }
                if !member.binding.is_empty() {
                    self.deferred_binding_claims_by_name.insert(
                        member.binding.clone(),
                        DuplicateClaimSite {
                            module_id: request.id.clone(),
                            export_name: Some(member.export_name.clone()),
                            claim_origin: Some(member.claim_origin.clone()),
                        },
                    );
                }
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
            .filter(|member| !member.resolves_after_chunk_analysis())
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
            .filter(|member| !member.resolves_after_chunk_analysis())
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

    fn has_recorded_anonymous_statement_failure(&self, request: &LogicalRequest) -> bool {
        self.anonymous_statement_diagnostics
            .iter()
            .any(|diagnostic| diagnostic.module_id == request.id)
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
            .any(MemberRequest::resolves_after_chunk_analysis);
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
        let mut deferred_targets = BTreeMap::<SelectorTargetId, (usize, usize)>::new();
        let mut anonymous_statement_targets = Vec::<AnonymousStatementTargetInfo>::new();
        let mut pending_constraints = Vec::<(usize, usize)>::new();
        let mut pending_source_match_groups = Vec::<(String, SourceMatchGroupAssignment)>::new();
        let mut pending_source_match_group_keys =
            BTreeSet::<(String, SourceMatchGroupCacheKey)>::new();
        if has_deferred_members {
            for (index, request) in explicit_requests.iter().enumerate() {
                let group_assignments = source_match_group_assignments(request);
                for (member_index, member) in request.members.iter().enumerate() {
                    if member.resolves_after_chunk_analysis()
                        && !member.binding.is_empty()
                        && self
                            .duplicate_deferred_binding_names
                            .contains(&member.binding)
                    {
                        continue;
                    }
                    if member.binding_selector.is_some() && !member.is_import_specifier {
                        let binding_id = top_level_id(&member.binding, chunk_top_level_mark);
                        if !declaration_by_name.contains_key(&binding_id) {
                            continue;
                        }
                    }
                    let Some(selector) = member_selector_ref_for_global_solver(member) else {
                        continue;
                    };
                    let target = builder.declare_member_target_in_module_ref(
                        &request.id,
                        &member.export_name,
                        selector,
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
                        pending_constraints.push((index, member_index));
                    }
                    if member.resolves_after_chunk_analysis() {
                        deferred_targets.insert(target, (index, member_index));
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
        let has_pending_source_match = !pending_source_match_groups.is_empty()
            || pending_constraints
                .iter()
                .any(|(request_index, member_index)| {
                    explicit_requests[*request_index].members[*member_index]
                        .source_match
                        .is_some()
                });
        let source_match_projection = has_pending_source_match.then(|| {
            (
                source_match::legacy_resolver::ChunkResolver::new(module),
                owner_by_body_index_and_binding(owner_graph, module),
            )
        });
        for (logical_module, group) in pending_source_match_groups {
            let Some((resolver, owner_by_binding)) = &source_match_projection else {
                continue;
            };
            let projected_rows = resolver
                .member_group_candidates(&logical_module, &group.selector, &group.exports_by_target)
                .and_then(|candidates| {
                    projected_source_match_group_candidate_rows(owner_by_binding, candidates)
                });
            if let Ok(rows) = projected_rows
                && !rows.is_empty()
            {
                builder.lower_projected_source_match_group_candidates(
                    &logical_module,
                    &group.exports_by_target,
                    rows,
                );
                continue;
            }
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
        for (request_index, member_index) in pending_constraints {
            let request = &explicit_requests[request_index];
            let member = &request.members[member_index];
            if let Some(selector) = &member.source_match {
                let Some((resolver, owner_by_binding)) = &source_match_projection else {
                    continue;
                };
                let projected_rows =
                    resolver
                        .member_candidates(&request.id, selector)
                        .and_then(|candidates| {
                            projected_source_match_candidate_rows(owner_by_binding, candidates)
                        });
                if let Ok(rows) = projected_rows
                    && !rows.is_empty()
                {
                    builder.lower_projected_source_match_candidates(
                        &request.id,
                        &member.export_name,
                        rows,
                    );
                    continue;
                }
            }
            let selector = member_selector_ref_for_global_solver(member)
                .expect("pending selector constraint should still be lowerable");
            builder.lower_member_constraints_in_module_ref(
                &request.id,
                &member.export_name,
                selector,
            )?;
        }
        let program = builder.into_program()?;
        if program.targets.is_empty() {
            return Ok(());
        }
        let facts = selector_fact_store_for_chunk(
            &program,
            chunk_id_interned,
            owner_graph,
            module,
            import_sources,
        )?;
        let fact_coverage = selector_fact_coverage(&facts);
        for (index, member_index) in deferred_targets.values().copied() {
            let request = &explicit_requests[index];
            if !request.anonymous_statements.is_empty() {
                continue;
            }
            let member = &request.members[member_index];
            if !binding_selector_has_fact_candidate(&fact_coverage, member) {
                bail!(
                    "logical_module {}: global selector solver found no match for selector member \
                     `{}` ({}): None",
                    request.id,
                    member.export_name,
                    member.claim_origin,
                );
            }
        }
        let result = solve_global_selector_program(&program, &facts)?;

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

        for (target, (index, member_index)) in deferred_targets {
            let request = &explicit_requests[index];
            let member = &request.members[member_index];
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
                        self.claim_imported_binding_after_chunk_analysis(
                            request,
                            member,
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
                    self.claim_binding_after_chunk_analysis(
                        request,
                        member,
                        binding,
                        index,
                        chunk_top_level_mark,
                        chunk_id,
                        declaration_by_name,
                    )?;
                }
                Some(ClaimOutcome::NoMatch) => {
                    if let Some(message) = native_source_match_no_match_message(request, member) {
                        if self.keep_going {
                            self.source_match_diagnostics
                                .push(SourceMatchDiagnostic::new(
                                    &request.id,
                                    &request.target_path,
                                    member,
                                    Vec::new(),
                                    message,
                                ));
                            continue;
                        }
                        bail!("{message}");
                    }
                    if member.binding_selector.is_some()
                        && member.source_match.is_none()
                        && member.relational.is_none()
                        && self.has_recorded_anonymous_statement_failure(request)
                    {
                        continue;
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
                        native_source_match_ambiguous_message(request, member, candidates);
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
                                    member,
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
        Ok(())
    }

    /// Build a `DuplicateBindingClaim` describing a clash between an
    /// already-claimed `binding` and the selector member now resolving to it.
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

    /// Claim a binding the global selector solver resolved to: record an
    /// unmatched-claim if the binding has no top-level declaration; error / record a
    /// duplicate if already claimed; otherwise move it out of the residual sweep and
    /// into module `index`, registering its export name, claim origin, and comment.
    /// The binding was parked at the residual plan when the pre-analysis sweep ran,
    /// before the solver knew its identity.
    #[allow(clippy::too_many_arguments)]
    fn claim_imported_binding_after_chunk_analysis(
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
    fn claim_binding_after_chunk_analysis(
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
        // The residual sweep ran before chunk analysis (before this pass), so the target
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
        self.deferred_binding_claims_by_name = HashMap::new();
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
                    Some(other_index) if Some(other_index) == self.residual_plan_index => {
                        self.binding_assignment
                            .insert(sibling_id.clone(), owner_index);
                        self.bindings_catalogue
                            .insert(sibling_id, BindingKind::Owned { module: owner_id });
                        self.module_plans[other_index].bindings.remove(sibling);
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
    /// its own synthesized plan at `__auto/mini/NNNN`. Run after rebind folding
    /// so the residual sweep and fold decisions have already settled which
    /// units are still unclaimed. Bindings
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

    /// Apply a batch of rebind-fold decisions produced from chunk analysis
    /// (`stage_one::compute_rebind_folds`).
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

    /// Borrow access to the current binding assignment so the rebind-fold
    /// composer can compute folds without mutating the builder.
    pub(super) fn binding_assignment(&self) -> &HashMap<Id, usize> {
        &self.binding_assignment
    }

    /// The residual landing-site plan index, if one was created by the residual
    /// sweep. Needed by rebind folding to know which existing claims count as
    /// "swept" (and hence still foldable).
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
