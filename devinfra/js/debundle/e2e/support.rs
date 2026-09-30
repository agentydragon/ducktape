//! Black-box harness for the `debundle` binary.
//!
//! Drives the CLI through a YAML spec and asserts on the emitted file
//! tree by reading files and re-running them under `node`.

use analysis::OwnerGraphReport;
use artifact::PackageManifest;
use runfiles::{Runfiles, rlocation};
use serde::Serialize;
use serde::de::DeserializeOwned;
use serde_json::Value;
use spec::{
    AnonymousStatement, BindingAnnotation, BindingSelector, BindingSourceKind, ChunkRenameMember,
    ChunkRenameSelector, ChunkRenames, CrossRefSelector, IntrinsicAliasSelector, LoadJsChunksArgs,
    LogicalModule, MakesDecorateCallSelector, MaterializeLogicalModulesConfig,
    Member as SpecMember, MemberOfModuleSelector, MemberSelector, PassedToCallSelector,
    ReadsMemberSelector, SourceMatch, SourceMatchBinding, SourceMatchBindingDetail,
    SourceMatchClaim, SourceMatchIdentifierMode, SwapVendorChunksConfig, TransformSpec,
    WriteJsTreeConfig,
};

/// Re-exported so test files can reference the spec enums behind
/// `Member::with_purity` / `Member::with_effect` without a direct `spec` dep.
pub use spec::{MemberEffect, MemberPurity};

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicUsize, Ordering};
use swc_common::FileName;
use swc_common::sync::Lrc;
use swc_ecma_ast::{
    Decl, ExportSpecifier, Module, ModuleDecl, ModuleExportName, ModuleItem, ObjectPatProp, Pat,
    Stmt,
};
use swc_ecma_parser::{Parser, StringInput, Syntax, TsSyntax, lexer::Lexer};
use tempfile::TempDir;

const DEBUNDLER_RLOCATION: &str = "_main/devinfra/js/debundle/debundle";
const NODE_RLOCATION: &str = "nodejs_linux_amd64/bin/node";

static MODULE_EXPORT_PROBE_COUNTER: AtomicUsize = AtomicUsize::new(0);
static GENERATED_MODULE_SCRIPT_COUNTER: AtomicUsize = AtomicUsize::new(0);

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
    effect: Option<MemberEffect>,
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
    /// several owners reference the anchor.
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
    /// source-declaration kind (`function_declaration`, …) when several owners
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
    /// (`class_declaration`, …) when several owners consume the module member.
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
    /// kind (`class_declaration`, …) when several owners are passed to the callee.
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

    /// Attach an author comment to be emitted above the binding's owner
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

    /// Attach a spec-level local-effect annotation to the binding. See
    /// `spec::BindingAnnotation::effect`.
    pub fn with_effect(mut self, effect: MemberEffect) -> Self {
        self.effect = Some(effect);
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
        if member.comment.is_some() || member.purity.is_some() || member.effect.is_some() {
            annotations.insert(
                member.name.to_string(),
                BindingAnnotation {
                    purity: member.purity.unwrap_or_default(),
                    effect: member.effect.unwrap_or_default(),
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
pub type LogicalModuleEntry = (String, Value);

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
        serde_json::to_value(LogicalModule {
            members: fixture_members(members),
            source_matches,
            annotations,
            anonymous_statements: anonymous_statements
                .into_iter()
                .map(|FixtureAnonymousStatement(inner)| inner)
                .collect(),
            comment,
            note: None,
        })
        .expect("logical module fixture must serialize")
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
    pub source: &'a str,
    pub logical_modules: Vec<LogicalModuleEntry>,
    /// Optional `chunk_renames` entry for this chunk. When set, the
    /// spec's top-level `chunk_renames` map carries the rename
    /// members; the materializer applies them in-place to bindings
    /// staying in entry's body without creating a `Logical(R)` for
    /// them.
    pub chunk_renames: Option<Value>,
    pub chunk_id: &'a str,
    /// `unassigned_mode` setting for this chunk. Required — every
    /// chunk listed in `logical_modules` or `chunk_renames` must
    /// declare an explicit mode (the spec validator enforces this).
    /// Renders as a YAML object with `kind: <discriminant>` plus
    /// any variant-specific fields. Use [`unassigned_mode_inline`],
    /// [`unassigned_mode_catchall_file`], or
    /// [`unassigned_mode_mini_factors`] to build typical bodies.
    pub unassigned_mode: Value,
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
    pub extra_files: &'a [(&'a str, &'a str)],
    /// Additional input chunks `(snapshot-relative path, source)` listed in
    /// `js-files.txt` alongside the entry chunk. Unlike `extra_files`
    /// (post-run runtime siblings), these are debundled artifact chunks the
    /// transform analyzes — e.g. an import target for cross-chunk tests.
    pub extra_chunks: &'a [(&'a str, &'a str)],
    /// Extra chunks to process, as `(chunk_id, logical modules)`. They must
    /// also appear in `extra_chunks`; used for cross-chunk emission tests.
    pub extra_chunk_logical_modules: &'a [(&'a str, Vec<LogicalModuleEntry>)],
    /// `chunk_export_purity` entries as `(defining chunk_id, assertion)`,
    /// built via [`ChunkExportPurityBuilder`]. Default empty.
    pub chunk_export_purity: &'a [(&'a str, spec::ChunkExportPurity)],
}

impl<'a> FixtureOpts<'a> {
    pub fn new(source: &'a str, logical_modules: Vec<LogicalModuleEntry>) -> Self {
        // Default mode is `catchall_file` — most fixtures exercise the
        // residual-module emission path and rely on
        // `static/app/modules/residual/unhandled.js` being written.
        // Tests that exercise `InlineInEntry` semantics override with
        // [`unassigned_mode_inline`]; tests that exercise mini factors
        // override with [`unassigned_mode_mini_factors`].
        Self {
            source,
            logical_modules,
            chunk_renames: None,
            chunk_id: "static/app",
            unassigned_mode: unassigned_mode_catchall_file(None),
            dataflow_aware_s_chain: false,
            trusted_dataflow_summaries: false,
            admission_overrides: &[],
            local_property_effects: false,
            extra_files: &[],
            extra_chunks: &[],
            extra_chunk_logical_modules: &[],
            chunk_export_purity: &[],
        }
    }

