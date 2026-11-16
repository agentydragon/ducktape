//! Small real-CLI fixtures for assigning source chunks to logical modules.

use super::{
    Fixture, FixtureOpts, LogicalModuleEntry, RejectedFixture, UnassignedMode,
    run_emit_on_rejection_fixture, run_fixture, run_fixture_with_graphs,
    unassigned_mode_coarse_atomic_groups,
};

/// Builds an ordinary multi-chunk spec and runs it through the `debundle` CLI.
///
/// This intentionally starts from source chunks and logical-module assignments;
/// it does not construct manifests, graph artifacts, or emitted files itself.
pub struct ProgramPlacement<'a> {
    entry_id: &'a str,
    entry_source: &'a str,
    entry_modules: Vec<LogicalModuleEntry>,
    extra_chunks: Vec<(&'a str, &'a str)>,
    extra_modules: Vec<(&'a str, Vec<LogicalModuleEntry>)>,
    unassigned_mode: UnassignedMode,
    dataflow_aware_s_chain: bool,
}

impl<'a> ProgramPlacement<'a> {
    pub fn new(entry_source: &'a str) -> Self {
        Self {
            entry_id: "static/app",
            entry_source,
            entry_modules: Vec::new(),
            extra_chunks: Vec::new(),
            extra_modules: Vec::new(),
            unassigned_mode: unassigned_mode_coarse_atomic_groups(),
            dataflow_aware_s_chain: false,
        }
    }

    pub fn with_entry_id(mut self, id: &'a str) -> Self {
        self.entry_id = id;
        self
    }

    pub fn with_unassigned_mode(mut self, mode: UnassignedMode) -> Self {
        self.unassigned_mode = mode;
        self
    }

    pub fn with_dataflow_aware_s_chain(mut self) -> Self {
        self.dataflow_aware_s_chain = true;
        self
    }

    pub fn place_in_entry(mut self, module: LogicalModuleEntry) -> Self {
        self.entry_modules.push(module);
        self
    }

    pub fn add_chunk(
        mut self,
        id: &'a str,
        source: &'a str,
        modules: Vec<LogicalModuleEntry>,
    ) -> Self {
        assert_ne!(
            id, self.entry_id,
            "entry chunk cannot also be an extra chunk"
        );
        assert!(
            self.extra_chunks
                .iter()
                .all(|(chunk_id, _)| *chunk_id != id),
            "duplicate placed chunk {id}"
        );
        self.extra_chunks.push((id, source));
        self.extra_modules.push((id, modules));
        self
    }

    /// Run the accepted placement through the real CLI.
    pub fn run(self) -> Fixture {
        let opts = self.fixture_opts();
        run_fixture(opts)
    }

    /// Run an accepted placement and export its dependency and program graphs
    /// for graph-backed assertions or follow-up CLI queries.
    pub fn run_with_graphs(self) -> Fixture {
        let opts = self.fixture_opts();
        run_fixture_with_graphs(opts)
    }

    /// Emit the candidate tree even when the real gate rejects its placement.
    pub fn emit_on_rejection(self) -> RejectedFixture {
        let opts = self.fixture_opts();
        run_emit_on_rejection_fixture(opts)
    }

    fn fixture_opts(&self) -> FixtureOpts<'_> {
        let opts = FixtureOpts::new(self.entry_source, self.entry_modules.clone())
            .with_chunk_id(self.entry_id)
            .with_extra_chunks(&self.extra_chunks)
            .with_extra_chunk_logical_modules(&self.extra_modules)
            .with_unassigned_mode(self.unassigned_mode.clone());
        if self.dataflow_aware_s_chain {
            opts.with_dataflow_aware_s_chain()
        } else {
            opts
        }
    }
}
