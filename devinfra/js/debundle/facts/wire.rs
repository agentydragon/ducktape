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
    ChunkFactAnalysis, EffectCell, RedundantPureMemberHint, RedundantPurityHint, SourceLocation,
    StatementEffectSummary, StatementFacts, StatementKind, StatementOrdinal,
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

/// In-memory mirror of `EffectCell`. Used as a `Globals`-independent
/// carrier so callers can pattern-match without holding the SWC
/// `Globals` that minted the underlying `Id`.
#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd)]
pub enum EffectCellReport {
    Binding { id: IdReport },
    GlobalProp { key: String },
}

impl EffectCellReport {
    pub fn from_cell(cell: &EffectCell) -> Self {
        match cell {
            EffectCell::Binding(id) => EffectCellReport::Binding {
                id: IdReport::from_id(id),
            },
            EffectCell::GlobalProp(key) => EffectCellReport::GlobalProp { key: key.clone() },
        }
    }

    pub fn to_cell(&self) -> EffectCell {
        match self {
            EffectCellReport::Binding { id } => EffectCell::Binding(id.to_id()),
            EffectCellReport::GlobalProp { key } => EffectCell::GlobalProp(key.clone()),
        }
    }
}

/// In-memory mirror of `StatementEffectSummary`.
#[derive(Debug, Clone, Eq, PartialEq)]
pub struct StatementEffectSummaryReport {
    pub writes: Vec<EffectCellReport>,
    pub reads: Vec<EffectCellReport>,
    pub cell_writes_summarizable: bool,
    pub dataflow_summarizable: bool,
}

impl StatementEffectSummaryReport {
    pub fn from_summary(summary: &StatementEffectSummary) -> Self {
        Self {
            writes: summary
                .writes
                .iter()
                .map(EffectCellReport::from_cell)
                .collect(),
            reads: summary
                .reads
                .iter()
                .map(EffectCellReport::from_cell)
                .collect(),
            cell_writes_summarizable: summary.cell_writes_summarizable,
            dataflow_summarizable: summary.dataflow_summarizable,
        }
    }

    pub fn to_summary(&self) -> StatementEffectSummary {
        StatementEffectSummary {
            writes: self.writes.iter().map(EffectCellReport::to_cell).collect(),
            reads: self.reads.iter().map(EffectCellReport::to_cell).collect(),
            cell_writes_summarizable: self.cell_writes_summarizable,
            dataflow_summarizable: self.dataflow_summarizable,
        }
    }
}

/// Wire mirror of `StatementFacts`. Field-by-field 1:1 with the native
/// type — see `facts/mod.rs` for the per-field invariants.
///
/// `Id` sets are mirrored as `Vec<IdReport>` (in `BTreeSet` iteration
/// order); the reverse conversion re-collects into a `BTreeSet`, so set
/// semantics are preserved regardless of the order.
#[derive(Debug, Clone, Eq, PartialEq)]
pub struct StatementFactsReport {
    pub ordinal: StatementOrdinal,
    pub source_location: Option<SourceLocation>,
    pub declared: Vec<IdReport>,
    pub eager_reads: Vec<IdReport>,
    pub eager_rebinds: Vec<IdReport>,
    pub lazy_reads: Vec<IdReport>,
    pub lazy_rebinds: Vec<IdReport>,
    pub first_order_lazy_reads: Vec<IdReport>,
    pub first_order_lazy_rebinds: Vec<IdReport>,
    pub local_effects: Vec<IdReport>,
    pub at_init_calls: Vec<IdReport>,
    pub body_calls: Vec<IdReport>,
    pub first_order_body_calls: Vec<IdReport>,
    pub at_init_unresolved_sources: Vec<IdReport>,
    pub at_init_unresolved_inline_fn: bool,
    pub first_order_unresolved_sources: Vec<IdReport>,
    pub first_order_unresolved_inline_fn: bool,
    pub declares_direct_function: bool,
    pub effects: StatementEffectSummaryReport,
    pub purity: Purity,
    pub kind: StatementKind,
}

impl StatementFactsReport {
    pub fn from_facts(facts: &StatementFacts) -> Self {
        Self {
            ordinal: facts.ordinal,
            source_location: facts.source_location.clone(),
            declared: ids_to_wire(&facts.declared),
            eager_reads: ids_to_wire(&facts.eager_reads),
            eager_rebinds: ids_to_wire(&facts.eager_rebinds),
            lazy_reads: ids_to_wire(&facts.lazy_reads),
            lazy_rebinds: ids_to_wire(&facts.lazy_rebinds),
            first_order_lazy_reads: ids_to_wire(&facts.first_order_lazy_reads),
            first_order_lazy_rebinds: ids_to_wire(&facts.first_order_lazy_rebinds),
            local_effects: ids_to_wire(&facts.local_effects),
            at_init_calls: ids_to_wire(&facts.at_init_calls),
            body_calls: ids_to_wire(&facts.body_calls),
            first_order_body_calls: ids_to_wire(&facts.first_order_body_calls),
            at_init_unresolved_sources: ids_to_wire(&facts.at_init_unresolved_sources),
            at_init_unresolved_inline_fn: facts.at_init_unresolved_inline_fn,
            first_order_unresolved_sources: ids_to_wire(&facts.first_order_unresolved_sources),
            first_order_unresolved_inline_fn: facts.first_order_unresolved_inline_fn,
            declares_direct_function: facts.declares_direct_function,
            effects: StatementEffectSummaryReport::from_summary(&facts.effects),
            purity: facts.purity.clone(),
            kind: facts.kind,
        }
    }

    pub fn to_facts(&self) -> StatementFacts {
        StatementFacts {
            ordinal: self.ordinal,
            source_location: self.source_location.clone(),
            declared: ids_from_wire(&self.declared),
            eager_reads: ids_from_wire(&self.eager_reads),
            eager_rebinds: ids_from_wire(&self.eager_rebinds),
            lazy_reads: ids_from_wire(&self.lazy_reads),
            lazy_rebinds: ids_from_wire(&self.lazy_rebinds),
            first_order_lazy_reads: ids_from_wire(&self.first_order_lazy_reads),
            first_order_lazy_rebinds: ids_from_wire(&self.first_order_lazy_rebinds),
            local_effects: ids_from_wire(&self.local_effects),
            at_init_calls: ids_from_wire(&self.at_init_calls),
            body_calls: ids_from_wire(&self.body_calls),
            first_order_body_calls: ids_from_wire(&self.first_order_body_calls),
            at_init_unresolved_sources: ids_from_wire(&self.at_init_unresolved_sources),
            at_init_unresolved_inline_fn: self.at_init_unresolved_inline_fn,
            first_order_unresolved_sources: ids_from_wire(&self.first_order_unresolved_sources),
            first_order_unresolved_inline_fn: self.first_order_unresolved_inline_fn,
            declares_direct_function: self.declares_direct_function,
            effects: self.effects.to_summary(),
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
