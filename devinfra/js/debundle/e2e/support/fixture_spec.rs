//! Typed fixture inputs and construction of transform specs. No process execution.

use super::PreparedFixture;
use super::ast_assertions::declared_bindings_in_source_match;
use serde::Deserialize;
use serde::de::value::{Error as DeserializeError, SeqDeserializer};
use spec::InitializerPurity;
use spec::{
    AnonymousStatement, BindingAnnotation, BindingSelector, BindingSourceKind, ChunkRenameMember,
    ChunkRenameSelector, ChunkRenames, CrossRefSelector, DependencyGraphOptions,
    IntrinsicAliasSelector, LoadJsChunksArgs, LogicalModule, MakesDecorateCallSelector,
    MaterializeLogicalModulesConfig, Member as SpecMember, MemberOfModuleSelector, MemberSelector,
    PassedToCallSelector, ReadsMemberSelector, SourceMatch, SourceMatchBinding,
    SourceMatchBindingDetail, SourceMatchClaim, SourceMatchIdentifierMode, SwapVendorChunksConfig,
    TransformSpec, UnassignedMode, WriteJsTreeConfig,
};
use spec::{MemberEffect, MemberPurity};
use std::collections::BTreeMap;

/// One member of a [`LogicalModuleEntry`].
///
/// `name` is the exported name in the materialized module; `selector` pins the
/// entity to extract.
#[derive(Default)]
pub struct Member {
    pub name: &'static str,
    selector: MemberSelector,
    source_match: Option<SourceMatch>,
    purity: Option<MemberPurity>,
    initializer_purity: Option<InitializerPurity>,
    effect: Option<MemberEffect>,
    no_sync_callback_members: Vec<String>,
    pub comment: Option<String>,
}

#[derive(Default)]
pub struct BindingGroup {
    match_source: String,
    adopt_names: Option<FixtureAdoptNames>,
    exports: BTreeMap<&'static str, &'static str>,
    comments: BTreeMap<&'static str, &'static str>,
    notes: BTreeMap<&'static str, &'static str>,
}

impl BindingGroup {
    /// Extract several bindings from one matched source context, typically a
    /// multi-declarator `var`/`let`/`const` statement. `exports` maps the
    /// selector-local binding name to the public export name.
    pub fn source_alpha(
        match_source: impl Into<String>,
        exports: &[(&'static str, &'static str)],
    ) -> Self {
        Self {
            match_source: match_source.into(),
            exports: exports.iter().copied().collect(),
            ..Default::default()
        }
    }

    pub fn source_alpha_adopt_all(match_source: impl Into<String>) -> Self {
        Self {
            match_source: match_source.into(),
            adopt_names: Some(FixtureAdoptNames::All),
            ..Default::default()
        }
    }

    pub fn source_alpha_adopt_names(
        match_source: impl Into<String>,
        names: &[&'static str],
    ) -> Self {
        Self {
            match_source: match_source.into(),
            adopt_names: Some(FixtureAdoptNames::Names(names.to_vec())),
            ..Default::default()
        }
    }

    pub fn with_comments(mut self, comments: &[(&'static str, &'static str)]) -> Self {
        self.comments = comments.iter().copied().collect();
        self
    }

    pub fn with_notes(mut self, notes: &[(&'static str, &'static str)]) -> Self {
        self.notes = notes.iter().copied().collect();
        self
    }
}

impl Member {
    fn pinned(name: &'static str, selector: MemberSelector) -> Self {
        Self {
            name,
            selector,
            ..Default::default()
        }
    }

    /// Extract a binding under its original name.
    pub fn new(name: &'static str) -> Self {
        Self::renamed(name, name)
    }

    /// Extract `binding` and re-export it as `name`.
    pub fn renamed(name: &'static str, binding: &'static str) -> Self {
        Self::renamed_with_kind(name, binding, None)
    }

    /// Like [`Self::renamed`] but narrows the binding selector to a specific
    /// source-declaration kind (`"import_specifier"`, `"class_declaration"`,
    /// `"function_declaration"`, `"variable_declarator"`).
    pub fn renamed_with_kind(
        name: &'static str,
        binding: &'static str,
        kind: Option<&'static str>,
    ) -> Self {
        Self::pinned(
            name,
            MemberSelector {
                binding: Some(BindingSelector {
                    name: binding.to_string(),
                    kind: parse_kind(kind),
                }),
                ..Default::default()
            },
        )
    }

    /// Extract a top-level single-binding declaration selected by source shape
    /// rather than by its current minified binding name.
    pub fn source_alpha(name: &'static str, match_source: impl Into<String>) -> Self {
        Self {
            name,
            source_match: Some(SourceMatch {
                identifiers: SourceMatchIdentifierMode::AlphaAll,
                target_binding: None,
                match_source: match_source.into(),
            }),
            ..Default::default()
        }
    }

