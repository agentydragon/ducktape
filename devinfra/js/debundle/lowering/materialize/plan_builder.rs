//! `ChunkPlanBuilder` owns the per-chunk mutable state that
//! `materialize_logical_chunk` used to thread as five loose `&mut`
//! maps through eight phases. Each phase becomes a method on the
//! builder; the same lookup (`bindings_catalogue` + `binding_assignment`)
//! that previously appeared in eight subtly different forms now lives
//! behind the builder's encapsulation.
//!
//! See `ARCHITECTURE_BACKLOG.md` § "`materialize_logical_chunk` is a
//! 750-line god function with parallel mutable state" for the
//! original motivation.

use super::super::ordinal::body_index_for_statement_ordinal;
use super::*;

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
    SelectorDiagnosticsReport, SelectorNearestCandidate,
};

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
    nearest_candidates: Vec<SelectorNearestCandidate>,
}

impl SourceMatchDiagnostic {
    fn new(
        runtime_module: &Module,
        module_id: &str,
        module_path: &str,
        member: &MemberRequest,
        message: String,
    ) -> Self {
        let selector = member
            .source_match
            .clone()
            .expect("source_match diagnostic requires unresolved selector");
        let SourceMatchReportDetails {
            body_indices,
            first_mismatch,
            nearest_candidates,
        } = source_match_report_details(runtime_module, module_id, &selector, &message);
        Self {
            module_id: module_id.to_string(),
            module_path: module_path.to_string(),
            export_name: member.export_name.clone(),
            claim_origin: member.claim_origin.clone(),
            selector,
            category: classify_source_match_failure(&message).to_string(),
            body_indices,
            first_mismatch,
            nearest_candidates,
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

struct SourceMatchReportDetails {
    body_indices: Vec<usize>,
    first_mismatch: Option<String>,
    nearest_candidates: Vec<SelectorNearestCandidate>,
}

fn source_match_report_details(
    runtime_module: &Module,
    request_id: &str,
    selector: &spec::AnonymousStatementSelector,
    message: &str,
) -> SourceMatchReportDetails {
    match source_match::source_match_body_debt(runtime_module, request_id, selector, 1, 3) {
        Ok(debt) => {
            let body_indices = debt
                .exact_groups
                .iter()
                .flat_map(|group| group.iter().flatten().copied())
                .collect::<BTreeSet<_>>()
                .into_iter()
                .collect::<Vec<_>>();
            let nearest_candidates = debt
                .near_misses
                .into_iter()
                .map(|candidate| SelectorNearestCandidate {
                    body_index: candidate.body_idx,
                    declared_bindings: candidate.declared_bindings,
                    score: candidate.score,
                    first_mismatch: candidate.reason,
                })
                .collect::<Vec<_>>();
            let first_mismatch = nearest_candidates
                .first()
                .map(|candidate| candidate.first_mismatch.clone())
                .or_else(|| first_relevant_error_line(message));
            SourceMatchReportDetails {
                body_indices,
                first_mismatch,
                nearest_candidates,
            }
        }
        Err(error) => SourceMatchReportDetails {
            body_indices: Vec::new(),
            first_mismatch: Some(format!("failed to analyze nearest candidates: {error:#}")),
            nearest_candidates: Vec::new(),
        },
    }
}

fn first_relevant_error_line(message: &str) -> Option<String> {
    message
        .lines()
        .find(|line| !line.trim().is_empty())
        .map(|line| line.trim().to_string())
}

fn classify_source_match_failure(message: &str) -> &'static str {
    if message.contains("ambiguous") {
        "ambiguous_selector"
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
            "Update the selector source to match the current chunk or inspect nearest_candidates before applying a mechanical rewrite."
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
    pub(super) runtime_module: &'a Module,
    pub(super) declaration_by_name: &'a HashMap<Id, usize>,
    pub(super) chunk_top_level_mark: swc_common::Mark,
    pub(super) target_dir: &'a str,
    pub(super) chunk_id: &'a str,
    pub(super) target_file: &'a str,
    pub(super) runtime_import_facts: &'a RuntimeImportFacts,
}

fn resolve_request_source_matches(
    request: &mut LogicalRequest,
    runtime_module: &Module,
    keep_going: bool,
    diagnostics: &mut Vec<SourceMatchDiagnostic>,
    source_match_cache: &mut BTreeMap<
        spec::AnonymousStatementSelector,
        source_match::ResolvedMemberBinding,
    >,
    source_match_group_cache: &mut BTreeMap<
        SourceMatchGroupCacheKey,
        BTreeMap<String, source_match::ResolvedMemberBinding>,
    >,
) -> Result<()> {
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

    let mut resolved_member_indices = BTreeSet::new();
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
        let cache_key = SourceMatchGroupCacheKey::new(selector.clone(), &exports_by_target);
        let resolved = match source_match_group_cache.get(&cache_key) {
            Some(resolved) => resolved.clone(),
            None => {
                let resolved = match source_match::resolve_member_binding_group(
                    runtime_module,
                    &request.id,
                    &selector,
                    &exports_by_target,
                ) {
                    Ok(resolved) => resolved,
                    Err(error) if keep_going => {
                        let message = format!("{error:#}");
                        let module_id = request.id.clone();
                        let module_path = request.target_path.clone();
                        for idx in member_indices {
                            let member = &request.members[idx];
                            diagnostics.push(SourceMatchDiagnostic::new(
                                runtime_module,
                                &module_id,
                                &module_path,
                                member,
                                message.clone(),
                            ));
                        }
                        continue;
                    }
                    Err(error) => return Err(error),
                };
                source_match_group_cache.insert(cache_key, resolved.clone());
                resolved
            }
        };
        for idx in member_indices {
            let member = &mut request.members[idx];
            let target_binding = member
                .source_match
                .as_ref()
                .and_then(|selector| selector.target_binding.as_ref())
                .expect("grouped selectors always have target_binding");
            let resolved = resolved.get(target_binding).with_context(|| {
                format!("binding group resolver did not return target_binding `{target_binding}`")
            })?;
            apply_resolved_member_binding(member, resolved.clone());
            resolved_member_indices.insert(idx);
        }
    }

    let module_id = request.id.clone();
    let module_path = request.target_path.clone();
    for (idx, member) in request.members.iter_mut().enumerate() {
        if resolved_member_indices.contains(&idx) {
            continue;
        }
        if let Err(error) =
            member.resolve_source_match(runtime_module, &request.id, source_match_cache)
        {
            if !keep_going {
                return Err(error);
            }
            diagnostics.push(SourceMatchDiagnostic::new(
                runtime_module,
                &module_id,
                &module_path,
                member,
                format!("{error:#}"),
            ));
        }
    }
    if keep_going {
        request
            .members
            .retain(|member| member.source_match.is_none());
    }
    Ok(())
}

fn apply_resolved_member_binding(
    member: &mut MemberRequest,
    resolved: source_match::ResolvedMemberBinding,
) {
    member.binding = resolved.binding_name;
    member.is_import_specifier = matches!(resolved.kind, Some(BindingSourceKind::ImportSpecifier));
    member.source_match = None;
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
    /// Successful member-form `source_match` resolutions within this
    /// chunk. Selector matching scans the same runtime chunk AST, and
    /// many large migrations repeat exact selectors across logical
    /// modules while converging duplicate ownership. Keeping successes
    /// at chunk scope avoids re-running the structural matcher for
    /// repeated selectors; failures still resolve live so diagnostics
    /// stay anchored to the current module/export.
    source_match_cache:
        BTreeMap<spec::AnonymousStatementSelector, source_match::ResolvedMemberBinding>,
    /// Successful grouped member-form source matches keyed by the
    /// selector body and requested selector-local target bindings.
    /// Export names are intentionally excluded: they only label
    /// diagnostics/timings, while the resolved source bindings are a
    /// function of the selector and target bindings.
    source_match_group_cache:
        BTreeMap<SourceMatchGroupCacheKey, BTreeMap<String, source_match::ResolvedMemberBinding>>,
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
            source_match_cache: BTreeMap::new(),
            source_match_group_cache: BTreeMap::new(),
            anonymous_statement_diagnostics: Vec::new(),
            keep_going,
        }
    }

