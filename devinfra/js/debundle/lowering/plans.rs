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
    pub(super) parsed_selector: source_match::ParsedSourceMatchSelector,
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
/// chunk AST and joined to it — by the global selector solver. A member carrying
/// one has an empty `binding` and `None` `source_match` until the solver resolves
/// the target into the plan.
#[derive(Debug, Clone)]
pub(super) enum RelationalSelector {
    /// `@Name` cross-reference: a relational edge to a separately-identified
    /// anchor member, resolved against the anchor's solver assignment.
    CrossRef(spec::CrossRefTarget),
    /// The member it reads (`obj.X`), resolved against the owner-graph
    /// `reads_member` EDB.
    ReadsMember(spec::ReadsMemberTarget),
    /// **Use-site** consumption (`mod.X`, `mod` an imported binding → its source
    /// module), resolved against the owner-graph `member_of_module` EDB joined to
    /// the import table.
    MemberOfModule(spec::MemberOfModuleTarget),
    /// The `resolves_to`-of-argument primitive: passed as an argument to a call of a
    /// known callee (`registry.register(Target)`).
    PassedToCall(spec::PassedToCallTarget),
    /// The inverse-direction sibling of `PassedToCall`: the **callee** of an esbuild
    /// `__decorate`-style application on a pinned class (`H([d], @Class.prototype,
    /// "m")`).
    MakesDecorateCall(spec::MakesDecorateCallTarget),
    /// The follow-on companion of `MakesDecorateCall`: an `Object.<property>`
    /// intrinsic alias (`var X = Object.defineProperty`) referenced by a known
    /// helper (`referenced_by: @<decorateHelper>`), with the referencer edge riding
    /// the owner graph's own `references` edge.
    IntrinsicAlias(spec::IntrinsicAliasTarget),
}

#[derive(Debug, Clone)]
pub(super) struct MemberRequest {
    pub(super) binding: String,
    pub(super) export_name: String,
    pub(super) binding_selector: Option<spec::BindingSelector>,
    pub(super) source_match: Option<spec::AnonymousStatementSelector>,
    pub(super) source_match_parsed: Option<source_match::ParsedSourceMatchSelector>,
    /// The relational selector pinning this member's target, if any. Mutually
    /// exclusive with `binding`/`source_match`: a member carrying one has an empty
    /// `binding` until the global selector solver resolves the target. See
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
    /// <docs/purity_soundness.md> "Declared purity" and docs/design.md A9.
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
    /// <docs/purity_soundness.md> "Declared purity". Empty when the spec doesn't carry a
    /// `pure_members` entry for this member.
    pub(super) pure_members: Vec<String>,
    /// Property names on the bound value whose calls do not
    /// synchronously invoke callback arguments. The call may still be
    /// impure; this only narrows callback-body at-init promotion.
    pub(super) no_sync_callback_members: Vec<String>,
    /// Per-binding human-readable comment from `annotations:`. Emitted as a
    /// `// ...` block above the binding's owner statement in the generated
    /// module body.
    pub(super) comment: Option<String>,
    /// YAML-only note carried through request expansion for conflict checks
    /// and diagnostics. It never emits into generated JS.
    pub(super) note: Option<String>,
    /// Short spec-facing description of how this member claim was authored.
    /// Used only in diagnostics after source-match selectors have resolved to
    /// concrete source bindings.
    pub(super) claim_origin: String,
}

impl MemberRequest {
    /// Whether this member's ownership is intentionally unknown until chunk
    /// analysis facts are available and the global selector solver runs.
    ///
    /// Source-match, relational, and non-import binding selectors resolve
    /// through the global solver. Plain binding selectors keep their binding
    /// spelling for hints and duplicate diagnostics, but ownership is read back
    /// from the solver.
    pub(super) fn resolves_after_chunk_analysis(&self) -> bool {
        self.source_match.is_some()
            || self.relational.is_some()
            || (self.binding_selector.is_some() && !self.is_import_specifier)
    }

    /// Extend `hints` with this member's spec-level trust assertions
    /// (purity, pure_members, effect). Spec annotations carried on any
    /// member form (logical-module member, chunk_renames member)
    /// propagate the same way — they are semantic trust assertions,
    /// not ownership claims; binding patches routed through
    /// chunk_renames still do not force factorizer grouping.
    pub(super) fn has_analysis_hints(&self) -> bool {
        self.purity != MemberPurity::Default
            || self.effect != MemberEffect::Default
            || !self.pure_members.is_empty()
            || !self.no_sync_callback_members.is_empty()
    }

