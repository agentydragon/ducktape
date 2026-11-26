import EsmSplit.CoreJS.Split
import EsmSplit.CoreJS.LinkedCells

/-! Live import cells for a full source plan containing trailing metadata.
Executable statements retain dense original IDs; later metadata cannot declare
bindings. This is a CoreJS source/plan contract, not a parser theorem. -/

namespace CoreJS.MetadataCells

/-- Source footprint facts for the executable prefix and declaration-free
trailing units. No statement or source ID is removed from the full bundle. -/
structure Footprints (ss : List Stmt) (B : EsmSplit.Bundle) : Prop where
  withinBundle : ss.length ≤ B.length
  statement : ∀ i s, ss[i]? = some s →
    (∀ x, x ∈ (B.stmt i).defs ↔ x ∈ s.bindings.map (·.1)) ∧
    ∀ x ∈ s.free, x ∈ (B.stmt i).defs ∨ x ∈ (B.stmt i).eager ++ (B.stmt i).lazy
  metadata : ∀ i, ss.length ≤ i → i < B.length → (B.stmt i).defs = []

variable {ss : List Stmt} {B : EsmSplit.Bundle} {P : EsmSplit.Plan}
  {frames : List (Nat × List (Name × Addr))}

theorem frame_keys (footprints : Footprints ss B) (hF : FramesOf ((List.range ss.length).zip ss) frames)
    {j : Nat} {x : Name} : x ∈ (frameAt frames j).map (·.1) ↔ j < ss.length ∧ x ∈ (B.stmt j).defs := by
  have h := frames_lookup hF j
  cases hs : ss[j]? with
  | none =>
    rw [hs] at h
    have : frames.lookup j = none := by simpa using h
    simp only [frameAt, this, Option.getD_none, List.map_nil, List.not_mem_nil, false_iff]
    rintro ⟨hj, _⟩
    rw [List.getElem?_eq_getElem hj] at hs
    cases hs
  | some s =>
    rw [hs] at h
    obtain ⟨f, hf, hfk⟩ := Option.map_eq_some_iff.1 h
    simp only [frameAt, hf, Option.getD_some, hfk, ← (footprints.statement j s hs).1 x]
    exact ⟨fun hx => ⟨(List.getElem?_eq_some_iff.1 hs).1, hx⟩, fun h => h.2⟩

/-- No matching source declaration in a module body means no local cell. -/
theorem scope_lookup_none (footprints : Footprints ss B) (hF : FramesOf ((List.range ss.length).zip ss) frames)
    {M : Module} {x : Name} (absent : ∀ p ∈ M.body, x ∉ (B.stmt p.1).defs) :
    (M.scope frames).lookup x = none := by
  rw [scope_eq]
  refine lookup_flatMap _ _ (fun p hp => Or.inl (lookup_none_iff_not_mem_keys.mpr fun hx => absent p hp ?_)) (Or.inl rfl)
  exact ((frame_keys footprints hF).1 hx).2

theorem scope_lookup_decl (footprints : Footprints ss B) (hF : FramesOf ((List.range ss.length).zip ss) frames)
    (unique : EsmSplit.UniqueDefs B) {M : Module} {x : Name} {j : Nat}
    (member : ∃ p ∈ M.body, p.1 = j) (within : j < ss.length) (declared : x ∈ (B.stmt j).defs) :
    (M.scope frames).lookup x = (frameAt frames j).lookup x := by
  rw [scope_eq]
  refine lookup_flatMap _ _ (fun p _ => ?_) (Or.inr ?_)
  · by_cases hk : x ∈ (frameAt frames p.1).map (·.1)
    · obtain ⟨hp, hxp⟩ := (frame_keys footprints hF).1 hk
      right
      rw [unique_defs unique (Nat.lt_of_lt_of_le hp footprints.withinBundle)
        (Nat.lt_of_lt_of_le within footprints.withinBundle) hxp declared]
    · exact Or.inl (lookup_none_iff_not_mem_keys.mpr hk)
  · obtain ⟨p, hp, rfl⟩ := member
    exact ⟨p, hp, rfl⟩

