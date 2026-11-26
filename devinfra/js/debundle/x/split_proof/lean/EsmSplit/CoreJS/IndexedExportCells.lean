import EsmSplit.CoreJS.LinkedCells

/-! Cell identity for original-indexed CoreJS bodies, including a hole for a
non-executable export-list unit. The source body and planner bundle retain the
original ordinals. This is a CoreJS proof, not parser or printer correspondence. -/

namespace CoreJS.IndexedExportCells

/-- Facts about the actual original executable body and full source bundle.
No ordinal is inferred from a compact list position. -/
structure Footprints (body : List (Nat × Stmt)) (B : EsmSplit.Bundle) : Prop where
  uniqueIds : (body.map Prod.fst).Nodup
  statement : ∀ id stmt, body.lookup id = some stmt →
    id < B.length ∧ ∀ name, name ∈ (B.stmt id).defs ↔ name ∈ stmt.bindings.map Prod.fst

variable {body : List (Nat × Stmt)} {B : EsmSplit.Bundle}
  {frames : List (Nat × List (Name × Addr))}

/-- Source lookup works for every actual executable unit, without assuming
that the original IDs are consecutive. -/
theorem body_lookup (footprints : Footprints body B) {p : Nat × Stmt} (member : p ∈ body) :
    body.lookup p.1 = some p.2 := by
  apply lookup_of_mem member
  intro other same
  exact nodup_keys footprints.uniqueIds same member

/-- The actual allocation frame has exactly the source declaration names at
an original executable ID. IDs absent from the source have no frame. -/
theorem frame_keys (footprints : Footprints body B) (frameNames : FramesOf body frames)
    {id : Nat} {name : Name} :
    name ∈ (frameAt frames id).map Prod.fst ↔
      ∃ stmt, body.lookup id = some stmt ∧ name ∈ (B.stmt id).defs := by
  have h := congrArg (List.lookup id) frameNames
  have lhs := lookup_map_snd (fun f : List (Name × Addr) => f.map Prod.fst) id frames
  have rhs := lookup_map_snd (fun s : Stmt => s.bindings.map Prod.fst) id body
  have corresponding := lhs.symm.trans (h.trans rhs)
  cases original : body.lookup id with
  | none =>
    rw [original] at corresponding
    have noFrame : frames.lookup id = none := by simpa using corresponding
    simp [frameAt, noFrame]
  | some stmt =>
    rw [original] at corresponding
    obtain ⟨f, hf, keys⟩ := Option.map_eq_some_iff.1 corresponding
    simp only [frameAt, hf, Option.getD_some, keys]
    constructor
    · intro member
      exact ⟨stmt, rfl, ((footprints.statement id stmt original).2 name).2 member⟩
    · rintro ⟨other, found, declared⟩
      cases found
      exact ((footprints.statement id stmt original).2 name).1 declared

/-- A module without a source declaration for a name has no local frame
for it, including when its body omits an interior source ordinal. -/
theorem scope_lookup_none (footprints : Footprints body B) (frameNames : FramesOf body frames)
    {m : Module} {name : Name} (absent : ∀ p ∈ m.body, name ∉ (B.stmt p.1).defs) :
    (m.scope frames).lookup name = none := by
  rw [scope_eq]
  refine lookup_flatMap _ _ (fun p hp => Or.inl (lookup_none_iff_not_mem_keys.mpr fun hx => absent p hp ?_)) (Or.inl rfl)
  obtain ⟨_, _, declared⟩ := (frame_keys footprints frameNames).1 hx
  exact declared

