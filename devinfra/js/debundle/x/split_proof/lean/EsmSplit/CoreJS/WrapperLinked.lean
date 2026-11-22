import EsmSplit.CoreJS.WrapperMove

namespace CoreJS.WrapperLinked

private def providerStmt (returned : Func) : Stmt :=
  .decl .var "makeWrapper" (.func (wrapperFactory "makeWrapper" ["callback", "wrapper"] returned))

private def callerStmt (callback : Func) : Stmt :=
  .decl .var "deferred" (.call (.var "importedWrapper") [.func callback])

private def movedStmt (moved : Func) : Stmt := .decl .var "independent" (.func moved)

/-- The provider exports its local `makeWrapper` as `wrapper`. The consumer calls it through the
import alias `importedWrapper`.
The same three statement identities occur in both orders. -/
def program (swap : Bool) (returned callback moved : Func) : Program where
  entry := "consumer"
  modules := [
    { name := "provider", imports := [], body := [(0, providerStmt returned)], exports := [("wrapper", "makeWrapper")] },
    { name := "consumer", imports := [⟨"provider", [("wrapper", "importedWrapper")]⟩],
      body := if swap then [(2, movedStmt moved), (1, callerStmt callback)]
              else [(1, callerStmt callback), (2, movedStmt moved)], exports := [] }]

private def providerEnv : Env := ⟨[("makeWrapper", ⟨1, 0⟩)], .undef⟩

private def consumerEnv (swap : Bool) : Env :=
  ⟨(if swap then [("independent", ⟨3, 0⟩), ("deferred", ⟨2, 0⟩)] else [("deferred", ⟨2, 0⟩), ("independent", ⟨3, 0⟩)]) ++
    [("importedWrapper", ⟨1, 0⟩)], .undef⟩

