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
    simp [CoreJS.Program.loaded, CoreJS.Program.evalOrder, CoreJS.Program.visit, CoreJS.Program.find, source, sourceModule]
  have targetLoaded : target.loaded = [targetModule] := by
    simp [CoreJS.Program.loaded, CoreJS.Program.evalOrder, CoreJS.Program.visit, CoreJS.Program.find, target, targetModule]
  have hregions : source.regions = target.regions := by
    simp [CoreJS.Program.regions, source, target, sourceModule, targetModule]
  have htops : source.tops = target.tops := by
    simp [CoreJS.Program.tops, CoreJS.Program.loaded, CoreJS.Program.evalOrder, CoreJS.Program.visit,
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

end CoreJS
