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
use readoff_render::kept_spans_for_anchor_set;
use selector_candidate_index::SelectorCandidateIndex;
use serde::Serialize;
use serde_yaml::Value;
use shape_index::ShapeIndex;
use source_match::BodyIndexFilter;
use spec::{SourceMatch, SourceMatchIdentifierMode};
use spec_modules::{collect_module_files, is_module_yaml, module_path_from_file};
use swc_common::{BytePos, DUMMY_SP, Span, Spanned, SyntaxContext};
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitMut, VisitMutWith, VisitWith};

// Hole keyword spellings come from `source_match_holes` so the minimizer
// emits exactly the tokens the matcher resolves.
use source_match_holes::{
    ANYTHING_HOLE_KEYWORD, ARGS_HOLE_KEYWORD, CASE_REST_HOLE_KEYWORD, CLASS_REST_HOLE_KEYWORD,
    DECLARATORS_HOLE_KEYWORD, EXPR_HOLE_KEYWORD, OBJECT_PROPS_HOLE_KEYWORD, STMT_HOLE_KEYWORD,
    STMT_LIST_HOLE_KEYWORD, STRING_LITERAL_REGEX_PREDICATE,
};

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

    // `Some(value)` replaces the member in place (singleton group rewrites to a
    // `source_match` member); `None` removes it (grouped members move into a
    // `binding_groups` entry). The borrow of `members` is released before we
    // touch `root` again to add `binding_groups`.
    let mut replacements: BTreeMap<usize, Option<Value>> = BTreeMap::new();
    let mut binding_groups = Vec::new();
    let mut groups_changed = 0;
    for (group_id, (decl_idx, group_members)) in grouped.into_iter().enumerate() {
        let synthesized = match synthesize_simplest_selector_for_group(
            index,
            decl_idx,
            &group_members,
            options.minimize_synthesized_selectors,
            options.full_ast_fallback,
        ) {
            Ok(GroupSelectorOutcome::Synthesized(synthesized)) => synthesized,
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
                continue;
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

    Ok(GroupSelectorOutcome::Synthesized(
        SynthesizedSelectorGroup {
            body_idx: decl.body_idx,
            target_bindings: targets,
            match_source,
            rewritten_holes: rewritten_holes.into_iter().collect(),
            candidate_count,
        },
    ))
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

// ===========================================================================
// Retention-driven selector minimizer.
//
// A selector is the target rendered with a *retention set*: the byte spans of
// the concrete tokens (literals, member/property names, callees, object keys)
// the selector pins. A node renders concretely iff a kept span lies inside it;
// every other position is holed — `ANYTHING` for a bare expression, and the
// run holes `STMT_LIST` / `OBJECT_PROPS` / `CLASS_REST` for dropped statement /
// object-property / class-member runs.
//
// Anchor selection favors keeping shallow literals (direct values/args), then
// escalates by tier (structural key/member presence, then deeper literals).
// Function and class single targets resolve through a matcher-driven minimum
// set cover (`cover_competitors` + `min_set_cover` B&B): each anchor's exclusion
// set is computed by the production matcher, so discrimination is exact and the
// cover is minimum-cardinality. Var declarations (single and multi-target
// groups) share one keep-shallow-then-escalate path (`minimize_var_group_selector`,
// the single declarator being its N=1 case), proven through the binding-group
// matcher used as a resolves-uniquely oracle; it keeps each slot's direct shallow
// literals and may over-pin rather than run an exact-minimum cover. Either way the
// chosen union is rendered once and proven by the matcher. This is a mechanical
// first pass — it finds *a* robust pin for the routine cases, not necessarily the
// most semantically meaningful one.
// ===========================================================================

/// `(lo, hi)` byte offsets of a retained concrete token.
type AnchorSpan = (u32, u32);

const MAX_MINIMIZER_ANCHORS: usize = 64;

fn span_key(span: Span) -> AnchorSpan {
    (span.lo.0, span.hi.0)
}

fn node_holds_anchor(node: Span, anchor: AnchorSpan) -> bool {
    node.lo.0 <= anchor.0 && anchor.1 <= node.hi.0
}

fn node_retains_any(node: Span, kept: &BTreeSet<AnchorSpan>) -> bool {
    kept.iter().any(|anchor| node_holds_anchor(node, *anchor))
}

fn ident_node(name: &str) -> Ident {
    Ident::new_no_ctxt(name.into(), DUMMY_SP)
}

fn anything_expr() -> Expr {
    Expr::Ident(ident_node(ANYTHING_HOLE_KEYWORD))
}

fn stmt_list_stmt() -> Stmt {
    Stmt::Expr(ExprStmt {
        span: DUMMY_SP,
        expr: Box::new(Expr::Ident(ident_node(STMT_LIST_HOLE_KEYWORD))),
    })
}

fn object_props_prop() -> PropOrSpread {
    PropOrSpread::Prop(Box::new(Prop::Shorthand(ident_node(
        OBJECT_PROPS_HOLE_KEYWORD,
    ))))
}

/// A `case CASE_REST:` switch-case hole that absorbs a run of dropped
/// `case`/`default` clauses (the switch analog of `CLASS_REST;`).
fn case_rest_case() -> SwitchCase {
    SwitchCase {
        span: DUMMY_SP,
        test: Some(Box::new(Expr::Ident(ident_node(CASE_REST_HOLE_KEYWORD)))),
        cons: vec![],
    }
}

fn anything_pat() -> Pat {
    Pat::Ident(BindingIdent {
        id: ident_node(ANYTHING_HOLE_KEYWORD),
        type_ann: None,
    })
}

fn holed_block(block: &BlockStmt, kept: &BTreeSet<AnchorSpan>) -> BlockStmt {
    let mut holed = block.clone();
    holed.stmts = hole_stmts(&block.stmts, kept);
    holed
}

/// Prune an expression into selector form: keep concrete tokens whose span is in
/// `kept`, and replace every subtree off a kept token's path with an `ANYTHING`
/// hole node. The result is an ordinary `swc` AST emitted by codegen, not a
/// hand-built string.
fn hole_expr(expr: &Expr, kept: &BTreeSet<AnchorSpan>) -> Expr {
    if !node_retains_any(expr.span(), kept) {
        return anything_expr();
    }
    match expr {
        Expr::Paren(paren) => hole_expr(&paren.expr, kept),
        Expr::Lit(_) | Expr::Ident(_) | Expr::Tpl(_) => expr.clone(),
        Expr::Member(member) => {
            let mut holed = member.clone();
            holed.obj = Box::new(hole_expr(&member.obj, kept));
            Expr::Member(holed)
        }
        Expr::Call(call) => {
            let mut holed = call.clone();
            holed.callee = hole_callee(&call.callee, kept);
            holed.args = hole_args(&call.args, kept);
            Expr::Call(holed)
        }
        Expr::New(new_expr) => {
            let mut holed = new_expr.clone();
            holed.callee = Box::new(hole_callee_expr(&new_expr.callee, kept));
            holed.args = new_expr.args.as_ref().map(|args| hole_args(args, kept));
            Expr::New(holed)
        }
        Expr::Object(object) => Expr::Object(hole_object(object, kept)),
        Expr::Await(await_expr) => {
            let mut holed = await_expr.clone();
            holed.arg = Box::new(hole_expr(&await_expr.arg, kept));
            Expr::Await(holed)
        }
        Expr::Unary(unary) => {
            let mut holed = unary.clone();
            holed.arg = Box::new(hole_expr(&unary.arg, kept));
            Expr::Unary(holed)
        }
        Expr::Bin(bin) => {
            let mut holed = bin.clone();
            holed.left = Box::new(hole_expr(&bin.left, kept));
            holed.right = Box::new(hole_expr(&bin.right, kept));
            Expr::Bin(holed)
        }
        // Unmodeled shapes carrying a kept anchor: keep verbatim rather than
        // risk an unsound hole. Over-pinning here is a future refinement.
        _ => expr.clone(),
    }
}

/// Hole a call's callee while keeping the invoked identity — a method name or a
/// bare function reference is a meaningful, stable pin; only a member receiver
/// is holed.
fn hole_callee(callee: &Callee, kept: &BTreeSet<AnchorSpan>) -> Callee {
    match callee {
        Callee::Expr(expr) => Callee::Expr(Box::new(hole_callee_expr(expr, kept))),
        Callee::Super(_) | Callee::Import(_) => callee.clone(),
    }
}

fn hole_callee_expr(expr: &Expr, kept: &BTreeSet<AnchorSpan>) -> Expr {
    match expr {
        Expr::Member(member) => {
            let mut holed = member.clone();
            holed.obj = Box::new(hole_expr(&member.obj, kept));
            Expr::Member(holed)
        }
        Expr::Ident(_) => expr.clone(),
        Expr::Paren(paren) => hole_callee_expr(&paren.expr, kept),
        _ => hole_expr(expr, kept),
    }
}

fn hole_args(args: &[ExprOrSpread], kept: &BTreeSet<AnchorSpan>) -> Vec<ExprOrSpread> {
    args.iter()
        .map(|arg| {
            let mut holed = arg.clone();
            if arg.spread.is_none() {
                holed.expr = Box::new(hole_expr(&arg.expr, kept));
            }
            holed
        })
        .collect()
}

fn hole_object(object: &ObjectLit, kept: &BTreeSet<AnchorSpan>) -> ObjectLit {
    let mut props = Vec::new();
    let mut dropped_run = false;
    for prop in &object.props {
        if node_retains_any(prop.span(), kept) {
            if dropped_run {
                props.push(object_props_prop());
                dropped_run = false;
            }
            props.push(hole_prop(prop, kept));
        } else {
            dropped_run = true;
        }
    }
    if dropped_run {
        props.push(object_props_prop());
    }
    let mut holed = object.clone();
    holed.props = props;
    holed
}

/// Hole an object literal for the read-off object form: keep only props carrying
/// a kept anchor, with an `OBJECT_PROPS` run hole before the first kept prop,
/// after the last, and **between every pair** of kept props.
///
/// Unlike [`hole_object`] (which only emits a list hole where a run of props was
/// actually dropped), this pads both edges and interleaves unconditionally.
/// Object properties are unordered enum/lookup entries: a kept key can move on a
/// rebuild, so anchoring one to the object's edge (`anchored_right` in the
/// matcher) or assuming two kept keys stay adjacent is fragile. Surrounding every
/// kept prop with `OBJECT_PROPS` matches each as an independent interior
/// subsequence element, so a minimal *key set* survives key reorder and arbitrary
/// gaps. With no kept prop this is a bare `{ OBJECT_PROPS }`; with one it is the
/// padded single-key form (unchanged from the edge-padding behavior).
fn hole_object_padded(object: &ObjectLit, kept: &BTreeSet<AnchorSpan>) -> ObjectLit {
    let mut props = vec![object_props_prop()];
    for prop in &object.props {
        if node_retains_any(prop.span(), kept) {
            props.push(hole_prop(prop, kept));
            props.push(object_props_prop());
        }
    }
    let mut holed = object.clone();
    holed.props = props;
    holed
}

fn hole_prop(prop: &PropOrSpread, kept: &BTreeSet<AnchorSpan>) -> PropOrSpread {
    let PropOrSpread::Prop(inner) = prop else {
        return prop.clone();
    };
    if let Prop::KeyValue(key_value) = inner.as_ref() {
        let mut holed = key_value.clone();
        holed.value = Box::new(hole_expr(&key_value.value, kept));
        PropOrSpread::Prop(Box::new(Prop::KeyValue(holed)))
    } else {
        prop.clone()
    }
}

/// Hole a statement list, collapsing runs of dropped statements into a single
/// `STMT_LIST;` hole statement.
fn hole_stmts(stmts: &[Stmt], kept: &BTreeSet<AnchorSpan>) -> Vec<Stmt> {
    let mut out = Vec::new();
    let mut dropped_run = false;
    for stmt in stmts {
        if node_retains_any(stmt.span(), kept) {
            if dropped_run {
                out.push(stmt_list_stmt());
                dropped_run = false;
            }
            out.push(hole_stmt(stmt, kept));
        } else {
            dropped_run = true;
        }
    }
    if dropped_run || out.is_empty() {
        out.push(stmt_list_stmt());
    }
    out
}

fn hole_stmt(stmt: &Stmt, kept: &BTreeSet<AnchorSpan>) -> Stmt {
    match stmt {
        Stmt::Expr(expr_stmt) => {
            let mut holed = expr_stmt.clone();
            holed.expr = Box::new(hole_expr(&expr_stmt.expr, kept));
            Stmt::Expr(holed)
        }
        Stmt::Return(ret) => {
            let mut holed = ret.clone();
            holed.arg = ret.arg.as_ref().map(|arg| Box::new(hole_expr(arg, kept)));
            Stmt::Return(holed)
        }
        Stmt::Throw(throw) => {
            let mut holed = throw.clone();
            holed.arg = Box::new(hole_expr(&throw.arg, kept));
            Stmt::Throw(holed)
        }
        Stmt::Decl(Decl::Var(var)) => {
            let mut holed = (**var).clone();
            for declarator in &mut holed.decls {
                if let Some(init) = &declarator.init {
                    declarator.init = Some(Box::new(hole_expr(init, kept)));
                }
            }
            Stmt::Decl(Decl::Var(Box::new(holed)))
        }
        Stmt::If(if_stmt) => {
            let mut holed = if_stmt.clone();
            holed.test = Box::new(hole_expr(&if_stmt.test, kept));
            let cons_stmts = match if_stmt.cons.as_ref() {
                Stmt::Block(block) => hole_stmts(&block.stmts, kept),
                other => hole_stmts(std::slice::from_ref(other), kept),
            };
            holed.cons = Box::new(Stmt::Block(BlockStmt {
                span: DUMMY_SP,
                ctxt: SyntaxContext::empty(),
                stmts: cons_stmts,
            }));
            holed.alt = None;
            Stmt::If(holed)
        }
        Stmt::Try(try_stmt) => {
            let mut holed = try_stmt.clone();
            holed.block = holed_block(&try_stmt.block, kept);
            if let Some(handler) = &mut holed.handler {
                if handler.param.is_some() {
                    handler.param = Some(anything_pat());
                }
                handler.body = holed_block(&handler.body, kept);
            }
            if let Some(finalizer) = &mut holed.finalizer {
                *finalizer = holed_block(finalizer, kept);
            }
            Stmt::Try(holed)
        }
        Stmt::Block(block) => Stmt::Block(holed_block(block, kept)),
        Stmt::Switch(switch) => {
            let mut holed = switch.clone();
            holed.discriminant = Box::new(hole_expr(&switch.discriminant, kept));
            holed.cases = hole_switch_cases(&switch.cases, kept);
            Stmt::Switch(holed)
        }
        // Unmodeled statement shapes carrying a kept anchor: keep verbatim.
        _ => stmt.clone(),
    }
}

/// Prune a `switch`'s case list: drop runs of non-discriminating
/// `case`/`default` clauses into `case CASE_REST:` holes, keeping only the
/// clauses that retain an anchor (their test literal or a body statement).
/// Mirrors [`hole_class_members`].
fn hole_switch_cases(cases: &[SwitchCase], kept: &BTreeSet<AnchorSpan>) -> Vec<SwitchCase> {
    let mut out = Vec::new();
    let mut dropped_run = false;
    for case in cases {
        if node_retains_any(case.span(), kept) {
            if dropped_run {
                out.push(case_rest_case());
                dropped_run = false;
            }
            out.push(hole_switch_case(case, kept));
        } else {
            dropped_run = true;
        }
    }
    if dropped_run || out.is_empty() {
        out.push(case_rest_case());
    }
    out
}

fn hole_switch_case(case: &SwitchCase, kept: &BTreeSet<AnchorSpan>) -> SwitchCase {
    let mut holed = case.clone();
    holed.test = case
        .test
        .as_ref()
        .map(|test| Box::new(hole_expr(test, kept)));
    holed.cons = hole_stmts(&case.cons, kept);
    holed
}

/// Candidate concrete anchors, split into preference tiers. Literal values are
/// stable, meaningful landmarks (a magic string, a config number/bool), so they
/// are tried before structural member/property and key names. Among literals,
/// shallower ones (fewer enclosing call/new levels) are preferred: a direct
/// `key: "primary"` pins less nested structure — and is more rebuild-robust —
/// than a literal buried inside `mk("primary")`.
#[derive(Default)]
struct AnchorCandidates {
    /// `(span, call-nesting depth)` for each literal.
    literals: Vec<(AnchorSpan, u32)>,
    structural: Vec<AnchorSpan>,
}

/// A literal at most this many call/new levels deep counts as a *direct* value
/// or argument — a meaningful, stable pin worth keeping. Deeper literals are
/// treated as buried in incidental computation and ranked below structural
/// key/member presence.
const SHALLOW_LITERAL_DEPTH: u32 = 1;

impl AnchorCandidates {
    /// Anchor tiers, most-preferred first:
    /// 1. shallow literals (direct values/args), by ascending call depth;
    /// 2. structural key/member presence;
    /// 3. deeper literals (buried in nested calls).
    ///
    /// Structural presence sits above deep literals because pinning a key's
    /// existence is leaner and more rebuild-robust than pinning a volatile
    /// computed value (`stableKey: ANYTHING` over `stableKey: mk("x")`).
    fn tiers(&self) -> Vec<Vec<AnchorSpan>> {
        let literal_tier = |depth: u32| -> Vec<AnchorSpan> {
            self.literals
                .iter()
                .filter(|(_, anchor_depth)| *anchor_depth == depth)
                .map(|(span, _)| *span)
                .collect()
        };
        let mut tiers = Vec::new();
        let Some(max_depth) = self.literals.iter().map(|(_, depth)| *depth).max() else {
            if !self.structural.is_empty() {
                tiers.push(self.structural.clone());
            }
            return tiers;
        };
        for depth in 0..=max_depth.min(SHALLOW_LITERAL_DEPTH) {
            let tier = literal_tier(depth);
            if !tier.is_empty() {
                tiers.push(tier);
            }
        }
        if !self.structural.is_empty() {
            tiers.push(self.structural.clone());
        }
        for depth in (SHALLOW_LITERAL_DEPTH + 1)..=max_depth {
            let tier = literal_tier(depth);
            if !tier.is_empty() {
                tiers.push(tier);
            }
        }
        tiers
    }

    /// Shallow literal anchors — direct values/args worth keeping as meaningful
    /// per-slot pins even when structure alone would already discriminate. This
    /// includes object-property values (`kind: "primary"`): the unified anchor
    /// policy favors keeping shallow literals over an exact-minimum cover, so an
    /// occasional over-pin (a shared `enabled: true`) is accepted as the price
    /// of a single policy across single and group targets.
    fn shallow_literals(&self) -> Vec<AnchorSpan> {
        self.literals
            .iter()
            .filter(|(_, depth)| *depth <= SHALLOW_LITERAL_DEPTH)
            .map(|(span, _)| *span)
            .collect()
    }

    /// Tiers consulted only when shallow literals are not enough on their own:
    /// structural key/member presence, then deeper literals by ascending depth.
    fn deep_cover_tiers(&self) -> Vec<Vec<AnchorSpan>> {
        let mut tiers = Vec::new();
        if !self.structural.is_empty() {
            tiers.push(self.structural.clone());
        }
        if let Some(max_depth) = self.literals.iter().map(|(_, depth)| *depth).max() {
            for depth in (SHALLOW_LITERAL_DEPTH + 1)..=max_depth {
                let tier: Vec<AnchorSpan> = self
                    .literals
                    .iter()
                    .filter(|(_, anchor_depth)| *anchor_depth == depth)
                    .map(|(span, _)| *span)
                    .collect();
                if !tier.is_empty() {
                    tiers.push(tier);
                }
            }
        }
        tiers
    }
}

fn collect_stmt_list_anchors(stmts: &[Stmt]) -> AnchorCandidates {
    let mut candidates = AnchorCandidates::default();
    for stmt in stmts {
        collect_stmt_anchors(stmt, &mut candidates);
    }
    candidates
}

fn collect_stmt_anchors(stmt: &Stmt, candidates: &mut AnchorCandidates) {
    match stmt {
        Stmt::Expr(expr_stmt) => collect_expr_anchors(&expr_stmt.expr, 0, candidates),
        Stmt::Return(ret) => {
            if let Some(arg) = &ret.arg {
                collect_expr_anchors(arg, 0, candidates);
            }
        }
        Stmt::Throw(throw) => collect_expr_anchors(&throw.arg, 0, candidates),
        Stmt::Decl(Decl::Var(var)) => {
            for declarator in &var.decls {
                if let Some(init) = &declarator.init {
                    collect_expr_anchors(init, 0, candidates);
                }
            }
        }
        Stmt::If(if_stmt) => {
            collect_expr_anchors(&if_stmt.test, 0, candidates);
            collect_block_anchors(&if_stmt.cons, candidates);
        }
        Stmt::Try(try_stmt) => {
            for stmt in &try_stmt.block.stmts {
                collect_stmt_anchors(stmt, candidates);
            }
        }
        Stmt::Block(block) => {
            for stmt in &block.stmts {
                collect_stmt_anchors(stmt, candidates);
            }
        }
        Stmt::Switch(switch) => {
            collect_expr_anchors(&switch.discriminant, 0, candidates);
            for case in &switch.cases {
                // A case test literal (`case "x":`) is the discriminating
                // landmark that the `CASE_REST` hole keeps; collect it as a
                // shallow literal so the cover can retain it.
                if let Some(test) = &case.test {
                    collect_expr_anchors(test, 0, candidates);
                }
                for stmt in &case.cons {
                    collect_stmt_anchors(stmt, candidates);
                }
            }
        }
        _ => {}
    }
}

fn collect_block_anchors(stmt: &Stmt, candidates: &mut AnchorCandidates) {
    match stmt {
        Stmt::Block(block) => {
            for stmt in &block.stmts {
                collect_stmt_anchors(stmt, candidates);
            }
        }
        _ => collect_stmt_anchors(stmt, candidates),
    }
}

/// `depth` counts enclosing call/new levels, so the tiered cover can prefer
/// shallower literal anchors. Object-property values (`kind: "primary"`) are
/// collected at the enclosing depth like any other literal; the unified
/// keep-shallow policy retains them rather than treating them as incidental.
fn collect_expr_anchors(expr: &Expr, depth: u32, candidates: &mut AnchorCandidates) {
    match expr {
        Expr::Lit(_) | Expr::Tpl(_) => {
            candidates.literals.push((span_key(expr.span()), depth));
        }
        Expr::Paren(paren) => collect_expr_anchors(&paren.expr, depth, candidates),
        Expr::Member(member) => {
            collect_expr_anchors(&member.obj, depth, candidates);
            if let MemberProp::Ident(ident) = &member.prop {
                candidates.structural.push(span_key(ident.span));
            } else if let MemberProp::Computed(computed) = &member.prop {
                collect_expr_anchors(&computed.expr, depth, candidates);
            }
        }
        Expr::Call(call) => {
            if let Callee::Expr(callee) = &call.callee {
                collect_expr_anchors(callee, depth, candidates);
            }
            for arg in &call.args {
                if arg.spread.is_none() {
                    collect_expr_anchors(&arg.expr, depth + 1, candidates);
                }
            }
        }
        Expr::New(new_expr) => {
            collect_expr_anchors(&new_expr.callee, depth, candidates);
            for arg in new_expr.args.as_deref().unwrap_or_default() {
                if arg.spread.is_none() {
                    collect_expr_anchors(&arg.expr, depth + 1, candidates);
                }
            }
        }
        Expr::Object(object) => {
            for prop in &object.props {
                if let PropOrSpread::Prop(prop) = prop {
                    if let Prop::KeyValue(key_value) = prop.as_ref() {
                        candidates.structural.push(span_key(key_value.key.span()));
                        collect_expr_anchors(&key_value.value, depth, candidates);
                    }
                }
            }
        }
        Expr::Await(await_expr) => collect_expr_anchors(&await_expr.arg, depth, candidates),
        Expr::Unary(unary) => collect_expr_anchors(&unary.arg, depth, candidates),
        Expr::Bin(bin) => {
            collect_expr_anchors(&bin.left, depth, candidates);
            collect_expr_anchors(&bin.right, depth, candidates);
        }
        _ => {}
    }
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
/// body collapse). Used by the body-index cover ([`cover_competitors`]).
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

fn holes_present(source: &str) -> BTreeSet<String> {
    let mut holes = BTreeSet::new();
    for keyword in [
        ANYTHING_HOLE_KEYWORD,
        STMT_LIST_HOLE_KEYWORD,
        OBJECT_PROPS_HOLE_KEYWORD,
        CLASS_REST_HOLE_KEYWORD,
        DECLARATORS_HOLE_KEYWORD,
    ] {
        if source.contains(keyword) {
            holes.insert(keyword.to_string());
        }
    }
    holes
}

/// Minimal anchor cover for a single-target declaration. `render_with` renders
/// the declaration's selector given a kept-anchor set, and `candidates` are its
/// concrete anchors. Competitors are exactly the items the maximally-holed
/// selector still matches; the tiered cover picks the fewest anchors that rule
/// them out, and the chosen selector is rendered and proven once.
fn minimize_via_retention(
    index: &ChunkSelectorIndex,
    decl: &IndexedDeclaration,
    target: &SynthesizedTargetBinding,
    candidates: &AnchorCandidates,
    render_with: &impl Fn(&BTreeSet<AnchorSpan>) -> Result<String>,
) -> Result<Option<SpecializedSelector>> {
    let empty = BTreeSet::new();
    let mut competitors = matched_body_indices(index, &target.export_name, &render_with(&empty)?)?;
    competitors.remove(&decl.body_idx);
    if competitors.is_empty() {
        return finish_minimized_selector(index, decl, target, render_with(&empty)?);
    }
    let Some(chosen) = cover_competitors(
        index,
        target,
        &competitors,
        &candidates.tiers(),
        render_with,
    )?
    else {
        return Ok(None);
    };
    finish_minimized_selector(index, decl, target, render_with(&chosen)?)
}

fn minimize_function_selector(
    index: &ChunkSelectorIndex,
    function: &Function,
    decl: &IndexedDeclaration,
    target: &SynthesizedTargetBinding,
) -> Result<Option<SpecializedSelector>> {
    let Some(body) = &function.body else {
        return Ok(None);
    };
    let render_with = |kept: &BTreeSet<AnchorSpan>| -> Result<String> {
        let mut holed = function.clone();
        holed.params = function.params.iter().map(|_| anything_param()).collect();
        if let Some(holed_body) = &mut holed.body {
            holed_body.stmts = hole_stmts(&body.stmts, kept);
        }
        emit_selector(ModuleItem::Stmt(Stmt::Decl(Decl::Fn(FnDecl {
            ident: ident_node(&target.export_name),
            declare: false,
            function: Box::new(holed),
        }))))
    };
    // W2: single-target functions read their minimal anchor set off the shape
    // index instead of running the cover search. The holed scaffold already
    // pins the param arity (one `ANYTHING` per real param), so a structural /
    // skeleton anchor needs no kept span; value anchors (string literals,
    // member/method names, member-path callees) map to the spans of the tokens
    // that exhibit them. The matcher proves the result (gate 1).
    //
    // Fallback: when the read-off cannot single out the target through its own
    // features — chiefly because the discriminator is a *number/bool* literal,
    // which the W1 feature taxonomy does not yet index (string literals only) —
    // it returns `None` and we fall through to the cover search, which collects
    // every literal kind. This keeps the migration honest (read-off is the
    // primary path; the cover is the not-yet-expressible tail), not full-AST:
    // the cover itself skips rather than dumping an untrimmed body. A later wave
    // that extends the feature taxonomy can drop this fallback.
    if let Some(selector) = render_via_read_off(index, decl, target, &render_with)? {
        return Ok(Some(selector));
    }
    let candidates = collect_stmt_list_anchors(&body.stmts);
    minimize_via_retention(index, decl, target, &candidates, &render_with)
}

/// Render a single-target selector via the W1 read-off API (W2).
///
/// Reads the minimal [`AnchorSet`] off the shape index, maps its value anchors
/// to kept byte spans over the target item, and renders + proves through the
/// supplied `render_with` (the same prune + codegen the cover path uses — no
/// second serializer). Returns `None` when the read-off cannot single out the
/// target (a genuine alpha-duplicate) or the rendered selector fails the matcher
/// gate, so the caller falls back to current behavior (exact-or-skip; never a
/// full-AST pin unless `--full-ast-fallback`).
fn render_via_read_off(
    index: &ChunkSelectorIndex,
    decl: &IndexedDeclaration,
    target: &SynthesizedTargetBinding,
    render_with: &impl Fn(&BTreeSet<AnchorSpan>) -> Result<String>,
) -> Result<Option<SpecializedSelector>> {
    // Structural fast path (mirrors the cover's empty-competitors case): if the
    // holed scaffold already pins enough — the declaration kind plus the param
    // arity / declarator shape the scaffold renders for free — to resolve
    // uniquely, keep no concrete token. This is the leanest, most rebuild-stable
    // selector, so it is preferred over any value anchor the read-off would add.
    let empty = BTreeSet::new();
    if matched_body_indices(index, &target.export_name, &render_with(&empty)?)?
        == BTreeSet::from([decl.body_idx])
    {
        return finish_minimized_selector(index, decl, target, render_with(&empty)?);
    }

    let Some(anchor_set) = index.shape_index.minimal_anchor_set(decl.body_idx) else {
        return Ok(None);
    };
    let item = index
        .parsed
        .module
        .body
        .get(decl.body_idx)
        .context("read-off body index no longer in module")?;
    let kept = kept_spans_for_anchor_set(item, &anchor_set);
    finish_minimized_selector(index, decl, target, render_with(&kept)?)
}

// ===========================================================================
// Regex-over-string-literal anchors (`STR_LITERAL_MATCHING_RE("<pattern>")`).
//
// A var-binding selector that pins an exact string literal breaks whenever a
// rebuild perturbs a volatile fragment of that literal (a content hash, build
// counter, or other generated tail). When the *stable* part of the literal
// already discriminates the target from its siblings, we can pin that stable
// structure with a regex and wildcard the volatile fragment, so the selector
// survives the rebuild.
//
// Derivation rule (intentionally conservative — see
// `selector_minimizer_discrimination.md`):
//
//   * We only derive a pattern when the literal ends in a *volatile tail*: a
//     trailing run of hex/digits. The tail must be at least
//     `MIN_VOLATILE_TAIL_LEN` chars so a one- to three-char numeric suffix
//     (which is more likely meaningful than generated) is left alone. Any
//     separator before the tail (`chunk-`, `main.`) stays in the pinned prefix.
//   * The derived pattern is `^<escaped stable prefix><tail class>$`, anchored
//     so `Regex::is_match` (which is otherwise a substring test) pins the whole
//     value. The stable prefix is escaped with `regex::escape`, so every
//     metacharacter in the literal is matched literally; only the volatile tail
//     becomes a character-class wildcard (`[0-9A-Fa-f]+` for a hex tail,
//     `[0-9]+` for a pure-digit tail).
//   * If the whole literal would be the volatile tail (no stable prefix), we
//     return `None`: a bare `^[0-9]+$` pins nothing meaningful and would almost
//     never discriminate.
//
// Limits we deliberately accept: this recognizes only trailing hex/digit
// volatility (the dominant bundler pattern: `chunk-a1b2c3`, `main.4f3a2b.js`,
// `vendor_1024`). It does not model embedded volatile fragments, GUID shapes,
// or base64 hashes. The cover never *requires* a regex anchor — it is offered
// only as an upgrade of an already-kept exact literal, and is taken only when
// the upgraded selector still resolves uniquely (so an over-broad pattern is
// rejected, never emitted). A pattern that fails to compile as `regex::Regex`
// is likewise never emitted.
// ===========================================================================

/// Minimum length of a trailing hex/digit run for it to count as a volatile
/// fragment worth wildcarding. Short numeric suffixes (`v2`, `s3`) are more
/// often meaningful than generated, so they stay pinned exactly.
const MIN_VOLATILE_TAIL_LEN: usize = 4;

/// Derive an anchored `STR_LITERAL_MATCHING_RE` pattern that pins the stable
/// prefix of `value` and wildcards a trailing volatile hex/digit fragment, or
/// `None` when no meaningful stable-prefix/volatile-tail split exists. The
/// returned pattern is always a valid `regex::Regex` (the only metacharacters
/// it introduces are the anchors and a character class; the prefix is escaped).
fn regex_anchor_pattern(value: &str) -> Option<String> {
    let chars: Vec<char> = value.chars().collect();
    // Length of the trailing run of hex digits.
    let hex_tail = chars
        .iter()
        .rev()
        .take_while(|c| c.is_ascii_hexdigit())
        .count();
    if hex_tail < MIN_VOLATILE_TAIL_LEN {
        return None;
    }
    // Prefer a pure-digit tail class when the whole tail is decimal; otherwise
    // a hex class. (Hex is a superset of digits, so the digit class is the
    // tighter, more honest wildcard when applicable.)
    let tail_is_decimal = chars[chars.len() - hex_tail..]
        .iter()
        .all(char::is_ascii_digit);
    let tail_class = if tail_is_decimal {
        "[0-9]+"
    } else {
        "[0-9A-Fa-f]+"
    };
    let stable_prefix: String = chars[..chars.len() - hex_tail].iter().collect();
    // A regex anchor must pin *something* stable: an empty or separator-only
    // prefix discriminates nothing, so decline. The trailing separator (if any)
    // stays in the pinned, escaped prefix — `chunk-a1b2c3` pins `chunk-`, not
    // `chunk`, which is the conservative choice (one fewer wildcarded char).
    if stable_prefix.is_empty() || stable_prefix.chars().all(|c| matches!(c, '-' | '_' | '.')) {
        return None;
    }
    let pattern = format!("^{}{tail_class}$", regex::escape(&stable_prefix));
    // Guard the invariant directly: never hand the matcher a pattern it cannot
    // compile (the matcher silently treats an uncompilable pattern as a
    // non-match, which would make the selector match nothing).
    regex::Regex::new(&pattern).ok()?;
    Some(pattern)
}

/// Build the `STR_LITERAL_MATCHING_RE("<pattern>")` call expression the matcher
/// interprets as a regex-over-string-literal predicate.
fn regex_predicate_call(pattern: &str) -> Expr {
    Expr::Call(CallExpr {
        span: DUMMY_SP,
        ctxt: SyntaxContext::empty(),
        callee: Callee::Expr(Box::new(Expr::Ident(ident_node(
            STRING_LITERAL_REGEX_PREDICATE,
        )))),
        args: vec![ExprOrSpread {
            spread: None,
            expr: Box::new(Expr::Lit(Lit::Str(Str {
                span: DUMMY_SP,
                value: pattern.into(),
                raw: None,
            }))),
        }],
        type_args: None,
    })
}

/// Replace each kept string literal whose span is a chosen regex anchor with the
/// `STR_LITERAL_MATCHING_RE` predicate call. Runs as a post-pass over the holed
/// selector AST: holed literals keep their original source span, so matching by
/// span here is exact and never touches a literal the cover did not select.
struct RegexAnchorSubstitution<'a> {
    patterns: &'a BTreeMap<AnchorSpan, String>,
}

impl VisitMut for RegexAnchorSubstitution<'_> {
    fn visit_mut_expr(&mut self, expr: &mut Expr) {
        if let Expr::Lit(Lit::Str(str_lit)) = expr {
            if let Some(pattern) = self.patterns.get(&span_key(str_lit.span)) {
                *expr = regex_predicate_call(pattern);
                return;
            }
        }
        expr.visit_mut_children_with(self);
    }
}

/// Candidate regex anchors for the var-binding minimizer: the `(span, pattern)`
/// of each string literal in a target slot's init for which `regex_anchor_pattern`
/// yields a wildcarding pattern. Only literals already in the kept set are ever
/// upgraded, so this is a superset filtered against `kept` at upgrade time.
fn collect_regex_anchor_candidates(init: &Expr) -> BTreeMap<AnchorSpan, String> {
    #[derive(Default)]
    struct Collector {
        candidates: BTreeMap<AnchorSpan, String>,
    }
    impl Visit for Collector {
        fn visit_expr(&mut self, expr: &Expr) {
            if let Expr::Lit(Lit::Str(str_lit)) = expr {
                if let Some(pattern) =
                    regex_anchor_pattern(str_lit.value.to_string_lossy().as_ref())
                {
                    self.candidates.insert(span_key(str_lit.span), pattern);
                }
            }
            expr.visit_children_with(self);
        }
    }
    let mut collector = Collector::default();
    init.visit_with(&mut collector);
    collector.candidates
}

/// A `DECLARATORS_<pos> = null` declarator hole absorbing a run of non-target
/// declarators in a binding-group selector.
fn declarator_hole(name: &str) -> VarDeclarator {
    VarDeclarator {
        span: DUMMY_SP,
        name: named_pat(name),
        init: Some(Box::new(Expr::Lit(Lit::Null(Null { span: DUMMY_SP })))),
        definite: false,
    }
}

/// Ranked anchor spans for an object literal's key-set cover, best-first: each
/// direct literal/template **value** (a value-level discriminator, tried first)
/// then each **key** (the key-set discriminator). Value member accesses and
/// nested expressions are intentionally excluded — a minified `.prop` is
/// rebuild-volatile, so the cover pins the stable key and holes the value to
/// `ANYTHING` rather than anchoring on a churning property name.
///
/// Within each class the order is source order; the cover's slot-resolution
/// greedy ([`cover_object_slot`]) then picks the most discriminating anchor, so
/// this ordering only breaks ties — preferring a unique value (`accent:
/// "…Accent"`) over its equally-unique key (`accent: ANYTHING`) when both single
/// out the slot.
fn object_anchor_ranking(object: &ObjectLit) -> Vec<AnchorSpan> {
    let key_value_props = || {
        object.props.iter().filter_map(|prop| match prop {
            PropOrSpread::Prop(prop) => match prop.as_ref() {
                Prop::KeyValue(key_value) => Some(key_value),
                _ => None,
            },
            PropOrSpread::Spread(_) => None,
        })
    };
    let values = key_value_props()
        .filter(|kv| matches!(kv.value.as_ref(), Expr::Lit(_) | Expr::Tpl(_)))
        .map(|kv| span_key(kv.value.span()));
    let keys = key_value_props().map(|kv| span_key(kv.key.span()));
    values.chain(keys).collect()
}

/// Slot-aware minimal cover for a single-target object inside a `var` group: a
/// greedy key-set set-cover that, at each step, adds the `ranked` anchor that best
/// steers the selector toward resolving to the target binding's own declarator
/// slot, until it proves unique. Unlike [`cover_competitors`] (which covers
/// distinct body indices and so cannot see two sibling declarators of the *same*
/// statement), this scores by whether the **target binding** is the one the
/// selector resolves, then by the match count.
///
/// The matcher reports one alignment per body (the leftmost declarator the holed
/// pattern fits), so a key shared with an *earlier* sibling slot
/// (`blue: "#00f"`, also in `firstPalette`) resolves there, not to the target —
/// hence the score's first key is "did the target slot resolve at all", which a
/// key/value unique to the target slot (`accent`) flips true. Keeps adding the
/// best anchor until the matcher's uniqueness proof passes, or the target's own
/// anchors are exhausted (then `None`, and the caller keeps its keep-shallow
/// form).
fn cover_object_slot(
    index: &ChunkSelectorIndex,
    decl: &IndexedDeclaration,
    target: &SynthesizedTargetBinding,
    ranked: &[AnchorSpan],
    render_with: &impl Fn(&BTreeSet<AnchorSpan>) -> Result<String>,
) -> Result<Option<SpecializedSelector>> {
    let targets = std::slice::from_ref(target);
    let mut kept: BTreeSet<AnchorSpan> = BTreeSet::new();
    while prove_synthesized_selector(index, decl, targets, &render_with(&kept)?).is_err() {
        // Score a trial by `(target slot not yet resolved, total matches)`; a
        // smaller score is better, so an anchor that makes the target binding the
        // resolved one and rules out the most competitors wins.
        let mut best: Option<((bool, usize), AnchorSpan)> = None;
        for &anchor in ranked.iter().take(MAX_MINIMIZER_ANCHORS) {
            if kept.contains(&anchor) {
                continue;
            }
            let mut trial = kept.clone();
            trial.insert(anchor);
            let matches =
                matched_binding_candidates(index, &target.export_name, &render_with(&trial)?)?;
            let target_unresolved = !matches.iter().any(|m| {
                m.body_idx == decl.body_idx && m.binding.binding_name == target.runtime_binding
            });
            let score = (target_unresolved, matches.len());
            if best.is_none_or(|(best_score, _)| score < best_score) {
                best = Some((score, anchor));
            }
        }
        // No remaining anchor to add: the target's own keys/values cannot single
        // out its slot. Defer to the caller.
        let Some((_, anchor)) = best else {
            return Ok(None);
        };
        kept.insert(anchor);
    }
    finish_minimized_selector(index, decl, target, render_with(&kept)?)
}

/// Read off a minimal selector for a single-target `var`/`let`/`const` whose
/// target declarator value is an object literal (W3 + key-set minimization).
///
/// Handles the single-target object whether it stands alone or sits inside a
/// multi-declarator group (one target slot, `DECLARATORS_*` holes for the rest).
/// Two passes, both rendering through one slot-aware `render_with` that holes the
/// object with [`hole_object_padded`] (interleaved `OBJECT_PROPS`) so the kept key
/// subset survives key reorder:
///
///   1. **Read-off.** The chunk-wide shape index ranks the statement's own
///      features by selective × stable, so a globally-rare discriminating
///      key/value wins. Its kept spans are restricted to the target declarator (a
///      group's anchor set may name a key carried only by a *sibling* declarator
///      this selector holes away), and the matcher proves the restricted pin.
///   2. **Cover fallback.** A matcher-driven minimal cover over the target
///      object's own keys (and direct literal values) — the key-set analogue of
///      greedy set-cover: anchor the rarest discriminating keys, `OBJECT_PROPS`
///      the common ones. This is what singles out a target object inside a
///      multi-declarator group, where the chunk-wide read-off cannot see the
///      per-slot key sets.
///
/// Returns `None` — so the caller falls back to the keep-shallow group path —
/// when the target is not a single object declarator or neither pass resolves it.
fn try_object_read_off(
    index: &ChunkSelectorIndex,
    var: &VarDecl,
    decl: &IndexedDeclaration,
    targets: &[SynthesizedTargetBinding],
    target_slots: &BTreeSet<usize>,
) -> Result<Option<SpecializedSelector>> {
    // Only the single-target case (one binding). A multi-target group needs the
    // tuple-resolving binding-group path; this owns the single object slot.
    let [target] = targets else {
        return Ok(None);
    };
    if target_slots.len() != 1 {
        return Ok(None);
    }
    let target_slot = *target_slots.iter().next().expect("one target slot");
    let declarator = &var.decls[target_slot];
    let Some(Expr::Object(object)) = declarator.init.as_deref() else {
        return Ok(None);
    };
    let target_decl_span = declarator.span();

    // Slot-aware render: `DECLARATORS_*` holes for the non-target declarators
    // (none when the target stands alone), the target's object holed to its kept
    // keys padded + interleaved with `OBJECT_PROPS`.
    let render_with = |kept: &BTreeSet<AnchorSpan>| -> Result<String> {
        let mut decls: Vec<VarDeclarator> = Vec::new();
        let mut skipped_run = false;
        let mut target_seen = 0usize;
        for (idx, other) in var.decls.iter().enumerate() {
            if idx != target_slot {
                skipped_run = true;
                continue;
            }
            if skipped_run {
                decls.push(declarator_hole(declarator_hole_name(target_seen, 1)));
                skipped_run = false;
            }
            let mut holed = other.clone();
            holed.name = named_pat(&target.export_name);
            holed.init = Some(Box::new(Expr::Object(hole_object_padded(object, kept))));
            decls.push(holed);
            target_seen += 1;
        }
        if skipped_run {
            decls.push(declarator_hole(declarator_hole_name(target_seen, 1)));
        }
        let mut holed_var = var.clone();
        holed_var.declare = false;
        holed_var.decls = decls;
        emit_selector(ModuleItem::Stmt(Stmt::Decl(Decl::Var(Box::new(holed_var)))))
    };

    if let Some(anchor_set) = index.shape_index.minimal_anchor_set(decl.body_idx) {
        let item = index
            .parsed
            .module
            .body
            .get(decl.body_idx)
            .context("read-off body index no longer in module")?;
        let kept: BTreeSet<AnchorSpan> = kept_spans_for_anchor_set(item, &anchor_set)
            .into_iter()
            .filter(|span| node_holds_anchor(target_decl_span, *span))
            .collect();
        if !kept.is_empty()
            && let Some(selector) =
                finish_minimized_selector(index, decl, target, render_with(&kept)?)?
        {
            return Ok(Some(selector));
        }
    }

    cover_object_slot(
        index,
        decl,
        target,
        &object_anchor_ranking(object),
        &render_with,
    )
}

/// Minimal anchor cover for a `var`/`let`/`const` binding group: render the
/// shared declaration keeping the target declarators (each `export = <holed
/// init>`) with `DECLARATORS_*` holes for non-target runs, and pin just enough
/// anchors for the group to resolve uniquely to the right declaration and
/// bindings. A single-declarator target is the N=1 case of this path: one
/// target slot, no `DECLARATORS_*` gaps, and the binding-group matcher's tuple
/// proof degenerates to the single-binding case in `prove_synthesized_selector`.
///
/// The proof uses `prove_synthesized_selector` as a boolean oracle (it yields a
/// count or an error rather than a candidate set), adding anchors tier by tier
/// (shallow literals first) until the group resolves correctly.
fn minimize_var_group_selector(
    index: &ChunkSelectorIndex,
    var: &VarDecl,
    decl: &IndexedDeclaration,
    targets: &[SynthesizedTargetBinding],
) -> Result<Option<SpecializedSelector>> {
    let export_for = |runtime: &str| {
        targets
            .iter()
            .find(|target| target.runtime_binding == runtime)
            .map(|target| target.export_name.as_str())
    };
    let target_slots: BTreeSet<usize> = var
        .decls
        .iter()
        .enumerate()
        .filter_map(|(idx, declarator)| {
            let name = single_ident_pat_name(&declarator.name)?;
            export_for(name).map(|_| idx)
        })
        .collect();
    if target_slots.len() != targets.len() {
        return Ok(None);
    }

    // W3 + key-set minimization: a single-target var whose target declarator is
    // an object literal reads its minimal selector off the shape index, then
    // (slot-aware) covers the target object's own keys — `OBJECT_PROPS` holes
    // around the discriminating key subset instead of keeping every key (the
    // `object_keys_over_pinned` / key-set group over-pin). Works whether the
    // object stands alone or sits inside a multi-declarator group. The matcher
    // proves it (gate 1); on `None` we fall through to the keep-shallow group path
    // below, so a target neither pass can single out is handled exactly as before.
    if let Some(selector) = try_object_read_off(index, var, decl, targets, &target_slots)? {
        return Ok(Some(selector));
    }

    let render_with = |kept: &BTreeSet<AnchorSpan>,
                       regex_anchors: &BTreeMap<AnchorSpan, String>|
     -> Result<String> {
        let mut decls: Vec<VarDeclarator> = Vec::new();
        let mut skipped_run = false;
        let mut target_seen = 0usize;
        for (idx, declarator) in var.decls.iter().enumerate() {
            if !target_slots.contains(&idx) {
                skipped_run = true;
                continue;
            }
            if skipped_run {
                decls.push(declarator_hole(declarator_hole_name(
                    target_seen,
                    targets.len(),
                )));
                skipped_run = false;
            }
            let name = single_ident_pat_name(&declarator.name)
                .expect("target declarator is a plain identifier");
            let mut holed = declarator.clone();
            holed.name = named_pat(export_for(name).expect("target declarator has an export"));
            holed.init = declarator.init.as_ref().map(|init| {
                let mut holed_init = hole_expr(init, kept);
                if !regex_anchors.is_empty() {
                    // Post-pass: swap each kept literal whose span was chosen as
                    // a regex anchor for the `STR_LITERAL_MATCHING_RE` predicate.
                    holed_init.visit_mut_with(&mut RegexAnchorSubstitution {
                        patterns: regex_anchors,
                    });
                }
                Box::new(holed_init)
            });
            decls.push(holed);
            target_seen += 1;
        }
        if skipped_run {
            decls.push(declarator_hole(declarator_hole_name(
                target_seen,
                targets.len(),
            )));
        }
        let mut holed_var = var.clone();
        holed_var.declare = false;
        holed_var.decls = decls;
        emit_selector(ModuleItem::Stmt(Stmt::Decl(Decl::Var(Box::new(holed_var)))))
    };

    let mut candidates = AnchorCandidates::default();
    for (idx, declarator) in var.decls.iter().enumerate() {
        if target_slots.contains(&idx) {
            if let Some(init) = &declarator.init {
                collect_expr_anchors(init, 0, &mut candidates);
            }
        }
    }

    let no_regex = BTreeMap::new();
    let resolves = |kept: &BTreeSet<AnchorSpan>| -> Result<bool> {
        Ok(
            prove_synthesized_selector(index, decl, targets, &render_with(kept, &no_regex)?)
                .is_ok(),
        )
    };
    // Always keep each slot's shallow literals (a group's declarators are its
    // meaningful targets), then escalate to structural/deeper anchors only if
    // the group does not yet resolve uniquely to the right bindings.
    let mut kept: BTreeSet<AnchorSpan> = candidates.shallow_literals().into_iter().collect();
    if !resolves(&kept)? {
        for tier in candidates.deep_cover_tiers() {
            kept.extend(tier);
            if resolves(&kept)? {
                break;
            }
        }
        if !resolves(&kept)? {
            return Ok(None);
        }
    }

    // Regex-literal upgrade: among kept string literals, offer a robust
    // `STR_LITERAL_MATCHING_RE` anchor for each that has a derivable
    // stable-prefix/volatile-tail pattern, but only accept the upgrade when the
    // resulting selector *still* resolves uniquely (gate 1). The exact-literal
    // form is the default; regex is opt-in by merit. Upgrades are applied one
    // literal at a time so a too-broad pattern on one literal never blocks a
    // sound upgrade on another.
    let mut regex_anchors: BTreeMap<AnchorSpan, String> = BTreeMap::new();
    for (idx, declarator) in var.decls.iter().enumerate() {
        if !target_slots.contains(&idx) {
            continue;
        }
        let Some(init) = &declarator.init else {
            continue;
        };
        for (span, pattern) in collect_regex_anchor_candidates(init) {
            if !kept.contains(&span) || regex_anchors.contains_key(&span) {
                continue;
            }
            regex_anchors.insert(span, pattern);
            let trial = render_with(&kept, &regex_anchors)?;
            if prove_synthesized_selector(index, decl, targets, &trial).is_err() {
                // The regex anchor broke uniqueness (over-broad among siblings),
                // so back it out and keep the exact literal.
                regex_anchors.remove(&span);
            }
        }
    }

    let source = render_with(&kept, &regex_anchors)?;
    let rewritten_holes = holes_present(&source);
    Ok(Some(SpecializedSelector {
        match_source: source,
        rewritten_holes,
    }))
}

/// Minimal anchor cover for a single-target class: render the class keeping
/// only members (and member-body statements) that carry a chosen anchor, with
/// `CLASS_REST` for dropped member runs, and cover sibling classes.
fn minimize_class_selector(
    index: &ChunkSelectorIndex,
    class: &Class,
    decl: &IndexedDeclaration,
    target: &SynthesizedTargetBinding,
) -> Result<Option<SpecializedSelector>> {
    let export = target.export_name.clone();
    let render_with = |kept: &BTreeSet<AnchorSpan>| -> Result<String> {
        let mut holed_class = class.clone();
        // A minified superclass identifier is alpha-wildcarded, so always hole
        // `extends` to ANYTHING — it still discriminates "has a superclass" from a
        // bare class without pinning the volatile name.
        holed_class.super_class = class
            .super_class
            .as_ref()
            .map(|_| Box::new(anything_expr()));
        holed_class.body = hole_class_members(&class.body, kept);
        emit_selector(ModuleItem::Stmt(Stmt::Decl(Decl::Class(ClassDecl {
            ident: ident_node(&export),
            declare: false,
            class: Box::new(holed_class),
        }))))
    };
    // Single-target classes read off their minimal anchor set the same way
    // functions and objects do: the holed scaffold pins the class kind plus
    // `extends ANYTHING`, and value anchors (a member name, a literal or callee
    // inside a member body) map to their token spans so only the member runs
    // carrying them survive between `CLASS_REST` holes. Fall back to the tiered
    // cover search when the read-off cannot single the class out through its own
    // value features.
    if let Some(selector) = render_via_read_off(index, decl, target, &render_with)? {
        return Ok(Some(selector));
    }
    let candidates = collect_class_anchors(class);
    minimize_via_retention(index, decl, target, &candidates, &render_with)
}

fn anything_param() -> Param {
    Param {
        span: DUMMY_SP,
        decorators: vec![],
        pat: anything_pat(),
    }
}

fn named_pat(name: &str) -> Pat {
    Pat::Ident(BindingIdent {
        id: ident_node(name),
        type_ann: None,
    })
}

/// A `CLASS_REST;` class-field hole that absorbs a run of dropped members.
fn class_rest_member() -> ClassMember {
    ClassMember::ClassProp(ClassProp {
        span: DUMMY_SP,
        key: PropName::Ident(IdentName::new(CLASS_REST_HOLE_KEYWORD.into(), DUMMY_SP)),
        value: None,
        type_ann: None,
        is_static: false,
        decorators: vec![],
        accessibility: None,
        is_abstract: false,
        is_optional: false,
        is_override: false,
        readonly: false,
        declare: false,
        definite: false,
    })
}

fn hole_class_members(members: &[ClassMember], kept: &BTreeSet<AnchorSpan>) -> Vec<ClassMember> {
    let mut out = Vec::new();
    let mut dropped_run = false;
    for member in members {
        if node_retains_any(member.span(), kept) {
            if dropped_run {
                out.push(class_rest_member());
                dropped_run = false;
            }
            out.push(hole_class_member(member, kept));
        } else {
            dropped_run = true;
        }
    }
    if dropped_run || out.is_empty() {
        out.push(class_rest_member());
    }
    out
}

fn hole_class_member(member: &ClassMember, kept: &BTreeSet<AnchorSpan>) -> ClassMember {
    match member {
        ClassMember::Method(m) => {
            let mut holed = m.clone();
            holed.function.params = m.function.params.iter().map(|_| anything_param()).collect();
            holed.function.body = m.function.body.as_ref().map(|body| holed_block(body, kept));
            ClassMember::Method(holed)
        }
        ClassMember::PrivateMethod(m) => {
            let mut holed = m.clone();
            holed.function.params = m.function.params.iter().map(|_| anything_param()).collect();
            holed.function.body = m.function.body.as_ref().map(|body| holed_block(body, kept));
            ClassMember::PrivateMethod(holed)
        }
        ClassMember::Constructor(ctor) => {
            let mut holed = ctor.clone();
            holed.params = ctor
                .params
                .iter()
                .map(|_| ParamOrTsParamProp::Param(anything_param()))
                .collect();
            holed.body = ctor.body.as_ref().map(|body| holed_block(body, kept));
            ClassMember::Constructor(holed)
        }
        // Class fields and other members carrying a kept anchor: keep verbatim.
        _ => member.clone(),
    }
}

/// Emit a synthesized selector item (a holed declaration) to source via the
/// shared codegen — the only AST→string step, and the matcher's parse inverts it.
fn emit_selector(item: ModuleItem) -> Result<String> {
    js_ast::emit_module_source(&Module {
        span: DUMMY_SP,
        body: vec![item],
        shebang: None,
    })
}

fn collect_class_anchors(class: &Class) -> AnchorCandidates {
    let mut candidates = AnchorCandidates::default();
    for member in &class.body {
        if let Some(span) = class_member_name_span(member) {
            candidates.structural.push(span_key(span));
        }
        match member {
            ClassMember::Method(m) => collect_block_stmt_anchors(&m.function.body, &mut candidates),
            ClassMember::PrivateMethod(m) => {
                collect_block_stmt_anchors(&m.function.body, &mut candidates)
            }
            ClassMember::Constructor(ctor) => {
                collect_block_stmt_anchors(&ctor.body, &mut candidates)
            }
            ClassMember::ClassProp(prop) => {
                if let Some(value) = &prop.value {
                    collect_expr_anchors(value, 0, &mut candidates);
                }
            }
            _ => {}
        }
    }
    candidates
}

fn collect_block_stmt_anchors(body: &Option<BlockStmt>, candidates: &mut AnchorCandidates) {
    if let Some(block) = body {
        for stmt in &block.stmts {
            collect_stmt_anchors(stmt, candidates);
        }
    }
}

fn class_member_name_span(member: &ClassMember) -> Option<Span> {
    let prop_name_span = |name: &PropName| match name {
        PropName::Ident(ident) => Some(ident.span),
        _ => None,
    };
    match member {
        ClassMember::Method(m) => prop_name_span(&m.key),
        ClassMember::ClassProp(prop) => prop_name_span(&prop.key),
        _ => None,
    }
}

/// Tiered minimum set cover. Tiers are tried most-preferred first; within a
/// tier, the matcher gives each anchor's exclusion set and a minimum-cardinality
/// cover (branch-and-bound) clears as many still-uncovered competitors as the
/// tier can, leaving the rest to later tiers. Tier order encodes meaning
/// preference; minimum cardinality within a tier avoids greedy over-pinning.
/// The final selector is proven once by the caller. Returns `None` if even all
/// anchors together cannot single out the target.
fn cover_competitors(
    index: &ChunkSelectorIndex,
    target: &SynthesizedTargetBinding,
    competitors: &BTreeSet<usize>,
    tiers: &[Vec<AnchorSpan>],
    render_with: &impl Fn(&BTreeSet<AnchorSpan>) -> Result<String>,
) -> Result<Option<BTreeSet<AnchorSpan>>> {
    let mut uncovered = competitors.clone();
    let mut chosen = BTreeSet::new();
    for tier in tiers {
        if uncovered.is_empty() {
            break;
        }
        let mut exclusions: Vec<(AnchorSpan, BTreeSet<usize>)> = Vec::new();
        for &anchor in tier.iter().take(MAX_MINIMIZER_ANCHORS) {
            let survivors = matched_body_indices(
                index,
                &target.export_name,
                &render_with(&BTreeSet::from([anchor]))?,
            )?;
            let excluded: BTreeSet<usize> = uncovered.difference(&survivors).copied().collect();
            if !excluded.is_empty() {
                exclusions.push((anchor, excluded));
            }
        }
        let coverable: BTreeSet<usize> = exclusions
            .iter()
            .flat_map(|(_, excluded)| excluded.iter().copied())
            .collect();
        if coverable.is_empty() {
            continue;
        }
        for anchor in min_set_cover(&exclusions, &coverable) {
            chosen.insert(anchor);
        }
        uncovered = uncovered.difference(&coverable).copied().collect();
    }
    if uncovered.is_empty() {
        Ok(Some(chosen))
    } else {
        Ok(None)
    }
}

/// Minimum-cardinality set cover of `universe` by `sets` (each an anchor and the
/// competitors it excludes). Branch-and-bound on the least-coverable element;
/// `universe` is the union of all sets, so a cover always exists. Instances are
/// tiny (anchors and competitors are per-chunk), so exhaustive search with
/// best-length pruning stays well within budget and avoids greedy over-pinning.
fn min_set_cover(
    sets: &[(AnchorSpan, BTreeSet<usize>)],
    universe: &BTreeSet<usize>,
) -> Vec<AnchorSpan> {
    let mut best: Option<Vec<AnchorSpan>> = None;
    let mut chosen: Vec<AnchorSpan> = Vec::new();
    min_set_cover_search(sets, universe.clone(), &mut chosen, &mut best);
    best.unwrap_or_default()
}

fn min_set_cover_search(
    sets: &[(AnchorSpan, BTreeSet<usize>)],
    remaining: BTreeSet<usize>,
    chosen: &mut Vec<AnchorSpan>,
    best: &mut Option<Vec<AnchorSpan>>,
) {
    if remaining.is_empty() {
        if best.as_ref().is_none_or(|found| chosen.len() < found.len()) {
            *best = Some(chosen.clone());
        }
        return;
    }
    if best
        .as_ref()
        .is_some_and(|found| chosen.len() + 1 >= found.len())
    {
        return;
    }
    // Branch on the still-uncovered competitor that the fewest anchors exclude:
    // every cover must include one of those anchors, which keeps the tree narrow.
    let Some(pivot) = remaining.iter().copied().min_by_key(|competitor| {
        sets.iter()
            .filter(|(_, excluded)| excluded.contains(competitor))
            .count()
    }) else {
        return;
    };
    for (anchor, excluded) in sets {
        if chosen.contains(anchor) || !excluded.contains(&pivot) {
            continue;
        }
        chosen.push(*anchor);
        min_set_cover_search(
            sets,
            remaining.difference(excluded).copied().collect(),
            chosen,
            best,
        );
        chosen.pop();
    }
}

fn finish_minimized_selector(
    index: &ChunkSelectorIndex,
    decl: &IndexedDeclaration,
    target: &SynthesizedTargetBinding,
    source: String,
) -> Result<Option<SpecializedSelector>> {
    let targets = std::slice::from_ref(target);
    if prove_synthesized_selector(index, decl, targets, &source).is_err() {
        return Ok(None);
    }
    let rewritten_holes = holes_present(&source);
    Ok(Some(SpecializedSelector {
        match_source: source,
        rewritten_holes,
    }))
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
mod regex_anchor_pattern_tests {
    use super::*;

    #[test]
    fn pins_stable_prefix_and_wildcards_hex_tail() {
        let pattern = regex_anchor_pattern("chunk-a1b2c3").expect("derivable hex tail");
        assert_eq!(pattern, "^chunk\\-[0-9A-Fa-f]+$");
        let re = regex::Regex::new(&pattern).unwrap();
        // The pattern matches the seen value and rebuild variants of the same
        // stable prefix, but not a different prefix.
        assert!(re.is_match("chunk-a1b2c3"));
        assert!(re.is_match("chunk-ffffff"));
        assert!(!re.is_match("widget-a1b2c3"));
        // Anchored: a longer string sharing the prefix must not match.
        assert!(!re.is_match("chunk-a1b2c3-extra"));
    }

    #[test]
    fn prefers_decimal_class_for_pure_digit_tail() {
        let pattern = regex_anchor_pattern("vendor_1024").expect("derivable digit tail");
        assert_eq!(pattern, "^vendor_[0-9]+$");
        let re = regex::Regex::new(&pattern).unwrap();
        assert!(re.is_match("vendor_1024"));
        assert!(re.is_match("vendor_9999"));
        // The decimal class is tighter than hex: a hex-only rebuild would not
        // match, which is fine — we wildcard only what we observed (digits).
        assert!(!re.is_match("vendor_abcd"));
    }

    #[test]
    fn escapes_regex_metacharacters_in_the_prefix() {
        let pattern = regex_anchor_pattern("main.bundle.4f3a2b").expect("derivable");
        assert_eq!(pattern, "^main\\.bundle\\.[0-9A-Fa-f]+$");
        let re = regex::Regex::new(&pattern).unwrap();
        assert!(re.is_match("main.bundle.4f3a2b"));
        // The `.`s are literal, not any-char wildcards.
        assert!(!re.is_match("mainXbundleY4f3a2b"));
    }

    #[test]
    fn declines_short_numeric_suffixes() {
        // A two/three-char numeric suffix is more likely meaningful than
        // generated, so no pattern is offered (`MIN_VOLATILE_TAIL_LEN`).
        assert_eq!(regex_anchor_pattern("v2"), None);
        assert_eq!(regex_anchor_pattern("step3"), None);
        assert_eq!(regex_anchor_pattern("h2o"), None);
    }

    #[test]
    fn declines_when_no_stable_prefix_remains() {
        // The whole literal is the volatile tail: a bare digit/hex anchor pins
        // nothing meaningful.
        assert_eq!(regex_anchor_pattern("123456"), None);
        assert_eq!(regex_anchor_pattern("deadbeef"), None);
        // Separator-only prefix is likewise rejected.
        assert_eq!(regex_anchor_pattern("-123456"), None);
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
                synthesize_simplest_selector_for_group(&index, decl_idx, &members, true, false)
                    .unwrap();
            if let GroupSelectorOutcome::Skipped(reason) = &off {
                assert!(reason.contains("no sparse selector"), "{reason}");
                let on =
                    synthesize_simplest_selector_for_group(&index, decl_idx, &members, true, true)
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
                synthesize_simplest_selector_for_group(&index, decl_idx, &members, true, true)
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
                    synthesize_simplest_selector_for_group(&index, decl_idx, &members, true, false)
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
