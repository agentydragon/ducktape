//! Shared materialization state for `anonymous_statements[]` claims and
//! diagnostics. Selector matching is handled by the global selector IR solver;
//! this module only carries resolved ordinals into the planner and renders
//! keep-going failures.

#[derive(Debug, Clone)]
pub(super) struct ResolvedAnonymousStatement {
    pub(super) ordinal: usize,
    pub(super) comment: Option<String>,
}

#[derive(Debug, Clone)]
pub(super) struct AnonymousStatementDiagnostic {
    pub(super) module_id: String,
    pub(super) selector: spec::AnonymousStatementSelector,
    pub(super) message: String,
}

impl AnonymousStatementDiagnostic {
    pub(super) fn render(&self) -> String {
        format!("module {}: {}", self.module_id, self.message)
    }
}
