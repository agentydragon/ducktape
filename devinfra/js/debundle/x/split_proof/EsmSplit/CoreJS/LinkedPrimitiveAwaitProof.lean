import EsmSplit.CoreJS.LinkedPrimitiveAwait
import EsmSplit.CoreJS.LinkWorld
import EsmSplit.CoreJS.PrimitiveAwait
import EsmSplit.CoreJS.Scope
import EsmSplit.CoreJS.Split
import EsmSplit.CoreJS.StateMatch
import EsmSplit.CoreJS.SyntaxMatch
import EsmSplit.Plan

/-! Derive the immediate outcome and queue premise from checked links. Only
linking is executed by this checker; it never tries a bounded evaluation. -/

open Lean (Json)

namespace CoreJS.LinkedPrimitiveAwait

open LinkedBehavior

variable {W : World}

private def checkUses (names : List Name) (left right : Env) :
    Option (PLift (Agree names left right)) := do
  let ⟨receiver⟩ ← SyntaxMatch.scalarEq left.this right.this
  let ⟨home⟩ ← SyntaxMatch.scalarEq left.home right.home
  if uses : ∀ x ∈ names, left.vars.lookup x = right.vars.lookup x then
    return ⟨⟨receiver, home, uses⟩⟩
  else none

private def checkUnits : (left right : List LinkedUnit) → Option (PLift (SameUnits left right))
  | [], [] => some ⟨.nil⟩
  | u :: us, v :: vs => do
    let ⟨id⟩ ← SyntaxMatch.scalarEq u.id v.id
    let ⟨stmt⟩ ← SyntaxMatch.stmtEq u.stmt v.stmt
    let ⟨agree⟩ ← checkUses u.stmt.free u.env v.env
    let ⟨rest⟩ ← checkUnits us vs
    return ⟨.cons ⟨id, stmt, agree⟩ rest⟩
  | _, _ => none

/-- All immediate outcomes of the actual reread placement, including the
complete state and exact pending queue. Public binding addresses are checked
separately from the entry environments. -/
def StagedImmediate (source files : Json) (plan : EsmSplit.Plan) : Prop :=
  ∃ placed : Placed, decodePlacedChecked source files plan = .ok placed ∧
    SameExports placed.before.program placed.after.program ∧
    ∀ w fuel, (runImmediate w fuel placed.before).observed = (runImmediate w fuel placed.after).observed

/-- The links and finite schedule comparison are world independent. No
evaluator fuel is selected, and abrupt and unknown results are retained. -/
def checkStagedImmediate (source files : Json) (plan : EsmSplit.Plan) :
    Except String (PLift (StagedImmediate source files plan)) := do
  match decoded : decodePlacedChecked source files plan with
  | .error reason => throw reason
  | .ok placed =>
    let pair ← checkProbePair placed.before.program placed.after.program
    let some ⟨eq⟩ := StateMatch.probe pair.before.ready pair.after.ready |
      throw "different state after linking"
    let some ⟨units⟩ := checkUnits (unitsOf pair.before.linked) (unitsOf pair.after.linked) |
      throw "different ordered linked units or bindings"
    if exported : exportsMatchFrom pair.before pair.after = true then
      if aEntry : (entryEnv placed.before.program.entry pair.before.linked).isSome = true then
        if bEntry : (entryEnv placed.after.program.entry pair.after.linked).isSome = true then
          return ⟨⟨placed, decoded, pair.sameExports exported, fun w fuel =>
            runImmediate_same (link_from_probe pair.before w)
              (by rw [link_from_probe pair.after w, ← eq]) aEntry bEntry placed.sameCuts units fuel⟩⟩
        else throw "missing placed entry"
      else throw "missing original entry"
    else throw "different public export bindings"

/-- With any chosen checkpoint policy, identical immediate outcomes yield
identical explicit drain observations, including refusal and the remaining tail. -/
theorem StagedImmediate.drains {source files plan} (h : StagedImmediate source files plan) :
    ∃ placed : Placed, decodePlacedChecked source files plan = .ok placed ∧
      SameExports placed.before.program placed.after.program ∧
      ∀ w fuel jobFuel quota,
        (runImmediate w fuel placed.before).observed.checkpoint jobFuel quota =
        (runImmediate w fuel placed.after).observed.checkpoint jobFuel quota := by
  obtain ⟨placed, decoded, exported, immediate⟩ := h
  exact ⟨placed, decoded, exported, fun w fuel jobFuel quota =>
    congrArg (TurnResult.checkpoint jobFuel quota) (immediate w fuel)⟩

end CoreJS.LinkedPrimitiveAwait
