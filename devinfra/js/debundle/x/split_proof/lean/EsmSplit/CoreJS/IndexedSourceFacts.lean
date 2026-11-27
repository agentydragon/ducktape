import EsmSplit.CoreJS.IndexedCheckedSplit
import EsmSplit.CoreJS.SourceRowOrder

namespace CoreJS.IndexedCheckedSplit

open IndexedSourceSchedule

/-- Project the decoder's source-item trace into CoreJS facts. The row order
is derived by induction over all source items, while coverage supplies
uniqueness and disjointness. No executions or syntax projections are compared. -/
def SourceFacts.fromSequence {decoded : LinkedInput.IndexedSource} {B : EsmSplit.Bundle}
    (sequence : IndexedSourceSchedule.Sequence decoded B) : SourceFacts decoded.program B decoded.localExports := by
  have bodyEq : sequence.source.body = decoded.source.body := by
    rw [← sequence.executableRows, sequence.unitsEq]
    exact decoded.executableProjection.symm
  have coverage : ((decoded.source.body.map Prod.fst) ++ decoded.localExports.map (·.ordinal)).Perm
      (List.range B.length) := by
    simpa [bodyEq] using sequence.sourceCoverage
  have allUnique : ((decoded.source.body.map Prod.fst) ++ decoded.localExports.map (·.ordinal)).Nodup :=
    coverage.nodup_iff.mpr List.nodup_range
  obtain ⟨_, unique, disjoint⟩ := List.nodup_append.mp allUnique
  have rowsOrdered := IndexedSourceSchedule.metadataRowsOrdered sequence
  refine ⟨decoded.source, decoded.singleton, coverage, unique, rowsOrdered, ?_⟩
  intro row member inBody
  have inRows : row.ordinal ∈ decoded.localExports.map (·.ordinal) := List.mem_map.mpr ⟨row, member, rfl⟩
  exact disjoint row.ordinal inBody row.ordinal inRows rfl

#print axioms SourceFacts.fromSequence

end CoreJS.IndexedCheckedSplit
