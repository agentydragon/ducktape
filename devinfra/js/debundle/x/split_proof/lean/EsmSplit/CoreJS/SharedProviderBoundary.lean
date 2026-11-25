import EsmSplit.CoreJS.LinkedBehavior
import EsmSplit.CoreJS.StaticProviderBoundary

/-! A common linked provider prefix and separately checked importer continuations. -/

namespace CoreJS.LinkedBehavior

variable {w : World}

/-- The units of a list execute in order, propagating any abrupt result from the prefix. -/
theorem runUnits_append (fuel : Nat) (pre suffix : List LinkedUnit) :
    (runUnits fuel (pre ++ suffix) : M w Unit) =
      (runUnits fuel pre >>= fun _ => runUnits fuel suffix) := by
  induction pre with
  | nil => rfl
  | cons unit rest ih =>
    obtain ⟨id, stmt, env⟩ := unit
    simp only [List.cons_append, runUnits, ih, M.bind_assoc]

/-- The provider loads first, exactly once, and its actual units are the prefix. -/
def ProviderPrefix (name : String) (linked : List (Module × Env)) (pre : List LinkedUnit) : Prop :=
  ∃ m env, linked.head? = some (m, env) ∧ m.name = name ∧
    linked.filter (fun pair => pair.1.name == name) = [(m, env)] ∧ unitsOf [(m, env)] = pre

/-- The two real links share a ready state, one provider prefix and its live cells.
The importer continuations are supplied separately and need not have the same syntax or scope. -/
structure SharedProvider (source candidate : Program) (w : World) where
  before : CheckedLink source w
  after : CheckedLink candidate w
  name : String
  cells : EntryExports.Bindings
  pre : List LinkedUnit
  sourceTail : List LinkedUnit
  candidateTail : List LinkedUnit
  ready : before.ready = after.ready
  sourceSchedule : unitsOf before.linked = pre ++ sourceTail
  candidateSchedule : unitsOf after.linked = pre ++ candidateTail
  sourceProvider : ProviderPrefix name before.linked pre
  candidateProvider : ProviderPrefix name after.linked pre
  sourceCells : EntryExports.resolve name before.linked = .ok cells
  candidateCells : EntryExports.resolve name after.linked = .ok cells
  entryCells : exportsMatchFrom before after = true

/-- Only successful provider states need a continuation proof; throws, unknowns and fuel
exhaustion propagate through the shared prefix without executing either continuation. -/
theorem SharedProvider.run_eq {source candidate : Program} (boundary : SharedProvider source candidate w)
    (fuel : Nat)
    (continuations : ∀ state, runUnits fuel boundary.pre boundary.before.ready = .ok () state →
      runUnits fuel boundary.sourceTail state = runUnits fuel boundary.candidateTail state) :
    runProgram w fuel source = runProgram w fuel candidate := by
  rw [run_after_link fuel boundary.before, run_after_link fuel boundary.after,
    boundary.sourceSchedule, boundary.candidateSchedule, ← boundary.ready, runUnits_append, runUnits_append]
  simp only [M.bind_apply]
  cases h : runUnits fuel boundary.pre boundary.before.ready with
  | ok value state => cases value; exact continuations state h
  | throw => rfl
  | unknown => rfl
  | fuel => rfl

/-- The public entry names resolve to the same live cells in every world. -/
theorem SharedProvider.sameExports {source candidate : Program}
    (boundary : ∀ w, SharedProvider source candidate w) : SameExports source candidate := by
  apply sameExports_of_match
  intro w
  rw [exportsMatch_from_checked (boundary w).before (boundary w).after]
  exact (boundary w).entryCells

#print axioms runUnits_append
#print axioms SharedProvider.run_eq
#print axioms SharedProvider.sameExports

end CoreJS.LinkedBehavior

namespace CoreJS.SharedProviderBoundaryExamples

open LinkedBehavior

set_option maxRecDepth 16000
set_option maxHeartbeats 4000000

