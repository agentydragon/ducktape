//! Mechanical subprocess execution shared by Debundle E2E support.

use std::error::Error;
use std::ffi::OsStr;
use std::fmt;
use std::io::{self, Write};
use std::process::{Command, Output, Stdio};
use std::thread;

const PURITY_COMPLETION_DISAGREEMENT: &str = "purity-completion-disagreement";

/// A child process could not be started or its output could not be collected.
#[derive(Debug)]
pub struct ProcessError {
    operation: &'static str,
    command: String,
    cwd: String,
    source: io::Error,
}

impl fmt::Display for ProcessError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "failed to {} command {} in cwd {}: {}",
            self.operation, self.command, self.cwd, self.source,
        )
    }
}

impl Error for ProcessError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        Some(&self.source)
    }
}

/// Run a child and retain its exact output bytes and exit status.
///
/// A normal nonzero exit is returned in [`Output`]. Only spawn failures are
/// errors, with the requested command and working directory attached.
pub fn output(command: &mut Command) -> Result<Output, ProcessError> {
    let description = describe_command(command);
    let cwd = describe_cwd(command);
    command.output().map_err(|source| ProcessError {
        operation: "spawn or collect output from",
        command: description,
        cwd,
        source,
    })
}

/// Run a child while sending exact input bytes to its stdin and retaining its
/// exact output bytes and exit status. Stdout and stderr are captured into the
/// returned [`Output`].
///
/// The writer runs concurrently with output collection, so a child that writes
/// to stdout or stderr before reading all input cannot fill an unread pipe.
pub fn output_with_input(command: &mut Command, input: &[u8]) -> Result<Output, ProcessError> {
    let description = describe_command(command);
    let cwd = describe_cwd(command);
    let mut child = command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|source| ProcessError {
            operation: "spawn",
            command: description.clone(),
            cwd: cwd.clone(),
            source,
        })?;
    let mut stdin = child.stdin.take().expect("stdin was configured as a pipe");
    let input = input.to_owned();
    let writer = thread::spawn(move || stdin.write_all(&input));
    let output_result = child.wait_with_output();
    let input_result = writer
        .join()
        .unwrap_or_else(|_| Err(io::Error::other("stdin writer thread panicked")));
    let output = output_result.map_err(|source| ProcessError {
        operation: "collect output from",
        command: description.clone(),
        cwd: cwd.clone(),
        source,
    })?;
    match input_result {
        Ok(()) => {}
        Err(source) if source.kind() == io::ErrorKind::BrokenPipe => {}
        Err(source) => {
            return Err(ProcessError {
                operation: "write stdin for",
                command: description,
                cwd,
                source,
            });
        }
    }
    Ok(output)
}

/// Run a Debundle CLI, analyzer or projector and check its shared E2E invariant.
///
/// Keep this explicit at Debundle invocation boundaries. Runtime observations
/// from Node and Bun use [`output`] and remain independent of this check.
pub fn debundle_output(command: &mut Command) -> Result<Output, ProcessError> {
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

/// Assert that a child exited successfully; the panic reports its exit status,
/// stdout and stderr.
#[track_caller]
pub fn assert_success(output: &Output) {
    assert!(
        output.status.success(),
        "{}\nstdout:\n{}\nstderr:\n{}",
        output.status,
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr),
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
    use super::{assert_success, debundle_output, output};
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
    fn output_with_input_preserves_stdin_and_stdout_bytes() {
        let mut command = Command::new("/bin/sh");
        command.args(["-c", "cat"]);
        let result = super::output_with_input(&mut command, &[0xff, b'\n'])
            .expect("spawn shell and send stdin");

        assert!(result.status.success());
        assert_eq!(result.stdout, [0xff, b'\n']);
        assert!(result.stderr.is_empty());
    }

    #[test]
    fn output_with_input_preserves_output_when_child_exits_before_reading_stdin() {
        let mut command = Command::new("/bin/sh");
        command.args(["-c", "printf stdout; printf stderr >&2; exit 17"]);
        let input = vec![b'x'; 1024 * 1024];
        let result = super::output_with_input(&mut command, &input)
            .expect("return output when the child closes stdin early");

        assert_eq!(result.status.code(), Some(17));
        assert_eq!(result.stdout, b"stdout");
        assert_eq!(result.stderr, b"stderr");
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

    #[test]
    #[should_panic(expected = "exit status: 3\nstdout:\nout\nstderr:\nerr")]
    fn assert_success_reports_status_and_both_streams_of_a_failed_child() {
        let mut command = Command::new("/bin/sh");
        command.args(["-c", "printf 'out'; printf 'err' >&2; exit 3"]);
        assert_success(&output(&mut command).expect("spawn shell"));
    }
}
