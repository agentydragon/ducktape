//! Mechanical, reviewable spec selector rewrites.
//!
//! This powers `debundle spec selector-codemod`: a scripting-safe CLI for
//! applying proven selector rewrites across a modules tree. The core
//! source-aware rewrite is framed as: given one or more target entities, come
//! up with the simplest selector/spec fragment that uniquely selects them.
//! This module starts with an indexed subset of that minimization problem:
//! build a per-chunk declaration table and binding-name index, group requested
//! bindings by source declaration, render a structural selector with
//! declarator gaps for non-target siblings, then prove uniqueness with the
//! production `source_match` matcher.

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail};
use serde::Serialize;
use serde_yaml::Value;
use spec::{SourceMatch, SourceMatchIdentifierMode};
use spec_modules::{collect_module_files, is_module_yaml, module_path_from_file};
use swc_common::{BytePos, Span, Spanned};
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitWith};

const ANYTHING_HOLE_KEYWORD: &str = "ANYTHING";
const EXPR_HOLE_KEYWORD: &str = "EXPR";
const STMT_HOLE_KEYWORD: &str = "STMT";
const CLASS_REST_HOLE_KEYWORD: &str = "CLASS_REST";
const DECLARATORS_HOLE_KEYWORD: &str = "DECLARATORS";
const ARGS_HOLE_KEYWORD: &str = "ARGS";
const OBJECT_PROPS_HOLE_KEYWORD: &str = "OBJECT_PROPS";
const MAX_VAR_GROUP_FRONTIER_TUPLES_PER_DECL: usize = 4096;
const MAX_VAR_FEATURE_SEARCH_NODES: usize = 200_000;

#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum SelectorCodemodRewrite {
    SingleTargetBinding,
    AnythingHoles,
    NameBindingToSourceMatch,
}

impl SelectorCodemodRewrite {
    pub fn name(self) -> &'static str {
        match self {
            Self::SingleTargetBinding => "single_target_binding",
            Self::AnythingHoles => "anything_holes",
            Self::NameBindingToSourceMatch => "name_binding_to_source_match",
        }
    }
}

#[derive(Debug, Clone)]
pub struct SelectorCodemodConfig {
    pub modules_root: PathBuf,
    pub apply: bool,
    pub rewrite: SelectorCodemodRewrite,
    pub minimize_synthesized_selectors: bool,
    pub files: Vec<PathBuf>,
    pub modules: Vec<String>,
    pub module_prefixes: Vec<String>,
    pub source_root: Option<PathBuf>,
    pub chunk: Option<PathBuf>,
    pub source_file: Option<PathBuf>,
    pub items: Vec<String>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum SelectorCodemodAction {
    WouldChange,
    Changed,
    Skipped,
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
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub rewritten_holes: Vec<String>,
    #[serde(default, skip_serializing_if = "is_zero")]
    pub replacement_count: usize,
    pub reason: Option<String>,
}

#[derive(Debug, Clone, Default, Serialize)]
pub struct SelectorCodemodSummary {
    pub dry_run: bool,
    pub files_scanned: usize,
    pub modules_scanned: usize,
    pub members_scanned: usize,
    pub source_match_members: usize,
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
    let synthesis_index = (config.rewrite == SelectorCodemodRewrite::NameBindingToSourceMatch)
        .then(|| load_synthesis_index(config))
        .transpose()?;

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

        let original_text =
            fs::read_to_string(&file).with_context(|| format!("reading {}", file.display()))?;
        let mut doc = yaml_edit::read_yaml(&file)?;
        let mut file_changed = false;
        let mut text_edits = Vec::new();
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
        if config.rewrite == SelectorCodemodRewrite::NameBindingToSourceMatch {
            let selected_exports = selected_item_exports.get(&module);
            if !selected_item_exports.is_empty() && selected_exports.is_none() {
                continue;
            }
            let outcomes = rewrite_name_bindings_to_source_match(
                &module,
                &file,
                root,
                synthesis_index
                    .as_ref()
                    .expect("source-aware rewrite loaded synthesis index"),
                selected_exports,
                &original_text,
                NameBindingRewriteOptions {
                    minimize_synthesized_selectors: config.minimize_synthesized_selectors,
                    apply: config.apply,
                },
            )?;
            text_edits.extend(outcomes.text_edits);
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
        } else {
            let Some(Value::Sequence(members)) = root.get_mut(yk("members")) else {
                continue;
            };
            for (member_index, member) in members.iter_mut().enumerate() {
                summary.members_scanned += 1;
                let candidate = match config.rewrite {
                    SelectorCodemodRewrite::SingleTargetBinding => rewrite_single_target_binding(
                        &module,
                        &file,
                        member_index,
                        member,
                        &original_text,
                        config.apply,
                    ),
                    SelectorCodemodRewrite::AnythingHoles => {
                        rewrite_anything_holes(&module, &file, member_index, member, config.apply)
                            .map(|outcome| outcome.map(SelectorCodemodOutcome::candidate_only))
                    }
                    SelectorCodemodRewrite::NameBindingToSourceMatch => unreachable!(),
                };
                let Some(outcome) = candidate? else {
                    continue;
                };
                if let Some(edit) = outcome.text_edit {
                    text_edits.push(edit);
                }
                let candidate = outcome.candidate;
                if matches!(
                    candidate.action,
                    SelectorCodemodAction::WouldChange | SelectorCodemodAction::Changed
                ) {
                    file_changed = true;
                    summary.changed_candidates += 1;
                } else {
                    summary.skipped_candidates += 1;
                }
                summary.source_match_members += 1;
                candidates.push(candidate);
            }
        }