/-- The earlier two-declaration example also supplies the generic boundary. -/
def fixedGraphBoundary (w : World) (initializer : Expr) :
    SharedProvider (StaticProviderBoundary.original initializer) (StaticProviderBoundary.split initializer) w where
  before := StaticProviderBoundary.sourceLink w initializer
  after := StaticProviderBoundary.splitLink w initializer
  name := "provider"
  cells := [("value", ⟨1, 0⟩), ("other", ⟨2, 0⟩)]
  pre := unitsOf [(StaticProviderBoundary.provider initializer, StaticProviderBoundary.providerEnv)]
  sourceTail := (unitsOf (StaticProviderBoundary.sourceLink w initializer).linked).drop
    (StaticProviderBoundary.provider initializer).body.length
  candidateTail := (unitsOf (StaticProviderBoundary.splitLink w initializer).linked).drop
    (StaticProviderBoundary.provider initializer).body.length
  ready := rfl
  sourceSchedule := rfl
  candidateSchedule := rfl
  sourceProvider := ⟨StaticProviderBoundary.provider initializer, StaticProviderBoundary.providerEnv, rfl, rfl, rfl, rfl⟩
  candidateProvider := ⟨StaticProviderBoundary.provider initializer, StaticProviderBoundary.providerEnv, rfl, rfl, rfl, rfl⟩
  sourceCells := rfl
  candidateCells := rfl
  entryCells := by cbv

/-- Recover the earlier result using only the generic prefix and continuation theorem. -/
theorem fixedGraph_allOutcomes (w : World) (fuel : Nat) (initializer : Expr) :
    runProgram w fuel (StaticProviderBoundary.original initializer) =
      runProgram w fuel (StaticProviderBoundary.split initializer) :=
  (fixedGraphBoundary w initializer).run_eq fuel (fun _ _ => rfl)

/-- The provider's body is opaque; only its two exported bindings are declared here. -/
def provider (code : Nat) : Module where
  name := "provider"
  imports := []
  body := [(0, .opaque code [("external", .var), ("other", .var)] [] [] .world)]
  exports := [("value", "external"), ("other", "other")]

def first : Nat × Stmt := (1, .decl .var "copy" (.var "input"))
def second : Nat × Stmt := (2, .expr (.prim .log [.var "copy", .var "input"]))

def original (code : Nat) : Program where
  entry := "main"
  modules := [provider code,
    ⟨"main", [⟨"provider", [("value", "input")]⟩], [first, second],
      [("public", "copy"), ("source", "input")]⟩]

def split (code : Nat) (wrong : Bool := false) : Program where
  entry := "main"
  modules := [provider code,
    ⟨"part", [⟨"provider", [("value", "input")]⟩], [first], [("copy", "copy")]⟩,
    ⟨"main", [⟨"part", [("copy", "copy")]⟩,
      ⟨"provider", [(if wrong then "other" else "value", "input")]⟩],
      [second], [("public", "copy"), ("source", "input")]⟩]

