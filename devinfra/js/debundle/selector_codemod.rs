//! Mechanical, reviewable spec selector rewrites.
//!
//! This powers `debundle spec selector-codemod`: a scripting-safe CLI for
//! applying proven YAML-only selector rewrites across a modules tree. The
//! first rewrite is deliberately narrow: member-form `selector.source_match`
//! entries that declare exactly one selector-local top-level binding and have
//! no `target_binding` get that `target_binding` filled in automatically.

use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use serde::Serialize;
use serde_yaml::{Mapping, Value};
use spec::SourceMatch;
use spec_modules::{collect_module_files, module_path_from_file};

#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum SelectorCodemodRewrite {
    SingleTargetBinding,
}

impl SelectorCodemodRewrite {
    pub fn name(self) -> &'static str {
        match self {
            Self::SingleTargetBinding => "single_target_binding",
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
    pub reason: Option<String>,
}

#[derive(Debug, Clone, Default, Serialize)]
pub struct SelectorCodemodSummary {
    pub dry_run: bool,
    pub files_scanned: usize,
    pub modules_scanned: usize,
    pub members_scanned: usize,
    pub source_match_members: usize,
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
    let files = collect_module_files(&config.modules_root)
        .with_context(|| format!("walking {}", config.modules_root.display()))?;
    let selected_files = config
        .files
        .iter()
        .map(|path| resolve_file_filter(&config.modules_root, path))
        .collect::<BTreeSet<_>>();
    let selected_modules = config.modules.iter().cloned().collect::<BTreeSet<_>>();

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
            reason: Some(reason),
        }));
    };

    insert_mapping_key_before_match(
        source_match_mapping,
        "target_binding",
        Value::String(target.clone()),
    );
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
        reason: None,
    }))
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

fn insert_mapping_key_before_match(mapping: &mut Mapping, key: &str, value: Value) {
    let key_value = yk(key);
    if mapping.contains_key(&key_value) {
        mapping.insert(key_value, value);
        return;
    }

    let old = std::mem::take(mapping);
    for (existing_key, existing_value) in old {
        if existing_key.as_str() == Some("match") {
            mapping.insert(key_value.clone(), value.clone());
        }
        mapping.insert(existing_key, existing_value);
    }
    if !mapping.contains_key(&key_value) {
        mapping.insert(key_value, value);
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
                let target = candidate.target_binding.as_deref().unwrap_or("-");
                out.push_str(&format!(
                    "  {:?} {} member#{} [{}] target_binding={}\n",
                    candidate.action, candidate.module, candidate.member_index, readable, target
                ));
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