    /// Process one explicit (non-residual) logical-module request:
    /// resolve its anonymous-statement matches, claim each named
    /// member, and append a `ModulePlan`. Duplicate-claim detection
    /// across all explicit requests is keyed by binding-name via the
    /// builder's `catalogue_index_by_name` scratch index.
    pub(super) fn add_explicit_request(
        &mut self,
        index: usize,
        request: &mut LogicalRequest,
        ctx: &ExplicitRequestContext<'_>,
        imported_binding_resolver: &mut ArtifactSourceImportResolutionCache<'_>,
        imported_from_by_src: &mut BTreeMap<String, String>,
    ) -> Result<()> {
        resolve_request_source_matches(
            request,
            ctx.runtime_module,
            self.keep_going,
            &mut self.source_match_diagnostics,
            &mut self.source_match_cache,
            &mut self.source_match_group_cache,
        )?;
        reject_duplicate_member_bindings("logical_module", &request.id, &request.members)?;
        let mut bindings = HashMap::<String, String>::new();
        let anonymous_statement_claims = resolve_anonymous_statement_ordinals(
            request,
            ctx.runtime_module,
            self.keep_going,
            &mut self.anonymous_statement_diagnostics,
        )?;
        for claim in &anonymous_statement_claims {
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
                    "anonymous_statements[].match in module {} also matches the \
                     top-level statement at ordinal {} already claimed by module {}; \
                     each anonymous statement may belong to at most one logical \
                     module.",
                    request.id,
                    claim.ordinal,
                    existing_id,
                );
            }
            self.anonymous_ordinal_assignment
                .insert(claim.ordinal, index);
        }
        let anonymous_statement_ordinals: Vec<usize> = anonymous_statement_claims
            .iter()
            .map(|claim| claim.ordinal)
            .collect();
        let anonymous_statement_comments: BTreeMap<usize, String> = anonymous_statement_claims
            .iter()
            .filter_map(|claim| {
                claim
                    .comment
                    .as_ref()
                    .map(|comment| (claim.ordinal, comment.clone()))
            })
            .collect();
        let dest_target_file = target_file_for_request(ctx.target_dir, &request.target_path)?;
        let module_id = ModuleId(LogicalModuleIndex(index));
        let mut duplicate_bindings = BTreeSet::<String>::new();
        for member in &request.members {
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
                if !self.keep_going {
                    bail!("{}", render_duplicate_binding_claims(&[duplicate]));
                }
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
            .filter(|member| !duplicate_bindings.contains(member.binding.as_str()))
            .map(|member| (member.binding.clone(), member.claim_origin.clone()))
            .collect();
        self.module_plans.push(ModulePlan {
            id: request.id.clone(),
            target_file: dest_target_file,
            target_path: request.target_path.clone(),
            explicit: true,
            bindings,
            anonymous_statement_ordinals,
            anonymous_statement_comments,
            comment: request.comment.clone(),
            binding_comments,
            binding_claim_origins,
        });
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
    /// cross-module import wiring must follow it. Runs before the
    /// residual sweep so the sweep doesn't route these bindings to
    /// the catchall while their declaring statement lives elsewhere
    /// (which emitted an `export { name }` whose declaration is in a
    /// different file — a SyntaxError at load).
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
                if self.binding_assignment.contains_key(id) {
                    continue;
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
                nearest_candidates: diagnostic.nearest_candidates.clone(),
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
                nearest_candidates: Vec::new(),
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
                "TODO: anonymous_statement source_match failures and blocker-comment diagnostics still need normalized JSON entries."
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
