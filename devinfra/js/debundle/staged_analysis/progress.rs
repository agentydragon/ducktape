use std::io::Write;

const DEBUNDLE_PROGRESS_ENV: &str = "DUCKTAPE_DEBUNDLE_PROGRESS";

fn debundle_progress_enabled() -> bool {
    std::env::var(DEBUNDLE_PROGRESS_ENV)
        .ok()
        .is_some_and(|raw| {
            !matches!(
                raw.to_ascii_lowercase().as_str(),
                "" | "0" | "false" | "off" | "no"
            )
        })
}

pub fn emit_debundle_progress(chunk_id: &str, phase: &str, state: &str) {
    if !debundle_progress_enabled() {
        return;
    }
    eprintln!("[debundle progress] chunk={chunk_id} phase={phase} state={state}");
    let _ = std::io::stderr().flush();
}
