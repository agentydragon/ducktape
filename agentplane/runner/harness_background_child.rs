use std::io::{self, Write};

use nix::sys::signal::{SigHandler, Signal, signal};
use nix::unistd::{ForkResult, fork, pause};

// Native-harness stand-in verifying inherited ownership across leader exit.
fn main() -> Result<(), Box<dyn std::error::Error>> {
    // SAFETY: this process is single-threaded. The child only calls pause after fork, and signal
    // dispositions use constants, not Rust callbacks. Set SIG_IGN before fork so the published
    // child PID also proves it is ready to ignore SIGTERM; the leader restores default handling.
    unsafe {
        signal(Signal::SIGTERM, SigHandler::SigIgn)?;
        match fork()? {
            ForkResult::Child => loop {
                pause();
            },
            ForkResult::Parent { child } => {
                signal(Signal::SIGTERM, SigHandler::SigDfl)?;
                println!("{child}");
                io::stdout().flush()?;
            }
        }
    }
    loop {
        pause();
    }
}
