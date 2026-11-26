import EsmSplit.CoreJS.LinkedBehavior
import EsmSplit.CoreJS.Split

/-! An order-preserving boundary for two top-level lexical initializers.
The first initializer cannot reference the later binding. This is a CoreJS
construction, independent of the frontend, printer, or publishing command. -/

namespace CoreJS.TwoConstBoundary

set_option maxRecDepth 16000
set_option maxHeartbeats 4000000

def source (first second : Expr) : Program :=
  ⟨"main", [⟨"main", [], [(0, .decl .const "x" first), (1, .decl .const "y" second)],
    [("x", "x"), ("y", "y")]⟩]⟩

/-- The first declaration moves to a provider; the entry imports its live cell. -/
def introduce (first second : Expr) : Program :=
  ⟨"main", [⟨"part", [], [(0, .decl .const "x" first)], [("x", "x")]⟩,
    ⟨"main", [⟨"part", [("x", "x")]⟩], [(1, .decl .const "y" second)],
      [("x", "x"), ("y", "y")]⟩]⟩

private def sourceEnv : Env := ⟨[("x", ⟨1, 0⟩), ("y", ⟨2, 0⟩)], .undef, none⟩
private def partEnv : Env := ⟨[("x", ⟨1, 0⟩)], .undef, none⟩
private def entryEnv : Env := ⟨[("y", ⟨2, 0⟩), ("x", ⟨1, 0⟩)], .undef, none⟩
private def ready (w : World) : State w :=
  let frame : Array Binding := #[⟨none, false⟩]
  { initialState w 2 with cells := #[#[], frame, frame] }

private def sourceLinked (a b : Expr) : List (Module × Env) :=
  [(⟨"main", [], [(0, .decl .const "x" a), (1, .decl .const "y" b)],
    [("x", "x"), ("y", "y")]⟩, sourceEnv)]
private def splitLinked (a b : Expr) : List (Module × Env) :=
  [(⟨"part", [], [(0, .decl .const "x" a)], [("x", "x")]⟩, partEnv),
   (⟨"main", [⟨"part", [("x", "x")]⟩], [(1, .decl .const "y" b)],
    [("x", "x"), ("y", "y")]⟩, entryEnv)]

private def sourceLink (w : World) (a b : Expr) : LinkedBehavior.CheckedLink (source a b) w :=
  ⟨sourceLinked a b, ready w, by cbv⟩
private def splitLink (w : World) (a b : Expr) : LinkedBehavior.CheckedLink (introduce a b) w :=
  ⟨splitLinked a b, ready w, by cbv⟩

private theorem firstAgree (a : Expr) (noLater : "y" ∉ a.free) :
    Agree (Stmt.decl .const "x" a).free sourceEnv partEnv := by
  refine ⟨rfl, rfl, ?_⟩
  intro name member
  have ne : name ≠ "y" := by
    intro eq
    subst name
    simp [Stmt.free] at member
    exact noLater member
  by_cases hx : name = "x"
  · subst name; rfl
  · simp [sourceEnv, partEnv, List.lookup_cons, beq_eq_false_iff_ne.mpr hx,
      beq_eq_false_iff_ne.mpr ne]

private theorem entryAgree (names : List Name) : Agree names sourceEnv entryEnv := by
  refine ⟨rfl, rfl, fun name _ => ?_⟩
  by_cases hx : name = "x"
  · subst name; rfl
  by_cases hy : name = "y"
  · subst name; rfl
  simp [sourceEnv, entryEnv, List.lookup_cons, beq_eq_false_iff_ne.mpr hx,
    beq_eq_false_iff_ne.mpr hy]

/-- The generated graph has the same full bounded result in every world.
The result includes effects and state for normal, thrown, unknown, and fuel endings. -/
theorem allOutcomes (a b : Expr) (noLater : "y" ∉ a.free) (w : World) (fuel : Nat) :
    runProgram w fuel (source a b) = runProgram w fuel (introduce a b) := by
  rw [LinkedBehavior.run_after_link fuel (sourceLink w a b),
    LinkedBehavior.run_after_link fuel (splitLink w a b)]
  apply congrFun (runUnits_same fuel ?_) (ready w)
  exact .cons ⟨rfl, rfl, firstAgree a noLater⟩
    (.cons ⟨rfl, rfl, entryAgree _⟩ .nil)

/-- The real linked schedules execute both declarations in their source order. -/
theorem linkedOrder (a b : Expr) (w : World) :
    ((unitsOf (sourceLink w a b).linked).map LinkedUnit.id) = [0, 1] ∧
    ((unitsOf (splitLink w a b).linked).map LinkedUnit.id) = [0, 1] := by
  exact ⟨rfl, rfl⟩

/-- The actual linker assigns the same addresses to the public exports. -/
theorem sameExports (a b : Expr) : LinkedBehavior.SameExports (source a b) (introduce a b) := by
  intro w
  refine ⟨[("x", ⟨1, 0⟩), ("y", ⟨2, 0⟩)], [("x", ⟨1, 0⟩), ("y", ⟨2, 0⟩)], ?_, ?_, List.Perm.refl _⟩
  · unfold LinkedBehavior.exports
    rw [(sourceLink w a b).equation]
    rfl
  · unfold LinkedBehavior.exports
    rw [(splitLink w a b).equation]
    rfl

/-- Read the two actual cells after normal initialization. -/
private def initialized (r : Res World.probe Unit) : Option (Val × Val) :=
  match r with
  | .ok _ state => do
    let x ← (state.cellAt ⟨1, 0⟩).bind (·.value)
    let y ← (state.cellAt ⟨2, 0⟩).bind (·.value)
    pure (x, y)
  | _ => none

/-- The entry initializer reads the already-initialized provider cell. -/
theorem initializedRead :
    initialized (runProgram World.probe 32 (source (.bool true) (.var "x"))) = some (.bool true, .bool true) ∧
    initialized (runProgram World.probe 32 (introduce (.bool true) (.var "x"))) = some (.bool true, .bool true) := by
  constructor <;> cbv

/-- Extract a thrown error and its already-produced output for concrete controls. -/
private def thrown (r : Res World.probe Unit) : Option (Val × Array String) :=
  match r with
  | .throw value state => some (value, state.out)
  | _ => none

/-- Removing the scope condition changes the thrown reference error: the source
sees the later TDZ binding, while the provider has no such local binding. -/
theorem forwardReference_changes_throw :
    thrown (runProgram World.probe 32 (source (.var "y") (.bool true))) =
      some (.error .reference "cannot access 'y' before initialization", #[]) ∧
    thrown (runProgram World.probe 32 (introduce (.var "y") (.bool true))) =
      some (.error .reference "y is not defined", #[]) := by
  constructor <;> cbv

/-- A throw following an effect keeps both the thrown value and output. -/
theorem effectThenThrow :
    thrown (runProgram World.probe 32 (source (.seq (.prim .log [.str "before"]) (.var "x")) (.bool true))) =
      some (.error .reference "cannot access 'x' before initialization", #["before"]) ∧
    thrown (runProgram World.probe 32 (introduce (.seq (.prim .log [.str "before"]) (.var "x")) (.bool true))) =
      some (.error .reference "cannot access 'x' before initialization", #["before"]) := by
  constructor <;> cbv

#print axioms linkedOrder
#print axioms initializedRead
#print axioms forwardReference_changes_throw
#print axioms effectThenThrow
#print axioms allOutcomes
#print axioms sameExports

end CoreJS.TwoConstBoundary
