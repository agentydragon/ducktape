//! Spec-derived request + plan structures plus the helpers that
//! convert spec entries into `LogicalRequest`s. Mini-factor plan
//! synthesis lives on `ChunkPlanBuilder::synthesize_mini_factors`.

use super::*;

#[derive(Debug, Clone)]
pub(super) struct LogicalRequest {
    pub(super) id: String,
    pub(super) target_path: String,
    pub(super) residual: bool,
    pub(super) members: Vec<MemberRequest>,
    /// Anonymous-statement members the spec asked to co-move into
    /// this module. Resolved later (after AST analysis) into
    /// [`ModulePlan::anonymous_statement_ordinals`] and
    /// [`ModulePlan::anonymous_statement_comments`].
    pub(super) anonymous_statements: Vec<AnonymousStatementRequest>,
    /// Module-level human-readable comment from the spec. Emitted
    /// at the top of the generated module file, before any imports.
    /// See [`spec::LogicalModule::comment`].
    pub(super) comment: Option<String>,
}

#[derive(Debug, Clone)]
pub(super) struct AnonymousStatementRequest {
    pub(super) selector: spec::AnonymousStatementSelector,
    /// Optional `comment:` text from the anonymous statement spec
    /// entry. `note:` is not emitted; it remains YAML scratch
    /// metadata.
    pub(super) comment: Option<String>,
}

/// A relational member selector: pins a member's target by a re-minify-invariant
/// relation to a separately-identified entity, not by the target's own minified
/// name. At most one relation can apply to a member, so the variants are mutually
/// exclusive (an enum, not sibling `Option`s). Each is resolved after the chunk's
/// owner graph is built — the relational facts live there / are derived from the
/// chunk AST and joined to it — by the matching post-Stage-A pass in
/// `materialize::*`. A member carrying one has an empty `binding` and `None`
/// `source_match` until its pass resolves the target into the plan.
#[derive(Debug, Clone)]
pub(super) enum RelationalSelector {
    /// `@Name` cross-reference: a relational edge to a separately-identified anchor
    /// member (`materialize::cross_ref`, resolved against the anchor's already-
    /// resolved binding).
    CrossRef(spec::CrossRefTarget),
    /// The member it reads (`obj.X`); `materialize::reads_member`, resolved against
    /// the owner-graph `reads_member` EDB.
    ReadsMember(spec::ReadsMemberTarget),
    /// **Use-site** consumption (`mod.X`, `mod` an imported binding → its source
    /// module); `materialize::member_of_module`, resolved against the owner-graph
    /// `member_of_module` EDB joined to the import table.
    MemberOfModule(spec::MemberOfModuleTarget),
    /// The `resolves_to`-of-argument primitive: passed as an argument to a call of a
    /// known callee (`registry.register(Target)`); `materialize::passed_to_call`.
    PassedToCall(spec::PassedToCallTarget),
    /// The inverse-direction sibling of `PassedToCall`: the **callee** of an esbuild
    /// `__decorate`-style application on a pinned class (`H([d], @Class.prototype,
    /// "m")`); `materialize::makes_decorate_call`.
    MakesDecorateCall(spec::MakesDecorateCallTarget),
    /// The follow-on companion of `MakesDecorateCall`: an `Object.<property>`
    /// intrinsic alias (`var X = Object.defineProperty`) referenced by a known
    /// helper (`referenced_by: @<decorateHelper>`); `materialize::intrinsic_alias`,
    /// the referencer edge riding the owner graph's own `references` edge.
    IntrinsicAlias(spec::IntrinsicAliasTarget),
}

