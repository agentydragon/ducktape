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
use selector_candidate_index::SelectorCandidateIndex;
use serde::Serialize;
use serde_yaml::Value;
use shape_index::ShapeIndex;
use source_match::BodyIndexFilter;
use spec::{SourceMatch, SourceMatchIdentifierMode};
use spec_modules::{collect_module_files, is_module_yaml, module_path_from_file};
use swc_common::{BytePos, DUMMY_SP, Span, Spanned};
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitMut, VisitMutWith, VisitWith};

// Hole keyword spellings come from `source_match_holes` so the minimizer
// emits exactly the tokens the matcher resolves.
use source_match_holes::{
    ANYTHING_HOLE_KEYWORD, ARGS_HOLE_KEYWORD, CLASS_REST_HOLE_KEYWORD, DECLARATORS_HOLE_KEYWORD,
    EXPR_HOLE_KEYWORD, OBJECT_PROPS_HOLE_KEYWORD, STMT_HOLE_KEYWORD,
};

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
    minimize_class_selector, minimize_class_selector_candidates, minimize_function_selector,
    minimize_function_selector_candidates, minimize_var_group_selector,
};
use crate::render::holes_present;

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
    /// When a binding group minimizes to no sparse selector, emit the exact
    /// full-AST `source_match` instead of skipping the member. Off by default:
    /// full-AST selectors are rebuild-fragile, so the default skips them.
    pub full_ast_fallback: bool,
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
                NameBindingRewriteOptions {
                    minimize_synthesized_selectors: config.minimize_synthesized_selectors,
                    full_ast_fallback: config.full_ast_fallback,
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
                        config.apply,
                    ),
                    SelectorCodemodRewrite::AnythingHoles => {
                        rewrite_anything_holes(&module, &file, member_index, member, config.apply)
                    }
                    SelectorCodemodRewrite::NameBindingToSourceMatch => unreachable!(),
                };
                let Some(candidate) = candidate? else {
                    continue;
                };
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

fn rewrite_single_target_binding(
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
    let source_match: SourceMatch =
        serde_yaml::from_value(Value::Mapping(source_match_mapping.clone()))
            .with_context(|| format!("{module}: members[{member_index}].selector.source_match"))?;
    if source_match.target_binding.is_some() {
        return Ok(Some(SelectorCodemodCandidate {
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
            alternatives: Vec::new(),
            reason: Some("target_binding already present".to_string()),
        }));
    }

    let declared = match source_match::source_match_declared_binding_names(module, &source_match) {
        Ok(declared) => declared,
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
                alternatives: Vec::new(),
                reason: Some(format!("source_match did not parse: {err:#}")),
            }));
        }
    };

    let [target] = declared.as_slice() else {
        let reason = match declared.len() {
            0 => "source_match declares no top-level bindings".to_string(),
            n => format!("source_match declares {n} top-level bindings"),
        };
        return Ok(Some(SelectorCodemodCandidate {
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
            alternatives: Vec::new(),
            reason: Some(reason),
        }));
    };

    if apply {
        // Insert `target_binding` ahead of the existing `match` key so the
        // dumped mapping keeps the conventional ordering.
        let mut rebuilt = serde_yaml::Mapping::new();
        for (key, value) in source_match_mapping.iter() {
            if key == &yk("match") && !rebuilt.contains_key(yk("target_binding")) {
                rebuilt.insert(yk("target_binding"), Value::String(target.clone()));
            }
            rebuilt.insert(key.clone(), value.clone());
        }
        if !rebuilt.contains_key(yk("target_binding")) {
            rebuilt.insert(yk("target_binding"), Value::String(target.clone()));
        }
        *source_match_mapping = rebuilt;
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
        target_binding: Some(target.clone()),
        declared_bindings: declared,
        group_id: None,
        matched_body_index: None,
        candidate_count: None,
        rewritten_holes: Vec::new(),
        replacement_count: 0,
        alternatives: Vec::new(),
        reason: None,
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
                alternatives: Vec::new(),
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
        alternatives: Vec::new(),
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
    full_ast_fallback: bool,
    apply: bool,
    candidates: usize,
}

