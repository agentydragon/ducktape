use analysis_identity::ANALYSIS_ENGINE_SHA256;
use analysis_thread::run_analysis;
use debundle_cli::run_debundle_cli_from_process;

fn main() -> std::process::ExitCode {
    run_analysis(|| run_debundle_cli_from_process(ANALYSIS_ENGINE_SHA256))
}
