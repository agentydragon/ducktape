//! Selector-codemod report types, candidate builders and the text renderer.

use std::path::Path;

use serde::Serialize;
use synthesis_failure::SynthesisSkipReason;

use crate::group_synthesis::SynthesizedSelectorGroup;

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum SelectorCodemodAction {
    WouldChange,
    Changed,
    Skipped,
}

/// Whether the selector-codemod report describes a preview or applied edits.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, strum::Display)]
#[serde(rename_all = "snake_case")]
#[strum(serialize_all = "snake_case")]
pub enum SelectorCodemodReportAction {
    Applied,
    DryRun,
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
    pub reason: Option<SynthesisSkipReason<selector_outcome::InvalidSelectorReport>>,
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
    pub action: SelectorCodemodReportAction,
    pub candidates: Vec<SelectorCodemodCandidate>,
    pub summary: SelectorCodemodSummary,
}

#[derive(Serialize)]
pub struct SelectorCodemodModuleResult<'a> {
    pub module: &'a str,
    pub file: String,
    pub candidates: &'a [SelectorCodemodCandidate],
    pub file_written: bool,
}

pub(crate) struct SynthesizedCandidateInput<'a> {
    pub(crate) module: &'a str,
    pub(crate) file: &'a Path,
    pub(crate) member_index: usize,
    pub(crate) export_name: Option<String>,
    pub(crate) apply: bool,
    pub(crate) group_id: usize,
    pub(crate) synthesized: &'a SynthesizedSelectorGroup,
    pub(crate) target_binding: Option<String>,
}

pub(crate) fn synthesized_candidate(
    input: SynthesizedCandidateInput<'_>,
) -> SelectorCodemodCandidate {
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

pub(crate) fn skipped_candidate(
    module: &str,
    file: &Path,
    member_index: usize,
    export_name: Option<String>,
    reason: SynthesisSkipReason,
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
        reason: Some(reason.to_report()),
    }
}

pub fn render_selector_codemod_text(report: &SelectorCodemodReport, out: &mut String) {
    let s = &report.summary;
    out.push_str(&format!(
        "{}: {} candidate(s), {} skipped, {} file(s) written\n",
        report.action,
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
                    candidate
                        .reason
                        .as_ref()
                        .expect("skipped candidates retain a reason")
                        .explanation()
                ));
            }
        }
    }
}

fn is_zero(value: &usize) -> bool {
    *value == 0
}