    /// Extract one binding from a matched declaration by naming that binding
    /// as it appears in the selector source.
    pub fn source_alpha_target(
        name: &'static str,
        target_binding: impl Into<String>,
        match_source: impl Into<String>,
    ) -> Self {
        Self {
            name,
            source_match: Some(SourceMatch {
                identifiers: SourceMatchIdentifierMode::AlphaAll,
                target_binding: Some(target_binding.into()),
                match_source: match_source.into(),
            }),
            ..Default::default()
        }
    }

    /// Pin a member as the entity that **references** the anchor member `@anchor`
    /// (a delegator / consumer body), re-exported under `name`. `kind` optionally
    /// narrows to one source-declaration kind (`function_declaration`, …) when
    /// several source_units reference the anchor.
    pub fn cross_ref_references(
        name: &'static str,
        anchor: &'static str,
        kind: Option<&'static str>,
    ) -> Self {
        Self::pinned(
            name,
            MemberSelector {
                cross_ref: Some(CrossRefSelector {
                    references: Some(anchor.to_string()),
                    aliases: None,
                    kind: parse_kind(kind),
                }),
                ..Default::default()
            },
        )
    }

    /// Pin a member as the var-decl that **aliases** the anchor member
    /// (`const T = @anchor`), re-exported under `name`.
    pub fn cross_ref_aliases(name: &'static str, anchor: &'static str) -> Self {
        Self::pinned(
            name,
            MemberSelector {
                cross_ref: Some(CrossRefSelector {
                    references: None,
                    aliases: Some(anchor.to_string()),
                    kind: None,
                }),
                ..Default::default()
            },
        )
    }

    /// Pin a member as the entity that **reads member `.member`** off an object,
    /// re-exported under `name`. `object` optionally constrains the object the
    /// member is read off (the readable `name:` of another member, the codegen
    /// context being the canonical object); `kind` optionally narrows to one
    /// source-declaration kind (`function_declaration`, …) when several source_units
    /// read the member.
    pub fn reads_member(
        name: &'static str,
        member: &'static str,
        object: Option<&'static str>,
        kind: Option<&'static str>,
    ) -> Self {
        Self::pinned(
            name,
            MemberSelector {
                reads_member: Some(ReadsMemberSelector {
                    member: member.to_string(),
                    object: object.map(str::to_string),
                    kind: parse_kind(kind),
                }),
                ..Default::default()
            },
        )
    }

    /// Pin a member as the entity **consumed as `module.member`** at a use site
    /// (`module` an import specifier, `member` an export name), re-exported under
    /// `name`. `kind` optionally narrows to one source-declaration kind
    /// (`class_declaration`, …) when several source_units consume the module member.
    pub fn member_of_module(
        name: &'static str,
        module: &'static str,
        member: &'static str,
        kind: Option<&'static str>,
    ) -> Self {
        Self::pinned(
            name,
            MemberSelector {
                member_of_module: Some(MemberOfModuleSelector {
                    module: module.to_string(),
                    member: member.to_string(),
                    kind: parse_kind(kind),
                }),
                ..Default::default()
            },
        )
    }

    /// Pin a member as the entity **passed as an argument** to a call of a known
    /// callee — "the class passed to `@object.callee_member(...)`" — re-exported
    /// under `name`. The `resolves_to`-of-argument primitive: pins a registry-style
    /// target by the call that names it, not its own body or minified name.
    /// `object` optionally constrains the callee's receiver (the readable `name:`
    /// of another member, the registry singleton); `arg_index` optionally pins the
    /// argument position; `kind` optionally narrows the target's own declaration
    /// kind (`class_declaration`, …) when several source_units are passed to the callee.
    pub fn passed_to_call(
        name: &'static str,
        callee_member: &'static str,
        object: Option<&'static str>,
        arg_index: Option<usize>,
        kind: Option<&'static str>,
    ) -> Self {
        Self::pinned(
            name,
            MemberSelector {
                passed_to_call: Some(PassedToCallSelector {
                    callee_member: callee_member.to_string(),
                    object: object.map(str::to_string),
                    arg_index,
                    kind: parse_kind(kind),
                }),
                ..Default::default()
            },
        )
    }

    /// Pin a member as the **callee** of an esbuild `__decorate`-style decorator
    /// application on a pinned class — "the helper that decorates `@class`" —
    /// re-exported under `name`. The inverse-direction sibling of `passed_to_call`:
    /// pins the byte-identical decorate-helper copies by the class each decorates,
    /// not by their own body or minified name. `member` optionally narrows to a
    /// specific decorated member literal; `kind` optionally narrows the helper's own
    /// declaration kind (`variable_declarator`).
    pub fn makes_decorate_call(
        name: &'static str,
        class: &'static str,
        member: Option<&'static str>,
        kind: Option<&'static str>,
    ) -> Self {
        Self::pinned(
            name,
            MemberSelector {
                makes_decorate_call: Some(MakesDecorateCallSelector {
                    class: class.to_string(),
                    member: member.map(str::to_string),
                    kind: parse_kind(kind),
                }),
                ..Default::default()
            },
        )
    }

