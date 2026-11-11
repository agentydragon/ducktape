//! The analysis recurses as deep as the analyzed call graph, so its stack is
//! sized here rather than left to the host's main-thread default.

use swc_common::{GLOBALS, Globals};

/// The depth-64 e2e cases (`soundness_at_init_value_flow_test`,
/// `synchronous_precision_report_test`) overflow at 4 MiB and pass at 8 MiB
/// in the test build; this is 4× the passing size.
const STACK_BYTES: usize = 32 << 20;

/// Runs `body` on a thread with the analysis stack, inside one SWC
/// `GLOBALS`. SWC hygiene (`Mark`, `SyntaxContext`) lives in that
/// thread-local arena: every parse and AST-touching operation runs inside it
/// so the `resolver` pass can mint marks, and one `Globals` per process keeps
/// `Mark` identity comparable across chunks.
pub fn run_analysis<R: Send>(body: impl FnOnce() -> R + Send) -> R {
    std::thread::scope(|scope| {
        std::thread::Builder::new()
            .stack_size(STACK_BYTES)
            .spawn_scoped(scope, || GLOBALS.set(&Globals::default(), body))
            .expect("spawn the analysis thread")
            .join()
            .unwrap_or_else(|panic| std::panic::resume_unwind(panic))
    })
}
