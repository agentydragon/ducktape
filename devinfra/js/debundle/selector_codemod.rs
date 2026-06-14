//! Mechanical, reviewable spec selector rewrites.
//!
//! This powers `debundle spec selector-codemod`: a scripting-safe CLI for
//! applying proven YAML-only selector rewrites across a modules tree. The
//! first rewrites are deliberately narrow: member-form `selector.source_match`
//! entries that declare exactly one selector-local top-level binding can have
//! `target_binding` filled in automatically, and anonymous typed holes can be
//! normalized to `ANYTHING` now that the matcher supports it.

use std::collections::BTreeSet;
use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail};
use serde::Serialize;
use serde_yaml::Value;
use spec::SourceMatch;
use spec_modules::{collect_module_files, module_path_from_file};
use swc_common::{BytePos, Span};
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitWith};

const ANYTHING_HOLE_KEYWORD: &str = "ANYTHING";
const EXPR_HOLE_KEYWORD: &str = "EXPR";
const STMT_HOLE_KEYWORD: &str = "STMT";
const CLASS_REST_HOLE_KEYWORD: &str = "CLASS_REST";
const DECLARATORS_HOLE_KEYWORD: &str = "DECLARATORS";
const ARGS_HOLE_KEYWORD: &str = "ARGS";
const OBJECT_PROPS_HOLE_KEYWORD: &str = "OBJECT_PROPS";

#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum SelectorCodemodRewrite {
    SingleTargetBinding,
    AnythingHoles,
}

impl SelectorCodemodRewrite {
    pub fn name(self) -> &'static str {
        match self {
            Self::SingleTargetBinding => "single_target_binding",
            Self::AnythingHoles => "anything_holes",
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

        let original_text =
            fs::read_to_string(&file).with_context(|| format!("reading {}", file.display()))?;
        let mut doc = yaml_edit::read_yaml(&file)?;
        let mut file_changed = false;
        let mut text_insertions = Vec::new();
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
                    &original_text,
                    config.apply,
                ),
                SelectorCodemodRewrite::AnythingHoles => {
                    rewrite_anything_holes(&module, &file, member_index, member, config.apply)
                        .map(|outcome| outcome.map(SelectorCodemodOutcome::candidate_only))
                }
            };
            let Some(outcome) = candidate? else {
                continue;
            };
            if let Some(insertion) = outcome.text_insertion {
                text_insertions.push(insertion);
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

        if config.apply && file_changed {
            let written = match config.rewrite {
                SelectorCodemodRewrite::SingleTargetBinding => {
                    let body = apply_text_insertions(&original_text, &text_insertions)
                        .with_context(|| format!("patching {}", file.display()))?;
                    let patched_doc: Value = serde_yaml::from_str(&body)
                        .with_context(|| format!("parsing patched {}", file.display()))?;
                    yaml_edit::write_yaml_body_if_semantic_changed(&file, &patched_doc, body)?
                }
                SelectorCodemodRewrite::AnythingHoles => {
                    yaml_edit::write_yaml_if_semantic_changed(&file, &doc)?
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
    text_insertion: Option<TextInsertion>,
}

impl SelectorCodemodOutcome {
    fn candidate_only(candidate: SelectorCodemodCandidate) -> Self {
        Self {
            candidate,
            text_insertion: None,
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
            rewritten_holes: Vec::new(),
            replacement_count: 0,
            reason: None,
        },
        text_insertion: apply.then_some(insertion),
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
        rewritten_holes,
        replacement_count,
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

#[derive(Debug, Clone)]
struct TextInsertion {
    offset: usize,
    text: String,
}

fn target_binding_text_insertion(
    source: &str,
    member_index: usize,
    target_binding: &str,
) -> Result<TextInsertion> {
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
    Ok(TextInsertion {
        offset: lines[match_line].start,
        text: format!("{indent}target_binding: {target_binding}\n"),
    })
}

fn apply_text_insertions(source: &str, insertions: &[TextInsertion]) -> Result<String> {
    let mut ordered = insertions.to_vec();
    ordered.sort_by_key(|insertion| insertion.offset);
    ordered.dedup_by_key(|insertion| insertion.offset);
    let mut output = String::with_capacity(
        source.len()
            + ordered
                .iter()
                .map(|insertion| insertion.text.len())
                .sum::<usize>(),
    );
    let mut cursor = 0;
    for insertion in ordered {
        if insertion.offset < cursor || !source.is_char_boundary(insertion.offset) {
            bail!("invalid text insertion offset {}", insertion.offset);
        }
        output.push_str(&source[cursor..insertion.offset]);
        output.push_str(&insertion.text);
        cursor = insertion.offset;
    }
    output.push_str(&source[cursor..]);
    Ok(output)
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
