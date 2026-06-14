//! Resolve readable source-pattern selectors against parsed JavaScript ASTs.
//!
//! `spec` owns the YAML-facing selector data. `js_ast` owns parsing and other
//! low-level AST helpers. This module is the bridge that interprets selector
//! semantics such as alpha-equivalent identifier matching.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::OnceLock;
use std::time::{Duration, Instant};

use anyhow::{Context, Result, bail};
use regex::Regex;
use spec::{
    AnonymousStatementSelector, BindingGroup, BindingGroupAdoptNames, BindingSourceKind,
    SourceMatch, SourceMatchIdentifierMode, TargetStatements, TargetStatementsAll,
};
use swc_atoms::{Atom, Wtf8Atom};
use swc_common::{EqIgnoreSpan, SyntaxContext};
use swc_ecma_ast::*;
use swc_ecma_visit::{Visit, VisitMut, VisitMutWith, VisitWith};

/// Syntactic-hole keywords. For single-node holes, the **bare keyword**
/// is the anonymous form: it matches independently at every occurrence
/// and never binds, so authors don't have to mint a unique name per
/// throwaway placeholder. A `<keyword>_<name>` identifier is the
/// **named** form, which binds for cross-occurrence equality — the same
/// name must match the same subtree/statement everywhere it appears.
///
/// `EXPR` matches one arbitrary expression and `STMT` one arbitrary
/// statement. `ARGS`, `STMT_LIST`, `CLASS_REST`, and `DECLARATORS` are
/// variable-length list holes (see
/// [`AstWildcardMatcher::match_list_with_holes`]): `ARGS` absorbs a run
/// of call/new arguments, `STMT_LIST` absorbs a run of block statements
/// (or top-level anonymous selector statements), `CLASS_REST` absorbs a
/// run of class members, and `DECLARATORS` absorbs a run of variable
/// declarators inside one `var`/`let`/`const` declaration. List-hole
/// suffixes are labels for readability; they do not bind the absorbed
/// sequence for cross-occurrence equality.
/// `STMT_LIST` must be checked before `STMT`, since `STMT` is a
/// keyword-prefix of it.
const EXPR_HOLE_KEYWORD: &str = "EXPR";
const STMT_HOLE_KEYWORD: &str = "STMT";
const STMT_LIST_HOLE_KEYWORD: &str = "STMT_LIST";
const CLASS_REST_HOLE_KEYWORD: &str = "CLASS_REST";
const DECLARATORS_HOLE_KEYWORD: &str = "DECLARATORS";
const ARGS_HOLE_KEYWORD: &str = "ARGS";
const STRING_LITERAL_REGEX_PREDICATE: &str = "STR_LITERAL_MATCHING_RE";

const SOURCE_MATCH_TIMINGS_ENV: &str = "DUCKTAPE_SOURCE_MATCH_TIMINGS";
const SOURCE_MATCH_TIMING_THRESHOLD_ENV: &str = "DUCKTAPE_SOURCE_MATCH_TIMING_THRESHOLD_MS";
const SOURCE_MATCH_TIMING_PREVIEW_ENV: &str = "DUCKTAPE_SOURCE_MATCH_TIMING_PREVIEW";

#[derive(Debug)]
struct SourceMatchTimingConfig {
    threshold: Duration,
    include_preview: bool,
}

fn source_match_timing_config() -> Option<&'static SourceMatchTimingConfig> {
    static CONFIG: OnceLock<Option<SourceMatchTimingConfig>> = OnceLock::new();
    CONFIG
        .get_or_init(|| {
            let enabled = std::env::var(SOURCE_MATCH_TIMINGS_ENV).ok()?;
            if matches!(
                enabled.to_ascii_lowercase().as_str(),
                "" | "0" | "false" | "off" | "no"
            ) {
                return None;
            }
            let threshold_ms = std::env::var(SOURCE_MATCH_TIMING_THRESHOLD_ENV)
                .ok()
                .and_then(|raw| raw.parse::<u64>().ok())
                .unwrap_or(0);
            let include_preview = std::env::var(SOURCE_MATCH_TIMING_PREVIEW_ENV)
                .ok()
                .is_none_or(|raw| {
                    !matches!(
                        raw.to_ascii_lowercase().as_str(),
                        "" | "0" | "false" | "off" | "no"
                    )
                });
            Some(SourceMatchTimingConfig {
                threshold: Duration::from_millis(threshold_ms),
                include_preview,
            })
        })
        .as_ref()
}

fn trace_source_match<T>(
    kind: &str,
    request_id: &str,
    selector: &AnonymousStatementSelector,
    run: impl FnOnce() -> Result<T>,
    summarize: impl FnOnce(&T) -> String,
) -> Result<T> {
    let Some(config) = source_match_timing_config() else {
        return run();
    };
    let started = Instant::now();
    let result = run();
    let elapsed = started.elapsed();
    if elapsed >= config.threshold {
        let status = match &result {
            Ok(value) => summarize(value),
            Err(error) => format!("error={}", first_error_line(error)),
        };
        eprintln!(
            "[debundle source_match] elapsed_ms={} request={} kind={} {} {}",
            elapsed.as_millis(),
            request_id,
            kind,
            status,
            source_match_timing_selector_details(selector, config),
        );
    }
    result
}

fn first_error_line(error: &anyhow::Error) -> String {
    let mut line = error.to_string();
    if let Some((first, _)) = line.split_once('\n') {
        line = first.to_string();
    }
    truncate_for_log(&line, 160)
}

fn source_match_preview(source: &str) -> String {
    let collapsed = source.split_whitespace().collect::<Vec<_>>().join(" ");
    truncate_for_log(&collapsed, 180)
}

fn source_match_timing_selector_details(
    selector: &AnonymousStatementSelector,
    config: &SourceMatchTimingConfig,
) -> String {
    let mut fields = vec![
        format!("selector_key={}", selector_timing_key(selector)),
        format!("body_key={}", selector_body_timing_key(selector)),
    ];
    if let Some(target_binding) = selector.target_binding.as_deref() {
        fields.push(format!("target_binding=`{target_binding}`"));
    }
    if let Some(target_statement) = selector.target_statement {
        fields.push(format!("target_statement={target_statement}"));
    }
    if let Some(target_statements) = &selector.target_statements {
        fields.push(format!("target_statements={target_statements:?}"));
    }
    if config.include_preview {
        fields.push(format!(
            "selector={}",
            source_match_preview(&selector.match_source)
        ));
    }
    fields.join(" ")
}

fn selector_body_timing_key(selector: &AnonymousStatementSelector) -> String {
    let mut state = Fnv1a64::new();
    state.update(b"body");
    state.update(format!("{:?}", selector.identifiers).as_bytes());
    state.update(b"\0");
    state.update(normalized_selector_source(&selector.match_source).as_bytes());
    format!("{:016x}", state.finish())
}

fn selector_timing_key(selector: &AnonymousStatementSelector) -> String {
    let mut state = Fnv1a64::new();
    state.update(b"selector");
    state.update(format!("{:?}", selector.identifiers).as_bytes());
    state.update(b"\0");
    state.update(normalized_selector_source(&selector.match_source).as_bytes());
    state.update(b"\0");
    if let Some(target_binding) = selector.target_binding.as_deref() {
        state.update(b"target_binding=");
        state.update(target_binding.as_bytes());
    }
    state.update(b"\0");
    if let Some(target_statement) = selector.target_statement {
        state.update(format!("target_statement={target_statement}").as_bytes());
    }
    state.update(b"\0");
    if let Some(target_statements) = &selector.target_statements {
        state.update(format!("target_statements={target_statements:?}").as_bytes());
    }
    format!("{:016x}", state.finish())
}

fn normalized_selector_source(source: &str) -> String {
    source.split_whitespace().collect::<Vec<_>>().join(" ")
}

struct Fnv1a64 {
    value: u64,
}

impl Fnv1a64 {
    const OFFSET: u64 = 0xcbf29ce484222325;
    const PRIME: u64 = 0x100000001b3;

    fn new() -> Self {
        Self {
            value: Self::OFFSET,
        }
    }

    fn update(&mut self, bytes: &[u8]) {
        for byte in bytes {
            self.value ^= u64::from(*byte);
            self.value = self.value.wrapping_mul(Self::PRIME);
        }
    }

    fn finish(&self) -> u64 {
        self.value
    }
}

fn truncate_for_log(value: &str, max_chars: usize) -> String {
    if value.chars().count() <= max_chars {
        return value.to_string();
    }
    let mut truncated = value.chars().take(max_chars).collect::<String>();
    truncated.push_str("...");
    truncated
}

fn render_timing_names<'a>(names: impl Iterator<Item = &'a str>) -> String {
    const MAX_NAMES: usize = 6;
    let names = names.collect::<Vec<_>>();
    if names.is_empty() {
        return "<none>".to_string();
    }
    let mut rendered = names
        .iter()
        .take(MAX_NAMES)
        .map(|name| format!("`{name}`"))
        .collect::<Vec<_>>();
    if names.len() > MAX_NAMES {
        rendered.push(format!("+{} more", names.len() - MAX_NAMES));
    }
    rendered.join(",")
}

fn render_timing_groups(groups: &[Vec<usize>]) -> String {
    const MAX_GROUPS: usize = 4;
    if groups.is_empty() {
        return "[]".to_string();
    }
    let mut rendered = groups
        .iter()
        .take(MAX_GROUPS)
        .map(|group| format!("{group:?}"))
        .collect::<Vec<_>>();
    if groups.len() > MAX_GROUPS {
        rendered.push(format!("+{} more", groups.len() - MAX_GROUPS));
    }
    rendered.join(",")
}

fn render_timing_body_indices<'a>(indices: impl Iterator<Item = &'a usize>) -> String {
    const MAX_INDICES: usize = 8;
    let indices = indices.copied().collect::<Vec<_>>();
    if indices.is_empty() {
        return "[]".to_string();
    }
    let mut rendered = indices
        .iter()
        .take(MAX_INDICES)
        .map(usize::to_string)
        .collect::<Vec<_>>();
    if indices.len() > MAX_INDICES {
        rendered.push(format!("+{} more", indices.len() - MAX_INDICES));
    }
    format!("[{}]", rendered.join(","))
}

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct ResolvedMemberBinding {
    pub binding_name: String,
    pub kind: Option<BindingSourceKind>,
}

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct SourceMatchNearMiss {
    pub body_idx: usize,
    pub declared_bindings: Vec<String>,
    pub score: usize,
    pub reason: String,
}

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct SourceMatchBodyDebt {
    pub exact_groups: Vec<Vec<Option<usize>>>,
    pub near_misses: Vec<SourceMatchNearMiss>,
}

#[derive(Debug, Clone, Eq, PartialEq)]
struct MemberBindingMatch {
    body_idx: usize,
    binding: ResolvedMemberBinding,
}

pub fn source_match_declared_binding_names(
    request_id: &str,
    source_match: &SourceMatch,
) -> Result<Vec<String>> {
    let parsed = js_ast::parse_js_module_ast(
        &format!("<binding group source_match in {request_id}>"),
        &source_match.match_source,
    )
    .with_context(|| {
        format!(
            "logical_module {request_id}: binding_groups[].source_match did not parse as JS:\n{}",
            source_match.match_source
        )
    })?;
    Ok(parsed
        .body
        .iter()
        .flat_map(declared_bindings)
        .map(|binding| binding.binding_name)
        .collect())
}

/// Expand one `binding_groups[]` entry into `(export_name,
/// member-form selector)` pairs — each selector is the group's
/// `source_match` with `target_binding` set to one selector-local
/// binding. This is the single expansion both the run pipeline's
/// member assembly (`lowering::build_members`) and the CLI edit gate
/// consume, so the two always agree on which owners a binding group
/// claims.
pub struct BindingGroupMemberSelector {
    pub export_name: String,
    pub selector: AnonymousStatementSelector,
    pub comment: Option<String>,
}

pub fn binding_group_member_selectors(
    request_id: &str,
    group: &BindingGroup,
) -> Result<Vec<BindingGroupMemberSelector>> {
    if group.source_match.target_binding.is_some() {
        bail!(
            "logical_module {request_id}: binding_groups[].source_match must not include \
             `target_binding`; use the `exports` keys to choose selector-local bindings"
        );
    }
    let exports = effective_binding_group_exports(group, request_id)?;
    let unknown_comments = group
        .comments
        .keys()
        .filter(|name| !exports.contains_key(*name))
        .cloned()
        .collect::<Vec<_>>();
    if !unknown_comments.is_empty() {
        bail!(
            "logical_module {request_id}: binding_groups[].comments names bindings that \
             are not exported by the group: {}",
            unknown_comments.join(", ")
        );
    }
    Ok(exports
        .into_iter()
        .map(|(target_binding, export_name)| {
            let mut selector = group.source_match.selector();
            selector.target_binding = Some(target_binding.clone());
            selector.target_statement = None;
            selector.target_statements = None;
            let comment = group.comments.get(&target_binding).cloned();
            BindingGroupMemberSelector {
                export_name,
                selector,
                comment,
            }
        })
        .collect())
}

pub fn binding_group_anonymous_statement_selector(
    group: &BindingGroup,
) -> Option<AnonymousStatementSelector> {
    if group.source_match.target_statement.is_none()
        && group.source_match.target_statements.is_none()
    {
        return None;
    }
    let mut selector = group.source_match.selector();
    selector.target_binding = None;
    Some(selector)
}

fn effective_binding_group_exports(
    group: &BindingGroup,
    request_id: &str,
) -> Result<BTreeMap<String, String>> {
    let mut exports = match &group.adopt_names {
        BindingGroupAdoptNames::None | BindingGroupAdoptNames::All(false) => BTreeMap::new(),
        BindingGroupAdoptNames::All(true) => {
            let names = declared_selector_binding_names(group, request_id)?;
            names
                .into_iter()
                .map(|name| (name.clone(), name))
                .collect::<BTreeMap<_, _>>()
        }
        BindingGroupAdoptNames::Names(names) => {
            let declared = declared_selector_binding_names(group, request_id)?;
            let declared_set = declared.into_iter().collect::<BTreeSet<_>>();
            let mut adopted = BTreeMap::new();
            for name in names {
                if !declared_set.contains(name) {
                    bail!(
                        "logical_module {request_id}: binding_groups[].adopt_names entry \
                         `{name}` is not declared by source_match.match"
                    );
                }
                if adopted.insert(name.clone(), name.clone()).is_some() {
                    bail!(
                        "logical_module {request_id}: binding_groups[].adopt_names repeats \
                         `{name}`"
                    );
                }
            }
            adopted
        }
    };
    exports.extend(group.exports.clone());
    if exports.is_empty() {
        bail!(
            "logical_module {request_id}: binding_groups[] must include non-empty `exports` \
             or `adopt_names`"
        );
    }
    Ok(exports)
}

fn declared_selector_binding_names(group: &BindingGroup, request_id: &str) -> Result<Vec<String>> {
    let names = source_match_declared_binding_names(request_id, &group.source_match)?;
    let mut seen = BTreeSet::new();
    let mut duplicates = BTreeSet::new();
    for name in &names {
        if !seen.insert(name.clone()) {
            duplicates.insert(name.clone());
        }
    }
    if !duplicates.is_empty() {
        bail!(
            "logical_module {request_id}: binding_groups[].source_match declares duplicate \
             selector-local binding names: {}",
            duplicates.into_iter().collect::<Vec<_>>().join(", ")
        );
    }
    if names.is_empty() {
        bail!(
            "logical_module {request_id}: binding_groups[].adopt_names found no declared \
             bindings in source_match.match"
        );
    }
    Ok(names)
}

/// All member-binding candidates a member-form selector matches in
/// `runtime_module`, without the exactly-one arbitration
/// [`resolve_member_binding`] applies. Used by callers that
/// aggregate matches across several source files (the CLI edit
/// gate) before deciding uniqueness.
pub fn member_binding_candidates(
    runtime_module: &Module,
    request_id: &str,
    selector: &AnonymousStatementSelector,
) -> Result<Vec<ResolvedMemberBinding>> {
    let matches = trace_source_match(
        "members[].selector.source_match candidates",
        request_id,
        selector,
        || find_member_binding_matches(runtime_module, request_id, selector),
        |matches: &Vec<MemberBindingMatch>| {
            format!(
                "matches={} body_indices={} bindings={}",
                matches.len(),
                render_timing_body_indices(matches.iter().map(|matched| &matched.body_idx)),
                render_timing_names(
                    matches
                        .iter()
                        .map(|matched| matched.binding.binding_name.as_str())
                )
            )
        },
    )?;
    Ok(matches.into_iter().map(|matched| matched.binding).collect())
}

pub fn resolve_anonymous_statement_body_index(
    runtime_module: &Module,
    request_id: &str,
    selector: &AnonymousStatementSelector,
) -> Result<usize> {
    let matches = resolve_anonymous_statement_body_indices(runtime_module, request_id, selector)?;
    match matches.as_slice() {
        [single] => Ok(*single),
        multiple => bail!(
            "logical_module {request_id}: anonymous_statements[].source_match resolved to {} \
             top-level statements at body indices {:?}; expected exactly one. Use the plural \
             resolver for selectors with `target_statements`.",
            multiple.len(),
            multiple,
        ),
    }
}

pub fn resolve_anonymous_statement_body_indices(
    runtime_module: &Module,
    request_id: &str,
    selector: &AnonymousStatementSelector,
) -> Result<Vec<usize>> {
    let matches = find_anonymous_statement_body_index_groups(runtime_module, request_id, selector)?;
    match matches.as_slice() {
        [single] => Ok(single.clone()),
        [] => bail!(
            "logical_module {request_id}: anonymous_statements[].match did not match any \
             top-level statement group in the chunk. Selector:\n{match_source}",
            match_source = selector.match_source,
        ),
        multiple => bail!(
            "logical_module {request_id}: anonymous_statements[].match is ambiguous — \
             matched {} top-level statement groups at body indices {:?}. Refine the selector. \
             Source:\n{match_source}",
            multiple.len(),
            multiple,
            match_source = selector.match_source,
        ),
    }
}

pub fn find_anonymous_statement_body_indices(
    runtime_module: &Module,
    request_id: &str,
    selector: &AnonymousStatementSelector,
) -> Result<Vec<usize>> {
    Ok(
        find_anonymous_statement_body_index_groups(runtime_module, request_id, selector)?
            .into_iter()
            .flatten()
            .collect(),
    )
}

pub fn find_anonymous_statement_body_index_groups(
    runtime_module: &Module,
    request_id: &str,
    selector: &AnonymousStatementSelector,
) -> Result<Vec<Vec<usize>>> {
    trace_source_match(
        "anonymous_statements[].source_match",
        request_id,
        selector,
        || {
            let parsed = js_ast::parse_js_module_ast(
                &format!("<anonymous_statement match in {request_id}>"),
                &selector.match_source,
            )
            .with_context(|| {
                format!(
                    "logical_module {request_id}: anonymous_statements[].match did not parse as JS:\n{}",
                    selector.match_source
                )
            })?;
            let target_indices =
                anonymous_selector_target_statement_indices(request_id, selector, &parsed.body)?;
            let mut groups = Vec::new();
            for alignment in
                find_matching_body_group_alignments(runtime_module, &parsed.body, selector)
            {
                let mut group = Vec::with_capacity(target_indices.len());
                for target_idx in &target_indices {
                    let Some(Some(body_idx)) = alignment.get(*target_idx) else {
                        bail!(
                            "logical_module {request_id}: anonymous_statements[].source_match \
                             target statement {target_idx} was matched by a STMT_LIST hole instead \
                             of a pinned selector statement. Refine the selector:\n{match_source}",
                            match_source = selector.match_source,
                        );
                    };
                    group.push(*body_idx);
                }
                groups.push(group);
            }
            Ok(groups)
        },
        |groups: &Vec<Vec<usize>>| {
            format!(
                "matches={} body_indices={}",
                groups.len(),
                render_timing_groups(groups)
            )
        },
    )
}

/// Source-aware fragility signal for selector-debt reporting.
///
/// This intentionally does not change selector semantics: it reuses the
/// normal exact matcher, then lists high-scoring non-matching top-level
/// items that look structurally close enough to become ambiguous after a
/// small source drift. The first slice only scores selectors whose source
/// parses to one pinned top-level item; multi-statement windows still use
/// the exact match count but do not get near-miss rows yet.
pub fn source_match_body_debt(
    runtime_module: &Module,
    request_id: &str,
    selector: &AnonymousStatementSelector,
    min_score: usize,
    limit: usize,
) -> Result<SourceMatchBodyDebt> {
    let parsed = js_ast::parse_js_module_ast(
        &format!("<source_match debt in {request_id}>"),
        &selector.match_source,
    )
    .with_context(|| {
        format!(
            "logical_module {request_id}: source_match did not parse as JS:\n{}",
            selector.match_source
        )
    })?;
    let exact_groups = find_matching_body_group_alignments(runtime_module, &parsed.body, selector);
    let [needle] = parsed.body.as_slice() else {
        return Ok(SourceMatchBodyDebt {
            exact_groups,
            near_misses: Vec::new(),
        });
    };
    if module_item_list_hole_name(needle).is_some() {
        return Ok(SourceMatchBodyDebt {
            exact_groups,
            near_misses: Vec::new(),
        });
    }
    let exact_body_indices = exact_groups
        .iter()
        .flat_map(|group| group.iter().flatten().copied())
        .collect::<BTreeSet<_>>();
    let wildcard_idents = wildcard_ident_names(needle);
    let alpha = selector.identifiers == SourceMatchIdentifierMode::AlphaAll;
    let mut near_misses = SyntaxContext::within_ignored_ctxt(|| {
        runtime_module
            .body
            .iter()
            .enumerate()
            .filter_map(|(body_idx, candidate)| {
                if exact_body_indices.contains(&body_idx) {
                    return None;
                }
                let reason =
                    first_mismatch_reason(needle, candidate, selector, &wildcard_idents, alpha)?;
                if reason.score < min_score {
                    return None;
                }
                let declared_bindings = declared_bindings(candidate)
                    .into_iter()
                    .map(|binding| binding.binding_name)
                    .collect::<Vec<_>>();
                Some(SourceMatchNearMiss {
                    body_idx,
                    declared_bindings,
                    score: reason.score,
                    reason: reason.reason,
                })
            })
            .collect::<Vec<_>>()
    });
    near_misses.sort_by(|left, right| {
        right
            .score
            .cmp(&left.score)
            .then_with(|| left.body_idx.cmp(&right.body_idx))
    });
    if limit > 0 {
        near_misses.truncate(limit);
    }
    Ok(SourceMatchBodyDebt {
        exact_groups,
        near_misses,
    })
}

pub fn resolve_member_binding(
    runtime_module: &Module,
    request_id: &str,
    export_name: &str,
    selector: &AnonymousStatementSelector,
) -> Result<ResolvedMemberBinding> {
    let kind = format!("members[].selector.source_match export=`{export_name}`");
    let matched = trace_source_match(
        &kind,
        request_id,
        selector,
        || {
            let matches = find_member_binding_matches(runtime_module, request_id, selector)?;
            let target_binding_hint = selector
                .target_binding
                .as_deref()
                .map(|target| format!(" target_binding `{target}`"))
                .unwrap_or_default();
            match matches.as_slice() {
                [single] => Ok(single.clone()),
                [] => {
                    let hint = source_match_no_match_hint(runtime_module, selector);
                    bail!(
                        "logical_module {request_id}: members[].selector.source_match for export \
                         `{export_name}`{target_binding_hint} did not match any top-level declaration in the chunk. \
                         Selector:\n{match_source}{hint}",
                        match_source = selector.match_source,
                        hint = hint.unwrap_or_default(),
                    )
                }
                multiple => bail!(
                    "logical_module {request_id}: members[].selector.source_match for export \
                     `{export_name}`{target_binding_hint} is ambiguous — matched {} top-level statements at body \
                     indices {:?} (bindings: {}). Refine the selector. Source:\n{match_source}",
                    multiple.len(),
                    multiple
                        .iter()
                        .map(|matched| matched.body_idx)
                        .collect::<Vec<_>>(),
                    multiple
                        .iter()
                        .map(|matched| matched.binding.binding_name.as_str())
                        .collect::<Vec<_>>()
                        .join(", "),
                    match_source = selector.match_source,
                ),
            }
        },
        |matched| {
            format!(
                "body_indices=[{}] binding={}",
                matched.body_idx, matched.binding.binding_name
            )
        },
    )?;
    Ok(matched.binding)
}

