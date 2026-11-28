import EsmSplit.CoreJS.LinkedBehavior
import EsmSplit.CoreJS.LinkedSuffix

/-! Conditional schedule obligation for the saved
`e2e/cases/emission/lowering/split_declarator_fragments/spec.yaml`.

The declaration prefix follows the original and emitted schedules. The
retained final operation is modeled as an arbitrary world interaction with
its checked lexical capture. The terminal export row (unit 5) remains in
the entry's export table. This file does not model the Node console. -/

namespace CoreJS.SplitDeclaratorSchedule

private def aDecl : Stmt :=
  .decl .const "a" (.num 1)

private def bDecl : Stmt :=
  .decl .const "b" (.num 2)

private def cDecl : Stmt :=
  .decl .const "c" (.binary .add (.var "a") (.var "b"))

private def zDecl : Stmt :=
  .decl .const "z" (.str "z")

private def publicNames : List (Name × Name) :=
  [("c", "c"), ("z", "z")]

private def originalModule : Module :=
  ⟨"static/app.js", [], [(0, aDecl), (1, bDecl), (2, cDecl), (3, zDecl)], publicNames⟩

private def entryPath :=
  "static/app/entry.js"

private def xPath :=
  "static/app/modules/x.js"

private def residualPath :=
  "static/app/modules/residual/unhandled.js"

private def entryModule : Module :=
  ⟨entryPath, [⟨xPath, [("c", "c")]⟩, ⟨residualPath, [("z", "z")]⟩], [], publicNames⟩

private def xModule : Module :=
  ⟨xPath, [⟨residualPath, [("a", "a"), ("b", "b")]⟩], [(2, cDecl)], [("c", "c")]⟩

private def residualModule : Module :=
  ⟨residualPath, [], [(0, aDecl), (1, bDecl), (3, zDecl)], [("a", "a"), ("b", "b"), ("z", "z")]⟩

def originalPrefix : Program :=
  ⟨originalModule.name, [originalModule]⟩

def splitPrefix : Program :=
  ⟨entryPath, [entryModule, xModule, residualModule]⟩

private def originalEnv : Env :=
  ⟨[("a", ⟨1, 0⟩), ("b", ⟨2, 0⟩), ("c", ⟨3, 0⟩), ("z", ⟨4, 0⟩)], .undef, none⟩

private def residualEnv : Env :=
  ⟨[("a", ⟨1, 0⟩), ("b", ⟨2, 0⟩), ("z", ⟨4, 0⟩)], .undef, none⟩

private def xEnv : Env :=
  ⟨[("c", ⟨3, 0⟩), ("a", ⟨1, 0⟩), ("b", ⟨2, 0⟩)], .undef, none⟩

private def entryEnv : Env :=
  ⟨[("c", ⟨3, 0⟩), ("z", ⟨4, 0⟩)], .undef, none⟩

private def originalLinked : List (Module × Env) :=
  [(originalModule, originalEnv)]

private def splitLinked : List (Module × Env) :=
  [(residualModule, residualEnv), (xModule, xEnv), (entryModule, entryEnv)]

