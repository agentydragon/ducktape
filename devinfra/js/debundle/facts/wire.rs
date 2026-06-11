//! In-memory mirror types for per-chunk static facts.
//!
//! The native `StatementFacts` type carries `Id = (Atom, SyntaxContext)`
//! and other SWC types that are awkward to pattern-match against without
//! holding the `Globals` that minted them. This module defines a sibling
//! shape that splits each `Id` into `(name: Atom, ctxt: u32)` so callers
//! (today: `graph.rs::build_owner_graph_with`) can hold and inspect the
//! facts in a single typed value.
//!
//! Naming convention follows `reports/schema.rs`: each `XxxReport`
//! is the in-memory mirror of `Xxx`, with `to_wire()` / `from_wire()`
//! conversion functions.
//!
//! These types are not serialized. An earlier design serialized them
//! as a per-chunk `facts.json` sidecar so a separate-process Stage B
//! could consume Stage A's cached output. That plan was abandoned —
//! the `ctxt` is `Globals`-local and the rejected alternatives are
//! all unsound or value-less. See
//! `docs/lessons_learned/cross_process_stage_b.md`.

use std::collections::BTreeSet;

use swc_atoms::Atom;
use swc_common::SyntaxContext;
use swc_ecma_ast::Id;

use crate::purity::Purity;
use crate::{
    ChunkFactAnalysis, PositionBucketed, RedundantPureMemberHint, RedundantPurityHint,
    SourceLocation, StatementFacts, StatementKind, StatementOrdinal,
};

/// In-memory mirror of `Id = (Atom, SyntaxContext)`. Stored as
/// `(name, ctxt: u32)` instead of the raw `Id` so callers can pattern-
/// match without holding the SWC `Globals` that minted the underlying
/// `SyntaxContext`. The `ctxt` is `Globals`-local; only convert back
/// via [`IdReport::to_id`] inside the same `Globals` that produced
/// the original `Id`.
#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd)]
pub struct IdReport {
    pub name: Atom,
    pub ctxt: u32,
}

impl IdReport {
    pub fn from_id(id: &Id) -> Self {
        Self {
            name: id.0.clone(),
            ctxt: id.1.as_u32(),
        }
    }

    pub fn to_id(&self) -> Id {
        (self.name.clone(), SyntaxContext::from_u32(self.ctxt))
    }
}

/// Wire mirror of `StatementFacts`. Field-by-field 1:1 with the native
/// type — see `facts/mod.rs` for the per-field invariants. The derived
/// `effects()` summary is not mirrored: it reconstructs from the
/// mirrored fields.
///
/// `Id` sets are mirrored as `Vec<IdReport>` (in `BTreeSet` iteration
/// order); the reverse conversion re-collects into a `BTreeSet`, so set
/// semantics are preserved regardless of the order.
#[derive(Debug, Clone, Eq, PartialEq)]
pub struct StatementFactsReport {
    pub ordinal: StatementOrdinal,
    pub source_location: Option<SourceLocation>,
    pub declared: Vec<IdReport>,
    pub reads: PositionBucketed<Vec<IdReport>>,
    pub rebinds: PositionBucketed<Vec<IdReport>>,
    pub calls: PositionBucketed<Vec<IdReport>>,
    pub local_effects: Vec<IdReport>,
    pub at_init_unresolved_sources: Vec<IdReport>,
    pub at_init_unresolved_inline_fn: bool,
    pub first_order_unresolved_sources: Vec<IdReport>,
    pub first_order_unresolved_inline_fn: bool,
    pub declares_direct_function: bool,
    pub global_writes: BTreeSet<String>,
    pub global_reads: BTreeSet<String>,
    pub cell_writes_summarizable: bool,
    pub dataflow_summarizable: bool,
    pub purity: Purity,
    pub kind: StatementKind,
}