    /// Add analyzed-but-not-materialized sibling chunks (see `extra_chunks`),
    /// e.g. an import target whose exports feed the cross-module purity oracle.
    pub fn with_extra_chunks(mut self, extra_chunks: &'a [(&'a str, &'a str)]) -> Self {
        self.extra_chunks = extra_chunks;
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
    pub fn with_chunk_renames(mut self, chunk_renames: Value) -> Self {
        self.chunk_renames = Some(chunk_renames);
        self
    }

    /// Override the default `chunk_id` of `static/app`.
    pub fn with_chunk_id(mut self, chunk_id: &'a str) -> Self {
        self.chunk_id = chunk_id;
        self
    }

    /// Override the default `unassigned_mode` of `catchall_file`.
    pub fn with_unassigned_mode(mut self, mode: Value) -> Self {
        self.unassigned_mode = mode;
        self
    }

    /// Extra files to mirror into the materialized app root post-run.
    pub fn with_extra_files(mut self, extra_files: &'a [(&'a str, &'a str)]) -> Self {
        self.extra_files = extra_files;
        self
    }
}

/// Build the JSON body for an `unassigned_mode: inline_in_entry`
/// entry — unclaimed bindings stay inline in the chunk's entry file.
pub fn unassigned_mode_inline() -> Value {
    serde_json::json!({ "kind": "inline_in_entry" })
}

/// Build the JSON body for an `unassigned_mode: catchall_file` entry.
/// `target` of `None` means "default residual target", which the
/// materializer resolves to `residual/unhandled`.
pub fn unassigned_mode_catchall_file(target: Option<&str>) -> Value {
    match target {
        Some(target) => serde_json::json!({ "kind": "catchall_file", "target": target }),
        None => serde_json::json!({ "kind": "catchall_file" }),
    }
}

/// Build the JSON body for an `unassigned_mode: mini_factors` entry.
pub fn unassigned_mode_mini_factors() -> Value {
    serde_json::json!({ "kind": "mini_factors" })
}

/// Fluent wrapper for building a `(chunk, ChunkExportPurity)` tuple with the
/// member-level and fluent surfaces populated.
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

    pub fn build(self) -> (&'static str, spec::ChunkExportPurity) {
        (self.chunk, self.purity)
    }
}

pub struct Fixture {
    pub chunk_id: String,
    pub entry_path: PathBuf,
    pub out_root: PathBuf,
    pub report_root: PathBuf,
    /// The debundler's stderr from the successful run, for asserting
    /// on warnings/notices (e.g. admission-override notices).
    pub stderr: String,
    // Held to keep the tempdir alive for the duration of assertions.
    _root: TempDir,
}

pub struct RejectedFixture {
    pub chunk_id: String,
    pub stderr: String,
    pub report_root: PathBuf,
    /// The `write_js_tree` output root, exposed so dry-run callers can
    /// assert the no-output contract (only `reports/` may exist).
    pub out_root: PathBuf,
    // Held to keep the tempdir alive for the duration of assertions.
    _root: TempDir,
}

impl Fixture {
    pub fn owner_graph(&self) -> OwnerGraphReport {
        read_owner_graph(&self.report_root, &self.chunk_id)
    }
}

impl RejectedFixture {
    pub fn owner_graph(&self) -> OwnerGraphReport {
        read_owner_graph(&self.report_root, &self.chunk_id)
    }
}

fn read_owner_graph(report_root: &Path, chunk_id: &str) -> OwnerGraphReport {
    read_json(&report_root.join(chunk_id).join("owner_graph.json"))
}

pub struct DryRunFixture {
    pub stderr: String,
    pub report_root: PathBuf,
    // Held to keep the tempdir alive for the duration of assertions.
    _root: TempDir,
}

pub fn run_fixture(opts: FixtureOpts<'_>) -> Fixture {
    let setup = setup_fixture(&opts);
    let spec_path = setup.root.path().join("transform_spec.yaml");
    let spec = build_spec(&opts, &setup);
    write_yaml_file(&spec_path, &spec);

    let result = spawn_transform(&spec_path);
    assert!(
        result.status.success(),
        "debundler exited {:?}\nstdout:\n{}\nstderr:\n{}",
        result.status.code(),
        result.stdout,
        result.stderr,
    );

    let app_root = setup.out_root.join("app");

    // Mirror `extra_files` into app_root after the transform runs, so
    // re-imports emitted by the materializer can resolve through
    // their relative paths under the runtime app tree.
    for (rel_path, content) in opts.extra_files {
        write_text_file(&app_root.join(rel_path), content);
    }

    let entry_path = app_root
        .join(opts.chunk_id.split('/').collect::<PathBuf>())
        .join("entry.js");
    Fixture {
        chunk_id: opts.chunk_id.to_string(),
        entry_path,
        out_root: app_root,
        report_root: setup.report_root,
        stderr: result.stderr,
        _root: setup.root,
    }
}

/// Run the materializer over `opts` and assert it rejects the spec
/// with stderr containing at least one of `error_substring_alternatives`
/// (case-insensitive). Use this helper when the rejection's exact
/// wording isn't pinned — e.g. when several rejection paths converge
/// on the same outcome and the caller is fine with any of them.
///
/// For tests that need to assert *specific evidence* in the error
/// (e.g. "the cycle report names mod_a AND mod_b"), use
/// [`expect_rejection_containing_all`] instead.
pub fn expect_rejection(opts: FixtureOpts<'_>, error_substring_alternatives: &[&str]) {
    let rejected = run_rejection_fixture(opts);
    let stderr = rejected.stderr;
    let stderr_lower = stderr.to_lowercase();
    assert!(
        error_substring_alternatives
            .iter()
            .any(|s| stderr_lower.contains(&s.to_lowercase())),
        "stderr did not contain any of {error_substring_alternatives:?}\nstderr:\n{stderr}",
    );
}

/// Stricter sibling of [`expect_rejection`]: the
/// stderr must contain **every** substring in `required_substrings`,
/// not just one. Use when the test's contract is that the error
/// names specific evidence (every module in a cycle, every binding
/// in a collision, etc.); a generic-but-empty error wouldn't pass
/// the contract.
pub fn expect_rejection_containing_all(opts: FixtureOpts<'_>, required_substrings: &[&str]) {
    let rejected = run_rejection_fixture(opts);
    let stderr = rejected.stderr;
    let stderr_lower = stderr.to_lowercase();
    let missing: Vec<&str> = required_substrings
        .iter()
        .copied()
        .filter(|s| !stderr_lower.contains(&s.to_lowercase()))
        .collect();
    assert!(
        missing.is_empty(),
        "stderr missing required substrings {missing:?}\nstderr:\n{stderr}",
    );
}

pub fn run_rejection_fixture(opts: FixtureOpts<'_>) -> RejectedFixture {
    run_rejection_fixture_with_args(opts, &[])
}

/// Like [`run_rejection_fixture`] but with `debundle run --dry-run`.
/// Dry-run skips all emitted-JS and accept-path report writes but
/// still materializes rejection evidence (`owner_graph.json` +
/// `cycles.json` / `atomic_unit_conflicts.json`) under the standard
/// per-chunk report layout.
pub fn run_dry_run_rejection_fixture(opts: FixtureOpts<'_>) -> RejectedFixture {
    run_rejection_fixture_with_args(opts, &["--dry-run"])
}

/// Like [`run_dry_run_rejection_fixture`] but opts out of the default
/// keep-going diagnostics and stops at the first supported failure.
fn run_fail_fast_dry_run_rejection_fixture(opts: FixtureOpts<'_>) -> RejectedFixture {
    run_rejection_fixture_with_args(opts, &["--dry-run", "--fail-fast"])
}