pub fn resolve_member_binding_group(
    runtime_module: &Module,
    request_id: &str,
    selector: &AnonymousStatementSelector,
    exports_by_target: &BTreeMap<String, String>,
) -> Result<BTreeMap<String, ResolvedMemberBinding>> {
    trace_source_match(
        "binding_groups[].source_match",
        request_id,
        selector,
        || {
            resolve_member_binding_group_impl(
                runtime_module,
                request_id,
                selector,
                exports_by_target,
            )
        },
        |resolved| {
            format!(
                "targets={} bindings={}",
                resolved.len(),
                render_timing_names(
                    resolved
                        .values()
                        .map(|matched| matched.binding_name.as_str())
                )
            )
        },
    )
}

fn resolve_member_binding_group_impl(
    runtime_module: &Module,
    request_id: &str,
    selector: &AnonymousStatementSelector,
    exports_by_target: &BTreeMap<String, String>,
) -> Result<BTreeMap<String, ResolvedMemberBinding>> {
    if selector.target_binding.is_some() {
        bail!(
            "logical_module {request_id}: binding group resolver received a selector with \
             target_binding already set"
        );
    }
    let parsed = js_ast::parse_js_module_ast(
        &format!("<binding group source_match in {request_id}>"),
        &selector.match_source,
    )
    .with_context(|| {
        format!(
            "logical_module {request_id}: binding_groups[].source_match did not parse as JS:\n{}",
            selector.match_source
        )
    })?;
    if parsed.body.is_empty() {
        bail!(
            "logical_module {request_id}: binding_groups[].source_match parsed to zero \
             statements; selector source must contain at least one top-level statement:\n{match_source}",
            match_source = selector.match_source,
        );
    }
    if parsed.body.len() == 1 && selector_single_var_declarator(&parsed.body[0]).is_some() {
        let mut resolved = BTreeMap::new();
        for (target_binding, export_name) in exports_by_target {
            let mut selector = selector.clone();
            selector.target_binding = Some(target_binding.clone());
            resolved.insert(
                target_binding.clone(),
                resolve_member_binding(runtime_module, request_id, export_name, &selector)?,
            );
        }
        return Ok(resolved);
    }
    if parsed.body.len() == 1 && selector_var_decl_has_declarator_holes(&parsed.body[0]) {
        return resolve_member_binding_group_with_declarator_holes(
            runtime_module,
            request_id,
            &parsed.body[0],
            selector,
            exports_by_target,
        );
    }

    let mut target_locations = BTreeMap::new();
    for target_binding in exports_by_target.keys() {
        target_locations.insert(
            target_binding.clone(),
            selector_binding_location(&parsed.body, request_id, selector, target_binding)?,
        );
    }

    let target_hint = exports_by_target
        .iter()
        .map(|(target_binding, export_name)| {
            format!("target_binding `{target_binding}` for export `{export_name}`")
        })
        .collect::<Vec<_>>()
        .join(", ");
    let alignments = find_matching_body_group_alignments(runtime_module, &parsed.body, selector);
    let alignment = match alignments.as_slice() {
        [single] => single,
        [] => {
            let hint = source_match_no_match_hint(runtime_module, selector);
            bail!(
                "logical_module {request_id}: binding_groups[].source_match for targets \
                 `{target_hint}` did not match any top-level declaration range in the chunk. \
                 Selector:\n{match_source}{hint}",
                match_source = selector.match_source,
                hint = hint.unwrap_or_default(),
            )
        }
        multiple => bail!(
            "logical_module {request_id}: binding_groups[].source_match for targets \
             `{target_hint}` is ambiguous — matched {} top-level declaration ranges at body \
             indices {:?}. Refine the selector. Source:\n{match_source}",
            multiple.len(),
            multiple
                .iter()
                .map(|alignment| alignment.iter().flatten().copied().collect::<Vec<_>>())
                .collect::<Vec<_>>(),
            match_source = selector.match_source,
        ),
    };

    let mut resolved = BTreeMap::new();
    for (target_binding, (target_item_idx, target_binding_idx)) in target_locations {
        let Some(Some(matched_body_idx)) = alignment.get(target_item_idx) else {
            bail!(
                "logical_module {request_id}: binding_groups[].source_match target_binding \
                 `{target_binding}` was matched by a STMT_LIST hole instead of a pinned \
                 selector statement. Refine the selector:\n{match_source}",
                match_source = selector.match_source,
            );
        };
        let matched_body_idx = *matched_body_idx;
        let item = runtime_module.body.get(matched_body_idx).with_context(|| {
            format!("body index {matched_body_idx} disappeared while resolving source_match")
        })?;
        let declared = declared_bindings(item);
        let Some(binding) = declared.get(target_binding_idx) else {
            bail!(
                "logical_module {request_id}: binding_groups[].source_match target_binding \
                 `{target_binding}` matched top-level statement at body index {matched_body_idx}, but \
                 the matched statement declares only {} bindings. Source:\n{match_source}",
                declared.len(),
                match_source = selector.match_source,
            );
        };
        resolved.insert(target_binding, binding.clone());
    }
    Ok(resolved)
}

fn selector_binding_location(
    needles: &[ModuleItem],
    request_id: &str,
    selector: &AnonymousStatementSelector,
    target_binding: &str,
) -> Result<(usize, usize)> {
    let selector_binding_locations: Vec<(usize, usize)> = needles
        .iter()
        .enumerate()
        .flat_map(|(item_idx, item)| {
            declared_bindings(item).into_iter().enumerate().filter_map(
                move |(binding_idx, binding)| {
                    (binding.binding_name == target_binding).then_some((item_idx, binding_idx))
                },
            )
        })
        .collect();
    match selector_binding_locations.as_slice() {
        [single] => Ok(*single),
        [] => bail!(
            "logical_module {request_id}: members[].selector.source_match target_binding \
             `{target_binding}` is not declared by the selector source:\n{match_source}",
            match_source = selector.match_source,
        ),
        multiple => bail!(
            "logical_module {request_id}: members[].selector.source_match target_binding \
             `{target_binding}` is ambiguous within the selector source at statement/binding \
             indices {:?}. Refine the selector source:\n{match_source}",
            multiple,
            match_source = selector.match_source,
        ),
    }
}

fn find_member_binding_matches(
    runtime_module: &Module,
    request_id: &str,
    selector: &AnonymousStatementSelector,
) -> Result<Vec<MemberBindingMatch>> {
    let parsed = js_ast::parse_js_module_ast(
        &format!("<member source_match in {request_id}>"),
        &selector.match_source,
    )
    .with_context(|| {
        format!(
            "logical_module {request_id}: members[].selector.source_match did not parse as JS:\n{}",
            selector.match_source
        )
    })?;
    if let Some(target_binding) = &selector.target_binding {
        if parsed.body.is_empty() {
            bail!(
                "logical_module {request_id}: members[].selector.source_match parsed to zero \
                 statements; selector source must contain at least one top-level statement \
                 when target_binding is used:\n{match_source}",
                match_source = selector.match_source,
            );
        }
        if parsed.body.len() == 1 && selector_single_var_declarator(&parsed.body[0]).is_some() {
            return find_matching_target_var_declarators(
                runtime_module,
                request_id,
                &parsed.body[0],
                selector,
                target_binding,
            );
        }
        return find_matching_target_bindings(
            runtime_module,
            request_id,
            &parsed.body,
            selector,
            target_binding,
        );
    }
    let parsed_items: Vec<&ModuleItem> = parsed.body.iter().collect();
    let needle = match parsed_items.as_slice() {
        [single] => *single,
        [] => bail!(
            "logical_module {request_id}: members[].selector.source_match parsed to zero \
             statements; selector source must contain exactly one top-level declaration:\n{match_source}",
            match_source = selector.match_source,
        ),
        _ => bail!(
            "logical_module {request_id}: members[].selector.source_match parsed to {} \
             statements; selector source must contain exactly one top-level declaration unless \
             target_binding is used:\n{match_source}",
            parsed_items.len(),
            match_source = selector.match_source,
        ),
    };
    if selector_single_var_declarator(needle).is_some() {
        return find_matching_var_declarators(runtime_module, needle, selector);
    }
    let mut matches = Vec::new();
    for body_idx in find_matching_body_indices(runtime_module, needle, selector) {
        let item = runtime_module.body.get(body_idx).with_context(|| {
            format!("body index {body_idx} disappeared while resolving source_match")
        })?;
        let declared = declared_bindings(item);
        match declared.as_slice() {
            [single] => matches.push(MemberBindingMatch {
                body_idx,
                binding: single.clone(),
            }),
            [] => {}
            _ => bail!(
                "logical_module {request_id}: members[].selector.source_match matched \
                 top-level statement at body index {body_idx}, but that statement declares \
                 {} bindings ({}). Use a single-declarator selector or refine the match. \
                 Source:\n{match_source}",
                declared.len(),
                declared
                    .iter()
                    .map(|binding| binding.binding_name.as_str())
                    .collect::<Vec<_>>()
                    .join(", "),
                match_source = selector.match_source,
            ),
        }
    }
    Ok(matches)
}

fn find_matching_target_bindings(
    runtime_module: &Module,
    request_id: &str,
    needles: &[ModuleItem],
    selector: &AnonymousStatementSelector,
    target_binding: &str,
) -> Result<Vec<MemberBindingMatch>> {
    if needles.len() == 1 && selector_var_decl_has_declarator_holes(&needles[0]) {
        return find_matching_target_var_decl_with_declarator_holes(
            runtime_module,
            request_id,
            &needles[0],
            selector,
            target_binding,
        );
    }
    let (target_item_idx, target_binding_idx) =
        selector_binding_location(needles, request_id, selector, target_binding)?;
    let mut matches = Vec::new();
    for body_idx in find_matching_body_ranges(runtime_module, needles, selector) {
        let matched_body_idx = body_idx + target_item_idx;
        let item = runtime_module.body.get(matched_body_idx).with_context(|| {
            format!("body index {matched_body_idx} disappeared while resolving source_match")
        })?;
        let declared = declared_bindings(item);
        let Some(binding) = declared.get(target_binding_idx) else {
            bail!(
                "logical_module {request_id}: members[].selector.source_match target_binding \
                 `{target_binding}` matched top-level statement at body index {matched_body_idx}, but \
                 the matched statement declares only {} bindings. Source:\n{match_source}",
                declared.len(),
                match_source = selector.match_source,
            );
        };
        matches.push(MemberBindingMatch {
            body_idx: matched_body_idx,
            binding: binding.clone(),
        });
    }
    Ok(matches)
}

fn find_matching_target_var_declarators(
    runtime_module: &Module,
    request_id: &str,
    needle: &ModuleItem,
    selector: &AnonymousStatementSelector,
    target_binding: &str,
) -> Result<Vec<MemberBindingMatch>> {
    let selector_bindings = declared_bindings(needle);
    let selector_binding_indices: Vec<usize> = selector_bindings
        .iter()
        .enumerate()
        .filter_map(|(idx, binding)| (binding.binding_name == target_binding).then_some(idx))
        .collect();
    let target_binding_idx = match selector_binding_indices.as_slice() {
        [single] => *single,
        [] => bail!(
            "logical_module {request_id}: members[].selector.source_match target_binding \
             `{target_binding}` is not declared by the selector source:\n{match_source}",
            match_source = selector.match_source,
        ),
        multiple => bail!(
            "logical_module {request_id}: members[].selector.source_match target_binding \
             `{target_binding}` is ambiguous within the selector source at declared-binding \
             indices {:?}. Refine the selector source:\n{match_source}",
            multiple,
            match_source = selector.match_source,
        ),
    };
    let prepared = PreparedNeedle::new(needle, selector);
    let prefilter = VarDeclaratorPrefilter::new(needle, &prepared);
    let mut matches = Vec::new();
    for (body_idx, item) in runtime_module.body.iter().enumerate() {
        let Some(candidate_var) = item_var_decl(item) else {
            continue;
        };
        if !prefilter.var_decl_can_match(candidate_var) {
            continue;
        }
        for declarator in &candidate_var.decls {
            if !prefilter.declarator_can_match(declarator) {
                continue;
            }
            let candidate_item = module_item_for_single_var_declarator(item, declarator);
            if !prepared.matches(&candidate_item) {
                continue;
            }
            let declared = declared_bindings_for_var_declarator(declarator);
            let Some(binding) = declared.get(target_binding_idx) else {
                bail!(
                    "logical_module {request_id}: members[].selector.source_match target_binding \
                     `{target_binding}` matched a variable declarator at body index {body_idx}, \
                     but that declarator declares only {} bindings. Source:\n{match_source}",
                    declared.len(),
                    match_source = selector.match_source,
                );
            };
            matches.push(MemberBindingMatch {
                body_idx,
                binding: binding.clone(),
            });
        }
    }
    Ok(matches)
}

fn find_matching_target_var_decl_with_declarator_holes(
    runtime_module: &Module,
    request_id: &str,
    needle: &ModuleItem,
    selector: &AnonymousStatementSelector,
    target_binding: &str,
) -> Result<Vec<MemberBindingMatch>> {
    let needle_var =
        item_var_decl(needle).expect("caller checked selector_var_decl_has_declarator_holes");
    let (target_decl_idx, target_binding_idx) =
        selector_var_declarator_binding_location(needle_var, request_id, selector, target_binding)?;
    let prepared = PreparedNeedle::new(needle, selector);
    let prefilter = VarDeclWithDeclaratorHolesPrefilter::new(needle_var, &prepared);
    let mut matches = Vec::new();
    for (body_idx, item) in runtime_module.body.iter().enumerate() {
        let Some(candidate_var) = item_var_decl(item) else {
            continue;
        };
        if !prefilter.var_decl_can_match(candidate_var) {
            continue;
        }
        if !prepared.matches(item) {
            continue;
        }
        let Some(alignment) = prepared.var_declarator_alignment(needle_var, candidate_var) else {
            continue;
        };
        let Some(Some(candidate_decl_idx)) = alignment.get(target_decl_idx) else {
            bail!(
                "logical_module {request_id}: members[].selector.source_match target_binding \
                 `{target_binding}` was matched by a DECLARATORS hole instead of a pinned \
                 selector declarator. Refine the selector:\n{match_source}",
                match_source = selector.match_source,
            );
        };
        let Some(candidate_declarator) = candidate_var.decls.get(*candidate_decl_idx) else {
            bail!(
                "logical_module {request_id}: members[].selector.source_match target_binding \
                 `{target_binding}` aligned to missing candidate declarator index \
                 {candidate_decl_idx}. Source:\n{match_source}",
                match_source = selector.match_source,
            );
        };
        let declared = declared_bindings_for_var_declarator(candidate_declarator);
        let Some(binding) = declared.get(target_binding_idx) else {
            bail!(
                "logical_module {request_id}: members[].selector.source_match target_binding \
                 `{target_binding}` matched a variable declarator at body index {body_idx}, \
                 but that declarator declares only {} bindings. Source:\n{match_source}",
                declared.len(),
                match_source = selector.match_source,
            );
        };
        matches.push(MemberBindingMatch {
            body_idx,
            binding: binding.clone(),
        });
    }
    Ok(matches)
}

fn resolve_member_binding_group_with_declarator_holes(
    runtime_module: &Module,
    request_id: &str,
    needle: &ModuleItem,
    selector: &AnonymousStatementSelector,
    exports_by_target: &BTreeMap<String, String>,
) -> Result<BTreeMap<String, ResolvedMemberBinding>> {
    let needle_var =
        item_var_decl(needle).expect("caller checked selector_var_decl_has_declarator_holes");
    let target_locations = exports_by_target
        .keys()
        .map(|target_binding| {
            Ok((
                target_binding.clone(),
                selector_var_declarator_binding_location(
                    needle_var,
                    request_id,
                    selector,
                    target_binding,
                )?,
            ))
        })
        .collect::<Result<BTreeMap<_, _>>>()?;
    let prepared = PreparedNeedle::new(needle, selector);
    let prefilter = VarDeclWithDeclaratorHolesPrefilter::new(needle_var, &prepared);
    let mut matches = Vec::new();
    for (body_idx, item) in runtime_module.body.iter().enumerate() {
        let Some(candidate_var) = item_var_decl(item) else {
            continue;
        };
        if !prefilter.var_decl_can_match(candidate_var) {
            continue;
        }
        if !prepared.matches(item) {
            continue;
        }
        let Some(alignment) = prepared.var_declarator_alignment(needle_var, candidate_var) else {
            continue;
        };
        let mut resolved = BTreeMap::new();
        for (target_binding, (target_decl_idx, target_binding_idx)) in &target_locations {
            let Some(Some(candidate_decl_idx)) = alignment.get(*target_decl_idx) else {
                bail!(
                    "logical_module {request_id}: binding_groups[].source_match \
                     target_binding `{target_binding}` was matched by a DECLARATORS hole \
                     instead of a pinned selector declarator. Refine the selector:\n{match_source}",
                    match_source = selector.match_source,
                );
            };
            let Some(candidate_declarator) = candidate_var.decls.get(*candidate_decl_idx) else {
                bail!(
                    "logical_module {request_id}: binding_groups[].source_match \
                     target_binding `{target_binding}` aligned to missing candidate \
                     declarator index {candidate_decl_idx}. Source:\n{match_source}",
                    match_source = selector.match_source,
                );
            };
            let declared = declared_bindings_for_var_declarator(candidate_declarator);
            let Some(binding) = declared.get(*target_binding_idx) else {
                bail!(
                    "logical_module {request_id}: binding_groups[].source_match \
                     target_binding `{target_binding}` matched a variable declarator at \
                     body index {body_idx}, but that declarator declares only {} bindings. \
                     Source:\n{match_source}",
                    declared.len(),
                    match_source = selector.match_source,
                );
            };
            resolved.insert(target_binding.clone(), binding.clone());
        }
        matches.push((body_idx, resolved));
    }
    match matches.as_slice() {
        [(_, resolved)] => Ok(resolved.clone()),
        [] => {
            let target_hint = exports_by_target
                .iter()
                .map(|(target_binding, export_name)| {
                    format!("target_binding `{target_binding}` for export `{export_name}`")
                })
                .collect::<Vec<_>>()
                .join(", ");
            let hint = source_match_no_match_hint(runtime_module, selector);
            bail!(
                "logical_module {request_id}: binding_groups[].source_match for targets \
                 `{target_hint}` did not match any top-level declaration range in the chunk. \
                 Selector:\n{match_source}{hint}",
                match_source = selector.match_source,
                hint = hint.unwrap_or_default(),
            )
        }
        multiple => bail!(
            "logical_module {request_id}: binding_groups[].source_match is ambiguous — \
             matched {} top-level declarations at body indices {:?}. Refine the selector. \
             Source:\n{match_source}",
            multiple.len(),
            multiple
                .iter()
                .map(|(body_idx, _)| *body_idx)
                .collect::<Vec<_>>(),
            match_source = selector.match_source,
        ),
    }
}

fn selector_var_declarator_binding_location(
    needle_var: &VarDecl,
    request_id: &str,
    selector: &AnonymousStatementSelector,
    target_binding: &str,
) -> Result<(usize, usize)> {
    let selector_binding_locations = needle_var
        .decls
        .iter()
        .enumerate()
        .flat_map(|(declarator_idx, declarator)| {
            declared_bindings_for_var_declarator(declarator)
                .into_iter()
                .enumerate()
                .filter_map(move |(binding_idx, binding)| {
                    (binding.binding_name == target_binding)
                        .then_some((declarator_idx, binding_idx))
                })
        })
        .collect::<Vec<_>>();
    match selector_binding_locations.as_slice() {
        [single] => Ok(*single),
        [] => bail!(
            "logical_module {request_id}: members[].selector.source_match target_binding \
             `{target_binding}` is not declared by the selector source:\n{match_source}",
            match_source = selector.match_source,
        ),
        multiple => bail!(
            "logical_module {request_id}: members[].selector.source_match target_binding \
             `{target_binding}` is ambiguous within the selector source at declarator/binding \
             indices {:?}. Refine the selector source:\n{match_source}",
            multiple,
            match_source = selector.match_source,
        ),
    }
}

fn selector_single_var_declarator(needle: &ModuleItem) -> Option<&VarDecl> {
    match needle {
        ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) if var.decls.len() == 1 => Some(var),
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) if matches!(&export.decl, Decl::Var(var) if var.decls.len() == 1) => {
            match &export.decl {
                Decl::Var(var) => Some(var),
                _ => None,
            }
        }
        _ => None,
    }
}

fn selector_var_decl_has_declarator_holes(needle: &ModuleItem) -> bool {
    item_var_decl(needle).is_some_and(|var| {
        var.decls
            .iter()
            .any(|declarator| declarator_list_hole_name(declarator).is_some())
    })
}

fn find_matching_var_declarators(
    runtime_module: &Module,
    needle: &ModuleItem,
    selector: &AnonymousStatementSelector,
) -> Result<Vec<MemberBindingMatch>> {
    let prepared = PreparedNeedle::new(needle, selector);
    let prefilter = VarDeclaratorPrefilter::new(needle, &prepared);
    let mut matches = Vec::new();
    for (body_idx, item) in runtime_module.body.iter().enumerate() {
        let Some(candidate_var) = item_var_decl(item) else {
            continue;
        };
        if !prefilter.var_decl_can_match(candidate_var) {
            continue;
        }
        for declarator in &candidate_var.decls {
            if !prefilter.declarator_can_match(declarator) {
                continue;
            }
            let candidate_item = module_item_for_single_var_declarator(item, declarator);
            if !prepared.matches(&candidate_item) {
                continue;
            }
            let declared = declared_bindings_for_var_declarator(declarator);
            match declared.as_slice() {
                [single] => matches.push(MemberBindingMatch {
                    body_idx,
                    binding: single.clone(),
                }),
                [] => {}
                _ => bail!(
                    "members[].selector.source_match matched a variable declarator at body \
                     index {body_idx}, but that declarator binds multiple names ({}). \
                     Refine the selector to a single-binding declarator.",
                    declared
                        .iter()
                        .map(|binding| binding.binding_name.as_str())
                        .collect::<Vec<_>>()
                        .join(", "),
                ),
            }
        }
    }
    Ok(matches)
}

/// Cheap candidate prefilters for the per-declarator matching loops.
/// Both `find_matching_var_declarators` paths clone a fresh
/// single-declarator `ModuleItem` per candidate declarator before
/// running the full structural match; these keys reject most
/// candidates before that clone.
struct VarDeclaratorPrefilter {
    /// `var`/`let`/`const` of the needle's declaration. Both the
    /// wildcard matcher (`match_var_decl`) and plain structural
    /// equality require kind equality, so a kind mismatch can never
    /// match — sound in every mode.
    needle_kind: VarDeclKind,
    /// The needle declarator's plain-`Ident` binding name, when the
    /// match is exact (no wildcards, no alpha renaming). In that mode
    /// equality requires the candidate declarator to bind the same
    /// plain ident, so a name mismatch can never match. `None`
    /// disables the name key (wildcards, alpha mode, or a
    /// destructuring pattern).
    needle_ident: Option<Atom>,
    /// Exact string-literal initializer of the needle declarator. This
    /// remains sound in alpha mode because alpha only renames
    /// identifiers; string literals must still match exactly unless
    /// selector-level string wildcards are enabled.
    needle_string_init: Option<String>,
}

impl VarDeclaratorPrefilter {
    fn new(needle: &ModuleItem, prepared: &PreparedNeedle) -> Self {
        let needle_var = selector_single_var_declarator(needle)
            .expect("caller checked the needle is a single-declarator var decl");
        let needle_declarator = &needle_var.decls[0];
        let needle_ident = (prepared.no_wildcards && !prepared.alpha)
            .then(|| match &needle_declarator.name {
                Pat::Ident(ident) => Some(ident.id.sym.clone()),
                _ => None,
            })
            .flatten();
        let needle_string_init = prepared
            .selector
            .wildcard_string_literals
            .is_empty()
            .then(|| {
                needle_declarator
                    .init
                    .as_deref()
                    .and_then(string_literal_expr_value)
            })
            .flatten();
        Self {
            needle_kind: needle_var.kind,
            needle_ident,
            needle_string_init,
        }
    }

    fn var_decl_can_match(&self, candidate: &VarDecl) -> bool {
        candidate.kind == self.needle_kind
    }

    fn declarator_can_match(&self, declarator: &VarDeclarator) -> bool {
        if let Some(needle_sym) = &self.needle_ident
            && !matches!(&declarator.name, Pat::Ident(ident) if ident.id.sym == *needle_sym)
        {
            return false;
        }
        let Some(needle_string_init) = &self.needle_string_init else {
            return true;
        };
        declarator
            .init
            .as_deref()
            .and_then(string_literal_expr_value)
            .as_ref()
            == Some(needle_string_init)
    }
}

