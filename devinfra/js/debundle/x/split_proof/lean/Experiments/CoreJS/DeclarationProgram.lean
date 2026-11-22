import Experiments.CoreJS.DeclarationGroup
import Experiments.CoreJS.Split

/-!
# Evaluating an unsplit CoreJS declaration module

The source model is one import-free module of top-level declaration lists with identifier
declarators, optionally exported inline. Instantiation allocates all the bindings before any
initializers run. Both executions use dense, left-to-right declarator identities as allocation
regions, so the comparison is equality of CoreJS results, including the entire final state for
normal and abrupt completion.

Extraction retains the inline export names. Evaluation of the closed module needs no export
table. `DeclarationCertificate.lean` connects this result to the checked plan.
-/

namespace CoreJS.DeclarationProgram

open DeclarationGroup (compileGroups declareGroups runGroups)

/-- The module after extracting one statement per declarator. -/
def extract (groups : List DeclarationGroup) : Module where
  name := "main"
  imports := []
  body := compileGroups 0 groups
  exports := groups.flatMap (·.exports)

def statements (groups : List DeclarationGroup) : List Stmt :=
  (extract groups).body.map Prod.snd

private def declaratorCount (groups : List DeclarationGroup) : Nat :=
  (groups.map DeclarationGroup.size).sum

private def scope (groups : List DeclarationGroup)
    (frames : List (Nat × List (Name × Addr))) : Env :=
  { vars := (List.range (declaratorCount groups)).flatMap fun id => (frames.lookup id).getD []
    this := .undef }

/-- Evaluate the original groups. Linking allocates the cells for the entire module before
evaluation steps through the groups in source order. -/
def run (W : World) (fuel : Nat) (groups : List DeclarationGroup) : Res W Unit :=
  (do
    let frames ← declareGroups 0 groups
    runGroups fuel (scope groups frames) 0 groups) (initialState W (declaratorCount groups))

private theorem statements_length (groups : List DeclarationGroup) :
    (statements groups).length = declaratorCount groups := by
  simp only [statements, extract, List.length_map, declaratorCount, DeclarationGroup.compileGroups_length]

private theorem body_eq (groups : List DeclarationGroup) :
    compileGroups 0 groups = (List.range (statements groups).length).zip (statements groups) := by
  have ids : (compileGroups 0 groups).map Prod.fst = List.range (compileGroups 0 groups).length := by
    simpa only [List.range_eq_range'] using DeclarationGroup.compileGroups_ids 0 groups
  symm
  unfold statements extract
  rw [List.length_map, ← ids, List.zip_map']
  exact List.map_id _

private theorem body_ids (ss : List Stmt) :
    ((List.range ss.length).zip ss).map Prod.fst = List.range ss.length :=
  List.map_fst_zip (by simp)

private theorem scope_eq (groups : List DeclarationGroup)
    (frames : List (Nat × List (Name × Addr))) :
    scope groups frames = bundleEnv (statements groups) frames := by
  let bindings (id : Nat) := (frames.lookup id).getD []
  change ({ vars := (List.range (declaratorCount groups)).flatMap bindings, this := .undef } : Env) =
    { vars := ((List.range (statements groups).length).zip (statements groups)).flatMap
        (fun u => bindings u.1) ++ [], this := .undef }
  rw [List.append_nil, ← List.flatMap_map Prod.fst bindings, body_ids, statements_length]

/-- The unsplit declaration module and its extracted single-module program have the same
result at each fuel bound. The linkage comparison is for a closed module. -/
theorem run_eq_single (W : World) (fuel : Nat) (groups : List DeclarationGroup) :
    run W fuel groups = runProgram W fuel (Program.single (statements groups)) := by
  unfold run runProgram
  rw [single_regions, statements_length]
  refine congrFun (?_ : _ = _) _
  rw [link_bind, single_tops, ← body_eq]
  have checked : (checkIdentities (compileGroups 0 groups) : M W Unit) = pure () := by
    rw [body_eq, checkIdentities, body_ids]
    simp [List.nodup_range]
  rw [checked, M.pure_bind, ← DeclarationGroup.declareGroups_eq]
  apply M.bind_congr rfl
  intro frames
  let env := scope groups frames
  have linked : (Program.single (statements groups)).scopes frames (Program.single (statements groups)).loaded =
      .ok [(bundleModule (statements groups), env)] := by
    dsimp only [env]
    rw [scope_eq, single_loaded]
    rfl
  rw [linked, lift, M.pure_bind]
  let attach : Nat × Stmt → Nat × Stmt × Env := fun (id, s) => (id, s, env)
  have sameUnits : unitsOf [(bundleModule (statements groups), env)] = (compileGroups 0 groups).map attach := by
    rw [body_eq]
    simp [unitsOf, bundleModule, attach]
  rw [sameUnits]
  have sorted : ((compileGroups 0 groups).map attach).Pairwise (fun a b => a.1 ≤ b.1) := by
    rw [body_eq]
    exact List.pairwise_map.2 (zip_range_sorted (statements groups))
  rw [sorted_units sorted]
  dsimp only [attach]
  rw [DeclarationGroup.initFunctions_compileGroups, M.pure_bind, DeclarationGroup.runGroups_eq]

#print axioms run_eq_single

end CoreJS.DeclarationProgram