#[derive(Debug, Clone)]
pub(super) struct MemberRequest {
    pub(super) binding: String,
    pub(super) export_name: String,
    pub(super) source_match: Option<spec::AnonymousStatementSelector>,
    /// The relational selector pinning this member's target, if any. Mutually
    /// exclusive with `binding`/`source_match`: a member carrying one has an empty
    /// `binding` until its post-Stage-A pass resolves the target. See
    /// [`RelationalSelector`].
    pub(super) relational: Option<RelationalSelector>,
    /// When `true`, the member's source is an import specifier in the
    /// source chunk (not a top-level decl). The materializer looks up
    /// the import statement by `binding` in the chunk body and rewrites
    /// it to a re-import in the destination module.
    pub(super) is_import_specifier: bool,
    /// Spec-level purity annotation. `Pure` asserts that calls to the
    /// bound function have no observable side effects — the validator
    /// trusts the annotation and drops S edges for `<binding>(...)`
    /// call sites. `Default` means "not annotated, fall back to
    /// inferred classification". An author-trust contract; see
    /// AGENTS.md "Declared purity" and docs/design.md A9.
    pub(super) purity: MemberPurity,
    /// Spec-level local-effect annotation. `TypescriptDecorateHelper`
    /// asserts that recognized calls to the bound helper mutate only
    /// their target class/prototype, so the analyzer can model a local
    /// effect edge instead of a global side-effect-order edge.
    pub(super) effect: MemberEffect,
    /// Property names on the bound value whose member calls
    /// (`<binding>.<prop>(args)` / `<binding>?.<prop>(args)`) the author
    /// asserts have no observable side effects beyond evaluating their
    /// arguments. Same author-trust contract as `purity: pure` — see
    /// AGENTS.md "Declared purity". Empty when the spec doesn't carry a
    /// `pure_members` entry for this member.
    pub(super) pure_members: Vec<String>,
    /// Property names on the bound value whose calls do not
    /// synchronously invoke callback arguments. The call may still be
    /// impure; this only narrows callback-body at-init promotion.
    pub(super) no_sync_callback_members: Vec<String>,
    /// Per-member human-readable comment from the spec. Emitted as a
    /// `// ...` block above the binding's owner statement in the
    /// generated module body. See [`spec::Member::comment`].
    pub(super) comment: Option<String>,
    /// Short spec-facing description of how this member claim was authored.
    /// Used only in diagnostics after source-match selectors have resolved to
    /// concrete source bindings.
    pub(super) claim_origin: String,
}

impl MemberRequest {
    /// Whether this member is pinned by a relational selector (and so resolves in a
    /// post-Stage-A pass, with an empty `binding` until then).
    pub(super) fn is_relational(&self) -> bool {
        self.relational.is_some()
    }

    pub(super) fn cross_ref(&self) -> Option<&spec::CrossRefTarget> {
        match &self.relational {
            Some(RelationalSelector::CrossRef(target)) => Some(target),
            _ => None,
        }
    }

    pub(super) fn reads_member(&self) -> Option<&spec::ReadsMemberTarget> {
        match &self.relational {
            Some(RelationalSelector::ReadsMember(target)) => Some(target),
            _ => None,
        }
    }

    pub(super) fn member_of_module(&self) -> Option<&spec::MemberOfModuleTarget> {
        match &self.relational {
            Some(RelationalSelector::MemberOfModule(target)) => Some(target),
            _ => None,
        }
    }

    pub(super) fn passed_to_call(&self) -> Option<&spec::PassedToCallTarget> {
        match &self.relational {
            Some(RelationalSelector::PassedToCall(target)) => Some(target),
            _ => None,
        }
    }

    pub(super) fn makes_decorate_call(&self) -> Option<&spec::MakesDecorateCallTarget> {
        match &self.relational {
            Some(RelationalSelector::MakesDecorateCall(target)) => Some(target),
            _ => None,
        }
    }

    pub(super) fn intrinsic_alias(&self) -> Option<&spec::IntrinsicAliasTarget> {
        match &self.relational {
            Some(RelationalSelector::IntrinsicAlias(target)) => Some(target),
            _ => None,
        }
    }