/// Cheap candidate prefilter for variable declarations that use
/// `DECLARATORS_*` list holes. The full matcher must still validate
/// declaration structure and alpha bindings, but pinned direct string
/// predicates are enough to reject most declarations before recursive
/// list-hole alignment.
struct VarDeclWithDeclaratorHolesPrefilter {
    needle_kind: VarDeclKind,
    pinned_literal_predicates: Vec<StringLiteralPredicate>,
}

impl VarDeclWithDeclaratorHolesPrefilter {
    fn new(needle: &VarDecl, prepared: &PreparedNeedle<'_>) -> Self {
        let pinned_literal_predicates = needle
            .decls
            .iter()
            .filter(|declarator| declarator_list_hole_name(declarator).is_none())
            .filter_map(|declarator| {
                declarator.init.as_deref().and_then(|init| {
                    string_literal_predicate_for_expr(
                        init,
                        prepared.selector,
                        &prepared.string_literal_regexes,
                    )
                })
            })
            .collect();
        Self {
            needle_kind: needle.kind,
            pinned_literal_predicates,
        }
    }

    fn var_decl_can_match(&self, candidate: &VarDecl) -> bool {
        if candidate.kind != self.needle_kind {
            return false;
        }
        if self.pinned_literal_predicates.is_empty() {
            return true;
        }
        let mut candidate_start = 0;
        for predicate in &self.pinned_literal_predicates {
            let Some(candidate_idx) = candidate
                .decls
                .iter()
                .enumerate()
                .skip(candidate_start)
                .find_map(|(candidate_idx, declarator)| {
                    declarator
                        .init
                        .as_deref()
                        .and_then(string_literal_expr_value_ref)
                        .is_some_and(|value| predicate.matches(value))
                        .then_some(candidate_idx)
                })
            else {
                return false;
            };
            candidate_start = candidate_idx + 1;
        }
        true
    }
}

#[derive(Clone)]
enum StringLiteralPredicate {
    Exact(String),
    Regex(Option<Regex>),
}

impl StringLiteralPredicate {
    fn regex(pattern: String) -> Self {
        Self::Regex(Regex::new(&pattern).ok())
    }

    fn matches(&self, candidate_value: &Wtf8Atom) -> bool {
        match self {
            Self::Exact(expected) => candidate_value.to_string_lossy().as_ref() == expected,
            Self::Regex(compiled) => compiled
                .as_ref()
                .is_some_and(|regex| regex.is_match(candidate_value.to_string_lossy().as_ref())),
        }
    }
}

#[derive(Default)]
struct CompiledStringLiteralRegexes {
    patterns: BTreeMap<String, StringLiteralPredicate>,
}

impl CompiledStringLiteralRegexes {
    fn for_module_item(needle: &ModuleItem) -> Self {
        let mut collector = StringLiteralRegexPatternCollector::default();
        needle.visit_with(&mut collector);
        Self {
            patterns: collector
                .patterns
                .into_iter()
                .map(|pattern| (pattern.clone(), StringLiteralPredicate::regex(pattern)))
                .collect(),
        }
    }

    fn matches(&self, pattern: &str, candidate_value: &Wtf8Atom) -> bool {
        self.patterns
            .get(pattern)
            .cloned()
            .unwrap_or_else(|| StringLiteralPredicate::regex(pattern.to_string()))
            .matches(candidate_value)
    }

    fn predicate(&self, pattern: &str) -> StringLiteralPredicate {
        self.patterns
            .get(pattern)
            .cloned()
            .unwrap_or_else(|| StringLiteralPredicate::regex(pattern.to_string()))
    }
}

#[derive(Default)]
struct StringLiteralRegexPatternCollector {
    patterns: BTreeSet<String>,
}

impl Visit for StringLiteralRegexPatternCollector {
    fn visit_expr(&mut self, expr: &Expr) {
        if let Some(pattern) = string_literal_regex_pattern(expr) {
            self.patterns.insert(pattern);
            return;
        }
        expr.visit_children_with(self);
    }
}

fn string_literal_predicate_for_expr(
    expr: &Expr,
    selector: &AnonymousStatementSelector,
    string_literal_regexes: &CompiledStringLiteralRegexes,
) -> Option<StringLiteralPredicate> {
    if let Some(pattern) = string_literal_regex_pattern(expr) {
        return Some(string_literal_regexes.predicate(&pattern));
    }
    let Expr::Lit(Lit::Str(str_)) = expr else {
        return None;
    };
    let value = str_.value.to_string_lossy();
    if selector.wildcard_string_literals.contains(value.as_ref()) {
        return None;
    }
    Some(StringLiteralPredicate::Exact(value.to_string()))
}

fn string_literal_expr_value(expr: &Expr) -> Option<String> {
    match expr {
        Expr::Lit(Lit::Str(str_)) => Some(str_.value.to_string_lossy().to_string()),
        _ => None,
    }
}

fn string_literal_expr_value_ref(expr: &Expr) -> Option<&Wtf8Atom> {
    match expr {
        Expr::Lit(Lit::Str(str_)) => Some(&str_.value),
        _ => None,
    }
}

fn item_var_decl(item: &ModuleItem) -> Option<&VarDecl> {
    match item {
        ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) => Some(var),
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => match &export.decl {
            Decl::Var(var) => Some(var),
            _ => None,
        },
        _ => None,
    }
}

fn module_item_for_single_var_declarator(
    source_item: &ModuleItem,
    declarator: &VarDeclarator,
) -> ModuleItem {
    let (source_var, export_span) = match source_item {
        ModuleItem::Stmt(Stmt::Decl(Decl::Var(var))) => (var.as_ref(), None),
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => match &export.decl {
            Decl::Var(var) => (var.as_ref(), Some(export.span)),
            _ => unreachable!("item_var_decl already filtered source_item"),
        },
        _ => unreachable!("item_var_decl already filtered source_item"),
    };
    let var_decl = Decl::Var(Box::new(VarDecl {
        span: source_var.span,
        ctxt: source_var.ctxt,
        kind: source_var.kind,
        declare: source_var.declare,
        decls: vec![declarator.clone()],
    }));
    match export_span {
        Some(span) => ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(ExportDecl {
            span,
            decl: var_decl,
        })),
        None => ModuleItem::Stmt(Stmt::Decl(var_decl)),
    }
}

fn declared_bindings(item: &ModuleItem) -> Vec<ResolvedMemberBinding> {
    match item {
        ModuleItem::Stmt(Stmt::Decl(decl)) => declared_bindings_for_decl(decl),
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(export)) => {
            declared_bindings_for_decl(&export.decl)
        }
        ModuleItem::ModuleDecl(ModuleDecl::Import(import)) => import
            .specifiers
            .iter()
            .map(|specifier| match specifier {
                ImportSpecifier::Named(named) => named.local.sym.to_string(),
                ImportSpecifier::Default(default) => default.local.sym.to_string(),
                ImportSpecifier::Namespace(namespace) => namespace.local.sym.to_string(),
            })
            .map(|binding_name| ResolvedMemberBinding {
                binding_name,
                kind: Some(BindingSourceKind::ImportSpecifier),
            })
            .collect(),
        _ => Vec::new(),
    }
}

fn declared_bindings_for_decl(decl: &Decl) -> Vec<ResolvedMemberBinding> {
    match decl {
        Decl::Fn(f) => vec![ResolvedMemberBinding {
            binding_name: f.ident.sym.to_string(),
            kind: Some(BindingSourceKind::FunctionDeclaration),
        }],
        Decl::Class(c) => vec![ResolvedMemberBinding {
            binding_name: c.ident.sym.to_string(),
            kind: Some(BindingSourceKind::ClassDeclaration),
        }],
        Decl::Var(var) => var
            .decls
            .iter()
            .flat_map(declared_bindings_for_var_declarator)
            .collect(),
        _ => Vec::new(),
    }
}

fn declared_bindings_for_var_declarator(declarator: &VarDeclarator) -> Vec<ResolvedMemberBinding> {
    if declarator_list_hole_name(declarator).is_some() {
        return Vec::new();
    }
    binding_targets::binding_name_strings(&declarator.name)
        .into_iter()
        .map(|binding_name| ResolvedMemberBinding {
            binding_name,
            kind: Some(BindingSourceKind::VariableDeclarator),
        })
        .collect()
}

fn find_matching_body_indices(
    runtime_module: &Module,
    needle: &ModuleItem,
    selector: &AnonymousStatementSelector,
) -> Vec<usize> {
    let prepared = PreparedNeedle::new(needle, selector);
    let declarator_hole_prefilter = item_var_decl(needle)
        .filter(|var| {
            var.decls
                .iter()
                .any(|declarator| declarator_list_hole_name(declarator).is_some())
        })
        .map(|var| VarDeclWithDeclaratorHolesPrefilter::new(var, &prepared));
    runtime_module
        .body
        .iter()
        .enumerate()
        .filter_map(|(body_idx, item)| {
            if let Some(prefilter) = &declarator_hole_prefilter {
                let candidate_var = item_var_decl(item)?;
                if !prefilter.var_decl_can_match(candidate_var) {
                    return None;
                }
            }
            prepared.matches(item).then_some(body_idx)
        })
        .collect()
}

fn find_matching_body_ranges(
    runtime_module: &Module,
    needles: &[ModuleItem],
    selector: &AnonymousStatementSelector,
) -> Vec<usize> {
    if needles.is_empty() || needles.len() > runtime_module.body.len() {
        return Vec::new();
    }
    if let [needle] = needles {
        let prepared = PreparedNeedle::new(needle, selector);
        let declarator_hole_prefilter = item_var_decl(needle)
            .filter(|var| {
                var.decls
                    .iter()
                    .any(|declarator| declarator_list_hole_name(declarator).is_some())
            })
            .map(|var| VarDeclWithDeclaratorHolesPrefilter::new(var, &prepared));
        return runtime_module
            .body
            .iter()
            .enumerate()
            .filter_map(|(body_idx, candidate)| {
                if let Some(prefilter) = &declarator_hole_prefilter {
                    let candidate_var = item_var_decl(candidate)?;
                    if !prefilter.var_decl_can_match(candidate_var) {
                        return None;
                    }
                }
                prepared.matches(candidate).then_some(body_idx)
            })
            .collect();
    }
    let wildcard_idents = wildcard_ident_names_for_module_items(needles);
    let alpha = selector.identifiers == SourceMatchIdentifierMode::AlphaAll;
    runtime_module
        .body
        .windows(needles.len())
        .enumerate()
        .filter_map(|(body_idx, candidates)| {
            SyntaxContext::within_ignored_ctxt(|| {
                let mut matcher = AstWildcardMatcher::new(selector, &wildcard_idents, alpha);
                needles
                    .iter()
                    .zip(candidates)
                    .all(|(needle, candidate)| matcher.match_module_item(needle, candidate))
            })
            .then_some(body_idx)
        })
        .collect()
}

fn find_matching_body_group_alignments(
    runtime_module: &Module,
    needles: &[ModuleItem],
    selector: &AnonymousStatementSelector,
) -> Vec<Vec<Option<usize>>> {
    if needles.is_empty() {
        return Vec::new();
    }
    let mut segments: Vec<(usize, usize)> = Vec::new();
    let mut idx = 0;
    while idx < needles.len() {
        if module_item_list_hole_name(&needles[idx]).is_some() {
            idx += 1;
            continue;
        }
        let start = idx;
        while idx < needles.len() && module_item_list_hole_name(&needles[idx]).is_none() {
            idx += 1;
        }
        segments.push((start, idx - start));
    }
    if segments.is_empty() {
        return Vec::new();
    }

    let wildcard_idents = wildcard_ident_names_for_module_items(needles);
    let alpha = selector.identifiers == SourceMatchIdentifierMode::AlphaAll;
    SyntaxContext::within_ignored_ctxt(|| {
        let mut matcher = AstWildcardMatcher::new(selector, &wildcard_idents, alpha);
        let search = SegmentSearch {
            needle: needles,
            candidate: &runtime_module.body,
            segments: &segments,
            anchored_left: false,
            anchored_right: false,
        };
        let mut alignment = vec![None; needles.len()];
        let mut matches = Vec::new();
        place_module_item_segments(&mut matcher, &search, 0, 0, &mut alignment, &mut matches);
        matches
    })
}

#[derive(Debug, Eq, PartialEq)]
struct MismatchReason {
    score: usize,
    reason: String,
}

struct ClassCandidateHint {
    body_idx: usize,
    declared: Vec<String>,
    member_labels: Vec<String>,
    matched_pinned_labels: usize,
    pinned_label_count: usize,
    reason: MismatchReason,
}

struct VarDeclCandidateHint {
    body_idx: usize,
    declared: Vec<String>,
    declarator_labels: Vec<String>,
    matched_pinned_declarators: usize,
    pinned_declarator_count: usize,
    candidate_declarator_count: usize,
    reason: MismatchReason,
}

fn source_match_no_match_hint(
    runtime_module: &Module,
    selector: &AnonymousStatementSelector,
) -> Option<String> {
    let parsed =
        js_ast::parse_js_module_ast("<source_match diagnostic>", &selector.match_source).ok()?;
    let [needle] = parsed.body.as_slice() else {
        return None;
    };
    if module_item_list_hole_name(needle).is_some() {
        return None;
    }
    let wildcard_idents = wildcard_ident_names(needle);
    let alpha = selector.identifiers == SourceMatchIdentifierMode::AlphaAll;
    SyntaxContext::within_ignored_ctxt(|| {
        if let Some(hint) = class_source_match_no_match_hint(
            runtime_module,
            needle,
            selector,
            &wildcard_idents,
            alpha,
        ) {
            return Some(hint);
        }
        if let Some(hint) = var_declarator_source_match_no_match_hint(
            runtime_module,
            needle,
            selector,
            &wildcard_idents,
            alpha,
        ) {
            return Some(hint);
        }
        runtime_module
            .body
            .iter()
            .enumerate()
            .filter_map(|(body_idx, candidate)| {
                first_mismatch_reason(needle, candidate, selector, &wildcard_idents, alpha).map(
                    |reason| {
                        let declared = declared_bindings(candidate)
                            .into_iter()
                            .map(|binding| binding.binding_name)
                            .collect::<Vec<_>>();
                        (body_idx, declared, reason)
                    },
                )
            })
            .max_by_key(|(_, _, reason)| reason.score)
            .map(|(body_idx, declared, reason)| {
                let declared_hint = if declared.is_empty() {
                    "declares no bindings".to_string()
                } else {
                    format!(
                        "declares {}",
                        declared
                            .iter()
                            .map(|name| format!("`{name}`"))
                            .collect::<Vec<_>>()
                            .join(", ")
                    )
                };
                format!(
                    "\nNearest candidate: top-level body index {body_idx} ({declared_hint}).\
                     \nFirst mismatch: {reason}",
                    reason = reason.reason,
                )
            })
    })
}

fn class_source_match_no_match_hint(
    runtime_module: &Module,
    needle: &ModuleItem,
    selector: &AnonymousStatementSelector,
    wildcard_idents: &WildcardIdents,
    alpha: bool,
) -> Option<String> {
    let needle_class = module_item_class_decl(needle)?;
    let pinned_label_count = needle_class
        .class
        .body
        .iter()
        .filter(|member| !is_class_rest_hole(member) && class_member_label(member).is_some())
        .count();
    if pinned_label_count == 0 {
        return None;
    }

    let mut candidates = runtime_module
        .body
        .iter()
        .enumerate()
        .filter_map(|(body_idx, candidate)| {
            let candidate_class = module_item_class_decl(candidate)?;
            let reason = first_class_decl_mismatch_reason(
                needle_class,
                candidate_class,
                selector,
                wildcard_idents,
                alpha,
            )?;
            let declared = declared_bindings(candidate)
                .into_iter()
                .map(|binding| binding.binding_name)
                .collect::<Vec<_>>();
            let member_labels = candidate_class
                .class
                .body
                .iter()
                .filter_map(class_member_label)
                .collect::<Vec<_>>();
            let matched_pinned_labels = count_pinned_class_member_labels_in_order(
                &needle_class.class,
                &candidate_class.class,
            );
            Some(ClassCandidateHint {
                body_idx,
                declared,
                member_labels,
                matched_pinned_labels,
                pinned_label_count,
                reason,
            })
        })
        .collect::<Vec<_>>();

    if candidates.is_empty() {
        return None;
    }

    candidates.sort_by(|left, right| {
        right
            .matched_pinned_labels
            .cmp(&left.matched_pinned_labels)
            .then_with(|| right.reason.score.cmp(&left.reason.score))
            .then_with(|| left.body_idx.cmp(&right.body_idx))
    });

    let rendered = candidates
        .iter()
        .take(3)
        .map(render_class_candidate_hint)
        .collect::<Vec<_>>()
        .join("\n");

    Some(format!("\nNearest class candidates:\n{rendered}"))
}

fn var_declarator_source_match_no_match_hint(
    runtime_module: &Module,
    needle: &ModuleItem,
    selector: &AnonymousStatementSelector,
    wildcard_idents: &WildcardIdents,
    alpha: bool,
) -> Option<String> {
    let needle_var = item_var_decl(needle)?;
    if !needle_var
        .decls
        .iter()
        .any(|declarator| declarator_list_hole_name(declarator).is_some())
    {
        return None;
    }
    let pinned_declarator_count = needle_var
        .decls
        .iter()
        .filter(|declarator| declarator_list_hole_name(declarator).is_none())
        .count();
    if pinned_declarator_count == 0 {
        return None;
    }

    let mut candidates = runtime_module
        .body
        .iter()
        .enumerate()
        .filter_map(|(body_idx, candidate)| {
            let candidate_var = item_var_decl(candidate)?;
            if needle_var.kind != candidate_var.kind {
                return None;
            }
            let reason = first_var_decl_mismatch_reason(
                needle_var,
                candidate_var,
                selector,
                wildcard_idents,
                alpha,
            );
            let declared = declared_bindings(candidate)
                .into_iter()
                .map(|binding| binding.binding_name)
                .collect::<Vec<_>>();
            let declarator_labels = candidate_var
                .decls
                .iter()
                .map(render_var_declarator_label)
                .collect::<Vec<_>>();
            let matched_pinned_declarators = count_pinned_var_declarators_in_order(
                needle_var,
                candidate_var,
                selector,
                wildcard_idents,
                alpha,
            );
            Some(VarDeclCandidateHint {
                body_idx,
                declared,
                declarator_labels,
                matched_pinned_declarators,
                pinned_declarator_count,
                candidate_declarator_count: candidate_var.decls.len(),
                reason,
            })
        })
        .collect::<Vec<_>>();

    if candidates.is_empty() {
        return None;
    }

    candidates.sort_by(|left, right| {
        right
            .matched_pinned_declarators
            .cmp(&left.matched_pinned_declarators)
            .then_with(|| {
                right
                    .candidate_declarator_count
                    .cmp(&left.candidate_declarator_count)
            })
            .then_with(|| right.reason.score.cmp(&left.reason.score))
            .then_with(|| left.body_idx.cmp(&right.body_idx))
    });

    let rendered = candidates
        .iter()
        .take(3)
        .map(render_var_decl_candidate_hint)
        .collect::<Vec<_>>()
        .join("\n");
    let guidance = render_var_declarator_selector_guidance(selector);

    Some(format!(
        "\nNearest variable declaration candidates:\n{rendered}\n{guidance}"
    ))
}

fn first_class_decl_mismatch_reason(
    needle: &ClassDecl,
    candidate: &ClassDecl,
    selector: &AnonymousStatementSelector,
    wildcard_idents: &WildcardIdents,
    alpha: bool,
) -> Option<MismatchReason> {
    let mut matcher = AstWildcardMatcher::new(selector, wildcard_idents, alpha);
    if matcher.match_decl(
        &Decl::Class(needle.clone()),
        &Decl::Class(candidate.clone()),
    ) {
        return None;
    }
    Some(first_decl_mismatch_reason(
        &Decl::Class(needle.clone()),
        &Decl::Class(candidate.clone()),
        selector,
        wildcard_idents,
        alpha,
    ))
}

fn render_class_candidate_hint(candidate: &ClassCandidateHint) -> String {
    let declared_hint = if candidate.declared.is_empty() {
        "declares no bindings".to_string()
    } else {
        format!(
            "declares {}",
            candidate
                .declared
                .iter()
                .map(|name| format!("`{name}`"))
                .collect::<Vec<_>>()
                .join(", ")
        )
    };
    let member_hint = render_class_member_labels(&candidate.member_labels);
    format!(
        "- top-level body index {body_idx} ({declared_hint}); members: {member_hint}; \
         matched {matched}/{total} pinned member names in order. First mismatch: {reason}",
        body_idx = candidate.body_idx,
        matched = candidate.matched_pinned_labels,
        total = candidate.pinned_label_count,
        reason = candidate.reason.reason,
    )
}

fn render_class_member_labels(labels: &[String]) -> String {
    const MAX_LABELS: usize = 12;
    if labels.is_empty() {
        return "<none>".to_string();
    }
    let mut rendered = labels
        .iter()
        .take(MAX_LABELS)
        .map(|label| format!("`{label}`"))
        .collect::<Vec<_>>();
    if labels.len() > MAX_LABELS {
        rendered.push(format!("... +{} more", labels.len() - MAX_LABELS));
    }
    rendered.join(", ")
}

fn render_var_decl_candidate_hint(candidate: &VarDeclCandidateHint) -> String {
    let declared_hint = if candidate.declared.is_empty() {
        "declares no bindings".to_string()
    } else {
        format!(
            "declares {}",
            candidate
                .declared
                .iter()
                .map(|name| format!("`{name}`"))
                .collect::<Vec<_>>()
                .join(", ")
        )
    };
    let declarator_hint = render_var_declarator_labels(&candidate.declarator_labels);
    format!(
        "- top-level body index {body_idx} ({declared_hint}); declarators: {declarator_hint}; \
         matched {matched}/{total} pinned declarators in order. First mismatch: {reason}",
        body_idx = candidate.body_idx,
        matched = candidate.matched_pinned_declarators,
        total = candidate.pinned_declarator_count,
        reason = candidate.reason.reason,
    )
}

fn render_var_declarator_selector_guidance(selector: &AnonymousStatementSelector) -> String {
    let mut guidance = "Selector guidance: add `DECLARATORS_* = null` pseudo-declarators \
before, after, or between pinned declarators for unrelated siblings in the same \
var/let/const declaration."
        .to_string();
    if selector.target_binding.is_some() {
        guidance.push_str(
            " `target_binding` resolves one selector-local binding for the current export; \
use one `binding_groups` entry when several exports should come from this matched declaration.",
        );
    } else {
        guidance.push_str(
            " Use `binding_groups` when several exports should come from one matched declaration.",
        );
    }
    guidance
}

fn render_var_declarator_labels(labels: &[String]) -> String {
    const MAX_LABELS: usize = 10;
    if labels.is_empty() {
        return "<none>".to_string();
    }
    let mut rendered = labels.iter().take(MAX_LABELS).cloned().collect::<Vec<_>>();
    if labels.len() > MAX_LABELS {
        rendered.push(format!("... +{} more", labels.len() - MAX_LABELS));
    }
    rendered.join(", ")
}

fn render_var_declarator_label(declarator: &VarDeclarator) -> String {
    if let Some(hole_name) = declarator_list_hole_name(declarator) {
        return format!("`{hole_name}`");
    }
    let bindings = binding_targets::binding_name_strings(&declarator.name);
    let binding_label = if bindings.is_empty() {
        "<pattern>".to_string()
    } else {
        bindings.join("/")
    };
    match declarator.init.as_deref() {
        Some(init) => format!("`{binding_label} = {}`", expr_shape_label(init)),
        None => format!("`{binding_label}`"),
    }
}

fn expr_shape_label(expr: &Expr) -> String {
    if string_literal_regex_pattern(expr).is_some() {
        return format!("{STRING_LITERAL_REGEX_PREDICATE}(...)");
    }
    match expr {
        Expr::Ident(ident) => ident.sym.to_string(),
        Expr::Lit(lit) => lit_shape_label(lit),
        Expr::Call(call) => format!("{}(...)", callee_shape_label(&call.callee)),
        Expr::New(new) => format!("new {}(...)", expr_shape_label(&new.callee)),
        Expr::Member(member) => member_expr_shape_label(member),
        Expr::Object(_) => "{...}".to_string(),
        Expr::Array(_) => "[...]".to_string(),
        Expr::Arrow(_) => "(...) => ...".to_string(),
        Expr::Fn(_) => "function(...)".to_string(),
        Expr::Class(_) => "class {...}".to_string(),
        Expr::Tpl(_) => "`...`".to_string(),
        Expr::TaggedTpl(tagged) => format!("{} `...`", expr_shape_label(&tagged.tag)),
        _ => "expression".to_string(),
    }
}

fn callee_shape_label(callee: &Callee) -> String {
    match callee {
        Callee::Super(_) => "super".to_string(),
        Callee::Import(_) => "import".to_string(),
        Callee::Expr(expr) => expr_shape_label(expr),
    }
}

