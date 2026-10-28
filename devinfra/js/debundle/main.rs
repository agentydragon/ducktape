use debundle_cli::run_debundle_cli_from_process;
use swc_common::{GLOBALS, Globals};

fn main() -> std::process::ExitCode {
    // SWC hygiene (`Mark`, `SyntaxContext`) is stored in a thread-local
    // arena managed by `GLOBALS`. Every parse and AST-touching operation
    // in the debundler runs inside this closure so the `resolver` pass
    // can mint marks and downstream consumers can compare `Id`s. A single
    // `Globals` instance per process keeps `Mark` identity comparable
    // across chunks.
    let globals = Globals::default();
    GLOBALS.set(&globals, run_debundle_cli_from_process)
}