def providerEnv : Env := ⟨[("external", ⟨1, 0⟩), ("other", ⟨1, 1⟩)], .undef, none⟩
def importerEnv : Env := ⟨[("copy", ⟨2, 0⟩), ("input", ⟨1, 0⟩)], .undef, none⟩
private def wrongEnv : Env := ⟨[("copy", ⟨2, 0⟩), ("input", ⟨1, 1⟩)], .undef, none⟩
private def ready (w : World) : State w :=
  let binding : Binding := ⟨some .undef, true⟩
  { initialState w 3 with cells := #[#[], #[binding, binding], #[binding], #[]] }

private def originalLinked (code : Nat) : List (Module × Env) :=
  [(provider code, providerEnv),
    (⟨"main", [⟨"provider", [("value", "input")]⟩], [first, second],
      [("public", "copy"), ("source", "input")]⟩, importerEnv)]

private def splitLinked (code : Nat) (wrong : Bool) : List (Module × Env) :=
  [(provider code, providerEnv),
    (⟨"part", [⟨"provider", [("value", "input")]⟩], [first], [("copy", "copy")]⟩, importerEnv),
    (⟨"main", [⟨"part", [("copy", "copy")]⟩,
      ⟨"provider", [(if wrong then "other" else "value", "input")]⟩], [second],
      [("public", "copy"), ("source", "input")]⟩, if wrong then wrongEnv else importerEnv)]

private def before (w : World) (code : Nat) : CheckedLink (original code) w :=
  ⟨originalLinked code, ready w, by cbv⟩

private def after (w : World) (code : Nat) : CheckedLink (split code) w :=
  ⟨splitLinked code false, ready w, by cbv⟩

private def wrongLink (w : World) (code : Nat) : CheckedLink (split code true) w :=
  ⟨splitLinked code true, ready w, by cbv⟩

/-- The real links satisfy the boundary without examining the opaque world program. -/
def boundary (w : World) (code : Nat) : SharedProvider (original code) (split code) w where
  before := before w code
  after := after w code
  name := "provider"
  cells := [("value", ⟨1, 0⟩), ("other", ⟨1, 1⟩)]
  pre := unitsOf [(provider code, providerEnv)]
  sourceTail := (unitsOf (before w code).linked).drop (provider code).body.length
  candidateTail := (unitsOf (after w code).linked).drop (provider code).body.length
  ready := rfl
  sourceSchedule := rfl
  candidateSchedule := rfl
  sourceProvider := ⟨provider code, providerEnv, rfl, rfl, rfl, rfl⟩
  candidateProvider := ⟨provider code, providerEnv, rfl, rfl, rfl, rfl⟩
  sourceCells := rfl
  candidateCells := rfl
  entryCells := by cbv

/-- The generic boundary proves every result equal for arbitrary opaque code and worlds. -/
theorem opaqueProvider_allOutcomes (w : World) (fuel code : Nat) :
    runProgram w fuel (original code) = runProgram w fuel (split code) :=
  (boundary w code).run_eq fuel (fun _ _ => rfl)

/-- Entry re-exports keep the provider's live cell. -/
theorem opaqueProvider_sameExports (code : Nat) : SameExports (original code) (split code) :=
  SharedProvider.sameExports (fun w => boundary w code)

/-- The wrong import fails the boundary's entry-cell check on the actual links. -/
theorem wrongAlias_rejected (w : World) (code : Nat) :
    exportsMatchFrom (before w code) (wrongLink w code) = false := by cbv

/-- This world initializes both opaque cells and counts each conversation turn. -/
def writingWorld : World where
  σ := Nat
  init := 0
  step := fun _ _ _ state => (.ret .undef, state)
  talk := fun _ state =>
    match state with
    | 0 => (.writeVar 0 (.prim (.bool true)), 1)
    | 1 => (.writeVar 1 (.prim (.bool false)), 2)
    | n + 2 => (.done (.prim .undef), n + 3)

private def observed (result : Res writingWorld Unit) : Array String × Nat :=
  match result with
  | .ok _ state => (state.out, state.world.state)
  | _ => (#[], 0)

/-- A single opaque provider run takes three turns. The wrong live cell changes the trace. -/
theorem wrongAlias_witness :
    observed (runProgram writingWorld 32 (original 4)) = (#["true true"], 3) ∧
    observed (runProgram writingWorld 32 (split 4)) = (#["true true"], 3) ∧
    observed (runProgram writingWorld 32 (split 4 true)) = (#["true false"], 3) := by
  constructor
  · cbv
  · constructor <;> cbv

/-- An abrupt provider result stops both graphs before the importer can run. -/
def throwingWorld : World where
  σ := Nat
  init := 0
  step := fun _ _ _ state => (.ret .undef, state)
  talk := fun _ state => (.throw (.prim (.str "stop")), state + 1)

private def stopped (result : Res throwingWorld Unit) : Bool :=
  match result with
  | .throw (.str message) state =>
    let count : Nat := state.world.state
    message == "stop" && state.out == #[] && count == 1
  | _ => false

theorem providerThrow_stops_importers :
    stopped (runProgram throwingWorld 32 (original 4)) = true ∧
    stopped (runProgram throwingWorld 32 (split 4)) = true := by
  constructor <;> cbv

/-- The counterexample changes the real interpreter result. -/
theorem wrongAlias_changes_run :
    runProgram writingWorld 32 (original 4) ≠ runProgram writingWorld 32 (split 4 true) := by
  intro eq
  have h := congrArg observed eq
  rw [wrongAlias_witness.1, wrongAlias_witness.2.2] at h
  exact (by decide : (#["true true"], 3) ≠ (#["true false"], 3)) h

#print axioms opaqueProvider_allOutcomes
#print axioms fixedGraph_allOutcomes
#print axioms opaqueProvider_sameExports
#print axioms wrongAlias_rejected
#print axioms wrongAlias_witness
#print axioms providerThrow_stops_importers
#print axioms wrongAlias_changes_run

end CoreJS.SharedProviderBoundaryExamples