    /// Pin a member as an **intrinsic-method alias off the unshadowed global
    /// `Object`** (`var X = Object.<property>`) referenced by a known helper,
    /// re-exported under `name`.
    pub fn intrinsic_alias(
        name: &'static str,
        property: &'static str,
        referenced_by: &'static str,
    ) -> Self {
        Self::pinned(
            name,
            MemberSelector {
                intrinsic_alias: Some(IntrinsicAliasSelector {
                    property: property.to_string(),
                    referenced_by: referenced_by.to_string(),
                }),
                ..Default::default()
            },
        )
    }

    /// Attach an author comment to be emitted above the binding's source_unit
    /// statement in the lowered module body. See `spec::Member::comment`.
    pub fn with_comment(mut self, comment: impl Into<String>) -> Self {
        self.comment = Some(comment.into());
        self
    }

    /// Attach a spec-level purity annotation (`pure` / `pure_new`) to the
    /// binding. See `spec::BindingAnnotation::purity`.
    pub fn with_purity(mut self, purity: MemberPurity) -> Self {
        self.purity = Some(purity);
        self
    }

    /// Assert purity of this binding's one variable initializer only. This
    /// does not affect calls or construction of the resulting value.
    pub fn with_initializer_purity(mut self) -> Self {
        self.initializer_purity = Some(InitializerPurity::Pure);
        self
    }

    /// Attach a spec-level local-effect annotation to the binding. See
    /// `spec::BindingAnnotation::effect`.
    pub fn with_effect(mut self, effect: MemberEffect) -> Self {
        self.effect = Some(effect);
        self
    }

    /// Assert that these calls store callback arguments without invoking them
    /// synchronously. See `spec::BindingAnnotation::no_sync_callback_members`.
    pub fn with_no_sync_callback_members(mut self, members: &[&str]) -> Self {
        self.no_sync_callback_members =
            members.iter().map(|member| (*member).to_string()).collect();
        self
    }
}

/// Translate the harness `&'static str` spelling of a statement kind into the
/// spec's typed `BindingSourceKind`. The wire spellings match
/// (`BindingSourceKind` is `#[serde(rename_all = "snake_case")]` and the harness
/// receives the same snake_case strings from builder callers).
fn parse_kind(kind: Option<&'static str>) -> Option<BindingSourceKind> {
    kind.map(|k| {
        serde_json::from_str(&format!("\"{k}\"")).expect("BindingSourceKind from builder kind")
    })
}

/// Construct a `SourceMatchBinding` preserving the harness's
/// `local == name ⇒ Local` wire-routing: `Local(local)` serializes as a bare
/// string, `Detailed { local, name }` as `{ local, name }`.
fn source_match_binding(local: impl Into<String>, name: impl Into<String>) -> SourceMatchBinding {
    let local = local.into();
    let name = name.into();
    if local == name {
        SourceMatchBinding::Local(local)
    } else {
        SourceMatchBinding::Detailed(SourceMatchBindingDetail {
            local,
            name: Some(name),
        })
    }
}

/// Newtype over `spec::AnonymousStatement` so the harness keeps its
/// `::exact` / `::alpha_all` / `.with_comment` builder spelling.
#[derive(Clone)]
struct FixtureAnonymousStatement(AnonymousStatement);

/// Internal-only (never serialized) selector for `fixture_grouped_source_matches`
/// describing which bindings a `BindingGroup` adopts from its matched source.
#[derive(Clone)]
enum FixtureAdoptNames {
    All,
    Names(Vec<&'static str>),
}

impl FixtureAnonymousStatement {
    fn exact(match_source: impl Into<String>) -> Self {
        Self(AnonymousStatement {
            match_source: Some(match_source.into()),
            source_match: None,
            comment: None,
            note: None,
        })
    }

    fn alpha_all(match_source: impl Into<String>) -> Self {
        Self(AnonymousStatement {
            match_source: None,
            source_match: Some(SourceMatch {
                identifiers: SourceMatchIdentifierMode::AlphaAll,
                target_binding: None,
                match_source: match_source.into(),
            }),
            comment: None,
            note: None,
        })
    }

