//! Shared materialization state for `anonymous_statements[]` claims. Selector
//! matching is handled by the global selector IR solver; this module only
//! carries resolved raw body indices into the planner.

use source_identity::StatementId;

#[derive(Debug, Clone)]
pub(super) struct ResolvedAnonymousStatement {
    pub(super) body_index: StatementId,
    pub(super) comment: Option<String>,
}
