import EsmSplit.CoreJS.Split
import EsmSplit.LocalExportUnit

/-! Ordered local-export rows at their original source ordinals. These facts
start at CoreJS source and planner metadata; frontend provenance is a separate
checked boundary. -/

namespace CoreJS.ExportRows

/-- An original source row is selected only by its original ordinal. -/
def rowAt (rows : List EsmSplit.LocalExportUnit) (id : Nat) : Option EsmSplit.LocalExportUnit :=
  (rows.map fun row => (row.ordinal, row)).lookup id

/-- The source's complete ordered public table comes from these distinct
non-executable local-export units. Mixed inline or default exports require a
separate source representation and cannot be silently dropped. -/
structure SourceRows (source : Module) (rows : List EsmSplit.LocalExportUnit) : Prop where
  exports : source.exports = rows.flatMap (·.names)
  unique : (rows.map (·.ordinal)).Nodup
  absent : ∀ row ∈ rows, row.ordinal ∉ source.body.map Prod.fst

/-- A finite source/plan check: every name published by the source is declared
by an original bundle unit. A public alias does not itself declare a local. -/
def LocalsDeclared (source : Module) (B : EsmSplit.Bundle) : Prop :=
  ∀ name ∈ source.exports.map Prod.snd, ∃ id ∈ List.range B.length, name ∈ (B.stmt id).defs

instance (source : Module) (B : EsmSplit.Bundle) : Decidable (LocalsDeclared source B) := by
  unfold LocalsDeclared
  infer_instance

/-- Source-name declaration coverage applies to every actual row alias,
including repeated aliases of the same local and empty rows. -/
theorem row_locals_declared {source : Module} {rows : List EsmSplit.LocalExportUnit} {B : EsmSplit.Bundle}
    (sourceRows : SourceRows source rows) (declared : LocalsDeclared source B)
    {pair : Name × Name} (member : pair ∈ rows.flatMap (·.names)) :
    ∃ id, id < B.length ∧ pair.2 ∈ (B.stmt id).defs := by
  have inSource : pair ∈ source.exports := by rw [sourceRows.exports]; exact member
  obtain ⟨id, inRange, localDecl⟩ := declared pair.2 (List.mem_map.mpr ⟨pair, inSource, rfl⟩)
  exact ⟨id, List.mem_range.1 inRange, localDecl⟩

/-- The source-name check is exactly the existing per-row declaration check
when the rows are the source publication; no new acceptance condition is
introduced, even for empty rows or repeated public aliases. -/
theorem locals_declared_iff {source : Module} {rows : List EsmSplit.LocalExportUnit} {B : EsmSplit.Bundle}
    (sourceRows : SourceRows source rows) :
    LocalsDeclared source B ↔
      ∀ pair ∈ rows.flatMap (·.names), ∃ id ∈ List.range B.length, pair.2 ∈ (B.stmt id).defs := by
  constructor
  · intro declared pair member
    obtain ⟨id, within, localDecl⟩ := row_locals_declared sourceRows declared member
    exact ⟨id, List.mem_range.2 within, localDecl⟩
  · intro declared name member
    obtain ⟨pair, inSource, same⟩ := List.mem_map.1 member
    have inRows : pair ∈ rows.flatMap (·.names) := by rw [← sourceRows.exports]; exact inSource
    subst name
    exact declared pair inRows

/-- Each authored entry unit either contributes no public exports or exactly
its actual row's ordered planner footprint. -/
def UnitMatches (B : EsmSplit.Bundle) (rows : List EsmSplit.LocalExportUnit) (id : Nat) : Prop :=
  match rowAt rows id with
  | none => (B.stmt id).exports = []
  | some row => row.FootprintMatches (B.stmt id)

instance (B : EsmSplit.Bundle) (rows : List EsmSplit.LocalExportUnit) (id : Nat) :
    Decidable (UnitMatches B rows id) := by
  unfold UnitMatches
  split <;> infer_instance

/-- A singleton row lookup depends only on the original ordinal. -/
theorem rowAt_singleton (row : EsmSplit.LocalExportUnit) (id : Nat) :
    rowAt [row] id = if id = row.ordinal then some row else none := by
  by_cases same : id = row.ordinal
  · subst id
    simp [rowAt, List.lookup_cons]
  · simp [rowAt, List.lookup_cons, beq_eq_false_iff_ne.mpr same, same]

theorem singleton_absent (row : EsmSplit.LocalExportUnit) : ∀ ids : List Nat,
    row.ordinal ∉ ids → ids.filterMap (rowAt [row]) = []
  | [], _ => rfl
  | id :: rest, absent => by
    have different : id ≠ row.ordinal := fun eq => absent (by simp [eq])
    have tail : row.ordinal ∉ rest := fun member => absent (by simp [member])
    simp [rowAt_singleton, different, singleton_absent row rest tail]

