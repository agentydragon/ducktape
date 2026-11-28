import EsmSplit.CoreJS.AuthenticatedSource
import EsmSplit.CoreJS.IndexedSourceSchedule

namespace CoreJS.IndexedSourceSchedule

private theorem metadataOrdinal {item : SourceUnit} {pair : Nat × List (Name × Name)}
    (selected : SourceUnit.localExport? item = some pair) : pair.1 = item.ordinal := by
  cases item <;>
    simp_all [SourceUnit.localExport?, SourceUnit.ordinal, LinkedInput.SourceItem.localExport?,
      LinkedInput.SourceItem.ordinal] <;>
    cases selected <;>
    rfl

/-- Induction over the actual source-item list preserves the order of every
metadata row, including rows with no public names. -/
theorem metadataOrderFromItems :
    ∀ items : List SourceUnit,
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

/-- The actual decoded export rows retain their source order. -/
theorem metadataRowsOrdered {decoded : AuthenticatedSource.IndexedSource} {bundle : EsmSplit.Bundle}
    (sequence : Sequence decoded bundle) :
    decoded.localExports.Pairwise (fun a b => a.ordinal < b.ordinal) := by
  have itemsOrdered : (sequence.units.map SourceUnit.ordinal).Pairwise (· < ·) := by
    rw [sequence.ordinals]
    exact List.pairwise_lt_range
  have ordered := metadataOrderFromItems sequence.units itemsOrdered
  rw [sequence.metadataSourceRows] at ordered
  simpa only [List.pairwise_map] using ordered

end CoreJS.IndexedSourceSchedule