/// Runs `fixture` keep-going and with `--fail-fast`, both dry. Keep-going
/// reports every outcome, at least two; fail-fast fails with exactly one of
/// those lines, of `kind`, and no other. Returns that line.
pub fn assert_fail_fast_stops_at_first_outcome<'a>(
    fixture: impl Fn() -> FixtureOpts<'a>,
    kind: &str,
) -> String {
    let keep_going = run_dry_run_rejection_fixture(fixture());
    let reported = keep_going
        .stderr
        .lines()
        .filter_map(|line| line.strip_prefix("  - ["))
        .map(|line| format!("[{line}"))
        .collect::<Vec<_>>();
    let recorded = read_selector_outcomes(&keep_going.report_root);
    assert_eq!(
        reported.len(),
        recorded.len(),
        "keep-going must print every recorded outcome\nstderr:\n{}\nrecorded: {recorded:#?}",
        keep_going.stderr
    );
    assert!(
        reported.len() >= 2,
        "the fixture needs outcomes after the first: {reported:#?}"
    );

    let fail_fast = run_fail_fast_dry_run_rejection_fixture(fixture());
    let stopped_at = reported
        .iter()
        .filter(|line| fail_fast.stderr.contains(line.as_str()))
        .collect::<Vec<_>>();
    let [line] = stopped_at[..] else {
        panic!(
            "fail-fast must report exactly one outcome, got {stopped_at:#?}\nstderr:\n{}",
            fail_fast.stderr
        );
    };
    assert!(
        line.starts_with(&format!("[{kind}] ")),
        "fail-fast stopped at {line:?}, expected a {kind} outcome"
    );
    assert!(
        !fail_fast.stderr.contains("Selector outcome report"),
        "fail-fast printed the keep-going report:\n{}",
        fail_fast.stderr
    );
    line.clone()
}

/// Run `debundle run --dry-run` over `opts` and assert it succeeds. The report
/// root holds whatever the pass still writes on success, such as selector
/// warnings in `selector_diagnostics.json`.
pub fn run_dry_run_fixture(opts: FixtureOpts<'_>) -> DryRunFixture {
    let setup = setup_fixture(&opts);
    let spec_path = setup.root.path().join("transform_spec.yaml");
    write_yaml_file(&spec_path, &build_spec(&opts, &setup));
    let result = spawn_transform_with_args(&spec_path, &["--dry-run"]);
    assert!(
        result.status.success(),
        "debundler exited {:?}\nstdout:\n{}\nstderr:\n{}",
        result.status.code(),
        result.stdout,
        result.stderr,
    );
    DryRunFixture {
        stderr: result.stderr,
        report_root: setup.report_root,
        _root: setup.root,
    }
}

fn run_rejection_fixture_with_args(opts: FixtureOpts<'_>, extra_args: &[&str]) -> RejectedFixture {
    let setup = setup_fixture(&opts);
    let spec_path = setup.root.path().join("transform_spec.yaml");
    let spec = build_spec(&opts, &setup);
    write_yaml_file(&spec_path, &spec);

    let result = spawn_transform_with_args(&spec_path, extra_args);
    assert!(
        !result.status.success(),
        "expected spec to be rejected\nstdout:\n{}\nstderr:\n{}",
        result.stdout,
        result.stderr,
    );
    RejectedFixture {
        chunk_id: opts.chunk_id.to_string(),
        stderr: result.stderr,
        report_root: setup.report_root,
        out_root: setup.out_root,
        _root: setup.root,
    }
}

/// A written transform spec ready to feed to a `debundle` subcommand other
/// than `run` (e.g. `spec validate`). The held [`TempDir`] keeps the spec,
/// source snapshot, and js-list alive for the duration of the test.
pub struct ValidateFixture {
    pub spec_path: PathBuf,
    _root: TempDir,
}

/// Materialize `opts` into an on-disk transform spec without running the
/// pipeline. Lets a CLI test point `debundle spec validate --spec <path>` at
/// exactly the same fixture shape the keep-going materialize tests build.
pub fn write_validate_fixture_spec(opts: FixtureOpts<'_>) -> ValidateFixture {
    let setup = setup_fixture(&opts);
    let spec_path = setup.root.path().join("transform_spec.yaml");
    let spec = build_spec(&opts, &setup);
    write_yaml_file(&spec_path, &spec);
    ValidateFixture {
        spec_path,
        _root: setup.root,
    }
}

/// `debundle spec validate --spec --format json` over `opts`, parsed; panics on a
/// non-zero exit.
pub fn validate_json(opts: FixtureOpts<'_>) -> Value {
    let fixture = write_validate_fixture_spec(opts);
    let out = run_spec_validate(&fixture.spec_path, &["--format", "json"]);
    assert!(
        out.status.success(),
        "spec validate exited non-zero: stderr={}",
        out.stderr
    );
    serde_json::from_str(&out.stdout)
        .unwrap_or_else(|err| panic!("parse validate json: {err}\nstdout:\n{}", out.stdout))
}

/// The `outcomes` array of a selector-outcome report.
pub fn outcomes(report: &Value) -> &[Value] {
    report["outcomes"]
        .as_array()
        .unwrap_or_else(|| panic!("outcomes must be an array: {report:#}"))
}

/// Run `debundle spec validate --spec <path> <extra_args>` and return its
/// captured stdio + exit status.
pub fn run_spec_validate(spec_path: &Path, extra_args: &[&str]) -> CommandResult {
    let bin = debundler_path();
    command_result(
        Command::new(&bin)
            .args(["spec", "validate", "--spec"])
            .arg(spec_path)
            .args(extra_args)
            .output()
            .unwrap_or_else(|e| panic!("spawn debundler {}: {e}", bin.display())),
    )
}

pub fn assert_entry_output(fixture: &Fixture, expected_stdout: &str) {
    assert_node_output(&fixture.entry_path, expected_stdout, "");
}

/// Run `node --check` against every emitted JavaScript file in a fixture.
pub fn assert_all_emitted_js_checks(fixture: &Fixture) {
    fn visit(dir: &Path, files: &mut Vec<PathBuf>) {
        for entry in fs::read_dir(dir).unwrap_or_else(|err| panic!("read {}: {err}", dir.display()))
        {
            let entry = entry.expect("read directory entry");
            let path = entry.path();
            if path.is_dir() {
                visit(&path, files);
            } else if path.extension().is_some_and(|extension| extension == "js") {
                files.push(path);
            }
        }
    }
    let mut files = Vec::new();
    visit(&fixture.out_root, &mut files);
    files.sort();
    let node = node_path();
    for file in files {
        let output = Command::new(&node)
            .arg("--check")
            .arg(&file)
            .output()
            .unwrap_or_else(|err| panic!("spawn node --check {}: {err}", file.display()));
        assert!(
            output.status.success(),
            "node --check failed for {}\nstdout:\n{}\nstderr:\n{}",
            file.display(),
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr),
        );
    }
}

pub fn list_module_exports(out_root: &Path, module_path: &str) -> Vec<String> {
    let counter = MODULE_EXPORT_PROBE_COUNTER.fetch_add(1, Ordering::Relaxed);
    let probe_path = out_root.join(format!("__probe_module_exports_{counter}.mjs"));
    let probe = format!(
        "const mod = await import({});\nprocess.stdout.write(JSON.stringify(Object.keys(mod)));\n",
        serde_json::to_string(&format!("./{module_path}")).unwrap(),
    );
    fs::write(&probe_path, probe).unwrap();
    let result = run_node_script(&probe_path);
    assert!(
        result.status.success(),
        "probing {} exited {:?}\nstderr:\n{}",
        module_path,
        result.status.code(),
        result.stderr,
    );
    serde_json::from_str(&result.stdout).expect("probe must emit JSON array")
}

