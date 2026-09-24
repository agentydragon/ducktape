//! Mechanical, reviewable spec selector rewrites.
//!
//! This powers `debundle spec selector-codemod`: a scripting-safe CLI for
//! applying proven selector rewrites across a modules tree. The core
//! source-aware rewrite is framed as: given one or more target entities, come
//! up with the simplest selector/spec fragment that uniquely selects them.
//! This module starts with an indexed subset of that minimization problem:
//! build a per-chunk declaration table and binding-name index, group requested
//! bindings by source declaration, render a structural selector with
//! declarator gaps for non-target siblings, then prove it with the selector
//! resolve every command uses: the selector must resolve on its own selector.

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail};
use selector_outcome::{Outcome, ResolvedBy};
use selector_resolve::{EntityIndex, Member, MemberSelector, SpecModule};
use serde::Serialize;
use serde_yaml::Value;
use shape_index::ShapeIndex;
use source_match::{MemberBindingMatch, ParsedSourceMatchSelector};
use spec::{SourceMatch, SourceMatchIdentifierMode};
use spec_modules::{collect_module_files, is_module_yaml, module_path_from_file};
use swc_common::DUMMY_SP;
use swc_ecma_ast::*;
use swc_ecma_visit::{VisitMut, VisitMutWith};

// Hole keyword spellings come from `source_match_holes` so the minimizer
// emits exactly the tokens the matcher resolves.
use source_match_holes::DECLARATORS_HOLE_KEYWORD;

// The selector minimizer is split by form: the AST-holing engine (`render`),
// the regex-over-string-literal anchors (`regex_anchor`), and the per-form
// minimizers (`minimize::{function,class,object,var,group}`).
mod minimize;
mod regex_anchor;
mod render;

// Read-only agent-facing selector query primitives (M1 of the
// selector-authoring plan), sharing this crate's source loading + prove-gate.
pub mod match_selector;

use crate::minimize::{
    minimize_class_selector_candidates, minimize_function_selector_candidates,
    minimize_var_group_selector, minimize_var_group_selector_candidates,
};
use crate::render::holes_present;

#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum SelectorCodemodRewrite {
    NameBindingToSourceMatch,
}

impl SelectorCodemodRewrite {
    pub fn name(self) -> &'static str {
        match self {
            Self::NameBindingToSourceMatch => "name_binding_to_source_match",
        }
    }
}

