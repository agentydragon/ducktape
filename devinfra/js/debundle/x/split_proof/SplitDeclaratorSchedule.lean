import EsmSplit.CoreJS.LinkedBehavior

/-! Conditional schedule obligation for the saved
`e2e/cases/emission/lowering/split_declarator_fragments/spec.yaml`.

CoreJS below models the four declaration units in the original source and
in the Rust-emitted graph. Unit 4 is the original `console.log(c)` and is
still an open obligation, represented by a continuation after the prefix.
The terminal export row (unit 5) is retained in the entry's export table.
This file does not lower or certify the JavaScript artifacts. -/

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

/-- What remains for unit 4: the same full observation of the actual
`console.log(c)` in the original and split scopes on the common prefix state.
In particular, no premise about the ambient console is derived here. -/
theorem with_console {w : World} (fuel : Nat) (observe : Env → M w Unit)
    (console :
      ∀ state,
        runProgram w fuel originalPrefix = .ok () state →
          observe originalEnv state = observe entryEnv state) :
    ((do
          runUnits fuel (unitsOf originalLinked);
          observe originalEnv)
        (ready w)) =
      ((do
          runUnits fuel (unitsOf splitLinked);
          observe entryEnv)
        (ready w)) := by
  have prior := prefix_outcomes w fuel
  rw [LinkedBehavior.run_after_link fuel (originalLink w),
    LinkedBehavior.run_after_link fuel (splitLink w)] at prior
  simp only [M.bind_apply]
  change
    runUnits fuel (unitsOf originalLinked) (ready w) =
      runUnits fuel (unitsOf splitLinked) (ready w) at prior
  rw [← prior]
  cases result : runUnits fuel (unitsOf originalLinked) (ready w) with
  | ok value state =>
    cases value
    apply console state
    rw [LinkedBehavior.run_after_link fuel (originalLink w)]
    exact result
  | throw => rfl
  | unknown => rfl
  | fuel => rfl

end CoreJS.SplitDeclaratorSchedule