    fn with_comment(mut self, comment: impl Into<String>) -> Self {
        self.0.comment = Some(comment.into());
        self
    }
}

fn fixture_members(members: &[Member]) -> Vec<SpecMember> {
    members
        .iter()
        .filter(|m| m.source_match.is_none())
        .map(|m| SpecMember {
            name: Some(m.name.to_string()),
            selector: m.selector.clone(),
        })
        .collect()
}

fn fixture_member_source_matches(members: &[Member]) -> Vec<SourceMatchClaim> {
    members
        .iter()
        .filter_map(|member| {
            let source_match = member.source_match.as_ref()?;
            let local = match source_match.target_binding.as_deref() {
                Some(target_binding) => target_binding.to_string(),
                None => {
                    let declared = declared_bindings_in_source_match(&source_match.match_source);
                    if declared.len() == 1 {
                        declared[0].clone()
                    } else {
                        member.name.to_string()
                    }
                }
            };
            Some(SourceMatchClaim {
                identifiers: SourceMatchIdentifierMode::default(),
                match_source: source_match.match_source.clone(),
                bindings: vec![source_match_binding(local, member.name)],
                note: None,
            })
        })
        .collect()
}

fn fixture_grouped_source_matches(binding_groups: &[BindingGroup]) -> Vec<SourceMatchClaim> {
    binding_groups
        .iter()
        .map(|group| {
            let locals = match &group.adopt_names {
                None => group
                    .exports
                    .keys()
                    .map(|name| (*name).to_string())
                    .collect(),
                Some(FixtureAdoptNames::Names(names)) => {
                    names.iter().map(|name| (*name).to_string()).collect()
                }
                Some(FixtureAdoptNames::All) => {
                    declared_bindings_in_source_match(&group.match_source)
                }
            };
            SourceMatchClaim {
                identifiers: SourceMatchIdentifierMode::default(),
                match_source: group.match_source.clone(),
                bindings: locals
                    .into_iter()
                    .map(|local| {
                        let public = group
                            .exports
                            .get(local.as_str())
                            .copied()
                            .unwrap_or(local.as_str())
                            .to_string();
                        source_match_binding(local, public)
                    })
                    .collect(),
                note: None,
            }
        })
        .collect()
}

fn fixture_annotations(
    members: &[Member],
    binding_groups: &[BindingGroup],
) -> BTreeMap<String, BindingAnnotation> {
    let mut annotations = BTreeMap::new();
    for member in members {
        if member.comment.is_some()
            || member.purity.is_some()
            || member.initializer_purity.is_some()
            || member.effect.is_some()
            || !member.no_sync_callback_members.is_empty()
        {
            annotations.insert(
                member.name.to_string(),
                BindingAnnotation {
                    purity: member.purity.unwrap_or_default(),
                    initializer_purity: member.initializer_purity.unwrap_or_default(),
                    effect: member.effect.unwrap_or_default(),
                    no_sync_callback_members: member.no_sync_callback_members.clone(),
                    comment: member.comment.clone(),
                    ..Default::default()
                },
            );
        }
    }
    for group in binding_groups {
        for (local, comment) in &group.comments {
            let public = group.exports.get(local).copied().unwrap_or(local);
            annotations.entry(public.to_string()).or_default().comment =
                Some((*comment).to_string());
        }
        for (local, note) in &group.notes {
            let public = group.exports.get(local).copied().unwrap_or(local);
            annotations.entry(public.to_string()).or_default().note = Some((*note).to_string());
        }
    }
    annotations
}

/// One entry of the spec's `logical_modules[chunk_id]` map: the target path
/// (the map key) plus its body (members).
pub type LogicalModuleEntry = (String, LogicalModule);

fn logical_module_entry(
    path: &str,
    members: &[Member],
    binding_groups: &[BindingGroup],
    anonymous_statements: Vec<FixtureAnonymousStatement>,
    comment: Option<String>,
) -> LogicalModuleEntry {
    (path.to_string(), {
        let mut source_matches = fixture_member_source_matches(members);
        source_matches.extend(fixture_grouped_source_matches(binding_groups));
        let annotations = fixture_annotations(members, binding_groups);
        LogicalModule {
            members: fixture_members(members),
            source_matches,
            annotations,
            anonymous_statements: anonymous_statements
                .into_iter()
                .map(|FixtureAnonymousStatement(inner)| inner)
                .collect(),
            comment,
            note: None,
        }
    })
}

pub fn logical_module(path: &str, members: &[Member]) -> LogicalModuleEntry {
    logical_module_entry(path, members, &[], Vec::new(), None)
}

pub fn logical_module_with_binding_groups(
    path: &str,
    members: &[Member],
    binding_groups: &[BindingGroup],
) -> LogicalModuleEntry {
    logical_module_entry(path, members, binding_groups, Vec::new(), None)
}

/// Like [`logical_module`] but attaches a module-level `comment:` block,
/// emitted at the top of the generated module file (above the lowerer's
/// pragma block). See `spec::LogicalModule::comment`.
pub fn logical_module_with_comment(
    path: &str,
    members: &[Member],
    comment: impl Into<String>,
) -> LogicalModuleEntry {
    logical_module_entry(path, members, &[], Vec::new(), Some(comment.into()))
}

/// Like [`logical_module`] but also emits an `anonymous_statements:`
/// list. Each entry's source is matched (modulo spans) against
/// the chunk's top-level statements; the resolver requires exactly
/// one match. Use this when the peel needs to co-move side-effect
/// statements that have no binding name (decorator applications,
/// IIFE preludes, etc.) — see the round-trip test for the canonical
/// shape.
pub fn logical_module_with_anon(
    path: &str,
    members: &[Member],
    anon_matches: &[&str],
) -> LogicalModuleEntry {
    logical_module_entry(
        path,
        members,
        &[],
        anon_matches
            .iter()
            .map(|m| FixtureAnonymousStatement::exact(*m))
            .collect(),
        None,
    )
}

pub fn logical_module_with_anon_alpha(
    path: &str,
    members: &[Member],
    anon_matches: &[&str],
) -> LogicalModuleEntry {
    logical_module_entry(
        path,
        members,
        &[],
        anon_matches
            .iter()
            .map(|m| FixtureAnonymousStatement::alpha_all(*m))
            .collect(),
        None,
    )
}

pub fn logical_module_with_anon_comment(
    path: &str,
    members: &[Member],
    anon_match: &str,
    comment: impl Into<String>,
) -> LogicalModuleEntry {
    logical_module_entry(
        path,
        members,
        &[],
        vec![FixtureAnonymousStatement::exact(anon_match).with_comment(comment)],
        None,
    )
}

pub struct FixtureOpts<'a> {
    pub execution_runtime: spec::ExecutionRuntime,
    pub source: &'a str,
    pub logical_modules: Vec<LogicalModuleEntry>,
    /// Optional `chunk_renames` entry for this chunk. When set, the
    /// spec's top-level `chunk_renames` map carries the rename
    /// members; the materializer applies them in-place to bindings
    /// staying in entry's body without creating a `Logical(R)` for
    /// them.
    pub chunk_renames: Option<ChunkRenames>,
    pub chunk_id: &'a str,
    /// `unassigned_mode` setting for this chunk. Required — every
    /// chunk listed in `logical_modules` or `chunk_renames` must
    /// declare an explicit mode (the spec validator enforces this).
    /// Renders as a YAML object with `kind: <discriminant>` plus
    /// any variant-specific fields. Use [`unassigned_mode_inline`],
    /// [`unassigned_mode_catchall_file`], or
    /// [`unassigned_mode_per_atomic_group`] to build typical bodies.
    pub unassigned_mode: UnassignedMode,
    /// Opt into the dataflow-aware S-chain emission in `graph/` for
    /// this chunk. Default `false` — leaves the strictly-conservative
    /// adjacent-impure chain. Tests that exercise the relaxation set
    /// this `true`.
    pub dataflow_aware_s_chain: bool,
    /// Author-trusted companion to `dataflow_aware_s_chain`: conservative
    /// but present dataflow summaries are used instead of global S-chain
    /// barriers.
    pub trusted_dataflow_summaries: bool,
    /// Input-chunk admission checks to disable for this chunk
    /// (`chunk_analysis_options.<chunk>.admission_overrides`), e.g.
    /// `&["a1_eval"]`. Default empty — all admission checks enforced.
    pub admission_overrides: &'a [&'a str],
    /// Opt into local-property-write effect scoping for this chunk
    /// (`chunk_analysis_options.<chunk>.local_property_effects`).
    /// Default `false` — property writes stay globally-ordered side
    /// effects.
    pub local_property_effects: bool,
    /// Author-certified browser-global slots, as `(root, static property)`.
    pub deferred_global_publications: &'a [(&'a str, &'a str)],
    pub extra_files: &'a [(&'a str, &'a str)],
    /// Additional input chunks `(snapshot-relative path, source)` listed in
    /// `js-files.txt` alongside the entry chunk. Unlike `extra_files`
    /// (post-run runtime siblings), these are debundled artifact chunks the
    /// transform analyzes — e.g. an import target for cross-chunk tests.
    pub extra_chunks: &'a [(&'a str, &'a str)],
    /// Stable chunk ID to original source path, for aliased extraction inputs.
    pub source_paths: &'a [(&'a str, &'a str)],
    /// Extra chunks to process, as `(chunk_id, logical modules)`. They must
    /// also appear in `extra_chunks`; used for cross-chunk emission tests.
    pub extra_chunk_logical_modules: &'a [(&'a str, Vec<LogicalModuleEntry>)],
    /// `chunk_export_purity` entries as `(defining chunk_id, assertion)`,
    /// built via [`ChunkExportPurityBuilder`]. Default empty.
    pub chunk_export_purity: &'a [(&'a str, spec::ChunkExportPurity)],
    /// Additional vendor marks keyed by chunk ID, for report and rewrite cases.
    pub vendor_marks: &'a [(&'a str, spec::VendorMark)],
    /// Keep only `chunk_id` in the materialization artifact while retaining
    /// the full chunk table, exercising incomplete-artifact fallback.
    pub prune_other_chunks: bool,
}