#[derive(Debug, Clone)]
pub struct SelectorCodemodConfig {
    pub modules_root: PathBuf,
    pub apply: bool,
    pub rewrite: SelectorCodemodRewrite,
    pub files: Vec<PathBuf>,
    pub modules: Vec<String>,
    pub module_prefixes: Vec<String>,
    pub source_root: Option<PathBuf>,
    pub chunk: Option<PathBuf>,
    pub source_file: Option<PathBuf>,
    pub items: Vec<String>,
    /// Emit up to N ranked candidate selectors per synthesized item (a menu); the
    /// extras beyond the primary pick are reported as `alternatives`. 1 = today's
    /// single-pick behavior.
    pub candidates: usize,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum SelectorCodemodAction {
    WouldChange,
    Changed,
    Skipped,
}

/// One alternative candidate selector for an item, beyond the minimizer's primary
/// pick — the `synthesize-selectors --candidates N` menu. Each proves uniquely (it
/// is a read-off candidate the matcher accepted), pinning a different anchor than
/// the primary; the agent reads them as a menu to override an incidental pick.
#[derive(Debug, Clone, Serialize)]
pub struct SelectorAlternative {
    pub match_source: String,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub rewritten_holes: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct SelectorCodemodCandidate {
    pub module: String,
    pub file: String,
    pub member_index: usize,
    pub export_name: Option<String>,
    pub action: SelectorCodemodAction,
    pub target_binding: Option<String>,
    pub declared_bindings: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub group_id: Option<usize>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub matched_body_index: Option<usize>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub candidate_count: Option<usize>,
    /// The synthesized primary selector's `match` source (synthesize-selectors
    /// only); `None` for non-synthesizing rewrites and skips. Makes the proposed
    /// selector visible in dry-run, and completes the `--candidates` menu (the
    /// primary here, the rest in `alternatives`).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub match_source: Option<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub rewritten_holes: Vec<String>,
    #[serde(default, skip_serializing_if = "is_zero")]
    pub replacement_count: usize,
    /// Extra ranked candidates beyond the primary, when `--candidates N > 1`.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub alternatives: Vec<SelectorAlternative>,
    pub reason: Option<String>,
}

#[derive(Debug, Clone, Default, Serialize)]
pub struct SelectorCodemodSummary {
    pub dry_run: bool,
    pub files_scanned: usize,
    pub modules_scanned: usize,
    pub members_scanned: usize,
    pub name_binding_members: usize,
    pub synthesized_groups: usize,
    pub changed_candidates: usize,
    pub skipped_candidates: usize,
    pub files_written: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct SelectorCodemodReport {
    pub rewrite: SelectorCodemodRewrite,
    pub action: String,
    pub candidates: Vec<SelectorCodemodCandidate>,
    pub summary: SelectorCodemodSummary,
}

pub fn run_selector_codemod(config: &SelectorCodemodConfig) -> Result<SelectorCodemodReport> {
    js_ast::with_swc_globals(|| run_selector_codemod_impl(config))
}

fn run_selector_codemod_impl(config: &SelectorCodemodConfig) -> Result<SelectorCodemodReport> {
    let selected_items = config
        .items
        .iter()
        .map(|item| parse_synthesis_item(item))
        .collect::<Result<BTreeSet<_>>>()?;
    let selected_item_exports = selected_item_exports_by_module(&selected_items);
    let selected_files = config
        .files
        .iter()
        .map(|path| resolve_file_filter(&config.modules_root, path))
        .collect::<BTreeSet<_>>();
    let selected_modules = config.modules.iter().cloned().collect::<BTreeSet<_>>();
    let files = collect_candidate_module_files(
        config,
        &selected_files,
        &selected_modules,
        &selected_item_exports,
    )
    .with_context(|| format!("selecting files under {}", config.modules_root.display()))?;
    let synthesis_module = load_synthesis_module(config)?;
    let synthesis_index = ChunkSelectorIndex::new(&synthesis_module.module);

    let mut candidates = Vec::new();
    let mut summary = SelectorCodemodSummary {
        dry_run: !config.apply,
        ..SelectorCodemodSummary::default()
    };

    for file in files {
        summary.files_scanned += 1;
        let module = module_path_from_file(&file, &config.modules_root);
        if !module_selected(
            &file,
            &module,
            &selected_files,
            &selected_modules,
            &config.module_prefixes,
        ) {
            continue;
        }
        summary.modules_scanned += 1;

        let mut doc = yaml_edit::read_yaml(&file)?;
        let mut file_changed = false;
        let Value::Mapping(root) = &mut doc else {
            candidates.push(skipped_candidate(
                &module,
                &file,
                0,
                None,
                "module YAML is not a mapping",
            ));
            continue;
        };
        match config.rewrite {
            SelectorCodemodRewrite::NameBindingToSourceMatch => {}
        }
        let selected_exports = selected_item_exports.get(&module);
        if !selected_item_exports.is_empty() && selected_exports.is_none() {
            continue;
        }
        let outcomes = rewrite_name_bindings_to_source_match(
            &module,
            &file,
            root,
            &synthesis_index,
            selected_exports,
            NameBindingRewriteOptions {
                apply: config.apply,
                candidates: config.candidates,
            },
        )?;
        summary.members_scanned += outcomes.members_scanned;
        summary.name_binding_members += outcomes.members_seen;
        summary.synthesized_groups += outcomes.groups_changed;
        for candidate in outcomes.candidates {
            if matches!(
                candidate.action,
                SelectorCodemodAction::WouldChange | SelectorCodemodAction::Changed
            ) {
                file_changed = true;
                summary.changed_candidates += 1;
            } else {
                summary.skipped_candidates += 1;
            }
            candidates.push(candidate);
        }

        if config.apply && file_changed && yaml_edit::write_yaml_if_semantic_changed(&file, &doc)? {
            summary.files_written.push(file.display().to_string());
        }
    }

    Ok(SelectorCodemodReport {
        rewrite: config.rewrite,
        action: if config.apply {
            "applied".to_string()
        } else {
            "dry_run".to_string()
        },
        candidates,
        summary,
    })
}

#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd)]
struct SynthesisItem {
    module: String,
    export_name: String,
}

#[derive(Debug)]
struct NameBindingRewriteOutcomes {
    candidates: Vec<SelectorCodemodCandidate>,
    members_scanned: usize,
    members_seen: usize,
    groups_changed: usize,
}

#[derive(Debug, Clone)]
struct NameBindingMember {
    member_index: usize,
    export_name: String,
    binding_name: String,
    annotation_fields: Vec<(&'static str, Value)>,
}

#[derive(Debug, Clone, Copy)]
struct NameBindingRewriteOptions {
    apply: bool,
    candidates: usize,
}

#[derive(Debug, Clone)]
struct MigratedSourceBinding {
    local: String,
    name: String,
}

#[derive(Debug, Clone)]
pub struct SynthesizedSelectorGroup {
    body_idx: usize,
    target_bindings: Vec<SynthesizedTargetBinding>,
    match_source: String,
    rewritten_holes: Vec<String>,
    alternatives: Vec<SelectorAlternative>,
}

#[derive(Debug, Clone)]
pub struct SynthesizedTargetBinding {
    export_name: String,
    runtime_binding: String,
}

/// One synthesized selector together with the members it covers and the
/// representative (first) declaration it was proven against. The anti-unification
/// grouping pass ([`merge_adjacent_same_shape_runs`]) operates on these: a
/// single-declaration group may merge with adjacent same-shape neighbors into a
/// run-based group whose `synthesized` spans several declarations.
#[derive(Debug, Clone)]
struct SynthesizedDeclGroup {
    decl_idx: usize,
    members: Vec<NameBindingMember>,
    synthesized: SynthesizedSelectorGroup,
}

/// Indexed source facts for selector synthesis.
///
/// The target architecture is a trie/lattice of stable AST discriminants:
/// declaration kind, wrapper shape, initializer kind, callee/member paths,
/// object keys, literal atoms, class/function names, and declarator slots.
/// Synthesis can then ask for the smallest feature path whose candidate set is
/// singleton and render everything else as selector holes.
struct ChunkSelectorIndex<'m> {
    module: &'m Module,
    chunk: selector_resolve::Chunk<'m>,
    decls: Vec<IndexedDeclaration>,
    binding_to_decl: BTreeMap<String, Vec<usize>>,
    /// Layer-1 read-off shape index (W2). Built once per chunk; the migrated
    /// forms (single-target function and var) read their minimal anchor set off
    /// it instead of running the cover search. The resolve
    /// ([`prove_synthesized_selector`]) stays the proof gate.
    shape_index: ShapeIndex,
}

#[derive(Debug)]
struct IndexedDeclaration {
    body_idx: usize,
    kind: IndexedDeclarationKind,
    declared_bindings: Vec<IndexedBinding>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
enum IndexedDeclarationKind {
    Function,
    Class,
    Var,
    Other,
}

#[derive(Debug)]
struct IndexedBinding {
    name: String,
}

fn rewrite_name_bindings_to_source_match(
    module: &str,
    file: &Path,
    root: &mut serde_yaml::Mapping,
    index: &ChunkSelectorIndex<'_>,
    selected_exports: Option<&BTreeSet<String>>,
    options: NameBindingRewriteOptions,
) -> Result<NameBindingRewriteOutcomes> {
    ensure_optional_sequence(root, "source_matches", module)?;
    let original_annotations = existing_annotations_mapping(root, module)?;
    let mut annotations = original_annotations.clone();
    let Some(Value::Sequence(members)) = root.get_mut(yk("members")) else {
        return Ok(NameBindingRewriteOutcomes {
            candidates: Vec::new(),
            members_scanned: 0,
            members_seen: 0,
            groups_changed: 0,
        });
    };
    let mut candidates = Vec::new();
    let mut grouped: BTreeMap<usize, Vec<NameBindingMember>> = BTreeMap::new();
    let mut remaining_exports = selected_exports.cloned();
    let mut members_scanned = 0;
    let mut members_seen = 0;

    for (member_index, member) in members.iter().enumerate() {
        if remaining_exports.as_ref().is_some_and(BTreeSet::is_empty) {
            break;
        }
        members_scanned += 1;
        let export_name = mapping_get(member, "name").and_then(value_as_string);
        let Some(export_name) = export_name else {
            continue;
        };
        if let Some(selected_exports) = selected_exports {
            if !selected_exports.contains(&export_name) {
                continue;
            }
            if let Some(remaining_exports) = &mut remaining_exports {
                remaining_exports.remove(&export_name);
            }
        }
        let Some(binding_name) =
            mapping_get_path(member, &["selector", "binding", "name"]).and_then(value_as_string)
        else {
            continue;
        };
        members_seen += 1;
        let decl_indices = index
            .binding_to_decl
            .get(&binding_name)
            .cloned()
            .unwrap_or_default();
        let [decl_idx] = decl_indices.as_slice() else {
            let reason = match decl_indices.len() {
                0 => format!("binding `{binding_name}` is not declared in source chunk"),
                n => format!("binding `{binding_name}` is declared in {n} source declarations"),
            };
            candidates.push(skipped_candidate(
                module,
                file,
                member_index,
                Some(export_name),
                reason,
            ));
            continue;
        };
        grouped
            .entry(*decl_idx)
            .or_default()
            .push(NameBindingMember {
                member_index,
                export_name,
                binding_name,
                annotation_fields: synthesized_member_annotation_fields(member),
            });
    }

    // Synthesize each declaration group; collect the successes for the grouping
    // pass and emit skip/error candidates immediately (they carry no selector to
    // group).
    let mut synthesized_groups = Vec::new();
    for (decl_idx, group_members) in grouped {
        match synthesize_simplest_selector_for_group(
            index,
            decl_idx,
            &group_members,
            options.candidates,
        ) {
            Ok(GroupSelectorOutcome::Synthesized(synthesized)) => {
                synthesized_groups.push(SynthesizedDeclGroup {
                    decl_idx,
                    members: group_members,
                    synthesized,
                });
            }
            Ok(GroupSelectorOutcome::Skipped(reason)) => {
                for member in group_members {
                    candidates.push(skipped_candidate(
                        module,
                        file,
                        member.member_index,
                        Some(member.export_name),
                        reason.clone(),
                    ));
                }
            }
            Err(err) => {
                for member in group_members {
                    candidates.push(skipped_candidate(
                        module,
                        file,
                        member.member_index,
                        Some(member.export_name),
                        format!("{err:#}"),
                    ));
                }
            }
        }
    }

    // Anti-unification grouping: collapse
    // maximal runs of adjacent, same-shape single-target declarations (any kind:
    // function, class, var) into one run-based source_matches[] claim. Multi-declarator var
    // groups (already grouped by shared declaration) and lone groups pass through
    // unchanged.
    let prepared_groups = merge_adjacent_same_shape_runs(index, synthesized_groups);

    // Successful rewrites become canonical `source_matches[]` claims. Rewritten
    // members are removed from `members`, and explicit member note/comment fields
    // move into module-level `annotations`.
    let mut replacements: BTreeMap<usize, Option<Value>> = BTreeMap::new();
    let mut source_matches = Vec::new();
    let groups_changed = prepared_groups.len();
    for (group_id, prepared) in prepared_groups.into_iter().enumerate() {
        let SynthesizedDeclGroup {
            members: group_members,
            synthesized,
            ..
        } = prepared;
        if group_members.len() == 1 {
            let member = &group_members[0];
            let target = &synthesized.target_bindings[0];
            candidates.push(synthesized_candidate(SynthesizedCandidateInput {
                module,
                file,
                member_index: member.member_index,
                export_name: Some(member.export_name.clone()),
                apply: options.apply,
                group_id,
                synthesized: &synthesized,
                target_binding: Some(target.export_name.clone()),
            }));
            if options.apply {
                add_synthesized_member_annotations(module, member, &mut annotations)?;
                source_matches.push(source_match_claim_value(
                    &synthesized.match_source,
                    vec![MigratedSourceBinding {
                        local: target.export_name.clone(),
                        name: member.export_name.clone(),
                    }],
                    None,
                ));
                replacements.insert(member.member_index, None);
            }
        } else {
            for member in &group_members {
                candidates.push(synthesized_candidate(SynthesizedCandidateInput {
                    module,
                    file,
                    member_index: member.member_index,
                    export_name: Some(member.export_name.clone()),
                    apply: options.apply,
                    group_id,
                    synthesized: &synthesized,
                    target_binding: None,
                }));
                if options.apply {
                    add_synthesized_member_annotations(module, member, &mut annotations)?;
                    replacements.insert(member.member_index, None);
                }
            }
            if options.apply {
                source_matches.push(synthesized_source_match_claim_value(&synthesized));
            }
        }
    }

    if options.apply {
        apply_member_replacements(members, replacements);
        if !source_matches.is_empty() {
            let entry = root
                .entry(yk("source_matches"))
                .or_insert_with(|| Value::Sequence(Vec::new()));
            match entry {
                Value::Sequence(existing) => existing.extend(source_matches),
                _ => bail!("{module}: source_matches exists but is not a sequence"),
            }
        }
        if annotations != original_annotations {
            root.insert(yk("annotations"), Value::Mapping(annotations));
        }
    }

    Ok(NameBindingRewriteOutcomes {
        candidates,
        members_scanned,
        members_seen,
        groups_changed,
    })
}

fn add_synthesized_member_annotations(
    module: &str,
    member: &NameBindingMember,
    annotations: &mut serde_yaml::Mapping,
) -> Result<()> {
    for (field, value) in &member.annotation_fields {
        merge_annotation_field(
            module,
            &format!("members[{}].{field}", member.member_index),
            annotations,
            &member.export_name,
            field,
            value,
        )?;
    }
    Ok(())
}

fn synthesized_member_annotation_fields(member: &Value) -> Vec<(&'static str, Value)> {
    let Some(member) = member.as_mapping() else {
        return Vec::new();
    };
    [
        "purity",
        "effect",
        "pure_members",
        "no_sync_callback_members",
        "comment",
        "note",
    ]
    .into_iter()
    .filter_map(|field| member.get(yk(field)).map(|value| (field, value.clone())))
    .collect()
}

fn ensure_optional_sequence(root: &serde_yaml::Mapping, key: &str, module: &str) -> Result<()> {
    if let Some(value) = root.get(yk(key))
        && !matches!(value, Value::Sequence(_))
    {
        bail!("{module}: {key} exists but is not a sequence");
    }
    Ok(())
}

fn existing_annotations_mapping(
    root: &serde_yaml::Mapping,
    module: &str,
) -> Result<serde_yaml::Mapping> {
    match root.get(yk("annotations")) {
        None => Ok(serde_yaml::Mapping::new()),
        Some(Value::Mapping(mapping)) => Ok(mapping.clone()),
        Some(_) => bail!("{module}: annotations exists but is not a mapping"),
    }
}

fn merge_annotation_field(
    module: &str,
    origin: &str,
    annotations: &mut serde_yaml::Mapping,
    export_name: &str,
    field: &str,
    value: &Value,
) -> Result<()> {
    if annotation_field_is_empty(field, value) {
        return Ok(());
    }
    validate_annotation_field_value(module, origin, field, value)?;
    let annotation = annotations
        .entry(Value::String(export_name.to_string()))
        .or_insert_with(|| Value::Mapping(serde_yaml::Mapping::new()));
    let Value::Mapping(annotation_mapping) = annotation else {
        bail!("{module}: annotations.{export_name} exists but is not a mapping");
    };
    match annotation_mapping.get(yk(field)) {
        Some(existing) if existing == value => Ok(()),
        Some(_) => bail!("{module}: {origin} conflicts with annotations.{export_name}.{field}"),
        None => {
            annotation_mapping.insert(yk(field), value.clone());
            Ok(())
        }
    }
}

fn annotation_field_is_empty(field: &str, value: &Value) -> bool {
    match (field, value) {
        (_, Value::Null) => true,
        ("purity" | "effect", Value::String(value)) => value == "default",
        ("pure_members" | "no_sync_callback_members", Value::Sequence(values)) => values.is_empty(),
        _ => false,
    }
}

fn validate_annotation_field_value(
    module: &str,
    origin: &str,
    field: &str,
    value: &Value,
) -> Result<()> {
    match field {
        "comment" | "note" | "purity" | "effect" => {
            if !matches!(value, Value::String(_)) {
                bail!("{module}: {origin}.{field} must be a string");
            }
        }
        "pure_members" | "no_sync_callback_members" => {
            let Value::Sequence(values) = value else {
                bail!("{module}: {origin}.{field} must be a string list");
            };
            for item in values {
                if !matches!(item, Value::String(_)) {
                    bail!("{module}: {origin}.{field} entries must be strings");
                }
            }
        }
        _ => bail!("{module}: unsupported annotation field `{field}`"),
    }
    Ok(())
}

fn source_match_claim_value(
    match_source: &str,
    bindings: Vec<MigratedSourceBinding>,
    note: Option<String>,
) -> Value {
    let mut claim = serde_yaml::Mapping::new();
    claim.insert(yk("match"), Value::String(match_source.to_string()));
    claim.insert(
        yk("bindings"),
        Value::Sequence(
            bindings
                .into_iter()
                .map(source_match_binding_value)
                .collect(),
        ),
    );
    if let Some(note) = note {
        claim.insert(yk("note"), Value::String(note));
    }
    Value::Mapping(claim)
}

fn source_match_binding_value(binding: MigratedSourceBinding) -> Value {
    if binding.local == binding.name {
        return Value::String(binding.local);
    }
    let mut detail = serde_yaml::Mapping::new();
    detail.insert(yk("local"), Value::String(binding.local));
    detail.insert(yk("name"), Value::String(binding.name));
    Value::Mapping(detail)
}

fn synthesized_source_match_claim_value(synthesized: &SynthesizedSelectorGroup) -> Value {
    source_match_claim_value(
        &synthesized.match_source,
        synthesized
            .target_bindings
            .iter()
            .map(|target| MigratedSourceBinding {
                local: target.export_name.clone(),
                name: target.export_name.clone(),
            })
            .collect(),
        None,
    )
}

fn load_synthesis_module(config: &SelectorCodemodConfig) -> Result<js_ast::ParsedJsModule> {
    let source_file = match (&config.source_file, &config.source_root, &config.chunk) {
        (Some(source_file), _, None) => source_file.clone(),
        (None, Some(source_root), Some(chunk)) => source_root.join(chunk),
        (Some(_), _, Some(_)) => {
            bail!("use either --source-file or --source-root with --chunk, not both")
        }
        _ => {
            bail!("name-binding-to-source-match requires --source-file or --source-root + --chunk")
        }
    };
    let source = fs::read_to_string(&source_file)
        .with_context(|| format!("reading source file {}", source_file.display()))?;
    js_ast::parse_js_module_consuming(&source_file.display().to_string(), source)
        .with_context(|| format!("parsing source file {}", source_file.display()))
}

fn parse_synthesis_item(raw: &str) -> Result<SynthesisItem> {
    let Some((module, export_name)) = raw.rsplit_once(':') else {
        bail!("--item must be `module/path:ExportName`, got `{raw}`");
    };
    if module.is_empty() || export_name.is_empty() {
        bail!("--item must be `module/path:ExportName`, got `{raw}`");
    }
    Ok(SynthesisItem {
        module: module.to_string(),
        export_name: export_name.to_string(),
    })
}

fn selected_item_exports_by_module(
    selected_items: &BTreeSet<SynthesisItem>,
) -> BTreeMap<String, BTreeSet<String>> {
    let mut by_module: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    for item in selected_items {
        by_module
            .entry(item.module.clone())
            .or_default()
            .insert(item.export_name.clone());
    }
    by_module
}

fn collect_candidate_module_files(
    config: &SelectorCodemodConfig,
    selected_files: &BTreeSet<PathBuf>,
    selected_modules: &BTreeSet<String>,
    selected_item_exports: &BTreeMap<String, BTreeSet<String>>,
) -> Result<Vec<PathBuf>> {
    let has_explicit_module_filters = !selected_files.is_empty()
        || !selected_modules.is_empty()
        || !config.module_prefixes.is_empty();
    if !has_explicit_module_filters && selected_item_exports.is_empty() {
        return collect_module_files(&config.modules_root);
    }

    let mut candidates = BTreeSet::new();
    for file in selected_files {
        add_existing_module_file(&mut candidates, file);
    }
    for module in selected_modules {
        add_existing_module_file(
            &mut candidates,
            &module_file_path(&config.modules_root, module),
        );
    }
    for module in selected_item_exports.keys() {
        add_existing_module_file(
            &mut candidates,
            &module_file_path(&config.modules_root, module),
        );
    }
    if selected_item_exports.is_empty() {
        for prefix in &config.module_prefixes {
            add_module_prefix_files(&mut candidates, &config.modules_root, prefix)?;
        }
    }

    Ok(candidates
        .into_iter()
        .filter(|file| {
            let module = module_path_from_file(file, &config.modules_root);
            module_selected(
                file,
                &module,
                selected_files,
                selected_modules,
                &config.module_prefixes,
            ) && (selected_item_exports.is_empty() || selected_item_exports.contains_key(&module))
        })
        .collect())
}

fn module_file_path(modules_root: &Path, module: &str) -> PathBuf {
    modules_root.join(format!("{module}.yaml"))
}

fn add_existing_module_file(candidates: &mut BTreeSet<PathBuf>, path: &Path) {
    if path.is_file() && is_module_yaml(path) {
        candidates.insert(path.to_path_buf());
    }
}

fn add_module_prefix_files(
    candidates: &mut BTreeSet<PathBuf>,
    modules_root: &Path,
    prefix: &str,
) -> Result<()> {
    if prefix.is_empty() {
        return Ok(());
    }
    add_existing_module_file(candidates, &module_file_path(modules_root, prefix));
    let dir = modules_root.join(prefix);
    if dir.is_dir() {
        candidates.extend(collect_module_files(&dir)?);
    }
    Ok(())
}

impl<'m> ChunkSelectorIndex<'m> {
    fn new(module: &'m Module) -> Self {
        let mut decls = Vec::new();
        let mut binding_to_decl: BTreeMap<String, Vec<usize>> = BTreeMap::new();
        for (body_idx, item) in module.body.iter().enumerate() {
            let indexed = IndexedDeclaration::from_item(body_idx, item);
            if indexed.declared_bindings.is_empty() {
                continue;
            }
            let decl_idx = decls.len();
            for binding in &indexed.declared_bindings {
                binding_to_decl
                    .entry(binding.name.clone())
                    .or_default()
                    .push(decl_idx);
            }
            decls.push(indexed);
        }
        let shape_index = ShapeIndex::new(module);
        Self {
            module,
            chunk: selector_resolve::Chunk::analyze(SYNTHESIS_MODULE, module),
            decls,
            binding_to_decl,
            shape_index,
        }
    }
}

impl IndexedDeclaration {
    fn from_item(body_idx: usize, item: &ModuleItem) -> Self {
        let (kind, declared_bindings) = match item_decl(item) {
            Some(Decl::Fn(function)) => (
                IndexedDeclarationKind::Function,
                vec![IndexedBinding {
                    name: function.ident.sym.to_string(),
                }],
            ),
            Some(Decl::Class(class)) => (
                IndexedDeclarationKind::Class,
                vec![IndexedBinding {
                    name: class.ident.sym.to_string(),
                }],
            ),
            Some(Decl::Var(var)) => (
                IndexedDeclarationKind::Var,
                var.decls
                    .iter()
                    .flat_map(|declarator| {
                        binding_names_for_pat(&declarator.name)
                            .into_iter()
                            .map(move |name| IndexedBinding { name })
                    })
                    .collect(),
            ),
            _ => (IndexedDeclarationKind::Other, Vec::new()),
        };
        Self {
            body_idx,
            kind,
            declared_bindings,
        }
    }
}

/// Outcome of trying to synthesize a selector for one declaration group.
///
/// `Skipped` is returned when minimization produces no proving sparse selector:
/// rather than pin the rebuild-fragile exact AST, the caller skips the members
/// with this reason.
enum GroupSelectorOutcome {
    Synthesized(SynthesizedSelectorGroup),
    Skipped(String),
}

fn synthesize_simplest_selector_for_group(
    index: &ChunkSelectorIndex<'_>,
    decl_idx: usize,
    members: &[NameBindingMember],
    candidates_limit: usize,
) -> Result<GroupSelectorOutcome> {
    let decl = index
        .decls
        .get(decl_idx)
        .with_context(|| format!("missing indexed declaration {decl_idx}"))?;
    let item = index
        .module
        .body
        .get(decl.body_idx)
        .with_context(|| format!("missing source body index {}", decl.body_idx))?;
    let targets = members
        .iter()
        .map(|member| SynthesizedTargetBinding {
            export_name: member.export_name.clone(),
            runtime_binding: member.binding_name.clone(),
        })
        .collect::<Vec<_>>();
    let Some(specialized) = synthesize_specialized_selector(index, item, decl, &targets)? else {
        return Ok(GroupSelectorOutcome::Skipped(
            "minimization found no sparse selector; skipping full-AST pin".to_string(),
        ));
    };
    let match_source = trim_selector_source_line_suffixes(&specialized.match_source);
    if prove_synthesized_selector(index, decl, &targets, &match_source).is_err() {
        return Ok(GroupSelectorOutcome::Skipped(
            "minimization found no sparse selector; skipping full-AST pin".to_string(),
        ));
    };
    let rewritten_holes = specialized.rewritten_holes;

    // `--candidates N > 1`: collect the rest of the ranked read-off menu beyond the
    // primary pick (deduped against it). Object/multi-declarator-var menus yield
    // none until their read-off forms are wired through the menu path.
    let alternatives = if candidates_limit > 1 {
        synthesize_specialized_selector_candidates(index, item, decl, &targets, candidates_limit)?
            .into_iter()
            .map(|candidate| SelectorAlternative {
                match_source: trim_selector_source_line_suffixes(&candidate.match_source),
                rewritten_holes: candidate.rewritten_holes.into_iter().collect(),
            })
            .filter(|alternative| alternative.match_source != match_source)
            .collect()
    } else {
        Vec::new()
    };

    Ok(GroupSelectorOutcome::Synthesized(
        SynthesizedSelectorGroup {
            body_idx: decl.body_idx,
            target_bindings: targets,
            match_source,
            rewritten_holes: rewritten_holes.into_iter().collect(),
            alternatives,
        },
    ))
}

// ===========================================================================
// Anti-unification grouping.
//
// `synthesize_simplest_selector_for_group` already groups members that share an
// enclosing declaration (multi-declarator var statements). The second grouping
// trigger — "minimal selectors overlap beyond a threshold" — collapses a run of
// adjacent, near-identical *single-target declarations* whose individually-
// minimized selectors share the bulk of their shape into one run-based
// grouped source_match, instead of N standalone source_match selectors. This started
// function-only (four context-accessor hooks each `function useX() { return
// ANYTHING.…; }`) and now generalizes to any single-target declaration kind:
// sibling class declarations, statement-run functions, object/var declarations,
// etc. — the same co-occurrence idea, not specialized to functions.
//
// The overlap test runs on the *minimized* selectors: same-purpose siblings
// collapse to the same minimal shape (`ANYTHING.<key>`), so two selectors are
// "same shape" iff their canonical signatures — every identifier, member/key
// name, and literal value blanked, leaving only structure ([`selector_shape_signature`])
// — are equal. The merged run-selector is re-proven through the matcher gate
// (kind-agnostic multi-statement alignment); on failure the run is emitted as
// individual selectors, never an unproven group.
// ===========================================================================

/// Collapse maximal runs of adjacent, same-shape single-target declaration
/// groups into one run-based grouped source_match. Other groups (multi-declarator var
/// groups, lone declarations, anything whose merged run fails the matcher gate)
/// pass through unchanged, preserving source order.
fn merge_adjacent_same_shape_runs(
    index: &ChunkSelectorIndex<'_>,
    groups: Vec<SynthesizedDeclGroup>,
) -> Vec<SynthesizedDeclGroup> {
    let mut merged = Vec::with_capacity(groups.len());
    let mut run: Vec<SynthesizedDeclGroup> = Vec::new();
    for group in groups {
        let extends_run = run
            .last()
            .is_some_and(|prev| same_shape_run_extends(index, prev, &group));
        if !extends_run {
            flush_same_shape_run(index, std::mem::take(&mut run), &mut merged);
        }
        run.push(group);
    }
    flush_same_shape_run(index, run, &mut merged);
    merged
}

/// Emit a candidate run: merge it into one grouped source_match when it holds ≥2 groups
/// and the merged selector proves unique, else emit each group individually.
fn flush_same_shape_run(
    index: &ChunkSelectorIndex<'_>,
    run: Vec<SynthesizedDeclGroup>,
    out: &mut Vec<SynthesizedDeclGroup>,
) {
    if run.len() >= 2
        && let Some(group) = merge_same_shape_run(index, &run)
    {
        out.push(group);
        return;
    }
    out.extend(run);
}

/// Whether `next` continues a same-shape run started by `prev`: both are
/// single-target declaration groups of the same declaration kind, they are
/// consecutive in source order, and their minimized selectors share the same
/// canonical shape.
fn same_shape_run_extends(
    index: &ChunkSelectorIndex<'_>,
    prev: &SynthesizedDeclGroup,
    next: &SynthesizedDeclGroup,
) -> bool {
    matches!(
        (single_target_decl_kind(index, prev), single_target_decl_kind(index, next)),
        (Some(prev_kind), Some(next_kind)) if prev_kind == next_kind
    ) && next.synthesized.body_idx == prev.synthesized.body_idx + 1
        && same_selector_shape(
            &prev.synthesized.match_source,
            &next.synthesized.match_source,
        )
}

/// The declaration kind of a single-target (one-member) group, or `None` when
/// the group covers multiple members (a multi-declarator var group, already
/// grouped by its shared declaration) or its declaration index is unknown. A
/// run only merges declarations of one kind so the concatenated selector stays a
/// homogeneous sibling run. `Other`-kind declarations are excluded: their
/// selector is an unmodeled verbatim statement, not a holed shape, so a
/// shape-signature match would be coincidental rather than a true co-occurrence.
fn single_target_decl_kind(
    index: &ChunkSelectorIndex<'_>,
    group: &SynthesizedDeclGroup,
) -> Option<IndexedDeclarationKind> {
    if group.members.len() != 1 {
        return None;
    }
    let kind = index.decls.get(group.decl_idx)?.kind;
    (kind != IndexedDeclarationKind::Other).then_some(kind)
}

/// Build one run-based grouped source_match from `run`: the selector is the run's
/// declarations concatenated in source order and binds every target. The
/// merged selector is re-proven through the matcher gate; `None` (proof failed)
/// leaves the run to be emitted individually.
fn merge_same_shape_run(
    index: &ChunkSelectorIndex<'_>,
    run: &[SynthesizedDeclGroup],
) -> Option<SynthesizedDeclGroup> {
    let first = run.first()?;
    let decl = index.decls.get(first.decl_idx)?;
    let match_source = run
        .iter()
        .map(|group| group.synthesized.match_source.as_str())
        .collect::<Vec<_>>()
        .join("\n");
    let targets = run
        .iter()
        .flat_map(|group| group.synthesized.target_bindings.iter().cloned())
        .collect::<Vec<_>>();
    prove_synthesized_selector(index, decl, &targets, &match_source).ok()?;
    let members = run
        .iter()
        .flat_map(|group| group.members.iter().cloned())
        .collect::<Vec<_>>();
    Some(SynthesizedDeclGroup {
        decl_idx: first.decl_idx,
        members,
        synthesized: SynthesizedSelectorGroup {
            body_idx: first.synthesized.body_idx,
            target_bindings: targets,
            rewritten_holes: holes_present(&match_source).into_iter().collect(),
            match_source,
            alternatives: Vec::new(),
        },
    })
}

/// Whether two single-declaration selector sources have the same canonical shape
/// (equal once every value-bearing leaf is blanked). `false` if either fails to
/// parse, so an unparseable source never grafts onto a run.
fn same_selector_shape(left: &str, right: &str) -> bool {
    matches!(
        (selector_shape_signature(left), selector_shape_signature(right)),
        (Some(left), Some(right)) if left == right
    )
}

/// Canonical structural signature of a selector source: the AST re-emitted with
/// every identifier, member/property name, object key, and literal value blanked
/// to a fixed placeholder, so selectors that differ only in their discriminating
/// anchors (a DRY accessor cluster) share a signature.
fn selector_shape_signature(match_source: &str) -> Option<String> {
    let mut module =
        js_ast::parse_js_module_ast("<selector shape signature>", match_source).ok()?;
    module.visit_mut_with(&mut ShapeSignatureCanonicalizer);
    js_ast::emit_module_source(&module).ok()
}

/// Placeholder every value-bearing leaf collapses to in a shape signature.
const SHAPE_SIGNATURE_BLANK: &str = "_";

/// Blanks identifiers, member/property names, object keys, and literal values so
/// only structural shape survives (see [`selector_shape_signature`]).
struct ShapeSignatureCanonicalizer;

impl VisitMut for ShapeSignatureCanonicalizer {
    fn visit_mut_ident(&mut self, ident: &mut Ident) {
        ident.sym = SHAPE_SIGNATURE_BLANK.into();
    }

