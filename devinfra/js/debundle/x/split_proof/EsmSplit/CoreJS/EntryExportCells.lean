import EsmSplit.CoreJS.Split

/-! Source/plan facts for the live cells available to a generated module.
An export list can publish a local declaration or an imported binding even
when no executable statement mentions that name. -/

namespace CoreJS.EntryExportCells

/-- A declared source name has an address in the original bundle scope. -/
theorem bundle_decl_cell {ss : List Stmt} {B : EsmSplit.Bundle}
    (footprints : FootprintsOfSyntax ss B) (uniqueDefs : EsmSplit.UniqueDefs B)
    {frames : List (Nat × List (Name × Addr))}
    (frameNames : FramesOf ((List.range ss.length).zip ss) frames)
    {j : Nat} {u : Name} (within : j < ss.length) (declared : u ∈ (B.stmt j).defs) :
    ∃ a, (bundleEnv ss frames).vars.lookup u = some a := by
  have inBundle : ∃ p ∈ (bundleModule ss).body, p.1 = j := by
    refine ⟨(j, ss[j]), ?_, rfl⟩
    show (j, ss[j]) ∈ (List.range ss.length).zip ss
    rw [← filterMap_numbered_range, mem_numbered]
    exact ⟨List.mem_range.2 within, by simp [within]⟩
  obtain ⟨a, ha⟩ : ∃ a, (frameAt frames j).lookup u = some a :=
    Option.ne_none_iff_exists'.1 fun h =>
      (lookup_none_iff_not_mem_keys.mp h) ((frame_keys footprints frameNames).2 ⟨within, declared⟩)
  refine ⟨a, ?_⟩
  show ((bundleModule ss).scope frames ++ []).lookup u = some a
  rw [List.append_nil, scope_lookup_decl footprints frameNames uniqueDefs inBundle within declared, ha]

/-- A name that a plan module declares or imports has an original source cell. -/
theorem original_cell_of_bound {ss : List Stmt} {B : EsmSplit.Bundle} {S : EsmSplit.Spec} {P : EsmSplit.Plan}
    (valid : EsmSplit.Valid B S P) (footprints : FootprintsOfSyntax ss B)
    {frames : List (Nat × List (Name × Addr))}
    (frameNames : FramesOf ((List.range ss.length).zip ss) frames)
    {md : EsmSplit.Module} (moduleIn : md ∈ P.mods) {u : Name}
    (bound : (∃ j ∈ md.stmts, u ∈ (B.stmt j).defs) ∨ u ∈ md.imports.flatMap (·.2)) :
    ∃ a, (bundleEnv ss frames).vars.lookup u = some a := by
  rcases bound with ⟨j, hj, hu⟩ | imported
  · have within : j < ss.length := footprints.1 ▸ valid.2.2.1.2.2.2.2.2.1 md moduleIn j hj
    exact bundle_decl_cell footprints valid.1 frameNames within hu
  · obtain ⟨e, he, hu⟩ := List.mem_flatMap.1 imported
    obtain ⟨_, _, a, _, _, _, _, _, original⟩ :=
      split_import_original_cell valid footprints frameNames moduleIn he hu
    exact ⟨a, original⟩