    /// Extend `hints` with this member's spec-level trust assertions
    /// (purity, pure_members, effect). Spec annotations carried on any
    /// member form (logical-module member, chunk_renames member)
    /// propagate the same way — they are semantic trust assertions,
    /// not ownership claims; binding patches routed through
    /// chunk_renames still do not force factorizer grouping.
    pub(super) fn collect_hints(&self, hints: &mut AnalysisHints) {
        if self.purity == MemberPurity::Pure {
            hints.declared_pure.insert(self.binding.clone());
        }
        if self.purity == MemberPurity::PureNew {
            hints.declared_pure_new.insert(self.binding.clone());
        }
        if !self.pure_members.is_empty() {
            hints
                .declared_pure_members
                .entry(self.binding.clone())
                .or_default()
                .extend(self.pure_members.iter().cloned());
        }
        if !self.no_sync_callback_members.is_empty() {
            hints
                .no_sync_callback_members
                .entry(self.binding.clone())
                .or_default()
                .extend(self.no_sync_callback_members.iter().cloned());
        }
        if let Some(effect) = known_effect_from_member_effect(self.effect) {
            hints.known_effects.insert(self.binding.clone(), effect);
        }
    }
}

#[derive(Debug, Clone)]
pub(super) struct ModulePlan {
    pub(super) id: String,
    pub(super) target_file: String,
    /// Logical module path the spec asked for (e.g. `"ai/mcp/foo"`).
    /// Distinct from `target_file`, which is the chunk-relative
    /// emitted file path (e.g. `"modules/foo.js"`).
    pub(super) target_path: String,
    pub(super) explicit: bool,
    /// Local-name → public-export-name for every owned binding this
    /// plan claims (i.e. members whose `selector.binding.kind` is
    /// _not_ `ImportSpecifier`). ImportSpecifier-bound members live
    /// in `ChunkFactorization.bindings` as `BindingKind::Imported` and their
    /// emit is driven from there. Iteration order is undefined;
    /// emit / report sites sort by local name before consuming so
    /// the emitted source and JSON shapes stay deterministic.
    pub(super) bindings: HashMap<String, String>,
    /// Source-chunk statement ordinals of anonymous-statement members
    /// claimed by this module. These owners have empty
    /// `declared_bindings`, so they can't be addressed by name —
    /// the spec resolves them by AST shape (see
    /// [`spec::LogicalModule::anonymous_statements`]). The
    /// materializer routes each such statement into this module's
    /// body in source order, alongside the named members.
    pub(super) anonymous_statement_ordinals: Vec<usize>,
    /// Source-chunk body index → anonymous-statement comment text.
    /// Emitted as a `// ...` block immediately above the matched
    /// statement in the generated module body.
    pub(super) anonymous_statement_comments: BTreeMap<usize, String>,
    /// Module-level human-readable comment from the spec, if any.
    /// Emitted at the top of the generated module file, before
    /// imports. See [`spec::LogicalModule::comment`].
    pub(super) comment: Option<String>,
    /// Local-name → per-member comment text from the spec, for the
    /// bindings this plan claims. Emitted as a `// ...` block above
    /// the binding's owner statement in the generated module body.
    /// See [`spec::Member::comment`].
    pub(super) binding_comments: BTreeMap<String, String>,
    /// Local-name → short spec-facing origin for each owned binding claim.
    /// This is diagnostic metadata; lowering behavior is driven by
    /// [`ModulePlan::bindings`].
    pub(super) binding_claim_origins: BTreeMap<String, String>,
}