/-- Every executable source declaration has an actual original bundle cell. -/
theorem bundle_decl_cell (footprints : Footprints ss B) (unique : EsmSplit.UniqueDefs B)
    (hF : FramesOf ((List.range ss.length).zip ss) frames)
    {j : Nat} {u : Name} (within : j < ss.length) (declared : u ∈ (B.stmt j).defs) :
    ∃ a, (bundleEnv ss frames).vars.lookup u = some a := by
  have inBundle : ∃ p ∈ (bundleModule ss).body, p.1 = j := by
    refine ⟨(j, ss[j]), ?_, rfl⟩
    show (j, ss[j]) ∈ (List.range ss.length).zip ss
    rw [← filterMap_numbered_range, mem_numbered]
    exact ⟨List.mem_range.2 within, by simp [within]⟩
  obtain ⟨a, cell⟩ : ∃ a, (frameAt frames j).lookup u = some a :=
    Option.ne_none_iff_exists'.1 fun h =>
      (lookup_none_iff_not_mem_keys.mp h) ((frame_keys footprints hF).2 ⟨within, declared⟩)
  refine ⟨a, ?_⟩
  show ((bundleModule ss).scope frames ++ []).lookup u = some a
  rw [List.append_nil, scope_lookup_decl footprints hF unique inBundle within declared, cell]

/-- Every actual generated import resolves to its original source cell, with
its full-plan declaring owner and authored original ordinal. -/
theorem split_import_original_cell {S : EsmSplit.Spec} (valid : EsmSplit.Valid B S P)
    (footprints : Footprints ss B) (hF : FramesOf ((List.range ss.length).zip ss) frames)
    {md : EsmSplit.Module} (moduleIn : md ∈ P.mods) {e : String × List Name} (edge : e ∈ md.imports)
    {u : Name} (nameIn : u ∈ e.2) :
    ∃ owner j a, P.find e.1 = some owner ∧ j ∈ owner.stmts ∧
      S.claimants B j = [owner.name] ∧ u ∈ (B.stmt j).defs ∧
      (splitProgram ss B P).importCell frames e.1 (EsmSplit.exportNameFor (P.namedExports B e.1) u) = .ok a ∧
      (bundleEnv ss frames).vars.lookup u = some a := by
  obtain ⟨unique, -, faithful, linkable, exportsUnique, -, -, -, -⟩ := valid
  obtain ⟨declares, exports⟩ := ((linkable md moduleIn).1 e edge).2.2 u nameIn
  unfold EsmSplit.Plan.declaresIn at declares
  unfold EsmSplit.Plan.exportsName at exports
  cases found : P.find e.1 with
  | none => rw [found] at declares; cases declares
  | some owner =>
    rw [found] at declares exports
    have ownerIn : owner ∈ P.mods := List.mem_of_find?_eq_some found
    obtain ⟨j, owned, hasName⟩ := List.any_eq_true.1 (by simpa using declares : owner.declares B u = true)
    have hasName := List.contains_iff_mem.1 hasName
    have jBundle := faithful.2.2.2.2.2.1 owner ownerIn j owned
    have jExecutable : j < ss.length := by
      by_cases executable : j < ss.length
      · exact executable
      · have empty := footprints.metadata j (Nat.le_of_not_gt executable) jBundle
        rw [empty] at hasName
        simp at hasName
    have someCell : ∃ a, (frameAt frames j).lookup u = some a :=
      Option.ne_none_iff_exists'.1 fun hn =>
        (lookup_none_iff_not_mem_keys.mp hn) ((frame_keys footprints hF).2 ⟨jExecutable, hasName⟩)
    obtain ⟨a, cell⟩ := someCell
    have inOwner : ∃ p ∈ (moduleOf ss B P owner).body, p.1 = j :=
      ⟨(j, ss[j]), (mem_numbered ss).2 ⟨owned, by simp [jExecutable]⟩, rfl⟩
    have inBundle : ∃ p ∈ (bundleModule ss).body, p.1 = j := by
      refine ⟨(j, ss[j]), ?_, rfl⟩
      show (j, ss[j]) ∈ (List.range ss.length).zip ss
      rw [← filterMap_numbered_range, mem_numbered]
      exact ⟨List.mem_range.2 jExecutable, by simp [jExecutable]⟩
    refine ⟨owner, j, a, rfl, owned, (faithful.2.2.2.2.1 owner ownerIn j (List.mem_range.2 jBundle)).1 owned,
      hasName, ?_, ?_⟩
    · rw [EsmSplit.Plan.namedExports, found]
      simp only [Option.map_some, Option.getD_some]
      exact Program.importCell_local (by rw [split_find, found, Option.map_some])
        (moduleOf_exports owner (by simpa using exports) (exportsUnique owner ownerIn))
        ((scope_lookup_decl footprints hF unique inOwner jExecutable hasName).trans cell)
    · show ((bundleModule ss).scope frames ++ []).lookup u = some a
      rw [List.append_nil, scope_lookup_decl footprints hF unique inBundle jExecutable hasName, cell]