/-- A module containing the original declaration identity resolves the binding
to that unit's allocation frame. -/
theorem scope_lookup_decl (footprints : Footprints body B) (frameNames : FramesOf body frames)
    (unique : EsmSplit.UniqueDefs B) {m : Module} {id : Nat} {name : Name} {stmt : Stmt}
    (inModule : (id, stmt) ∈ m.body) (inSource : body.lookup id = some stmt)
    (declared : name ∈ (B.stmt id).defs) :
    (m.scope frames).lookup name = (frameAt frames id).lookup name := by
  rw [scope_eq]
  refine lookup_flatMap _ _ (fun p _ => ?_) (Or.inr ?_)
  · by_cases has : name ∈ (frameAt frames p.1).map Prod.fst
    · obtain ⟨other, found, otherDecl⟩ := (frame_keys footprints frameNames).1 has
      right
      have otherBound := (footprints.statement p.1 other found).1
      have sourceBound := (footprints.statement id stmt inSource).1
      rw [unique_defs unique otherBound sourceBound otherDecl declared]
    · exact Or.inl (lookup_none_iff_not_mem_keys.mpr has)
  · exact ⟨(id, stmt), inModule, rfl⟩

/-- The same original declaration in the source and a generated module is
resolved to one present live address, regardless of its position in either
body. The module contains the original declaration unit. -/
theorem source_and_module_cell (footprints : Footprints body B) (frameNames : FramesOf body frames)
    (unique : EsmSplit.UniqueDefs B) {source m : Module} (sourceBody : source.body = body)
    {id : Nat} {name : Name} {stmt : Stmt} (inSource : body.lookup id = some stmt)
    (inModule : (id, stmt) ∈ m.body)
    (declared : name ∈ (B.stmt id).defs) :
    ∃ a, (source.scope frames).lookup name = some a ∧ (m.scope frames).lookup name = some a := by
  have member : (id, stmt) ∈ source.body := by
    rw [sourceBody]
    exact lookup_mem inSource
  have inFrame : ∃ a, (frameAt frames id).lookup name = some a :=
    Option.ne_none_iff_exists'.1 fun none =>
      (lookup_none_iff_not_mem_keys.mp none) ((frame_keys footprints frameNames).2 ⟨stmt, inSource, declared⟩)
  obtain ⟨a, address⟩ := inFrame
  exact ⟨a, (scope_lookup_decl footprints frameNames unique member inSource declared).trans address,
    (scope_lookup_decl footprints frameNames unique inModule inSource declared).trans address⟩

/-- An actual program import of a provider's public alias resolves to the
original source cell. The module lookup and alias lookup are structural facts
of the constructor's plan, not an execution comparison. -/
theorem source_and_import_cell (footprints : Footprints body B) (frameNames : FramesOf body frames)
    (unique : EsmSplit.UniqueDefs B) {source provider : Module} (sourceBody : source.body = body)
    {id : Nat} {name publicName : Name} {stmt : Stmt} (inSource : body.lookup id = some stmt)
    (inProvider : (id, stmt) ∈ provider.body) (declared : name ∈ (B.stmt id).defs)
    {program : Program} {providerName : String} (findProvider : program.find providerName = some provider)
    (publicLookup : provider.exports.lookup publicName = some name) :
    ∃ a, (source.scope frames).lookup name = some a ∧
      program.importCell frames providerName publicName = .ok a := by
  obtain ⟨a, sourceCell, providerCell⟩ :=
    source_and_module_cell footprints frameNames unique sourceBody inSource inProvider declared
  exact ⟨a, sourceCell, Program.importCell_local findProvider publicLookup providerCell⟩

/-- Distinct source names cannot share an allocated address, even when their
initialized values happen to be equal. -/
theorem distinct_source_cells {w : World} {s s' : State w}
    (allocated : declareTops body s = .ok frames s') {source : Module} {x y : Name} {a b : Addr}
    (different : x ≠ y) (xCell : (source.scope frames).lookup x = some a)
    (yCell : (source.scope frames).lookup y = some b) : a ≠ b := by
  intro same
  subst b
  exact different (declareTops_scope_injective allocated source xCell yCell)

#print axioms body_lookup
#print axioms frame_keys
#print axioms scope_lookup_none
#print axioms scope_lookup_decl
#print axioms source_and_module_cell
#print axioms source_and_import_cell
#print axioms distinct_source_cells

end CoreJS.IndexedExportCells
