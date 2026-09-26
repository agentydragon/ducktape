//! The one place a chunk records selector outcomes, so fail-fast and
//! keep-going differ in exactly one branch.

use anyhow::{Result, bail};
use selector_outcome::{SelectorOutcome, SelectorOutcomeReport, Severity, TemplateIdentifiers};

/// Outcome lines a failing chunk prints; `selector_diagnostics.json` keeps them
/// all.
const HUMAN_OUTCOME_REPORT_LIMIT: usize = 200;

/// Every selector outcome of one chunk worth reporting: each entity that did
/// not resolve, and each resolved-by-elimination warning. Warnings print as
/// they arrive and never stop the chunk. With `fail_fast` the first error
/// stops it with that outcome's line; otherwise the entity stays unclaimed,
/// the chunk goes on, and [`OutcomeSink::finish`] fails it with every error.
pub(super) struct OutcomeSink {
    outcomes: Vec<SelectorOutcome>,
    fail_fast: bool,
    /// `Some` when the report lists template identifiers.
    templates: Option<Vec<TemplateIdentifiers>>,
}

impl OutcomeSink {
    pub(super) fn new(fail_fast: bool, list_template_identifiers: bool) -> Self {
        Self {
            outcomes: Vec::new(),
            fail_fast,
            templates: list_template_identifiers.then(Vec::new),
        }
    }

    pub(super) fn list_templates(&mut self, templates: Vec<TemplateIdentifiers>) {
        if let Some(listed) = &mut self.templates {
            listed.extend(templates);
        }
    }

    pub(super) fn fail_fast(&self) -> bool {
        self.fail_fast
    }

    pub(super) fn record(&mut self, outcome: SelectorOutcome) -> Result<()> {
        match outcome.severity() {
            Severity::Error if self.fail_fast => bail!("{}", outcome.render_line()),
            Severity::Warning => eprintln!("{}", outcome.render_line()),
            Severity::Ok | Severity::Error => {}
        }
        self.outcomes.push(outcome);
        Ok(())
    }

    /// Every recorded outcome, sorted, and the listed templates; `None` when
    /// there are no outcomes and templates are not listed.
    pub(super) fn report(&self) -> Option<SelectorOutcomeReport> {
        if self.outcomes.is_empty() && self.templates.is_none() {
            return None;
        }
        let mut outcomes = self.outcomes.clone();
        outcomes.sort();
        let mut templates = self.templates.clone().unwrap_or_default();
        templates.sort();
        Some(SelectorOutcomeReport {
            outcomes,
            templates,
        })
    }

    /// Fails with a header and one line per error, if there is any.
    pub(super) fn finish(self) -> Result<()> {
        let mut failed = self
            .outcomes
            .into_iter()
            .filter(|outcome| outcome.severity() == Severity::Error)
            .collect::<Vec<_>>();
        if failed.is_empty() {
            return Ok(());
        }
        failed.sort();
        let mut report = String::from(
            "Selector outcome report: in keep-going mode, selectors that did not resolve are \
             left unclaimed so the rest of the chunk can still be checked.\n",
        );
        SelectorOutcomeReport {
            outcomes: failed,
            templates: Vec::new(),
        }
        .render_text(&mut report, Some(HUMAN_OUTCOME_REPORT_LIMIT));
        bail!("{report}")
    }
}