/-- The actual imports succeed and every name declared or imported by the
module has the original bundle lookup, including export-only local uses. -/
theorem module_scope_cells {S : EsmSplit.Spec} (valid : EsmSplit.Valid B S P)
    (footprints : Footprints ss B) (hF : FramesOf ((List.range ss.length).zip ss) frames)
    {md : EsmSplit.Module} (moduleIn : md ∈ P.mods) :
    ∃ cells, (splitProgram ss B P).imported frames (moduleOf ss B P md).imports = .ok cells ∧
      ∀ u, (∃ j ∈ md.stmts, u ∈ (B.stmt j).defs) ∨ u ∈ md.imports.flatMap (·.2) →
        ((moduleOf ss B P md).scope frames ++ cells).lookup u = (bundleEnv ss frames).vars.lookup u := by
  have hv := valid
  obtain ⟨unique, -, faithful, linkable, -, -, -, -, -⟩ := valid
  have within : ∀ j ∈ md.stmts, ∀ u ∈ (B.stmt j).defs, j < ss.length := by
    intro j hj u hu
    by_cases executable : j < ss.length
    · exact executable
    · have bound := faithful.2.2.2.2.2.1 md moduleIn j hj
      have empty := footprints.metadata j (Nat.le_of_not_gt executable) bound
      rw [empty] at hu
      simp at hu
  have imported : ∃ cells,
      (splitProgram ss B P).imported frames (moduleOf ss B P md).imports = .ok cells ∧
      ∀ u, cells.lookup u = if u ∈ md.imports.flatMap (·.2) then (bundleEnv ss frames).vars.lookup u else none := by
    apply imported_ok (splitProgram ss B P) frames ((bundleEnv ss frames).vars.lookup ·)
      (fun src u => EsmSplit.exportNameFor (P.namedExports B src) u) md.imports
    intro e he
    refine ⟨?_, ?_⟩
    · rw [split_find, Option.isSome_map]
      exact ((linkable md moduleIn).1 e he).2.1
    · intro u hu
      obtain ⟨_, _, a, _, _, _, _, cell, original⟩ :=
        split_import_original_cell hv footprints hF moduleIn he hu
      exact ⟨a, cell, original⟩
  obtain ⟨cells, linked, lookup⟩ := imported
  refine ⟨cells, linked, ?_⟩
  intro u admitted
  rw [List.lookup_append]
  by_cases declared : ∃ j ∈ md.stmts, u ∈ (B.stmt j).defs
  · obtain ⟨j, hj, hu⟩ := declared
    have hjn := within j hj u hu
    have inModule : ∃ p ∈ (moduleOf ss B P md).body, p.1 = j :=
      ⟨(j, ss[j]), (mem_numbered ss).2 ⟨hj, by simp [hjn]⟩, rfl⟩
    have inBundle : ∃ p ∈ (bundleModule ss).body, p.1 = j := by
      refine ⟨(j, ss[j]), ?_, rfl⟩
      show (j, ss[j]) ∈ (List.range ss.length).zip ss
      rw [← filterMap_numbered_range, mem_numbered]
      exact ⟨List.mem_range.2 hjn, by simp [hjn]⟩
    have someCell : ∃ a, (frameAt frames j).lookup u = some a :=
      Option.ne_none_iff_exists'.1 fun h =>
        (lookup_none_iff_not_mem_keys.mp h) ((frame_keys footprints hF).2 ⟨hjn, hu⟩)
    obtain ⟨a, ha⟩ := someCell
    rw [scope_lookup_decl footprints hF unique inModule hjn hu, ha, Option.some_or]
    show some a = ((bundleModule ss).scope frames ++ []).lookup u
    rw [List.append_nil, scope_lookup_decl footprints hF unique inBundle hjn hu, ha]
  · have noneLocal : ∀ j ∈ md.stmts, u ∉ (B.stmt j).defs := fun j hj hu => declared ⟨j, hj, hu⟩
    have noScope : ((moduleOf ss B P md).scope frames).lookup u = none :=
      scope_lookup_none footprints hF fun p hp => noneLocal p.1 ((mem_numbered ss).1 hp).1
    have isImport : u ∈ md.imports.flatMap (·.2) := admitted.resolve_left declared
    rw [noScope, Option.none_or, lookup]
    simp [isImport]