pub fn assert_module_exports(
    out_root: &Path,
    module_path: &str,
    includes: &[&str],
    excludes: &[&str],
) {
    let exported: BTreeSet<String> = list_module_exports(out_root, module_path)
        .into_iter()
        .collect();
    let summary = if exported.is_empty() {
        "<none>".to_string()
    } else {
        exported.iter().cloned().collect::<Vec<_>>().join(", ")
    };
    for name in includes {
        assert!(
            exported.contains(*name),
            "expected {module_path} to export {name}; actual exports: {summary}",
        );
    }
    for name in excludes {
        assert!(
            !exported.contains(*name),
            "expected {module_path} to not export {name}; actual exports: {summary}",
        );
    }
}

pub fn assert_module_source(
    out_root: &Path,
    module_path: &str,
    contains: &[&str],
    does_not_contain: &[&str],
) {
    let code = fs::read_to_string(out_root.join(module_path))
        .unwrap_or_else(|e| panic!("read {module_path}: {e}"));
    for needle in contains {
        assert!(
            code.contains(*needle),
            "{module_path} did not contain {needle:?}\n--- {module_path} ---\n{code}",
        );
    }
    for needle in does_not_contain {
        assert!(
            !code.contains(*needle),
            "{module_path} unexpectedly contained {needle:?}\n--- {module_path} ---\n{code}",
        );
    }
}

/// Asserts that the one line of `module_path` starting with `below_prefix`
/// is immediately preceded by the line `above` (e.g. a `// comment` line).
pub fn assert_line_directly_above(
    out_root: &Path,
    module_path: &str,
    above: &str,
    below_prefix: &str,
) {
    let code = fs::read_to_string(out_root.join(module_path))
        .unwrap_or_else(|e| panic!("read {module_path}: {e}"));
    let lines: Vec<&str> = code.lines().collect();
    let below_indices: Vec<usize> = lines
        .iter()
        .enumerate()
        .filter(|(_, line)| line.starts_with(below_prefix))
        .map(|(index, _)| index)
        .collect();
    let [below_index] = below_indices[..] else {
        panic!(
            "expected exactly one line starting with {below_prefix:?} in {module_path}, found {}\n--- {module_path} ---\n{code}",
            below_indices.len(),
        );
    };
    assert!(
        below_index > 0 && lines[below_index - 1] == above,
        "expected {above:?} directly above {below_prefix:?} in {module_path}\n--- {module_path} ---\n{code}",
    );
}

pub fn assert_pure_cycle_break(
    source: &str,
    logical_modules: Vec<LogicalModuleEntry>,
    module_path: &str,
    contains: &[&str],
    does_not_contain: &[&str],
    expected_stdout: &str,
) -> Fixture {
    assert_pure_cycle_break_with_opts(
        FixtureOpts::new(source, logical_modules),
        module_path,
        contains,
        does_not_contain,
        expected_stdout,
    )
}

pub fn assert_pure_cycle_break_with_opts(
    opts: FixtureOpts<'_>,
    module_path: &str,
    contains: &[&str],
    does_not_contain: &[&str],
    expected_stdout: &str,
) -> Fixture {
    let fixture = run_fixture(opts);
    assert_module_source(
        &fixture.out_root,
        &format!("{}/modules/{module_path}.js", fixture.chunk_id),
        contains,
        does_not_contain,
    );
    assert_entry_output(&fixture, expected_stdout);
    fixture
}

pub fn expect_pure_cycle_rejection(opts: FixtureOpts<'_>, module_path: &str) {
    expect_rejection_containing_all(opts, &["cycle", module_path, "residual"]);
}

pub fn assert_file_ends_with_single_newline(out_root: &Path, module_path: &str) {
    let code = fs::read_to_string(out_root.join(module_path))
        .unwrap_or_else(|e| panic!("read {module_path}: {e}"));
    let terminal_newlines = code
        .as_bytes()
        .iter()
        .rev()
        .take_while(|&&byte| byte == b'\n')
        .count();
    assert_eq!(
        terminal_newlines, 1,
        "{module_path} must end with exactly one newline:\n{code:?}",
    );
}

fn assert_generated_module_script(out_root: &Path, source: &str, expected_stdout: &str) {
    let counter = GENERATED_MODULE_SCRIPT_COUNTER.fetch_add(1, Ordering::Relaxed);
    let assertion_path = out_root.join(format!("assert_generated_module_{counter}.mjs"));
    fs::write(&assertion_path, source).unwrap();
    assert_node_output(&assertion_path, expected_stdout, "");
}

pub fn assert_generated_module_after_entry_script(
    fixture: &Fixture,
    source: &str,
    expected_stdout: &str,
) {
    let entry_specifier = format!("./{}/entry.js", fixture.chunk_id);
    // Silences the entry's own console.log (the entry already executes its
    // top-level effect) before running the caller-supplied probe script.
    let wrapped = format!(
        "const __log = console.log;\n\
         console.log = () => {{}};\n\
         await import({});\n\
         console.log = __log;\n\
         {source}",
        serde_json::to_string(&entry_specifier).unwrap(),
    );
    assert_generated_module_script(&fixture.out_root, &wrapped, expected_stdout);
}

/// Append a marker print to each listed emitted module file, run the
/// entry under Node, and return the order in which the instrumented
/// module bodies finished evaluating — the observable ECMA-262
/// Phase-2 evaluation post-order (docs/design.md "Lemma 1").
///
/// `modules` are logical-module paths under the chunk's `modules/`
/// directory; the special name `"entry"` resolves to the chunk's
/// `entry.js` — the ESM DFS root, which hosts residual's unclaimed
/// anonymous statements and corresponds to the gate simulator's
/// residual node. The instrumentation happens after the debundler
/// has run, so it perturbs neither the analyzed graph nor the
/// realizability verdict — but it does mutate the emitted files, so
/// run `assert_entry_output`-style checks before calling this.
pub fn node_module_evaluation_order(fixture: &Fixture, modules: &[&str]) -> Vec<String> {
    const MARKER: &str = "__module_eval__:";
    for label in modules {
        let rel_path = if *label == "entry" {
            format!("{}/entry.js", fixture.chunk_id)
        } else {
            format!("{}/modules/{label}.js", fixture.chunk_id)
        };
        let path = fixture.out_root.join(&rel_path);
        let mut code = fs::read_to_string(&path)
            .unwrap_or_else(|err| panic!("read emitted module {rel_path}: {err}"));
        code.push_str(&format!("\nconsole.log(\"{MARKER}{label}\");\n"));
        fs::write(&path, code).unwrap();
    }
    let result = run_node_script(&fixture.entry_path);
    assert!(
        result.status.success(),
        "node {} exited {:?}\nstdout:\n{}\nstderr:\n{}",
        fixture.entry_path.display(),
        result.status.code(),
        result.stdout,
        result.stderr,
    );
    result
        .stdout
        .lines()
        .filter_map(|line| line.strip_prefix(MARKER))
        .map(str::to_string)
        .collect()
}

pub fn assert_node_output(path: &Path, expected_stdout: &str, expected_stderr: &str) {
    let result = run_node_script(path);
    assert!(
        result.status.success(),
        "node {} exited {:?}\nstdout:\n{}\nstderr:\n{}",
        path.display(),
        result.status.code(),
        result.stdout,
        result.stderr,
    );
    assert_eq!(result.stdout, expected_stdout, "stdout mismatch");
    assert_eq!(result.stderr, expected_stderr, "stderr mismatch");
}

