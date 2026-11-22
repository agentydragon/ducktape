import Experiments.CoreJS.DeclarationInput
import Experiments.CoreJS.DeclarationProgram
import Experiments.CoreJS.BindingMoves

namespace CoreJS.DeclarationCertificate

/-- Evaluate the declaration lists of a closed, annotated source module without going through unit extraction. -/
def eval (W : World) (fuel : Nat) (source : Lean.Json) : Except String (Res W Unit) :=
  match DeclarationInput.decode source with
  | .ok groups => .ok (DeclarationProgram.run W fuel groups)
  | .error reason => .error reason

/-- The input decoded by the independent declaration-list evaluator matches the checker's actual units. -/
def covers (source : Lean.Json) (ss : List Stmt) : Bool :=
  match DeclarationInput.decode source with
  | .ok groups => DeclarationInput.matchesStatements groups ss
  | .error _ => false

theorem eval_eq_single {W : World} {source : Lean.Json} {ss : List Stmt} (covered : covers source ss = true)
    (fuel : Nat) : eval W fuel source = .ok (runProgram W fuel (Program.single ss)) := by
  unfold covers at covered
  cases h : DeclarationInput.decode source with
  | error err => simp [h] at covered
  | ok groups =>
    simp only [h] at covered
    have same : DeclarationProgram.statements groups = ss := DeclarationInput.matches_sound covered
    simp only [eval, h, DeclarationProgram.run_eq_single, same]

/-- An order-preserving checked placement agrees with evaluation of the original declaration lists,
including abrupt completion. The endpoint is the modeled split plan. -/
theorem order_kept {W : World} {source : Lean.Json} {ss : List Stmt} {B : EsmSplit.Bundle}
    {S : EsmSplit.Spec} {P : EsmSplit.Plan} (covered : covers source ss = true)
    (valid : EsmSplit.Valid B S P) (footprints : FootprintsOfSyntax ss B) (order : OrderKept ss P)
    (fuel : Nat) : eval W fuel source = .ok (runProgram W fuel (splitProgram ss B P)) := by
  rw [eval_eq_single covered]
  obtain ⟨regions, tops⟩ := split_holds_statements (B := B) (orderKept_perm order)
  exact congrArg Except.ok (run_same regions tops (split_scopes_agree valid footprints order) fuel)

/-- The syntax and binding move checks preserve successful outcomes of the original declaration module. -/
theorem moves {W : World} {source : Lean.Json} {ss : List Stmt} {B : EsmSplit.Bundle}
    {S : EsmSplit.Spec} {P : EsmSplit.Plan} (covered : covers source ss = true)
    (valid : EsmSplit.Valid B S P) (footprints : FootprintsOfSyntax ss B)
    (order : movesOk ss.toArray P.execOrder = true ∨ certifiedMovesOk ss B P.execOrder = true)
    (fuel : Nat) (s : State W) :
    eval W fuel source = .ok (.ok () s) ↔ runProgram W fuel (splitProgram ss B P) = .ok () s := by
  rw [eval_eq_single covered, Except.ok.injEq]
  rcases order with h | h
  · exact moves_preserve_run valid footprints ((movesOk_iff ss _).1 h) fuel s
  · exact moves_preserve_run_of_sound valid footprints (certifiedMayMove_sound footprints valid.1)
      ((certifiedMovesOk_iff ss B _).1 h) fuel s

end CoreJS.DeclarationCertificate