fn member_expr_shape_label(member: &MemberExpr) -> String {
    let obj = expr_shape_label(&member.obj);
    match &member.prop {
        MemberProp::Ident(prop) => format!("{obj}.{}", prop.sym),
        MemberProp::PrivateName(prop) => format!("{obj}.#{}", prop.name),
        MemberProp::Computed(_) => format!("{obj}[...]"),
    }
}

fn lit_shape_label(lit: &Lit) -> String {
    match lit {
        Lit::Str(str_) => format!("\"{}\"", str_.value.to_string_lossy()),
        Lit::Bool(bool_) => bool_.value.to_string(),
        Lit::Null(_) => "null".to_string(),
        Lit::Num(num) => num.value.to_string(),
        Lit::BigInt(_) => "0n".to_string(),
        Lit::Regex(_) => "/.../".to_string(),
        Lit::JSXText(_) => "jsx-text".to_string(),
    }
}

fn key_value_prop_ident_value(prop: &KeyValueProp) -> Option<&Ident> {
    match prop.value.as_ref() {
        Expr::Ident(ident) => Some(ident),
        _ => None,
    }
}

fn key_value_pat_binding_ident_value(prop: &KeyValuePatProp) -> Option<&BindingIdent> {
    match prop.value.as_ref() {
        Pat::Ident(ident) => Some(ident),
        _ => None,
    }
}

fn prop_name_matches_ident_key(prop_name: &PropName, ident: &Ident) -> bool {
    match prop_name {
        PropName::Ident(key) => key.sym == ident.sym,
        PropName::Str(key) => key.value.to_string_lossy() == ident.sym.as_ref(),
        _ => false,
    }
}

fn prop_name_matches_binding_key(prop_name: &PropName, ident: &BindingIdent) -> bool {
    prop_name_matches_ident_key(prop_name, &ident.id)
}

fn count_pinned_class_member_labels_in_order(needle: &Class, candidate: &Class) -> usize {
    let mut candidate_start = 0;
    let mut matched = 0;
    for needle_member in &needle.body {
        if is_class_rest_hole(needle_member) {
            continue;
        }
        let Some(needle_label) = class_member_label(needle_member) else {
            continue;
        };
        let Some(candidate_idx) = candidate
            .body
            .iter()
            .enumerate()
            .skip(candidate_start)
            .find_map(|(candidate_idx, candidate_member)| {
                (class_member_label(candidate_member).as_deref() == Some(needle_label.as_str()))
                    .then_some(candidate_idx)
            })
        else {
            break;
        };
        matched += 1;
        candidate_start = candidate_idx + 1;
    }
    matched
}

fn count_pinned_var_declarators_in_order(
    needle: &VarDecl,
    candidate: &VarDecl,
    selector: &AnonymousStatementSelector,
    wildcard_idents: &WildcardIdents,
    alpha: bool,
) -> usize {
    pinned_var_declarator_matches_in_order(needle, candidate, selector, wildcard_idents, alpha)
        .len()
}

fn pinned_var_declarator_matches_in_order(
    needle: &VarDecl,
    candidate: &VarDecl,
    selector: &AnonymousStatementSelector,
    wildcard_idents: &WildcardIdents,
    alpha: bool,
) -> Vec<(usize, usize)> {
    let mut matcher = AstWildcardMatcher::new(selector, wildcard_idents, alpha);
    let mut candidate_start = 0;
    let mut matches = Vec::new();
    for (needle_idx, needle_declarator) in needle.decls.iter().enumerate() {
        if declarator_list_hole_name(needle_declarator).is_some() {
            continue;
        }
        let Some(candidate_idx) = candidate
            .decls
            .iter()
            .enumerate()
            .skip(candidate_start)
            .find_map(|(candidate_idx, candidate_declarator)| {
                let snapshot = matcher.snapshot();
                if matcher.match_var_declarator(needle_declarator, candidate_declarator) {
                    Some(candidate_idx)
                } else {
                    matcher.restore(snapshot);
                    None
                }
            })
        else {
            break;
        };
        matches.push((needle_idx, candidate_idx));
        candidate_start = candidate_idx + 1;
    }
    matches
}

fn first_pinned_var_declarator_mismatch_reason(
    needle: &VarDecl,
    candidate: &VarDecl,
    selector: &AnonymousStatementSelector,
    wildcard_idents: &WildcardIdents,
    alpha: bool,
) -> String {
    let matches =
        pinned_var_declarator_matches_in_order(needle, candidate, selector, wildcard_idents, alpha);
    let pinned_indices = needle
        .decls
        .iter()
        .enumerate()
        .filter_map(|(idx, declarator)| {
            declarator_list_hole_name(declarator)
                .is_none()
                .then_some(idx)
        })
        .collect::<Vec<_>>();
    if let Some(&needle_idx) = pinned_indices.get(matches.len()) {
        let candidate_start = matches
            .last()
            .map(|(_, candidate_idx)| candidate_idx + 1)
            .unwrap_or(0);
        let remaining = candidate
            .decls
            .iter()
            .skip(candidate_start)
            .map(render_var_declarator_label)
            .collect::<Vec<_>>();
        let remaining_hint = if remaining.is_empty() {
            "no candidate declarators remain".to_string()
        } else {
            format!(
                "remaining candidate declarators: {}",
                render_var_declarator_labels(&remaining)
            )
        };
        return format!(
            "selector pinned declarator #{needle_idx} {} was not found in order ({remaining_hint})",
            render_var_declarator_label(&needle.decls[needle_idx]),
        );
    }
    first_var_declarator_hole_placement_mismatch_reason(needle, candidate, &matches)
}

fn first_var_declarator_hole_placement_mismatch_reason(
    needle: &VarDecl,
    candidate: &VarDecl,
    matches: &[(usize, usize)],
) -> String {
    let Some(&(first_needle_idx, first_candidate_idx)) = matches.first() else {
        return "pinned declarators matched in order, but DECLARATORS_* hole placement differed"
            .to_string();
    };
    if first_candidate_idx > 0 && !has_declarator_hole_before(needle, first_needle_idx) {
        let skipped = candidate
            .decls
            .iter()
            .take(first_candidate_idx)
            .map(render_var_declarator_label)
            .collect::<Vec<_>>();
        return format!(
            "candidate has unmatched leading declarator(s) before selector declarator \
             #{first_needle_idx} {}: {}. Add a `DECLARATORS_* = null` pseudo-declarator \
             before the first pinned declarator.",
            render_var_declarator_label(&needle.decls[first_needle_idx]),
            render_var_declarator_labels(&skipped),
        );
    }

    for pair in matches.windows(2) {
        let [left, right] = pair else {
            continue;
        };
        let (left_needle_idx, left_candidate_idx) = *left;
        let (right_needle_idx, right_candidate_idx) = *right;
        let gap_start = left_candidate_idx + 1;
        if gap_start >= right_candidate_idx {
            continue;
        }
        if has_declarator_hole_between(needle, left_needle_idx, right_needle_idx) {
            continue;
        }
        let skipped = candidate.decls[gap_start..right_candidate_idx]
            .iter()
            .map(render_var_declarator_label)
            .collect::<Vec<_>>();
        return format!(
            "candidate has unmatched declarator(s) between selector declarator \
             #{left_needle_idx} {} and #{right_needle_idx} {}: {}. Add a \
             `DECLARATORS_* = null` pseudo-declarator between those pinned declarators.",
            render_var_declarator_label(&needle.decls[left_needle_idx]),
            render_var_declarator_label(&needle.decls[right_needle_idx]),
            render_var_declarator_labels(&skipped),
        );
    }

    let Some(&(last_needle_idx, last_candidate_idx)) = matches.last() else {
        return "pinned declarators matched in order, but DECLARATORS_* hole placement differed"
            .to_string();
    };
    if last_candidate_idx + 1 < candidate.decls.len()
        && !has_declarator_hole_after(needle, last_needle_idx)
    {
        let skipped = candidate
            .decls
            .iter()
            .skip(last_candidate_idx + 1)
            .map(render_var_declarator_label)
            .collect::<Vec<_>>();
        return format!(
            "candidate has unmatched trailing declarator(s) after selector declarator \
             #{last_needle_idx} {}: {}. Add a `DECLARATORS_* = null` pseudo-declarator \
             after the last pinned declarator.",
            render_var_declarator_label(&needle.decls[last_needle_idx]),
            render_var_declarator_labels(&skipped),
        );
    }

    "pinned declarators matched in order, but DECLARATORS_* hole placement differed. \
Check that each unrelated sibling declarator is covered by a hole before, after, or \
between pinned declarators."
        .to_string()
}

fn has_declarator_hole_before(needle: &VarDecl, needle_idx: usize) -> bool {
    needle.decls[..needle_idx]
        .iter()
        .any(|declarator| declarator_list_hole_name(declarator).is_some())
}

fn has_declarator_hole_between(
    needle: &VarDecl,
    left_needle_idx: usize,
    right_needle_idx: usize,
) -> bool {
    needle.decls[left_needle_idx + 1..right_needle_idx]
        .iter()
        .any(|declarator| declarator_list_hole_name(declarator).is_some())
}

fn has_declarator_hole_after(needle: &VarDecl, needle_idx: usize) -> bool {
    needle.decls[needle_idx + 1..]
        .iter()
        .any(|declarator| declarator_list_hole_name(declarator).is_some())
}

fn module_item_class_decl(item: &ModuleItem) -> Option<&ClassDecl> {
    match item {
        ModuleItem::Stmt(Stmt::Decl(Decl::Class(class))) => Some(class),
        ModuleItem::ModuleDecl(ModuleDecl::ExportDecl(ExportDecl {
            decl: Decl::Class(class),
            ..
        })) => Some(class),
        _ => None,
    }
}

fn first_mismatch_reason(
    needle: &ModuleItem,
    candidate: &ModuleItem,
    selector: &AnonymousStatementSelector,
    wildcard_idents: &WildcardIdents,
    alpha: bool,
) -> Option<MismatchReason> {
    let mut matcher = AstWildcardMatcher::new(selector, wildcard_idents, alpha);
    if matcher.match_module_item(needle, candidate) {
        return None;
    }
    Some(match (needle, candidate) {
        (ModuleItem::Stmt(needle), ModuleItem::Stmt(candidate)) => {
            first_stmt_mismatch_reason(needle, candidate, selector, wildcard_idents, alpha)
        }
        (ModuleItem::ModuleDecl(needle), ModuleItem::ModuleDecl(candidate)) => {
            first_module_decl_mismatch_reason(needle, candidate, selector, wildcard_idents, alpha)
        }
        _ => MismatchReason {
            score: 1,
            reason: format!(
                "top-level item kind differs: selector is {}, candidate is {}",
                module_item_kind(needle),
                module_item_kind(candidate),
            ),
        },
    })
}

fn first_module_decl_mismatch_reason(
    needle: &ModuleDecl,
    candidate: &ModuleDecl,
    selector: &AnonymousStatementSelector,
    wildcard_idents: &WildcardIdents,
    alpha: bool,
) -> MismatchReason {
    match (needle, candidate) {
        (ModuleDecl::ExportDecl(needle), ModuleDecl::ExportDecl(candidate)) => {
            first_decl_mismatch_reason(
                &needle.decl,
                &candidate.decl,
                selector,
                wildcard_idents,
                alpha,
            )
        }
        _ if std::mem::discriminant(needle) != std::mem::discriminant(candidate) => {
            MismatchReason {
                score: 10,
                reason: format!(
                    "module declaration kind differs: selector is {}, candidate is {}",
                    module_decl_kind(needle),
                    module_decl_kind(candidate),
                ),
            }
        }
        _ => MismatchReason {
            score: 20,
            reason: "module declaration shape differs".to_string(),
        },
    }
}

fn first_stmt_mismatch_reason(
    needle: &Stmt,
    candidate: &Stmt,
    selector: &AnonymousStatementSelector,
    wildcard_idents: &WildcardIdents,
    alpha: bool,
) -> MismatchReason {
    match (needle, candidate) {
        (Stmt::Decl(needle), Stmt::Decl(candidate)) => {
            first_decl_mismatch_reason(needle, candidate, selector, wildcard_idents, alpha)
        }
        _ if std::mem::discriminant(needle) != std::mem::discriminant(candidate) => {
            MismatchReason {
                score: 10,
                reason: format!(
                    "statement kind differs: selector is {}, candidate is {}",
                    stmt_kind(needle),
                    stmt_kind(candidate),
                ),
            }
        }
        _ => MismatchReason {
            score: 20,
            reason: "statement shape differs".to_string(),
        },
    }
}

fn first_decl_mismatch_reason(
    needle: &Decl,
    candidate: &Decl,
    selector: &AnonymousStatementSelector,
    wildcard_idents: &WildcardIdents,
    alpha: bool,
) -> MismatchReason {
    match (needle, candidate) {
        (Decl::Class(needle), Decl::Class(candidate)) => {
            if !alpha && needle.ident.sym != candidate.ident.sym {
                return MismatchReason {
                    score: 40,
                    reason: format!(
                        "class name differs: selector `{}`, candidate `{}`",
                        needle.ident.sym, candidate.ident.sym,
                    ),
                };
            }
            first_class_mismatch_reason(
                &needle.class,
                &candidate.class,
                selector,
                wildcard_idents,
                alpha,
            )
        }
        (Decl::Fn(needle), Decl::Fn(candidate)) => {
            if !alpha && needle.ident.sym != candidate.ident.sym {
                return MismatchReason {
                    score: 40,
                    reason: format!(
                        "function name differs: selector `{}`, candidate `{}`",
                        needle.ident.sym, candidate.ident.sym,
                    ),
                };
            }
            MismatchReason {
                score: 35,
                reason: "function signature or body differs".to_string(),
            }
        }
        (Decl::Var(needle), Decl::Var(candidate)) => {
            first_var_decl_mismatch_reason(needle, candidate, selector, wildcard_idents, alpha)
        }
        _ if std::mem::discriminant(needle) != std::mem::discriminant(candidate) => {
            MismatchReason {
                score: 30,
                reason: format!(
                    "declaration kind differs: selector is {}, candidate is {}",
                    decl_kind(needle),
                    decl_kind(candidate),
                ),
            }
        }
        _ => MismatchReason {
            score: 35,
            reason: "declaration shape differs".to_string(),
        },
    }
}

fn first_var_decl_mismatch_reason(
    needle: &VarDecl,
    candidate: &VarDecl,
    selector: &AnonymousStatementSelector,
    wildcard_idents: &WildcardIdents,
    alpha: bool,
) -> MismatchReason {
    if needle.kind != candidate.kind {
        return MismatchReason {
            score: 45,
            reason: format!(
                "variable declaration kind differs: selector is {}, candidate is {}",
                var_decl_kind(needle.kind),
                var_decl_kind(candidate.kind),
            ),
        };
    }
    let mut matcher = AstWildcardMatcher::new(selector, wildcard_idents, alpha);
    if matcher
        .match_var_declarator_slice_with_alignment(&needle.decls, &candidate.decls)
        .is_none()
    {
        let reason = if needle
            .decls
            .iter()
            .any(|declarator| declarator_list_hole_name(declarator).is_some())
        {
            first_pinned_var_declarator_mismatch_reason(
                needle,
                candidate,
                selector,
                wildcard_idents,
                alpha,
            )
        } else {
            format!(
                "variable declarators differ: selector has {} declarator(s), candidate has {}",
                needle.decls.len(),
                candidate.decls.len(),
            )
        };
        return MismatchReason { score: 55, reason };
    }
    MismatchReason {
        score: 35,
        reason: "variable declaration shape differs".to_string(),
    }
}

fn first_class_mismatch_reason(
    needle: &Class,
    candidate: &Class,
    selector: &AnonymousStatementSelector,
    wildcard_idents: &WildcardIdents,
    alpha: bool,
) -> MismatchReason {
    let mut candidate_start = 0;
    for needle_member in &needle.body {
        if is_class_rest_hole(needle_member) {
            continue;
        }
        let Some(needle_label) = class_member_label(needle_member) else {
            continue;
        };
        let mut found_label = false;
        for (candidate_idx, candidate_member) in
            candidate.body.iter().enumerate().skip(candidate_start)
        {
            if class_member_label(candidate_member).as_deref() != Some(needle_label.as_str()) {
                continue;
            }
            found_label = true;
            candidate_start = candidate_idx + 1;
            let mut matcher = AstWildcardMatcher::new(selector, wildcard_idents, alpha);
            if !matcher.match_class_member(needle_member, candidate_member) {
                return MismatchReason {
                    score: 65,
                    reason: format!(
                        "class member `{needle_label}` matched by name, but its signature or body differs"
                    ),
                };
            }
            break;
        }
        if !found_label {
            return MismatchReason {
                score: 70,
                reason: format!(
                    "selector class pinned member `{needle_label}` was not found in the candidate class body in order"
                ),
            };
        }
    }
    MismatchReason {
        score: 45,
        reason: "class heritage, decorators, or member order differs".to_string(),
    }
}

fn class_member_label(member: &ClassMember) -> Option<String> {
    match member {
        ClassMember::Constructor(_) => Some("constructor".to_string()),
        ClassMember::Method(method) => prop_name_label(&method.key),
        ClassMember::PrivateMethod(method) => Some(format!("#{}", method.key.name)),
        ClassMember::ClassProp(prop) => prop_name_label(&prop.key),
        ClassMember::PrivateProp(prop) => Some(format!("#{}", prop.key.name)),
        ClassMember::AutoAccessor(accessor) => match &accessor.key {
            Key::Public(key) => prop_name_label(key),
            Key::Private(key) => Some(format!("#{}", key.name)),
        },
        ClassMember::StaticBlock(_) => Some("static block".to_string()),
        _ => None,
    }
}

fn prop_name_label(name: &PropName) -> Option<String> {
    match name {
        PropName::Ident(ident) => Some(ident.sym.to_string()),
        PropName::Str(str_) => Some(str_.value.to_string_lossy().to_string()),
        PropName::Num(num) => Some(num.value.to_string()),
        PropName::BigInt(bigint) => Some(bigint.value.to_string()),
        PropName::Computed(_) => Some("<computed>".to_string()),
    }
}

fn module_item_kind(item: &ModuleItem) -> &'static str {
    match item {
        ModuleItem::ModuleDecl(_) => "module declaration",
        ModuleItem::Stmt(_) => "statement",
    }
}

fn module_decl_kind(decl: &ModuleDecl) -> &'static str {
    match decl {
        ModuleDecl::Import(_) => "import",
        ModuleDecl::ExportDecl(_) => "export declaration",
        ModuleDecl::ExportNamed(_) => "named export",
        ModuleDecl::ExportDefaultDecl(_) => "default declaration export",
        ModuleDecl::ExportDefaultExpr(_) => "default expression export",
        ModuleDecl::ExportAll(_) => "export all",
        ModuleDecl::TsImportEquals(_) => "typescript import equals",
        ModuleDecl::TsExportAssignment(_) => "typescript export assignment",
        ModuleDecl::TsNamespaceExport(_) => "typescript namespace export",
    }
}

fn stmt_kind(stmt: &Stmt) -> &'static str {
    match stmt {
        Stmt::Block(_) => "block",
        Stmt::Empty(_) => "empty",
        Stmt::Debugger(_) => "debugger",
        Stmt::With(_) => "with",
        Stmt::Return(_) => "return",
        Stmt::Labeled(_) => "labeled",
        Stmt::Break(_) => "break",
        Stmt::Continue(_) => "continue",
        Stmt::If(_) => "if",
        Stmt::Switch(_) => "switch",
        Stmt::Throw(_) => "throw",
        Stmt::Try(_) => "try",
        Stmt::While(_) => "while",
        Stmt::DoWhile(_) => "do while",
        Stmt::For(_) => "for",
        Stmt::ForIn(_) => "for in",
        Stmt::ForOf(_) => "for of",
        Stmt::Decl(_) => "declaration",
        Stmt::Expr(_) => "expression",
    }
}

fn decl_kind(decl: &Decl) -> &'static str {
    match decl {
        Decl::Class(_) => "class",
        Decl::Fn(_) => "function",
        Decl::Var(_) => "variable",
        Decl::Using(_) => "using",
        Decl::TsInterface(_) => "typescript interface",
        Decl::TsTypeAlias(_) => "typescript type alias",
        Decl::TsEnum(_) => "typescript enum",
        Decl::TsModule(_) => "typescript module",
    }
}

fn var_decl_kind(kind: VarDeclKind) -> &'static str {
    match kind {
        VarDeclKind::Var => "var",
        VarDeclKind::Let => "let",
        VarDeclKind::Const => "const",
    }
}

fn place_module_item_segments(
    matcher: &mut AstWildcardMatcher<'_>,
    search: &SegmentSearch<'_, ModuleItem>,
    seg_idx: usize,
    cand_min: usize,
    alignment: &mut [Option<usize>],
    matches: &mut Vec<Vec<Option<usize>>>,
) {
    let Some(&(needle_start, seg_len)) = search.segments.get(seg_idx) else {
        matches.push(alignment.to_vec());
        return;
    };
    let remaining: usize = search.segments[seg_idx..].iter().map(|(_, len)| len).sum();
    let Some(latest_start) = search.candidate.len().checked_sub(remaining) else {
        return;
    };
    for start in cand_min..=latest_start {
        let snapshot = matcher.snapshot();
        let alignment_snapshot = alignment.to_vec();
        let mut segment_ok = true;
        for offset in 0..seg_len {
            let needle_idx = needle_start + offset;
            let candidate_idx = start + offset;
            if !matcher
                .match_module_item(&search.needle[needle_idx], &search.candidate[candidate_idx])
            {
                segment_ok = false;
                break;
            }
            alignment[needle_idx] = Some(candidate_idx);
        }
        if segment_ok {
            place_module_item_segments(
                matcher,
                search,
                seg_idx + 1,
                start + seg_len,
                alignment,
                matches,
            );
        }
        matcher.restore(snapshot);
        alignment.copy_from_slice(&alignment_snapshot);
    }
}

fn anonymous_selector_target_statement_indices(
    request_id: &str,
    selector: &AnonymousStatementSelector,
    parsed_items: &[ModuleItem],
) -> Result<Vec<usize>> {
    if selector.target_statement.is_some() && selector.target_statements.is_some() {
        bail!(
            "logical_module {request_id}: anonymous_statements[].source_match cannot include \
             both `target_statement` and `target_statements`:\n{match_source}",
            match_source = selector.match_source,
        );
    }
    if let Some(target_statement) = selector.target_statement {
        if parsed_items.is_empty() {
            bail!(
                "logical_module {request_id}: anonymous_statements[].source_match with \
                 target_statement parsed to zero statements:\n{match_source}",
                match_source = selector.match_source,
            );
        }
        return validate_anonymous_target_statement_indices(
            request_id,
            selector,
            parsed_items,
            vec![target_statement],
            "target_statement",
        );
    }
    if let Some(target_statements) = &selector.target_statements {
        if parsed_items.is_empty() {
            bail!(
                "logical_module {request_id}: anonymous_statements[].source_match with \
                 target_statements parsed to zero statements:\n{match_source}",
                match_source = selector.match_source,
            );
        }
        let indices = match target_statements {
            TargetStatements::Indices(indices) => indices.clone(),
            TargetStatements::All(TargetStatementsAll::All) => parsed_items
                .iter()
                .enumerate()
                .filter_map(|(idx, item)| module_item_list_hole_name(item).is_none().then_some(idx))
                .collect(),
        };
        return validate_anonymous_target_statement_indices(
            request_id,
            selector,
            parsed_items,
            indices,
            "target_statements",
        );
    }

    match parsed_items {
        [] => bail!(
            "logical_module {request_id}: anonymous_statements[].match parsed to zero \
             statements; selector source must contain exactly one top-level statement:\n{match_source}",
            match_source = selector.match_source,
        ),
        [single] if module_item_list_hole_name(single).is_some() => bail!(
            "logical_module {request_id}: anonymous_statements[].match parsed to a STMT_LIST \
             hole; selector source must contain a pinned top-level statement to claim:\n{match_source}",
            match_source = selector.match_source,
        ),
        [_] => Ok(vec![0]),
        _ => bail!(
            "logical_module {request_id}: anonymous_statements[].match parsed to {} statements; \
             selector source must contain exactly one top-level statement unless \
             `target_statement` or `target_statements` is set:\n{match_source}",
            parsed_items.len(),
            match_source = selector.match_source,
        ),
    }
}