struct FixtureSetup {
    root: TempDir,
    out_root: PathBuf,
    report_root: PathBuf,
    snapshot_root: PathBuf,
    js_list_path: PathBuf,
}

fn setup_fixture(opts: &FixtureOpts<'_>) -> FixtureSetup {
    let root = TempDir::with_prefix(current_test_prefix()).expect("create tempdir");
    let extracted_root = root.path().join("extracted");
    let out_root = root.path().join("out");
    let report_root = out_root.join("reports").join("tree");
    let snapshot_root = root.path().join("snapshot");
    fs::create_dir_all(&extracted_root).unwrap();
    fs::create_dir_all(&out_root).unwrap();
    fs::create_dir_all(&snapshot_root).unwrap();

    // Mark the snapshot tree as ESM so node loads emitted .js files as modules.
    write_text_file(
        &snapshot_root.join("package.json"),
        &format!(
            "{}\n",
            serde_json::to_string_pretty(&PackageManifest {
                module_type: "module"
            })
            .unwrap()
        ),
    );

    let entry_file = format!("{}.js", opts.chunk_id);
    write_text_file(&snapshot_root.join(&entry_file), opts.source);
    for (rel_path, content) in opts.extra_files {
        write_text_file(&snapshot_root.join(rel_path), content);
    }

    // `extra_chunks` are listed in js-files.txt so they are parsed and
    // analyzed (their exports feed the cross-module oracle), unlike
    // `extra_files` which are runtime-only siblings.
    let mut js_list = format!("{entry_file}\n");
    for (chunk_id, source) in opts.extra_chunks {
        let chunk_file = format!("{chunk_id}.js");
        write_text_file(&snapshot_root.join(&chunk_file), source);
        js_list.push_str(&chunk_file);
        js_list.push('\n');
    }
    let js_list_path = extracted_root.join("js-files.txt");
    write_text_file(&js_list_path, &js_list);

    FixtureSetup {
        root,
        out_root,
        report_root,
        snapshot_root,
        js_list_path,
    }
}

fn build_spec(opts: &FixtureOpts<'_>, setup: &FixtureSetup) -> TransformSpec {
    let chunk_id = opts.chunk_id;
    let mut logical_modules = BTreeMap::new();
    if !opts.logical_modules.is_empty() {
        let for_chunk = opts
            .logical_modules
            .iter()
            .map(|(path, body)| {
                (
                    path.clone(),
                    serde_json::from_value(body.clone()).expect(
                        "logical module fixture body deserializes into spec::LogicalModule",
                    ),
                )
            })
            .collect();
        logical_modules.insert(chunk_id.to_string(), for_chunk);
    }
    for (extra_chunk, modules) in opts.extra_chunk_logical_modules {
        let for_chunk = modules
            .iter()
            .map(|(path, body)| {
                (
                    path.clone(),
                    serde_json::from_value(body.clone()).expect(
                        "extra chunk logical module fixture body deserializes into spec::LogicalModule",
                    ),
                )
            })
            .collect();
        logical_modules.insert((*extra_chunk).to_string(), for_chunk);
    }

    let mut chunk_renames = BTreeMap::new();
    if let Some(renames) = &opts.chunk_renames {
        chunk_renames.insert(
            chunk_id.to_string(),
            serde_json::from_value(renames.clone())
                .expect("chunk_renames fixture deserializes into spec::ChunkRenames"),
        );
    }

    let mut unassigned_mode = BTreeMap::new();
    unassigned_mode.insert(
        chunk_id.to_string(),
        serde_json::from_value(opts.unassigned_mode.clone())
            .expect("unassigned_mode fixture deserializes into spec::UnassignedMode"),
    );
    for (extra_chunk, _) in opts.extra_chunk_logical_modules {
        unassigned_mode.insert(
            (*extra_chunk).to_string(),
            serde_json::from_value(unassigned_mode_catchall_file(None))
                .expect("unassigned_mode fixture deserializes into spec::UnassignedMode"),
        );
    }

    let chunk_analysis_options = if opts.dataflow_aware_s_chain
        || opts.trusted_dataflow_summaries
        || opts.local_property_effects
        || !opts.admission_overrides.is_empty()
    {
        let mut analysis = serde_json::Map::new();
        if opts.dataflow_aware_s_chain {
            analysis.insert("dataflow_aware_s_chain".to_string(), Value::Bool(true));
        }
        if opts.trusted_dataflow_summaries {
            analysis.insert("trusted_dataflow_summaries".to_string(), Value::Bool(true));
        }
        if opts.local_property_effects {
            analysis.insert("local_property_effects".to_string(), Value::Bool(true));
        }
        if !opts.admission_overrides.is_empty() {
            analysis.insert(
                "admission_overrides".to_string(),
                serde_json::json!(opts.admission_overrides),
            );
        }
        let mut map = BTreeMap::new();
        map.insert(
            chunk_id.to_string(),
            serde_json::from_value(Value::Object(analysis))
                .expect("analysis options deserialize into spec::OwnerGraphOptions"),
        );
        map
    } else {
        BTreeMap::new()
    };

    let chunk_export_purity: BTreeMap<String, spec::ChunkExportPurity> = opts
        .chunk_export_purity
        .iter()
        .map(|(chunk, assertion)| ((*chunk).to_string(), assertion.clone()))
        .collect();

    TransformSpec {
        inputs: LoadJsChunksArgs {
            input_root: setup.snapshot_root.clone(),
            js_list_path: setup.js_list_path.clone(),
        },
        vendor: BTreeMap::new(),
        logical_modules,
        chunk_renames,
        unassigned_mode,
        chunk_analysis_options,
        chunk_export_purity,
        swap_vendor_chunks: SwapVendorChunksConfig::default(),
        materialize_logical_modules: MaterializeLogicalModulesConfig {
            prune_other_chunks: false,
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

/// Slugified test path for the current `#[test]` thread, used as the
/// tempdir prefix so failed runs are easy to pick out of `/tmp` without
/// each test having to repeat its own name.
fn current_test_prefix() -> String {
    let thread = std::thread::current();
    let slug: String = thread
        .name()
        .unwrap_or("unknown")
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() {
                c.to_ascii_lowercase()
            } else {
                '-'
            }
        })
        .collect();
    let compact = slug
        .split('-')
        .filter(|part| !part.is_empty())
        .collect::<Vec<_>>()
        .join("-");
    format!("debundle-e2e-{compact}-")
}

pub fn write_text_file(path: &Path, content: &str) {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).unwrap();
    }
    fs::write(path, content).unwrap();
}

/// Parse a CLI invocation's stdout as JSON, panicking with both stdio streams
/// on failure so a non-JSON (e.g. error) stdout is legible in the test log.
pub fn parse_stdout_json(out: &std::process::Output) -> Value {
    serde_json::from_slice(&out.stdout).unwrap_or_else(|err| {
        panic!(
            "stdout is not JSON ({err})\nstdout:\n{}\nstderr:\n{}",
            String::from_utf8_lossy(&out.stdout),
            String::from_utf8_lossy(&out.stderr),
        )
    })
}