impl<'a> FixtureOpts<'a> {
    pub fn new(source: &'a str, logical_modules: Vec<LogicalModuleEntry>) -> Self {
        // Default mode is `catchall_file` — most fixtures exercise the
        // residual-module emission path and rely on
        // `static/app/modules/residual/unhandled.js` being written.
        // Tests that exercise `InlineInEntry` semantics override with
        // [`unassigned_mode_inline`]; tests that exercise per-group modules
        // override with [`unassigned_mode_per_atomic_group`].
        Self {
            execution_runtime: spec::ExecutionRuntime::default(),
            source,
            logical_modules,
            chunk_renames: None,
            chunk_id: "static/app",
            unassigned_mode: unassigned_mode_catchall_file(None),
            dataflow_aware_s_chain: false,
            trusted_dataflow_summaries: false,
            admission_overrides: &[],
            local_property_effects: false,
            deferred_global_publications: &[],
            extra_files: &[],
            extra_chunks: &[],
            source_paths: &[],
            extra_chunk_logical_modules: &[],
            chunk_export_purity: &[],
            vendor_marks: &[],
            prune_other_chunks: false,
        }
    }

    /// Add analyzed-but-not-materialized sibling chunks (see `extra_chunks`),
    /// e.g. an import target whose exports feed the cross-module purity oracle.
    pub fn with_extra_chunks(mut self, extra_chunks: &'a [(&'a str, &'a str)]) -> Self {
        self.extra_chunks = extra_chunks;
        self
    }

