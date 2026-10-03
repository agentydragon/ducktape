use std::env;
use std::ffi::OsString;
use std::fs::{File, OpenOptions};
use std::io::Write;
use std::os::fd::AsRawFd;
use std::os::fd::{FromRawFd, RawFd};
use std::os::unix::process::{CommandExt, ExitStatusExt};
use std::path::PathBuf;
use std::process::{self, Child, Command, ExitStatus};

use anyhow::{Context, Result};
use nix::errno::Errno;
use nix::fcntl::{FcntlArg, FdFlag, fcntl};
use nix::sys::prctl::set_pdeathsig;
use nix::sys::signal::{SigSet, SigmaskHow, Signal, killpg};
use nix::unistd::{Pid, alarm, getppid};

mod pid_line;

use pid_line::format_pid_line;

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

struct AgentIsolation {
    cgroup_procs: File,
    cgroup_kill: File,
    uid: u32,
    gid: u32,
}

fn move_child_into_cgroup(cgroup_procs_fd: RawFd) -> std::io::Result<()> {
    let pid = unsafe { libc::getpid() } as u32;
    let (digits, start) = format_pid_line(pid);
    let bytes = &digits[start..];
    let written = unsafe { libc::write(cgroup_procs_fd, bytes.as_ptr().cast(), bytes.len()) };
    if written != bytes.len() as isize {
        return Err(std::io::Error::last_os_error());
    }
    Ok(())
}

#[repr(C)]
struct CapabilityHeader {
    version: u32,
    pid: i32,
}

#[repr(C)]
#[derive(Clone, Copy, Default)]
struct CapabilityData {
    effective: u32,
    permitted: u32,
    inheritable: u32,
}

unsafe extern "C" {
    fn capset(header: *const CapabilityHeader, data: *const CapabilityData) -> libc::c_int;
}

fn clear_capabilities() -> std::io::Result<()> {
    let header = CapabilityHeader {
        version: 0x2008_0522,
        pid: 0,
    };
    let data = [CapabilityData::default(); 2];
    if unsafe { capset(&header, data.as_ptr()) } != 0 {
        return Err(std::io::Error::last_os_error());
    }
    Ok(())
}

fn drop_agent_privileges(uid: u32, gid: u32) -> std::io::Result<()> {
    // The runner unit receives CAP_KILL to stop a child after it changes UID, plus CAP_SETUID/
    // CAP_SETGID to launch that child. Clear all three before exec so tools cannot regain them.
    if unsafe {
        libc::prctl(
            libc::PR_CAP_AMBIENT,
            libc::PR_CAP_AMBIENT_CLEAR_ALL,
            0,
            0,
            0,
        )
    } != 0
    {
        return Err(std::io::Error::last_os_error());
    }
    if unsafe { libc::setgroups(1, &gid) } != 0 {
        return Err(std::io::Error::last_os_error());
    }
    if unsafe { libc::setgid(gid) } != 0 {
        return Err(std::io::Error::last_os_error());
    }
    if unsafe { libc::setuid(uid) } != 0 {
        return Err(std::io::Error::last_os_error());
    }
    clear_capabilities()?;
    Ok(())
}

fn kill_cgroup(isolation: &mut Option<AgentIsolation>) -> Result<()> {
    if let Some(isolation) = isolation {
        isolation
            .cgroup_kill
            .write_all(b"1\n")
            .context("kill remaining agent cgroup processes")?;
    }
    Ok(())
}

