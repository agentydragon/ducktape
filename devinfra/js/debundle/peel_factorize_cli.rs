//! Back-compat shim for the unified `peel_cli` binary.
//!
//! New callers should use `peel_cli --view factorize` instead. This shim keeps
//! the legacy argv shape (`peel_factorize_cli --graph ... --modules ...
//! [--size-cap-lines N]`) working so external consumers (e.g. gaffer's
//! tana-peel skill steps that bazel-run this target by name) don't need to be
//! updated atomically.

use std::process::ExitCode;

fn main() -> ExitCode {
    peel_cli::factorize_main()
}
