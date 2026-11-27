import EsmSplit.CoreJS.IndexedCheckedSplit
import EsmSplit.CoreJS.IndexedSourceSchedule

namespace CoreJS.IndexedCheckedSplit

open IndexedSourceSchedule

private theorem metadataOrdinal {item : SourceUnit} {pair : Nat × List (Name × Name)}
    (selected : SourceUnit.localExport? item = some pair) : pair.1 = item.ordinal := by
  cases item <;> simp_all [SourceUnit.localExport?, SourceUnit.ordinal,
    LinkedInput.SourceItem.localExport?, LinkedInput.SourceItem.ordinal] <;> cases selected <;> rfl

/-- Induction over the actual source-item list preserves the order of every
metadata row, including rows with no public names. -/
theorem metadataOrderFromItems : ∀ items : List SourceUnit,
    (items.map SourceUnit.ordinal).Pairwise (· < ·) →
      (items.filterMap SourceUnit.localExport?).Pairwise (fun a b => a.1 < b.1)
  | [], _ => by simp
  | item :: rest, ordered => by
    have allItems : (item :: rest).Pairwise (fun a b => a.ordinal < b.ordinal) :=
      List.pairwise_map.mp ordered
    obtain ⟨before, tail⟩ := List.pairwise_cons.mp allItems
    have ih := metadataOrderFromItems rest (List.pairwise_map.mpr tail)
    cases selected : SourceUnit.localExport? item with
    | none => simpa [selected] using ih
    | some pair =>
      simp only [List.filterMap_cons, selected, List.pairwise_cons]
      refine ⟨?_, ih⟩
      intro later member
      obtain ⟨other, inRest, found⟩ := List.mem_filterMap.mp member
      rw [metadataOrdinal selected, metadataOrdinal found]
      exact before other inRest

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
  have itemsOrdered : (sequence.units.map SourceUnit.ordinal).Pairwise (· < ·) := by
    rw [sequence.ordinals]
    exact List.pairwise_lt_range
  have rowPairsOrdered := metadataOrderFromItems sequence.units itemsOrdered
  rw [sequence.metadataSourceRows] at rowPairsOrdered
  have rowsOrdered : decoded.localExports.Pairwise (fun a b => a.ordinal < b.ordinal) :=
    by simpa only [List.pairwise_map] using rowPairsOrdered
  refine ⟨decoded.source, decoded.singleton, coverage, unique, rowsOrdered, ?_⟩
  intro row member inBody
  have inRows : row.ordinal ∈ decoded.localExports.map (·.ordinal) := List.mem_map.mpr ⟨row, member, rfl⟩
  exact disjoint row.ordinal inBody row.ordinal inRows rfl

#print axioms metadataOrderFromItems
#print axioms SourceFacts.fromSequence

end CoreJS.IndexedCheckedSplit