        if config.apply && file_changed {
            let written = match config.rewrite {
                SelectorCodemodRewrite::SingleTargetBinding => {
                    let body = apply_text_edits(&original_text, &text_edits)
                        .with_context(|| format!("patching {}", file.display()))?;
                    let patched_doc: Value = serde_yaml::from_str(&body)
                        .with_context(|| format!("parsing patched {}", file.display()))?;
                    yaml_edit::write_yaml_body_if_semantic_changed(&file, &patched_doc, body)?
                }
                SelectorCodemodRewrite::AnythingHoles => {
                    yaml_edit::write_yaml_if_semantic_changed(&file, &doc)?
                }
                SelectorCodemodRewrite::NameBindingToSourceMatch => {
                    let body = apply_text_edits(&original_text, &text_edits)
                        .with_context(|| format!("patching {}", file.display()))?;
                    let patched_doc: Value = serde_yaml::from_str(&body)
                        .with_context(|| format!("parsing patched {}", file.display()))?;
                    if patched_doc != doc {
                        bail!(
                            "text-preserving YAML patch for {} did not match semantic rewrite",
                            file.display()
                        );
                    }
                    yaml_edit::write_yaml_body_if_semantic_changed(&file, &patched_doc, body)?
                }
            };
            if written {
                summary.files_written.push(file.display().to_string());
            }
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

struct SelectorCodemodOutcome {
    candidate: SelectorCodemodCandidate,
    text_edit: Option<TextEdit>,
}

impl SelectorCodemodOutcome {
    fn candidate_only(candidate: SelectorCodemodCandidate) -> Self {
        Self {
            candidate,
            text_edit: None,
        }
    }
}

fn rewrite_single_target_binding(
    module: &str,
    file: &Path,
    member_index: usize,
    member: &mut Value,
    source_text: &str,
    apply: bool,
) -> Result<Option<SelectorCodemodOutcome>> {
    let export_name = mapping_get(member, "name").and_then(value_as_string);
    let Some(source_match_value) = mapping_get_mut_path(member, &["selector", "source_match"])
    else {
        return Ok(None);
    };
    let Value::Mapping(source_match_mapping) = source_match_value else {
        return Ok(Some(SelectorCodemodOutcome::candidate_only(
            skipped_candidate(
                module,
                file,
                member_index,
                export_name,
                "selector.source_match is not a mapping",
            ),
        )));
    };
    let source_match: SourceMatch =
        serde_yaml::from_value(Value::Mapping(source_match_mapping.clone()))
            .with_context(|| format!("{module}: members[{member_index}].selector.source_match"))?;
    if source_match.target_binding.is_some() {
        return Ok(Some(SelectorCodemodOutcome::candidate_only(
            SelectorCodemodCandidate {
                module: module.to_string(),
                file: file.display().to_string(),
                member_index,
                export_name,
                action: SelectorCodemodAction::Skipped,
                target_binding: source_match.target_binding,
                declared_bindings: Vec::new(),
                group_id: None,
                matched_body_index: None,
                candidate_count: None,
                rewritten_holes: Vec::new(),
                replacement_count: 0,
                reason: Some("target_binding already present".to_string()),
            },
        )));
    }

    let declared = match source_match::source_match_declared_binding_names(module, &source_match) {
        Ok(declared) => declared,
        Err(err) => {
            return Ok(Some(SelectorCodemodOutcome::candidate_only(
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
                    rewritten_holes: Vec::new(),
                    replacement_count: 0,
                    reason: Some(format!("source_match did not parse: {err:#}")),
                },
            )));
        }
    };

    let [target] = declared.as_slice() else {
        let reason = match declared.len() {
            0 => "source_match declares no top-level bindings".to_string(),
            n => format!("source_match declares {n} top-level bindings"),
        };
        return Ok(Some(SelectorCodemodOutcome::candidate_only(
            SelectorCodemodCandidate {
                module: module.to_string(),
                file: file.display().to_string(),
                member_index,
                export_name,
                action: SelectorCodemodAction::Skipped,
                target_binding: None,
                declared_bindings: declared,
                group_id: None,
                matched_body_index: None,
                candidate_count: None,
                rewritten_holes: Vec::new(),
                replacement_count: 0,
                reason: Some(reason),
            },
        )));
    };

    let insertion = match target_binding_text_insertion(source_text, member_index, target) {
        Ok(insertion) => insertion,
        Err(err) => {
            return Ok(Some(SelectorCodemodOutcome::candidate_only(
                SelectorCodemodCandidate {
                    module: module.to_string(),
                    file: file.display().to_string(),
                    member_index,
                    export_name,
                    action: SelectorCodemodAction::Skipped,
                    target_binding: Some(target.clone()),
                    declared_bindings: declared,
                    group_id: None,
                    matched_body_index: None,
                    candidate_count: None,
                    rewritten_holes: Vec::new(),
                    replacement_count: 0,
                    reason: Some(format!("cannot preserve YAML text edit: {err:#}")),
                },
            )));
        }
    };

    Ok(Some(SelectorCodemodOutcome {
        candidate: SelectorCodemodCandidate {
            module: module.to_string(),
            file: file.display().to_string(),
            member_index,
            export_name,
            action: if apply {
                SelectorCodemodAction::Changed
            } else {
                SelectorCodemodAction::WouldChange
            },
            target_binding: Some(target.clone()),
            declared_bindings: declared,
            group_id: None,
            matched_body_index: None,
            candidate_count: None,
            rewritten_holes: Vec::new(),
            replacement_count: 0,
            reason: None,
        },
        text_edit: apply.then_some(insertion),
    }))
}

fn rewrite_anything_holes(
    module: &str,
    file: &Path,
    member_index: usize,
    member: &mut Value,
    apply: bool,
) -> Result<Option<SelectorCodemodCandidate>> {
    let export_name = mapping_get(member, "name").and_then(value_as_string);
    let Some(source_match_value) = mapping_get_mut_path(member, &["selector", "source_match"])
    else {
        return Ok(None);
    };
    let Value::Mapping(source_match_mapping) = source_match_value else {
        return Ok(Some(skipped_candidate(
            module,
            file,
            member_index,
            export_name,
            "selector.source_match is not a mapping",
        )));
    };
    let Some(match_source_value) = source_match_mapping.get_mut(yk("match")) else {
        return Ok(Some(skipped_candidate(
            module,
            file,
            member_index,
            export_name,
            "selector.source_match.match is missing",
        )));
    };
    let Some(match_source) = match_source_value.as_str() else {
        return Ok(Some(skipped_candidate(
            module,
            file,
            member_index,
            export_name,
            "selector.source_match.match is not a string",
        )));
    };
    let replacements = match anonymous_typed_hole_replacements(module, member_index, match_source) {
        Ok(replacements) => replacements,
        Err(err) => {
            return Ok(Some(SelectorCodemodCandidate {
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
                rewritten_holes: Vec::new(),
                replacement_count: 0,
                reason: Some(format!("source_match did not parse: {err:#}")),
            }));
        }
    };
    if replacements.is_empty() {
        return Ok(Some(skipped_candidate(
            module,
            file,
            member_index,
            export_name,
            "no anonymous typed holes can be normalized to ANYTHING",
        )));
    }

    let rewritten_holes = replacements
        .iter()
        .map(|replacement| replacement.from.clone())
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect::<Vec<_>>();
    let replacement_count = replacements.len();
    let rewritten = apply_source_replacements(match_source, &replacements);
    if apply {
        *match_source_value = Value::String(rewritten);
    }
    Ok(Some(SelectorCodemodCandidate {
        module: module.to_string(),
        file: file.display().to_string(),
        member_index,
        export_name,
        action: if apply {
            SelectorCodemodAction::Changed
        } else {
            SelectorCodemodAction::WouldChange
        },
        target_binding: None,
        declared_bindings: Vec::new(),
        group_id: None,
        matched_body_index: None,
        candidate_count: None,
        rewritten_holes,
        replacement_count,
        reason: None,
    }))
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
    text_edits: Vec<TextEdit>,
}

#[derive(Debug, Clone)]
struct NameBindingMember {
    member_index: usize,
    export_name: String,
    binding_name: String,
    comment: Option<String>,
}

#[derive(Debug, Clone, Copy)]
struct NameBindingRewriteOptions {
    minimize_synthesized_selectors: bool,
    apply: bool,
}

#[derive(Debug, Clone)]
pub struct SynthesizedSelectorGroup {
    body_idx: usize,
    target_bindings: Vec<SynthesizedTargetBinding>,
    match_source: String,
    rewritten_holes: Vec<String>,
    candidate_count: usize,
}

#[derive(Debug, Clone)]
pub struct SynthesizedTargetBinding {
    export_name: String,
    runtime_binding: String,
}

/// Indexed source facts for selector synthesis.
///
/// The target architecture is a trie/lattice of stable AST discriminants:
/// declaration kind, wrapper shape, initializer kind, callee/member paths,
/// object keys, literal atoms, class/function names, and declarator slots.
/// Synthesis can then ask for the smallest feature path whose candidate set is
/// singleton and render everything else as selector holes. This first slice
/// builds the binding-to-declaration and declarator-slot index needed for exact
/// function/class recovery plus multi-declarator var gap minimization.
struct ChunkSelectorIndex {
    parsed: js_ast::ParsedJsModule,
    source: String,
    decls: Vec<IndexedDeclaration>,
    binding_to_decl: BTreeMap<String, Vec<usize>>,
    feature_to_body_indices: BTreeMap<SelectorAnchorFeature, BTreeSet<usize>>,
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

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
enum SelectorAnchorFeature {
    DeclarationKind(IndexedDeclarationKind),
    VarKind(String),
    FunctionArity(usize),
    ClassMember(String),
    ObjectKey(String),
    CallCallee(String),
    StringLiteral(String),
    NumberLiteral(String),
    BoolLiteral(bool),
    NullLiteral,
}

#[derive(Debug)]
struct IndexedBinding {
    name: String,
    declarator_idx: Option<usize>,
}

fn rewrite_name_bindings_to_source_match(
    module: &str,
    file: &Path,
    root: &mut serde_yaml::Mapping,
    index: &ChunkSelectorIndex,
    selected_exports: Option<&BTreeSet<String>>,
    source_text: &str,
    options: NameBindingRewriteOptions,
) -> Result<NameBindingRewriteOutcomes> {
    let Some(Value::Sequence(members)) = root.get_mut(yk("members")) else {
        return Ok(NameBindingRewriteOutcomes {
            candidates: Vec::new(),
            members_scanned: 0,
            members_seen: 0,
            groups_changed: 0,
            text_edits: Vec::new(),
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
                comment: mapping_get(member, "comment").and_then(value_as_string),
            });
    }

    let mut replacements: BTreeMap<usize, Option<Value>> = BTreeMap::new();
    let mut binding_groups = Vec::new();
    let mut removed_group_member_indices = BTreeSet::new();
    let mut text_edits = Vec::new();
    let mut groups_changed = 0;
    for (group_id, (decl_idx, group_members)) in grouped.into_iter().enumerate() {
        let synthesized = match synthesize_simplest_selector_for_group(
            index,
            decl_idx,
            &group_members,
            options.minimize_synthesized_selectors,
        ) {
            Ok(synthesized) => synthesized,
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
                continue;
            }
        };
        groups_changed += 1;
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
                let edit = source_match_selector_text_edit(
                    source_text,
                    member.member_index,
                    &synthesized.match_source,
                    &target.export_name,
                )
                .with_context(|| {
                    format!(
                        "cannot preserve YAML text edit for {module}:{}",
                        member.export_name
                    )
                })?;
                let replacement = member_with_source_match(
                    &member.export_name,
                    member.comment.clone(),
                    &synthesized.match_source,
                    &target.export_name,
                );
                replacements.insert(member.member_index, Some(replacement));
                text_edits.push(edit);
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
                    replacements.insert(member.member_index, None);
                    removed_group_member_indices.insert(member.member_index);
                }
            }
            if options.apply {
                binding_groups.push(binding_group_value(&synthesized, &group_members));
            }
        }
    }

    if options.apply {
        if !binding_groups.is_empty() {
            text_edits.extend(binding_group_text_edits(
                source_text,
                members.len(),
                &removed_group_member_indices,
                &binding_groups,
            )?);
        }
        apply_member_replacements(members, replacements);
        if !binding_groups.is_empty() {
            let entry = root
                .entry(yk("binding_groups"))
                .or_insert_with(|| Value::Sequence(Vec::new()));
            match entry {
                Value::Sequence(existing) => existing.extend(binding_groups),
                _ => bail!("binding_groups exists but is not a sequence"),
            }
        }
    }

    Ok(NameBindingRewriteOutcomes {
        candidates,
        members_scanned,
        members_seen,
        groups_changed,
        text_edits,
    })
}

