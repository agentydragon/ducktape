//! Orthogonal read/write query surface for the debundle CLI.
//!
//! These subcommands are the canonical entry points for spec authors
//! and agent tooling. They sit at the top level of the `debundle`
//! CLI (e.g. `debundle binding describe ...`, `debundle scc ...`)
//! rather than under `peel`, so the surface reflects concepts
//! (bindings, SCCs, modules) instead of pipeline internals.
//!
//! All listing commands default to JSON. Pass `--ndjson` for one
//! record per line so the output is pipeable into `jq` / `grep`
//! without extra parsing.

pub mod binding;
pub mod cluster;
pub mod io;
pub mod move_batch;
pub mod scc;
pub mod validation;

use anyhow::{Context, Result};
use clap::Subcommand;

#[derive(Debug, Subcommand)]
pub enum QueryCommand {
    /// Inspect and edit one binding (spec record, source, module assignment).
    #[command(subcommand)]
    Binding(binding::BindingCommand),
    /// Inspect strongly-connected components in the module-quotient graph.
    Scc(scc::SccArgs),
    /// Inspect adjacent modules of a binding or module (1-hop quotient neighbors).
    Cluster(cluster::ClusterArgs),
}

pub fn run_query(command: QueryCommand) -> Result<()> {
    match command {
        QueryCommand::Binding(args) => binding::run(args).context("running binding query"),
        QueryCommand::Scc(args) => scc::run(args).context("running scc query"),
        QueryCommand::Cluster(args) => cluster::run(args).context("running cluster query"),
    }
}