private def ready (w : World) : State w :=
  let frame : Array Binding := #[⟨none, false⟩]
  { initialState w 4 with cells := #[#[], frame, frame, frame, frame] }

private def originalLink (w : World) : LinkedBehavior.CheckedLink originalPrefix w :=
  ⟨originalLinked, ready w, by cbv⟩

private def splitLink (w : World) : LinkedBehavior.CheckedLink splitPrefix w :=
  ⟨splitLinked, ready w, by cbv⟩

/-- The entry's priority names x first; x evaluates its residual dependency first. -/
theorem dependency_schedule :
    splitPrefix.evalOrder = [residualPath, xPath, entryPath] ∧
      (unitsOf originalLinked).map LinkedUnit.id = [0, 1, 2, 3] ∧
      (unitsOf splitLinked).map LinkedUnit.id = [0, 1, 3, 2] := by
  cbv

/-- The independently linked source cells are used for the same terminal alias table. -/
theorem terminal_exports (w : World) :
    LinkedBehavior.exports w originalPrefix = some [("c", ⟨3, 0⟩), ("z", ⟨4, 0⟩)] ∧
      LinkedBehavior.exports w splitPrefix = some [("c", ⟨3, 0⟩), ("z", ⟨4, 0⟩)] := by
  unfold LinkedBehavior.exports
  rw [(originalLink w).equation, (splitLink w).equation]
  exact ⟨rfl, rfl⟩

/-- In both scopes the argument of unit 4 refers to the original c cell. -/
theorem console_argument :
    originalEnv.vars.lookup "c" = some ⟨3, 0⟩ ∧ entryEnv.vars.lookup "c" = some ⟨3, 0⟩ :=
  ⟨rfl, rfl⟩

/-- Moving z before c preserves the entire initialized state. Low fuel also
agrees: the first literals or the binary expression exhaust fuel on both sides. -/
theorem prefix_outcomes (w : World) (fuel : Nat) :
    runProgram w fuel originalPrefix = runProgram w fuel splitPrefix := by
  rw [LinkedBehavior.run_after_link fuel (originalLink w),
    LinkedBehavior.run_after_link fuel (splitLink w)]
  cases fuel with
  | zero => rfl
  | succ n =>
    cases n with
    | zero => rfl
    | succ n =>
      cases n with
      | zero => rfl
      | succ n => rfl

/-- The saved tail gives the host code the linked cell of c. This is the
retained original unit, not a semantic interpretation of JavaScript console. -/
private def tail : Nat × Stmt := (4, .opaque 4 [] [] ["c"] .world)

private def retainTail (entry : Module) : Module := { entry with body := entry.body ++ [tail] }

def originalWithTail : Program := ⟨originalModule.name, [retainTail originalModule]⟩

def splitWithTail : Program :=
  ⟨entryPath, [retainTail entryModule, xModule, residualModule]⟩

private def readyWithTail (w : World) : State w :=
  { ready w with heap := (ready w).heap.push #[], cells := (ready w).cells.push #[] }

private def originalTailLink (w : World) : LinkedBehavior.CheckedLink originalWithTail w :=
  ⟨[(retainTail originalModule, originalEnv)], readyWithTail w, by cbv⟩

private def splitTailLink (w : World) : LinkedBehavior.CheckedLink splitWithTail w :=
  ⟨[(residualModule, residualEnv), (xModule, xEnv), (retainTail entryModule, entryEnv)],
    readyWithTail w, by cbv⟩

/-- The proved declaration prefix still agrees in the full linked state:
the final operation's additional, empty region is present on both sides. -/
private theorem fullPrefix (w : World) (fuel : Nat) :
    runUnits fuel ((unitsOf (originalTailLink w).linked).take 4) (originalTailLink w).ready =
    runUnits fuel ((unitsOf (splitTailLink w).linked).take 4) (splitTailLink w).ready := by
  cases fuel with
  | zero => rfl
  | succ n =>
    cases n with
    | zero => rfl
    | succ n =>
      cases n with
      | zero => rfl
      | succ n => rfl

/-- The complete modeled execution, including any result from the retained
world operation, agrees without a premise about the actual console. -/
theorem retained_outcomes (w : World) (fuel : Nat) :
    runProgram w fuel originalWithTail = runProgram w fuel splitWithTail := by
  let before := originalTailLink w
  let after := splitTailLink w
  apply LinkedSuffix.run_eq fuel 4 before after (fullPrefix w fuel)
  change SameUnits [(tail.1, tail.2, originalEnv)] [(tail.1, tail.2, entryEnv)]
  apply SameUnits.cons _ SameUnits.nil
  refine ⟨rfl, rfl, rfl, rfl, ?_⟩
  intro x member
  change x ∈ ["c"] at member
  have name : x = "c" := by simpa using member
  subst x
  rfl

theorem retained_same_exports : LinkedBehavior.SameExports originalWithTail splitWithTail := by
  intro w
  let table : List (Name × Addr) := [("c", ⟨3, 0⟩), ("z", ⟨4, 0⟩)]
  refine ⟨table, table, ?_, ?_, List.Perm.refl _⟩
  · unfold LinkedBehavior.exports
    rw [(originalTailLink w).equation]
    rfl
  · unfold LinkedBehavior.exports
    rw [(splitTailLink w).equation]
    rfl

end CoreJS.SplitDeclaratorSchedule
