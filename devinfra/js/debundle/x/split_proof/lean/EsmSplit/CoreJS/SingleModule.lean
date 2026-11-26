import EsmSplit.CoreJS.LinkedBehavior
import EsmSplit.CoreJS.Move
import EsmSplit.CoreJS.Split

namespace CoreJS

private theorem sameUnits_refl : ∀ units : List CoreJS.LinkedUnit, CoreJS.SameUnits units units
  | [] => .nil
  | (_, stmt, env) :: rest => .cons ⟨rfl, rfl, CoreJS.Agree.refl stmt.free env⟩ (sameUnits_refl rest)

/-- Renaming an import-free singleton module or changing its export table leaves
its execution unchanged. Its original body identities determine allocation. -/
theorem runProgram_singleModuleMetadata (body : List (Nat × CoreJS.Stmt))
    (hbody : body.Pairwise fun a b => a.1 ≤ b.1) (name : String) (exports : List (String × String))
    (W : CoreJS.World) (fuel : Nat) :
    CoreJS.runProgram W fuel
      ({ entry := name, modules := [{ name, imports := [], body, exports }] } : CoreJS.Program) =
    CoreJS.runProgram W fuel
      ({ entry := "main", modules := [{ name := "main", imports := [], body, exports := [] }] } : CoreJS.Program) := by
  let sourceModule : CoreJS.Module := { name, imports := [], body, exports }
  let targetModule : CoreJS.Module := { name := "main", imports := [], body, exports := [] }
  let source : CoreJS.Program := { entry := name, modules := [sourceModule] }
  let target : CoreJS.Program := { entry := "main", modules := [targetModule] }
  change CoreJS.runProgram W fuel source = CoreJS.runProgram W fuel target
  have sourceLoaded : source.loaded = [sourceModule] := by
    simp [CoreJS.Program.loaded, CoreJS.Program.evalOrder, CoreJS.Program.visit,
      EsmSplit.visitImports, CoreJS.Program.find, source, sourceModule]
  have targetLoaded : target.loaded = [targetModule] := by
    simp [CoreJS.Program.loaded, CoreJS.Program.evalOrder, CoreJS.Program.visit,
      EsmSplit.visitImports, CoreJS.Program.find, target, targetModule]
  have hregions : source.regions = target.regions := by
    simp [CoreJS.Program.regions, source, target, sourceModule, targetModule]
  have htops : source.tops = target.tops := by
    simp [CoreJS.Program.tops, CoreJS.Program.loaded, CoreJS.Program.evalOrder, CoreJS.Program.visit, EsmSplit.visitImports,
      CoreJS.Program.find, source, target, sourceModule, targetModule]
  have hscopes : CoreJS.ScopesAgree source target := by
    intro frames _
    let env : CoreJS.Env := { vars := sourceModule.scope frames, this := .undef }
    have envEq : env = { vars := targetModule.scope frames, this := .undef } := by
      simp [env, sourceModule, targetModule, CoreJS.Module.scope]
    refine ⟨[(sourceModule, env)], [(targetModule, env)], ?_, ?_, ?_, ?_⟩
    · rw [sourceLoaded]
      simp [CoreJS.Program.scopes, CoreJS.Program.imported, sourceModule, env] <;> rfl
    · rw [targetLoaded]
      simp [CoreJS.Program.scopes, CoreJS.Program.imported, targetModule, env, envEq] <;> rfl
    · simp only [CoreJS.unitsOf, List.flatMap_cons, List.flatMap_nil, List.append_nil]
      change CoreJS.SameUnits
        (sourceModule.body.map fun (id, stmt) => (id, stmt, env))
        (targetModule.body.map fun (id, stmt) => (id, stmt, env))
      rw [show targetModule.body = sourceModule.body by rfl]
      exact sameUnits_refl _
    · simp only [CoreJS.unitsOf, List.flatMap_cons, List.flatMap_nil, List.append_nil]
      change (sourceModule.body.map fun (id, stmt) => (id, stmt, env)).Pairwise
        fun a b : CoreJS.LinkedUnit => a.1 ≤ b.1
      exact (List.pairwise_map
        (f := fun p : Nat × CoreJS.Stmt => (p.1, p.2, env))
        (R := fun a b : CoreJS.LinkedUnit => a.1 ≤ b.1)).2 hbody
  exact CoreJS.run_same hregions htops hscopes fuel

#print axioms runProgram_singleModuleMetadata

private theorem declareConstTops_ok {W : World} (tops : List (Nat × Stmt))
    (consts : ∀ p ∈ tops, ∃ name value, p.2 = .decl .const name value) (s : State W)
    (regions : ∀ p ∈ tops, p.1 + 1 < s.cells.size) :
    ∃ frames ready, declareTops tops s = .ok frames ready ∧ ready.cells.size = s.cells.size := by
  induction tops generalizing s with
  | nil => exact ⟨[], s, rfl, rfl⟩
  | cons p rest ih =>
    obtain ⟨id, statement⟩ := p
    obtain ⟨name, value, rfl⟩ := consts (id, statement) (by simp)
    have inside := regions (id, .decl .const name value) (by simp)
    let cells := s.cells[id + 1]'inside
    let next : State W := { s with cells := s.cells.set! (id + 1) (cells.push ⟨none, false⟩), region := 0 }
    have size : next.cells.size = s.cells.size := by simp [next, Array.set!]
    let frame := [(name, (⟨id + 1, cells.size⟩ : Addr))]
    have allocated : inRegion id (newCells (Stmt.decl .const name value).bindings) s = .ok frame next := by
      change inRegion id (newCells [(name, ⟨none, false⟩)]) s = .ok frame next
      simp [inRegion, newCells, newCell, M.bind_apply, modifyState, pure,
        Array.getElem?_eq_getElem inside, cells, frame, next] <;> rfl
    obtain ⟨frames, ready, tail, readySize⟩ := ih
      (fun p hp => consts p (List.mem_cons_of_mem _ hp)) next
      (fun p hp => size.symm ▸ regions p (List.mem_cons_of_mem _ hp))
    refine ⟨(id, frame) :: frames, ready, ?_, readySize.trans size⟩
    rw [declareTops, M.bind_apply, allocated]
    change (declareTops rest >>= fun frames => pure ((id, frame) :: frames)) next = _
    rw [M.bind_apply, tail] <;> rfl