    fn visit_mut_binding_ident(&mut self, ident: &mut BindingIdent) {
        ident.id.sym = SHAPE_SIGNATURE_BLANK.into();
        ident.type_ann.visit_mut_with(self);
    }

    fn visit_mut_member_prop(&mut self, prop: &mut MemberProp) {
        match prop {
            MemberProp::Ident(ident) => ident.sym = SHAPE_SIGNATURE_BLANK.into(),
            MemberProp::PrivateName(_) => {}
            MemberProp::Computed(computed) => computed.visit_mut_children_with(self),
        }
    }

    fn visit_mut_prop_name(&mut self, name: &mut PropName) {
        match name {
            PropName::Ident(ident) => ident.sym = SHAPE_SIGNATURE_BLANK.into(),
            PropName::Str(str_lit) => js_ast::set_str_value(str_lit, SHAPE_SIGNATURE_BLANK.into()),
            other => other.visit_mut_children_with(self),
        }
    }

    fn visit_mut_expr(&mut self, expr: &mut Expr) {
        if matches!(expr, Expr::Lit(_)) {
            *expr = Expr::Lit(Lit::Null(Null { span: DUMMY_SP }));
            return;
        }
        expr.visit_mut_children_with(self);
    }
}

/// The module name the synthesis probes resolve under.
const SYNTHESIS_MODULE: &str = "<selector-codemod>";

/// Every place `match_source`, projected onto `export_name`, matches in the
/// chunk: the minimizer's measure of how far a candidate is from unique.
fn match_single_member_selector(
    index: &ChunkSelectorIndex<'_>,
    export_name: &str,
    match_source: &str,
) -> Result<Vec<MemberBindingMatch>> {
    index.chunk.matcher().member_candidates(
        SYNTHESIS_MODULE,
        &SourceMatch {
            match_source: match_source.to_string(),
            identifiers: SourceMatchIdentifierMode::AlphaAll,
            target_binding: Some(export_name.to_string()),
        }
        .selector(),
    )
}

/// The proof gate: `match_source`, claiming every target by its export name,
/// resolves each target on its own selector to its intended runtime binding,
/// the first of them at `decl`. A selector unique only by elimination does not
/// prove: it would move when the claimer is edited.
fn prove_synthesized_selector(
    index: &ChunkSelectorIndex<'_>,
    decl: &IndexedDeclaration,
    targets: &[SynthesizedTargetBinding],
    match_source: &str,
) -> Result<()> {
    if targets.is_empty() {
        bail!("selector synthesis group has no targets");
    }
    let members = targets
        .iter()
        .map(|target| {
            let selector = SourceMatch {
                match_source: match_source.to_string(),
                identifiers: SourceMatchIdentifierMode::AlphaAll,
                target_binding: Some(target.export_name.clone()),
            }
            .selector();
            Ok(Member {
                export_name: target.export_name.clone(),
                selector: MemberSelector::SourceMatch(ParsedSourceMatchSelector::parse(
                    SYNTHESIS_MODULE,
                    "source_match",
                    format!("<source_match needle in {SYNTHESIS_MODULE}>"),
                    &selector,
                    "source_match",
                )?),
            })
        })
        .collect::<Result<Vec<_>>>()?;
    let resolution = index.chunk.resolve(&[SpecModule {
        path: SYNTHESIS_MODULE.to_string(),
        members,
        anonymous_statements: Vec::new(),
    }])?;
    let mut first_owner = usize::MAX;
    for (member_index, target) in targets.iter().enumerate() {
        let outcome = resolution
            .outcome(0, EntityIndex::Member(member_index))
            .context("a source_match member has an outcome")?;
        match &outcome.outcome {
            Outcome::Resolved {
                owner,
                binding: Some(binding),
                resolved_by: ResolvedBy::OwnSelector,
            } if *binding == target.runtime_binding => first_owner = first_owner.min(*owner),
            _ => bail!(
                "synthesized selector does not prove `{}` as `{}`: {}",
                target.runtime_binding,
                target.export_name,
                outcome.render_line()
            ),
        }
    }
    if first_owner != decl.body_idx {
        bail!(
            "synthesized selector matched body index {first_owner} instead of intended {}",
            decl.body_idx
        );
    }
    Ok(())
}

struct SpecializedSelector {
    match_source: String,
    rewritten_holes: BTreeSet<String>,
}

fn synthesize_specialized_selector(
    index: &ChunkSelectorIndex<'_>,
    item: &ModuleItem,
    decl: &IndexedDeclaration,
    targets: &[SynthesizedTargetBinding],
) -> Result<Option<SpecializedSelector>> {
    match decl.kind {
        // Function and class single-pick is the candidates read-off at limit 1
        // (`read_off_candidates` stops at the first proving selector). On an
        // empty result the caller skips instead of emitting a full-AST pin.
        IndexedDeclarationKind::Function | IndexedDeclarationKind::Class => Ok(
            synthesize_specialized_selector_candidates(index, item, decl, targets, 1)?
                .into_iter()
                .next(),
        ),
        IndexedDeclarationKind::Var => {
            synthesize_specialized_var_selector(index, item, decl, targets)
        }
        IndexedDeclarationKind::Other => Ok(None),
    }
}

/// Up to `limit` ranked candidate selectors for the item — the
/// `synthesize-selectors --candidates N` menu. The function/class read-off forms
/// return their full ranked walk; object/multi-declarator-var emit only the single
/// pick for now (their menus are not yet wired through the var/object read-off).
fn synthesize_specialized_selector_candidates(
    index: &ChunkSelectorIndex<'_>,
    item: &ModuleItem,
    decl: &IndexedDeclaration,
    targets: &[SynthesizedTargetBinding],
    limit: usize,
) -> Result<Vec<SpecializedSelector>> {
    match decl.kind {
        IndexedDeclarationKind::Function => {
            let [target] = targets else {
                return Ok(Vec::new());
            };
            let Some(Decl::Fn(function)) = item_decl(item) else {
                return Ok(Vec::new());
            };
            minimize_function_selector_candidates(index, &function.function, decl, target, limit)
        }
        IndexedDeclarationKind::Class => {
            let [target] = targets else {
                return Ok(Vec::new());
            };
            let Some(Decl::Class(class_decl)) = item_decl(item) else {
                return Ok(Vec::new());
            };
            minimize_class_selector_candidates(index, &class_decl.class, decl, target, limit)
        }
        IndexedDeclarationKind::Var => {
            let var =
                item_var_decl(item).context("indexed var declaration no longer has var AST")?;
            minimize_var_group_selector_candidates(index, var, decl, targets, limit)
        }
        IndexedDeclarationKind::Other => {
            Ok(synthesize_specialized_selector(index, item, decl, targets)?
                .into_iter()
                .collect())
        }
    }
}

fn synthesize_specialized_var_selector(
    index: &ChunkSelectorIndex<'_>,
    item: &ModuleItem,
    decl: &IndexedDeclaration,
    targets: &[SynthesizedTargetBinding],
) -> Result<Option<SpecializedSelector>> {
    let var = item_var_decl(item).context("indexed var declaration no longer has var AST")?;
    // Single-target and multi-target vars both route through the AST-prune group
    // path (the single case is the N=1 group). On `None`, the caller skips
    // instead of emitting a full-AST pin.
    minimize_var_group_selector(index, var, decl, targets)
}

/// Distinct body indices the selector resolves to (slot alignments within one
/// body collapse). Used by the read-off structural fast path
/// (the bare-scaffold branch of `read_off_candidates`).
fn matched_body_indices(
    index: &ChunkSelectorIndex<'_>,
    export_name: &str,
    match_source: &str,
) -> Result<BTreeSet<usize>> {
    Ok(
        match_single_member_selector(index, export_name, match_source)?
            .iter()
            .map(|candidate| candidate.body_idx)
            .collect(),
    )
}

/// The declarator-run hole for a binding-group selector. The matcher treats every
/// `DECLARATORS` / `DECLARATORS_*` run hole identically -- the positional suffix
/// is not equality-binding, only used in human-facing hint text -- so the
/// renderer always emits the plain keyword. Suffixed forms remain accepted on
/// input.
fn declarator_hole_name() -> &'static str {
    DECLARATORS_HOLE_KEYWORD
}

fn trim_selector_source_line_suffixes(source: &str) -> String {
    source
        .split('\n')
        .map(|line| line.trim_end_matches([' ', '\t']))
        .collect::<Vec<_>>()
        .join("\n")
}

struct SynthesizedCandidateInput<'a> {
    module: &'a str,
    file: &'a Path,
    member_index: usize,
    export_name: Option<String>,
    apply: bool,
    group_id: usize,
    synthesized: &'a SynthesizedSelectorGroup,
    target_binding: Option<String>,
}

fn synthesized_candidate(input: SynthesizedCandidateInput<'_>) -> SelectorCodemodCandidate {
    SelectorCodemodCandidate {
        module: input.module.to_string(),
        file: input.file.display().to_string(),
        member_index: input.member_index,
        export_name: input.export_name,
        action: if input.apply {
            SelectorCodemodAction::Changed
        } else {
            SelectorCodemodAction::WouldChange
        },
        target_binding: input.target_binding,
        declared_bindings: input
            .synthesized
            .target_bindings
            .iter()
            .map(|target| target.export_name.clone())
            .collect(),
        group_id: Some(input.group_id),
        matched_body_index: Some(input.synthesized.body_idx),
        // A proven selector matches exactly once.
        candidate_count: Some(1),
        match_source: Some(input.synthesized.match_source.clone()),
        rewritten_holes: input.synthesized.rewritten_holes.clone(),
        replacement_count: input.synthesized.rewritten_holes.len(),
        alternatives: input.synthesized.alternatives.clone(),
        reason: None,
    }
}

fn apply_member_replacements(
    members: &mut Vec<Value>,
    replacements: BTreeMap<usize, Option<Value>>,
) {
    let mut next = Vec::with_capacity(members.len());
    for (idx, member) in std::mem::take(members).into_iter().enumerate() {
        match replacements.get(&idx) {
            Some(Some(replacement)) => next.push(replacement.clone()),
            Some(None) => {}
            None => next.push(member),
        }
    }
    *members = next;
}

fn skipped_candidate(
    module: &str,
    file: &Path,
    member_index: usize,
    export_name: Option<String>,
    reason: impl Into<String>,
) -> SelectorCodemodCandidate {
    SelectorCodemodCandidate {
        module: module.to_string(),
        file: file.display().to_string(),
        member_index,
        export_name,
        action: SelectorCodemodAction::Skipped,
        target_binding: None,
        declared_bindings: Vec::new(),
        group_id: None,
        matched_body_index: None,
        candidate_count: None,
        match_source: None,
        rewritten_holes: Vec::new(),
        replacement_count: 0,
        alternatives: Vec::new(),
        reason: Some(reason.into()),
    }
}

fn module_selected(
    file: &Path,
    module: &str,
    selected_files: &BTreeSet<PathBuf>,
    selected_modules: &BTreeSet<String>,
    module_prefixes: &[String],
) -> bool {
    if selected_files.is_empty() && selected_modules.is_empty() && module_prefixes.is_empty() {
        return true;
    }
    if selected_files.contains(file)
        || selected_files.contains(&PathBuf::from(format!("{module}.yaml")))
    {
        return true;
    }
    if selected_modules.contains(module) {
        return true;
    }
    module_prefixes
        .iter()
        .any(|prefix| module == prefix || module.starts_with(&format!("{prefix}/")))
}

fn resolve_file_filter(modules_root: &Path, path: &Path) -> PathBuf {
    if path.is_absolute() {
        return path.to_path_buf();
    }
    let under_modules = modules_root.join(path);
    if under_modules.exists() {
        under_modules
    } else {
        path.to_path_buf()
    }
}

fn mapping_get<'a>(value: &'a Value, key: &str) -> Option<&'a Value> {
    let Value::Mapping(mapping) = value else {
        return None;
    };
    mapping.get(yk(key))
}

