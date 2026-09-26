//! Shared materialization state for `anonymous_statements[]` claims. Selector
//! matching is handled by the global selector IR solver; this module only
//! carries resolved ordinals into the planner.

#[derive(Debug, Clone)]
pub(super) struct ResolvedAnonymousStatement {
    pub(super) ordinal: usize,
    pub(super) comment: Option<String>,
}