    pub fn with_extra_chunk_logical_modules(
        mut self,
        modules: &'a [(&'a str, Vec<LogicalModuleEntry>)],
    ) -> Self {
        self.extra_chunk_logical_modules = modules;
        self
    }

    pub fn with_source_paths(mut self, source_paths: &'a [(&'a str, &'a str)]) -> Self {
        self.source_paths = source_paths;
        self
    }

    /// Attach `chunk_export_purity` author assertions (see the field).
    pub fn with_chunk_export_purity(
        mut self,
        entries: &'a [(&'a str, spec::ChunkExportPurity)],
    ) -> Self {
        self.chunk_export_purity = entries;
        self
    }

    pub fn with_vendor_marks(mut self, vendor_marks: &'a [(&'a str, spec::VendorMark)]) -> Self {
        self.vendor_marks = vendor_marks;
        self
    }

    pub fn with_prune_other_chunks(mut self) -> Self {
        self.prune_other_chunks = true;
        self
    }

    /// Disable the named admission checks for this chunk via
    /// `chunk_analysis_options.<chunk>.admission_overrides`.
    pub fn with_admission_overrides(mut self, overrides: &'a [&'a str]) -> Self {
        self.admission_overrides = overrides;
        self
    }

    /// Enable the dataflow-aware S-chain emission for this chunk. Used
    /// by tests that pin the relaxation; production specs opt in via
    /// `chunk_analysis_options:` in YAML.
    pub fn with_dataflow_aware_s_chain(mut self) -> Self {
        self.dataflow_aware_s_chain = true;
        self
    }

    /// Enable the trusted dataflow-summary opt-in for this chunk.
    pub fn with_trusted_dataflow_summaries(mut self) -> Self {
        self.trusted_dataflow_summaries = true;
        self
    }

    /// Enable local-property-write effect scoping for this chunk (see
    /// the `local_property_effects` field).
    pub fn with_local_property_effects(mut self) -> Self {
        self.local_property_effects = true;
        self
    }

    /// Attach a `TransformSpec.chunk_renames` entry for this chunk.
    pub fn with_chunk_renames(mut self, chunk_renames: ChunkRenames) -> Self {
        self.chunk_renames = Some(chunk_renames);
        self
    }

    /// Override the default `chunk_id` of `static/app`.
    pub fn with_chunk_id(mut self, chunk_id: &'a str) -> Self {
        self.chunk_id = chunk_id;
        self
    }

    /// Override the default `unassigned_mode` of `catchall_file`.
    pub fn with_unassigned_mode(mut self, mode: UnassignedMode) -> Self {
        self.unassigned_mode = mode;
        self
    }

    /// Extra files to mirror into the materialized app root post-run.
    pub fn with_extra_files(mut self, extra_files: &'a [(&'a str, &'a str)]) -> Self {
        self.extra_files = extra_files;
        self
    }
}

/// Build an `unassigned_mode: inline_in_entry`
/// entry — unclaimed bindings stay inline in the chunk's entry file.
pub fn unassigned_mode_inline() -> UnassignedMode {
    UnassignedMode::InlineInEntry
}

/// Build an `unassigned_mode: catchall_file` entry.
/// `target` of `None` means "default residual target", which the
/// materializer resolves to `residual/unhandled`.
pub fn unassigned_mode_catchall_file(target: Option<&str>) -> UnassignedMode {
    UnassignedMode::CatchallFile {
        target: target.map(str::to_string),
    }
}

/// Build an `unassigned_mode: per_atomic_group` entry.
pub fn unassigned_mode_per_atomic_group() -> UnassignedMode {
    UnassignedMode::PerAtomicGroup
}

/// Fluent wrapper for building a `(chunk, ChunkExportPurity)` tuple with the
/// export-level assertion surfaces populated.
pub struct ChunkExportPurityBuilder {
    chunk: &'static str,
    purity: spec::ChunkExportPurity,
}

impl ChunkExportPurityBuilder {
    pub fn new(chunk: &'static str) -> Self {
        Self {
            chunk,
            purity: spec::ChunkExportPurity::default(),
        }
    }

    pub fn with_pure_exports(mut self, exports: &[&str]) -> Self {
        self.purity.pure_exports = exports.iter().map(|s| (*s).to_string()).collect();
        self
    }

    /// Assert construction through the listed exports is pure, while
    /// constructor argument expressions retain their own effects.
    pub fn with_pure_new_exports(mut self, exports: &[&str]) -> Self {
        self.purity.pure_new_exports = exports.iter().map(|s| (*s).to_string()).collect();
        self
    }