fn mapping_get_path<'a>(value: &'a Value, path: &[&str]) -> Option<&'a Value> {
    let mut current = value;
    for key in path {
        let Value::Mapping(mapping) = current else {
            return None;
        };
        current = mapping.get(yk(key))?;
    }
    Some(current)
}

fn value_as_string(value: &Value) -> Option<String> {
    match value {
        Value::String(value) => Some(value.clone()),
        _ => None,
    }
}

fn item_decl(item: &ModuleItem) -> Option<&Decl> {
    match item {
        ModuleItem::Stmt(Stmt::Decl(decl)) => Some(decl),
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => Some(&export.decl),
        _ => None,
    }
}

fn item_var_decl(item: &ModuleItem) -> Option<&VarDecl> {
    match item_decl(item) {
        Some(Decl::Var(var)) => Some(var),
        _ => None,
    }
}

fn binding_names_for_pat(pat: &Pat) -> Vec<String> {
    let mut names = Vec::new();
    binding_names_for_pat_into(pat, &mut names);
    names
}

fn binding_names_for_pat_into(pat: &Pat, names: &mut Vec<String>) {
    match pat {
        Pat::Ident(ident) => names.push(ident.id.sym.to_string()),
        Pat::Array(array) => {
            for elem in array.elems.iter().flatten() {
                binding_names_for_pat_into(elem, names);
            }
        }
        Pat::Object(object) => {
            for prop in &object.props {
                match prop {
                    ObjectPatProp::KeyValue(key_value) => {
                        binding_names_for_pat_into(&key_value.value, names);
                    }
                    ObjectPatProp::Assign(assign) => names.push(assign.key.id.sym.to_string()),
                    ObjectPatProp::Rest(rest) => binding_names_for_pat_into(&rest.arg, names),
                }
            }
        }
        Pat::Rest(rest) => binding_names_for_pat_into(&rest.arg, names),
        Pat::Assign(assign) => binding_names_for_pat_into(&assign.left, names),
        Pat::Invalid(_) | Pat::Expr(_) => {}
    }
}

