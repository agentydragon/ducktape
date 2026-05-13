//! Back-compat shim for the unified `peel_cli` binary.
//!
//! New callers should use `peel_cli --view horizon` instead. This shim keeps
//! the legacy argv shape (`peel_horizon_cli --graph ... --modules ...
//! [--limit N] [--near-missing N] [--max-companions N] [--json]`) working so
//! external consumers (e.g. gaffer's tana-peel skill steps that bazel-run this
//! target by name) don't need to be updated atomically.

use std::process::ExitCode;

fn main() -> ExitCode {
    peel_cli::horizon_main()
}
