//! Program-level cross-module function-purity oracle.
//!
//! The per-chunk purity classifier ([`purity::ChunkCodeGraph`])
//! treats a call to an imported binding as `unknown_call` — it cannot see
//! into the other module's body. On a real app bundle that is the dominant
//! impurity (every `memo(fn)` / `forwardRef(fn)` factory wrap), and via the
//! dataflow-aware S-chain it over-merges atomic factor groups.
//!
//! This oracle closes that gap. Given every module's entry body plus its
//! resolved imports and exports, it computes, per module, the purity verdict
//! for each imported function binding — the map fed to that module's
//! [`ChunkCodeGraph::build_full`] (`imported_purities`).
//!
//! ## Algorithm — bottom-up effect closure
//!
//! Canonical body classification emits direct effects, proof dependencies,
//! and exact obligations to inferred local/imported functions. A single typed
//! worklist across all participating modules propagates finite effects through
//! those obligations. Pure is projected only after closure, so recursive pure
//! components remain pure without optimistic verdicts.
//!
//! `Pure` here also means order-free for the importer: a body that reads its
//! module's state, or an import a provider can rebind, gets an
//! `OrderDependentRead` effect (`purity::OrderDependentCells`),
//! because no importer's dependency graph orders that read against the
//! state's writers.
//!
//! Soundness is conservative by construction: an import whose target module or
//! export is absent (external/vendor module not analyzed, a non-function
//! export, an unresolved re-export) simply gets no entry, so the call stays
//! `unknown_call` exactly as before. Pinned assertions remain trusted axioms.

pub mod callable_summaries;
pub mod export_primitives;
pub mod imported_purities;
pub mod module_facts;
pub mod stable_imports;
mod swc_parallel;
#[cfg(test)]
mod tests;