fn single_ident_pat_name(pat: &Pat) -> Option<&str> {
    match pat {
        Pat::Ident(ident) => Some(ident.id.sym.as_ref()),
        _ => None,
    }
}

fn yk(key: &str) -> Value {
    Value::String(key.to_string())
}

pub fn render_selector_codemod_text(report: &SelectorCodemodReport, out: &mut String) {
    let s = &report.summary;
    out.push_str(&format!(
        "{} {}: {} candidate(s), {} skipped, {} file(s) written\n",
        report.action,
        report.rewrite.name(),
        s.changed_candidates,
        s.skipped_candidates,
        s.files_written.len()
    ));
    for candidate in &report.candidates {
        let readable = candidate.export_name.as_deref().unwrap_or("-");
        match candidate.action {
            SelectorCodemodAction::WouldChange | SelectorCodemodAction::Changed => {
                if candidate.replacement_count > 0 {
                    out.push_str(&format!(
                        "  {:?} {} member#{} [{}] replacements={} holes={}\n",
                        candidate.action,
                        candidate.module,
                        candidate.member_index,
                        readable,
                        candidate.replacement_count,
                        candidate.rewritten_holes.join(",")
                    ));
                } else {
                    let target = candidate.target_binding.as_deref().unwrap_or("-");
                    out.push_str(&format!(
                        "  {:?} {} member#{} [{}] projected_binding={}\n",
                        candidate.action,
                        candidate.module,
                        candidate.member_index,
                        readable,
                        target
                    ));
                }
            }
            SelectorCodemodAction::Skipped => {
                out.push_str(&format!(
                    "  skipped {} member#{} [{}]: {}\n",
                    candidate.module,
                    candidate.member_index,
                    readable,
                    candidate.reason.as_deref().unwrap_or("unknown reason")
                ));
            }
        }
    }
}

