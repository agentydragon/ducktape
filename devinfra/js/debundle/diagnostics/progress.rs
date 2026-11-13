//! Live, invocation-scoped phase observation. Formatting and I/O belong to callers.

#[derive(Debug, Clone, Copy, PartialEq, Eq, strum::Display)]
#[strum(serialize_all = "snake_case")]
pub enum Phase {
    PrepareCrossModulePurity,
    ComputeChunkAnalysis,
    MaterializeLogicalChunk,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, strum::Display)]
#[strum(serialize_all = "snake_case")]
pub enum State {
    Start,
    End,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Event<'a> {
    pub chunk: &'a str,
    pub phase: Phase,
    pub state: State,
}

/// Called synchronously at the phase boundary, including from parallel workers.
/// An observer owns its error policy; command adapters retain output failures.
pub trait Observer: Sync {
    fn observe(&self, event: Event<'_>);
}

impl<F: Fn(Event<'_>) + Sync> Observer for F {
    fn observe(&self, event: Event<'_>) {
        self(event);
    }
}