/// Run `debundle <args>` and return its raw output.
pub fn run_debundle(args: &[&str]) -> std::process::Output {
    Command::new(debundler_path())
        .args(args)
        .output()
        .expect("spawn debundle")
}

/// Run `debundle spec synthesize-selectors --modules <dir> [extra...]`, asserting
/// success and returning the raw output for the caller to parse.
pub fn run_synthesize_selectors(modules: &Path, extra: &[&str]) -> std::process::Output {
    let mut args = vec![
        "spec",
        "synthesize-selectors",
        "--modules",
        modules.to_str().unwrap(),
    ];
    args.extend_from_slice(extra);
    let out = run_debundle(&args);
    assert!(
        out.status.success(),
        "non-zero exit\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
    out
}

/// A single-member chunk-renames spec mapping the binding `from_binding` to the
/// exported name `rename_to`.
pub fn chunk_rename(rename_to: &str, from_binding: &str) -> Value {
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
pub fn chunk_renames(entries: &[ChunkRenameEntry]) -> Value {
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
    serde_json::to_value(ChunkRenames {
        members,
        annotations: BTreeMap::new(),
    })
    .expect("chunk renames fixture must serialize")
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
) -> Value {
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
    serde_json::to_value(ChunkRenames {
        members,
        annotations,
    })
    .expect("chunk renames fixture must serialize")
}

/// Owner-graph node id that declares `binding`, panicking (with the node dump)
/// if none does.
pub fn owner_for_binding<'a>(graph: &'a OwnerGraphReport, binding: &str) -> &'a str {
    let node = graph
        .nodes
        .iter()
        .find(|node| node.declared_bindings.iter().any(|b| b.binding == binding))
        .unwrap_or_else(|| {
            panic!(
                "no owner-graph node declares binding `{binding}`; \
                 nodes: {:#?}",
                graph.nodes,
            )
        });
    node.id.as_str()
}

/// An `owner_graph.json` node for the statement `ordinal`, declaring `binding`
/// (exported under its own name) and destined for the module `destination`.
pub fn owner_node(id: &str, ordinal: usize, binding: &str, destination: &str) -> Value {
    serde_json::json!({
        "id": id,
        "statement_ordinal": ordinal,
        "declared_bindings": [ { "binding": binding, "export_name": binding } ],
        "statement_kind": "var_decl",
        "purity": { "kind": "pure" },
        "destination": destination
    })
}

/// An `owner_graph.json` edge of `edge_kind` (`eager_use`, `eager_rebind`) that
/// constrains init order: `source` depends on `binding` of `target`.
pub fn owner_edge(
    id: &str,
    edge_kind: &str,
    source: &str,
    target: &str,
    binding: &str,
    ordinal: usize,
) -> Value {
    serde_json::json!({
        "id": id,
        "source": source,
        "target": target,
        "edge_kind": edge_kind,
        "binding": binding,
        "statement_ordinal": ordinal,
        "constrains_init_order": true
    })
}

/// An `owner_graph.json` body over `nodes` and `edges`, with empty module and
/// atomic graphs.
pub fn owner_graph(chunk_id: &str, nodes: Vec<Value>, edges: Vec<Value>) -> Value {
    serde_json::json!({
        "chunk_id": chunk_id,
        "nodes": nodes,
        "edges": edges,
        "module_graph": { "nodes": [], "edges": [], "sccs": [] },
        "atomic_graph": { "nodes": [], "edges": [] }
    })
}

/// Owner graph for a two-statement atomic unit: `alpha` and `beta` mutually
/// `eager_rebind` each other and share destination `home/atom`, so the
/// realizability gate must keep them co-located.
pub fn graph_with_atomic_unit() -> String {
    owner_graph(
        "test/chunk",
        vec![
            owner_node("owner:0", 0, "alpha", "home/atom"),
            owner_node("owner:1", 1, "beta", "home/atom"),
        ],
        vec![
            owner_edge(
                "owner_edge:0",
                "eager_rebind",
                "owner:0",
                "owner:1",
                "beta",
                0,
            ),
            owner_edge(
                "owner_edge:1",
                "eager_rebind",
                "owner:1",
                "owner:0",
                "alpha",
                1,
            ),
        ],
    )
    .to_string()
}

/// Owner graph for an acyclic cross-module read: `alpha` (module `a`)
/// `eager_use`s `beta` (module `b`) with no back edge, so the split is
/// realizable.
pub fn graph_with_acyclic_cross_module_read() -> String {
    owner_graph(
        "test/chunk",
        vec![
            owner_node("owner:0", 0, "alpha", "a"),
            owner_node("owner:1", 1, "beta", "b"),
        ],
        vec![owner_edge(
            "owner_edge:0",
            "eager_use",
            "owner:0",
            "owner:1",
            "beta",
            0,
        )],
    )
    .to_string()
}

/// Owner graph with three owners — alpha (module `a`), beta (`b`), gamma (`c`) —
/// and the `eager_use` chain alpha → gamma → beta: a DAG quotient `a → c → b`
/// that closes into the 2-cycle `m ↔ c` when `a` and `b` merge into `m`.
pub fn graph_with_merge_cycle_potential() -> String {
    owner_graph(
        "test/chunk",
        vec![
            owner_node("owner:0", 0, "alpha", "a"),
            owner_node("owner:1", 1, "beta", "b"),
            owner_node("owner:2", 2, "gamma", "c"),
        ],
        vec![
            owner_edge(
                "owner_edge:0",
                "eager_use",
                "owner:0",
                "owner:2",
                "gamma",
                0,
            ),
            owner_edge("owner_edge:1", "eager_use", "owner:2", "owner:1", "beta", 2),
        ],
    )
    .to_string()
}

/// Write [`graph_with_atomic_unit`] plus a single module co-locating `alpha`
/// and `beta`, returning `(modules_dir, owner_graph_path)`.
pub fn write_atomic_unit_fixture(root: &Path) -> (PathBuf, PathBuf) {
    let modules = root.join("modules");
    let graph = root.join("owner_graph.json");
    write_text_file(&graph, &graph_with_atomic_unit());
    // Pre-edit: alpha + beta co-located in one module — atom
    // respected, realizable.
    write_text_file(
        &modules.join("home/atom.yaml"),
        "members:\n  - selector: { binding: { name: alpha } }\n  - selector: { binding: { name: beta } }\n",
    );
    (modules, graph)
}

pub fn write_yaml_file<T: Serialize + ?Sized>(path: &Path, value: &T) {
    // `serde_json` is built with `arbitrary_precision` workspace-wide (feature
    // unification), which makes `serde_yaml` emit a `serde_json::Value::Number`
    // as a map — so a spec carrying a number (e.g. `passed_to_call.arg_index`)
    // round-trips as garbage. Serialize to JSON first (serde_json serializes its
    // own arbitrary-precision numbers correctly), then re-emit as YAML. JSON is a
    // YAML subset, so the debundler's serde_yaml reader parses it unchanged.
    let json = serde_json::to_string(value).expect("serialize spec to JSON");
    let yaml: serde_yaml::Value = serde_yaml::from_str(&json).expect("reparse JSON as YAML");
    fs::write(path, format!("{}\n", serde_yaml::to_string(&yaml).unwrap())).unwrap();
}