fn is_zero(value: &usize) -> bool {
    *value == 0
}

#[cfg(test)]
mod selector_minimizer_proptest;

#[cfg(test)]
mod adjacent_function_grouping_tests {
    use super::*;

    fn signature(source: &str) -> String {
        js_ast::with_swc_globals(|| selector_shape_signature(source).expect("selector parses"))
    }

    #[test]
    fn accessors_differing_only_in_member_key_share_a_shape() {
        // The minimized accessor selectors differ only in the holed function name
        // and the trailing member key; blanking those discriminating leaves
        // collapses them to one canonical shape — the run-grouping trigger.
        let alpha = "function selectedAlphaAccessor() { return ANYTHING.alpha; }";
        let beta = "function selectedBetaAccessor() { return ANYTHING.beta; }";
        let core = "function selectedDeltaAccessor() { return ANYTHING.coreServices; }";
        assert_eq!(signature(alpha), signature(beta));
        assert_eq!(signature(alpha), signature(core));
    }

    #[test]
    fn different_shapes_do_not_share_a_signature() {
        // A zero-arg member-return accessor anti-unifies to neither a one-arg
        // arithmetic helper (param count + body differ) nor a bare call (body
        // structure differs), so such neighbors never merge into one run.
        let accessor = "function a() { return ANYTHING.alpha; }";
        assert_ne!(
            signature(accessor),
            signature("function h(value) { return value * 2; }")
        );
        assert_ne!(
            signature(accessor),
            signature("function c() { return ANYTHING(); }")
        );
    }

    #[test]
    fn member_chain_depth_is_structural() {
        // Blanking erases key *names* but keeps chain depth, so a one-member and a
        // two-member access are distinct shapes and stay separate selectors.
        assert_ne!(
            signature("function a() { return ANYTHING.alpha; }"),
            signature("function a() { return ANYTHING.services.alpha; }")
        );
    }

    #[test]
    fn same_selector_shape_matches_same_shape_only() {
        js_ast::with_swc_globals(|| {
            assert!(same_selector_shape(
                "function a() { return ANYTHING.alpha; }",
                "function b() { return ANYTHING.beta; }",
            ));
            // A structurally different neighbor never grafts onto the run.
            assert!(!same_selector_shape(
                "function a() { return ANYTHING.alpha; }",
                "function h(value) { return value * 2; }",
            ));
        });
    }
}