pub(super) fn logical_requests_for_chunk(
    chunk_logical_modules: Option<&BTreeMap<String, LogicalModule>>,
    chunk_unassigned_mode: &UnassignedMode,
    chunk_renames_present: bool,
    chunk_id: &str,
    target_dir: &str,
) -> Result<Vec<LogicalRequest>> {
    let mut requests = Vec::new();
    let catchall_target = chunk_unassigned_mode
        .catchall_file_target()
        .map(str::to_string);
    let mut explicit_module_at_catchall = false;
    if let Some(by_target_path) = chunk_logical_modules {
        for (target_path, module) in by_target_path {
            let id = format!("{chunk_id}::{target_path}");
            let members = build_members(&module.members, &module.binding_groups, &id)?;
            reject_duplicate_export_names("logical_module", &id, &members)?;
            reject_duplicate_member_bindings("logical_module", &id, &members)?;
            let mut anonymous_statements = module
                .anonymous_statements
                .iter()
                .map(|stmt| {
                    Ok(AnonymousStatementRequest {
                        selector: stmt.selector()?,
                        comment: stmt.comment.clone(),
                    })
                })
                .collect::<Result<Vec<_>>>()?;
            anonymous_statements.extend(module.binding_groups.iter().filter_map(|group| {
                source_match::binding_group_anonymous_statement_selector(group).map(|selector| {
                    AnonymousStatementRequest {
                        selector,
                        comment: None,
                    }
                })
            }));
            if catchall_target.as_deref() == Some(target_path.as_str()) {
                explicit_module_at_catchall = true;
            }
            requests.push(LogicalRequest {
                id,
                target_path: target_path.clone(),
                residual: false,
                members,
                anonymous_statements,
                comment: module.comment.clone(),
            });
        }
    }
    // Synthesize a memberless catchall-file request when the chunk's
    // `unassigned_mode` is `CatchallFile` and no explicit logical
    // module already claims the catchall target. When an explicit
    // module *is* at the catchall target, the residual sweep in
    // `materialize_logical_chunk` will append unclaimed bindings to
    // that explicit plan instead.
    if let Some(target_path) = catchall_target
        && !explicit_module_at_catchall
    {
        requests.push(LogicalRequest {
            id: format!("{chunk_id}::residual"),
            target_path,
            residual: true,
            members: Vec::new(),
            anonymous_statements: Vec::new(),
            comment: None,
        });
    }
    // Fallback: when the spec is silent about this chunk (no
    // `logical_modules`, default `InlineInEntry` mode, no
    // `chunk_renames`), inject a memberless residual so the
    // materializer has at least one module to point unowned decls
    // at. Skipped when the spec has any `chunk_renames` for the
    // chunk — that signals the spec wants bindings to stay in
    // `ResidualEntry`-land (no `Logical(R)` module, no separate
    // residual file emitted), with renames applied in-place by the
    // lowerer. Skipped when `MiniFactors` is active — the
    // synthesizer takes care of placing unclaimed code into
    // mini-factor modules.
    if requests.is_empty()
        && !chunk_renames_present
        && !matches!(chunk_unassigned_mode, UnassignedMode::MiniFactors)
    {
        requests.push(LogicalRequest {
            id: format!("{chunk_id}::residual"),
            target_path: join_module_path(&[target_dir, "unhandled"]),
            residual: true,
            members: Vec::new(),
            anonymous_statements: Vec::new(),
            comment: None,
        });
    }
    Ok(requests)
}