private theorem initFunctions_const {W : World} (units : List LinkedUnit)
    (consts : ∀ u ∈ units, ∃ name value, u.stmt = .decl .const name value) :
    (initFunctions units : M W Unit) = pure () := by
  induction units with
  | nil => rfl
  | cons u rest ih =>
    obtain ⟨id, statement, env⟩ := u
    obtain ⟨name, value, eq⟩ := consts (id, statement, env) (by simp)
    change statement = .decl .const name value at eq
    subst statement
    simp only [initFunctions, initFunction, M.pure_bind,
      ih (fun u hu => consts u (List.mem_cons_of_mem _ hu))]

/-- Link the original singleton declaration module with its explicit public/local
export table. Its actual allocation frames are available to compare entry lookups
and to construct the linked scopes of an order-preserving plan. Const initializers
are only evaluated after linking. -/
theorem link_singleModuleConst (name : Name) (exports : List (Name × Name))
    (ss : List Stmt) (consts : ∀ s ∈ ss, ∃ x value, s = .decl .const x value) (w : World) :
    let m : Module := ⟨name, [], (List.range ss.length).zip ss, exports⟩
    let source : Program := ⟨name, [m]⟩
    ∃ (frames : List (Nat × List (Name × Addr))) (before : LinkedBehavior.CheckedLink source w),
      declareTops m.body (initialState w source.regions) = .ok frames before.ready ∧
      FramesOf m.body frames ∧
      before.linked = [(m, bundleEnv ss frames)] := by
  let m : Module := ⟨name, [], (List.range ss.length).zip ss, exports⟩
  let source : Program := ⟨name, [m]⟩
  change ∃ (frames : List (Nat × List (Name × Addr))) (before : LinkedBehavior.CheckedLink source w),
    declareTops m.body (initialState w source.regions) = .ok frames before.ready ∧
    FramesOf m.body frames ∧ before.linked = [(m, bundleEnv ss frames)]
  have loaded : source.loaded = [m] := by
    simp [Program.loaded, Program.evalOrder, Program.visit, EsmSplit.visitImports, Program.find, source, m]
  have tops : source.tops = m.body := by simp [Program.tops, loaded, m, mergeSort_zip_range]
  have regions : source.regions = ss.length := single_regions ss
  have bounds : ∀ p ∈ m.body, p.1 + 1 < (initialState w source.regions).cells.size := by
    intro p hp
    have ordinal := List.mem_range.1 (List.of_mem_zip hp).1
    simpa [initialState, regions] using Nat.succ_lt_succ ordinal
  obtain ⟨frames, ready, allocated, _⟩ := declareConstTops_ok m.body
    (fun p hp => consts p.2 (List.of_mem_zip hp).2) (initialState w source.regions) bounds
  let env := bundleEnv ss frames
  have scopes : source.scopes frames source.loaded = .ok [(m, env)] := by
    rw [loaded] <;> rfl
  have identities : (checkIdentities m.body : M w Unit) = pure () := by
    change checkIdentities ((List.range ss.length).zip ss) = _
    rw [checkIdentities, List.map_fst_zip (by simp)]
    simp [List.nodup_range]
  have sorted : (unitsOf [(m, env)]).Pairwise fun a b => a.id ≤ b.id := by
    change (((List.range ss.length).zip ss).map (fun p : Nat × Stmt => (p.1, p.2, env)) ++ []).Pairwise
      (fun a b : LinkedUnit => a.id ≤ b.id)
    rw [List.append_nil, List.pairwise_map]
    exact zip_range_sorted ss
  have hoists : (initFunctions (unitsOf [(m, env)]) : M w Unit) = pure () := by
    apply initFunctions_const
    intro u hu
    simp only [unitsOf, List.flatMap_cons, List.flatMap_nil, List.append_nil, m] at hu
    obtain ⟨⟨id, s⟩, hs, rfl⟩ := List.mem_map.1 hu
    exact consts s (List.of_mem_zip hs).2
  have equation : link source (initialState w source.regions) = .ok [(m, env)] ready := by
    unfold link linkUsingScopes
    rw [tops, identities, M.pure_bind, M.bind_apply, allocated]
    change (lift (source.scopes frames source.loaded) >>= fun linked => do
      initFunctions ((unitsOf linked).mergeSort fun a b => decide (a.id ≤ b.id))
      pure linked) ready = _
    rw [scopes, lift, M.pure_bind, sorted_units sorted, hoists, M.pure_bind] <;> rfl
  exact ⟨frames, ⟨[(m, env)], ready, equation⟩, allocated, declareTops_names _ allocated, rfl⟩

#print axioms link_singleModuleConst

end CoreJS