pub fn read_json<T: DeserializeOwned>(path: &Path) -> T {
    serde_json::from_str(
        &fs::read_to_string(path)
            .unwrap_or_else(|err| panic!("read JSON report {}: {err}", path.display())),
    )
    .unwrap_or_else(|err| panic!("parse JSON report {}: {err}", path.display()))
}

pub fn debundler_path() -> PathBuf {
    let r = Runfiles::create().expect("create runfiles");
    rlocation!(r, DEBUNDLER_RLOCATION)
        .unwrap_or_else(|| panic!("could not resolve debundler runfile: {DEBUNDLER_RLOCATION}"))
}

/// `debundle` as a release download runs it: a copy of the binary alone in an
/// otherwise empty directory, spawned with an empty environment (no runfiles).
pub struct StandaloneDebundle {
    dir: TempDir,
}

impl StandaloneDebundle {
    pub fn install() -> Self {
        let dir = TempDir::with_prefix(current_test_prefix()).expect("create install dir");
        fs::copy(debundler_path(), dir.path().join("debundle")).expect("copy debundle binary");
        Self { dir }
    }

    pub fn run(&self, args: &[&str]) -> CommandResult {
        let bin = self.dir.path().join("debundle");
        let output = Command::new(&bin)
            .args(args)
            .env_clear()
            .output()
            .unwrap_or_else(|e| panic!("spawn debundle {}: {e}", bin.display()));
        command_result(output)
    }
}

fn node_path() -> PathBuf {
    let r = Runfiles::create().expect("create runfiles");
    rlocation!(r, NODE_RLOCATION)
        .unwrap_or_else(|| panic!("could not resolve node runfile: {NODE_RLOCATION}"))
}

pub struct CommandResult {
    pub stdout: String,
    pub stderr: String,
    pub status: std::process::ExitStatus,
}

fn command_result(output: std::process::Output) -> CommandResult {
    CommandResult {
        stdout: String::from_utf8_lossy(&output.stdout).into_owned(),
        stderr: String::from_utf8_lossy(&output.stderr).into_owned(),
        status: output.status,
    }
}

/// `debundle spec validate --modules <modules_root> --source-file <source_file>`:
/// the source-only preflight.
pub fn run_source_only_validate(
    modules_root: &Path,
    source_file: &Path,
    extra_args: &[&str],
) -> CommandResult {
    let bin = debundler_path();
    let output = Command::new(&bin)
        .args(["spec", "validate", "--modules"])
        .arg(modules_root)
        .arg("--source-file")
        .arg(source_file)
        .args(extra_args)
        .output()
        .unwrap_or_else(|e| panic!("spawn debundler {}: {e}", bin.display()));
    command_result(output)
}

/// `debundle spec match-selector --source-file <source_file> --match <selector>
/// --format json`, parsed; panics on a non-zero exit.
pub fn run_match_selector(source_file: &Path, selector: &str, extra_args: &[&str]) -> Value {
    let bin = debundler_path();
    let result = command_result(
        Command::new(&bin)
            .args(["spec", "match-selector", "--source-file"])
            .arg(source_file)
            .args(["--match", selector, "--format", "json"])
            .args(extra_args)
            .output()
            .unwrap_or_else(|e| panic!("spawn debundler {}: {e}", bin.display())),
    );
    assert!(
        result.status.success(),
        "match-selector exited {:?}\nstdout:\n{}\nstderr:\n{}",
        result.status.code(),
        result.stdout,
        result.stderr,
    );
    serde_json::from_str(&result.stdout).unwrap_or_else(|e| {
        panic!(
            "match-selector stdout is not JSON ({e}):\n{}",
            result.stdout
        )
    })
}

/// The `outcomes` of the `static/app` chunk's `selector_diagnostics.json`
/// under a `debundle run --dry-run` report root.
pub fn read_selector_outcomes(report_root: &Path) -> Vec<Value> {
    read_chunk_selector_outcomes(report_root, "static/app")
}

/// The `outcomes` of `chunk`'s `selector_diagnostics.json` under a report root.
pub fn read_chunk_selector_outcomes(report_root: &Path, chunk: &str) -> Vec<Value> {
    let report: Value = read_json(&report_root.join(chunk).join("selector_diagnostics.json"));
    report["outcomes"]
        .as_array()
        .unwrap_or_else(|| panic!("outcomes must be an array: {report:#}"))
        .clone()
}

/// The outcome record of `kind` whose entity is the export `export_name`.
pub fn find_outcome<'a>(outcomes: &'a [Value], kind: &str, export_name: &str) -> &'a Value {
    outcomes
        .iter()
        .find(|record| {
            record["outcome"]["kind"] == kind
                && record["placement"]["entity"]["export"] == export_name
        })
        .unwrap_or_else(|| panic!("missing {kind} outcome for export {export_name}: {outcomes:#?}"))
}

fn spawn_transform(spec_path: &Path) -> CommandResult {
    run_debundler(spec_path, &[])
}

fn spawn_transform_with_args(spec_path: &Path, extra_args: &[&str]) -> CommandResult {
    let bin = debundler_path();
    command_result(
        Command::new(&bin)
            .arg("run")
            .arg("--spec")
            .arg(spec_path)
            .args(extra_args)
            .output()
            .unwrap_or_else(|e| panic!("spawn debundler {}: {e}", bin.display())),
    )
}

/// Run `debundle run --spec <path> [--package-root <name>=<dir> ...]` and return its
/// captured stdio + exit status. Used by tests that exercise pipeline stages
/// outside the logical-modules harness in [`run_fixture`].
pub fn run_debundler(spec_path: &Path, package_roots: &[(&str, &Path)]) -> CommandResult {
    let bin = debundler_path();
    let mut command = Command::new(&bin);
    command.arg("run").arg("--spec").arg(spec_path);
    for (name, dir) in package_roots {
        command
            .arg("--package-root")
            .arg(format!("{name}={}", dir.display()));
    }
    command_result(
        command
            .output()
            .unwrap_or_else(|e| panic!("spawn debundler {}: {e}", bin.display())),
    )
}

/// A tree-authored spec over several chunks: `chunks` are `(chunk id,
/// source)`, `module_roots` are `(tree root, chunk id)`, and `modules` are
/// `(path below the modules root, module YAML)`. Every chunk is inlined into
/// its entry when unassigned; the first chunk is `main_chunk_id`.
pub struct TreeFixture<'a> {
    pub chunks: &'a [(&'a str, &'a str)],
    pub module_roots: &'a [(&'a str, &'a str)],
    pub modules: &'a [(&'a str, &'a str)],
}

pub struct TreeRun {
    _root: TempDir,
    pub out_root: PathBuf,
    pub report_root: PathBuf,
    pub result: CommandResult,
}

