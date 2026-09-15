use std::env;
use std::ffi::OsString;
use std::fs::File;
use std::io::Write;
use std::os::fd::{FromRawFd, RawFd};
use std::os::unix::process::{CommandExt, ExitStatusExt};
use std::process::{self, Child, Command, ExitStatus};

use anyhow::{Context, Result};
use nix::errno::Errno;
use nix::fcntl::{FcntlArg, FdFlag, fcntl};
use nix::sys::prctl::set_pdeathsig;
use nix::sys::signal::{SigSet, SigmaskHow, Signal, killpg};
use nix::unistd::{Pid, alarm, getppid};

fn signal_group(leader: Pid, signal: Signal) -> Result<()> {
    match killpg(leader, signal) {
        Ok(()) | Err(Errno::ESRCH) => Ok(()),
        result => result.with_context(|| format!("send {signal} to harness group {leader}")),
    }
}

fn wait_for_harness(child: &mut Child, signals: &SigSet) -> Result<ExitStatus> {
    let leader = Pid::from_raw(child.id() as i32);
    let mut stopping = false;
    loop {
        match signals
            .wait()
            .context("wait for harness supervisor signal")?
        {
            Signal::SIGTERM | Signal::SIGUSR1 if !stopping => {
                stopping = true;
                // Give the harness time to persist resume state. The supervisor retains the
                // inherited state-owner descriptor throughout graceful shutdown and escalation.
                alarm::set(5);
                signal_group(leader, Signal::SIGTERM)?;
            }
            Signal::SIGALRM => signal_group(leader, Signal::SIGKILL)?,
            _ => {}
        }
        if let Some(status) = child.try_wait().context("wait for harness")? {
            alarm::cancel();
            // No tool may outlive its native leader: after we exit there is no supervisor left
            // to receive parent-death notification and release an orphan's inherited owner lock.
            signal_group(leader, Signal::SIGKILL)?;
            return Ok(status);
        }
    }
}

fn supervise(mut report: File, program: &OsString, args: &[OsString]) -> Result<i32> {
    let parent = getppid();
    let mut signals = SigSet::empty();
    for signal in [
        Signal::SIGCHLD,
        Signal::SIGTERM,
        Signal::SIGUSR1,
        Signal::SIGALRM,
    ] {
        signals.add(signal);
    }
    // Blocking before spawn makes both exit and stop notifications pending until sigwait
    // consumes them; there is no signal-handler/check/wait race or shared signal-handler state.
    let original_mask = signals.thread_swap_mask(SigmaskHow::SIG_BLOCK)?;
    set_pdeathsig(Signal::SIGUSR1).context("set harness parent-death signal")?;
    if getppid() != parent {
        return Ok(128 + Signal::SIGUSR1 as i32);
    }

    // Only the supervisor reports the PID. Other inherited descriptors (especially the state
    // owner's open file description) deliberately survive exec into the harness and its tools.
    fcntl(&report, FcntlArg::F_SETFD(FdFlag::FD_CLOEXEC))?;
    let mut command = Command::new(program);
    command.args(args).process_group(0);
    // SAFETY: the pre-exec callback only restores the signal mask with pthread_sigmask, an
    // async-signal-safe syscall. It does not allocate or acquire Rust locks after fork.
    unsafe {
        command.pre_exec(move || {
            original_mask
                .thread_set_mask()
                .map_err(std::io::Error::from)
        });
    }
    let mut child = command.spawn().context("start native harness")?;
    let outcome = (|| {
        writeln!(report, "{}", child.id()).context("report harness pid")?;
        drop(report);
        wait_for_harness(&mut child, &signals)
    })();
    if outcome.is_err() {
        // Reporting/waiting failures must not leave a native writer alive after we release our
        // ownership descriptor. The child also retains that descriptor until it exits.
        signal_group(Pid::from_raw(child.id() as i32), Signal::SIGKILL)?;
        child
            .wait()
            .context("reap harness after supervisor failure")?;
    }
    let status = outcome?;
    Ok(status
        .code()
        .unwrap_or_else(|| 128 + status.signal().expect("reaped harness has an exit status")))
}

fn main() {
    let args: Vec<_> = env::args_os().skip(1).collect();
    let [flag, descriptor, program, args @ ..] = args.as_slice() else {
        eprintln!("harness supervisor requires --native-pid-fd FD and a command");
        process::exit(64);
    };
    if flag != "--native-pid-fd" {
        eprintln!("harness supervisor requires --native-pid-fd FD and a command");
        process::exit(64);
    }
    let Some(descriptor) = descriptor
        .to_str()
        .and_then(|value| value.parse::<RawFd>().ok())
        .filter(|value| *value >= 0)
    else {
        eprintln!("harness supervisor received an invalid native pid descriptor");
        process::exit(64);
    };
    // SAFETY: HarnessProcess passes ownership of this open pipe descriptor across exec.
    let report = unsafe { File::from_raw_fd(descriptor) };
    match supervise(report, program, args) {
        Ok(code) => process::exit(code),
        Err(error) => {
            eprintln!("harness supervisor: {error:#}");
            process::exit(125);
        }
    }
}
