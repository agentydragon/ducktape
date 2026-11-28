import EsmSplit.CoreJS.Move

namespace CoreJS.CustomMoveExample

/-- Two top-level expression statements. The general syntax checker refuses the inversion. -/
private def statements : List Stmt :=
  [.expr (.bool true), .expr (.bool false)]

theorem syntax_refuses : movesOk statements.toArray [1, 0] = false :=
  rfl

private def extra (i j : Nat) : Bool :=
  i == 1 && j == 0

private theorem literal_steps_commute {W : World} (fuel i j : Nat) (env : Env) (a b : Bool) :
    Commute (runUnit fuel env i (.expr (.bool a)) : M W Flow)
      (runUnit fuel env j (.expr (.bool b))) := by
  intro s u t x y hx hy
  rcases fuel with _ | _ | fuel
  · change Res.fuel = Res.ok x u at hx
    cases hx
  · change Res.fuel = Res.ok x u at hx
    cases hx
  · change Res.ok .normal { s with region := 0 } = Res.ok x u at hx
    cases hx
    change Res.ok .normal _ = Res.ok y t at hy
    cases hy
    exact ⟨{ s with region := 0 }, rfl, rfl⟩

/-- Supply the missing pair to the same semantic interface the production move checker uses. -/
theorem extra_sound (W : World) : MovesSound W statements extra := by
  intro frames s t linked fuel i j different allowed
  simp [extra] at allowed
  obtain ⟨rfl, rfl⟩ := allowed
  exact ⟨literal_steps_commute fuel 1 0 _ false true, literal_steps_commute fuel 0 1 _ true false⟩

theorem swapped {W : World} {B : EsmSplit.Bundle} {S : EsmSplit.Spec} {P : EsmSplit.Plan}
    (valid : EsmSplit.Valid B S P) (fp : FootprintsOfSyntax statements B)
    (order : P.execOrder = [1, 0]) (fuel : Nat) (state : State W) :
    runProgram W fuel (.single statements) = .ok () state ↔
      runProgram W fuel (splitProgram statements B P) = .ok () state := by
  apply moves_preserve_run_of_sound valid fp (extra_sound W) _ fuel state
  rw [order]
  decide

end CoreJS.CustomMoveExample