fn load_synthesis_index(config: &SelectorCodemodConfig) -> Result<ChunkSelectorIndex> {
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
    let parsed = js_ast::parse_js_module_consuming(&source_file.display().to_string(), source)
        .with_context(|| format!("parsing source file {}", source_file.display()))?;
    Ok(ChunkSelectorIndex::new(parsed))
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

impl ChunkSelectorIndex {
    fn new(parsed: js_ast::ParsedJsModule) -> Self {
        let source = parsed.source_text();
        let mut decls = Vec::new();
        let mut binding_to_decl: BTreeMap<String, Vec<usize>> = BTreeMap::new();
        let mut feature_to_body_indices: BTreeMap<SelectorAnchorFeature, BTreeSet<usize>> =
            BTreeMap::new();
        for (body_idx, item) in parsed.module.body.iter().enumerate() {
            let indexed = IndexedDeclaration::from_item(body_idx, item);
            if indexed.declared_bindings.is_empty() {
                continue;
            }
            for feature in selector_anchor_features_for_item(item, indexed.kind) {
                feature_to_body_indices
                    .entry(feature)
                    .or_default()
                    .insert(body_idx);
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
        Self {
            parsed,
            source,
            decls,
            binding_to_decl,
            feature_to_body_indices,
        }
    }

    fn frontier_for_features(&self, features: &BTreeSet<SelectorAnchorFeature>) -> BTreeSet<usize> {
        let mut postings = features
            .iter()
            .filter_map(|feature| {
                self.feature_to_body_indices
                    .get(feature)
                    .map(|body_indices| (feature, body_indices))
            })
            .collect::<Vec<_>>();
        postings.sort_by_key(|(_, body_indices)| body_indices.len());
        let Some((_, first)) = postings.first() else {
            return self.decls.iter().map(|decl| decl.body_idx).collect();
        };
        let mut frontier = (*first).clone();
        for (_, body_indices) in postings.into_iter().skip(1) {
            frontier = frontier
                .intersection(body_indices)
                .copied()
                .collect::<BTreeSet<_>>();
            if frontier.is_empty() {
                break;
            }
        }
        frontier
    }
}

impl IndexedDeclaration {
    fn from_item(body_idx: usize, item: &ModuleItem) -> Self {
        let (kind, declared_bindings) = match item_decl(item) {
            Some(Decl::Fn(function)) => (
                IndexedDeclarationKind::Function,
                vec![IndexedBinding {
                    name: function.ident.sym.to_string(),
                    declarator_idx: None,
                }],
            ),
            Some(Decl::Class(class)) => (
                IndexedDeclarationKind::Class,
                vec![IndexedBinding {
                    name: class.ident.sym.to_string(),
                    declarator_idx: None,
                }],
            ),
            Some(Decl::Var(var)) => (
                IndexedDeclarationKind::Var,
                var.decls
                    .iter()
                    .enumerate()
                    .flat_map(|(declarator_idx, declarator)| {
                        binding_names_for_pat(&declarator.name)
                            .into_iter()
                            .map(move |name| IndexedBinding {
                                name,
                                declarator_idx: Some(declarator_idx),
                            })
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

fn synthesize_simplest_selector_for_group(
    index: &ChunkSelectorIndex,
    decl_idx: usize,
    members: &[NameBindingMember],
    minimize_synthesized_selectors: bool,
) -> Result<SynthesizedSelectorGroup> {
    let decl = index
        .decls
        .get(decl_idx)
        .with_context(|| format!("missing indexed declaration {decl_idx}"))?;
    let item = index
        .parsed
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
    let exact_selector = || {
        render_exact_selector_for_group(index, item, decl, &targets).map(|(source, holes)| {
            (
                trim_selector_source_line_suffixes(&source),
                holes.into_iter().collect::<BTreeSet<_>>(),
            )
        })
    };
    let specialized = minimize_synthesized_selectors
        .then(|| synthesize_specialized_selector(index, item, decl, &targets))
        .transpose()?
        .flatten();
    let (match_source, rewritten_holes, candidate_count) = if let Some(specialized) = specialized {
        let match_source = trim_selector_source_line_suffixes(&specialized.match_source);
        match prove_synthesized_selector(index, decl, &targets, &match_source) {
            Ok(candidate_count) => (match_source, specialized.rewritten_holes, candidate_count),
            Err(_) => {
                let (exact_source, exact_holes) = exact_selector()?;
                let candidate_count =
                    prove_synthesized_selector(index, decl, &targets, &exact_source)
                        .context("proving exact synthesized selector fallback")?;
                (exact_source, exact_holes, candidate_count)
            }
        }
    } else {
        let (exact_source, exact_holes) = exact_selector()?;
        let candidate_count = prove_synthesized_selector(index, decl, &targets, &exact_source)
            .context("proving exact synthesized selector")?;
        (exact_source, exact_holes, candidate_count)
    };

    Ok(SynthesizedSelectorGroup {
        body_idx: decl.body_idx,
        target_bindings: targets,
        match_source,
        rewritten_holes: rewritten_holes.into_iter().collect(),
        candidate_count,
    })
}

fn prove_synthesized_selector(
    index: &ChunkSelectorIndex,
    decl: &IndexedDeclaration,
    targets: &[SynthesizedTargetBinding],
    match_source: &str,
) -> Result<usize> {
    if targets.is_empty() {
        bail!("selector synthesis group has no targets");
    }
    if targets.len() > 1 {
        let selector = SourceMatch {
            match_source: match_source.to_string(),
            identifiers: SourceMatchIdentifierMode::AlphaAll,
            target_binding: None,
            target_statement: None,
            target_statements: None,
            wildcard_string_literals: BTreeSet::new(),
        }
        .selector();
        let exports_by_target = targets
            .iter()
            .map(|target| (target.export_name.clone(), target.export_name.clone()))
            .collect::<BTreeMap<_, _>>();
        let matched = source_match::resolve_member_binding_group_match(
            &index.parsed.module,
            "<selector synthesis>",
            &selector,
            &exports_by_target,
        )?;
        if matched.body_idx != decl.body_idx {
            bail!(
                "synthesized selector matched body index {} instead of intended {}",
                matched.body_idx,
                decl.body_idx
            );
        }
        for target in targets {
            let binding = matched.bindings.get(&target.export_name).with_context(|| {
                format!(
                    "synthesized selector target `{}` did not resolve a binding",
                    target.export_name
                )
            })?;
            if binding.binding_name != target.runtime_binding {
                bail!(
                    "synthesized selector target `{}` resolved `{}` instead of intended `{}`",
                    target.export_name,
                    binding.binding_name,
                    target.runtime_binding
                );
            }
        }
        return Ok(1);
    }

    let [target] = targets else {
        unreachable!("target length already handled")
    };
    let source_match = SourceMatch {
        match_source: match_source.to_string(),
        identifiers: SourceMatchIdentifierMode::AlphaAll,
        target_binding: Some(target.export_name.clone()),
        target_statement: None,
        target_statements: None,
        wildcard_string_literals: BTreeSet::new(),
    };
    let selector = source_match.selector();
    let matches = source_match::member_binding_candidate_matches(
        &index.parsed.module,
        "<selector synthesis>",
        &selector,
    )?;
    let candidate_count = matches.len();
    let [candidate] = matches.as_slice() else {
        bail!("synthesized selector matched {candidate_count} candidate declaration groups");
    };
    if candidate.body_idx != decl.body_idx {
        bail!(
            "synthesized selector matched body index {} instead of intended {}",
            candidate.body_idx,
            decl.body_idx
        );
    };
    if candidate.binding.binding_name != target.runtime_binding {
        bail!(
            "synthesized selector target `{}` resolved `{}` instead of intended `{}`",
            target.export_name,
            candidate.binding.binding_name,
            target.runtime_binding
        );
    }
    Ok(candidate_count)
}

struct SpecializedSelector {
    match_source: String,
    rewritten_holes: BTreeSet<String>,
}

fn render_exact_selector_for_group(
    index: &ChunkSelectorIndex,
    item: &ModuleItem,
    decl: &IndexedDeclaration,
    targets: &[SynthesizedTargetBinding],
) -> Result<(String, Vec<String>)> {
    match decl.kind {
        IndexedDeclarationKind::Var => render_var_group_selector(index, item, decl, targets),
        IndexedDeclarationKind::Function | IndexedDeclarationKind::Class => {
            let target = targets
                .first()
                .context("function/class synthesis requires one target")?;
            if targets.len() != 1 {
                bail!("grouped function/class declarations are not supported");
            }
            Ok((
                render_single_binding_item_selector(
                    index,
                    item,
                    &target.runtime_binding,
                    &target.export_name,
                )?,
                Vec::new(),
            ))
        }
        IndexedDeclarationKind::Other => {
            bail!("unsupported declaration kind for selector synthesis")
        }
    }
}

fn synthesize_specialized_selector(
    index: &ChunkSelectorIndex,
    item: &ModuleItem,
    decl: &IndexedDeclaration,
    targets: &[SynthesizedTargetBinding],
) -> Result<Option<SpecializedSelector>> {
    match decl.kind {
        IndexedDeclarationKind::Function => {
            synthesize_specialized_function_selector(index, item, decl, targets)
        }
        IndexedDeclarationKind::Class => {
            synthesize_specialized_class_selector(index, item, decl, targets)
        }
        IndexedDeclarationKind::Var => {
            synthesize_specialized_var_selector(index, item, decl, targets)
        }
        IndexedDeclarationKind::Other => Ok(None),
    }
}

fn synthesize_specialized_function_selector(
    index: &ChunkSelectorIndex,
    item: &ModuleItem,
    decl: &IndexedDeclaration,
    targets: &[SynthesizedTargetBinding],
) -> Result<Option<SpecializedSelector>> {
    let [target] = targets else {
        return Ok(None);
    };
    let Some(Decl::Fn(function)) = item_decl(item) else {
        return Ok(None);
    };
    let mut features = BTreeSet::from([
        SelectorAnchorFeature::DeclarationKind(IndexedDeclarationKind::Function),
        SelectorAnchorFeature::FunctionArity(function.function.params.len()),
    ]);
    if !frontier_is_small_and_contains_target(index, decl, &features) {
        return Ok(None);
    }
    let params = function
        .function
        .params
        .iter()
        .map(|_| ANYTHING_HOLE_KEYWORD)
        .collect::<Vec<_>>()
        .join(", ");
    features.insert(SelectorAnchorFeature::DeclarationKind(
        IndexedDeclarationKind::Function,
    ));
    Ok(Some(SpecializedSelector {
        match_source: format!(
            "function {}({params}) {{\n  STMT_LIST;\n}}",
            target.export_name
        ),
        rewritten_holes: BTreeSet::from([
            ANYTHING_HOLE_KEYWORD.to_string(),
            "STMT_LIST".to_string(),
        ]),
    }))
}

fn synthesize_specialized_class_selector(
    index: &ChunkSelectorIndex,
    item: &ModuleItem,
    decl: &IndexedDeclaration,
    targets: &[SynthesizedTargetBinding],
) -> Result<Option<SpecializedSelector>> {
    let [target] = targets else {
        return Ok(None);
    };
    let Some(Decl::Class(class_decl)) = item_decl(item) else {
        return Ok(None);
    };
    let mut features = BTreeSet::from([SelectorAnchorFeature::DeclarationKind(
        IndexedDeclarationKind::Class,
    )]);
    if frontier_is_small_and_contains_target(index, decl, &features) {
        return Ok(Some(SpecializedSelector {
            match_source: format!("class {} {{\n  CLASS_REST;\n}}", target.export_name),
            rewritten_holes: BTreeSet::from(["CLASS_REST".to_string()]),
        }));
    }

    let mut member_names = class_decl
        .class
        .body
        .iter()
        .filter_map(class_member_feature_name)
        .collect::<Vec<_>>();
    member_names.sort_by_key(|name| {
        index
            .feature_to_body_indices
            .get(&SelectorAnchorFeature::ClassMember(name.clone()))
            .map_or(usize::MAX, BTreeSet::len)
    });
    let mut selected = BTreeSet::new();
    for name in member_names {
        features.insert(SelectorAnchorFeature::ClassMember(name.clone()));
        selected.insert(name);
        if frontier_is_small_and_contains_target(index, decl, &features) {
            let members = render_class_member_skeletons(&class_decl.class, &selected, index)?;
            return Ok(Some(SpecializedSelector {
                match_source: format!("class {} {{\n{members}}}", target.export_name),
                rewritten_holes: BTreeSet::from([
                    ANYTHING_HOLE_KEYWORD.to_string(),
                    "STMT_LIST".to_string(),
                    "CLASS_REST".to_string(),
                ]),
            }));
        }
    }
    Ok(None)
}

fn synthesize_specialized_var_selector(
    index: &ChunkSelectorIndex,
    item: &ModuleItem,
    decl: &IndexedDeclaration,
    targets: &[SynthesizedTargetBinding],
) -> Result<Option<SpecializedSelector>> {
    let var = item_var_decl(item).context("indexed var declaration no longer has var AST")?;
    let target_slots = targets
        .iter()
        .map(|target| {
            decl.declared_bindings
                .iter()
                .find(|binding| binding.name == target.runtime_binding)
                .and_then(|binding| binding.declarator_idx)
                .with_context(|| {
                    format!(
                        "target `{}` is not a var declarator",
                        target.runtime_binding
                    )
                })
        })
        .collect::<Result<Vec<_>>>()?;
    if !var_group_frontier_is_bounded(index, var.kind, targets.len()) {
        return Ok(None);
    }
    let target_decl_indices = target_slots.iter().copied().collect::<BTreeSet<_>>();
    let mut slot_constraints = vec![BTreeSet::new(); targets.len()];
    let mut frontier = var_group_frontier(index, var.kind, targets.len(), &slot_constraints);
    if !target_tuple_is_unique(&frontier, decl, &target_slots) {
        slot_constraints = select_min_cost_var_slot_constraints(
            index,
            var.kind,
            targets.len(),
            decl,
            &target_slots,
        )
        .unwrap_or_default();
        frontier = var_group_frontier(index, var.kind, targets.len(), &slot_constraints);
        if !target_tuple_is_unique(&frontier, decl, &target_slots) {
            return Ok(None);
        }
    }

    let export_by_runtime = targets
        .iter()
        .map(|target| (target.runtime_binding.as_str(), target.export_name.as_str()))
        .collect::<BTreeMap<_, _>>();
    let mut parts = Vec::new();
    let mut holes = BTreeSet::new();
    let mut skipped_run = 0usize;
    let mut target_seen = 0usize;
    for (idx, declarator) in var.decls.iter().enumerate() {
        if !target_decl_indices.contains(&idx) {
            skipped_run += 1;
            continue;
        }
        if skipped_run > 0 {
            let hole = declarator_hole_name(target_seen, target_decl_indices.len());
            parts.push(format!("{hole} = null"));
            holes.insert(hole.to_string());
            skipped_run = 0;
        }
        let runtime_name = single_ident_pat_name(&declarator.name)
            .context("only identifier var declarators can be synthesized today")?;
        let export_name = export_by_runtime
            .get(runtime_name)
            .copied()
            .context("target declarator missing export name")?;
        let init = declarator
            .init
            .as_deref()
            .map(|expr| {
                render_specialized_expr_selector(index, expr, &slot_constraints[target_seen])
            })
            .transpose()?
            .unwrap_or_else(anything_expr_selector);
        holes.extend(init.holes.clone());
        parts.push(format!("{export_name} = {}", init.source));
        target_seen += 1;
    }
    if skipped_run > 0 {
        let hole = declarator_hole_name(target_seen, target_decl_indices.len());
        parts.push(format!("{hole} = null"));
        holes.insert(hole.to_string());
    }
    Ok(Some(SpecializedSelector {
        match_source: format!("{} {};", var_kind_label(var.kind), parts.join(",\n  ")),
        rewritten_holes: holes,
    }))
}

fn frontier_is_small_and_contains_target(
    index: &ChunkSelectorIndex,
    decl: &IndexedDeclaration,
    features: &BTreeSet<SelectorAnchorFeature>,
) -> bool {
    let frontier = index.frontier_for_features(features);
    frontier.contains(&decl.body_idx) && frontier.len() == 1
}

fn select_min_cost_var_slot_constraints(
    index: &ChunkSelectorIndex,
    var_kind: VarDeclKind,
    target_count: usize,
    decl: &IndexedDeclaration,
    target_slots: &[usize],
) -> Option<Vec<BTreeSet<SelectorAnchorFeature>>> {
    let empty_constraints = vec![BTreeSet::new(); target_count];
    let tuples =
        var_group_frontier_with_features(index, var_kind, target_count, &empty_constraints);
    let target_tuple = VarGroupTuple {
        body_idx: decl.body_idx,
        slots: target_slots.to_vec(),
    };
    let target_tuple_idx = tuples
        .iter()
        .position(|tuple| tuple.tuple == target_tuple)?;
    let competitors = tuples
        .iter()
        .enumerate()
        .filter_map(|(tuple_idx, _)| (tuple_idx != target_tuple_idx).then_some(tuple_idx))
        .collect::<BTreeSet<_>>();
    if competitors.is_empty() {
        return Some(empty_constraints);
    }

    let options = var_slot_feature_options(&tuples[target_tuple_idx], target_count);
    if options.is_empty() {
        return None;
    }
    let option_covers = options
        .iter()
        .map(|option| {
            competitors
                .iter()
                .filter_map(|tuple_idx| {
                    (!var_group_tuple_has_slot_feature(&tuples[*tuple_idx], option))
                        .then_some(*tuple_idx)
                })
                .collect::<BTreeSet<_>>()
        })
        .collect::<Vec<_>>();
    let usable_options = options
        .into_iter()
        .zip(option_covers)
        .filter(|(_, covered)| !covered.is_empty())
        .map(|(option, covered)| VarSlotSearchOption { option, covered })
        .collect::<Vec<_>>();
    if usable_options.is_empty() {
        return None;
    }

    let mut search = VarSlotConstraintSearch::new(usable_options, competitors);
    search.solve().map(|selected| {
        let mut slot_constraints = vec![BTreeSet::new(); target_count];
        for option_idx in selected {
            let option = &search.options[option_idx].option;
            slot_constraints[option.target_pos].insert(option.feature.clone());
        }
        slot_constraints
    })
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
struct VarSlotFeatureOption {
    target_pos: usize,
    feature: SelectorAnchorFeature,
}

#[derive(Debug)]
struct VarSlotSearchOption {
    option: VarSlotFeatureOption,
    covered: BTreeSet<usize>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
struct SelectorSearchCost {
    weighted_features: usize,
    feature_count: usize,
}

impl SelectorSearchCost {
    const ZERO: Self = Self {
        weighted_features: 0,
        feature_count: 0,
    };

    fn add(self, feature: &SelectorAnchorFeature) -> Self {
        Self {
            weighted_features: self.weighted_features + feature_cost(feature),
            feature_count: self.feature_count + 1,
        }
    }
}

fn var_slot_feature_options(
    target_tuple: &VarGroupTupleWithFeatures,
    target_count: usize,
) -> Vec<VarSlotFeatureOption> {
    (0..target_count)
        .flat_map(|target_pos| {
            target_tuple
                .slot_features
                .get(target_pos)
                .cloned()
                .unwrap_or_default()
                .into_iter()
                .map(move |feature| VarSlotFeatureOption {
                    target_pos,
                    feature,
                })
        })
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect()
}

fn var_group_tuple_has_slot_feature(
    tuple: &VarGroupTupleWithFeatures,
    option: &VarSlotFeatureOption,
) -> bool {
    tuple
        .slot_features
        .get(option.target_pos)
        .is_some_and(|features| features.contains(&option.feature))
}

struct VarSlotConstraintSearch {
    options: Vec<VarSlotSearchOption>,
    uncovered_initial: BTreeSet<usize>,
    options_covering_competitor: BTreeMap<usize, Vec<usize>>,
    best: Option<(SelectorSearchCost, Vec<usize>)>,
    memo: BTreeMap<BTreeSet<usize>, SelectorSearchCost>,
    nodes: usize,
}

impl VarSlotConstraintSearch {
    fn new(options: Vec<VarSlotSearchOption>, uncovered_initial: BTreeSet<usize>) -> Self {
        let mut options_covering_competitor: BTreeMap<usize, Vec<usize>> = BTreeMap::new();
        for (option_idx, option) in options.iter().enumerate() {
            for tuple_idx in &option.covered {
                options_covering_competitor
                    .entry(*tuple_idx)
                    .or_default()
                    .push(option_idx);
            }
        }
        for option_indices in options_covering_competitor.values_mut() {
            option_indices
                .sort_by(|a, b| option_sort_key(&options[*a]).cmp(&option_sort_key(&options[*b])));
        }
        Self {
            options,
            uncovered_initial,
            options_covering_competitor,
            best: None,
            memo: BTreeMap::new(),
            nodes: 0,
        }
    }

    fn solve(&mut self) -> Option<Vec<usize>> {
        let greedy = self.greedy_upper_bound()?;
        self.best = Some(greedy);
        self.branch(
            self.uncovered_initial.clone(),
            Vec::new(),
            BTreeSet::new(),
            SelectorSearchCost::ZERO,
        );
        self.best.as_ref().map(|(_, selected)| selected.clone())
    }

    fn greedy_upper_bound(&self) -> Option<(SelectorSearchCost, Vec<usize>)> {
        let mut uncovered = self.uncovered_initial.clone();
        let mut selected = Vec::new();
        let mut selected_set = BTreeSet::new();
        let mut cost = SelectorSearchCost::ZERO;
        while !uncovered.is_empty() {
            let (option_idx, _) = self
                .options
                .iter()
                .enumerate()
                .filter(|(option_idx, _)| !selected_set.contains(option_idx))
                .map(|(option_idx, option)| {
                    let covered_count = option.covered.intersection(&uncovered).count();
                    (option_idx, covered_count)
                })
                .filter(|(_, covered_count)| *covered_count > 0)
                .max_by(|(a_idx, a_covered), (b_idx, b_covered)| {
                    a_covered.cmp(b_covered).then_with(|| {
                        option_sort_key(&self.options[*b_idx])
                            .cmp(&option_sort_key(&self.options[*a_idx]))
                    })
                })?;
            selected_set.insert(option_idx);
            selected.push(option_idx);
            cost = cost.add(&self.options[option_idx].option.feature);
            uncovered = uncovered
                .difference(&self.options[option_idx].covered)
                .copied()
                .collect();
        }
        selected.sort_by(|a, b| {
            option_sort_key(&self.options[*a]).cmp(&option_sort_key(&self.options[*b]))
        });
        Some((cost, selected))
    }

    fn branch(
        &mut self,
        uncovered: BTreeSet<usize>,
        selected: Vec<usize>,
        selected_set: BTreeSet<usize>,
        cost: SelectorSearchCost,
    ) {
        self.nodes += 1;
        if self.nodes > MAX_VAR_FEATURE_SEARCH_NODES {
            return;
        }
        if let Some((best_cost, _)) = &self.best
            && cost >= *best_cost
        {
            return;
        }
        if uncovered.is_empty() {
            let mut normalized = selected;
            normalized.sort_by(|a, b| {
                option_sort_key(&self.options[*a]).cmp(&option_sort_key(&self.options[*b]))
            });
            let replace = self.best.as_ref().is_none_or(|(best_cost, best_selected)| {
                cost < *best_cost
                    || (cost == *best_cost
                        && selected_sort_key(&self.options, &normalized)
                            < selected_sort_key(&self.options, best_selected))
            });
            if replace {
                self.best = Some((cost, normalized));
            }
            return;
        }
        if self
            .memo
            .get(&uncovered)
            .is_some_and(|seen_cost| *seen_cost <= cost)
        {
            return;
        }
        self.memo.insert(uncovered.clone(), cost);

        let Some(pivot) = self.best_pivot_competitor(&uncovered, &selected_set) else {
            return;
        };
        let Some(option_indices) = self.options_covering_competitor.get(&pivot).cloned() else {
            return;
        };
        for option_idx in option_indices {
            if selected_set.contains(&option_idx) {
                continue;
            }
            let option = &self.options[option_idx];
            let new_uncovered = uncovered
                .difference(&option.covered)
                .copied()
                .collect::<BTreeSet<_>>();
            if new_uncovered.len() == uncovered.len() {
                continue;
            }
            let new_cost = cost.add(&option.option.feature);
            if let Some((best_cost, _)) = &self.best
                && new_cost >= *best_cost
            {
                continue;
            }
            let mut new_selected = selected.clone();
            new_selected.push(option_idx);
            let mut new_selected_set = selected_set.clone();
            new_selected_set.insert(option_idx);
            self.branch(new_uncovered, new_selected, new_selected_set, new_cost);
        }
    }

    fn best_pivot_competitor(
        &self,
        uncovered: &BTreeSet<usize>,
        selected_set: &BTreeSet<usize>,
    ) -> Option<usize> {
        uncovered
            .iter()
            .filter_map(|tuple_idx| {
                let covering_options = self.options_covering_competitor.get(tuple_idx)?;
                let available_count = covering_options
                    .iter()
                    .filter(|option_idx| !selected_set.contains(option_idx))
                    .count();
                (available_count > 0).then_some((*tuple_idx, available_count))
            })
            .min_by_key(|(tuple_idx, available_count)| (*available_count, *tuple_idx))
            .map(|(tuple_idx, _)| tuple_idx)
    }
}

fn option_sort_key(option: &VarSlotSearchOption) -> (usize, usize, usize, &SelectorAnchorFeature) {
    (
        feature_cost(&option.option.feature),
        option.option.target_pos,
        usize::MAX - option.covered.len(),
        &option.option.feature,
    )
}

fn selected_sort_key<'a>(
    options: &'a [VarSlotSearchOption],
    selected: &'a [usize],
) -> Vec<(usize, usize, usize, &'a SelectorAnchorFeature)> {
    selected
        .iter()
        .map(|option_idx| option_sort_key(&options[*option_idx]))
        .collect()
}

fn feature_cost(feature: &SelectorAnchorFeature) -> usize {
    match feature {
        SelectorAnchorFeature::DeclarationKind(_)
        | SelectorAnchorFeature::VarKind(_)
        | SelectorAnchorFeature::FunctionArity(_) => 0,
        SelectorAnchorFeature::ObjectKey(_) | SelectorAnchorFeature::ClassMember(_) => 1,
        SelectorAnchorFeature::CallCallee(_) => 2,
        SelectorAnchorFeature::BoolLiteral(_) | SelectorAnchorFeature::NullLiteral => 3,
        SelectorAnchorFeature::StringLiteral(_) | SelectorAnchorFeature::NumberLiteral(_) => 4,
    }
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
struct VarGroupTuple {
    body_idx: usize,
    slots: Vec<usize>,
}

#[derive(Debug, Clone)]
struct VarGroupTupleWithFeatures {
    tuple: VarGroupTuple,
    slot_features: Vec<BTreeSet<SelectorAnchorFeature>>,
}

fn selector_anchor_features_for_item(
    item: &ModuleItem,
    kind: IndexedDeclarationKind,
) -> BTreeSet<SelectorAnchorFeature> {
    let mut features = BTreeSet::from([SelectorAnchorFeature::DeclarationKind(kind)]);
    match item_decl(item) {
        Some(Decl::Fn(function)) => {
            features.insert(SelectorAnchorFeature::FunctionArity(
                function.function.params.len(),
            ));
        }
        Some(Decl::Var(var)) => {
            features.insert(SelectorAnchorFeature::VarKind(
                var_kind_label(var.kind).to_string(),
            ));
        }
        _ => {}
    }
    let mut collector = SelectorAnchorFeatureCollector::default();
    item.visit_with(&mut collector);
    features.extend(collector.features);
    features
}

#[derive(Default)]
struct SelectorAnchorFeatureCollector {
    features: BTreeSet<SelectorAnchorFeature>,
}

impl Visit for SelectorAnchorFeatureCollector {
    fn visit_expr(&mut self, expr: &Expr) {
        if is_hole_expr(expr) {
            return;
        }
        collect_expr_anchor_features(expr, &mut self.features);
        expr.visit_children_with(self);
    }

    fn visit_prop_or_spread(&mut self, prop_or_spread: &PropOrSpread) {
        if is_object_property_hole(prop_or_spread) {
            return;
        }
        if let PropOrSpread::Prop(prop) = prop_or_spread
            && let Some(name) = prop_key_name(prop.as_ref())
        {
            self.features.insert(SelectorAnchorFeature::ObjectKey(name));
        }
        prop_or_spread.visit_children_with(self);
    }

    fn visit_class_member(&mut self, member: &ClassMember) {
        if is_class_rest_hole_member(member) {
            return;
        }
        if let Some(name) = class_member_feature_name(member) {
            self.features
                .insert(SelectorAnchorFeature::ClassMember(name));
        }
        member.visit_children_with(self);
    }
}

fn collect_expr_anchor_features(expr: &Expr, features: &mut BTreeSet<SelectorAnchorFeature>) {
    if is_hole_expr(expr) {
        return;
    }
    match expr {
        Expr::Call(call) => {
            if let Some(callee) = call_callee_feature_name(&call.callee) {
                features.insert(SelectorAnchorFeature::CallCallee(callee));
            }
            for arg in &call.args {
                collect_expr_anchor_features(&arg.expr, features);
            }
        }
        Expr::Object(object) => {
            for prop in &object.props {
                if let PropOrSpread::Prop(prop) = prop
                    && let Some(name) = prop_key_name(prop.as_ref())
                {
                    features.insert(SelectorAnchorFeature::ObjectKey(name));
                }
            }
            let mut collector = SelectorAnchorFeatureCollector::default();
            object.visit_children_with(&mut collector);
            features.extend(collector.features);
        }
        Expr::Lit(lit) => match lit {
            Lit::Str(value) => {
                features.insert(SelectorAnchorFeature::StringLiteral(
                    value.value.to_string_lossy().to_string(),
                ));
            }
            Lit::Num(value) => {
                features.insert(SelectorAnchorFeature::NumberLiteral(
                    value.value.to_string(),
                ));
            }
            Lit::Bool(value) => {
                features.insert(SelectorAnchorFeature::BoolLiteral(value.value));
            }
            Lit::Null(_) => {
                features.insert(SelectorAnchorFeature::NullLiteral);
            }
            _ => {}
        },
        _ => {
            let mut collector = SelectorAnchorFeatureCollector::default();
            expr.visit_children_with(&mut collector);
            features.extend(collector.features);
        }
    }
}

fn collect_renderable_var_anchor_features(
    expr: &Expr,
    features: &mut BTreeSet<SelectorAnchorFeature>,
) {
    if is_hole_expr(expr) {
        return;
    }
    match expr {
        Expr::Call(call) => {
            if let Some(callee) = call_callee_feature_name(&call.callee) {
                features.insert(SelectorAnchorFeature::CallCallee(callee));
            }
            for arg in &call.args {
                if let Expr::Object(object) = arg.expr.as_ref() {
                    collect_direct_object_key_features(object, features);
                }
            }
        }
        Expr::Object(object) => collect_direct_object_key_features(object, features),
        _ => {}
    }
}

fn collect_direct_object_key_features(
    object: &ObjectLit,
    features: &mut BTreeSet<SelectorAnchorFeature>,
) {
    for prop in &object.props {
        if let PropOrSpread::Prop(prop) = prop
            && let Some(name) = prop_key_name(prop.as_ref())
        {
            features.insert(SelectorAnchorFeature::ObjectKey(name));
        }
    }
}

fn var_group_frontier(
    index: &ChunkSelectorIndex,
    var_kind: VarDeclKind,
    target_count: usize,
    slot_constraints: &[BTreeSet<SelectorAnchorFeature>],
) -> BTreeSet<VarGroupTuple> {
    var_group_frontier_with_features(index, var_kind, target_count, slot_constraints)
        .into_iter()
        .map(|tuple| tuple.tuple)
        .collect()
}

fn var_group_frontier_with_features(
    index: &ChunkSelectorIndex,
    var_kind: VarDeclKind,
    target_count: usize,
    slot_constraints: &[BTreeSet<SelectorAnchorFeature>],
) -> Vec<VarGroupTupleWithFeatures> {
    let mut tuples = Vec::new();
    for decl in &index.decls {
        if decl.kind != IndexedDeclarationKind::Var {
            continue;
        }
        let (Some(ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))))
        | Some(ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(ExportDecl {
            decl: Decl::Var(var),
            ..
        })))) = index.parsed.module.body.get(decl.body_idx)
        else {
            continue;
        };
        if var.kind != var_kind || var.decls.len() < target_count {
            continue;
        }
        let declarator_features = var
            .decls
            .iter()
            .map(|declarator| {
                let mut features = BTreeSet::new();
                if let Some(init) = declarator.init.as_deref() {
                    collect_renderable_var_anchor_features(init, &mut features);
                }
                features
            })
            .collect::<Vec<_>>();
        let mut combinations = Vec::new();
        collect_slot_combinations(
            var.decls.len(),
            target_count,
            0,
            &mut Vec::new(),
            &mut combinations,
        );
        for slots in combinations {
            let matches_constraints = slots.iter().enumerate().all(|(target_pos, slot)| {
                let constraints = &slot_constraints[target_pos];
                constraints.is_empty()
                    || declarator_features
                        .get(*slot)
                        .is_some_and(|features| constraints.is_subset(features))
            });
            if matches_constraints {
                let slot_features = slots
                    .iter()
                    .map(|slot| declarator_features.get(*slot).cloned().unwrap_or_default())
                    .collect();
                tuples.push(VarGroupTupleWithFeatures {
                    tuple: VarGroupTuple {
                        body_idx: decl.body_idx,
                        slots,
                    },
                    slot_features,
                });
            }
        }
    }
    tuples
}

fn var_group_frontier_is_bounded(
    index: &ChunkSelectorIndex,
    var_kind: VarDeclKind,
    target_count: usize,
) -> bool {
    index.decls.iter().all(|decl| {
        if decl.kind != IndexedDeclarationKind::Var {
            return true;
        }
        let (Some(ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))))
        | Some(ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(ExportDecl {
            decl: Decl::Var(var),
            ..
        })))) = index.parsed.module.body.get(decl.body_idx)
        else {
            return true;
        };
        var.kind != var_kind
            || var.decls.len() < target_count
            || !combination_count_exceeds(
                var.decls.len(),
                target_count,
                MAX_VAR_GROUP_FRONTIER_TUPLES_PER_DECL,
            )
    })
}

fn combination_count_exceeds(n: usize, k: usize, limit: usize) -> bool {
    if k > n {
        return false;
    }
    let k = k.min(n - k);
    let mut count = 1u128;
    let limit = limit as u128;
    for i in 1..=k {
        count = count * (n - k + i) as u128 / i as u128;
        if count > limit {
            return true;
        }
    }
    false
}

fn collect_slot_combinations(
    len: usize,
    target_count: usize,
    start: usize,
    current: &mut Vec<usize>,
    out: &mut Vec<Vec<usize>>,
) {
    if current.len() == target_count {
        out.push(current.clone());
        return;
    }
    for idx in start..len {
        current.push(idx);
        collect_slot_combinations(len, target_count, idx + 1, current, out);
        current.pop();
    }
}

fn target_tuple_is_unique(
    frontier: &BTreeSet<VarGroupTuple>,
    decl: &IndexedDeclaration,
    target_slots: &[usize],
) -> bool {
    frontier.len() == 1
        && frontier.contains(&VarGroupTuple {
            body_idx: decl.body_idx,
            slots: target_slots.to_vec(),
        })
}

struct RenderedExprSelector {
    source: String,
    holes: BTreeSet<String>,
}

fn render_specialized_expr_selector(
    index: &ChunkSelectorIndex,
    expr: &Expr,
    selected_features: &BTreeSet<SelectorAnchorFeature>,
) -> Result<RenderedExprSelector> {
    match expr {
        Expr::Call(call) if call_has_selected_feature(call, selected_features) => {
            render_specialized_call_selector(index, call, selected_features)
        }
        Expr::Object(object) if object_has_selected_key(object, selected_features) => Ok(
            render_specialized_object_selector(object, selected_features),
        ),
        _ => Ok(RenderedExprSelector {
            source: ANYTHING_HOLE_KEYWORD.to_string(),
            holes: BTreeSet::from([ANYTHING_HOLE_KEYWORD.to_string()]),
        }),
    }
}

fn render_specialized_call_selector(
    index: &ChunkSelectorIndex,
    call: &CallExpr,
    selected_features: &BTreeSet<SelectorAnchorFeature>,
) -> Result<RenderedExprSelector> {
    let callee = match &call.callee {
        Callee::Expr(expr) => source_for_span(index, expr.span())?.text,
        _ => return Ok(anything_expr_selector()),
    };
    let mut args = Vec::new();
    let mut holes = BTreeSet::new();
    let mut skipped = false;
    for arg in &call.args {
        if let Expr::Object(object) = arg.expr.as_ref()
            && object_has_selected_key(object, selected_features)
        {
            if skipped {
                args.push(ARGS_HOLE_KEYWORD.to_string());
                holes.insert(ARGS_HOLE_KEYWORD.to_string());
                skipped = false;
            }
            let rendered = render_specialized_object_selector(object, selected_features);
            holes.extend(rendered.holes.clone());
            args.push(rendered.source);
        } else {
            skipped = true;
        }
    }
    if skipped || args.is_empty() {
        args.push(ARGS_HOLE_KEYWORD.to_string());
        holes.insert(ARGS_HOLE_KEYWORD.to_string());
    }
    Ok(RenderedExprSelector {
        source: format!("{callee}({})", args.join(", ")),
        holes,
    })
}

fn render_specialized_object_selector(
    object: &ObjectLit,
    selected_features: &BTreeSet<SelectorAnchorFeature>,
) -> RenderedExprSelector {
    let mut parts = Vec::new();
    let mut skipped = false;
    for prop in &object.props {
        let key = match prop {
            PropOrSpread::Prop(prop) => prop_key_name(prop.as_ref()),
            PropOrSpread::Spread(_) => None,
        };
        if let Some(key) = key
            && selected_features.contains(&SelectorAnchorFeature::ObjectKey(key.clone()))
        {
            if skipped {
                parts.push(ANYTHING_HOLE_KEYWORD.to_string());
                skipped = false;
            }
            parts.push(format!("{key}: {ANYTHING_HOLE_KEYWORD}"));
        } else {
            skipped = true;
        }
    }
    if skipped || parts.is_empty() {
        parts.push(ANYTHING_HOLE_KEYWORD.to_string());
    }
    RenderedExprSelector {
        source: format!("{{ {} }}", parts.join(", ")),
        holes: BTreeSet::from([ANYTHING_HOLE_KEYWORD.to_string()]),
    }
}

fn anything_expr_selector() -> RenderedExprSelector {
    RenderedExprSelector {
        source: ANYTHING_HOLE_KEYWORD.to_string(),
        holes: BTreeSet::from([ANYTHING_HOLE_KEYWORD.to_string()]),
    }
}

fn call_has_selected_feature(
    call: &CallExpr,
    selected_features: &BTreeSet<SelectorAnchorFeature>,
) -> bool {
    call_callee_feature_name(&call.callee).is_some_and(|callee| {
        selected_features.contains(&SelectorAnchorFeature::CallCallee(callee))
    }) || call.args.iter().any(|arg| {
        matches!(
            arg.expr.as_ref(),
            Expr::Object(object) if object_has_selected_key(object, selected_features)
        )
    })
}

fn object_has_selected_key(
    object: &ObjectLit,
    selected_features: &BTreeSet<SelectorAnchorFeature>,
) -> bool {
    object.props.iter().any(|prop| {
        let Some(key) = (match prop {
            PropOrSpread::Prop(prop) => prop_key_name(prop.as_ref()),
            PropOrSpread::Spread(_) => None,
        }) else {
            return false;
        };
        selected_features.contains(&SelectorAnchorFeature::ObjectKey(key))
    })
}

fn prop_key_name(prop: &Prop) -> Option<String> {
    match prop {
        Prop::KeyValue(key_value) => prop_name_to_string(&key_value.key),
        Prop::Method(method) => prop_name_to_string(&method.key),
        Prop::Getter(getter) => prop_name_to_string(&getter.key),
        Prop::Setter(setter) => prop_name_to_string(&setter.key),
        Prop::Shorthand(ident) if !is_hole_name(ident.sym.as_ref()) => Some(ident.sym.to_string()),
        Prop::Assign(assign) if !is_hole_name(assign.key.sym.as_ref()) => {
            Some(assign.key.sym.to_string())
        }
        _ => None,
    }
}

fn prop_name_to_string(name: &PropName) -> Option<String> {
    match name {
        PropName::Ident(ident) if !is_hole_name(ident.sym.as_ref()) => Some(ident.sym.to_string()),
        PropName::Str(value) => Some(value.value.to_string_lossy().to_string()),
        PropName::Num(value) => Some(value.value.to_string()),
        _ => None,
    }
}

fn class_member_feature_name(member: &ClassMember) -> Option<String> {
    match member {
        ClassMember::Constructor(_) => Some("constructor".to_string()),
        ClassMember::Method(method) => prop_name_to_string(&method.key),
        ClassMember::PrivateMethod(method) => Some(method.key.name.to_string()),
        ClassMember::ClassProp(prop) => prop_name_to_string(&prop.key),
        ClassMember::PrivateProp(prop) => Some(prop.key.name.to_string()),
        ClassMember::AutoAccessor(_) => None,
        _ => None,
    }
}

fn render_class_member_skeletons(
    class: &Class,
    selected: &BTreeSet<String>,
    index: &ChunkSelectorIndex,
) -> Result<String> {
    let mut lines = vec!["  CLASS_REST;".to_string()];
    for member in &class.body {
        let Some(name) = class_member_feature_name(member) else {
            continue;
        };
        if !selected.contains(&name) {
            continue;
        }
        lines.push(render_class_member_skeleton(member, &name, index)?);
        lines.push("  CLASS_REST;".to_string());
    }
    Ok(lines.join("\n") + "\n")
}

fn render_class_member_skeleton(
    member: &ClassMember,
    name: &str,
    index: &ChunkSelectorIndex,
) -> Result<String> {
    match member {
        ClassMember::Constructor(constructor) => {
            let params = constructor
                .params
                .iter()
                .map(|_| ANYTHING_HOLE_KEYWORD)
                .collect::<Vec<_>>()
                .join(", ");
            Ok(format!("  constructor({params}) {{\n    STMT_LIST;\n  }}"))
        }
        ClassMember::Method(method) => {
            let params = method
                .function
                .params
                .iter()
                .map(|_| ANYTHING_HOLE_KEYWORD)
                .collect::<Vec<_>>()
                .join(", ");
            Ok(format!("  {name}({params}) {{\n    STMT_LIST;\n  }}"))
        }
        ClassMember::ClassProp(prop) => {
            let key = prop_name_to_string(&prop.key).unwrap_or_else(|| name.to_string());
            Ok(format!("  {key} = {ANYTHING_HOLE_KEYWORD};"))
        }
        _ => Ok(format!("  {}", source_for_span(index, member.span())?.text)),
    }
}

fn call_callee_feature_name(callee: &Callee) -> Option<String> {
    match callee {
        Callee::Expr(expr) => expr_feature_name(expr),
        Callee::Super(_) => Some("super".to_string()),
        Callee::Import(_) => Some("import".to_string()),
    }
}

fn expr_feature_name(expr: &Expr) -> Option<String> {
    match expr {
        Expr::Ident(ident) if !is_hole_name(ident.sym.as_ref()) => Some(ident.sym.to_string()),
        Expr::Member(member) => {
            let obj = match member.obj.as_ref() {
                Expr::Ident(ident) if !is_hole_name(ident.sym.as_ref()) => {
                    Some(ident.sym.to_string())
                }
                Expr::Member(_) => expr_feature_name(member.obj.as_ref()),
                _ => None,
            }?;
            let prop = match &member.prop {
                MemberProp::Ident(ident) if !is_hole_name(ident.sym.as_ref()) => {
                    Some(ident.sym.to_string())
                }
                MemberProp::PrivateName(name) => Some(name.name.to_string()),
                MemberProp::Computed(_) => None,
                _ => None,
            }?;
            Some(format!("{obj}.{prop}"))
        }
        _ => None,
    }
}

fn is_hole_expr(expr: &Expr) -> bool {
    matches!(expr, Expr::Ident(ident) if is_hole_name(ident.sym.as_ref()))
}

fn is_object_property_hole(prop_or_spread: &PropOrSpread) -> bool {
    matches!(
        prop_or_spread,
        PropOrSpread::Prop(prop)
            if matches!(prop.as_ref(), Prop::Shorthand(ident) if is_hole_name(ident.sym.as_ref()))
    )
}

fn is_class_rest_hole_member(member: &ClassMember) -> bool {
    matches!(
        member,
        ClassMember::ClassProp(prop)
            if prop.value.is_none()
                && matches!(&prop.key, PropName::Ident(ident) if is_hole_name(ident.sym.as_ref()))
    )
}

fn is_hole_name(name: &str) -> bool {
    name == ANYTHING_HOLE_KEYWORD
        || name == EXPR_HOLE_KEYWORD
        || name.starts_with("EXPR_")
        || name == STMT_HOLE_KEYWORD
        || name.starts_with("STMT_")
        || name == "STMT_LIST"
        || name.starts_with("STMT_LIST_")
        || name == CLASS_REST_HOLE_KEYWORD
        || name.starts_with("CLASS_REST_")
        || name == DECLARATORS_HOLE_KEYWORD
        || name.starts_with("DECLARATORS_")
        || name == ARGS_HOLE_KEYWORD
        || name.starts_with("ARGS_")
        || name == OBJECT_PROPS_HOLE_KEYWORD
        || name.starts_with("OBJECT_PROPS_")
}

fn render_var_group_selector(
    index: &ChunkSelectorIndex,
    item: &ModuleItem,
    decl: &IndexedDeclaration,
    targets: &[SynthesizedTargetBinding],
) -> Result<(String, Vec<String>)> {
    let var = item_var_decl(item).context("indexed var declaration no longer has var AST")?;
    let target_decl_indices = targets
        .iter()
        .map(|target| {
            decl.declared_bindings
                .iter()
                .find(|binding| binding.name == target.runtime_binding)
                .and_then(|binding| binding.declarator_idx)
                .with_context(|| {
                    format!(
                        "target `{}` is not a var declarator",
                        target.runtime_binding
                    )
                })
        })
        .collect::<Result<BTreeSet<_>>>()?;
    let export_by_runtime = targets
        .iter()
        .map(|target| (target.runtime_binding.as_str(), target.export_name.as_str()))
        .collect::<BTreeMap<_, _>>();
    let mut parts = Vec::new();
    let mut holes = Vec::new();
    let mut skipped_run = 0usize;
    let mut target_seen = 0usize;
    for (idx, declarator) in var.decls.iter().enumerate() {
        if !target_decl_indices.contains(&idx) {
            skipped_run += 1;
            continue;
        }
        if skipped_run > 0 {
            let hole = declarator_hole_name(target_seen, target_decl_indices.len());
            parts.push(format!("{hole} = null"));
            holes.push(hole.to_string());
            skipped_run = 0;
        }
        let runtime_name = single_ident_pat_name(&declarator.name)
            .context("only identifier var declarators can be synthesized today")?;
        let export_name = export_by_runtime
            .get(runtime_name)
            .copied()
            .context("target declarator missing export name")?;
        parts.push(render_var_declarator_selector(
            index,
            declarator,
            runtime_name,
            export_name,
        )?);
        target_seen += 1;
    }
    if skipped_run > 0 {
        let hole = declarator_hole_name(target_seen, target_decl_indices.len());
        parts.push(format!("{hole} = null"));
        holes.push(hole.to_string());
    }
    if parts.is_empty() {
        bail!("var selector synthesis selected no target declarators");
    }
    Ok((
        format!("{} {};", var_kind_label(var.kind), parts.join(",\n  ")),
        holes,
    ))
}

fn declarator_hole_name(target_seen: usize, target_count: usize) -> &'static str {
    match (target_seen, target_count) {
        (0, _) => "DECLARATORS_BEFORE",
        (seen, total) if seen == total => "DECLARATORS_AFTER",
        _ => "DECLARATORS_BETWEEN",
    }
}

fn render_var_declarator_selector(
    index: &ChunkSelectorIndex,
    declarator: &VarDeclarator,
    runtime_name: &str,
    export_name: &str,
) -> Result<String> {
    let source = source_for_span(index, declarator.span())?;
    replace_ident_span_text(
        index,
        &source.text,
        source.start,
        declarator.name.span(),
        runtime_name,
        export_name,
    )
}

fn render_single_binding_item_selector(
    index: &ChunkSelectorIndex,
    item: &ModuleItem,
    runtime_binding: &str,
    export_name: &str,
) -> Result<String> {
    let source = source_for_span(index, item.span())?;
    let ident_span = match item_decl(item) {
        Some(Decl::Fn(function)) => function.ident.span,
        Some(Decl::Class(class)) => class.ident.span,
        _ => bail!("unsupported single-binding declaration kind"),
    };
    replace_ident_span_text(
        index,
        &source.text,
        source.start,
        ident_span,
        runtime_binding,
        export_name,
    )
}

struct SourceSlice {
    start: usize,
    text: String,
}

fn source_for_span(index: &ChunkSelectorIndex, span: Span) -> Result<SourceSlice> {
    let (mut start, mut end) = span_offsets(index, span)?;
    while start < end && index.source.as_bytes()[start].is_ascii_whitespace() {
        start += 1;
    }
    while end > start && index.source.as_bytes()[end - 1].is_ascii_whitespace() {
        end -= 1;
    }
    let mut text = index
        .source
        .get(start..end)
        .context("span is not on UTF-8 boundaries")?
        .to_string();
    if !text.ends_with(';') && span_item_like_semicolon(&text) {
        text.push(';');
    }
    Ok(SourceSlice { start, text })
}

fn span_offsets(index: &ChunkSelectorIndex, span: Span) -> Result<(usize, usize)> {
    let file = index
        .parsed
        .cm
        .files()
        .first()
        .cloned()
        .context("source map has no file")?;
    let start = span
        .lo()
        .0
        .checked_sub(file.start_pos.0)
        .context("span starts before source file")? as usize;
    let end = span
        .hi()
        .0
        .checked_sub(file.start_pos.0)
        .context("span ends before source file")? as usize;
    Ok((start, end))
}

fn span_item_like_semicolon(source: &str) -> bool {
    source.starts_with("const ") || source.starts_with("let ") || source.starts_with("var ")
}

fn trim_selector_source_line_suffixes(source: &str) -> String {
    source
        .split('\n')
        .map(|line| line.trim_end_matches([' ', '\t']))
        .collect::<Vec<_>>()
        .join("\n")
}

fn replace_ident_span_text(
    index: &ChunkSelectorIndex,
    local_source: &str,
    local_start: usize,
    ident_span: Span,
    expected: &str,
    replacement: &str,
) -> Result<String> {
    if expected == replacement {
        return Ok(local_source.to_string());
    }
    let (source_start, source_end) = span_offsets(index, ident_span)?;
    let rel_start = source_start
        .checked_sub(local_start)
        .context("identifier span starts before local source")?;
    let rel_end = source_end
        .checked_sub(local_start)
        .context("identifier span ends before local source")?;
    if local_source.get(rel_start..rel_end) != Some(expected) {
        bail!(
            "identifier span expected `{expected}` but found `{:?}`",
            local_source.get(rel_start..rel_end)
        );
    }
    let mut out = String::new();
    out.push_str(&local_source[..rel_start]);
    out.push_str(replacement);
    out.push_str(&local_source[rel_end..]);
    Ok(out)
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
        candidate_count: Some(input.synthesized.candidate_count),
        rewritten_holes: input.synthesized.rewritten_holes.clone(),
        replacement_count: input.synthesized.rewritten_holes.len(),
        reason: None,
    }
}

fn member_with_source_match(
    export_name: &str,
    comment: Option<String>,
    match_source: &str,
    target_binding: &str,
) -> Value {
    let mut member = serde_yaml::Mapping::new();
    member.insert(yk("name"), Value::String(export_name.to_string()));
    if let Some(comment) = comment {
        member.insert(yk("comment"), Value::String(comment));
    }
    let mut source_match = serde_yaml::Mapping::new();
    source_match.insert(yk("identifiers"), Value::String("alpha_all".to_string()));
    source_match.insert(
        yk("target_binding"),
        Value::String(target_binding.to_string()),
    );
    source_match.insert(yk("match"), Value::String(match_source.to_string()));
    let mut selector = serde_yaml::Mapping::new();
    selector.insert(yk("source_match"), Value::Mapping(source_match));
    member.insert(yk("selector"), Value::Mapping(selector));
    Value::Mapping(member)
}

fn binding_group_value(
    synthesized: &SynthesizedSelectorGroup,
    members: &[NameBindingMember],
) -> Value {
    let mut source_match = serde_yaml::Mapping::new();
    source_match.insert(yk("identifiers"), Value::String("alpha_all".to_string()));
    source_match.insert(yk("match"), Value::String(synthesized.match_source.clone()));

    let mut exports = serde_yaml::Mapping::new();
    for target in &synthesized.target_bindings {
        exports.insert(
            Value::String(target.export_name.clone()),
            Value::String(target.export_name.clone()),
        );
    }
    let mut comments = serde_yaml::Mapping::new();
    for member in members {
        if let Some(comment) = &member.comment {
            comments.insert(
                Value::String(member.export_name.clone()),
                Value::String(comment.clone()),
            );
        }
    }
    let mut group = serde_yaml::Mapping::new();
    group.insert(yk("source_match"), Value::Mapping(source_match));
    group.insert(yk("exports"), Value::Mapping(exports));
    if !comments.is_empty() {
        group.insert(yk("comments"), Value::Mapping(comments));
    }
    Value::Mapping(group)
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
        rewritten_holes: Vec::new(),
        replacement_count: 0,
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

fn mapping_get_mut_path<'a>(value: &'a mut Value, path: &[&str]) -> Option<&'a mut Value> {
    let mut current = value;
    for key in path {
        let Value::Mapping(mapping) = current else {
            return None;
        };
        current = mapping.get_mut(yk(key))?;
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

fn var_kind_label(kind: VarDeclKind) -> &'static str {
    match kind {
        VarDeclKind::Var => "var",
        VarDeclKind::Let => "let",
        VarDeclKind::Const => "const",
    }
}

#[derive(Debug, Clone)]
struct TextEdit {
    start: usize,
    end: usize,
    text: String,
}

fn target_binding_text_insertion(
    source: &str,
    member_index: usize,
    target_binding: &str,
) -> Result<TextEdit> {
    if !is_safe_plain_yaml_scalar(target_binding) {
        bail!("target_binding `{target_binding}` needs YAML quoting");
    }
    let lines = source_lines(source);
    let members_line_idx =
        find_mapping_key_line(&lines, 0, lines.len(), 0, "members").context("missing members:")?;
    let member_starts = member_start_lines(&lines, members_line_idx + 1)?;
    let Some(&member_start) = member_starts.get(member_index) else {
        bail!("could not locate members[{member_index}] in source text");
    };
    let member_end = member_starts
        .get(member_index + 1)
        .copied()
        .or_else(|| next_root_key_line(&lines, member_start + 1))
        .unwrap_or(lines.len());

    let source_match_line =
        find_mapping_key_line(&lines, member_start, member_end, usize::MAX, "source_match")
            .context("could not locate selector.source_match block in source text")?;
    let source_match_indent = lines[source_match_line].indent;
    let source_match_end = next_mapping_peer_or_parent_line(
        &lines,
        source_match_line + 1,
        member_end,
        source_match_indent,
    )
    .unwrap_or(member_end);
    if find_mapping_key_line(
        &lines,
        source_match_line + 1,
        source_match_end,
        source_match_indent + 1,
        "target_binding",
    )
    .is_some()
    {
        bail!("target_binding already present in source text");
    }
    let match_line = find_mapping_key_line(
        &lines,
        source_match_line + 1,
        source_match_end,
        source_match_indent + 1,
        "match",
    )
    .context("could not locate source_match.match line in source text")?;
    let indent = lines[match_line].indent_text();
    Ok(TextEdit {
        start: lines[match_line].start,
        end: lines[match_line].start,
        text: format!("{indent}target_binding: {target_binding}\n"),
    })
}

fn source_match_selector_text_edit(
    source: &str,
    member_index: usize,
    match_source: &str,
    target_binding: &str,
) -> Result<TextEdit> {
    if !is_safe_plain_yaml_scalar(target_binding) {
        bail!("target_binding `{target_binding}` needs YAML quoting");
    }
    let lines = source_lines(source);
    let members_block = members_text_block(&lines)?;
    let (member_start, member_end) = member_line_range(&members_block, member_index)?;
    let selector_line =
        find_mapping_key_line(&lines, member_start, member_end, usize::MAX, "selector")
            .context("could not locate selector block in source text")?;
    let selector_indent = lines[selector_line].indent;
    let selector_end =
        next_mapping_peer_or_parent_line(&lines, selector_line + 1, member_end, selector_indent)
            .unwrap_or(member_end);
    Ok(TextEdit {
        start: lines[selector_line].start,
        end: line_start_or_eof(source, &lines, selector_end),
        text: render_source_match_selector_block(
            lines[selector_line].indent_text(),
            match_source,
            target_binding,
        ),
    })
}

fn binding_group_text_edits(
    source: &str,
    member_count: usize,
    removed_member_indices: &BTreeSet<usize>,
    binding_groups: &[Value],
) -> Result<Vec<TextEdit>> {
    if binding_groups.is_empty() {
        return Ok(Vec::new());
    }
    let lines = source_lines(source);
    let members_block = members_text_block(&lines)?;
    if removed_member_indices.len() == member_count {
        let mut edits = Vec::new();
        edits.push(TextEdit {
            start: lines[members_block.members_line].start,
            end: line_start_or_eof(source, &lines, members_block.end_line),
            text: "members: []\n".to_string(),
        });
        edits.extend(binding_group_insertion_edits(
            source,
            &lines,
            Some(members_block.end_line),
            binding_groups,
        )?);
        return Ok(edits);
    }

    let mut edits = Vec::new();
    for member_index in removed_member_indices {
        let (start_line, end_line) = member_line_range(&members_block, *member_index)?;
        edits.push(TextEdit {
            start: lines[start_line].start,
            end: line_start_or_eof(source, &lines, end_line),
            text: String::new(),
        });
    }
    edits.extend(binding_group_insertion_edits(
        source,
        &lines,
        Some(members_block.end_line),
        binding_groups,
    )?);
    Ok(edits)
}

fn binding_group_insertion_edits(
    source: &str,
    lines: &[SourceLine<'_>],
    fallback_insert_line: Option<usize>,
    binding_groups: &[Value],
) -> Result<Vec<TextEdit>> {
    if let Some(binding_groups_line) =
        find_mapping_key_line_at_indent(lines, 0, lines.len(), 0, "binding_groups")
    {
        let existing_indent = lines
            .iter()
            .enumerate()
            .skip(binding_groups_line + 1)
            .find(|(_, line)| !line.trimmed.is_empty() && !line.trimmed.starts_with('#'))
            .map(|(_, line)| line.indent_text().to_string())
            .context("cannot append to empty or inline binding_groups block")?;
        let binding_groups_end =
            next_root_key_line(lines, binding_groups_line + 1).unwrap_or(lines.len());
        return Ok(vec![TextEdit {
            start: line_start_or_eof(source, lines, binding_groups_end),
            end: line_start_or_eof(source, lines, binding_groups_end),
            text: render_yaml_sequence_items(binding_groups, &existing_indent)?,
        }]);
    }

    let insert_line = fallback_insert_line.context("missing insertion point for binding_groups")?;
    let mut text = String::from("binding_groups:\n");
    text.push_str(&render_yaml_sequence_items(binding_groups, "  ")?);
    Ok(vec![TextEdit {
        start: line_start_or_eof(source, lines, insert_line),
        end: line_start_or_eof(source, lines, insert_line),
        text,
    }])
}

fn render_source_match_selector_block(
    indent: &str,
    match_source: &str,
    target_binding: &str,
) -> String {
    let mut out = String::new();
    out.push_str(&format!("{indent}selector:\n"));
    out.push_str(&format!("{indent}  source_match:\n"));
    out.push_str(&format!("{indent}    identifiers: alpha_all\n"));
    out.push_str(&format!("{indent}    target_binding: {target_binding}\n"));
    out.push_str(&format!("{indent}    match: |-\n"));
    push_literal_block(&mut out, match_source, &format!("{indent}      "));
    out
}

fn render_yaml_sequence_items(items: &[Value], indent: &str) -> Result<String> {
    let yaml = serde_yaml::to_string(&Value::Sequence(items.to_vec()))
        .context("serializing YAML sequence items")?;
    let mut out = String::new();
    for line in yaml.lines() {
        if line == "---" || line == "..." {
            continue;
        }
        out.push_str(indent);
        out.push_str(line);
        out.push('\n');
    }
    Ok(out)
}

fn push_literal_block(out: &mut String, text: &str, indent: &str) {
    for line in text.split('\n') {
        if !line.is_empty() {
            out.push_str(indent);
            out.push_str(line);
        }
        out.push('\n');
    }
}

fn apply_text_edits(source: &str, edits: &[TextEdit]) -> Result<String> {
    let mut ordered = edits.to_vec();
    ordered.sort_by_key(|edit| (edit.start, edit.end));
    let mut output = String::with_capacity(
        source.len() + ordered.iter().map(|edit| edit.text.len()).sum::<usize>(),
    );
    let mut cursor = 0;
    for edit in ordered {
        if edit.start < cursor
            || edit.end < edit.start
            || !source.is_char_boundary(edit.start)
            || !source.is_char_boundary(edit.end)
        {
            bail!(
                "invalid or overlapping text edit {}..{}",
                edit.start,
                edit.end
            );
        }
        output.push_str(&source[cursor..edit.start]);
        output.push_str(&edit.text);
        cursor = edit.end;
    }
    output.push_str(&source[cursor..]);
    Ok(output)
}

struct MembersTextBlock {
    members_line: usize,
    member_starts: Vec<usize>,
    end_line: usize,
}

fn members_text_block(lines: &[SourceLine<'_>]) -> Result<MembersTextBlock> {
    let members_line = find_mapping_key_line_at_indent(lines, 0, lines.len(), 0, "members")
        .context("missing members:")?;
    let member_starts = member_start_lines(lines, members_line + 1)?;
    let end_line = next_root_key_line(lines, members_line + 1).unwrap_or(lines.len());
    Ok(MembersTextBlock {
        members_line,
        member_starts,
        end_line,
    })
}

fn member_line_range(block: &MembersTextBlock, member_index: usize) -> Result<(usize, usize)> {
    let Some(&member_start) = block.member_starts.get(member_index) else {
        bail!("could not locate members[{member_index}] in source text");
    };
    let member_end = block
        .member_starts
        .get(member_index + 1)
        .copied()
        .unwrap_or(block.end_line);
    Ok((member_start, member_end))
}

fn line_start_or_eof(source: &str, lines: &[SourceLine<'_>], line: usize) -> usize {
    lines.get(line).map_or(source.len(), |line| line.start)
}

#[derive(Debug)]
struct SourceLine<'a> {
    start: usize,
    text: &'a str,
    indent: usize,
    trimmed: &'a str,
}

impl SourceLine<'_> {
    fn indent_text(&self) -> &str {
        &self.text[..self.indent]
    }
}

fn source_lines(source: &str) -> Vec<SourceLine<'_>> {
    let mut offset = 0;
    source
        .split_inclusive('\n')
        .map(|line| {
            let line_without_newline = line.strip_suffix('\n').unwrap_or(line);
            let line_without_newline = line_without_newline
                .strip_suffix('\r')
                .unwrap_or(line_without_newline);
            let indent = line_without_newline
                .chars()
                .take_while(|ch| *ch == ' ')
                .count();
            let start = offset;
            offset += line.len();
            SourceLine {
                start,
                text: line_without_newline,
                indent,
                trimmed: line_without_newline.trim_start(),
            }
        })
        .collect()
}

fn member_start_lines(lines: &[SourceLine<'_>], start: usize) -> Result<Vec<usize>> {
    let Some(first_member) = lines
        .iter()
        .enumerate()
        .skip(start)
        .find(|(_, line)| !line.trimmed.is_empty() && !line.trimmed.starts_with('#'))
    else {
        return Ok(Vec::new());
    };
    if !first_member.1.trimmed.starts_with("- ") {
        bail!("members: is not followed by a block sequence");
    }
    let member_indent = first_member.1.indent;
    Ok(lines
        .iter()
        .enumerate()
        .skip(first_member.0)
        .take_while(|(_, line)| {
            line.trimmed.is_empty() || line.trimmed.starts_with('#') || line.indent >= member_indent
        })
        .filter_map(|(idx, line)| {
            (line.indent == member_indent && line.trimmed.starts_with("- ")).then_some(idx)
        })
        .collect())
}

fn next_root_key_line(lines: &[SourceLine<'_>], start: usize) -> Option<usize> {
    lines
        .iter()
        .enumerate()
        .skip(start)
        .find_map(|(idx, line)| {
            (!line.trimmed.is_empty()
                && !line.trimmed.starts_with('#')
                && line.indent == 0
                && mapping_key(line.trimmed).is_some())
            .then_some(idx)
        })
}

fn next_mapping_peer_or_parent_line(
    lines: &[SourceLine<'_>],
    start: usize,
    end: usize,
    parent_indent: usize,
) -> Option<usize> {
    lines
        .iter()
        .enumerate()
        .take(end)
        .skip(start)
        .find_map(|(idx, line)| {
            (!line.trimmed.is_empty()
                && !line.trimmed.starts_with('#')
                && line.indent <= parent_indent
                && (line.trimmed.starts_with("- ") || mapping_key(line.trimmed).is_some()))
            .then_some(idx)
        })
}

fn find_mapping_key_line(
    lines: &[SourceLine<'_>],
    start: usize,
    end: usize,
    min_indent: usize,
    key: &str,
) -> Option<usize> {
    lines
        .iter()
        .enumerate()
        .take(end)
        .skip(start)
        .find_map(|(idx, line)| {
            let indent_matches = min_indent == usize::MAX || line.indent >= min_indent;
            (indent_matches && mapping_key(line.trimmed) == Some(key)).then_some(idx)
        })
}

fn find_mapping_key_line_at_indent(
    lines: &[SourceLine<'_>],
    start: usize,
    end: usize,
    indent: usize,
    key: &str,
) -> Option<usize> {
    lines
        .iter()
        .enumerate()
        .take(end)
        .skip(start)
        .find_map(|(idx, line)| {
            (line.indent == indent && mapping_key(line.trimmed) == Some(key)).then_some(idx)
        })
}

fn mapping_key(trimmed: &str) -> Option<&str> {
    if trimmed.starts_with('#') || trimmed.starts_with("- ") {
        return None;
    }
    let (key, rest) = trimmed.split_once(':')?;
    (!key.is_empty()
        && !key.chars().any(char::is_whitespace)
        && (rest.is_empty() || rest.starts_with(' ') || rest.starts_with('#')))
    .then_some(key)
}

fn is_safe_plain_yaml_scalar(value: &str) -> bool {
    !value.is_empty()
        && value
            .chars()
            .all(|ch| ch.is_ascii_alphanumeric() || matches!(ch, '_' | '$'))
}

#[derive(Debug, Clone)]
struct SourceReplacement {
    start: usize,
    end: usize,
    from: String,
}

#[derive(Debug, Clone)]
struct SpanReplacement {
    span: Span,
    from: &'static str,
}

fn anonymous_typed_hole_replacements(
    module: &str,
    member_index: usize,
    match_source: &str,
) -> Result<Vec<SourceReplacement>> {
    let parsed = js_ast::parse_js_module(
        &format!("<selector-codemod anything holes in {module} member {member_index}>"),
        match_source,
    )?;
    let mut collector = AnonymousTypedHoleCollector::default();
    parsed.module.visit_with(&mut collector);
    let files = parsed.cm.files();
    let Some(file) = files.first() else {
        return Ok(Vec::new());
    };
    let mut replacements = collector
        .replacements
        .into_iter()
        .map(|replacement| span_replacement_to_source(match_source, file.start_pos, replacement))
        .collect::<Result<Vec<_>>>()?;
    replacements.sort_by_key(|replacement| (replacement.start, replacement.end));
    replacements.dedup_by_key(|replacement| (replacement.start, replacement.end));
    ensure_non_overlapping_replacements(module, member_index, &replacements)?;
    Ok(replacements)
}

fn span_replacement_to_source(
    source: &str,
    file_start: BytePos,
    replacement: SpanReplacement,
) -> Result<SourceReplacement> {
    let start = replacement
        .span
        .lo()
        .0
        .checked_sub(file_start.0)
        .context("selector source span starts before source file")? as usize;
    let end = replacement
        .span
        .hi()
        .0
        .checked_sub(file_start.0)
        .context("selector source span ends before source file")? as usize;
    let Some(actual) = source.get(start..end) else {
        bail!("selector source span is not a valid UTF-8 boundary");
    };
    if actual != replacement.from {
        bail!(
            "selector source span expected `{}` but found `{actual}`",
            replacement.from
        );
    }
    Ok(SourceReplacement {
        start,
        end,
        from: replacement.from.to_string(),
    })
}

fn ensure_non_overlapping_replacements(
    module: &str,
    member_index: usize,
    replacements: &[SourceReplacement],
) -> Result<()> {
    for pair in replacements.windows(2) {
        if pair[0].end > pair[1].start {
            bail!(
                "{module}: members[{member_index}].selector.source_match produced overlapping \
                 anonymous-hole replacements"
            );
        }
    }
    Ok(())
}

fn apply_source_replacements(source: &str, replacements: &[SourceReplacement]) -> String {
    let mut output = String::with_capacity(source.len());
    let mut cursor = 0;
    for replacement in replacements {
        output.push_str(&source[cursor..replacement.start]);
        output.push_str(ANYTHING_HOLE_KEYWORD);
        cursor = replacement.end;
    }
    output.push_str(&source[cursor..]);
    output
}

#[derive(Default)]
struct AnonymousTypedHoleCollector {
    replacements: Vec<SpanReplacement>,
}

impl AnonymousTypedHoleCollector {
    fn record(&mut self, span: Span, from: &'static str) {
        self.replacements.push(SpanReplacement { span, from });
    }
}

impl Visit for AnonymousTypedHoleCollector {
    fn visit_expr(&mut self, expr: &Expr) {
        if let Expr::Ident(ident) = expr
            && ident.sym.as_ref() == EXPR_HOLE_KEYWORD
        {
            self.record(ident.span, EXPR_HOLE_KEYWORD);
            return;
        }
        expr.visit_children_with(self);
    }

    fn visit_stmt(&mut self, stmt: &Stmt) {
        if let Stmt::Expr(expr_stmt) = stmt
            && let Expr::Ident(ident) = expr_stmt.expr.as_ref()
            && ident.sym.as_ref() == STMT_HOLE_KEYWORD
        {
            self.record(ident.span, STMT_HOLE_KEYWORD);
            return;
        }
        stmt.visit_children_with(self);
    }

    fn visit_var_declarator(&mut self, declarator: &VarDeclarator) {
        if let Pat::Ident(ident) = &declarator.name
            && ident.id.sym.as_ref() == DECLARATORS_HOLE_KEYWORD
        {
            self.record(ident.id.span, DECLARATORS_HOLE_KEYWORD);
            return;
        }
        declarator.visit_children_with(self);
    }

    fn visit_expr_or_spread(&mut self, expr_or_spread: &ExprOrSpread) {
        if expr_or_spread.spread.is_none()
            && let Expr::Ident(ident) = expr_or_spread.expr.as_ref()
            && ident.sym.as_ref() == ARGS_HOLE_KEYWORD
        {
            self.record(ident.span, ARGS_HOLE_KEYWORD);
            return;
        }
        expr_or_spread.visit_children_with(self);
    }

    fn visit_prop_or_spread(&mut self, prop_or_spread: &PropOrSpread) {
        if let PropOrSpread::Prop(prop) = prop_or_spread
            && let Prop::Shorthand(ident) = prop.as_ref()
            && ident.sym.as_ref() == OBJECT_PROPS_HOLE_KEYWORD
        {
            self.record(ident.span, OBJECT_PROPS_HOLE_KEYWORD);
            return;
        }
        prop_or_spread.visit_children_with(self);
    }

    fn visit_class_member(&mut self, member: &ClassMember) {
        if let ClassMember::ClassProp(prop) = member
            && prop.value.is_none()
            && let PropName::Ident(ident) = &prop.key
            && ident.sym.as_ref() == CLASS_REST_HOLE_KEYWORD
        {
            self.record(ident.span, CLASS_REST_HOLE_KEYWORD);
            return;
        }
        member.visit_children_with(self);
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
                        "  {:?} {} member#{} [{}] target_binding={}\n",
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