/-- Every binding declared or imported by a generated module resolves to the
same cell as the original bundle scope. This applies to export-list locals,
not just names occurring in executable statements. Imports are resolved by
the actual `splitProgram` linker. -/
theorem module_scope_cells {ss : List Stmt} {B : EsmSplit.Bundle} {S : EsmSplit.Spec} {P : EsmSplit.Plan}
    (valid : EsmSplit.Valid B S P) (footprints : FootprintsOfSyntax ss B)
    {frames : List (Nat × List (Name × Addr))}
    (frameNames : FramesOf ((List.range ss.length).zip ss) frames)
    {md : EsmSplit.Module} (moduleIn : md ∈ P.mods) :
    ∃ cells, (splitProgram ss B P).imported frames (moduleOf ss B P md).imports = .ok cells ∧
      ∀ u, (∃ j ∈ md.stmts, u ∈ (B.stmt j).defs) ∨ u ∈ md.imports.flatMap (·.2) →
        ((moduleOf ss B P md).scope frames ++ cells).lookup u = (bundleEnv ss frames).vars.lookup u := by
  have hv := valid
  obtain ⟨uniqueDefs, -, faithful, linkable, -, -, -, -, -⟩ := valid
  have within : ∀ j ∈ md.stmts, j < ss.length := fun j hj =>
    footprints.1 ▸ faithful.2.2.2.2.2.1 md moduleIn j hj
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
        split_import_original_cell hv footprints frameNames moduleIn he hu
      exact ⟨a, cell, original⟩
  obtain ⟨cells, linked, lookup⟩ := imported
  refine ⟨cells, linked, ?_⟩
  intro u admitted
  rw [List.lookup_append]
  by_cases declared : ∃ j ∈ md.stmts, u ∈ (B.stmt j).defs
  · obtain ⟨j, hj, hu⟩ := declared
    have hjn := within j hj
    have inModule : ∃ p ∈ (moduleOf ss B P md).body, p.1 = j :=
      ⟨(j, ss[j]), (mem_numbered ss).2 ⟨hj, by simp [hjn]⟩, rfl⟩
    have inBundle : ∃ p ∈ (bundleModule ss).body, p.1 = j := by
      refine ⟨(j, ss[j]), ?_, rfl⟩
      show (j, ss[j]) ∈ (List.range ss.length).zip ss
      rw [← filterMap_numbered_range, mem_numbered]
      exact ⟨List.mem_range.2 hjn, by simp [hjn]⟩
    have someCell : ∃ a, (frameAt frames j).lookup u = some a :=
      Option.ne_none_iff_exists'.1 fun h =>
        (lookup_none_iff_not_mem_keys.mp h) ((frame_keys footprints frameNames).2 ⟨hjn, hu⟩)
    obtain ⟨a, ha⟩ := someCell
    rw [scope_lookup_decl footprints frameNames uniqueDefs inModule hjn hu, ha, Option.some_or]
    show some a = ((bundleModule ss).scope frames ++ []).lookup u
    rw [List.append_nil, scope_lookup_decl footprints frameNames uniqueDefs inBundle hjn hu, ha]
  · have noneLocal : ∀ j ∈ md.stmts, u ∉ (B.stmt j).defs := fun j hj hu => declared ⟨j, hj, hu⟩
    have noScope : ((moduleOf ss B P md).scope frames).lookup u = none :=
      scope_lookup_none footprints frameNames fun p hp => noneLocal p.1 ((mem_numbered ss).1 hp).1
    have isImport : u ∈ md.imports.flatMap (·.2) := admitted.resolve_left declared
    rw [noScope, Option.none_or, lookup]
    simp [isImport]

/-- If the plan binds every local named by a module's exports, the generated
module resolves every such export local to the original bundle cell. The
premise is a source/plan name-resolution condition; it does not inspect or
compare the candidate's execution. -/
theorem module_export_cells {ss : List Stmt} {B : EsmSplit.Bundle} {S : EsmSplit.Spec} {P : EsmSplit.Plan}
    (valid : EsmSplit.Valid B S P) (footprints : FootprintsOfSyntax ss B)
    {frames : List (Nat × List (Name × Addr))}
    (frameNames : FramesOf ((List.range ss.length).zip ss) frames)
    {md : EsmSplit.Module} (moduleIn : md ∈ P.mods)
    (bound : ∀ e ∈ md.namedExports B,
      (∃ j ∈ md.stmts, e.2 ∈ (B.stmt j).defs) ∨ e.2 ∈ md.imports.flatMap (·.2)) :
    ∃ cells, (splitProgram ss B P).imported frames (moduleOf ss B P md).imports = .ok cells ∧
      ∀ e ∈ (moduleOf ss B P md).exports, ∃ a,
        (bundleEnv ss frames).vars.lookup e.2 = some a ∧
        ((moduleOf ss B P md).scope frames ++ cells).lookup e.2 = some a := by
  obtain ⟨cells, linked, agreement⟩ := module_scope_cells valid footprints frameNames moduleIn
  refine ⟨cells, linked, ?_⟩
  intro e he
  obtain ⟨a, original⟩ := original_cell_of_bound valid footprints frameNames moduleIn (bound e he)
  exact ⟨a, original, (agreement e.2 (bound e he)).trans original⟩