impl StatementFactsReport {
    pub fn from_facts(facts: &StatementFacts) -> Self {
        Self {
            ordinal: facts.ordinal,
            source_location: facts.source_location.clone(),
            declared: ids_to_wire(&facts.declared),
            reads: bucketed_to_wire(&facts.reads),
            rebinds: bucketed_to_wire(&facts.rebinds),
            calls: bucketed_to_wire(&facts.calls),
            local_effects: ids_to_wire(&facts.local_effects),
            at_init_unresolved_sources: ids_to_wire(&facts.at_init_unresolved_sources),
            at_init_unresolved_inline_fn: facts.at_init_unresolved_inline_fn,
            first_order_unresolved_sources: ids_to_wire(&facts.first_order_unresolved_sources),
            first_order_unresolved_inline_fn: facts.first_order_unresolved_inline_fn,
            declares_direct_function: facts.declares_direct_function,
            global_writes: facts.global_writes.clone(),
            global_reads: facts.global_reads.clone(),
            cell_writes_summarizable: facts.cell_writes_summarizable,
            dataflow_summarizable: facts.dataflow_summarizable,
            purity: facts.purity.clone(),
            kind: facts.kind,
        }
    }

    pub fn to_facts(&self) -> StatementFacts {
        StatementFacts {
            ordinal: self.ordinal,
            source_location: self.source_location.clone(),
            declared: ids_from_wire(&self.declared),
            reads: bucketed_from_wire(&self.reads),
            rebinds: bucketed_from_wire(&self.rebinds),
            calls: bucketed_from_wire(&self.calls),
            local_effects: ids_from_wire(&self.local_effects),
            at_init_unresolved_sources: ids_from_wire(&self.at_init_unresolved_sources),
            at_init_unresolved_inline_fn: self.at_init_unresolved_inline_fn,
            first_order_unresolved_sources: ids_from_wire(&self.first_order_unresolved_sources),
            first_order_unresolved_inline_fn: self.first_order_unresolved_inline_fn,
            declares_direct_function: self.declares_direct_function,
            global_writes: self.global_writes.clone(),
            global_reads: self.global_reads.clone(),
            cell_writes_summarizable: self.cell_writes_summarizable,
            dataflow_summarizable: self.dataflow_summarizable,
            purity: self.purity.clone(),
            kind: self.kind,
        }
    }
}

/// Top-level chunk-facts in-memory envelope. Mirrors the chunk-wide
/// fields of `ChunkFactAnalysis` (top-level await, redundant-hint
/// diagnostics) and the per-statement records, in a form callers can
/// pass around without needing a SWC `Globals` to interpret.
#[derive(Debug, Clone, Eq, PartialEq)]
pub struct ChunkFactsReport {
    pub facts: Vec<StatementFactsReport>,
    pub top_level_await: Option<StatementOrdinal>,
    pub redundant_purity_hints: Vec<RedundantPurityHint>,
    pub redundant_pure_member_hints: Vec<RedundantPureMemberHint>,
}

impl ChunkFactsReport {
    pub fn from_analysis(analysis: &ChunkFactAnalysis) -> Self {
        Self {
            facts: analysis
                .facts
                .iter()
                .map(StatementFactsReport::from_facts)
                .collect(),
            top_level_await: analysis.top_level_await,
            redundant_purity_hints: analysis.redundant_purity_hints.clone(),
            redundant_pure_member_hints: analysis.redundant_pure_member_hints.clone(),
        }
    }

    pub fn to_analysis(&self) -> ChunkFactAnalysis {
        ChunkFactAnalysis {
            facts: self
                .facts
                .iter()
                .map(StatementFactsReport::to_facts)
                .collect(),
            top_level_await: self.top_level_await,
            redundant_purity_hints: self.redundant_purity_hints.clone(),
            redundant_pure_member_hints: self.redundant_pure_member_hints.clone(),
        }
    }
}

fn ids_to_wire(set: &BTreeSet<Id>) -> Vec<IdReport> {
    set.iter().map(IdReport::from_id).collect()
}

fn ids_from_wire(list: &[IdReport]) -> BTreeSet<Id> {
    list.iter().map(IdReport::to_id).collect()
}

fn bucketed_to_wire(bucketed: &PositionBucketed<BTreeSet<Id>>) -> PositionBucketed<Vec<IdReport>> {
    PositionBucketed {
        eager: ids_to_wire(&bucketed.eager),
        lazy: ids_to_wire(&bucketed.lazy),
        first_order_lazy: ids_to_wire(&bucketed.first_order_lazy),
    }
}

fn bucketed_from_wire(
    bucketed: &PositionBucketed<Vec<IdReport>>,
) -> PositionBucketed<BTreeSet<Id>> {
    PositionBucketed {
        eager: ids_from_wire(&bucketed.eager),
        lazy: ids_from_wire(&bucketed.lazy),
        first_order_lazy: ids_from_wire(&bucketed.first_order_lazy),
    }
}
