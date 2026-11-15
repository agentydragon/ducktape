//! Mechanical subprocess execution shared by Debundle E2E support.

use std::error::Error;
use std::ffi::OsStr;
use std::fmt;
use std::io;
use std::process::{Command, Output};

const PURITY_COMPLETION_DISAGREEMENT: &str = "purity-completion-disagreement";

/// Details for a child process that could not be spawned.
#[derive(Debug)]
pub struct SpawnError {
    command: String,
    cwd: String,
    source: io::Error,
}

impl fmt::Display for SpawnError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "failed to spawn command {} in cwd {}: {}",
            self.command, self.cwd, self.source
        )
    }
}

impl Error for SpawnError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        Some(&self.source)
    }
}

/// Run a child and retain its exact output bytes and exit status.
///
/// A normal nonzero exit is returned in [`Output`]. Only spawn failures are
/// errors, with the requested command and working directory attached.
pub fn output(command: &mut Command) -> Result<Output, SpawnError> {
    let description = describe_command(command);
    let cwd = describe_cwd(command);
    command.output().map_err(|source| SpawnError {
        command: description,
        cwd,
        source,
    })
}

/// Run a Debundle CLI, analyzer or projector and check its shared E2E invariant.
///
/// Keep this explicit at Debundle invocation boundaries. Runtime observations
/// from Node and Bun use [`output`] and remain independent of this check.
pub fn debundle_output(command: &mut Command) -> Result<Output, SpawnError> {
    let output = output(command)?;
    assert_debundle_invariants(&output);
    Ok(output)
}

/// Reject a Debundle diagnostic that indicates classifier-pure execution did
/// not complete synchronously, regardless of which Debundle binary emitted it.
pub fn assert_debundle_invariants(output: &Output) {
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        !stderr.contains(PURITY_COMPLETION_DISAGREEMENT),
        "classifier-pure completion regression is an e2e bug: {stderr}"
    );
}

fn describe_command(command: &Command) -> String {
    let mut parts = vec![format_os_str(command.get_program())];
    parts.extend(command.get_args().map(format_os_str));
    parts.join(" ")
}

fn format_os_str(value: &OsStr) -> String {
    format!("{value:?}")
}

fn describe_cwd(command: &Command) -> String {
    match command.get_current_dir() {
        Some(path) => format!("{path:?}"),
        None => match std::env::current_dir() {
            Ok(path) => format!("inherited {path:?}"),
            Err(_) => "inherited (unknown)".to_owned(),
        },
    }
}

#[cfg(test)]
mod tests {
    use super::{debundle_output, output};
    use std::path::Path;
    use std::process::Command;

    #[test]
    fn output_preserves_zero_status_and_non_utf8_stream_bytes() {
        let mut command = Command::new("/bin/sh");
        command.args(["-c", "printf '\\377'; printf '\\376' >&2"]);
        let result = output(&mut command).expect("spawn shell");

        assert!(result.status.success());
        assert_eq!(result.stdout, [0xff]);
        assert_eq!(result.stderr, [0xfe]);
    }

    #[test]
    fn output_returns_normal_nonzero_status_and_bytes() {
        let mut command = Command::new("/bin/sh");
        command.args(["-c", "printf 'out'; printf 'err' >&2; exit 17"]);
        let result = output(&mut command).expect("spawn shell");

        assert_eq!(result.status.code(), Some(17));
        assert_eq!(result.stdout, b"out");
        assert_eq!(result.stderr, b"err");
    }

    #[test]
    fn spawn_error_names_command_and_requested_cwd() {
        let cwd = std::env::current_dir().expect("test cwd");
        let mut command = Command::new("debundle-process-runner-missing-command");
        command.current_dir(&cwd).arg("--test-argument");
        let error = output(&mut command).expect_err("missing executable must fail to spawn");
        let diagnostic = error.to_string();

        assert!(diagnostic.contains("debundle-process-runner-missing-command"));
        assert!(diagnostic.contains("--test-argument"));
        assert!(diagnostic.contains(&format!("{cwd:?}")));
        assert!(diagnostic.contains("No such file or directory"));
    }

    #[test]
    #[should_panic(expected = "classifier-pure completion regression is an e2e bug")]
    fn debundle_output_applies_the_shared_invariant_to_real_process_output() {
        let mut command = Command::new("/bin/sh");
        command.args(["-c", "printf 'purity-completion-disagreement' >&2; exit 1"]);
        debundle_output(&mut command).expect("spawn shell");
    }

    #[test]
    #[should_panic(expected = "classifier-pure completion regression is an e2e bug")]
    fn saved_analysis_adapter_guards_staged_tool_output() {
        use debundle_e2e_support::saved_analysis::command_with;

        command_with(
            Path::new("/bin/sh"),
            &["-c", "printf 'purity-completion-disagreement' >&2; exit 1"],
        );
    }

    #[test]
    fn debundle_output_returns_nonzero_output_when_invariant_holds() {
        let mut command = Command::new("/bin/sh");
        command.args(["-c", "printf 'rejected'; exit 3"]);
        let result = debundle_output(&mut command).expect("spawn shell");

        assert_eq!(result.status.code(), Some(3));
        assert_eq!(result.stdout, b"rejected");
    }
}