fn supervise(
    mut report: File,
    program: &OsString,
    args: &[OsString],
    mut isolation: Option<AgentIsolation>,
) -> Result<i32> {
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

    // Only the supervisor reports the PID. The state-owner descriptor deliberately survives exec
    // into the harness and its tools; cgroup control descriptors do not.
    fcntl(&report, FcntlArg::F_SETFD(FdFlag::FD_CLOEXEC))?;
    let mut command = Command::new(program);
    command.args(args).process_group(0);
    let cgroup_procs_fd = isolation
        .as_ref()
        .map(|value| value.cgroup_procs.as_raw_fd());
    let agent_uid = isolation.as_ref().map(|value| value.uid);
    let agent_gid = isolation.as_ref().map(|value| value.gid);
    // SAFETY: the pre-exec callback only restores the signal mask with pthread_sigmask, an
    // async-signal-safe syscall, then writes its own pid to an already-open cgroup control file
    // and drops capabilities/identity using direct syscalls. It does not allocate or acquire
    // Rust locks after fork.
    unsafe {
        command.pre_exec(move || {
            original_mask
                .thread_set_mask()
                .map_err(std::io::Error::from)
                .and_then(|()| {
                    if let Some(fd) = cgroup_procs_fd {
                        move_child_into_cgroup(fd)?;
                    }
                    if let (Some(uid), Some(gid)) = (agent_uid, agent_gid) {
                        drop_agent_privileges(uid, gid)?;
                    }
                    Ok(())
                })
        });
    }
    let mut child = command.spawn().context("start native harness")?;
    let outcome = (|| {
        // One write, never `writeln!`: that writes the digits and the newline separately, and
        // the runner closes its end after a single read, so the second write fails with EPIPE.
        // A pipe write this short is atomic; that one read sees the whole line.
        report
            .write_all(format!("{}\n", child.id()).as_bytes())
            .context("report harness pid")?;
        drop(report);
        wait_for_harness(&mut child, &signals)
    })();
    let status = match outcome {
        Ok(status) => status,
        Err(error) => {
            // Reporting/waiting failures must not leave a native writer alive after we release our
            // ownership descriptor. Try both signal routes before waiting so cgroup.kill still fences
            // the child if cross-UID group signalling is unavailable.
            let signal_result = signal_group(Pid::from_raw(child.id() as i32), Signal::SIGKILL);
            let cgroup_result = kill_cgroup(&mut isolation);
            let wait_result = child
                .wait()
                .context("reap harness after supervisor failure");
            if let Err(cleanup_error) = signal_result
                .and(cgroup_result)
                .and(wait_result.map(|_| ()))
            {
                return Err(cleanup_error)
                    .context(format!("clean up after supervisor failure: {error:#}"));
            }
            return Err(error);
        }
    };
    kill_cgroup(&mut isolation)?;
    Ok(status
        .code()
        .unwrap_or_else(|| 128 + status.signal().expect("reaped harness has an exit status")))
}

fn main() {
    let args: Vec<_> = env::args_os().skip(1).collect();
    let mut native_pid_fd = None;
    let mut cgroup_procs = None;
    let mut cgroup_kill = None;
    let mut uid = None;
    let mut gid = None;
    let mut index = 0;
    while index + 1 < args.len() {
        let Some(flag) = args[index].to_str() else {
            break;
        };
        if !flag.starts_with("--") {
            break;
        }
        let value = &args[index + 1];
        match flag {
            "--native-pid-fd" => {
                native_pid_fd = value.to_str().and_then(|value| value.parse::<RawFd>().ok());
            }
            "--cgroup-procs" => cgroup_procs = Some(PathBuf::from(value)),
            "--cgroup-kill" => cgroup_kill = Some(PathBuf::from(value)),
            "--agent-uid" => uid = value.to_str().and_then(|value| value.parse::<u32>().ok()),
            "--agent-gid" => gid = value.to_str().and_then(|value| value.parse::<u32>().ok()),
            _ => break,
        }
        index += 2;
    }
    let Some(descriptor) = native_pid_fd.filter(|value| *value >= 0) else {
        eprintln!("harness supervisor requires --native-pid-fd FD and a command");
        process::exit(64);
    };
    if cgroup_procs.is_some() != cgroup_kill.is_some() || uid.is_some() != gid.is_some() {
        eprintln!("harness supervisor received incomplete agent isolation options");
        process::exit(64);
    }
    if cgroup_procs.is_some() != uid.is_some() {
        eprintln!("harness supervisor requires cgroup and agent identity options together");
        process::exit(64);
    }
    let [program, command_args @ ..] = &args[index..] else {
        eprintln!("harness supervisor requires a command");
        process::exit(64);
    };
    // SAFETY: HarnessProcess passes ownership of this open pipe descriptor across exec.
    let report = unsafe { File::from_raw_fd(descriptor) };
    let isolation = match (cgroup_procs, cgroup_kill, uid, gid) {
        (Some(procs), Some(kill), Some(uid), Some(gid)) => {
            let opened = (|| -> Result<AgentIsolation> {
                Ok(AgentIsolation {
                    cgroup_procs: OpenOptions::new().write(true).open(procs)?,
                    cgroup_kill: OpenOptions::new().write(true).open(kill)?,
                    uid,
                    gid,
                })
            })();
            match opened {
                Ok(isolation) => Some(isolation),
                Err(error) => {
                    eprintln!("harness supervisor: cannot open agent cgroup controls: {error:#}");
                    process::exit(125);
                }
            }
        }
        (None, None, None, None) => None,
        _ => unreachable!("validated isolation options"),
    };
    match supervise(report, program, command_args, isolation) {
        Ok(code) => process::exit(code),
        Err(error) => {
            eprintln!("harness supervisor: {error:#}");
            process::exit(125);
        }
    }
}