pub(super) fn build_members(
    members: &[spec::Member],
    binding_groups: &[spec::BindingGroup],
    request_id: &str,
) -> Result<Vec<MemberRequest>> {
    let mut requests = members
        .iter()
        .map(|m| {
            let selected = m.selector.selected()?;
            let kind_label = selected.selector_kind_label();
            // A relational / source-match member's public export name can't default
            // to a binding name — the binding isn't known until the selector
            // resolves — so `name:` is required. The kind label colors the message.
            let require_name = || {
                m.name.clone().ok_or_else(|| {
                    anyhow::anyhow!(
                        "logical_module {request_id}: members[].selector.{kind_label} requires \
                         `name:` because the public export name cannot default to a binding name \
                         until the selector is resolved"
                    )
                })
            };
            let (binding, export_name, source_match, relational, is_import_specifier) =
                match selected {
                    spec::MemberSelectorSpec::Binding(binding) => {
                        let export_name = m.name.clone().unwrap_or_else(|| binding.name.clone());
                        let is_import_specifier =
                            matches!(binding.kind, Some(BindingSourceKind::ImportSpecifier));
                        (binding.name, export_name, None, None, is_import_specifier)
                    }
                    spec::MemberSelectorSpec::SourceMatch(selector) => {
                        (String::new(), require_name()?, Some(selector), None, false)
                    }
                    spec::MemberSelectorSpec::CrossRef(target) => {
                        let relational = RelationalSelector::CrossRef(target);
                        (
                            String::new(),
                            require_name()?,
                            None,
                            Some(relational),
                            false,
                        )
                    }
                    spec::MemberSelectorSpec::ReadsMember(target) => {
                        let relational = RelationalSelector::ReadsMember(target);
                        (
                            String::new(),
                            require_name()?,
                            None,
                            Some(relational),
                            false,
                        )
                    }
                    spec::MemberSelectorSpec::MemberOfModule(target) => {
                        let relational = RelationalSelector::MemberOfModule(target);
                        (
                            String::new(),
                            require_name()?,
                            None,
                            Some(relational),
                            false,
                        )
                    }
                    spec::MemberSelectorSpec::PassedToCall(target) => {
                        let relational = RelationalSelector::PassedToCall(target);
                        (
                            String::new(),
                            require_name()?,
                            None,
                            Some(relational),
                            false,
                        )
                    }
                    spec::MemberSelectorSpec::MakesDecorateCall(target) => {
                        let relational = RelationalSelector::MakesDecorateCall(target);
                        (
                            String::new(),
                            require_name()?,
                            None,
                            Some(relational),
                            false,
                        )
                    }
                    spec::MemberSelectorSpec::IntrinsicAlias(target) => {
                        let relational = RelationalSelector::IntrinsicAlias(target);
                        (
                            String::new(),
                            require_name()?,
                            None,
                            Some(relational),
                            false,
                        )
                    }
                };
            // `source_match` with an explicit `target_binding` and a plain `binding`
            // get richer origins; every other (relational / bare source_match) form
            // is just its selector-kind label.
            let claim_origin = if relational.is_some() {
                format!(
                    "members[].selector.{kind_label} as `{}`",
                    m.name.as_deref().unwrap_or("<unnamed>")
                )
            } else {
                match &m.selector.source_match {
                    Some(selector) => match selector.target_binding.as_deref() {
                        Some(target) => format!(
                            "members[].selector.source_match target_binding `{target}` as `{}`",
                            m.name.as_deref().unwrap_or("<unnamed>")
                        ),
                        None => format!(
                            "members[].selector.source_match as `{}`",
                            m.name.as_deref().unwrap_or("<unnamed>")
                        ),
                    },
                    None => format!(
                        "members[].selector.binding as `{}`",
                        m.name.as_deref().unwrap_or(&binding)
                    ),
                }
            };
            Ok(MemberRequest {
                binding,
                export_name,
                source_match,
                relational,
                is_import_specifier,
                purity: m.purity,
                effect: m.effect,
                pure_members: m.pure_members.clone(),
                no_sync_callback_members: m.no_sync_callback_members.clone(),
                comment: m.comment.clone(),
                claim_origin,
            })
        })
        .collect::<Result<Vec<_>>>()?;

    for group in binding_groups {
        for expanded in source_match::binding_group_member_selectors(request_id, group)? {
            let target_binding = expanded.selector.target_binding.clone();
            requests.push(MemberRequest {
                binding: String::new(),
                export_name: expanded.export_name,
                source_match: Some(expanded.selector),
                relational: None,
                is_import_specifier: false,
                purity: MemberPurity::Default,
                effect: MemberEffect::Default,
                pure_members: Vec::new(),
                no_sync_callback_members: Vec::new(),
                comment: expanded.comment,
                claim_origin: match target_binding {
                    Some(target) => format!("binding_groups[].exports[`{target}`]"),
                    None => "binding_groups[]".to_string(),
                },
            });
        }
    }

    Ok(requests)
}

pub(super) fn known_effect_from_member_effect(effect: MemberEffect) -> Option<KnownEffect> {
    match effect {
        MemberEffect::Default => None,
        MemberEffect::TypescriptDecorateHelper => Some(KnownEffect::TypescriptDecorateHelper),
    }
}