fn validate_anonymous_target_statement_indices(
    request_id: &str,
    selector: &AnonymousStatementSelector,
    parsed_items: &[ModuleItem],
    indices: Vec<usize>,
    field_name: &str,
) -> Result<Vec<usize>> {
    if indices.is_empty() {
        bail!(
            "logical_module {request_id}: anonymous_statements[].source_match \
             `{field_name}` selected no top-level statements:\n{match_source}",
            match_source = selector.match_source,
        );
    }
    let mut seen = BTreeSet::new();
    for idx in &indices {
        if !seen.insert(*idx) {
            bail!(
                "logical_module {request_id}: anonymous_statements[].source_match \
                 `{field_name}` contains duplicate index {idx}:\n{match_source}",
                match_source = selector.match_source,
            );
        }
        if *idx >= parsed_items.len() {
            bail!(
                "logical_module {request_id}: anonymous_statements[].source_match \
                 `{field_name}` index {idx} is out of range for {} parsed top-level \
                 statements:\n{match_source}",
                parsed_items.len(),
                match_source = selector.match_source,
            );
        }
        if module_item_list_hole_name(&parsed_items[*idx]).is_some() {
            bail!(
                "logical_module {request_id}: anonymous_statements[].source_match \
                 `{field_name}` index {idx} points at a STMT_LIST hole, not a pinned \
                 selector statement:\n{match_source}",
                match_source = selector.match_source,
            );
        }
    }
    Ok(indices)
}

/// Needle-derived matching state hoisted out of the per-candidate
/// loops. The previous `module_items_match` recomputed the needle's
/// wildcard-ident set — and, in alpha mode without wildcards, cloned
/// and re-canonicalized BOTH trees — once per candidate comparison;
/// the needle side of that work is invariant across candidates.
struct PreparedNeedle<'a> {
    needle: &'a ModuleItem,
    selector: &'a AnonymousStatementSelector,
    wildcard_idents: WildcardIdents,
    string_literal_regexes: CompiledStringLiteralRegexes,
    alpha: bool,
    /// Neither string-literal nor syntactic-hole wildcards/predicates
    /// present — the plain structural-equality fast path applies.
    no_wildcards: bool,
    /// The needle pre-canonicalized once for the no-wildcard alpha
    /// path (`None` otherwise).
    canonical_needle: Option<ModuleItem>,
}

impl<'a> PreparedNeedle<'a> {
    fn new(needle: &'a ModuleItem, selector: &'a AnonymousStatementSelector) -> Self {
        SyntaxContext::within_ignored_ctxt(|| {
            let wildcard_idents = wildcard_ident_names(needle);
            let string_literal_regexes = CompiledStringLiteralRegexes::for_module_item(needle);
            let alpha = selector.identifiers == SourceMatchIdentifierMode::AlphaAll;
            let no_wildcards =
                selector.wildcard_string_literals.is_empty() && wildcard_idents.is_empty();
            let canonical_needle = (no_wildcards && alpha).then(|| {
                let mut canonical = needle.clone();
                canonical.visit_mut_with(&mut AlphaIdentCanonicalizer::new(&wildcard_idents));
                canonical
            });
            Self {
                needle,
                selector,
                wildcard_idents,
                string_literal_regexes,
                alpha,
                no_wildcards,
                canonical_needle,
            }
        })
    }

    fn matches(&self, candidate: &ModuleItem) -> bool {
        SyntaxContext::within_ignored_ctxt(|| {
            if self.no_wildcards {
                // No wildcards: plain structural equality. The cheap
                // shape prefilter rejects most candidates before the
                // alpha path's per-candidate clone + canonicalize.
                if !no_wildcard_shape_prefilter(self.needle, candidate) {
                    return false;
                }
                // Without wildcards the two trees have identical shape,
                // so scoped alpha-canonicalization can stay on the cheap
                // clone-and-compare path.
                if let Some(canonical_needle) = &self.canonical_needle {
                    if alpha_shorthand_sensitive(self.needle)
                        || alpha_shorthand_sensitive(candidate)
                    {
                        return AstWildcardMatcher::new(
                            self.selector,
                            &self.wildcard_idents,
                            self.alpha,
                        )
                        .match_module_item(self.needle, candidate);
                    }
                    let mut candidate = candidate.clone();
                    candidate
                        .visit_mut_with(&mut AlphaIdentCanonicalizer::new(&self.wildcard_idents));
                    return canonical_needle.eq_ignore_span(&candidate);
                }
                return self.needle.eq_ignore_span(candidate);
            }
            // Wildcards present: the structural matcher tracks an identifier
            // bijection for alpha mode (see `AstWildcardMatcher::alpha`), so
            // holes that absorb identifier-bearing subtrees don't desync the
            // identifiers after them — and it walks borrowed trees with no
            // per-comparison clone + canonicalize.
            AstWildcardMatcher::new_with_string_literal_regexes(
                self.selector,
                &self.wildcard_idents,
                &self.string_literal_regexes,
                self.alpha,
            )
            .match_module_item(self.needle, candidate)
        })
    }

    fn var_declarator_alignment(
        &self,
        needle: &VarDecl,
        candidate: &VarDecl,
    ) -> Option<Vec<Option<usize>>> {
        SyntaxContext::within_ignored_ctxt(|| {
            let mut matcher = AstWildcardMatcher::new_with_string_literal_regexes(
                self.selector,
                &self.wildcard_idents,
                &self.string_literal_regexes,
                self.alpha,
            );
            matcher.match_var_declarator_slice_with_alignment(&needle.decls, &candidate.decls)
        })
    }
}

/// Cheap top-level shape check, sound only in **no-wildcard** mode
/// (where matching is structural equality): a `false` return proves
/// the full comparison cannot succeed. Compares the item/statement/
/// declaration discriminants and, for variable declarations, the
/// `var`/`let`/`const` kind and declarator count.
fn no_wildcard_shape_prefilter(needle: &ModuleItem, candidate: &ModuleItem) -> bool {
    fn decl_shape(n: &Decl, c: &Decl) -> bool {
        if std::mem::discriminant(n) != std::mem::discriminant(c) {
            return false;
        }
        match (n, c) {
            (Decl::Var(nv), Decl::Var(cv)) => {
                nv.kind == cv.kind && nv.decls.len() == cv.decls.len()
            }
            _ => true,
        }
    }
    match (needle, candidate) {
        (ModuleItem::Stmt(n), ModuleItem::Stmt(c)) => {
            std::mem::discriminant(n) == std::mem::discriminant(c)
                && match (n, c) {
                    (Stmt::Decl(nd), Stmt::Decl(cd)) => decl_shape(nd, cd),
                    _ => true,
                }
        }
        (ModuleItem::ModuleDecl(n), ModuleItem::ModuleDecl(c)) => {
            std::mem::discriminant(n) == std::mem::discriminant(c)
                && match (n, c) {
                    (ModuleDecl::ExportDecl(ne), ModuleDecl::ExportDecl(ce)) => {
                        decl_shape(&ne.decl, &ce.decl)
                    }
                    _ => true,
                }
        }
        _ => false,
    }
}

fn alpha_shorthand_sensitive(item: &ModuleItem) -> bool {
    let mut visitor = AlphaShorthandSensitiveVisitor::default();
    item.visit_with(&mut visitor);
    visitor.found
}

#[derive(Default)]
struct AlphaShorthandSensitiveVisitor {
    found: bool,
}

impl Visit for AlphaShorthandSensitiveVisitor {
    fn visit_prop(&mut self, prop: &Prop) {
        if self.found {
            return;
        }
        match prop {
            Prop::Shorthand(_) => {
                self.found = true;
            }
            Prop::KeyValue(prop) if key_value_prop_ident_value(prop).is_some() => {
                self.found = true;
            }
            _ => prop.visit_children_with(self),
        }
    }

