//! The one transaction behind every graph-backed ownership edit (`modules
//! merge`, `modules delete`, `bindings assign`, `bindings unassign`, `modules
//! apply`): optional full-candidate assessment → graph check → writes →
//! import-order update → [`MutationOutcome`]. Nothing is written until the
//! assessment and the check both pass.
//!
//! A verb plans its edit ([`EditPlan`]), picks its check ([`EditCheck`]) and
//! labelling rule ([`OutcomeRule`]). Whether an assessment may accompany a
//! write is decided when the verb resolves its assessment arguments
//! ([`candidate_assessment::CandidateEffectAssessmentArgs`]).

use std::collections::BTreeSet;
use std::path::Path;

use anyhow::{Context, Result};
use candidate_assessment::ResolvedCandidateEffectAssessment;
use edit_gate::{
    CheckedImportOrder, Gate, ModuleDocs, PostEditSpec, SourceUnitModuleAssignments,
    post_edit_spec_from_docs,
};
use outcome::{GateOutcome, MutationAction, MutationOutcome, MutationVerb};
use spec::ModulePath;
use spec_tree::ModuleTreeScope;

/// Complete typed post-edit module documents, minus the modules the write
/// deletes.
#[derive(Clone, Copy)]
pub struct CandidateDocs<'d> {
    pub docs: &'d ModuleDocs,
    pub deleted: &'d BTreeSet<String>,
}

impl CandidateDocs<'_> {
    pub fn post_spec(&self) -> Result<PostEditSpec> {
        post_edit_spec_from_docs(self.docs, self.deleted)
    }
}

/// The verb-specific half of a [`GraphEdit`]: the planned candidate and how
/// it reaches disk.
pub trait EditPlan {
    /// The candidate's complete documents, when the verb planned them. An
    /// assessment requires them.
    fn documents(&self) -> Option<CandidateDocs<'_>>;

    /// The post-edit view the graph check reads, built only when it runs.
    fn post_spec(&self) -> Result<PostEditSpec> {
        self.documents()
            .context("edit plan has neither documents nor a post-edit view")?
            .post_spec()
    }

    /// Persist the edit (or, under `--dry-run`, only list it) and return
    /// `(files_written, files_deleted)`.
    fn write(self, dry_run: bool) -> Result<(Vec<String>, Vec<String>)>;
}

/// How the candidate is proven before any write.
pub enum EditCheck<'a> {
    /// The post-edit claims gate. Its priority orders the assessment, and it
    /// stages any `--import-order` update.
    Claims(Gate<'a>),
    /// The reviewed complete source-unit assignment under an explicit
    /// priority, which the verb staged into its tree config.
    SourceUnits {
        gate: Gate<'a>,
        assignments: &'a SourceUnitModuleAssignments,
        scope: Option<&'a ModuleTreeScope>,
        import_order: &'a [ModulePath],
        staged_order: CheckedImportOrder,
    },
}

/// How the outcome labels the edit.
#[derive(Clone, Copy, PartialEq, Eq)]
pub enum OutcomeRule {
    /// `applied` / `dry-run` under the check's gate label.
    Write,
    /// Nothing was requested: no check runs; `noop` / `not_required`.
    EmptyRequest,
    /// Like [`Self::Write`], but `unchanged` when no file changes.
    WriteOrUnchanged,
}

pub struct GraphEdit<'a> {
    pub verb: MutationVerb,
    pub modules_root: &'a Path,
    pub dry_run: bool,
    pub check: EditCheck<'a>,
    pub assessment: Option<&'a ResolvedCandidateEffectAssessment<'a>>,
}

pub struct EditResult {
    pub outcome: MutationOutcome,
    pub effect_relocation_assessment: Option<gate::CandidateRelocationAssessmentReport>,
}

impl GraphEdit<'_> {
    pub fn run(
        self,
        stderr: &mut (dyn std::io::Write + Send),
        plan: impl EditPlan,
        rule: OutcomeRule,
    ) -> Result<EditResult> {
        let Self {
            verb,
            modules_root,
            dry_run,
            check,
            assessment,
        } = self;
        let effect_relocation_assessment = assessment
            .map(|context| {
                let candidate = plan
                    .documents()
                    .context("candidate effect assessment requires complete candidate documents")?;
                let priority = match &check {
                    EditCheck::Claims(gate) => {
                        gate.import_priority_for_spec(modules_root, &candidate.post_spec()?)?
                    }
                    EditCheck::SourceUnits { import_order, .. } => import_order.to_vec(),
                };
                let assessment = context
                    .assess(
                        stderr,
                        modules_root,
                        candidate.docs,
                        candidate.deleted,
                        &priority,
                    )
                    .context("candidate effect assessment failed")?;
                // Dry-run-only verbs resolve an assessment only under
                // `--dry-run`, where this never refuses.
                pipeline::ensure_candidate_emittable_for_write(&assessment, dry_run)?;
                Ok::<_, anyhow::Error>(assessment)
            })
            .transpose()?;

        let (checks, checked_order) = match check {
            _ if rule == OutcomeRule::EmptyRequest => {
                (GateOutcome::NotRequired, CheckedImportOrder::Unchanged)
            }
            EditCheck::Claims(gate) => (
                GateOutcome::from(&gate),
                gate.check(stderr, modules_root, || plan.post_spec())?,
            ),
            EditCheck::SourceUnits {
                gate,
                assignments,
                scope,
                import_order,
                staged_order,
            } => {
                gate.check_source_units(
                    stderr,
                    modules_root,
                    &plan.post_spec()?,
                    assignments,
                    scope,
                    import_order,
                )?;
                (GateOutcome::from(&gate), staged_order)
            }
        };

        let (mut files_written, files_deleted) = plan.write(dry_run)?;
        checked_order.write(dry_run, &mut files_written)?;
        let action = match rule {
            OutcomeRule::EmptyRequest => MutationAction::Noop,
            OutcomeRule::WriteOrUnchanged
                if files_written.is_empty() && files_deleted.is_empty() =>
            {
                MutationAction::Unchanged
            }
            OutcomeRule::Write | OutcomeRule::WriteOrUnchanged => {
                MutationAction::for_write(dry_run)
            }
        };
        Ok(EditResult {
            outcome: MutationOutcome::ownership_edit(
                verb,
                action,
                checks,
                effect_relocation_assessment.as_ref(),
                files_written,
                files_deleted,
            ),
            effect_relocation_assessment,
        })
    }
}