/-- Unique planned units select one explicitly owned row exactly once. -/
theorem singleton_selection (row : EsmSplit.LocalExportUnit) : ∀ ids : List Nat,
    ids.Nodup → row.ordinal ∈ ids → ids.filterMap (rowAt [row]) = [row]
  | [], _, owned => by simp at owned
  | id :: rest, unique, owned => by
    by_cases same : id = row.ordinal
    · subst id
      have absent := (List.nodup_cons.1 unique).1
      simp [rowAt_singleton, singleton_absent row rest absent]
    · have inTail : row.ordinal ∈ rest := (List.mem_cons.1 owned).resolve_left (Ne.symm same)
      have tail := singleton_selection row rest (List.nodup_cons.1 unique).2 inTail
      simp [rowAt_singleton, same, tail]

/-- A sole row and otherwise unpublished entry units satisfy the generic
per-unit source/plan check. -/
theorem singleton_matches {B : EsmSplit.Bundle} {owner : EsmSplit.Module}
    {row : EsmSplit.LocalExportUnit} (footprint : row.FootprintMatches (B.stmt row.ordinal))
    (onlyRow : ∀ id ∈ owner.stmts, id ≠ row.ordinal → (B.stmt id).exports = []) :
    ∀ id ∈ owner.stmts, UnitMatches B [row] id := by
  intro id member
  by_cases same : id = row.ordinal
  · subst id
    simpa [UnitMatches, rowAt_singleton] using footprint
  · simpa [UnitMatches, rowAt_singleton, same] using onlyRow id member same

private theorem decode_encoded (names : List (Name × Name)) :
    EsmSplit.localExports (names.map fun (publicName, localName) => (publicName, some localName)) = names := by
  unfold EsmSplit.localExports
  simp only [List.filterMap_map]
  induction names with
  | nil => rfl
  | cons pair rest ih => cases pair <;> simp [Function.comp_def]

/-- Fold the actual entry unit IDs, retaining each matched row's position,
aliases, and multiplicity. This is a source/plan statement; it does not inspect
or compare candidate export tables. -/
theorem fold_entry_exports (B : EsmSplit.Bundle) (rows : List EsmSplit.LocalExportUnit) :
    ∀ ids : List Nat, (∀ id ∈ ids, UnitMatches B rows id) →
      EsmSplit.localExports (ids.flatMap fun id => (B.stmt id).exports) =
        (ids.filterMap (rowAt rows)).flatMap (·.names)
  | [], _ => rfl
  | id :: rest, allMatches => by
    have head := allMatches id (by simp)
    have tail : ∀ j ∈ rest, UnitMatches B rows j := fun j hj => allMatches j (by simp [hj])
    have ih := fold_entry_exports B rows rest tail
    cases selected : rowAt rows id with
    | none =>
      simp only [UnitMatches, selected] at head
      simp only [List.flatMap_cons, head, List.nil_append, List.filterMap_cons, selected,
        ih]
    | some row =>
      simp only [UnitMatches, selected] at head
      simp only [List.flatMap_cons, head.exports, List.filterMap_cons, selected,
        List.flatMap_cons]
      unfold EsmSplit.localExports at *
      rw [List.filterMap_append]
      have decoded := decode_encoded row.names
      unfold EsmSplit.localExports at decoded
      rw [decoded, ih]

/-- The actual planner's entry export metadata is exactly the source-ordered
rows when the checked entry units select precisely those rows, other units are
unpublished, and the planner produces no extra glue exports. -/
theorem exact_entry_exports {B : EsmSplit.Bundle} {owner : EsmSplit.Module}
    {rows : List EsmSplit.LocalExportUnit}
    (unitMatches : ∀ id ∈ owner.stmts, UnitMatches B rows id)
    (orderedRows : owner.stmts.filterMap (rowAt rows) = rows)
    (noGlue : owner.exports = []) : owner.namedExports B = rows.flatMap (·.names) := by
  unfold EsmSplit.Module.namedExports EsmSplit.Module.namedExportsFrom EsmSplit.Module.publishedFrom
  simp only [noGlue, List.nil_append, EsmSplit.Bundle.toArray_getD]
  rw [fold_entry_exports B rows owner.stmts unitMatches, orderedRows]

#print axioms fold_entry_exports
#print axioms exact_entry_exports
#print axioms row_locals_declared
#print axioms locals_declared_iff
#print axioms singleton_selection
#print axioms singleton_matches

end CoreJS.ExportRows