    /// Assert member calls on the named namespace exports are pure
    /// (see `spec::ChunkExportPurity::pure_members`).
    pub fn with_pure_members(mut self, export: &str, members: &[&str]) -> Self {
        self.purity.pure_members.insert(
            export.to_string(),
            members.iter().map(|s| (*s).to_string()).collect(),
        );
        self
    }

    /// Assert the listed exports are deeply-pure fluent-API roots
    /// (see `spec::ChunkExportPurity::fluent_exports`).
    pub fn with_fluent_exports(mut self, exports: &[&str]) -> Self {
        self.purity.fluent_exports = exports.iter().map(|s| (*s).to_string()).collect();
        self
    }

    /// Assert the listed exports satisfy the audited callback-factory contract
    /// (see `spec::ChunkExportPurity::callback_factory_exports`).
    pub fn with_callback_factories(mut self, exports: &[&str]) -> Self {
        self.purity.callback_factory_exports = exports.iter().map(|s| (*s).to_string()).collect();
        self
    }

    pub fn with_inline_callback_factories(mut self, exports: &[&str]) -> Self {
        self.purity.inline_callback_factory_exports =
            exports.iter().map(|s| (*s).to_string()).collect();
        self
    }

    pub fn with_callback_factory_members(mut self, export: &str, members: &[&str]) -> Self {
        self.purity.callback_factory_members.insert(
            export.to_string(),
            members.iter().map(|s| (*s).to_string()).collect(),
        );
        self
    }

    pub fn with_no_reentry_members(mut self, export: &str, members: &[&str]) -> Self {
        self.purity.no_reentry_members.insert(
            export.to_string(),
            members.iter().map(|s| (*s).to_string()).collect(),
        );
        self
    }

    pub fn with_no_reentry_record_members(mut self, export: &str, members: &[&str]) -> Self {
        self.purity.no_reentry_record_members.insert(
            export.to_string(),
            members.iter().map(|s| (*s).to_string()).collect(),
        );
        self
    }

    pub fn with_no_reentry_exports(mut self, exports: &[&str]) -> Self {
        self.purity.no_reentry_exports = exports.iter().map(|s| (*s).to_string()).collect();
        self
    }

    pub fn with_namespace_independent_members(mut self, export: &str, members: &[&str]) -> Self {
        self.purity.namespace_independent_members.insert(
            export.to_string(),
            members.iter().map(|s| (*s).to_string()).collect(),
        );
        self
    }

    pub fn build(self) -> (&'static str, spec::ChunkExportPurity) {
        (self.chunk, self.purity)
    }
}

pub(super) fn build_spec(opts: &FixtureOpts<'_>, setup: &PreparedFixture) -> TransformSpec {
    let chunk_id = opts.chunk_id;
    let mut logical_modules = BTreeMap::new();
    if !opts.logical_modules.is_empty() {
        logical_modules.insert(
            chunk_id.to_string(),
            opts.logical_modules.iter().cloned().collect(),
        );
    }
    for (extra_chunk, modules) in opts.extra_chunk_logical_modules {
        logical_modules.insert(
            (*extra_chunk).to_string(),
            modules.iter().cloned().collect(),
        );
    }
    let chunk_renames = opts
        .chunk_renames
        .iter()
        .map(|renames| (chunk_id.to_string(), renames.clone()))
        .collect();
    let mut unassigned_mode =
        BTreeMap::from([(chunk_id.to_string(), opts.unassigned_mode.clone())]);
    for (extra_chunk, _) in opts.extra_chunk_logical_modules {
        unassigned_mode.insert(
            (*extra_chunk).to_string(),
            unassigned_mode_catchall_file(None),
        );
    }
    let chunk_analysis_options = if opts.dataflow_aware_s_chain
        || opts.trusted_dataflow_summaries
        || opts.local_property_effects
        || !opts.deferred_global_publications.is_empty()
        || !opts.admission_overrides.is_empty()
    {
        BTreeMap::from([(
            chunk_id.to_string(),
            DependencyGraphOptions {
                dataflow_aware_s_chain: opts.dataflow_aware_s_chain,
                trusted_dataflow_summaries: opts.trusted_dataflow_summaries,
                local_property_effects: opts.local_property_effects,
                deferred_global_publications: opts.deferred_global_publications.iter().fold(
                    BTreeMap::new(),
                    |mut slots, (root, property)| {
                        let root = match *root {
                            "globalThis" => spec::BuiltinGlobalRoot::GlobalThis,
                            "window" => spec::BuiltinGlobalRoot::Window,
                            _ => panic!("unsupported fixture global root: {root}"),
                        };
                        slots
                            .entry(root)
                            .or_insert_with(std::collections::BTreeSet::new)
                            .insert((*property).to_string());
                        slots
                    },
                ),
                admission_overrides: spec::AdmissionOverrides::deserialize(SeqDeserializer::<
                    _,
                    DeserializeError,
                >::new(
                    opts.admission_overrides.iter().copied(),
                ))
                .expect("valid admission override names"),
            },
        )])
    } else {
        BTreeMap::new()
    };

    let chunk_export_purity: BTreeMap<String, spec::ChunkExportPurity> = opts
        .chunk_export_purity
        .iter()
        .map(|(chunk, assertion)| ((*chunk).to_string(), assertion.clone()))
        .collect();

    TransformSpec {
        execution_runtime: opts.execution_runtime,
        inputs: LoadJsChunksArgs {
            input_root: setup.snapshot_root.clone(),
            js_list_path: setup.js_list_path.clone(),
        },
        vendor: opts
            .vendor_marks
            .iter()
            .map(|(chunk_id, mark)| ((*chunk_id).to_string(), mark.clone()))
            .collect(),
        logical_modules,
        chunk_renames,
        unassigned_mode,
        chunk_analysis_options,
        chunk_export_purity,
        swap_vendor_chunks: SwapVendorChunksConfig::default(),
        materialize_logical_modules: MaterializeLogicalModulesConfig {
            prune_other_chunks: opts.prune_other_chunks,
            report_out_dir: Some(setup.report_root.clone()),
            target_dir: "modules".to_string(),
            ..Default::default()
        },
        write_js_tree: Some(WriteJsTreeConfig {
            out_dir: setup.out_root.clone(),
        }),
        emit_browser_harness: None,
    }
}