#[derive(Debug, Clone)]
pub struct SynthesizedSelectorGroup {
    body_idx: usize,
    target_bindings: Vec<SynthesizedTargetBinding>,
    match_source: String,
    rewritten_holes: Vec<String>,
    candidate_count: usize,
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
/// singleton and render everything else as selector holes. This first slice
/// builds the binding-to-declaration and declarator-slot index needed for exact
/// function/class recovery plus multi-declarator var gap minimization.
struct ChunkSelectorIndex {
    parsed: js_ast::ParsedJsModule,
    source: String,
    decls: Vec<IndexedDeclaration>,
    binding_to_decl: BTreeMap<String, Vec<usize>>,
    /// Built once per chunk and shared across every member and binding group so
    /// the minimizer's per-anchor matcher scans only plausible top-level body
    /// indices instead of the whole chunk. A pure prefilter — see
    /// [`matched_body_indices`].
    candidate_index: SelectorCandidateIndex,
    /// Layer-1 read-off shape index (W2). Built once per chunk; the migrated
    /// forms (single-target function and var) read their minimal anchor set off
    /// it instead of running the cover search. The matcher stays the gate.
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
    declarator_idx: Option<usize>,
}

fn rewrite_name_bindings_to_source_match(
    module: &str,
    file: &Path,
    root: &mut serde_yaml::Mapping,
    index: &ChunkSelectorIndex,
    selected_exports: Option<&BTreeSet<String>>,
    options: NameBindingRewriteOptions,
) -> Result<NameBindingRewriteOutcomes> {
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
                comment: mapping_get(member, "comment").and_then(value_as_string),
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
            options.minimize_synthesized_selectors,
            options.full_ast_fallback,
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

    // Anti-unification grouping (readoff_minimization.md items 5 + 7): collapse
    // maximal runs of adjacent, same-shape single-target declarations (any kind:
    // function, class, var) into one run-based binding_group. Multi-declarator var
    // groups (already grouped by shared declaration) and lone groups pass through
    // unchanged.
    let prepared_groups = merge_adjacent_same_shape_runs(index, synthesized_groups);

    // `Some(value)` replaces the member in place (singleton group rewrites to a
    // `source_match` member); `None` removes it (grouped members move into a
    // `binding_groups` entry). The borrow of `members` is released before we
    // touch `root` again to add `binding_groups`.
    let mut replacements: BTreeMap<usize, Option<Value>> = BTreeMap::new();
    let mut binding_groups = Vec::new();
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
                let replacement = member_with_source_match(
                    &member.export_name,
                    member.comment.clone(),
                    &synthesized.match_source,
                    &target.export_name,
                );
                replacements.insert(member.member_index, Some(replacement));
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
                }
            }
            if options.apply {
                binding_groups.push(binding_group_value(&synthesized, &group_members));
            }
        }
    }

    if options.apply {
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
        for (body_idx, item) in parsed.module.body.iter().enumerate() {
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
        let candidate_index = SelectorCandidateIndex::new(&parsed.module);
        let shape_index = ShapeIndex::new(&parsed.module);
        Self {
            parsed,
            source,
            decls,
            binding_to_decl,
            candidate_index,
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

/// Outcome of trying to synthesize a selector for one declaration group.
///
/// `Skipped` is returned when minimization produces no sparse selector and the
/// full-AST fallback is disabled (the default): rather than pin the exact AST
/// (rebuild-fragile), the caller skips the members with this reason.
enum GroupSelectorOutcome {
    Synthesized(SynthesizedSelectorGroup),
    Skipped(String),
}

fn synthesize_simplest_selector_for_group(
    index: &ChunkSelectorIndex,
    decl_idx: usize,
    members: &[NameBindingMember],
    minimize_synthesized_selectors: bool,
    full_ast_fallback: bool,
    candidates_limit: usize,
) -> Result<GroupSelectorOutcome> {
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
    let exact_proved =
        |full_ast_fallback: bool| -> Result<Option<(String, BTreeSet<String>, usize)>> {
            // The exact selector is the full-AST fallback. With `--no-minimize` the
            // caller explicitly opted out of minimization and wants the exact
            // selector, so emit it regardless of `full_ast_fallback`.
            if minimize_synthesized_selectors && !full_ast_fallback {
                return Ok(None);
            }
            let (exact_source, exact_holes) = exact_selector()?;
            let candidate_count = prove_synthesized_selector(index, decl, &targets, &exact_source)
                .context("proving exact synthesized selector")?;
            Ok(Some((exact_source, exact_holes, candidate_count)))
        };
    let specialized = minimize_synthesized_selectors
        .then(|| synthesize_specialized_selector(index, item, decl, &targets))
        .transpose()?
        .flatten();
    let proved = if let Some(specialized) = specialized {
        let match_source = trim_selector_source_line_suffixes(&specialized.match_source);
        match prove_synthesized_selector(index, decl, &targets, &match_source) {
            Ok(candidate_count) => {
                Some((match_source, specialized.rewritten_holes, candidate_count))
            }
            Err(_) => exact_proved(full_ast_fallback)?,
        }
    } else {
        exact_proved(full_ast_fallback)?
    };

    let Some((match_source, rewritten_holes, candidate_count)) = proved else {
        return Ok(GroupSelectorOutcome::Skipped(
            "minimization found no sparse selector; skipping full-AST pin \
             (pass --full-ast-fallback to emit the exact selector)"
                .to_string(),
        ));
    };

    // `--candidates N > 1`: collect the rest of the ranked read-off menu beyond the
    // primary pick (deduped against it). Only meaningful for the minimized forms;
    // the exact-fallback and the not-yet-covered object/var menus yield none.
    let alternatives = if candidates_limit > 1 && minimize_synthesized_selectors {
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
            candidate_count,
            alternatives,
        },
    ))
}

// ===========================================================================
// Anti-unification grouping (readoff_minimization.md items 5 + 7).
//
// `synthesize_simplest_selector_for_group` already groups members that share an
// enclosing declaration (multi-declarator var statements). The second grouping
// trigger — "minimal selectors overlap beyond a threshold" — collapses a run of
// adjacent, near-identical *single-target declarations* whose individually-
// minimized selectors share the bulk of their shape into one run-based
// binding_group, instead of N standalone source_match selectors. This started
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
/// groups into one run-based binding_group. Other groups (multi-declarator var
/// groups, lone declarations, anything whose merged run fails the matcher gate)
/// pass through unchanged, preserving source order.
fn merge_adjacent_same_shape_runs(
    index: &ChunkSelectorIndex,
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

/// Emit a candidate run: merge it into one binding_group when it holds ≥2 groups
/// and the merged selector proves unique, else emit each group individually.
fn flush_same_shape_run(
    index: &ChunkSelectorIndex,
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
    index: &ChunkSelectorIndex,
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
    index: &ChunkSelectorIndex,
    group: &SynthesizedDeclGroup,
) -> Option<IndexedDeclarationKind> {
    if group.members.len() != 1 {
        return None;
    }
    let kind = index.decls.get(group.decl_idx)?.kind;
    (kind != IndexedDeclarationKind::Other).then_some(kind)
}

/// Build one run-based binding_group from `run`: the source_match is the run's
/// declarations concatenated in source order, `exports` maps every target. The
/// merged selector is re-proven through the matcher gate; `None` (proof failed)
/// leaves the run to be emitted individually.
fn merge_same_shape_run(
    index: &ChunkSelectorIndex,
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
    let candidate_count = prove_synthesized_selector(index, decl, &targets, &match_source).ok()?;
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
            candidate_count,
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
    // Prove fast-path. The candidate index is a sound match superset: every
    // body index the matcher could resolve for this selector carries all of the
    // selector's anchor features, so true matches ⊆ the posting-list
    // intersection. Restrict the (correctness-bearing) matcher to that
    // intersection instead of scanning the whole chunk. When the intersection is
    // already `{decl.body_idx}` the matcher inspects a single item — proving
    // both existence and uniqueness — rather than the O(chunk) per-member scan
    // the unfiltered `member_binding_candidate_matches` would run. The matcher
    // still gates the result, so an index false positive (a body in the
    // intersection the matcher rejects) can never be accepted, and an empty/
    // ambiguous match still `bail!`s below. See `prove_fast_path_*` tests and
    // `selector_candidate_index`'s superset invariant.
    let candidates: BTreeSet<usize> = index
        .candidate_index
        .candidate_set_for_source_match(&selector)?
        .body_indices()
        .collect();
    let matches = source_match::member_binding_candidate_matches_within(
        &index.parsed.module,
        "<selector synthesis>",
        &selector,
        BodyIndexFilter::Restricted(&candidates),
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

/// Up to `limit` ranked candidate selectors for the item — the
/// `synthesize-selectors --candidates N` menu. The function/class read-off forms
/// return their full ranked walk; object/multi-declarator-var emit only the single
/// pick for now (their menus are not yet wired through the var/object read-off).
fn synthesize_specialized_selector_candidates(
    index: &ChunkSelectorIndex,
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
        IndexedDeclarationKind::Var | IndexedDeclarationKind::Other => {
            Ok(synthesize_specialized_selector(index, item, decl, targets)?
                .into_iter()
                .collect())
        }
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
    // On `None`, the caller falls back to the exact selector.
    minimize_function_selector(index, &function.function, decl, target)
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
    // On `None`, the caller falls back to the exact selector.
    minimize_class_selector(index, &class_decl.class, decl, target)
}

fn synthesize_specialized_var_selector(
    index: &ChunkSelectorIndex,
    item: &ModuleItem,
    decl: &IndexedDeclaration,
    targets: &[SynthesizedTargetBinding],
) -> Result<Option<SpecializedSelector>> {
    let var = item_var_decl(item).context("indexed var declaration no longer has var AST")?;
    // Single-target and multi-target vars both route through the AST-prune group
    // path (the single case is the N=1 group). On `None`, the caller falls back
    // to the exact selector.
    minimize_var_group_selector(index, var, decl, targets)
}

/// Every `(body, binding-slot)` alignment a `target_binding` selector with
/// `match_source` resolves to.
///
/// The shared per-chunk [`SelectorCandidateIndex`] narrows the matcher's scan to
/// the top-level body indices whose features could still match the selector,
/// turning the inner loop from O(all top-level statements) into O(plausible
/// candidates). The candidate set is a sound superset of the full-scan matches,
/// so the still-run structural matcher remains the source of truth: it proves
/// every reported match and discards index false positives. See
/// `prefilter_matches_brute_force_scan` for the superset invariant test.
fn matched_binding_candidates(
    index: &ChunkSelectorIndex,
    export_name: &str,
    match_source: &str,
) -> Result<Vec<source_match::MemberBindingMatch>> {
    let source_match = SourceMatch {
        match_source: match_source.to_string(),
        identifiers: SourceMatchIdentifierMode::AlphaAll,
        target_binding: Some(export_name.to_string()),
        target_statement: None,
        target_statements: None,
        wildcard_string_literals: BTreeSet::new(),
    };
    let selector = source_match.selector();
    let candidates: BTreeSet<usize> = index
        .candidate_index
        .candidate_set_for_source_match(&selector)?
        .body_indices()
        .collect();
    source_match::member_binding_candidate_matches_within(
        &index.parsed.module,
        "<selector minimization>",
        &selector,
        BodyIndexFilter::Restricted(&candidates),
    )
}

/// Distinct body indices the selector resolves to (slot alignments within one
/// body collapse). Used by the read-off structural fast path
/// ([`render_via_read_off`]).
fn matched_body_indices(
    index: &ChunkSelectorIndex,
    export_name: &str,
    match_source: &str,
) -> Result<BTreeSet<usize>> {
    Ok(
        matched_binding_candidates(index, export_name, match_source)?
            .iter()
            .map(|candidate| candidate.body_idx)
            .collect(),
    )
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
    for (idx, declarator) in var.decls.iter().enumerate() {
        if !target_decl_indices.contains(&idx) {
            skipped_run += 1;
            continue;
        }
        if skipped_run > 0 {
            let hole = declarator_hole_name();
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
    }
    if skipped_run > 0 {
        let hole = declarator_hole_name();
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

/// The declarator-run hole for a binding-group selector. The matcher treats every
/// `DECLARATORS` / `DECLARATORS_*` run hole identically — the positional suffix is
/// not equality-binding (only used in human-facing hint text) — so the renderer
/// always emits the plain keyword. (Suffixed forms remain accepted on input.)
fn declarator_hole_name() -> &'static str {
    DECLARATORS_HOLE_KEYWORD
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
        alternatives: input.synthesized.alternatives.clone(),
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

#[cfg(test)]
mod full_ast_fallback_tests {
    use super::*;

    fn outcome_for(
        source: &str,
        runtime: &str,
        minimize: bool,
        full_ast_fallback: bool,
    ) -> GroupSelectorOutcome {
        js_ast::with_swc_globals(|| {
            let parsed =
                js_ast::parse_js_module_consuming("<fallback-test>", source.to_string()).unwrap();
            let index = ChunkSelectorIndex::new(parsed);
            let decl_idx = *index
                .binding_to_decl
                .get(runtime)
                .and_then(|decls| decls.first())
                .expect("chunk declares the target binding");
            let members = [NameBindingMember {
                member_index: 0,
                export_name: "Target".to_string(),
                binding_name: runtime.to_string(),
                comment: None,
            }];
            synthesize_simplest_selector_for_group(
                &index,
                decl_idx,
                &members,
                minimize,
                full_ast_fallback,
                1,
            )
            .unwrap()
        })
    }

    // A function whose body's only discriminator (a deep numeric literal) is
    // not surfaced by the bounded body-anchor candidate generator, among
    // same-arity siblings: the minimizer finds no sparse selector, so synthesis
    // must fall back to the exact full-AST selector.
    const HARD_TO_MINIMIZE: &str = "\
function target(a, b) {\n\
  return wrap(a, b, deep(nest({ inner: [0, 0, 0, 0, 1234567] })));\n\
}\n\
function siblingOne(a, b) {\n\
  return wrap(a, b, deep(nest({ inner: [0, 0, 0, 0, 7654321] })));\n\
}\n\
function siblingTwo(a, b) {\n\
  return wrap(a, b, deep(nest({ inner: [0, 0, 0, 0, 9999999] })));\n\
}\n\
function wrap(a, b, c) { return c; }\n\
function deep(x) { return x; }\n\
function nest(x) { return x; }\n\
export { target };\n";

    #[test]
    fn full_ast_fallback_only_affects_the_exact_fallback_decision() {
        // Core invariant of the flag: enabling it can only turn a default skip
        // into an (exact) emission, never the reverse. Whenever the default
        // (fallback off) skips, the reason is the no-sparse-selector message and
        // the same group with the flag on emits instead.
        js_ast::with_swc_globals(|| {
            let index = ChunkSelectorIndex::new(
                js_ast::parse_js_module_consuming("<fallback-test>", HARD_TO_MINIMIZE.to_string())
                    .unwrap(),
            );
            let decl_idx = *index
                .binding_to_decl
                .get("target")
                .unwrap()
                .first()
                .unwrap();
            let members = [NameBindingMember {
                member_index: 0,
                export_name: "Target".to_string(),
                binding_name: "target".to_string(),
                comment: None,
            }];
            let off =
                synthesize_simplest_selector_for_group(&index, decl_idx, &members, true, false, 1)
                    .unwrap();
            if let GroupSelectorOutcome::Skipped(reason) = &off {
                assert!(reason.contains("no sparse selector"), "{reason}");
                let on = synthesize_simplest_selector_for_group(
                    &index, decl_idx, &members, true, true, 1,
                )
                .unwrap();
                assert!(
                    matches!(on, GroupSelectorOutcome::Synthesized(_)),
                    "fallback on must emit where the default skipped"
                );
            }
        });
    }

    #[test]
    fn full_ast_fallback_on_never_skips_where_off_could_emit() {
        // With the fallback enabled, the exact selector is emitted; the only
        // non-emission is a hard error (alpha-ambiguous chunk), never a skip.
        js_ast::with_swc_globals(|| {
            let index = ChunkSelectorIndex::new(
                js_ast::parse_js_module_consuming("<fallback-test>", HARD_TO_MINIMIZE.to_string())
                    .unwrap(),
            );
            let decl_idx = *index
                .binding_to_decl
                .get("target")
                .unwrap()
                .first()
                .unwrap();
            let members = [NameBindingMember {
                member_index: 0,
                export_name: "Target".to_string(),
                binding_name: "target".to_string(),
                comment: None,
            }];
            let GroupSelectorOutcome::Synthesized(group) =
                synthesize_simplest_selector_for_group(&index, decl_idx, &members, true, true, 1)
                    .unwrap()
            else {
                panic!("full_ast_fallback=true must not skip");
            };
            assert_eq!(group.candidate_count, 1);
            // The selector resolves uniquely back to the intended target.
            let matched = matched_body_indices(&index, "Target", &group.match_source).unwrap();
            assert_eq!(matched, BTreeSet::from([group.body_idx]));
        });
    }

    #[test]
    fn no_minimize_emits_exact_regardless_of_fallback_flag() {
        // `--no-minimize` is an explicit request for the exact selector, so it
        // emits even when full_ast_fallback is off.
        let outcome = outcome_for(HARD_TO_MINIMIZE, "target", false, false);
        assert!(matches!(outcome, GroupSelectorOutcome::Synthesized(_)));
    }
}

#[cfg(test)]
mod prefilter_soundness_tests {
    use super::*;

    /// Many same-shaped sibling `const`s plus a few non-var statements, so the
    /// candidate-index prefilter has both true matches to keep and unrelated
    /// statements to prune. Synthetic only.
    fn many_sibling_chunk() -> String {
        let mut chunk = String::new();
        for idx in 0..200 {
            chunk.push_str(&format!(
                "const binding{idx} = makeWidget(\"token-{idx}\", {{ role: \"button\" }});\n"
            ));
        }
        for idx in 0..50 {
            chunk.push_str(&format!("function helper{idx}(a, b) {{ return a + b; }}\n"));
        }
        chunk.push_str("sideEffect();\n");
        chunk
    }

    /// The full unfiltered scan: matcher over every top-level statement. Source
    /// of truth the prefilter must not under-include.
    fn brute_force_matches(
        index: &ChunkSelectorIndex,
        export: &str,
        source: &str,
    ) -> BTreeSet<usize> {
        let source_match = SourceMatch {
            match_source: source.to_string(),
            identifiers: SourceMatchIdentifierMode::AlphaAll,
            target_binding: Some(export.to_string()),
            target_statement: None,
            target_statements: None,
            wildcard_string_literals: BTreeSet::new(),
        };
        source_match::member_binding_candidate_matches_within(
            &index.parsed.module,
            "<brute-force>",
            &source_match.selector(),
            BodyIndexFilter::All,
        )
        .unwrap()
        .iter()
        .map(|matched| matched.body_idx)
        .collect()
    }

    #[test]
    fn prefilter_matches_brute_force_scan() {
        js_ast::with_swc_globals(|| {
            let index = ChunkSelectorIndex::new(
                js_ast::parse_js_module_consuming("<prefilter-test>", many_sibling_chunk())
                    .unwrap(),
            );
            // A selector specific enough to match exactly one sibling, and a holed
            // selector that matches every sibling: both must agree between the
            // prefiltered and brute-force scans.
            for (export, source) in [
                (
                    "Target",
                    r#"const Target = makeWidget("token-137", { role: "button" });"#,
                ),
                (
                    "Target",
                    r#"const Target = makeWidget(EXPR_HOLE, { role: "button" });"#,
                ),
            ] {
                let prefiltered = matched_body_indices(&index, export, source).unwrap();
                let brute = brute_force_matches(&index, export, source);
                let candidates: BTreeSet<usize> = index
                    .candidate_index
                    .candidate_set_for_source_match(&{
                        SourceMatch {
                            match_source: source.to_string(),
                            identifiers: SourceMatchIdentifierMode::AlphaAll,
                            target_binding: Some(export.to_string()),
                            target_statement: None,
                            target_statements: None,
                            wildcard_string_literals: BTreeSet::new(),
                        }
                        .selector()
                    })
                    .unwrap()
                    .body_indices()
                    .collect();
                assert!(
                    brute.is_subset(&candidates),
                    "candidate set must be a sound superset of the brute-force matches: \
                     brute={brute:?} candidates={candidates:?}"
                );
                assert_eq!(
                    prefiltered, brute,
                    "prefiltered scan must report identical matches to the full scan"
                );
            }
        });
    }

    /// Candidate set for the single-target prove selector (same construction as
    /// the fast-path in `prove_synthesized_selector`).
    fn prove_candidate_set(
        index: &ChunkSelectorIndex,
        export: &str,
        source: &str,
    ) -> BTreeSet<usize> {
        let source_match = SourceMatch {
            match_source: source.to_string(),
            identifiers: SourceMatchIdentifierMode::AlphaAll,
            target_binding: Some(export.to_string()),
            target_statement: None,
            target_statements: None,
            wildcard_string_literals: BTreeSet::new(),
        };
        index
            .candidate_index
            .candidate_set_for_source_match(&source_match.selector())
            .unwrap()
            .body_indices()
            .collect()
    }

    /// The prove fast-path's soundness contract: for sampled synthesized
    /// single-target selectors, whenever the index candidate set is a singleton
    /// `{body_idx}`, the production matcher must resolve uniquely to exactly that
    /// body index — i.e. an index-singleton proof never differs from the
    /// full-matcher verdict. Walks the synthesized selector for every `const`
    /// sibling in the chunk, so it covers both the genuinely-discriminated
    /// targets (singleton candidate set) and any that need matcher
    /// disambiguation (non-singleton, fall-through path).
    #[test]
    fn index_singleton_proof_implies_unique_matcher_resolution() {
        js_ast::with_swc_globals(|| {
            let index = ChunkSelectorIndex::new(
                js_ast::parse_js_module_consuming("<prove-fast-path>", many_sibling_chunk())
                    .unwrap(),
            );
            let mut singleton_hits = 0usize;
            for sample in [0usize, 1, 42, 137, 199] {
                let runtime = format!("binding{sample}");
                let decl_idx = *index
                    .binding_to_decl
                    .get(&runtime)
                    .and_then(|decls| decls.first())
                    .expect("chunk declares the sampled binding");
                let members = [NameBindingMember {
                    member_index: 0,
                    export_name: "Target".to_string(),
                    binding_name: runtime.clone(),
                    comment: None,
                }];
                let GroupSelectorOutcome::Synthesized(group) =
                    synthesize_simplest_selector_for_group(
                        &index, decl_idx, &members, true, false, 1,
                    )
                    .unwrap()
                else {
                    panic!("each distinct-token sibling must synthesize a selector");
                };

                let candidates = prove_candidate_set(&index, "Target", &group.match_source);
                // The full, unfiltered matcher verdict — the source of truth.
                let brute = brute_force_matches(&index, "Target", &group.match_source);
                assert!(
                    brute.is_subset(&candidates),
                    "index candidate set must be a sound match superset: \
                     brute={brute:?} candidates={candidates:?}"
                );
                if candidates == BTreeSet::from([decl_idx]) {
                    singleton_hits += 1;
                    // The crux: a singleton index proof must coincide with the
                    // matcher resolving uniquely to that same body index.
                    assert_eq!(
                        brute,
                        BTreeSet::from([decl_idx]),
                        "index-singleton proof must imply unique matcher resolution; \
                         selector:\n{}",
                        group.match_source
                    );
                }
                // Independently, the prove gate (which now runs the candidate-
                // restricted matcher) must agree the selector resolves uniquely.
                let targets = [SynthesizedTargetBinding {
                    export_name: "Target".to_string(),
                    runtime_binding: runtime,
                }];
                let decl = &index.decls[decl_idx];
                assert_eq!(
                    prove_synthesized_selector(&index, decl, &targets, &group.match_source)
                        .unwrap(),
                    1,
                    "prove gate must accept the synthesized selector as unique"
                );
            }
            assert!(
                singleton_hits > 0,
                "the distinct-token siblings must exercise the singleton fast-path"
            );
        });
    }
}
