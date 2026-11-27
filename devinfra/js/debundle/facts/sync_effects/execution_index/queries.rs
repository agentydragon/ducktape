//! Read-only queries over a collected `ExecutionIndex`.

use super::{Call, CallKind, ExecutionIndex};
use source_identity::SourceUnitOrdinal;
use std::sync::Arc;
use swc_common::Span;
use swc_ecma_ast::*;
use value_model::CallSite;

impl ExecutionIndex {
    /// Whether `callable`, or an arrow or other callable nested in it, reads
    /// `this`. An unindexed or opaque callable may.
    pub fn reads_this(&self, callable: u32) -> bool {
        let Some(span) = self.callable_spans.get(&callable) else {
            return true;
        };
        self.callables.get(&callable).is_none_or(|body| body.opaque)
            || self
                .callable_spans
                .range(span.lo.0..span.hi.0)
                .any(|(nested, _)| {
                    self.callables
                        .get(nested)
                        .is_some_and(|nested| nested.implicit_capture)
                })
    }

    /// The bindings referenced in `[lo, hi)`.
    pub fn references_within(&self, lo: u32, hi: u32) -> impl Iterator<Item = &Id> {
        let start = self
            .ident_references
            .partition_point(|(position, _)| *position < lo);
        self.ident_references[start..]
            .iter()
            .take_while(move |(position, _)| *position < hi)
            .map(|(_, id)| id)
    }

    /// A source binding used exactly once, at this member read. A second use
    /// could alias, delete, redefine or publish the instance before the read.
    pub fn sole_binding_reference_at(&self, binding: &Id, span: Span) -> bool {
        self.dynamic_scope_owners.is_empty()
            && !self.reassigned_bindings.contains(binding)
            && !self.binding_target_writes.contains(binding)
            && !self.exported_bindings.contains(binding)
            && !self.imported_bindings.contains(binding)
            && self
                .binding_declaration_writes
                .get(binding)
                .is_some_and(|writes| matches!(writes.as_slice(), [write] if write.direct_top_level && write.simple_identifier))
            && self.binding_use_bounds.get(binding).is_some_and(|(first, last)| {
                first == last && span.lo.0 <= *first && *first < span.hi.0
            })
    }

    /// This member operation belongs to a direct module-level initializer,
    /// whose expression is evaluated at most once. A lexical use inside a
    /// callable can execute again after its first result exposes the instance.
    pub fn single_eager_initializer_read(&self, call: &Call) -> bool {
        call.caller.is_none()
            && self.top_calls[call.owner.0].iter().any(|indexed| {
                indexed.kind == CallKind::LocalMemberRead
                    && indexed.span == call.span
                    && indexed.caller.is_none()
            })
            && self
                .binding_declaration_writes
                .values()
                .flatten()
                .any(|write| {
                    write.owner == call.owner
                        && write.direct_top_level
                        && write
                            .initializer_span
                            .is_some_and(|span| span.contains(call.span))
                })
    }

    pub fn unconditionally_evaluated_member_read(&self, call: &Call) -> bool {
        !self
            .conditional_top_member_reads
            .contains(&CallSite::from(call.span))
    }

    pub fn statement_span(&self, owner: SourceUnitOrdinal) -> Span {
        self.original_statement_spans[owner.0]
    }

    pub fn locate_native_integrity_witnesses(
        &mut self,
        mut locate: impl FnMut(Span) -> Option<analysis_vocabulary::execution_evidence::SourceLocation>,
    ) {
        if self
            .native_integrity_witnesses
            .iter()
            .any(|witness| witness.chunk_id.is_empty())
        {
            for witness in Arc::make_mut(&mut self.native_integrity_witnesses) {
                if witness.chunk_id.is_empty() {
                    witness.source_location = locate(witness.span);
                }
            }
        }
        for mutation in Arc::make_mut(&mut self.native_prototype_mutations) {
            if mutation.chunk_id.is_empty() {
                mutation.source_location = locate(mutation.span);
            }
        }
    }
}
