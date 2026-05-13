//! Back-compat shim for the unified `peel_cli` binary.
//!
//! New callers should use `peel_cli --view inventory` instead. This shim keeps
//! the legacy argv shape (`peel_inventory_cli --graph ... --modules ...
//! [--readable-only] [--limit N] [--by-destination] [--json]`) working so
//! external consumers (e.g. gaffer's tana-peel skill steps that bazel-run this
//! target by name) don't need to be updated atomically.

use std::process::ExitCode;

fn main() -> ExitCode {
    peel_cli::inventory_main()
}
