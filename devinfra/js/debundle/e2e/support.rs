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
    BindingIdent, BlockStmtOrExpr, Decl, ExportSpecifier, Expr, FnDecl, Function, ImportSpecifier,
    Module, ModuleDecl, ModuleExportName, ModuleItem, ObjectPatProp, Pat, Stmt, VarDeclKind,
    VarDeclarator,
};
use swc_ecma_parser::{Parser, StringInput, Syntax, TsSyntax, lexer::Lexer};
use tempfile::TempDir;

const DEBUNDLER_RLOCATION: &str = "_main/devinfra/js/debundle/debundle";
const NODE_RLOCATION: &str = "nodejs_linux_amd64/bin/node";

static MODULE_EXPORT_PROBE_COUNTER: AtomicUsize = AtomicUsize::new(0);
static GENERATED_MODULE_SCRIPT_COUNTER: AtomicUsize = AtomicUsize::new(0);

/// One member of a [`LogicalModuleEntry`].
///
/// `name` is the exported name in the materialized module; `binding` is the
/// original top-level binding to extract. When `binding` is `None`, the
/// exported name and the original binding are the same.
pub struct Member {
    pub name: &'static str,
    pub binding: Option<&'static str>,
    binding_kind: Option<BindingSourceKind>,
    source_match: Option<SourceMatch>,
    cross_ref: Option<CrossRefSelector>,
    reads_member: Option<ReadsMemberSelector>,
    member_of_module: Option<MemberOfModuleSelector>,
    passed_to_call: Option<PassedToCallSelector>,
    makes_decorate_call: Option<MakesDecorateCallSelector>,
    intrinsic_alias: Option<IntrinsicAliasSelector>,
    purity: Option<MemberPurity>,
    effect: Option<MemberEffect>,
    pure_members: Vec<String>,
    no_sync_callback_members: Vec<String>,
    pub comment: Option<String>,
    pub note: Option<String>,
}

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
            adopt_names: None,
            exports: exports.iter().copied().collect(),
            comments: BTreeMap::new(),
            notes: BTreeMap::new(),
        }
    }

    pub fn source_alpha_adopt_all(match_source: impl Into<String>) -> Self {
        Self {
            match_source: match_source.into(),
            adopt_names: Some(FixtureAdoptNames::All(true)),
            exports: BTreeMap::new(),
            comments: BTreeMap::new(),
            notes: BTreeMap::new(),
        }
    }

    pub fn source_alpha_adopt_names(
        match_source: impl Into<String>,
        names: &[&'static str],
    ) -> Self {
        Self {
            match_source: match_source.into(),
            adopt_names: Some(FixtureAdoptNames::Names(names.to_vec())),
            exports: BTreeMap::new(),
            comments: BTreeMap::new(),
            notes: BTreeMap::new(),
        }
    }

    pub fn source_alpha_adopt_all_with_exports(
        match_source: impl Into<String>,
        exports: &[(&'static str, &'static str)],
    ) -> Self {
        Self {
            match_source: match_source.into(),
            adopt_names: Some(FixtureAdoptNames::All(true)),
            exports: exports.iter().copied().collect(),
            comments: BTreeMap::new(),
            notes: BTreeMap::new(),
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
    /// Whether any selector (source-match or a relational form) is set, so the
    /// fixture binding should not default to `name`.
    fn has_selector(&self) -> bool {
        self.source_match.is_some()
            || self.cross_ref.is_some()
            || self.reads_member.is_some()
            || self.member_of_module.is_some()
            || self.passed_to_call.is_some()
            || self.makes_decorate_call.is_some()
            || self.intrinsic_alias.is_some()
    }

    /// Extract a binding under its original name.
    pub fn new(name: &'static str) -> Self {
        Self::new_with_kind(name, None)
    }

    /// Like [`Self::new`] but narrows the binding selector to a specific
    /// source-declaration kind (`"import_specifier"`, `"class_declaration"`,
    /// `"function_declaration"`, `"variable_declarator"`).
    pub fn new_with_kind(name: &'static str, kind: Option<&'static str>) -> Self {
        Self {
            name,
            binding: None,
            binding_kind: parse_kind(kind),
            source_match: None,
            cross_ref: None,
            reads_member: None,
            member_of_module: None,
            passed_to_call: None,
            makes_decorate_call: None,
            intrinsic_alias: None,
            purity: None,
            effect: None,
            pure_members: Vec::new(),
            no_sync_callback_members: Vec::new(),
            comment: None,
            note: None,
        }
    }

    /// Extract `binding` and re-export it as `name`.
    pub fn renamed(name: &'static str, binding: &'static str) -> Self {
        Self::renamed_with_kind(name, binding, None)
    }

    /// Like [`Self::renamed`] but narrows the binding selector to a specific
    /// source-declaration kind.
    pub fn renamed_with_kind(
        name: &'static str,
        binding: &'static str,
        kind: Option<&'static str>,
    ) -> Self {
        Self {
            name,
            binding: Some(binding),
            binding_kind: parse_kind(kind),
            source_match: None,
            cross_ref: None,
            reads_member: None,
            member_of_module: None,
            passed_to_call: None,
            makes_decorate_call: None,
            intrinsic_alias: None,
            purity: None,
            effect: None,
            pure_members: Vec::new(),
            no_sync_callback_members: Vec::new(),
            comment: None,
            note: None,
        }
    }

    /// Extract a top-level single-binding declaration selected by source shape
    /// rather than by its current minified binding name.
    pub fn source_alpha(name: &'static str, match_source: impl Into<String>) -> Self {
        Self {
            name,
            binding: None,
            binding_kind: None,
            source_match: Some(SourceMatch {
                identifiers: SourceMatchIdentifierMode::AlphaAll,
                target_binding: None,
                match_source: match_source.into(),
            }),
            cross_ref: None,
            reads_member: None,
            member_of_module: None,
            passed_to_call: None,
            makes_decorate_call: None,
            intrinsic_alias: None,
            purity: None,
            effect: None,
            pure_members: Vec::new(),
            no_sync_callback_members: Vec::new(),
            comment: None,
            note: None,
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
            binding: None,
            binding_kind: None,
            source_match: Some(SourceMatch {
                identifiers: SourceMatchIdentifierMode::AlphaAll,
                target_binding: Some(target_binding.into()),
                match_source: match_source.into(),
            }),
            cross_ref: None,
            reads_member: None,
            member_of_module: None,
            passed_to_call: None,
            makes_decorate_call: None,
            intrinsic_alias: None,
            purity: None,
            effect: None,
            pure_members: Vec::new(),
            no_sync_callback_members: Vec::new(),
            comment: None,
            note: None,
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
        Self {
            name,
            binding: None,
            binding_kind: None,
            source_match: None,
            cross_ref: Some(CrossRefSelector {
                references: Some(anchor.to_string()),
                aliases: None,
                kind: parse_kind(kind),
            }),
            reads_member: None,
            member_of_module: None,
            passed_to_call: None,
            makes_decorate_call: None,
            intrinsic_alias: None,
            purity: None,
            effect: None,
            pure_members: Vec::new(),
            no_sync_callback_members: Vec::new(),
            comment: None,
            note: None,
        }
    }

    /// Pin a member as the var-decl that **aliases** the anchor member
    /// (`const T = @anchor`), re-exported under `name`.
    pub fn cross_ref_aliases(name: &'static str, anchor: &'static str) -> Self {
        Self {
            name,
            binding: None,
            binding_kind: None,
            source_match: None,
            cross_ref: Some(CrossRefSelector {
                references: None,
                aliases: Some(anchor.to_string()),
                kind: None,
            }),
            reads_member: None,
            member_of_module: None,
            passed_to_call: None,
            makes_decorate_call: None,
            intrinsic_alias: None,
            purity: None,
            effect: None,
            pure_members: Vec::new(),
            no_sync_callback_members: Vec::new(),
            comment: None,
            note: None,
        }
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
        Self {
            name,
            binding: None,
            binding_kind: None,
            source_match: None,
            cross_ref: None,
            reads_member: Some(ReadsMemberSelector {
                member: member.to_string(),
                object: object.map(str::to_string),
                kind: parse_kind(kind),
            }),
            member_of_module: None,
            passed_to_call: None,
            makes_decorate_call: None,
            intrinsic_alias: None,
            purity: None,
            effect: None,
            pure_members: Vec::new(),
            no_sync_callback_members: Vec::new(),
            comment: None,
            note: None,
        }
    }

    /// Pin a member as the entity **consumed as `module.member`** at a use site
    /// (`module` an import specifier, `member` an export name), re-exported under
    /// `name`. The first use-site selector — pins by how the entity is consumed,
    /// not its own body or minified name. `kind` optionally narrows to one
    /// source-declaration kind (`class_declaration`, …) when several owners
    /// consume the module member.
    pub fn member_of_module(
        name: &'static str,
        module: &'static str,
        member: &'static str,
        kind: Option<&'static str>,
    ) -> Self {
        Self {
            name,
            binding: None,
            binding_kind: None,
            source_match: None,
            cross_ref: None,
            reads_member: None,
            member_of_module: Some(MemberOfModuleSelector {
                module: module.to_string(),
                member: member.to_string(),
                kind: parse_kind(kind),
            }),
            passed_to_call: None,
            makes_decorate_call: None,
            intrinsic_alias: None,
            purity: None,
            effect: None,
            pure_members: Vec::new(),
            no_sync_callback_members: Vec::new(),
            comment: None,
            note: None,
        }
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
        Self {
            name,
            binding: None,
            binding_kind: None,
            source_match: None,
            cross_ref: None,
            reads_member: None,
            member_of_module: None,
            passed_to_call: Some(PassedToCallSelector {
                callee_member: callee_member.to_string(),
                object: object.map(str::to_string),
                arg_index,
                kind: parse_kind(kind),
            }),
            makes_decorate_call: None,
            intrinsic_alias: None,
            purity: None,
            effect: None,
            pure_members: Vec::new(),
            no_sync_callback_members: Vec::new(),
            comment: None,
            note: None,
        }
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
        Self {
            name,
            binding: None,
            binding_kind: None,
            source_match: None,
            cross_ref: None,
            reads_member: None,
            member_of_module: None,
            passed_to_call: None,
            makes_decorate_call: Some(MakesDecorateCallSelector {
                class: class.to_string(),
                member: member.map(str::to_string),
                kind: parse_kind(kind),
            }),
            intrinsic_alias: None,
            purity: None,
            effect: None,
            pure_members: Vec::new(),
            no_sync_callback_members: Vec::new(),
            comment: None,
            note: None,
        }
    }

    /// Pin a member as an **intrinsic-method alias off the unshadowed global
    /// `Object`** (`var X = Object.<property>`) referenced by a known helper —
    /// "the `Object.<property>` alias the `@referenced_by` helper reads" —
    /// re-exported under `name`. The follow-on companion of `makes_decorate_call`:
    /// pins the byte-identical esbuild decorate-companion copies (which make no
    /// decorator call of their own) by the helper that reads them, not by their own
    /// minified name.
    pub fn intrinsic_alias(
        name: &'static str,
        property: &'static str,
        referenced_by: &'static str,
    ) -> Self {
        Self {
            name,
            binding: None,
            binding_kind: None,
            source_match: None,
            cross_ref: None,
            reads_member: None,
            member_of_module: None,
            passed_to_call: None,
            makes_decorate_call: None,
            intrinsic_alias: Some(IntrinsicAliasSelector {
                property: property.to_string(),
                referenced_by: referenced_by.to_string(),
            }),
            purity: None,
            effect: None,
            pure_members: Vec::new(),
            no_sync_callback_members: Vec::new(),
            comment: None,
            note: None,
        }
    }

    /// Attach an author comment to be emitted above the binding's owner
    /// statement in the lowered module body. See `spec::Member::comment`.
    pub fn with_comment(mut self, comment: impl Into<String>) -> Self {
        self.comment = Some(comment.into());
        self
    }

    /// Attach a YAML-only author note. Unlike [`Self::with_comment`], this is
    /// preserved in the spec but never emitted into generated JavaScript.
    pub fn with_note(mut self, note: impl Into<String>) -> Self {
        self.note = Some(note.into());
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

    /// Attach `pure_members` to the binding's annotation. See
    /// `spec::BindingAnnotation::pure_members`.
    pub fn with_pure_members(mut self, pure_members: Vec<String>) -> Self {
        self.pure_members = pure_members;
        self
    }

    /// Attach `no_sync_callback_members` to the binding's annotation. See
    /// `spec::BindingAnnotation::no_sync_callback_members`.
    pub fn with_no_sync_callback_members(mut self, members: Vec<String>) -> Self {
        self.no_sync_callback_members = members;
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
/// `::exact` / `::alpha_all` / `.with_comment` / `.with_note` builder spelling.
/// Serialization is delegated to the wrapped spec type.
#[derive(Clone, Serialize)]
#[serde(transparent)]
struct FixtureAnonymousStatement(AnonymousStatement);

/// Internal-only (never serialized) selector for `fixture_grouped_source_matches`
/// describing which bindings a `BindingGroup` adopts from its matched source.
#[derive(Clone)]
enum FixtureAdoptNames {
    All(bool),
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

    fn with_note(mut self, note: impl Into<String>) -> Self {
        self.0.note = Some(note.into());
        self
    }
}

fn fixture_members(members: &[Member]) -> Vec<SpecMember> {
    members
        .iter()
        .filter(|m| m.source_match.is_none())
        .map(|m| SpecMember {
            name: Some(m.name.to_string()),
            selector: MemberSelector {
                binding: m
                    .binding
                    .or_else(|| (!m.has_selector()).then_some(m.name))
                    .map(|name| BindingSelector {
                        name: name.to_string(),
                        kind: m.binding_kind,
                    }),
                cross_ref: m.cross_ref.clone(),
                reads_member: m.reads_member.clone(),
                member_of_module: m.member_of_module.clone(),
                passed_to_call: m.passed_to_call.clone(),
                makes_decorate_call: m.makes_decorate_call.clone(),
                intrinsic_alias: m.intrinsic_alias.clone(),
            },
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
                Some(FixtureAdoptNames::All(true)) => {
                    declared_bindings_in_source_match(&group.match_source)
                }
                Some(FixtureAdoptNames::All(false)) => Vec::new(),
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
        let has_annotation = member.comment.is_some()
            || member.note.is_some()
            || member.purity.is_some()
            || member.effect.is_some()
            || !member.pure_members.is_empty()
            || !member.no_sync_callback_members.is_empty();
        if has_annotation {
            annotations.insert(
                member.name.to_string(),
                BindingAnnotation {
                    purity: member.purity.unwrap_or_default(),
                    effect: member.effect.unwrap_or_default(),
                    pure_members: member.pure_members.clone(),
                    no_sync_callback_members: member.no_sync_callback_members.clone(),
                    comment: member.comment.clone(),
                    note: member.note.clone(),
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
    logical_module_entry_with_note(
        path,
        members,
        binding_groups,
        anonymous_statements,
        comment,
        None,
    )
}

fn logical_module_entry_with_note(
    path: &str,
    members: &[Member],
    binding_groups: &[BindingGroup],
    anonymous_statements: Vec<FixtureAnonymousStatement>,
    comment: Option<String>,
    note: Option<String>,
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
            note,
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

pub fn logical_module_with_source_matches(
    path: &str,
    members: &[Member],
    source_matches: &[BindingGroup],
) -> LogicalModuleEntry {
    logical_module_entry(path, members, source_matches, Vec::new(), None)
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

/// Like [`logical_module`] but attaches a module-level YAML-only `note:` block.
pub fn logical_module_with_note(
    path: &str,
    members: &[Member],
    note: impl Into<String>,
) -> LogicalModuleEntry {
    logical_module_entry_with_note(path, members, &[], Vec::new(), None, Some(note.into()))
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
    anon_match: &str,
) -> LogicalModuleEntry {
    logical_module_entry(
        path,
        members,
        &[],
        vec![FixtureAnonymousStatement::alpha_all(anon_match)],
        None,
    )
}

pub fn logical_module_with_anon_alpha_many(
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

pub fn logical_module_with_anon_note(
    path: &str,
    members: &[Member],
    anon_match: &str,
    note: impl Into<String>,
) -> LogicalModuleEntry {
    logical_module_entry(
        path,
        members,
        &[],
        vec![FixtureAnonymousStatement::exact(anon_match).with_note(note)],
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
    /// Opt into the dataflow-aware S-chain emission in `graph.rs` for
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
    /// `chunk_export_purity` entries as `(defining chunk_id, assertion)`,
    /// built via [`chunk_export_purity`] / [`ChunkExportPurityBuilder`]. Default
    /// empty.
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

/// Build a `spec::ChunkExportPurity` author assertion for the defining chunk
/// `chunk`, starting from `pure_exports`. Chain `.with_pure_members(...)` /
/// `.with_fluent_exports(...)` for the other assertion surfaces. See
/// `spec::ChunkExportPurity`.
pub fn chunk_export_purity(
    chunk: &'static str,
    pure_exports: &[&str],
) -> (&'static str, spec::ChunkExportPurity) {
    (
        chunk,
        spec::ChunkExportPurity {
            pure_exports: pure_exports.iter().map(|s| (*s).to_string()).collect(),
            ..Default::default()
        },
    )
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

    /// Assert the listed export names are pure (see `spec::ChunkExportPurity::pure_exports`).
    pub fn with_pure_exports(mut self, exports: &[&str]) -> Self {
        self.purity.pure_exports = exports.iter().map(|s| (*s).to_string()).collect();
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
    pub stderr: String,
    pub report_root: PathBuf,
    /// The `write_js_tree` output root, exposed so dry-run callers can
    /// assert the no-output contract (only `reports/` may exist).
    pub out_root: PathBuf,
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
pub fn run_fail_fast_dry_run_rejection_fixture(opts: FixtureOpts<'_>) -> RejectedFixture {
    run_rejection_fixture_with_args(opts, &["--dry-run", "--fail-fast"])
}

/// Compatibility spelling for tests that need to document the old explicit
/// flag; keep-going is now the default for broad pipeline runs.
pub fn run_keep_going_dry_run_rejection_fixture(opts: FixtureOpts<'_>) -> RejectedFixture {
    run_rejection_fixture_with_args(opts, &["--dry-run", "--keep-going"])
}

fn run_rejection_fixture_with_args(opts: FixtureOpts<'_>, extra_args: &[&str]) -> RejectedFixture {
    run_rejection_fixture_with_args_and_env(opts, extra_args, &[])
}

fn run_rejection_fixture_with_args_and_env(
    opts: FixtureOpts<'_>,
    extra_args: &[&str],
    env: &[(&str, &str)],
) -> RejectedFixture {
    let setup = setup_fixture(&opts);
    let spec_path = setup.root.path().join("transform_spec.yaml");
    let spec = build_spec(&opts, &setup);
    write_yaml_file(&spec_path, &spec);

    let result = spawn_transform_with_args(&spec_path, extra_args, env);
    assert!(
        !result.status.success(),
        "expected spec to be rejected\nstdout:\n{}\nstderr:\n{}",
        result.stdout,
        result.stderr,
    );
    RejectedFixture {
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

/// Run `debundle spec validate --spec <path> <extra_args>` and return its
/// captured stdio + exit status.
pub fn run_spec_validate(spec_path: &Path, extra_args: &[&str]) -> CommandResult {
    let bin = debundler_path();
    let mut command = Command::new(&bin);
    command
        .arg("spec")
        .arg("validate")
        .arg("--spec")
        .arg(spec_path);
    command.args(extra_args);
    let output = command
        .output()
        .unwrap_or_else(|e| panic!("spawn debundler {}: {e}", bin.display()));
    CommandResult {
        stdout: String::from_utf8_lossy(&output.stdout).into_owned(),
        stderr: String::from_utf8_lossy(&output.stderr).into_owned(),
        status: output.status,
    }
}

pub fn assert_entry_output(fixture: &Fixture, expected_stdout: &str) {
    assert_node_output(&fixture.entry_path, expected_stdout, "");
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
    let exported: std::collections::BTreeSet<String> = list_module_exports(out_root, module_path)
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

pub fn assert_generated_module_script(out_root: &Path, source: &str, expected_stdout: &str) {
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

// ---------------------------------------------------------------------------
// Internals
// ---------------------------------------------------------------------------

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
    let name = thread.name().unwrap_or("unknown");
    let slug: String = name
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() {
                c.to_ascii_lowercase()
            } else {
                '-'
            }
        })
        .collect();
    let trimmed = slug.trim_matches('-');
    let mut compact = String::with_capacity(trimmed.len());
    let mut prev_dash = false;
    for c in trimmed.chars() {
        if c == '-' {
            if !prev_dash {
                compact.push(c);
            }
            prev_dash = true;
        } else {
            compact.push(c);
            prev_dash = false;
        }
    }
    format!("debundle-e2e-{compact}-")
}

pub fn write_text_file(path: &Path, content: &str) {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).unwrap();
    }
    fs::write(path, content).unwrap();
}

/// Write `body` to `root`-relative path `rel`, creating parent dirs. The
/// root-relative convenience over [`write_text_file`] for CLI tests that
/// scatter several fixture files under one temp root.
pub fn write_file(root: &Path, rel: &str, body: &str) {
    write_text_file(&root.join(rel), body);
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
    let out = Command::new(debundler_path())
        .args(&args)
        .output()
        .expect("spawn debundle");
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

/// Owner graph for a two-statement atomic unit: `alpha` and `beta` mutually
/// `eager_rebind` each other and share destination `home/atom`, so the
/// realizability gate must keep them co-located.
pub fn graph_with_atomic_unit() -> String {
    serde_json::json!({
        "chunk_id": "test/chunk",
        "nodes": [
            {
                "id": "owner:0",
                "statement_ordinal": 0,
                "declared_bindings": [
                    { "binding": "alpha", "export_name": "alpha" }
                ],
                "statement_kind": "var_decl",
                "purity": { "kind": "pure" },
                "destination": "home/atom"
            },
            {
                "id": "owner:1",
                "statement_ordinal": 1,
                "declared_bindings": [
                    { "binding": "beta", "export_name": "beta" }
                ],
                "statement_kind": "var_decl",
                "purity": { "kind": "pure" },
                "destination": "home/atom"
            }
        ],
        "edges": [
            {
                "id": "owner_edge:0",
                "source": "owner:0",
                "target": "owner:1",
                "edge_kind": "eager_rebind",
                "binding": "beta",
                "statement_ordinal": 0,
                "constrains_init_order": true
            },
            {
                "id": "owner_edge:1",
                "source": "owner:1",
                "target": "owner:0",
                "edge_kind": "eager_rebind",
                "binding": "alpha",
                "statement_ordinal": 1,
                "constrains_init_order": true
            }
        ],
        "module_graph": { "nodes": [], "edges": [], "sccs": [] },
        "atomic_graph": { "nodes": [], "edges": [] }
    })
    .to_string()
}

/// Owner graph for an acyclic cross-module read: `alpha` (module `a`)
/// `eager_use`s `beta` (module `b`) with no back edge, so the split is
/// realizable.
pub fn graph_with_acyclic_cross_module_read() -> String {
    serde_json::json!({
        "chunk_id": "test/chunk",
        "nodes": [
            {
                "id": "owner:0",
                "statement_ordinal": 0,
                "declared_bindings": [
                    { "binding": "alpha", "export_name": "alpha" }
                ],
                "statement_kind": "var_decl",
                "purity": { "kind": "pure" },
                "destination": "a"
            },
            {
                "id": "owner:1",
                "statement_ordinal": 1,
                "declared_bindings": [
                    { "binding": "beta", "export_name": "beta" }
                ],
                "statement_kind": "var_decl",
                "purity": { "kind": "pure" },
                "destination": "b"
            }
        ],
        "edges": [
            {
                "id": "owner_edge:0",
                "source": "owner:0",
                "target": "owner:1",
                "edge_kind": "eager_use",
                "binding": "beta",
                "statement_ordinal": 0,
                "constrains_init_order": true
            }
        ],
        "module_graph": { "nodes": [], "edges": [], "sccs": [] },
        "atomic_graph": { "nodes": [], "edges": [] }
    })
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

fn spawn_transform(spec_path: &Path) -> CommandResult {
    run_debundler(spec_path, &[])
}

fn spawn_transform_with_args(
    spec_path: &Path,
    extra_args: &[&str],
    env: &[(&str, &str)],
) -> CommandResult {
    let bin = debundler_path();
    let mut command = Command::new(&bin);
    command.arg("run").arg("--spec").arg(spec_path);
    command.args(extra_args);
    for (name, value) in env {
        command.env(name, value);
    }
    let output = command
        .output()
        .unwrap_or_else(|e| panic!("spawn debundler {}: {e}", bin.display()));
    CommandResult {
        stdout: String::from_utf8_lossy(&output.stdout).into_owned(),
        stderr: String::from_utf8_lossy(&output.stderr).into_owned(),
        status: output.status,
    }
}

/// Run `debundle run --spec <path> [--package-root <name>=<dir> ...]` and return its
/// captured stdio + exit status. Used by tests that exercise pipeline stages
/// outside the logical-modules harness in [`run_fixture`].
pub fn run_debundler(spec_path: &Path, package_roots: &[(&str, &Path)]) -> CommandResult {
    run_debundler_with_env(spec_path, package_roots, &[])
}

pub fn run_debundler_with_env(
    spec_path: &Path,
    package_roots: &[(&str, &Path)],
    env: &[(&str, &str)],
) -> CommandResult {
    let bin = debundler_path();
    let mut command = Command::new(&bin);
    command.arg("run").arg("--spec").arg(spec_path);
    for (name, dir) in package_roots {
        command
            .arg("--package-root")
            .arg(format!("{name}={}", dir.display()));
    }
    for (name, value) in env {
        command.env(name, value);
    }
    let output = command
        .output()
        .unwrap_or_else(|e| panic!("spawn debundler {}: {e}", bin.display()));
    CommandResult {
        stdout: String::from_utf8_lossy(&output.stdout).into_owned(),
        stderr: String::from_utf8_lossy(&output.stderr).into_owned(),
        status: output.status,
    }
}

/// Run the tree-authoring form of `debundle run` with explicit environment
/// variables. Keeping the environment on a child process makes diagnostics
/// tests safe when Rust executes test functions concurrently.
pub fn run_debundler_tree_with_env(
    config_path: &Path,
    modules_root: &Path,
    vendor_marks_path: &Path,
    source_root: &Path,
    out_root: &Path,
    env: &[(&str, &str)],
) -> CommandResult {
    let bin = debundler_path();
    let mut command = Command::new(&bin);
    command
        .arg("run")
        .arg("--tree-config")
        .arg(config_path)
        .arg("--tree-modules")
        .arg(modules_root)
        .arg("--tree-vendor-marks")
        .arg(vendor_marks_path)
        .arg("--tree-source-root")
        .arg(source_root)
        .arg("--out-root")
        .arg(out_root);
    for (name, value) in env {
        command.env(name, value);
    }
    let output = command
        .output()
        .unwrap_or_else(|e| panic!("spawn debundler {}: {e}", bin.display()));
    CommandResult {
        stdout: String::from_utf8_lossy(&output.stdout).into_owned(),
        stderr: String::from_utf8_lossy(&output.stderr).into_owned(),
        status: output.status,
    }
}

fn run_node_script(path: &Path) -> CommandResult {
    let node = node_path();
    let output = Command::new(&node)
        .arg(path)
        .output()
        .unwrap_or_else(|e| panic!("spawn node {}: {e}", node.display()));
    CommandResult {
        stdout: String::from_utf8_lossy(&output.stdout).into_owned(),
        stderr: String::from_utf8_lossy(&output.stderr).into_owned(),
        status: output.status,
    }
}

// --- AST-walking assertion helpers ---------------------------------------

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

/// Parse `source` and assert that every named import specifier binds a
/// distinct local symbol. Mirrors the duplicate-declaration check Node
/// would perform at module-load time.
pub fn assert_unique_import_locals(source: &str) {
    let module = parse_module(source);
    let mut seen = BTreeSet::new();
    for item in &module.body {
        let ModuleItem::ModuleDecl(ModuleDecl::Import(import)) = item else {
            continue;
        };
        for specifier in &import.specifiers {
            let local = match specifier {
                ImportSpecifier::Named(named) => named.local.sym.to_string(),
                ImportSpecifier::Default(default) => default.local.sym.to_string(),
                ImportSpecifier::Namespace(namespace) => namespace.local.sym.to_string(),
            };
            assert!(
                seen.insert(local.clone()),
                "duplicate import local `{local}` in:\n{source}",
            );
        }
    }
}

/// Parse `source` and assert exactly one `export { ... }` specifier has
/// `orig.sym == expected_orig`, with its `exported` either absent (when
/// `expected_exported_as` is `None`) or `Ident { sym: expected_exported_as }`.
/// Walks the parsed specifier tree so a corrupted `export { aH$1 as aH$1 }`
/// fails — a substring check on `aH$1 as aH` would accept both shapes.
pub fn assert_export_named_specifier(
    source: &str,
    expected_orig: &str,
    expected_exported_as: Option<&str>,
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
        1,
        "expected exactly one `export {{ {expected_orig} ... }}` specifier; got {} in:\n{source}",
        matched.len(),
    );
    let actual = match &matched[0].exported {
        Some(ModuleExportName::Ident(ident)) => Some(ident.sym.to_string()),
        Some(ModuleExportName::Str(_)) => panic!("unexpected string export in:\n{source}"),
        None => None,
    };
    assert_eq!(
        actual.as_deref(),
        expected_exported_as,
        "export {{ {expected_orig} ... }} `as` clause mismatch in:\n{source}",
    );
}

/// Assert that no function-body scope in `source` declares `target_name`
/// more than once (counting destructured params and `let`/`const` decls;
/// `var` is excluded because it allows redeclaration in the same scope).
/// Mirrors Node's lexical-binding duplicate check.
pub fn assert_unique_lexical_decls_per_scope(source: &str, target_name: &str) {
    fn pat_binds(pat: &Pat, target: &str) -> bool {
        match pat {
            Pat::Ident(BindingIdent { id, .. }) => id.sym.as_ref() == target,
            Pat::Object(object) => object.props.iter().any(|prop| match prop {
                ObjectPatProp::KeyValue(kv) => pat_binds(&kv.value, target),
                ObjectPatProp::Assign(assign) => assign.key.id.sym.as_ref() == target,
                ObjectPatProp::Rest(rest) => pat_binds(&rest.arg, target),
            }),
            Pat::Array(array) => array
                .elems
                .iter()
                .flatten()
                .any(|elem| pat_binds(elem, target)),
            Pat::Assign(assign) => pat_binds(&assign.left, target),
            Pat::Rest(rest) => pat_binds(&rest.arg, target),
            _ => false,
        }
    }

    fn check_function(function: &Function, target: &str, source: &str) {
        let Some(body) = &function.body else {
            return;
        };
        let mut count = 0;
        for param in &function.params {
            if pat_binds(&param.pat, target) {
                count += 1;
            }
        }
        for stmt in &body.stmts {
            // `var` allows redeclaration in the same scope (and `function f(a){var a;}`
            // is legal); only `let`/`const`/`class`/`function` are subject to the
            // "Identifier 'X' has already been declared" lexical check.
            if let Stmt::Decl(Decl::Var(var)) = stmt
                && matches!(var.kind, VarDeclKind::Let | VarDeclKind::Const)
            {
                for declarator in &var.decls {
                    if pat_binds(&declarator.name, target) {
                        count += 1;
                    }
                }
            }
        }
        assert!(
            count <= 1,
            "scope binds `{target}` {count} times in:\n{source}",
        );
        for stmt in &body.stmts {
            descend_stmt(stmt, target, source);
        }
    }

    fn descend_stmt(stmt: &Stmt, target: &str, source: &str) {
        match stmt {
            Stmt::Decl(Decl::Fn(FnDecl { function, .. })) => {
                check_function(function, target, source)
            }
            Stmt::Decl(Decl::Var(var)) => {
                for VarDeclarator { init, .. } in &var.decls {
                    if let Some(init) = init {
                        descend_expr(init, target, source);
                    }
                }
            }
            Stmt::Block(block) => {
                for stmt in &block.stmts {
                    descend_stmt(stmt, target, source);
                }
            }
            _ => {}
        }
    }

    fn descend_expr(expr: &Expr, target: &str, source: &str) {
        match expr {
            Expr::Fn(fn_expr) => check_function(&fn_expr.function, target, source),
            Expr::Arrow(arrow) => {
                let mut count = 0;
                for param in &arrow.params {
                    if pat_binds(param, target) {
                        count += 1;
                    }
                }
                if let BlockStmtOrExpr::BlockStmt(block) = &*arrow.body {
                    for stmt in &block.stmts {
                        if let Stmt::Decl(Decl::Var(var)) = stmt
                            && matches!(var.kind, VarDeclKind::Let | VarDeclKind::Const)
                        {
                            for declarator in &var.decls {
                                if pat_binds(&declarator.name, target) {
                                    count += 1;
                                }
                            }
                        }
                    }
                }
                assert!(
                    count <= 1,
                    "arrow scope binds `{target}` {count} times in:\n{source}",
                );
            }
            _ => {}
        }
    }

    let module = parse_module(source);
    for item in &module.body {
        if let ModuleItem::Stmt(stmt) = item {
            descend_stmt(stmt, target_name, source);
        }
    }
}
