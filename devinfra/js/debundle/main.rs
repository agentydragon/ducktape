use analysis_identity::ANALYSIS_ENGINE_SHA256;
use debundle_cli::run_debundle_cli_from_process;
use swc_common::{GLOBALS, Globals};

fn main() -> std::process::ExitCode {
    let bytes: usize = std::env::var("DEBUNDLE_PROBE_STACK_BYTES")
        .ok()
        .and_then(|value| value.parse().ok())
        .unwrap_or(64 << 20);
    std::thread::Builder::new()
        .stack_size(bytes)
        .spawn(|| {
            let globals = Globals::default();
            GLOBALS.set(&globals, || {
                run_debundle_cli_from_process(ANALYSIS_ENGINE_SHA256)
            })
        })
        .expect("spawn the analysis thread")
        .join()
        .expect("the analysis thread panicked")
}