/// Writes `fixture` and runs `debundle run` on it in tree form with
/// `extra_args`, writing the JS tree.
pub fn run_tree_fixture(fixture: &TreeFixture<'_>, extra_args: &[&str]) -> TreeRun {
    let root = TempDir::with_prefix(current_test_prefix()).expect("create tempdir");
    let snapshot = root.path().join("snapshot");
    let modules = root.path().join("modules");
    let out_root = root.path().join("out");
    let mut js_list = String::new();
    let mut unassigned_mode = String::new();
    for (chunk, source) in fixture.chunks {
        write_text_file(&snapshot.join(format!("{chunk}.js")), source);
        js_list.push_str(&format!("{chunk}.js\n"));
        unassigned_mode.push_str(&format!("  {chunk}: {{ kind: inline_in_entry }}\n"));
    }
    write_text_file(&root.path().join("js-files.txt"), &js_list);
    let module_roots = fixture
        .module_roots
        .iter()
        .map(|(tree, chunk)| format!("  {tree}: {chunk}\n"))
        .collect::<String>();
    for (path, body) in fixture.modules {
        write_text_file(&modules.join(path), body);
    }
    let config = root.path().join("spec_config.yaml");
    write_text_file(
        &config,
        &format!(
            "main_chunk_id: {}\nmodule_roots:\n{module_roots}inputs:\n  root: snapshot\n  \
             js_list_path: js-files.txt\nwrite_js_tree: true\nunassigned_mode:\n{unassigned_mode}",
            fixture.chunks[0].0,
        ),
    );
    let vendor_marks = root.path().join("vendor_marks.yaml");
    write_text_file(&vendor_marks, "vendor_marks: []\n");
    let bin = debundler_path();
    let output = Command::new(&bin)
        .arg("run")
        .arg("--tree-config")
        .arg(&config)
        .arg("--tree-modules")
        .arg(&modules)
        .arg("--tree-vendor-marks")
        .arg(&vendor_marks)
        .arg("--tree-source-root")
        .arg(root.path())
        .arg("--out-root")
        .arg(&out_root)
        .args(extra_args)
        .output()
        .unwrap_or_else(|e| panic!("spawn debundler {}: {e}", bin.display()));
    TreeRun {
        report_root: out_root.join("reports").join("tree"),
        out_root,
        result: command_result(output),
        _root: root,
    }
}

fn run_node_script(path: &Path) -> CommandResult {
    let node = node_path();
    command_result(
        Command::new(&node)
            .arg(path)
            .output()
            .unwrap_or_else(|e| panic!("spawn node {}: {e}", node.display())),
    )
}

/// Parse `source` as an ESM module and return the SWC AST. Tests use this
/// when the substring-on-emit checks aren't precise enough — e.g. when
/// they need to walk specifiers to disambiguate `aH$1 as aH` (correct)
/// from `aH$1 as aH$1` (corrupt).
pub fn parse_module(source: &str) -> Module {
    let cm: Lrc<swc_common::SourceMap> = Default::default();
    let fm = cm.new_source_file(
        FileName::Custom("entry.js".into()).into(),
        source.to_string(),
    );
    let lexer = Lexer::new(
        Syntax::Typescript(TsSyntax {
            tsx: true,
            decorators: true,
            no_early_errors: true,
            ..Default::default()
        }),
        Default::default(),
        StringInput::from(&*fm),
        None,
    );
    Parser::new_from(lexer)
        .parse_module()
        .unwrap_or_else(|err| panic!("entry must parse, got {err:?}; source:\n{source}"))
}

fn declared_bindings_in_source_match(source: &str) -> Vec<String> {
    let module = parse_module(source);
    let mut names = Vec::new();
    for item in &module.body {
        match item {
            ModuleItem::Stmt(Stmt::Decl(decl)) => {
                collect_declared_bindings_from_decl(decl, &mut names)
            }
            ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => {
                collect_declared_bindings_from_decl(&export.decl, &mut names)
            }
            _ => {}
        }
    }
    names
        .into_iter()
        .filter(|name| !is_declarator_list_hole_name(name))
        .collect()
}

fn is_declarator_list_hole_name(name: &str) -> bool {
    name == "DECLARATORS" || name.starts_with("DECLARATORS_")
}

fn collect_declared_bindings_from_decl(decl: &Decl, names: &mut Vec<String>) {
    match decl {
        Decl::Class(class) => names.push(class.ident.sym.to_string()),
        Decl::Fn(function) => names.push(function.ident.sym.to_string()),
        Decl::Var(var) => {
            for declarator in &var.decls {
                collect_declared_bindings_from_pat(&declarator.name, names);
            }
        }
        _ => {}
    }
}

fn collect_declared_bindings_from_pat(pat: &Pat, names: &mut Vec<String>) {
    match pat {
        Pat::Ident(ident) => names.push(ident.id.sym.to_string()),
        Pat::Array(array) => {
            for elem in array.elems.iter().flatten() {
                collect_declared_bindings_from_pat(elem, names);
            }
        }
        Pat::Rest(rest) => collect_declared_bindings_from_pat(&rest.arg, names),
        Pat::Object(object) => {
            for prop in &object.props {
                match prop {
                    ObjectPatProp::KeyValue(kv) => {
                        collect_declared_bindings_from_pat(&kv.value, names)
                    }
                    ObjectPatProp::Assign(assign) => names.push(assign.key.sym.to_string()),
                    ObjectPatProp::Rest(rest) => {
                        collect_declared_bindings_from_pat(&rest.arg, names)
                    }
                }
            }
        }
        Pat::Assign(assign) => collect_declared_bindings_from_pat(&assign.left, names),
        Pat::Expr(_) | Pat::Invalid(_) => {}
    }
}

/// Parse `source` and assert the named export specifiers for `expected_orig` match
/// the supplied set of exported names. `None` means the `as` clause is absent.
/// Walking the parsed specifier tree rejects near-matches such as
/// `export { aH$1 as aH$1 }` that substring assertions can accept.
pub fn assert_export_named_specifiers(
    source: &str,
    expected_orig: &str,
    expected_exported_as: &[Option<&str>],
) {
    let module = parse_module(source);
    let matched: Vec<_> = module
        .body
        .iter()
        .filter_map(|item| match item {
            ModuleItem::ModuleDecl(ModuleDecl::ExportNamed(named)) => Some(named),
            _ => None,
        })
        .flat_map(|named| named.specifiers.iter())
        .filter_map(|spec| match spec {
            ExportSpecifier::Named(named) => Some(named),
            _ => None,
        })
        .filter(|spec| {
            let ModuleExportName::Ident(ident) = &spec.orig else {
                return false;
            };
            ident.sym.as_ref() == expected_orig
        })
        .collect();
    assert_eq!(
        matched.len(),
        expected_exported_as.len(),
        "expected {} `export {{ {expected_orig} ... }}` specifiers; got {} in:\n{source}",
        expected_exported_as.len(),
        matched.len(),
    );
    let mut actual: Vec<Option<String>> = matched
        .iter()
        .map(|spec| match &spec.exported {
            Some(ModuleExportName::Ident(ident)) => Some(ident.sym.to_string()),
            Some(ModuleExportName::Str(_)) => panic!("unexpected string export in:\n{source}"),
            None => None,
        })
        .collect();
    let mut expected: Vec<Option<String>> = expected_exported_as
        .iter()
        .map(|name| name.map(str::to_owned))
        .collect();
    actual.sort();
    expected.sort();
    assert_eq!(
        actual, expected,
        "export {{ {expected_orig} ... }} `as` clauses mismatch in:\n{source}",
    );
}

/// Assert exactly one `export { ... }` specifier has the requested shape.
pub fn assert_export_named_specifier(
    source: &str,
    expected_orig: &str,
    expected_exported_as: Option<&str>,
) {
    assert_export_named_specifiers(source, expected_orig, &[expected_exported_as]);
}