/-- Two generated imports of one binding retain the same live address. -/
theorem import_alias {S : EsmSplit.Spec} (valid : EsmSplit.Valid B S P)
    (footprints : Footprints ss B) (hF : FramesOf ((List.range ss.length).zip ss) frames)
    {m₁ m₂ : EsmSplit.Module} (hm₁ : m₁ ∈ P.mods) (hm₂ : m₂ ∈ P.mods)
    {e₁ e₂ : String × List Name} (he₁ : e₁ ∈ m₁.imports) (he₂ : e₂ ∈ m₂.imports)
    {u : Name} (hu₁ : u ∈ e₁.2) (hu₂ : u ∈ e₂.2) :
    ∃ a, (bundleEnv ss frames).vars.lookup u = some a ∧
      (splitProgram ss B P).importCell frames e₁.1 (EsmSplit.exportNameFor (P.namedExports B e₁.1) u) = .ok a ∧
      (splitProgram ss B P).importCell frames e₂.1 (EsmSplit.exportNameFor (P.namedExports B e₂.1) u) = .ok a := by
  obtain ⟨_, _, a, _, _, _, _, ca, sa⟩ := split_import_original_cell valid footprints hF hm₁ he₁ hu₁
  obtain ⟨_, _, b, _, _, _, _, cb, sb⟩ := split_import_original_cell valid footprints hF hm₂ he₂ hu₂
  rw [sa] at sb
  cases sb
  exact ⟨a, sa, ca, cb⟩

/-- Distinct declared names resolve to different live cells, regardless of
whether their initialized values happen to be equal. -/
theorem import_distinct {S : EsmSplit.Spec} (valid : EsmSplit.Valid B S P)
    (footprints : Footprints ss B) (hF : FramesOf ((List.range ss.length).zip ss) frames)
    {w : World} {s s' : State w} (allocated : declareTops ((List.range ss.length).zip ss) s = .ok frames s')
    {m₁ m₂ : EsmSplit.Module} (hm₁ : m₁ ∈ P.mods) (hm₂ : m₂ ∈ P.mods)
    {e₁ e₂ : String × List Name} (he₁ : e₁ ∈ m₁.imports) (he₂ : e₂ ∈ m₂.imports)
    {u v : Name} (hu : u ∈ e₁.2) (hv : v ∈ e₂.2) (different : u ≠ v) :
    ∃ a b, a ≠ b ∧
      (splitProgram ss B P).importCell frames e₁.1 (EsmSplit.exportNameFor (P.namedExports B e₁.1) u) = .ok a ∧
      (splitProgram ss B P).importCell frames e₂.1 (EsmSplit.exportNameFor (P.namedExports B e₂.1) v) = .ok b := by
  obtain ⟨_, _, a, _, _, _, _, ca, sa⟩ := split_import_original_cell valid footprints hF hm₁ he₁ hu
  obtain ⟨_, _, b, _, _, _, _, cb, sb⟩ := split_import_original_cell valid footprints hF hm₂ he₂ hv
  refine ⟨a, b, ?_, ca, cb⟩
  intro same
  subst b
  exact declareTops_bundleEnv_disjoint allocated different a sa sb

#print axioms bundle_decl_cell
#print axioms frame_keys
#print axioms scope_lookup_none
#print axioms scope_lookup_decl
#print axioms split_import_original_cell
#print axioms module_scope_cells
#print axioms import_alias
#print axioms import_distinct

end CoreJS.MetadataCells