/// A single-member chunk-renames spec mapping the binding `from_binding` to the
/// exported name `rename_to`.
pub fn chunk_rename(rename_to: &str, from_binding: &str) -> ChunkRenames {
    chunk_renames(&[ChunkRenameEntry::new(rename_to, from_binding)])
}

/// One entry in a multi-member [`chunk_renames`] body. `rename_to` is the final
/// readable export name; `from_binding` is the source binding being renamed.
pub struct ChunkRenameEntry {
    rename_to: String,
    from_binding: String,
    kind: Option<&'static str>,
}

impl ChunkRenameEntry {
    pub fn new(rename_to: impl Into<String>, from_binding: impl Into<String>) -> Self {
        Self {
            rename_to: rename_to.into(),
            from_binding: from_binding.into(),
            kind: None,
        }
    }

    /// Narrow the binding selector to a specific source-declaration kind
    /// (`"import_specifier"`, `"class_declaration"`, …).
    pub fn with_kind(mut self, kind: &'static str) -> Self {
        self.kind = Some(kind);
        self
    }
}

/// Build a chunk-renames body from one or more [`ChunkRenameEntry`]s. The wire
/// shape is `{ members: [{ name, selector: { binding: { name, kind? } } }, …] }`
/// plus an empty (omitted) `annotations` map.
pub fn chunk_renames(entries: &[ChunkRenameEntry]) -> ChunkRenames {
    let members = entries
        .iter()
        .map(|entry| ChunkRenameMember {
            name: Some(entry.rename_to.clone()),
            selector: ChunkRenameSelector {
                binding: BindingSelector {
                    name: entry.from_binding.clone(),
                    kind: parse_kind(entry.kind),
                },
            },
        })
        .collect();
    ChunkRenames {
        members,
        annotations: BTreeMap::new(),
    }
}

/// Build a single-member chunk-renames body that carries a `purity` annotation
/// for the renamed binding. Covers the MobX-style `cx -> getMobxGlobalState`
/// idiom where the rename target must be marked `pure` so the peel doesn't
/// induce a cycle.
pub fn chunk_rename_with_purity(
    rename_to: &str,
    from_binding: &str,
    kind: Option<&'static str>,
    purity: MemberPurity,
) -> ChunkRenames {
    let mut annotations = BTreeMap::new();
    annotations.insert(
        rename_to.to_string(),
        BindingAnnotation {
            purity,
            ..Default::default()
        },
    );
    let members = vec![ChunkRenameMember {
        name: Some(rename_to.to_string()),
        selector: ChunkRenameSelector {
            binding: BindingSelector {
                name: from_binding.to_string(),
                kind: parse_kind(kind),
            },
        },
    }];
    ChunkRenames {
        members,
        annotations,
    }
}

/// One fixture exercising a no-match, an ambiguous selector and a duplicate
/// claim (two members resolving to the same declaration) at once.
pub fn mixed_selector_failure_fixture() -> FixtureOpts<'static> {
    let missing_selector = r#"function selectedFormatter(value) {
  return value.toLowerCase();
}"#;
    let ambiguous_selector = r#"function repeatedHelper() {
  return "shared";
}"#;
    FixtureOpts::new(
        r#"function renderCard(value) {
  return value.trim();
}
function decoratePrimary() {
  return "shared";
}
function decorateSecondary() {
  return "shared";
}
console.log(renderCard(" ok "), decoratePrimary(), decorateSecondary());
export { renderCard, decoratePrimary, decorateSecondary };
"#,
        vec![
            logical_module(
                "diagnostics/missing",
                &[Member::source_alpha("MissingFormatter", missing_selector)],
            ),
            logical_module("source_units/card", &[Member::new("renderCard")]),
            logical_module(
                "duplicates/card",
                &[Member::renamed("renderCardAgain", "renderCard")],
            ),
            logical_module(
                "diagnostics/ambiguous",
                &[Member::source_alpha("AmbiguousHelper", ambiguous_selector)],
            ),
        ],
    )
}