    fn visit_object_pat_prop(&mut self, prop: &ObjectPatProp) {
        if self.found {
            return;
        }
        match prop {
            ObjectPatProp::Assign(prop) if prop.value.is_none() => {
                self.found = true;
            }
            ObjectPatProp::KeyValue(prop) if key_value_pat_binding_ident_value(prop).is_some() => {
                self.found = true;
            }
            _ => prop.visit_children_with(self),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn selector(match_source: &str) -> AnonymousStatementSelector {
        AnonymousStatementSelector {
            match_source: match_source.to_string(),
            identifiers: SourceMatchIdentifierMode::AlphaAll,
            target_binding: None,
            target_statement: None,
            target_statements: None,
            wildcard_string_literals: BTreeSet::new(),
        }
    }

    fn parse_one(source: &str) -> ModuleItem {
        js_ast::with_swc_globals(|| {
            let mut module = js_ast::parse_js_module_ast("<test>", source).unwrap();
            assert_eq!(module.body.len(), 1);
            module.body.remove(0)
        })
    }

    fn prefilter_for(
        needle: &ModuleItem,
        selector: &AnonymousStatementSelector,
    ) -> VarDeclaratorPrefilter {
        let prepared = PreparedNeedle::new(needle, selector);
        VarDeclaratorPrefilter::new(needle, &prepared)
    }

    fn single_declarator(item: &ModuleItem) -> &VarDeclarator {
        let var = item_var_decl(item).unwrap();
        assert_eq!(var.decls.len(), 1);
        &var.decls[0]
    }

    fn single_declarator_init(item: &ModuleItem) -> &Expr {
        single_declarator(item).init.as_deref().unwrap()
    }

    #[test]
    fn var_declarator_prefilter_uses_string_literal_in_alpha_mode() {
        let selector = selector(r#"const readableName = "stable-css-class";"#);
        let needle = parse_one(&selector.match_source);
        let prefilter = prefilter_for(&needle, &selector);
        let matching = parse_one(r#"const minifiedName = "stable-css-class";"#);
        let non_matching = parse_one(r#"const otherName = "other-css-class";"#);

        assert!(prefilter.declarator_can_match(single_declarator(&matching)));
        assert!(!prefilter.declarator_can_match(single_declarator(&non_matching)));
    }

    #[test]
    fn var_declarator_prefilter_respects_string_literal_wildcards() {
        let mut selector = selector(r#"const readableName = "STRING_HOLE";"#);
        selector
            .wildcard_string_literals
            .insert("STRING_HOLE".to_string());
        let needle = parse_one(&selector.match_source);
        let prefilter = prefilter_for(&needle, &selector);
        let candidate = parse_one(r#"const minifiedName = "runtime-css-class";"#);

        assert!(prefilter.declarator_can_match(single_declarator(&candidate)));
    }

    #[test]
    fn string_literal_regex_predicate_recognizes_only_direct_string_pattern_calls() {
        let selector = parse_one(r#"const readable = STR_LITERAL_MATCHING_RE("^Card-[0-9]+$");"#);
        assert_eq!(
            string_literal_regex_pattern(single_declarator_init(&selector)).as_deref(),
            Some("^Card-[0-9]+$"),
        );

        let non_string_arg = parse_one(r#"const readable = STR_LITERAL_MATCHING_RE(pattern);"#);
        assert!(string_literal_regex_pattern(single_declarator_init(&non_string_arg)).is_none());

        let two_args = parse_one(r#"const readable = STR_LITERAL_MATCHING_RE("Card", "Panel");"#);
        assert!(string_literal_regex_pattern(single_declarator_init(&two_args)).is_none());
    }

    #[test]
    fn string_literal_regex_predicate_matches_string_literal_ast_nodes() {
        let selector = selector(r#"const readable = STR_LITERAL_MATCHING_RE("^Card-[0-9]+$");"#);
        let needle = parse_one(&selector.match_source);
        let prepared = PreparedNeedle::new(&needle, &selector);
        let matching = parse_one(r#"const minified = "Card-42";"#);
        let non_matching = parse_one(r#"const minified = "Panel-42";"#);
        let non_string = parse_one(r#"const minified = makeCardName();"#);

        assert!(!prepared.no_wildcards);
        assert!(prepared.matches(&matching));
        assert!(!prepared.matches(&non_matching));
        assert!(!prepared.matches(&non_string));
    }

    #[test]
    fn declarator_hole_prefilter_uses_regex_string_literal_predicates() {
        let selector = selector(
            r#"const DECLARATORS_BEFORE = null,
  readable = STR_LITERAL_MATCHING_RE("^generic-token-[0-9]+$"),
  DECLARATORS_AFTER = null;"#,
        );
        let needle = parse_one(&selector.match_source);
        let prepared = PreparedNeedle::new(&needle, &selector);
        let prefilter =
            VarDeclWithDeclaratorHolesPrefilter::new(item_var_decl(&needle).unwrap(), &prepared);

        let matching = parse_one(
            r#"const before = "other",
  minified = "generic-token-42",
  after = "other";"#,
        );
        let non_matching = parse_one(
            r#"const before = "other",
  minified = "different-token-42",
  after = "other";"#,
        );

        assert!(prefilter.var_decl_can_match(item_var_decl(&matching).unwrap()));
        assert!(!prefilter.var_decl_can_match(item_var_decl(&non_matching).unwrap()));
    }

    #[test]
    fn declarator_hole_body_index_matching_prefilters_by_literal_predicate() {
        js_ast::with_swc_globals(|| {
            let runtime = js_ast::parse_js_module_ast(
                "<test>",
                r#"const unrelatedA = "generic-token-1";
const before = "other",
  minified = "generic-token-42",
  after = "other";
const unrelatedB = "different-token-42";"#,
            )
            .unwrap();
            let selector = selector(
                r#"const DECLARATORS_BEFORE = null,
  readable = STR_LITERAL_MATCHING_RE("^generic-token-42$"),
  DECLARATORS_AFTER = null;"#,
            );
            let needle = parse_one(&selector.match_source);

            assert_eq!(
                find_matching_body_indices(&runtime, &needle, &selector),
                vec![1]
            );
        });
    }
}

#[derive(Clone, Default)]
struct WildcardReplacements {
    strings: BTreeMap<String, Wtf8Atom>,
    expressions: BTreeMap<String, Expr>,
    statements: BTreeMap<String, Stmt>,
}

#[derive(Clone, Default)]
struct AlphaMatchScope {
    forward: BTreeMap<Atom, Atom>,
    backward: BTreeMap<Atom, Atom>,
}

struct AstWildcardMatcher<'a> {
    selector: &'a AnonymousStatementSelector,
    wildcard_idents: &'a WildcardIdents,
    string_literal_regexes: Option<&'a CompiledStringLiteralRegexes>,
    replacements: WildcardReplacements,
    /// Whether value/binding identifiers are alpha-renamable. When true,
    /// identifier equality is tracked as a bijection built incrementally
    /// at structurally-corresponding positions, instead of pre-renaming
    /// both trees. Because holes accept (and never recurse into) the
    /// subtrees they absorb, absorbed identifiers never enter the
    /// bijection — so a hole no longer desyncs the numbering of the nodes
    /// after it, and there is no per-comparison clone + canonicalize.
    alpha: bool,
    alpha_scopes: Vec<AlphaMatchScope>,
}

/// A clone of the matcher's mutable binding state, captured before a
/// tentative segment placement during ordered-subsequence (multi-hole)
/// list matching and restored when that placement fails — so a
/// half-applied segment never leaks identifier or wildcard bindings into
/// the next attempt.
#[derive(Clone)]
struct MatcherState {
    replacements: WildcardReplacements,
    alpha_scopes: Vec<AlphaMatchScope>,
}

/// Loop-invariant inputs for the recursive ordered-subsequence search in
/// [`AstWildcardMatcher::match_list_with_holes`]. Only the `seg_idx` and
/// `cand_min` cursor arguments change as the search descends.
struct SegmentSearch<'a, T> {
    needle: &'a [T],
    candidate: &'a [T],
    /// `(needle_start, len)` of each maximal fixed (non-hole) run, in
    /// source order.
    segments: &'a [(usize, usize)],
    /// Whether the first segment is pinned to the candidate's start
    /// (true unless a hole leads the needle list).
    anchored_left: bool,
    /// Whether the last segment is pinned to the candidate's end (true
    /// unless a hole trails the needle list).
    anchored_right: bool,
}

impl<'a> AstWildcardMatcher<'a> {
    fn new(
        selector: &'a AnonymousStatementSelector,
        wildcard_idents: &'a WildcardIdents,
        alpha: bool,
    ) -> Self {
        Self::new_impl(selector, wildcard_idents, None, alpha)
    }

    fn new_with_string_literal_regexes(
        selector: &'a AnonymousStatementSelector,
        wildcard_idents: &'a WildcardIdents,
        string_literal_regexes: &'a CompiledStringLiteralRegexes,
        alpha: bool,
    ) -> Self {
        Self::new_impl(
            selector,
            wildcard_idents,
            Some(string_literal_regexes),
            alpha,
        )
    }

    fn new_impl(
        selector: &'a AnonymousStatementSelector,
        wildcard_idents: &'a WildcardIdents,
        string_literal_regexes: Option<&'a CompiledStringLiteralRegexes>,
        alpha: bool,
    ) -> Self {
        Self {
            selector,
            wildcard_idents,
            string_literal_regexes,
            replacements: WildcardReplacements::default(),
            alpha,
            alpha_scopes: vec![AlphaMatchScope::default()],
        }
    }

    fn with_alpha_scope(&mut self, f: impl FnOnce(&mut Self) -> bool) -> bool {
        if !self.alpha {
            return f(self);
        }
        self.alpha_scopes.push(AlphaMatchScope::default());
        let ok = f(self);
        self.alpha_scopes.pop();
        ok
    }

    /// Match two identifier references. In alpha mode, references first
    /// consult the visible lexical scope stack, then create a mapping in
    /// the current scope if neither side is known yet.
    fn match_sym(&mut self, needle: &Atom, candidate: &Atom) -> bool {
        if !self.alpha {
            return needle == candidate;
        }

        for scope in self.alpha_scopes.iter().rev() {
            if let Some(mapped) = scope.forward.get(needle) {
                return mapped == candidate;
            }
            if scope.backward.contains_key(candidate) {
                return false;
            }
        }
        self.bind_alpha_sym_in_current_scope(needle, candidate)
    }

    /// Match two binding identifiers. Unlike references, a binding is
    /// allowed to shadow an outer binding with the same spelling, so only
    /// the current lexical frame is consulted before creating the pair.
    fn match_binding_sym(&mut self, needle: &Atom, candidate: &Atom) -> bool {
        if !self.alpha {
            return needle == candidate;
        }
        self.bind_alpha_sym_in_current_scope(needle, candidate)
    }

    fn bind_alpha_sym_in_current_scope(&mut self, needle: &Atom, candidate: &Atom) -> bool {
        let scope = self
            .alpha_scopes
            .last_mut()
            .expect("alpha matcher always has a root scope");
        match (scope.forward.get(needle), scope.backward.get(candidate)) {
            (Some(mapped), _) => mapped == candidate,
            (None, Some(_)) => false,
            (None, None) => {
                scope.forward.insert(needle.clone(), candidate.clone());
                scope.backward.insert(candidate.clone(), needle.clone());
                true
            }
        }
    }

    fn match_ident(&mut self, needle: &Ident, candidate: &Ident) -> bool {
        needle.optional == candidate.optional && self.match_sym(&needle.sym, &candidate.sym)
    }

    fn match_binding_ident(&mut self, needle: &Ident, candidate: &Ident) -> bool {
        needle.optional == candidate.optional && self.match_binding_sym(&needle.sym, &candidate.sym)
    }

    fn match_binding_binding_ident(
        &mut self,
        needle: &BindingIdent,
        candidate: &BindingIdent,
    ) -> bool {
        needle.type_ann.eq_ignore_span(&candidate.type_ann)
            && self.match_binding_ident(&needle.id, &candidate.id)
    }

    fn match_binding_ident_as_ref(
        &mut self,
        needle: &BindingIdent,
        candidate: &BindingIdent,
    ) -> bool {
        needle.type_ann.eq_ignore_span(&candidate.type_ann)
            && self.match_ident(&needle.id, &candidate.id)
    }

    fn match_opt_ident(&mut self, needle: &Option<Ident>, candidate: &Option<Ident>) -> bool {
        match (needle, candidate) {
            (Some(needle), Some(candidate)) => self.match_ident(needle, candidate),
            (None, None) => true,
            _ => false,
        }
    }

    fn match_opt_binding_ident(
        &mut self,
        needle: &Option<Ident>,
        candidate: &Option<Ident>,
    ) -> bool {
        match (needle, candidate) {
            (Some(needle), Some(candidate)) => self.match_binding_ident(needle, candidate),
            (None, None) => true,
            _ => false,
        }
    }

    fn bind_string(&mut self, wildcard: &str, candidate_value: &Wtf8Atom) -> bool {
        match self.replacements.strings.get(wildcard) {
            Some(existing) => existing == candidate_value,
            None => {
                self.replacements
                    .strings
                    .insert(wildcard.to_string(), candidate_value.clone());
                true
            }
        }
    }

    fn bind_expr(&mut self, wildcard: &str, candidate: &Expr) -> bool {
        // The bare keyword `EXPR` is an anonymous wildcard: every
        // occurrence matches independently, so authors don't have to mint
        // a unique name per placeholder. Named holes (`EXPR_FOO`) keep
        // their cross-occurrence equality.
        if hole_is_anonymous(wildcard, EXPR_HOLE_KEYWORD) {
            return true;
        }
        match self.replacements.expressions.get(wildcard) {
            Some(existing) => existing.eq_ignore_span(candidate),
            None => {
                self.replacements
                    .expressions
                    .insert(wildcard.to_string(), candidate.clone());
                true
            }
        }
    }

    fn bind_stmt(&mut self, wildcard: &str, candidate: &Stmt) -> bool {
        // Bare `STMT` is anonymous; see [`Self::bind_expr`].
        if hole_is_anonymous(wildcard, STMT_HOLE_KEYWORD) {
            return true;
        }
        match self.replacements.statements.get(wildcard) {
            Some(existing) => existing.eq_ignore_span(candidate),
            None => {
                self.replacements
                    .statements
                    .insert(wildcard.to_string(), candidate.clone());
                true
            }
        }
    }

    fn match_module_item(&mut self, needle: &ModuleItem, candidate: &ModuleItem) -> bool {
        match (needle, candidate) {
            (ModuleItem::Stmt(needle), ModuleItem::Stmt(candidate)) => {
                self.match_stmt(needle, candidate)
            }
            (ModuleItem::ModuleDecl(needle), ModuleItem::ModuleDecl(candidate)) => {
                self.match_module_decl(needle, candidate)
            }
            _ => needle.eq_ignore_span(candidate),
        }
    }

    fn match_module_decl(&mut self, needle: &ModuleDecl, candidate: &ModuleDecl) -> bool {
        match (needle, candidate) {
            (ModuleDecl::Import(needle), ModuleDecl::Import(candidate)) => {
                needle.specifiers.eq_ignore_span(&candidate.specifiers)
                    && needle.type_only == candidate.type_only
                    && needle.phase.eq_ignore_span(&candidate.phase)
                    && needle.with.eq_ignore_span(&candidate.with)
                    && self.match_str(&needle.src, &candidate.src)
            }
            (ModuleDecl::ExportDecl(needle), ModuleDecl::ExportDecl(candidate)) => {
                self.match_decl(&needle.decl, &candidate.decl)
            }
            (ModuleDecl::ExportDefaultDecl(needle), ModuleDecl::ExportDefaultDecl(candidate)) => {
                self.match_default_decl(&needle.decl, &candidate.decl)
            }
            (ModuleDecl::ExportDefaultExpr(needle), ModuleDecl::ExportDefaultExpr(candidate)) => {
                self.match_expr(&needle.expr, &candidate.expr)
            }
            (ModuleDecl::ExportAll(needle), ModuleDecl::ExportAll(candidate)) => {
                needle.type_only == candidate.type_only
                    && needle.with.eq_ignore_span(&candidate.with)
                    && self.match_str(&needle.src, &candidate.src)
            }
            (ModuleDecl::ExportNamed(needle), ModuleDecl::ExportNamed(candidate)) => {
                needle.specifiers.eq_ignore_span(&candidate.specifiers)
                    && needle.type_only == candidate.type_only
                    && needle.with.eq_ignore_span(&candidate.with)
                    && self.match_option_box_str(&needle.src, &candidate.src)
            }
            (ModuleDecl::TsExportAssignment(needle), ModuleDecl::TsExportAssignment(candidate)) => {
                self.match_expr(&needle.expr, &candidate.expr)
            }
            _ => needle.eq_ignore_span(candidate),
        }
    }

    fn match_decl(&mut self, needle: &Decl, candidate: &Decl) -> bool {
        match (needle, candidate) {
            (Decl::Var(needle), Decl::Var(candidate)) => self.match_var_decl(needle, candidate),
            (Decl::Fn(needle), Decl::Fn(candidate)) => {
                self.match_binding_ident(&needle.ident, &candidate.ident)
                    && needle.declare == candidate.declare
                    && self.match_function(&needle.function, &candidate.function)
            }
            (Decl::Class(needle), Decl::Class(candidate)) => {
                self.match_binding_ident(&needle.ident, &candidate.ident)
                    && needle.declare == candidate.declare
                    && self.match_class(&needle.class, &candidate.class)
            }
            (Decl::Using(needle), Decl::Using(candidate)) => {
                self.match_using_decl(needle, candidate)
            }
            _ => needle.eq_ignore_span(candidate),
        }
    }

    fn match_default_decl(&mut self, needle: &DefaultDecl, candidate: &DefaultDecl) -> bool {
        match (needle, candidate) {
            (DefaultDecl::Class(needle), DefaultDecl::Class(candidate)) => {
                self.match_opt_binding_ident(&needle.ident, &candidate.ident)
                    && self.match_class(&needle.class, &candidate.class)
            }
            (DefaultDecl::Fn(needle), DefaultDecl::Fn(candidate)) => {
                self.match_opt_binding_ident(&needle.ident, &candidate.ident)
                    && self.match_function(&needle.function, &candidate.function)
            }
            _ => needle.eq_ignore_span(candidate),
        }
    }

    fn match_function(&mut self, needle: &Function, candidate: &Function) -> bool {
        self.match_slice(
            &needle.decorators,
            &candidate.decorators,
            Self::match_decorator,
        ) && needle.is_generator == candidate.is_generator
            && needle.is_async == candidate.is_async
            && needle.type_params.eq_ignore_span(&candidate.type_params)
            && needle.return_type.eq_ignore_span(&candidate.return_type)
            && self.with_alpha_scope(|matcher| {
                matcher.match_slice(&needle.params, &candidate.params, Self::match_param)
                    && matcher.match_option_block_stmt(&needle.body, &candidate.body)
            })
    }

    fn match_param(&mut self, needle: &Param, candidate: &Param) -> bool {
        self.match_slice(
            &needle.decorators,
            &candidate.decorators,
            Self::match_decorator,
        ) && self.match_pat(&needle.pat, &candidate.pat)
    }

    fn match_var_decl(&mut self, needle: &VarDecl, candidate: &VarDecl) -> bool {
        needle.kind == candidate.kind
            && needle.declare == candidate.declare
            && self
                .match_var_declarator_slice_with_alignment(&needle.decls, &candidate.decls)
                .is_some()
    }

    fn match_using_decl(&mut self, needle: &UsingDecl, candidate: &UsingDecl) -> bool {
        needle.is_await == candidate.is_await
            && self.match_slice(&needle.decls, &candidate.decls, Self::match_var_declarator)
    }

    fn match_var_declarator(&mut self, needle: &VarDeclarator, candidate: &VarDeclarator) -> bool {
        needle.definite == candidate.definite
            && self.match_pat(&needle.name, &candidate.name)
            && self.match_option_box_expr(&needle.init, &candidate.init)
    }

    fn match_stmt(&mut self, needle: &Stmt, candidate: &Stmt) -> bool {
        if let Some(hole_name) = statement_hole_name(needle)
            && self.wildcard_idents.statements.contains(hole_name)
        {
            return self.bind_stmt(hole_name, candidate);
        }
        match (needle, candidate) {
            (Stmt::Block(needle), Stmt::Block(candidate)) => {
                self.match_block_stmt(needle, candidate)
            }
            (Stmt::With(needle), Stmt::With(candidate)) => {
                self.match_expr(&needle.obj, &candidate.obj)
                    && self.match_stmt(&needle.body, &candidate.body)
            }
            (Stmt::Return(needle), Stmt::Return(candidate)) => {
                self.match_option_box_expr(&needle.arg, &candidate.arg)
            }
            (Stmt::Labeled(needle), Stmt::Labeled(candidate)) => {
                needle.label.eq_ignore_span(&candidate.label)
                    && self.match_stmt(&needle.body, &candidate.body)
            }
            (Stmt::If(needle), Stmt::If(candidate)) => {
                self.match_expr(&needle.test, &candidate.test)
                    && self.match_stmt(&needle.cons, &candidate.cons)
                    && self.match_option_box_stmt(&needle.alt, &candidate.alt)
            }
            (Stmt::Switch(needle), Stmt::Switch(candidate)) => {
                self.match_expr(&needle.discriminant, &candidate.discriminant)
                    && self.match_slice(&needle.cases, &candidate.cases, Self::match_switch_case)
            }
            (Stmt::Throw(needle), Stmt::Throw(candidate)) => {
                self.match_expr(&needle.arg, &candidate.arg)
            }
            (Stmt::Try(needle), Stmt::Try(candidate)) => {
                self.match_block_stmt(&needle.block, &candidate.block)
                    && self.match_option_catch_clause(&needle.handler, &candidate.handler)
                    && self.match_option_block_stmt(&needle.finalizer, &candidate.finalizer)
            }
            (Stmt::While(needle), Stmt::While(candidate)) => {
                self.match_expr(&needle.test, &candidate.test)
                    && self.match_stmt(&needle.body, &candidate.body)
            }
            (Stmt::DoWhile(needle), Stmt::DoWhile(candidate)) => {
                self.match_expr(&needle.test, &candidate.test)
                    && self.match_stmt(&needle.body, &candidate.body)
            }
            (Stmt::For(needle), Stmt::For(candidate)) => {
                self.match_option_var_decl_or_expr(&needle.init, &candidate.init)
                    && self.match_option_box_expr(&needle.test, &candidate.test)
                    && self.match_option_box_expr(&needle.update, &candidate.update)
                    && self.match_stmt(&needle.body, &candidate.body)
            }
            (Stmt::ForIn(needle), Stmt::ForIn(candidate)) => {
                self.match_for_head(&needle.left, &candidate.left)
                    && self.match_expr(&needle.right, &candidate.right)
                    && self.match_stmt(&needle.body, &candidate.body)
            }
            (Stmt::ForOf(needle), Stmt::ForOf(candidate)) => {
                needle.is_await == candidate.is_await
                    && self.match_for_head(&needle.left, &candidate.left)
                    && self.match_expr(&needle.right, &candidate.right)
                    && self.match_stmt(&needle.body, &candidate.body)
            }
            (Stmt::Decl(needle), Stmt::Decl(candidate)) => self.match_decl(needle, candidate),
            (Stmt::Expr(needle), Stmt::Expr(candidate)) => {
                self.match_expr(&needle.expr, &candidate.expr)
            }
            _ => needle.eq_ignore_span(candidate),
        }
    }

    fn match_expr(&mut self, needle: &Expr, candidate: &Expr) -> bool {
        if let Some(pattern) = string_literal_regex_pattern(needle) {
            return match candidate {
                Expr::Lit(Lit::Str(candidate)) => self.string_literal_regexes.map_or_else(
                    || string_literal_matches_regex(&pattern, &candidate.value),
                    |regexes| regexes.matches(&pattern, &candidate.value),
                ),
                _ => false,
            };
        }
        if let Some(hole_name) = expression_hole_name(needle)
            && self.wildcard_idents.expressions.contains(hole_name)
        {
            return self.bind_expr(hole_name, candidate);
        }
        match (needle, candidate) {
            (Expr::Array(needle), Expr::Array(candidate)) => self.match_slice(
                &needle.elems,
                &candidate.elems,
                Self::match_option_expr_or_spread,
            ),
            (Expr::Object(needle), Expr::Object(candidate)) => {
                self.match_slice(&needle.props, &candidate.props, Self::match_prop_or_spread)
            }
            (Expr::Ident(needle), Expr::Ident(candidate)) => self.match_ident(needle, candidate),
            (Expr::Fn(needle), Expr::Fn(candidate)) => {
                self.match_opt_ident(&needle.ident, &candidate.ident)
                    && self.match_function(&needle.function, &candidate.function)
            }
            (Expr::Class(needle), Expr::Class(candidate)) => {
                self.match_opt_ident(&needle.ident, &candidate.ident)
                    && self.match_class(&needle.class, &candidate.class)
            }
            (Expr::Unary(needle), Expr::Unary(candidate)) => {
                needle.op == candidate.op && self.match_expr(&needle.arg, &candidate.arg)
            }
            (Expr::Update(needle), Expr::Update(candidate)) => {
                needle.op == candidate.op
                    && needle.prefix == candidate.prefix
                    && self.match_expr(&needle.arg, &candidate.arg)
            }
            (Expr::Bin(needle), Expr::Bin(candidate)) => {
                needle.op == candidate.op
                    && self.match_expr(&needle.left, &candidate.left)
                    && self.match_expr(&needle.right, &candidate.right)
            }
            (Expr::Assign(needle), Expr::Assign(candidate)) => {
                needle.op == candidate.op
                    && self.match_assign_target(&needle.left, &candidate.left)
                    && self.match_expr(&needle.right, &candidate.right)
            }
            (Expr::Member(needle), Expr::Member(candidate)) => {
                self.match_member_expr(needle, candidate)
            }
            (Expr::SuperProp(needle), Expr::SuperProp(candidate)) => {
                self.match_super_prop(&needle.prop, &candidate.prop)
            }
            (Expr::Cond(needle), Expr::Cond(candidate)) => {
                self.match_expr(&needle.test, &candidate.test)
                    && self.match_expr(&needle.cons, &candidate.cons)
                    && self.match_expr(&needle.alt, &candidate.alt)
            }
            (Expr::Call(needle), Expr::Call(candidate)) => self.match_call_expr(needle, candidate),
            (Expr::New(needle), Expr::New(candidate)) => {
                needle.type_args.eq_ignore_span(&candidate.type_args)
                    && self.match_expr(&needle.callee, &candidate.callee)
                    && self.match_option_expr_or_spread_vec(&needle.args, &candidate.args)
            }
            (Expr::Seq(needle), Expr::Seq(candidate)) => self.match_slice(
                &needle.exprs,
                &candidate.exprs,
                |matcher, needle, candidate| matcher.match_expr(needle, candidate),
            ),
            (Expr::Lit(needle), Expr::Lit(candidate)) => self.match_lit(needle, candidate),
            (Expr::Tpl(needle), Expr::Tpl(candidate)) => {
                needle.quasis.eq_ignore_span(&candidate.quasis)
                    && self.match_slice(
                        &needle.exprs,
                        &candidate.exprs,
                        |matcher, needle, candidate| matcher.match_expr(needle, candidate),
                    )
            }
            (Expr::TaggedTpl(needle), Expr::TaggedTpl(candidate)) => {
                needle.type_params.eq_ignore_span(&candidate.type_params)
                    && self.match_expr(&needle.tag, &candidate.tag)
                    && self.match_tpl(&needle.tpl, &candidate.tpl)
            }
            (Expr::Arrow(needle), Expr::Arrow(candidate)) => {
                needle.is_async == candidate.is_async
                    && needle.is_generator == candidate.is_generator
                    && needle.type_params.eq_ignore_span(&candidate.type_params)
                    && needle.return_type.eq_ignore_span(&candidate.return_type)
                    && self.with_alpha_scope(|matcher| {
                        matcher.match_slice(&needle.params, &candidate.params, Self::match_pat)
                            && matcher.match_block_stmt_or_expr(&needle.body, &candidate.body)
                    })
            }
            (Expr::Yield(needle), Expr::Yield(candidate)) => {
                needle.delegate == candidate.delegate
                    && self.match_option_box_expr(&needle.arg, &candidate.arg)
            }
            (Expr::Await(needle), Expr::Await(candidate)) => {
                self.match_expr(&needle.arg, &candidate.arg)
            }
            (Expr::Paren(needle), Expr::Paren(candidate)) => {
                self.match_expr(&needle.expr, &candidate.expr)
            }
            (Expr::JSXElement(needle), Expr::JSXElement(candidate)) => {
                self.match_jsx_element(needle, candidate)
            }
            (Expr::JSXFragment(needle), Expr::JSXFragment(candidate)) => {
                self.match_jsx_fragment(needle, candidate)
            }
            (Expr::TsConstAssertion(needle), Expr::TsConstAssertion(candidate)) => {
                self.match_expr(&needle.expr, &candidate.expr)
            }
            (Expr::TsNonNull(needle), Expr::TsNonNull(candidate)) => {
                self.match_expr(&needle.expr, &candidate.expr)
            }
            (Expr::TsAs(needle), Expr::TsAs(candidate)) => {
                needle.type_ann.eq_ignore_span(&candidate.type_ann)
                    && self.match_expr(&needle.expr, &candidate.expr)
            }
            (Expr::TsSatisfies(needle), Expr::TsSatisfies(candidate)) => {
                needle.type_ann.eq_ignore_span(&candidate.type_ann)
                    && self.match_expr(&needle.expr, &candidate.expr)
            }
            (Expr::TsTypeAssertion(needle), Expr::TsTypeAssertion(candidate)) => {
                needle.type_ann.eq_ignore_span(&candidate.type_ann)
                    && self.match_expr(&needle.expr, &candidate.expr)
            }
            (Expr::TsInstantiation(needle), Expr::TsInstantiation(candidate)) => {
                needle.type_args.eq_ignore_span(&candidate.type_args)
                    && self.match_expr(&needle.expr, &candidate.expr)
            }
            (Expr::OptChain(needle), Expr::OptChain(candidate)) => {
                needle.optional == candidate.optional
                    && self.match_opt_chain_base(&needle.base, &candidate.base)
            }
            _ => needle.eq_ignore_span(candidate),
        }
    }

    fn match_jsx_element(&mut self, needle: &JSXElement, candidate: &JSXElement) -> bool {
        self.match_jsx_opening_element(&needle.opening, &candidate.opening)
            && self.match_slice(
                &needle.children,
                &candidate.children,
                Self::match_jsx_element_child,
            )
            && self.match_option_jsx_closing_element(&needle.closing, &candidate.closing)
    }

    fn match_jsx_opening_element(
        &mut self,
        needle: &JSXOpeningElement,
        candidate: &JSXOpeningElement,
    ) -> bool {
        needle.name.eq_ignore_span(&candidate.name)
            && needle.self_closing == candidate.self_closing
            && needle.type_args.eq_ignore_span(&candidate.type_args)
            && self.match_slice(
                &needle.attrs,
                &candidate.attrs,
                Self::match_jsx_attr_or_spread,
            )
    }

    fn match_jsx_attr_or_spread(
        &mut self,
        needle: &JSXAttrOrSpread,
        candidate: &JSXAttrOrSpread,
    ) -> bool {
        match (needle, candidate) {
            (JSXAttrOrSpread::JSXAttr(needle), JSXAttrOrSpread::JSXAttr(candidate)) => {
                self.match_jsx_attr(needle, candidate)
            }
            (JSXAttrOrSpread::SpreadElement(needle), JSXAttrOrSpread::SpreadElement(candidate)) => {
                self.match_expr(&needle.expr, &candidate.expr)
            }
            _ => false,
        }
    }

    fn match_jsx_attr(&mut self, needle: &JSXAttr, candidate: &JSXAttr) -> bool {
        needle.name.eq_ignore_span(&candidate.name)
            && self.match_option_jsx_attr_value(&needle.value, &candidate.value)
    }

    fn match_option_jsx_attr_value(
        &mut self,
        needle: &Option<JSXAttrValue>,
        candidate: &Option<JSXAttrValue>,
    ) -> bool {
        match (needle, candidate) {
            (Some(needle), Some(candidate)) => self.match_jsx_attr_value(needle, candidate),
            (None, None) => true,
            _ => false,
        }
    }

    fn match_jsx_attr_value(&mut self, needle: &JSXAttrValue, candidate: &JSXAttrValue) -> bool {
        match (needle, candidate) {
            (JSXAttrValue::Str(needle), JSXAttrValue::Str(candidate)) => {
                self.match_str(needle, candidate)
            }
            (JSXAttrValue::JSXExprContainer(needle), JSXAttrValue::JSXExprContainer(candidate)) => {
                self.match_jsx_expr_container(needle, candidate)
            }
            (JSXAttrValue::JSXElement(needle), JSXAttrValue::JSXElement(candidate)) => {
                self.match_jsx_element(needle, candidate)
            }
            (JSXAttrValue::JSXFragment(needle), JSXAttrValue::JSXFragment(candidate)) => {
                self.match_jsx_fragment(needle, candidate)
            }
            _ => false,
        }
    }

    fn match_jsx_expr_container(
        &mut self,
        needle: &JSXExprContainer,
        candidate: &JSXExprContainer,
    ) -> bool {
        self.match_jsx_expr(&needle.expr, &candidate.expr)
    }

    fn match_jsx_expr(&mut self, needle: &JSXExpr, candidate: &JSXExpr) -> bool {
        match (needle, candidate) {
            (JSXExpr::JSXEmptyExpr(needle), JSXExpr::JSXEmptyExpr(candidate)) => {
                needle.eq_ignore_span(candidate)
            }
            (JSXExpr::Expr(needle), JSXExpr::Expr(candidate)) => self.match_expr(needle, candidate),
            _ => false,
        }
    }

    fn match_jsx_element_child(
        &mut self,
        needle: &JSXElementChild,
        candidate: &JSXElementChild,
    ) -> bool {
        match (needle, candidate) {
            (JSXElementChild::JSXText(needle), JSXElementChild::JSXText(candidate)) => {
                needle.eq_ignore_span(candidate)
            }
            (
                JSXElementChild::JSXExprContainer(needle),
                JSXElementChild::JSXExprContainer(candidate),
            ) => self.match_jsx_expr_container(needle, candidate),
            (
                JSXElementChild::JSXSpreadChild(needle),
                JSXElementChild::JSXSpreadChild(candidate),
            ) => self.match_expr(&needle.expr, &candidate.expr),
            (JSXElementChild::JSXElement(needle), JSXElementChild::JSXElement(candidate)) => {
                self.match_jsx_element(needle, candidate)
            }
            (JSXElementChild::JSXFragment(needle), JSXElementChild::JSXFragment(candidate)) => {
                self.match_jsx_fragment(needle, candidate)
            }
            _ => false,
        }
    }

    fn match_jsx_fragment(&mut self, needle: &JSXFragment, candidate: &JSXFragment) -> bool {
        needle.opening.eq_ignore_span(&candidate.opening)
            && needle.closing.eq_ignore_span(&candidate.closing)
            && self.match_slice(
                &needle.children,
                &candidate.children,
                Self::match_jsx_element_child,
            )
    }

    fn match_option_jsx_closing_element(
        &mut self,
        needle: &Option<JSXClosingElement>,
        candidate: &Option<JSXClosingElement>,
    ) -> bool {
        match (needle, candidate) {
            (Some(needle), Some(candidate)) => needle.eq_ignore_span(candidate),
            (None, None) => true,
            _ => false,
        }
    }

    fn match_block_stmt(&mut self, needle: &BlockStmt, candidate: &BlockStmt) -> bool {
        self.match_stmt_slice(&needle.stmts, &candidate.stmts)
    }

    fn match_switch_case(&mut self, needle: &SwitchCase, candidate: &SwitchCase) -> bool {
        self.match_option_box_expr(&needle.test, &candidate.test)
            && self.match_stmt_slice(&needle.cons, &candidate.cons)
    }

    fn match_catch_clause(&mut self, needle: &CatchClause, candidate: &CatchClause) -> bool {
        self.with_alpha_scope(|matcher| {
            matcher.match_option_pat(&needle.param, &candidate.param)
                && matcher.match_block_stmt(&needle.body, &candidate.body)
        })
    }

    fn match_var_decl_or_expr(
        &mut self,
        needle: &VarDeclOrExpr,
        candidate: &VarDeclOrExpr,
    ) -> bool {
        match (needle, candidate) {
            (VarDeclOrExpr::VarDecl(needle), VarDeclOrExpr::VarDecl(candidate)) => {
                self.match_var_decl(needle, candidate)
            }
            (VarDeclOrExpr::Expr(needle), VarDeclOrExpr::Expr(candidate)) => {
                self.match_expr(needle, candidate)
            }
            _ => false,
        }
    }

    fn match_for_head(&mut self, needle: &ForHead, candidate: &ForHead) -> bool {
        match (needle, candidate) {
            (ForHead::VarDecl(needle), ForHead::VarDecl(candidate)) => {
                self.match_var_decl(needle, candidate)
            }
            (ForHead::Pat(needle), ForHead::Pat(candidate)) => {
                self.match_ref_pat(needle, candidate)
            }
            _ => needle.eq_ignore_span(candidate),
        }
    }

    fn match_pat(&mut self, needle: &Pat, candidate: &Pat) -> bool {
        match (needle, candidate) {
            (Pat::Array(needle), Pat::Array(candidate)) => {
                needle.optional == candidate.optional
                    && needle.type_ann.eq_ignore_span(&candidate.type_ann)
                    && self.match_slice(&needle.elems, &candidate.elems, Self::match_option_pat)
            }
            (Pat::Object(needle), Pat::Object(candidate)) => {
                needle.optional == candidate.optional
                    && needle.type_ann.eq_ignore_span(&candidate.type_ann)
                    && self.match_slice(
                        &needle.props,
                        &candidate.props,
                        Self::match_object_pat_prop,
                    )
            }
            (Pat::Assign(needle), Pat::Assign(candidate)) => {
                self.match_assign_pat(needle, candidate)
            }
            (Pat::Rest(needle), Pat::Rest(candidate)) => {
                needle.type_ann.eq_ignore_span(&candidate.type_ann)
                    && self.match_pat(&needle.arg, &candidate.arg)
            }
            (Pat::Expr(needle), Pat::Expr(candidate)) => self.match_expr(needle, candidate),
            (Pat::Ident(needle), Pat::Ident(candidate)) => {
                self.match_binding_binding_ident(needle, candidate)
            }
            _ => needle.eq_ignore_span(candidate),
        }
    }

    fn match_assign_pat(&mut self, needle: &AssignPat, candidate: &AssignPat) -> bool {
        self.match_pat(&needle.left, &candidate.left)
            && self.match_expr(&needle.right, &candidate.right)
    }

    fn match_object_pat_prop(&mut self, needle: &ObjectPatProp, candidate: &ObjectPatProp) -> bool {
        match (needle, candidate) {
            (ObjectPatProp::KeyValue(needle), ObjectPatProp::KeyValue(candidate)) => {
                self.match_prop_name_exact(&needle.key, &candidate.key)
                    && self.match_pat(&needle.value, &candidate.value)
            }
            (ObjectPatProp::KeyValue(needle), ObjectPatProp::Assign(candidate)) => {
                self.match_key_value_pat_against_assign_pat(needle, candidate)
            }
            (ObjectPatProp::Assign(needle), ObjectPatProp::KeyValue(candidate)) => {
                self.match_assign_pat_against_key_value_pat(needle, candidate)
            }
            (ObjectPatProp::Assign(needle), ObjectPatProp::Assign(candidate)) => {
                self.match_binding_binding_ident(&needle.key, &candidate.key)
                    && self.match_option_box_expr(&needle.value, &candidate.value)
            }
            (ObjectPatProp::Rest(needle), ObjectPatProp::Rest(candidate)) => {
                needle.type_ann.eq_ignore_span(&candidate.type_ann)
                    && self.match_pat(&needle.arg, &candidate.arg)
            }
            _ => false,
        }
    }

    fn match_ref_pat(&mut self, needle: &Pat, candidate: &Pat) -> bool {
        match (needle, candidate) {
            (Pat::Array(needle), Pat::Array(candidate)) => {
                needle.optional == candidate.optional
                    && needle.type_ann.eq_ignore_span(&candidate.type_ann)
                    && self.match_slice(&needle.elems, &candidate.elems, Self::match_option_ref_pat)
            }
            (Pat::Object(needle), Pat::Object(candidate)) => {
                needle.optional == candidate.optional
                    && needle.type_ann.eq_ignore_span(&candidate.type_ann)
                    && self.match_slice(
                        &needle.props,
                        &candidate.props,
                        Self::match_ref_object_pat_prop,
                    )
            }
            (Pat::Assign(needle), Pat::Assign(candidate)) => {
                self.match_ref_assign_pat(needle, candidate)
            }
            (Pat::Rest(needle), Pat::Rest(candidate)) => {
                needle.type_ann.eq_ignore_span(&candidate.type_ann)
                    && self.match_ref_pat(&needle.arg, &candidate.arg)
            }
            (Pat::Expr(needle), Pat::Expr(candidate)) => self.match_expr(needle, candidate),
            (Pat::Ident(needle), Pat::Ident(candidate)) => {
                self.match_binding_ident_as_ref(needle, candidate)
            }
            _ => needle.eq_ignore_span(candidate),
        }
    }

    fn match_option_ref_pat(&mut self, needle: &Option<Pat>, candidate: &Option<Pat>) -> bool {
        match (needle, candidate) {
            (Some(needle), Some(candidate)) => self.match_ref_pat(needle, candidate),
            (None, None) => true,
            _ => false,
        }
    }

    fn match_ref_assign_pat(&mut self, needle: &AssignPat, candidate: &AssignPat) -> bool {
        self.match_ref_pat(&needle.left, &candidate.left)
            && self.match_expr(&needle.right, &candidate.right)
    }

    fn match_ref_object_pat_prop(
        &mut self,
        needle: &ObjectPatProp,
        candidate: &ObjectPatProp,
    ) -> bool {
        match (needle, candidate) {
            (ObjectPatProp::KeyValue(needle), ObjectPatProp::KeyValue(candidate)) => {
                self.match_prop_name_exact(&needle.key, &candidate.key)
                    && self.match_ref_pat(&needle.value, &candidate.value)
            }
            (ObjectPatProp::KeyValue(needle), ObjectPatProp::Assign(candidate)) => {
                self.match_key_value_ref_pat_against_assign_pat(needle, candidate)
            }
            (ObjectPatProp::Assign(needle), ObjectPatProp::KeyValue(candidate)) => {
                self.match_assign_pat_against_key_value_ref_pat(needle, candidate)
            }
            (ObjectPatProp::Assign(needle), ObjectPatProp::Assign(candidate)) => {
                self.match_binding_ident_as_ref(&needle.key, &candidate.key)
                    && self.match_option_box_expr(&needle.value, &candidate.value)
            }
            (ObjectPatProp::Rest(needle), ObjectPatProp::Rest(candidate)) => {
                needle.type_ann.eq_ignore_span(&candidate.type_ann)
                    && self.match_ref_pat(&needle.arg, &candidate.arg)
            }
            _ => false,
        }
    }

    fn match_member_expr(&mut self, needle: &MemberExpr, candidate: &MemberExpr) -> bool {
        self.match_expr(&needle.obj, &candidate.obj)
            && self.match_member_prop(&needle.prop, &candidate.prop)
    }

    fn match_member_prop(&mut self, needle: &MemberProp, candidate: &MemberProp) -> bool {
        match (needle, candidate) {
            (MemberProp::Ident(needle), MemberProp::Ident(candidate)) => {
                needle.eq_ignore_span(candidate)
            }
            (MemberProp::PrivateName(needle), MemberProp::PrivateName(candidate)) => {
                needle.eq_ignore_span(candidate)
            }
            (MemberProp::Computed(needle), MemberProp::Computed(candidate)) => {
                self.match_expr(&needle.expr, &candidate.expr)
            }
            _ => false,
        }
    }

    fn match_super_prop(&mut self, needle: &SuperProp, candidate: &SuperProp) -> bool {
        match (needle, candidate) {
            (SuperProp::Ident(needle), SuperProp::Ident(candidate)) => {
                needle.eq_ignore_span(candidate)
            }
            (SuperProp::Computed(needle), SuperProp::Computed(candidate)) => {
                self.match_expr(&needle.expr, &candidate.expr)
            }
            _ => false,
        }
    }

    fn match_call_expr(&mut self, needle: &CallExpr, candidate: &CallExpr) -> bool {
        needle.type_args.eq_ignore_span(&candidate.type_args)
            && self.match_callee(&needle.callee, &candidate.callee)
            && self.match_expr_or_spread_slice(&needle.args, &candidate.args)
    }

    fn match_callee(&mut self, needle: &Callee, candidate: &Callee) -> bool {
        match (needle, candidate) {
            (Callee::Super(_), Callee::Super(_)) => true,
            (Callee::Import(needle), Callee::Import(candidate)) => {
                needle.phase.eq_ignore_span(&candidate.phase)
            }
            (Callee::Expr(needle), Callee::Expr(candidate)) => self.match_expr(needle, candidate),
            _ => false,
        }
    }

    fn match_expr_or_spread(&mut self, needle: &ExprOrSpread, candidate: &ExprOrSpread) -> bool {
        needle.spread.is_some() == candidate.spread.is_some()
            && self.match_expr(&needle.expr, &candidate.expr)
    }

    fn match_option_expr_or_spread(
        &mut self,
        needle: &Option<ExprOrSpread>,
        candidate: &Option<ExprOrSpread>,
    ) -> bool {
        match (needle, candidate) {
            (Some(needle), Some(candidate)) => self.match_expr_or_spread(needle, candidate),
            (None, None) => true,
            _ => false,
        }
    }

    /// Match a call/new argument list. `ARGS` / `ARGS_*` holes split
    /// the needle into fixed argument segments matched as an ordered
    /// subsequence with gaps; with no hole this is an exact element-wise
    /// match.
    fn match_expr_or_spread_slice(
        &mut self,
        needle: &[ExprOrSpread],
        candidate: &[ExprOrSpread],
    ) -> bool {
        if needle
            .iter()
            .any(|arg| argument_list_hole_name(arg).is_some())
        {
            self.match_list_with_holes(
                needle,
                candidate,
                |arg| argument_list_hole_name(arg).is_some(),
                Self::match_expr_or_spread,
            )
        } else {
            self.match_slice(needle, candidate, Self::match_expr_or_spread)
        }
    }

    fn match_prop_or_spread(&mut self, needle: &PropOrSpread, candidate: &PropOrSpread) -> bool {
        match (needle, candidate) {
            (PropOrSpread::Spread(needle), PropOrSpread::Spread(candidate)) => {
                self.match_expr(&needle.expr, &candidate.expr)
            }
            (PropOrSpread::Prop(needle), PropOrSpread::Prop(candidate)) => {
                self.match_prop(needle, candidate)
            }
            _ => false,
        }
    }

    fn match_prop(&mut self, needle: &Prop, candidate: &Prop) -> bool {
        match (needle, candidate) {
            (Prop::Shorthand(needle), Prop::Shorthand(candidate)) => {
                needle.eq_ignore_span(candidate)
            }
            (Prop::Shorthand(needle), Prop::KeyValue(candidate)) => {
                self.match_shorthand_against_key_value_prop(needle, candidate)
            }
            (Prop::KeyValue(needle), Prop::Shorthand(candidate)) => {
                self.match_key_value_against_shorthand_prop(needle, candidate)
            }
            (Prop::KeyValue(needle), Prop::KeyValue(candidate)) => {
                self.match_prop_name(&needle.key, &candidate.key)
                    && self.match_expr(&needle.value, &candidate.value)
            }
            (Prop::Assign(needle), Prop::Assign(candidate)) => {
                needle.key.eq_ignore_span(&candidate.key)
                    && self.match_expr(&needle.value, &candidate.value)
            }
            (Prop::Getter(needle), Prop::Getter(candidate)) => {
                self.match_prop_name(&needle.key, &candidate.key)
                    && needle.type_ann.eq_ignore_span(&candidate.type_ann)
                    && self.match_option_block_stmt(&needle.body, &candidate.body)
            }
            (Prop::Setter(needle), Prop::Setter(candidate)) => {
                self.match_prop_name(&needle.key, &candidate.key)
                    && needle.this_param.eq_ignore_span(&candidate.this_param)
                    && self.with_alpha_scope(|matcher| {
                        matcher.match_pat(&needle.param, &candidate.param)
                            && matcher.match_option_block_stmt(&needle.body, &candidate.body)
                    })
            }
            (Prop::Method(needle), Prop::Method(candidate)) => {
                self.match_prop_name(&needle.key, &candidate.key)
                    && self.match_function(&needle.function, &candidate.function)
            }
            _ => needle.eq_ignore_span(candidate),
        }
    }

    fn match_prop_name(&mut self, needle: &PropName, candidate: &PropName) -> bool {
        match (needle, candidate) {
            (PropName::Str(needle), PropName::Str(candidate)) => self.match_str(needle, candidate),
            (PropName::Computed(needle), PropName::Computed(candidate)) => {
                self.match_expr(&needle.expr, &candidate.expr)
            }
            _ => needle.eq_ignore_span(candidate),
        }
    }

    fn match_prop_name_exact(&mut self, needle: &PropName, candidate: &PropName) -> bool {
        match (needle, candidate) {
            (PropName::Computed(needle), PropName::Computed(candidate)) => {
                self.match_expr(&needle.expr, &candidate.expr)
            }
            _ => needle.eq_ignore_span(candidate),
        }
    }

    fn match_shorthand_against_key_value_prop(
        &mut self,
        needle: &Ident,
        candidate: &KeyValueProp,
    ) -> bool {
        self.key_value_prop_is_shorthand_equivalent(candidate, needle)
            && self.match_ident(
                needle,
                key_value_prop_ident_value(candidate)
                    .expect("checked by key_value_prop_is_shorthand_equivalent"),
            )
    }

    fn match_key_value_against_shorthand_prop(
        &mut self,
        needle: &KeyValueProp,
        candidate: &Ident,
    ) -> bool {
        self.key_value_prop_is_shorthand_equivalent(needle, candidate)
            && self.match_ident(
                key_value_prop_ident_value(needle)
                    .expect("checked by key_value_prop_is_shorthand_equivalent"),
                candidate,
            )
    }

    fn key_value_prop_is_shorthand_equivalent(
        &mut self,
        prop: &KeyValueProp,
        shorthand: &Ident,
    ) -> bool {
        prop_name_matches_ident_key(&prop.key, shorthand)
            && key_value_prop_ident_value(prop).is_some_and(|value| {
                value.sym == shorthand.sym && value.optional == shorthand.optional
            })
    }

    fn match_key_value_pat_against_assign_pat(
        &mut self,
        needle: &KeyValuePatProp,
        candidate: &AssignPatProp,
    ) -> bool {
        self.key_value_pat_is_assign_equivalent(needle, candidate)
            && self.match_pat(&needle.value, &Pat::Ident(candidate.key.clone()))
    }

    fn match_assign_pat_against_key_value_pat(
        &mut self,
        needle: &AssignPatProp,
        candidate: &KeyValuePatProp,
    ) -> bool {
        self.key_value_pat_is_assign_equivalent(candidate, needle)
            && self.match_pat(&Pat::Ident(needle.key.clone()), &candidate.value)
    }

    fn match_key_value_ref_pat_against_assign_pat(
        &mut self,
        needle: &KeyValuePatProp,
        candidate: &AssignPatProp,
    ) -> bool {
        self.key_value_pat_is_assign_equivalent(needle, candidate)
            && self.match_ref_pat(&needle.value, &Pat::Ident(candidate.key.clone()))
    }

    fn match_assign_pat_against_key_value_ref_pat(
        &mut self,
        needle: &AssignPatProp,
        candidate: &KeyValuePatProp,
    ) -> bool {
        self.key_value_pat_is_assign_equivalent(candidate, needle)
            && self.match_ref_pat(&Pat::Ident(needle.key.clone()), &candidate.value)
    }

    fn key_value_pat_is_assign_equivalent(
        &mut self,
        prop: &KeyValuePatProp,
        shorthand: &AssignPatProp,
    ) -> bool {
        shorthand.value.is_none()
            && prop_name_matches_binding_key(&prop.key, &shorthand.key)
            && key_value_pat_binding_ident_value(prop).is_some_and(|value| {
                value.id.sym == shorthand.key.sym
                    && value.id.optional == shorthand.key.optional
                    && value.type_ann.is_none()
            })
    }

    fn match_lit(&mut self, needle: &Lit, candidate: &Lit) -> bool {
        match (needle, candidate) {
            (Lit::Str(needle), Lit::Str(candidate)) => self.match_str(needle, candidate),
            _ => needle.eq_ignore_span(candidate),
        }
    }

    fn match_str(&mut self, needle: &Str, candidate: &Str) -> bool {
        let wildcard = needle.value.to_string_lossy();
        if self
            .selector
            .wildcard_string_literals
            .contains(wildcard.as_ref())
        {
            return self.bind_string(wildcard.as_ref(), &candidate.value);
        }
        needle.eq_ignore_span(candidate)
    }

    fn match_tpl(&mut self, needle: &Tpl, candidate: &Tpl) -> bool {
        needle.quasis.eq_ignore_span(&candidate.quasis)
            && self.match_slice(
                &needle.exprs,
                &candidate.exprs,
                |matcher, needle, candidate| matcher.match_expr(needle, candidate),
            )
    }

    fn match_block_stmt_or_expr(
        &mut self,
        needle: &BlockStmtOrExpr,
        candidate: &BlockStmtOrExpr,
    ) -> bool {
        match (needle, candidate) {
            (BlockStmtOrExpr::BlockStmt(needle), BlockStmtOrExpr::BlockStmt(candidate)) => {
                self.match_block_stmt(needle, candidate)
            }
            (BlockStmtOrExpr::Expr(needle), BlockStmtOrExpr::Expr(candidate)) => {
                self.match_expr(needle, candidate)
            }
            _ => false,
        }
    }

    fn match_assign_target(&mut self, needle: &AssignTarget, candidate: &AssignTarget) -> bool {
        match (needle, candidate) {
            (AssignTarget::Simple(needle), AssignTarget::Simple(candidate)) => {
                self.match_simple_assign_target(needle, candidate)
            }
            (AssignTarget::Pat(needle), AssignTarget::Pat(candidate)) => {
                self.match_assign_target_pat(needle, candidate)
            }
            _ => false,
        }
    }

    fn match_assign_target_pat(
        &mut self,
        needle: &AssignTargetPat,
        candidate: &AssignTargetPat,
    ) -> bool {
        match (needle, candidate) {
            (AssignTargetPat::Array(needle), AssignTargetPat::Array(candidate)) => {
                self.match_slice(&needle.elems, &candidate.elems, Self::match_option_ref_pat)
            }
            (AssignTargetPat::Object(needle), AssignTargetPat::Object(candidate)) => self
                .match_slice(
                    &needle.props,
                    &candidate.props,
                    Self::match_ref_object_pat_prop,
                ),
            _ => needle.eq_ignore_span(candidate),
        }
    }

    fn match_simple_assign_target(
        &mut self,
        needle: &SimpleAssignTarget,
        candidate: &SimpleAssignTarget,
    ) -> bool {
        match (needle, candidate) {
            (SimpleAssignTarget::Ident(needle), SimpleAssignTarget::Ident(candidate)) => {
                needle.type_ann.eq_ignore_span(&candidate.type_ann)
                    && self.match_ident(&needle.id, &candidate.id)
            }
            (SimpleAssignTarget::Member(needle), SimpleAssignTarget::Member(candidate)) => {
                self.match_member_expr(needle, candidate)
            }
            (SimpleAssignTarget::SuperProp(needle), SimpleAssignTarget::SuperProp(candidate)) => {
                self.match_super_prop(&needle.prop, &candidate.prop)
            }
            (SimpleAssignTarget::Paren(needle), SimpleAssignTarget::Paren(candidate)) => {
                self.match_expr(&needle.expr, &candidate.expr)
            }
            (SimpleAssignTarget::TsAs(needle), SimpleAssignTarget::TsAs(candidate)) => {
                needle.type_ann.eq_ignore_span(&candidate.type_ann)
                    && self.match_expr(&needle.expr, &candidate.expr)
            }
            (
                SimpleAssignTarget::TsSatisfies(needle),
                SimpleAssignTarget::TsSatisfies(candidate),
            ) => {
                needle.type_ann.eq_ignore_span(&candidate.type_ann)
                    && self.match_expr(&needle.expr, &candidate.expr)
            }
            (SimpleAssignTarget::TsNonNull(needle), SimpleAssignTarget::TsNonNull(candidate)) => {
                self.match_expr(&needle.expr, &candidate.expr)
            }
            (
                SimpleAssignTarget::TsTypeAssertion(needle),
                SimpleAssignTarget::TsTypeAssertion(candidate),
            ) => {
                needle.type_ann.eq_ignore_span(&candidate.type_ann)
                    && self.match_expr(&needle.expr, &candidate.expr)
            }
            (
                SimpleAssignTarget::TsInstantiation(needle),
                SimpleAssignTarget::TsInstantiation(candidate),
            ) => {
                needle.type_args.eq_ignore_span(&candidate.type_args)
                    && self.match_expr(&needle.expr, &candidate.expr)
            }
            _ => needle.eq_ignore_span(candidate),
        }
    }

    fn match_opt_chain_base(&mut self, needle: &OptChainBase, candidate: &OptChainBase) -> bool {
        match (needle, candidate) {
            (OptChainBase::Member(needle), OptChainBase::Member(candidate)) => {
                self.match_member_expr(needle, candidate)
            }
            (OptChainBase::Call(needle), OptChainBase::Call(candidate)) => {
                needle.type_args.eq_ignore_span(&candidate.type_args)
                    && self.match_expr(&needle.callee, &candidate.callee)
                    && self.match_expr_or_spread_slice(&needle.args, &candidate.args)
            }
            _ => false,
        }
    }

    fn match_class(&mut self, needle: &Class, candidate: &Class) -> bool {
        needle.is_abstract == candidate.is_abstract
            && needle.type_params.eq_ignore_span(&candidate.type_params)
            && needle
                .super_type_params
                .eq_ignore_span(&candidate.super_type_params)
            && needle.implements.eq_ignore_span(&candidate.implements)
            && self.match_slice(
                &needle.decorators,
                &candidate.decorators,
                Self::match_decorator,
            )
            && self.match_option_box_expr(&needle.super_class, &candidate.super_class)
            && self.match_class_member_slice(&needle.body, &candidate.body)
    }

    fn match_decorator(&mut self, needle: &Decorator, candidate: &Decorator) -> bool {
        self.match_expr(&needle.expr, &candidate.expr)
    }

    fn match_class_member(&mut self, needle: &ClassMember, candidate: &ClassMember) -> bool {
        match (needle, candidate) {
            (ClassMember::Constructor(needle), ClassMember::Constructor(candidate)) => {
                self.match_constructor(needle, candidate)
            }
            (ClassMember::Method(needle), ClassMember::Method(candidate)) => {
                self.match_class_method(needle, candidate)
            }
            (ClassMember::PrivateMethod(needle), ClassMember::PrivateMethod(candidate)) => {
                self.match_private_method(needle, candidate)
            }
            (ClassMember::ClassProp(needle), ClassMember::ClassProp(candidate)) => {
                self.match_class_prop(needle, candidate)
            }
            (ClassMember::PrivateProp(needle), ClassMember::PrivateProp(candidate)) => {
                self.match_private_prop(needle, candidate)
            }
            (ClassMember::StaticBlock(needle), ClassMember::StaticBlock(candidate)) => {
                self.match_block_stmt(&needle.body, &candidate.body)
            }
            (ClassMember::AutoAccessor(needle), ClassMember::AutoAccessor(candidate)) => {
                self.match_auto_accessor(needle, candidate)
            }
            _ => needle.eq_ignore_span(candidate),
        }
    }

    fn match_constructor(&mut self, needle: &Constructor, candidate: &Constructor) -> bool {
        needle.key.eq_ignore_span(&candidate.key)
            && needle
                .accessibility
                .eq_ignore_span(&candidate.accessibility)
            && needle.is_optional == candidate.is_optional
            && self.with_alpha_scope(|matcher| {
                matcher.match_slice(
                    &needle.params,
                    &candidate.params,
                    Self::match_param_or_ts_param_prop,
                ) && matcher.match_option_block_stmt(&needle.body, &candidate.body)
            })
    }

    fn match_param_or_ts_param_prop(
        &mut self,
        needle: &ParamOrTsParamProp,
        candidate: &ParamOrTsParamProp,
    ) -> bool {
        match (needle, candidate) {
            (ParamOrTsParamProp::Param(needle), ParamOrTsParamProp::Param(candidate)) => {
                self.match_param(needle, candidate)
            }
            (
                ParamOrTsParamProp::TsParamProp(needle),
                ParamOrTsParamProp::TsParamProp(candidate),
            ) => self.match_ts_param_prop(needle, candidate),
            _ => false,
        }
    }

    fn match_ts_param_prop(&mut self, needle: &TsParamProp, candidate: &TsParamProp) -> bool {
        needle
            .accessibility
            .eq_ignore_span(&candidate.accessibility)
            && needle.is_override == candidate.is_override
            && needle.readonly == candidate.readonly
            && self.match_slice(
                &needle.decorators,
                &candidate.decorators,
                Self::match_decorator,
            )
            && self.match_ts_param_prop_param(&needle.param, &candidate.param)
    }

    fn match_ts_param_prop_param(
        &mut self,
        needle: &TsParamPropParam,
        candidate: &TsParamPropParam,
    ) -> bool {
        match (needle, candidate) {
            (TsParamPropParam::Ident(needle), TsParamPropParam::Ident(candidate)) => {
                self.match_binding_binding_ident(needle, candidate)
            }
            (TsParamPropParam::Assign(needle), TsParamPropParam::Assign(candidate)) => {
                self.match_assign_pat(needle, candidate)
            }
            _ => false,
        }
    }

    fn match_class_method(&mut self, needle: &ClassMethod, candidate: &ClassMethod) -> bool {
        needle.kind == candidate.kind
            && needle.is_static == candidate.is_static
            && needle
                .accessibility
                .eq_ignore_span(&candidate.accessibility)
            && needle.is_abstract == candidate.is_abstract
            && needle.is_optional == candidate.is_optional
            && needle.is_override == candidate.is_override
            && self.match_prop_name(&needle.key, &candidate.key)
            && self.match_function(&needle.function, &candidate.function)
    }

    fn match_private_method(&mut self, needle: &PrivateMethod, candidate: &PrivateMethod) -> bool {
        needle.key.eq_ignore_span(&candidate.key)
            && needle.kind == candidate.kind
            && needle.is_static == candidate.is_static
            && needle
                .accessibility
                .eq_ignore_span(&candidate.accessibility)
            && needle.is_abstract == candidate.is_abstract
            && needle.is_optional == candidate.is_optional
            && needle.is_override == candidate.is_override
            && self.match_function(&needle.function, &candidate.function)
    }

    fn match_class_prop(&mut self, needle: &ClassProp, candidate: &ClassProp) -> bool {
        needle.type_ann.eq_ignore_span(&candidate.type_ann)
            && needle.is_static == candidate.is_static
            && self.match_slice(
                &needle.decorators,
                &candidate.decorators,
                Self::match_decorator,
            )
            && needle
                .accessibility
                .eq_ignore_span(&candidate.accessibility)
            && needle.is_abstract == candidate.is_abstract
            && needle.is_optional == candidate.is_optional
            && needle.is_override == candidate.is_override
            && needle.readonly == candidate.readonly
            && needle.declare == candidate.declare
            && needle.definite == candidate.definite
            && self.match_prop_name(&needle.key, &candidate.key)
            && self.match_option_box_expr(&needle.value, &candidate.value)
    }

    fn match_private_prop(&mut self, needle: &PrivateProp, candidate: &PrivateProp) -> bool {
        needle.key.eq_ignore_span(&candidate.key)
            && needle.type_ann.eq_ignore_span(&candidate.type_ann)
            && needle.is_static == candidate.is_static
            && self.match_slice(
                &needle.decorators,
                &candidate.decorators,
                Self::match_decorator,
            )
            && needle
                .accessibility
                .eq_ignore_span(&candidate.accessibility)
            && needle.is_optional == candidate.is_optional
            && needle.is_override == candidate.is_override
            && needle.readonly == candidate.readonly
            && needle.definite == candidate.definite
            && self.match_option_box_expr(&needle.value, &candidate.value)
    }

    fn match_auto_accessor(&mut self, needle: &AutoAccessor, candidate: &AutoAccessor) -> bool {
        needle.type_ann.eq_ignore_span(&candidate.type_ann)
            && needle.is_static == candidate.is_static
            && self.match_slice(
                &needle.decorators,
                &candidate.decorators,
                Self::match_decorator,
            )
            && needle
                .accessibility
                .eq_ignore_span(&candidate.accessibility)
            && needle.is_abstract == candidate.is_abstract
            && needle.is_override == candidate.is_override
            && needle.definite == candidate.definite
            && self.match_key(&needle.key, &candidate.key)
            && self.match_option_box_expr(&needle.value, &candidate.value)
    }

    fn match_key(&mut self, needle: &Key, candidate: &Key) -> bool {
        match (needle, candidate) {
            (Key::Public(needle), Key::Public(candidate)) => {
                self.match_prop_name(needle, candidate)
            }
            (Key::Private(needle), Key::Private(candidate)) => needle.eq_ignore_span(candidate),
            _ => false,
        }
    }

    fn match_option_box_expr(
        &mut self,
        needle: &Option<Box<Expr>>,
        candidate: &Option<Box<Expr>>,
    ) -> bool {
        match (needle, candidate) {
            (Some(needle), Some(candidate)) => self.match_expr(needle, candidate),
            (None, None) => true,
            _ => false,
        }
    }

    fn match_option_box_stmt(
        &mut self,
        needle: &Option<Box<Stmt>>,
        candidate: &Option<Box<Stmt>>,
    ) -> bool {
        match (needle, candidate) {
            (Some(needle), Some(candidate)) => self.match_stmt(needle, candidate),
            (None, None) => true,
            _ => false,
        }
    }

    fn match_option_block_stmt(
        &mut self,
        needle: &Option<BlockStmt>,
        candidate: &Option<BlockStmt>,
    ) -> bool {
        match (needle, candidate) {
            (Some(needle), Some(candidate)) => self.match_block_stmt(needle, candidate),
            (None, None) => true,
            _ => false,
        }
    }

    fn match_option_var_decl_or_expr(
        &mut self,
        needle: &Option<VarDeclOrExpr>,
        candidate: &Option<VarDeclOrExpr>,
    ) -> bool {
        match (needle, candidate) {
            (Some(needle), Some(candidate)) => self.match_var_decl_or_expr(needle, candidate),
            (None, None) => true,
            _ => false,
        }
    }

    fn match_option_catch_clause(
        &mut self,
        needle: &Option<CatchClause>,
        candidate: &Option<CatchClause>,
    ) -> bool {
        match (needle, candidate) {
            (Some(needle), Some(candidate)) => self.match_catch_clause(needle, candidate),
            (None, None) => true,
            _ => false,
        }
    }

    fn match_option_pat(&mut self, needle: &Option<Pat>, candidate: &Option<Pat>) -> bool {
        match (needle, candidate) {
            (Some(needle), Some(candidate)) => self.match_pat(needle, candidate),
            (None, None) => true,
            _ => false,
        }
    }

    fn match_option_box_str(
        &mut self,
        needle: &Option<Box<Str>>,
        candidate: &Option<Box<Str>>,
    ) -> bool {
        match (needle, candidate) {
            (Some(needle), Some(candidate)) => self.match_str(needle, candidate),
            (None, None) => true,
            _ => false,
        }
    }

    fn match_option_expr_or_spread_vec(
        &mut self,
        needle: &Option<Vec<ExprOrSpread>>,
        candidate: &Option<Vec<ExprOrSpread>>,
    ) -> bool {
        match (needle, candidate) {
            (Some(needle), Some(candidate)) => self.match_expr_or_spread_slice(needle, candidate),
            (None, None) => true,
            _ => false,
        }
    }

    /// Match a statement list. `STMT_LIST;` holes split the needle into
    /// fixed segments matched as an ordered subsequence with gaps (see
    /// [`Self::match_list_with_holes`]); with no hole this is an exact
    /// element-wise match.
    fn match_stmt_slice(&mut self, needle: &[Stmt], candidate: &[Stmt]) -> bool {
        if needle
            .iter()
            .any(|stmt| statement_list_hole_name(stmt).is_some())
        {
            self.match_list_with_holes(
                needle,
                candidate,
                |stmt| statement_list_hole_name(stmt).is_some(),
                Self::match_stmt,
            )
        } else {
            self.match_slice(needle, candidate, Self::match_stmt)
        }
    }

    /// Match a class member list. `CLASS_REST;` holes split the needle
    /// into fixed segments matched as an ordered subsequence with gaps
    /// (see [`Self::match_list_with_holes`]); with no hole this is an
    /// exact element-wise match.
    fn match_class_member_slice(
        &mut self,
        needle: &[ClassMember],
        candidate: &[ClassMember],
    ) -> bool {
        if needle.iter().any(is_class_rest_hole) {
            self.match_list_with_holes(
                needle,
                candidate,
                is_class_rest_hole,
                Self::match_class_member,
            )
        } else {
            self.match_slice(needle, candidate, Self::match_class_member)
        }
    }

    /// Match a variable declarator list. `DECLARATORS` /
    /// `DECLARATORS_*` holes split the needle into fixed declarator
    /// segments matched as an ordered subsequence with gaps. The return
    /// value maps each needle declarator index to the candidate
    /// declarator index it matched; list-hole entries are `None`.
    fn match_var_declarator_slice_with_alignment(
        &mut self,
        needle: &[VarDeclarator],
        candidate: &[VarDeclarator],
    ) -> Option<Vec<Option<usize>>> {
        let mut alignment = vec![None; needle.len()];
        if !needle
            .iter()
            .any(|declarator| declarator_list_hole_name(declarator).is_some())
        {
            if needle.len() != candidate.len() {
                return None;
            }
            for (idx, (needle, candidate)) in needle.iter().zip(candidate).enumerate() {
                if !self.match_var_declarator(needle, candidate) {
                    return None;
                }
                alignment[idx] = Some(idx);
            }
            return Some(alignment);
        }

        let mut segments: Vec<(usize, usize)> = Vec::new();
        let mut idx = 0;
        while idx < needle.len() {
            if declarator_list_hole_name(&needle[idx]).is_some() {
                idx += 1;
                continue;
            }
            let start = idx;
            while idx < needle.len() && declarator_list_hole_name(&needle[idx]).is_none() {
                idx += 1;
            }
            segments.push((start, idx - start));
        }
        if segments.is_empty() {
            return Some(alignment);
        }

        let search = SegmentSearch {
            needle,
            candidate,
            segments: &segments,
            anchored_left: declarator_list_hole_name(&needle[0]).is_none(),
            anchored_right: declarator_list_hole_name(&needle[needle.len() - 1]).is_none(),
        };
        self.place_var_declarator_segments(&search, 0, 0, &mut alignment)
            .then_some(alignment)
    }

    fn place_var_declarator_segments(
        &mut self,
        search: &SegmentSearch<VarDeclarator>,
        seg_idx: usize,
        cand_min: usize,
        alignment: &mut [Option<usize>],
    ) -> bool {
        let Some(&(needle_start, seg_len)) = search.segments.get(seg_idx) else {
            return true;
        };
        let remaining: usize = search.segments[seg_idx..].iter().map(|(_, len)| len).sum();
        let Some(latest_start) = search.candidate.len().checked_sub(remaining) else {
            return false;
        };
        let mut lo = cand_min;
        let mut hi = latest_start;
        if seg_idx == 0 && search.anchored_left {
            hi = hi.min(0);
        }
        if seg_idx == search.segments.len() - 1 && search.anchored_right {
            lo = lo.max(latest_start);
        }
        for start in lo..=hi {
            let snapshot = self.snapshot();
            let alignment_snapshot = alignment.to_vec();
            let mut segment_ok = true;
            for offset in 0..seg_len {
                let needle_idx = needle_start + offset;
                let candidate_idx = start + offset;
                if !self.match_var_declarator(
                    &search.needle[needle_idx],
                    &search.candidate[candidate_idx],
                ) {
                    segment_ok = false;
                    break;
                }
                alignment[needle_idx] = Some(candidate_idx);
            }
            if segment_ok
                && self.place_var_declarator_segments(
                    search,
                    seg_idx + 1,
                    start + seg_len,
                    alignment,
                )
            {
                return true;
            }
            self.restore(snapshot);
            alignment.copy_from_slice(&alignment_snapshot);
        }
        false
    }

    /// Match a needle list carrying one or more list-holes against a
    /// candidate list as an **ordered subsequence with gaps**. The holes
    /// partition the needle into maximal fixed-element segments; each
    /// segment must appear in the candidate as a contiguous block, the
    /// segments in source order and non-overlapping, with the gaps
    /// between them (plus any leading/trailing hole) absorbing arbitrary
    /// runs of candidate elements — including empty runs. A leading hole
    /// un-anchors the first segment from the candidate's start; a
    /// trailing hole un-anchors the last segment from its end. A single
    /// interior hole degenerates to the old contiguous prefix/suffix
    /// match (both ends anchored, one gap in the middle).
    ///
    /// Matching is greedy-leftmost and commits the first placement under
    /// which every segment matches, keeping the identifier/wildcard
    /// bindings it accumulated. This is a pure existence check: the
    /// interior alignment chosen when several placements are possible
    /// never changes *which* enclosing declaration matched, and the
    /// "matched more than one top-level declaration" ambiguity is still a
    /// hard error in the caller that counts those matches.
    fn match_list_with_holes<T>(
        &mut self,
        needle: &[T],
        candidate: &[T],
        is_hole: impl Fn(&T) -> bool,
        match_item: fn(&mut Self, &T, &T) -> bool,
    ) -> bool {
        let mut segments: Vec<(usize, usize)> = Vec::new();
        let mut idx = 0;
        while idx < needle.len() {
            if is_hole(&needle[idx]) {
                idx += 1;
                continue;
            }
            let start = idx;
            while idx < needle.len() && !is_hole(&needle[idx]) {
                idx += 1;
            }
            segments.push((start, idx - start));
        }
        // An all-holes needle pins nothing, so it matches any candidate
        // run (including an empty one).
        if segments.is_empty() {
            return true;
        }
        let search = SegmentSearch {
            needle,
            candidate,
            segments: &segments,
            anchored_left: !is_hole(&needle[0]),
            anchored_right: !is_hole(&needle[needle.len() - 1]),
        };
        self.place_segments(&search, 0, 0, match_item)
    }

    /// Recursive ordered-subsequence search backing
    /// [`Self::match_list_with_holes`]. Places `segments[seg_idx..]` into
    /// `candidate[cand_min..]`, trying the leftmost feasible start first
    /// and rolling the matcher state back after each failed attempt.
    /// Returns true — leaving the committed bindings in place — once
    /// every remaining segment is placed.
    fn place_segments<T>(
        &mut self,
        search: &SegmentSearch<T>,
        seg_idx: usize,
        cand_min: usize,
        match_item: fn(&mut Self, &T, &T) -> bool,
    ) -> bool {
        let Some(&(needle_start, seg_len)) = search.segments.get(seg_idx) else {
            return true; // every segment placed
        };
        let remaining: usize = search.segments[seg_idx..].iter().map(|(_, len)| len).sum();
        // The latest start that still leaves room for this and every
        // following segment; `None` means the candidate is too short.
        let Some(latest_start) = search.candidate.len().checked_sub(remaining) else {
            return false;
        };
        let mut lo = cand_min;
        let mut hi = latest_start;
        if seg_idx == 0 && search.anchored_left {
            // The first segment must start at the candidate's first element.
            hi = hi.min(0);
        }
        if seg_idx == search.segments.len() - 1 && search.anchored_right {
            // The last segment must end at the candidate's last element.
            lo = lo.max(latest_start);
        }
        // An empty `lo..=hi` (e.g. an anchor pushed `lo` past `hi`) means
        // no feasible placement, so the search backtracks.
        for start in lo..=hi {
            let snapshot = self.snapshot();
            let mut segment_ok = true;
            for offset in 0..seg_len {
                if !match_item(
                    self,
                    &search.needle[needle_start + offset],
                    &search.candidate[start + offset],
                ) {
                    segment_ok = false;
                    break;
                }
            }
            if segment_ok && self.place_segments(search, seg_idx + 1, start + seg_len, match_item) {
                return true;
            }
            self.restore(snapshot);
        }
        false
    }

    fn snapshot(&self) -> MatcherState {
        MatcherState {
            replacements: self.replacements.clone(),
            alpha_scopes: self.alpha_scopes.clone(),
        }
    }

    fn restore(&mut self, state: MatcherState) {
        self.replacements = state.replacements;
        self.alpha_scopes = state.alpha_scopes;
    }

    fn match_slice<T>(
        &mut self,
        needle: &[T],
        candidate: &[T],
        mut match_item: impl FnMut(&mut Self, &T, &T) -> bool,
    ) -> bool {
        needle.len() == candidate.len()
            && needle
                .iter()
                .zip(candidate)
                .all(|(needle, candidate)| match_item(self, needle, candidate))
    }
}

#[derive(Default)]
struct AlphaCanonicalScope {
    names: BTreeMap<Atom, Atom>,
}

#[derive(Default)]
struct AlphaIdentCanonicalizer {
    next: usize,
    scopes: Vec<AlphaCanonicalScope>,
    reserved_idents: BTreeSet<String>,
}

impl AlphaIdentCanonicalizer {
    fn new(wildcard_idents: &WildcardIdents) -> Self {
        Self {
            scopes: vec![AlphaCanonicalScope::default()],
            reserved_idents: wildcard_idents
                .expressions
                .iter()
                .chain(&wildcard_idents.statements)
                .chain(&wildcard_idents.statement_lists)
                .chain(&wildcard_idents.declarator_lists)
                .chain(&wildcard_idents.argument_lists)
                .cloned()
                .collect(),
            ..Self::default()
        }
    }
}

impl AlphaIdentCanonicalizer {
    fn with_scope(&mut self, f: impl FnOnce(&mut Self)) {
        self.scopes.push(AlphaCanonicalScope::default());
        f(self);
        self.scopes.pop();
    }

    fn visible_canonical(&self, sym: &Atom) -> Option<Atom> {
        self.scopes
            .iter()
            .rev()
            .find_map(|scope| scope.names.get(sym).cloned())
    }

    fn canonical_ref(&mut self, sym: &Atom) -> Atom {
        if let Some(existing) = self.visible_canonical(sym) {
            return existing;
        }
        self.canonical_binding(sym)
    }

    fn canonical_binding(&mut self, sym: &Atom) -> Atom {
        let scope = self
            .scopes
            .last_mut()
            .expect("alpha canonicalizer always has a root scope");
        if let Some(existing) = scope.names.get(sym) {
            return existing.clone();
        }
        let canonical = Atom::from(format!("__debundle_alpha_{}", self.next));
        self.next += 1;
        scope.names.insert(sym.clone(), canonical.clone());
        canonical
    }
}

impl VisitMut for AlphaIdentCanonicalizer {
    fn visit_mut_ident(&mut self, ident: &mut swc_ecma_ast::Ident) {
        if self.reserved_idents.contains(ident.sym.as_ref()) {
            return;
        }
        ident.sym = self.canonical_ref(&ident.sym);
    }

    fn visit_mut_binding_ident(&mut self, ident: &mut BindingIdent) {
        if !self.reserved_idents.contains(ident.id.sym.as_ref()) {
            ident.id.sym = self.canonical_binding(&ident.id.sym);
        }
        ident.type_ann.visit_mut_with(self);
    }

    fn visit_mut_function(&mut self, function: &mut Function) {
        self.with_scope(|visitor| function.visit_mut_children_with(visitor));
    }

    fn visit_mut_arrow_expr(&mut self, arrow: &mut ArrowExpr) {
        self.with_scope(|visitor| arrow.visit_mut_children_with(visitor));
    }

    fn visit_mut_catch_clause(&mut self, catch_clause: &mut CatchClause) {
        self.with_scope(|visitor| catch_clause.visit_mut_children_with(visitor));
    }

    fn visit_mut_member_expr(&mut self, member: &mut MemberExpr) {
        member.obj.visit_mut_with(self);
        match &mut member.prop {
            MemberProp::Ident(_) | MemberProp::PrivateName(_) => {}
            MemberProp::Computed(prop) => prop.expr.visit_mut_with(self),
        }
    }

    fn visit_mut_prop(&mut self, prop: &mut Prop) {
        match prop {
            Prop::Shorthand(_) => {}
            Prop::KeyValue(prop) => {
                visit_computed_prop_name(&mut prop.key, self);
                prop.value.visit_mut_with(self);
            }
            Prop::Assign(prop) => {
                prop.value.visit_mut_with(self);
            }
            Prop::Getter(prop) => {
                visit_computed_prop_name(&mut prop.key, self);
                prop.type_ann.visit_mut_with(self);
                prop.body.visit_mut_with(self);
            }
            Prop::Setter(prop) => {
                visit_computed_prop_name(&mut prop.key, self);
                prop.this_param.visit_mut_with(self);
                prop.param.visit_mut_with(self);
                prop.body.visit_mut_with(self);
            }
            Prop::Method(prop) => {
                visit_computed_prop_name(&mut prop.key, self);
                prop.function.visit_mut_with(self);
            }
        }
    }
}

fn visit_computed_prop_name(prop_name: &mut PropName, visitor: &mut AlphaIdentCanonicalizer) {
    if let PropName::Computed(prop_name) = prop_name {
        prop_name.expr.visit_mut_with(visitor);
    }
}

#[derive(Default)]
struct WildcardIdents {
    expressions: BTreeSet<String>,
    statements: BTreeSet<String>,
    /// `STMT_LIST` statement-list hole names (reserved from
    /// alpha-canonicalization like the single-node holes).
    statement_lists: BTreeSet<String>,
    /// `DECLARATORS` variable-declarator-list hole names. These are
    /// pseudo-declarators and must not be alpha-canonicalized as real
    /// bindings.
    declarator_lists: BTreeSet<String>,
    /// `ARGS` argument-list hole names. These are pseudo-arguments and
    /// must route to ordered-subsequence argument matching.
    argument_lists: BTreeSet<String>,
    /// Whether the selector contains any `STR_LITERAL_MATCHING_RE(...)`
    /// expression predicate. This is not an identifier hole, but it
    /// changes expression shape (`CallExpr` in the selector,
    /// `Lit::Str` in the candidate), so the selector still needs the
    /// wildcard matcher.
    string_literal_regex_present: bool,
    /// Whether the selector contains any `CLASS_REST` class-member
    /// hole. The marker is a class field key (an `IdentName`, not an
    /// `Ident`), so it survives alpha-canonicalization without being
    /// reserved — only presence matters for routing.
    class_rest_present: bool,
}

impl WildcardIdents {
    /// True when the selector carries no holes of any kind, so the
    /// caller can take the plain `eq_ignore_span` fast path. List holes
    /// count: a selector with only `STMT_LIST` / `CLASS_REST` holes still
    /// needs the structural matcher.
    fn is_empty(&self) -> bool {
        self.expressions.is_empty()
            && self.statements.is_empty()
            && self.statement_lists.is_empty()
            && self.declarator_lists.is_empty()
            && self.argument_lists.is_empty()
            && !self.string_literal_regex_present
            && !self.class_rest_present
    }
}

fn wildcard_ident_names_for_module_items(needles: &[ModuleItem]) -> WildcardIdents {
    let mut collector = WildcardIdentCollector::default();
    for needle in needles {
        needle.visit_with(&mut collector);
    }
    collector.idents
}

fn wildcard_ident_names(needle: &ModuleItem) -> WildcardIdents {
    let mut collector = WildcardIdentCollector::default();
    needle.visit_with(&mut collector);
    collector.idents
}

#[derive(Default)]
struct WildcardIdentCollector {
    idents: WildcardIdents,
}

impl Visit for WildcardIdentCollector {
    fn visit_expr(&mut self, expr: &Expr) {
        if string_literal_regex_pattern(expr).is_some() {
            self.idents.string_literal_regex_present = true;
            return;
        }
        if let Some(hole_name) = expression_hole_name(expr) {
            self.idents.expressions.insert(hole_name.to_string());
            return;
        }
        expr.visit_children_with(self);
    }

    fn visit_stmt(&mut self, stmt: &Stmt) {
        if let Some(hole_name) = statement_hole_name(stmt) {
            // `STMT` is a keyword-prefix of `STMT_LIST`, so the list hole
            // must win first.
            if hole_name_for(hole_name, STMT_LIST_HOLE_KEYWORD).is_some() {
                self.idents.statement_lists.insert(hole_name.to_string());
                return;
            }
            if hole_name_for(hole_name, STMT_HOLE_KEYWORD).is_some() {
                self.idents.statements.insert(hole_name.to_string());
                return;
            }
        }
        stmt.visit_children_with(self);
    }

    fn visit_class_member(&mut self, member: &ClassMember) {
        if is_class_rest_hole(member) {
            self.idents.class_rest_present = true;
            return;
        }
        member.visit_children_with(self);
    }

    fn visit_var_declarator(&mut self, declarator: &VarDeclarator) {
        if let Some(hole_name) = declarator_list_hole_name(declarator) {
            self.idents.declarator_lists.insert(hole_name.to_string());
            return;
        }
        declarator.visit_children_with(self);
    }

    fn visit_expr_or_spread(&mut self, expr_or_spread: &ExprOrSpread) {
        if let Some(hole_name) = argument_list_hole_name(expr_or_spread) {
            self.idents.argument_lists.insert(hole_name.to_string());
            return;
        }
        expr_or_spread.visit_children_with(self);
    }
}

fn expression_hole_name(expr: &Expr) -> Option<&str> {
    let Expr::Ident(ident) = expr else {
        return None;
    };
    hole_name_for(ident.sym.as_ref(), EXPR_HOLE_KEYWORD)
}

fn string_literal_regex_pattern(expr: &Expr) -> Option<String> {
    let Expr::Call(call) = expr else {
        return None;
    };
    if call.args.len() != 1 || call.args[0].spread.is_some() {
        return None;
    }
    let Callee::Expr(callee) = &call.callee else {
        return None;
    };
    let Expr::Ident(callee) = callee.as_ref() else {
        return None;
    };
    if callee.sym.as_ref() != STRING_LITERAL_REGEX_PREDICATE {
        return None;
    }
    let Expr::Lit(Lit::Str(pattern)) = call.args[0].expr.as_ref() else {
        return None;
    };
    Some(pattern.value.to_string_lossy().to_string())
}

fn string_literal_matches_regex(pattern: &str, candidate_value: &Wtf8Atom) -> bool {
    Regex::new(pattern)
        .is_ok_and(|regex| regex.is_match(candidate_value.to_string_lossy().as_ref()))
}

fn statement_hole_name(stmt: &Stmt) -> Option<&str> {
    let Stmt::Expr(ExprStmt { expr, .. }) = stmt else {
        return None;
    };
    let Expr::Ident(ident) = expr.as_ref() else {
        return None;
    };
    Some(ident.sym.as_ref())
}

/// The name of a statement-list hole (`STMT_LIST` / `STMT_LIST_*;`) if
/// `stmt` is one.
fn statement_list_hole_name(stmt: &Stmt) -> Option<&str> {
    hole_name_for(statement_hole_name(stmt)?, STMT_LIST_HOLE_KEYWORD)
}

/// The name of a top-level statement-list hole (`STMT_LIST` /
/// `STMT_LIST_*;`) if `item` is one. Module declarations cannot be
/// holes because the selector syntax is an expression statement.
fn module_item_list_hole_name(item: &ModuleItem) -> Option<&str> {
    let ModuleItem::Stmt(stmt) = item else {
        return None;
    };
    statement_list_hole_name(stmt)
}

/// The name of a declarator-list hole (`DECLARATORS` /
/// `DECLARATORS_*`) if `declarator` is one. The initializer is ignored:
/// `const` syntax requires one, so selectors usually write
/// `DECLARATORS_BEFORE = null`.
fn declarator_list_hole_name(declarator: &VarDeclarator) -> Option<&str> {
    let Pat::Ident(ident) = &declarator.name else {
        return None;
    };
    hole_name_for(ident.id.sym.as_ref(), DECLARATORS_HOLE_KEYWORD)
}

/// The name of an argument-list hole (`ARGS` / `ARGS_*`) if
/// `expr_or_spread` is one. The hole token itself is a plain identifier
/// argument; the run it absorbs may contain spread or non-spread
/// arguments.
fn argument_list_hole_name(expr_or_spread: &ExprOrSpread) -> Option<&str> {
    if expr_or_spread.spread.is_some() {
        return None;
    }
    let Expr::Ident(ident) = expr_or_spread.expr.as_ref() else {
        return None;
    };
    hole_name_for(ident.sym.as_ref(), ARGS_HOLE_KEYWORD)
}

/// If `name` is a hole identifier for `keyword` — the bare keyword
/// (anonymous) or a `<keyword>_<suffix>` (named) form — return it. The
/// keyword must be followed by end-of-string or `_`, so `EXPR` and
/// `EXPR_FOO` match for keyword `EXPR` but `EXPRESSION` does not.
fn hole_name_for<'a>(name: &'a str, keyword: &str) -> Option<&'a str> {
    let rest = name.strip_prefix(keyword)?;
    (rest.is_empty() || rest.starts_with('_')).then_some(name)
}

/// Whether a hole name is the anonymous form: the bare keyword, or the
/// keyword with a trailing underscore but no suffix. Anonymous holes
/// match independently at every occurrence instead of binding for
/// cross-occurrence equality.
fn hole_is_anonymous(name: &str, keyword: &str) -> bool {
    matches!(name.strip_prefix(keyword), Some("") | Some("_"))
}

/// Whether `member` is a `CLASS_REST;` class-member hole: a class field
/// whose key is exactly the keyword and which has no initializer. Matched
/// as an exact token (not a `CLASS_REST_*` prefix) so it never collides
/// with a real field whose name merely starts with `CLASS_REST`; since it
/// never binds, a suffix would carry no meaning anyway.
fn is_class_rest_hole(member: &ClassMember) -> bool {
    let ClassMember::ClassProp(prop) = member else {
        return false;
    };
    prop.value.is_none()
        && matches!(
            &prop.key,
            PropName::Ident(ident) if ident.sym.as_ref() == CLASS_REST_HOLE_KEYWORD
        )
}