/-- The actual scopes constructed for all loaded modules have the original
cell for every local declaration and every generated import. In particular,
the property includes imported names used only by an export list. -/
theorem linked_scope_cells {ss : List Stmt} {B : EsmSplit.Bundle} {S : EsmSplit.Spec} {P : EsmSplit.Plan}
    (valid : EsmSplit.Valid B S P) (footprints : FootprintsOfSyntax ss B)
    {frames : List (Nat × List (Name × Addr))}
    (frameNames : FramesOf ((List.range ss.length).zip ss) frames) :
    ∃ linked, (splitProgram ss B P).scopes frames (splitProgram ss B P).loaded = .ok linked ∧
      ∀ p ∈ linked, ∃ md ∈ P.mods, p.1 = moduleOf ss B P md ∧
        ∀ u, (∃ j ∈ md.stmts, u ∈ (B.stmt j).defs) ∨ u ∈ md.imports.flatMap (·.2) →
          p.2.vars.lookup u = (bundleEnv ss frames).vars.lookup u := by
  obtain ⟨linked, hlinked, _, every⟩ := scopes_ok (splitProgram ss B P) frames
    (fun m cells => ∃ md ∈ P.mods, m = moduleOf ss B P md ∧
      ∀ u, (∃ j ∈ md.stmts, u ∈ (B.stmt j).defs) ∨ u ∈ md.imports.flatMap (·.2) →
        ((moduleOf ss B P md).scope frames ++ cells).lookup u = (bundleEnv ss frames).vars.lookup u)
    (splitProgram ss B P).loaded (by
      intro m hm
      obtain ⟨name, _, found⟩ := List.mem_filterMap.1 hm
      rw [split_find] at found
      obtain ⟨md, hfind, rfl⟩ := Option.map_eq_some_iff.1 found
      have hmd : md ∈ P.mods := List.mem_of_find?_eq_some hfind
      obtain ⟨cells, hcells, hagree⟩ := module_scope_cells valid footprints frameNames hmd
      exact ⟨cells, hcells, md, hmd, rfl, hagree⟩)
  refine ⟨linked, hlinked, ?_⟩
  intro p hp
  obtain ⟨cells, ⟨md, hmd, hm, hagree⟩, henv⟩ := every p hp
  refine ⟨md, hmd, hm, ?_⟩
  intro u hu
  rw [henv, hm]
  exact hagree u hu

/-- For each generated export local that the source plan binds, the actual
linked module scope and the bundle scope contain the same live address. -/
theorem linked_export_cells {ss : List Stmt} {B : EsmSplit.Bundle} {S : EsmSplit.Spec} {P : EsmSplit.Plan}
    (valid : EsmSplit.Valid B S P) (footprints : FootprintsOfSyntax ss B)
    {frames : List (Nat × List (Name × Addr))}
    (frameNames : FramesOf ((List.range ss.length).zip ss) frames) :
    ∃ linked, (splitProgram ss B P).scopes frames (splitProgram ss B P).loaded = .ok linked ∧
      ∀ p ∈ linked, ∃ md ∈ P.mods, p.1 = moduleOf ss B P md ∧
        ∀ e ∈ md.namedExports B,
          ((∃ j ∈ md.stmts, e.2 ∈ (B.stmt j).defs) ∨ e.2 ∈ md.imports.flatMap (·.2)) →
          ∃ a, (bundleEnv ss frames).vars.lookup e.2 = some a ∧ p.2.vars.lookup e.2 = some a := by
  obtain ⟨linked, hlinked, every⟩ := linked_scope_cells valid footprints frameNames
  refine ⟨linked, hlinked, ?_⟩
  intro p hp
  obtain ⟨md, hmd, hm, agreement⟩ := every p hp
  refine ⟨md, hmd, hm, ?_⟩
  intro e _ bound
  obtain ⟨a, original⟩ := original_cell_of_bound valid footprints frameNames hmd bound
  exact ⟨a, original, (agreement e.2 bound).trans original⟩

/-- The generated module preserves both aliasing and distinctness of its
source-plan-bound names. Equality here is equality of addresses, not values. -/
theorem linked_alias_iff {ss : List Stmt} {B : EsmSplit.Bundle} {S : EsmSplit.Spec} {P : EsmSplit.Plan}
    (valid : EsmSplit.Valid B S P) (footprints : FootprintsOfSyntax ss B)
    {frames : List (Nat × List (Name × Addr))}
    (frameNames : FramesOf ((List.range ss.length).zip ss) frames) :
    ∃ linked, (splitProgram ss B P).scopes frames (splitProgram ss B P).loaded = .ok linked ∧
      ∀ p ∈ linked, ∃ md ∈ P.mods, p.1 = moduleOf ss B P md ∧
        ∀ u v, ((∃ j ∈ md.stmts, u ∈ (B.stmt j).defs) ∨ u ∈ md.imports.flatMap (·.2)) →
          ((∃ j ∈ md.stmts, v ∈ (B.stmt j).defs) ∨ v ∈ md.imports.flatMap (·.2)) →
          (p.2.vars.lookup u = p.2.vars.lookup v ↔
            (bundleEnv ss frames).vars.lookup u = (bundleEnv ss frames).vars.lookup v) := by
  obtain ⟨linked, hlinked, every⟩ := linked_scope_cells valid footprints frameNames
  refine ⟨linked, hlinked, ?_⟩
  intro p hp
  obtain ⟨md, hmd, hm, agreement⟩ := every p hp
  refine ⟨md, hmd, hm, ?_⟩
  intro u v hu hv
  rw [agreement u hu, agreement v hv]


end CoreJS.EntryExportCells