    pub(super) fn collect_hints_for_binding(&self, hints: &mut AnalysisHints, binding: &str) {
        if self.purity == MemberPurity::Pure {
            hints.declared_pure.insert(binding.to_string());
        }
        if self.purity == MemberPurity::PureNew {
            hints.declared_pure_new.insert(binding.to_string());
        }
        if !self.pure_members.is_empty() {
            hints
                .declared_pure_members
                .entry(binding.to_string())
                .or_default()
                .extend(self.pure_members.iter().cloned());
        }
        if !self.no_sync_callback_members.is_empty() {
            hints
                .no_sync_callback_members
                .entry(binding.to_string())
                .or_default()
                .extend(self.no_sync_callback_members.iter().cloned());
        }
        if let Some(effect) = known_effect_from_member_effect(self.effect) {
            hints.known_effects.insert(binding.to_string(), effect);
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
            let members = build_members(
                &module.members,
                &module.source_matches,
                &module.annotations,
                &id,
            )?;
            reject_duplicate_export_names("logical_module", &id, &members)?;
            reject_duplicate_member_bindings("logical_module", &id, &members)?;
            let anonymous_statements = module
                .anonymous_statements
                .iter()
                .map(|stmt| {
                    let selector = stmt.selector()?;
                    let parsed_selector = source_match::ParsedSourceMatchSelector::parse(
                        &id,
                        "source_match",
                        format!("<anonymous source_match in {id}>"),
                        &selector,
                        "source_match",
                    )?;
                    Ok(AnonymousStatementRequest {
                        selector,
                        parsed_selector,
                        comment: stmt.comment.clone(),
                    })
                })
                .collect::<Result<Vec<_>>>()?;
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
    source_matches: &[spec::SourceMatchClaim],
    annotations: &BTreeMap<String, spec::BindingAnnotation>,
    request_id: &str,
) -> Result<Vec<MemberRequest>> {
    let mut requests = members
        .iter()
        .map(|m| {
            let selected = m.selector.selected()?;
            let kind_label = selected.selector_kind_label();
            // A solver-resolved member's public export name can't default to a
            // binding name — the binding isn't known until the selector resolves
            // — so `name:` is required. The kind label colors the message.
            let require_name = || {
                m.name.clone().ok_or_else(|| {
                    anyhow::anyhow!(
                        "logical_module {request_id}: members[].selector.{kind_label} requires \
                         `name:` because the public export name cannot default to a binding name \
                         until the selector is resolved"
                    )
                })
            };
            let (
                binding,
                export_name,
                binding_selector,
                source_match,
                relational,
                is_import_specifier,
            ) = match selected.clone() {
                spec::MemberSelectorSpec::Binding(binding) => {
                    let export_name = m.name.clone().unwrap_or_else(|| binding.name.clone());
                    let is_import_specifier =
                        matches!(binding.kind, Some(BindingSourceKind::ImportSpecifier));
                    (
                        binding.name.clone(),
                        export_name,
                        Some(binding),
                        None,
                        None,
                        is_import_specifier,
                    )
                }
                spec::MemberSelectorSpec::SourceMatch(selector) => (
                    String::new(),
                    require_name()?,
                    None,
                    Some(selector),
                    None,
                    false,
                ),
                spec::MemberSelectorSpec::CrossRef(target) => {
                    let relational = RelationalSelector::CrossRef(target);
                    (
                        String::new(),
                        require_name()?,
                        None,
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
                        None,
                        Some(relational),
                        false,
                    )
                }
            };
            let source_match_parsed = source_match
                .as_ref()
                .map(|selector| {
                    source_match::ParsedSourceMatchSelector::parse(
                        request_id,
                        "source_match",
                        format!("<source_match selector in {request_id}>"),
                        selector,
                        "source_match",
                    )
                })
                .transpose()?;
            let claim_origin = match selected {
                spec::MemberSelectorSpec::Binding(_) => {
                    format!(
                        "members[].selector.binding as `{}`",
                        m.name.as_deref().unwrap_or(&binding)
                    )
                }
                spec::MemberSelectorSpec::SourceMatch(selector) => {
                    match selector.target_binding.as_deref() {
                        Some(target) => format!(
                            "source_matches[].bindings[`{target}`] as `{}`",
                            m.name.as_deref().unwrap_or("<unnamed>")
                        ),
                        None => format!(
                            "source_matches[] as `{}`",
                            m.name.as_deref().unwrap_or("<unnamed>")
                        ),
                    }
                }
                _ => format!(
                    "members[].selector.{kind_label} as `{}`",
                    m.name.as_deref().unwrap_or("<unnamed>")
                ),
            };
            Ok(MemberRequest {
                binding,
                export_name,
                binding_selector,
                source_match,
                source_match_parsed,
                relational,
                is_import_specifier,
                purity: MemberPurity::Default,
                effect: MemberEffect::Default,
                pure_members: Vec::new(),
                no_sync_callback_members: Vec::new(),
                comment: None,
                note: None,
                claim_origin,
            })
        })
        .collect::<Result<Vec<_>>>()?;

    for claim in source_matches {
        for expanded in source_match::source_match_claim_member_selectors(request_id, claim)? {
            let source_match::BindingGroupMemberSelector {
                export_name,
                selector,
                parsed_selector,
                comment,
                note,
            } = expanded;
            let target_binding = selector.target_binding.clone();
            requests.push(MemberRequest {
                binding: String::new(),
                export_name,
                binding_selector: None,
                source_match: Some(selector),
                source_match_parsed: Some(parsed_selector),
                relational: None,
                is_import_specifier: false,
                purity: MemberPurity::Default,
                effect: MemberEffect::Default,
                pure_members: Vec::new(),
                no_sync_callback_members: Vec::new(),
                comment,
                note,
                claim_origin: match target_binding {
                    Some(target) => format!("source_matches[].bindings[`{target}`]"),
                    None => "source_matches[]".to_string(),
                },
            });
        }
    }

    apply_binding_annotations(request_id, &mut requests, annotations)?;
    Ok(requests)
}

fn apply_binding_annotations(
    request_id: &str,
    requests: &mut [MemberRequest],
    annotations: &BTreeMap<String, spec::BindingAnnotation>,
) -> Result<()> {
    let mut by_export_name = BTreeMap::<String, Vec<usize>>::new();
    for (idx, request) in requests.iter().enumerate() {
        by_export_name
            .entry(request.export_name.clone())
            .or_default()
            .push(idx);
    }

    for (export_name, annotation) in annotations {
        let Some(indices) = by_export_name.get(export_name) else {
            bail!(
                "logical_module {request_id}: annotations key `{export_name}` does not match \
                 any member name or source_matches[].bindings name"
            );
        };
        if indices.len() != 1 {
            bail!(
                "logical_module {request_id}: annotations key `{export_name}` is ambiguous \
                 because that readable binding name is claimed {} times",
                indices.len()
            );
        }
        merge_binding_annotation(
            request_id,
            export_name,
            &mut requests[indices[0]],
            annotation,
        )?;
    }
    Ok(())
}

fn merge_binding_annotation(
    request_id: &str,
    export_name: &str,
    request: &mut MemberRequest,
    annotation: &spec::BindingAnnotation,
) -> Result<()> {
    if annotation.purity != MemberPurity::Default {
        if request.purity != MemberPurity::Default && request.purity != annotation.purity {
            bail_annotation_conflict(request_id, export_name, "purity")?;
        }
        request.purity = annotation.purity;
    }

    if annotation.effect != MemberEffect::Default {
        if request.effect != MemberEffect::Default && request.effect != annotation.effect {
            bail_annotation_conflict(request_id, export_name, "effect")?;
        }
        request.effect = annotation.effect;
    }

    if !annotation.pure_members.is_empty() {
        if !request.pure_members.is_empty() && request.pure_members != annotation.pure_members {
            bail_annotation_conflict(request_id, export_name, "pure_members")?;
        }
        request.pure_members = annotation.pure_members.clone();
    }

    if !annotation.no_sync_callback_members.is_empty() {
        if !request.no_sync_callback_members.is_empty()
            && request.no_sync_callback_members != annotation.no_sync_callback_members
        {
            bail_annotation_conflict(request_id, export_name, "no_sync_callback_members")?;
        }
        request.no_sync_callback_members = annotation.no_sync_callback_members.clone();
    }

    if let Some(comment) = &annotation.comment {
        if let Some(existing) = &request.comment
            && existing != comment
        {
            bail_annotation_conflict(request_id, export_name, "comment")?;
        }
        request.comment = Some(comment.clone());
    }

    if let Some(note) = &annotation.note {
        if let Some(existing) = &request.note
            && existing != note
        {
            bail_annotation_conflict(request_id, export_name, "note")?;
        }
        request.note = Some(note.clone());
    }

    Ok(())
}

fn bail_annotation_conflict(request_id: &str, export_name: &str, field: &str) -> Result<()> {
    bail!(
        "logical_module {request_id}: annotations.{export_name}.{field} conflicts with \
         metadata already declared on the member"
    )
}

pub(super) fn known_effect_from_member_effect(effect: MemberEffect) -> Option<KnownEffect> {
    match effect {
        MemberEffect::Default => None,
        MemberEffect::TypescriptDecorateHelper => Some(KnownEffect::TypescriptDecorateHelper),
    }
}