private def linkedState (W : World) : State W :=
  let frame : Array Binding := #[⟨some .undef, true⟩]
  { initialState W 3 with cells := #[#[], frame, frame, frame] }

private def units (swap : Bool) (returned callback moved : Func) : List (Nat × Stmt × Env) :=
  let a := (1, callerStmt callback, consumerEnv swap)
  let b := (2, movedStmt moved, consumerEnv swap)
  (0, providerStmt returned, providerEnv) :: (if swap then [b, a] else [a, b])

private def frames : List (Nat × List (Name × Addr)) :=
  [(0, [("makeWrapper", ⟨1, 0⟩)]), (1, [("deferred", ⟨2, 0⟩)]), (2, [("independent", ⟨3, 0⟩)])]

private theorem sort_three (swap : Bool) (a b c : α) :
    (((0, a) :: (if swap then [(2, c), (1, b)] else [(1, b), (2, c)]) : List (Nat × α)).mergeSort
      (fun x y => decide (x.1 ≤ y.1))) = [(0, a), (1, b), (2, c)] := by
  cases swap <;>
    simp [List.mergeSort, List.MergeSort.Internal.splitInTwo, List.splitAt_eq, List.take, List.drop]

/-- Compute the linker on both module orders, including the export and import aliases. -/
private theorem run_linked (W : World) (fuel : Nat) (swap : Bool) (returned callback moved : Func) :
    runProgram W fuel (program swap returned callback moved) =
      runUnits fuel (units swap returned callback moved) (linkedState W) := by
  let p := program swap returned callback moved
  let tops := [(0, providerStmt returned), (1, callerStmt callback), (2, movedStmt moved)]
  let modules := p.loaded.zip [providerEnv, consumerEnv swap]
  have ht : p.tops = tops := by
    cases swap
    · exact sort_three false _ _ _
    · exact sort_three true _ _ _
  have hr : p.regions = 3 := by cases swap <;> rfl
  have hi : (checkIdentities tops : M W Unit) = pure () := by
    have ids : (tops.map (·.1)).Nodup := by change ([0, 1, 2] : List Nat).Nodup; decide
    simp [checkIdentities, ids]
  have hd : declareTops tops (initialState W 3) = .ok frames (linkedState W) := by rfl
  have hs : p.scopes frames p.loaded = .ok modules := by cases swap <;> rfl
  have hu : unitsOf modules = units swap returned callback moved := by cases swap <;> rfl
  have init : (initFunctions ((units swap returned callback moved).mergeSort fun a b => decide (a.1 ≤ b.1)) :
    M W Unit) = pure () := by
    unfold units
    rw [sort_three]
    rfl
  change runProgram W fuel p = _
  unfold runProgram
  rw [link_bind, ht, hi, M.pure_bind, hr, bind_of_ok hd, hs, lift, M.pure_bind, hu, init, M.pure_bind]

private theorem consumer_agree (names : List Name) : Agree names (consumerEnv true) (consumerEnv false) := by
  refine ⟨rfl, fun x _ => ?_⟩
  by_cases hp : x = "deferred"
  · subst x; rfl
  by_cases hj : x = "independent"
  · subst x; rfl
  simp [consumerEnv, List.lookup_cons, beq_eq_false_iff_ne.mpr hp, beq_eq_false_iff_ne.mpr hj]

/-- Both orders of the linked two-module program give exactly the same normal final states, for
every world, fuel bound, and choice of callback, returned function, and independent closure. -/
theorem normal_iff (W : World) (fuel : Nat) (returned callback moved : Func) (t : State W) :
    runProgram W fuel (program false returned callback moved) = .ok () t ↔
    runProgram W fuel (program true returned callback moved) = .ok () t := by
  rw [run_linked, run_linked]
  simp only [units, Bool.false_eq_true, ↓reduceIte, runUnits]
  rw [runUnit_agree (s := movedStmt moved) (consumer_agree _) fuel 2,
      runUnit_agree (s := callerStmt callback) (consumer_agree _) fuel 1]
  apply bind_ok_congr
  intro _ s hprovider _ t
  obtain ⟨cell, factory, hr, h⟩ := wrapperVar_origin hprovider
  have hc : cell = ⟨1, 0⟩ := by
    have hx : some (⟨1, 0⟩ : Addr) = some cell := h.lookup
    exact (Option.some.inj hx).symm
  subst cell
  have hx : ∀ c, (consumerEnv false).vars.lookup "deferred" = some c → c.region = 2 := by
    intro c h
    change some (⟨2, 0⟩ : Addr) = some c at h
    cases h; rfl
  have hy : ∀ c, (consumerEnv false).vars.lookup "independent" = some c → c.region = 3 := by
    intro c h
    change some (⟨3, 0⟩ : Addr) = some c at h
    cases h; rfl
  have hl := decl_local (W := W) (r := 3) (R := fun _ => False) (e := .func moved) rfl fuel
    (consumerEnv false) .var "independent" (by intro z hz; cases hz) hy
  have linked : (consumerEnv false).vars.lookup "importedWrapper" = some (⟨1, 0⟩ : Addr) := rfl
  have swap := wrapper_var_commutes_at (i := 1) (j := 2) (h.alias linked) hx (by decide) hl
    (fun _ h => h.elim) (by decide) (by omega) fuel callback
  constructor
  · intro run
    obtain ⟨a, u, ha, rest⟩ := bind_ok run
    obtain ⟨b, v, hb, done_⟩ := bind_ok rest
    cases done_
    obtain ⟨u', hb', ha'⟩ := (swap a b t).1 ⟨u, ha, hb⟩
    change runUnit fuel (consumerEnv false) 2 (movedStmt moved) s = .ok b u' at hb'
    change runUnit fuel (consumerEnv false) 1 (callerStmt callback) u' = .ok a t at ha'
    rw [bind_of_ok hb', bind_of_ok ha']
    rfl
  · intro run
    obtain ⟨b, u, hb, rest⟩ := bind_ok run
    obtain ⟨a, v, ha, done_⟩ := bind_ok rest
    cases done_
    obtain ⟨u', ha', hb'⟩ := (swap a b t).2 ⟨u, hb, ha⟩
    change runUnit fuel (consumerEnv false) 1 (callerStmt callback) s = .ok a u' at ha'
    change runUnit fuel (consumerEnv false) 2 (movedStmt moved) u' = .ok b t at hb'
    rw [bind_of_ok ha', bind_of_ok hb']
    rfl

end CoreJS.WrapperLinked
