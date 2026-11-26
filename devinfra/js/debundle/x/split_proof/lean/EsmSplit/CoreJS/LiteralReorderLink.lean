import EsmSplit.CoreJS.LiteralSwapCheck
import EsmSplit.CoreJS.DeploymentPlacement

/-! Source-derived links for the two independent declarations in the authored
reordering fixture. The binding names and literal initializers are source data. -/

namespace CoreJS.LiteralReorderLink

open ExactLiteralMove

deriving instance DecidableEq for EsmSplit.Selector, EsmSplit.Spec, EsmSplit.Module, EsmSplit.Plan

set_option maxRecDepth 16000
set_option maxHeartbeats 4000000

def stmts (x y z : Name) (a b : Literal) : List Stmt :=
  [decl x a, decl y b, .decl .const z (.var x)]

def source (x y z : Name) (a b : Literal) : Program :=
  ⟨"entry.mjs", [⟨"entry.mjs", [], [(0, decl x a), (1, decl y b),
    (2, .decl .const z (.var x))], [(z, z)]⟩]⟩

def bundle (x y z : Name) : EsmSplit.Bundle := [
  { src := "", defs := [x] },
  { src := "", defs := [y] },
  { src := "", defs := [z], eager := [x], exports := [(z, some z)] }]

def spec : EsmSplit.Spec := AuthoredFixture.spec

def output (x y z : Name) (a b : Literal) : Program :=
  splitProgram (stmts x y z a b) (bundle x y z) (EsmSplit.mkPlan (bundle x y z) spec)

/-- The narrow supported planner shape for the authored placement. -/
def expectedPlan (x : Name) : EsmSplit.Plan := {
  entry := "entry.mjs"
  mods := [
    ⟨"entry.mjs", [2], [("right.mjs", []), ("left.mjs", [x])], []⟩,
    ⟨"left.mjs", [0], [], [(x, x)]⟩,
    ⟨"right.mjs", [1], [], []⟩]
}

/-- Check the actual source-derived bundle's plan, without evaluating either program. -/
def checkPlan (x y z : Name) : Except String (PLift (EsmSplit.mkPlan (bundle x y z) spec = expectedPlan x)) :=
  if shape : EsmSplit.mkPlan (bundle x y z) spec = expectedPlan x then .ok ⟨shape⟩
  else .error "the actual authored plan does not have the supported owners, imports and exports"

/-- The original public fixture's actual planner has this shape. -/
theorem fixture_plan : EsmSplit.mkPlan (bundle "a" "b" "result") spec = expectedPlan "a" := by cbv

private def sourceEnv (x y z : Name) : Env :=
  ⟨[(x, ⟨1, 0⟩), (y, ⟨2, 0⟩), (z, ⟨3, 0⟩)], .undef, none⟩
private def rightEnv (y : Name) : Env := ⟨[(y, ⟨2, 0⟩)], .undef, none⟩
private def leftEnv (x : Name) : Env := ⟨[(x, ⟨1, 0⟩)], .undef, none⟩
private def entryEnv (x z : Name) : Env := ⟨[(z, ⟨3, 0⟩), (x, ⟨1, 0⟩)], .undef, none⟩
private def ready (w : World) : State w :=
  let frame : Array Binding := #[⟨none, false⟩]
  { initialState w 3 with cells := #[#[], frame, frame, frame] }
def sourceLinked (x y z : Name) (a b : Literal) : List (Module × Env) :=
  [(⟨"entry.mjs", [], [(0, decl x a), (1, decl y b),
    (2, .decl .const z (.var x))], [(z, z)]⟩, sourceEnv x y z)]
def outputLinked (x y z : Name) (a b : Literal) : List (Module × Env) :=
  [(⟨"right.mjs", [], [(1, decl y b)], []⟩, rightEnv y),
   (⟨"left.mjs", [], [(0, decl x a)], [(x, x)]⟩, leftEnv x),
   (⟨"entry.mjs", [⟨"right.mjs", []⟩, ⟨"left.mjs", [(x, x)]⟩],
     [(2, .decl .const z (.var x))], [(z, z)]⟩, entryEnv x z)]

def sourceLink (w : World) (x y z : Name) (a b : Literal) :
    LinkedBehavior.CheckedLink (source x y z a b) w :=
  ⟨sourceLinked x y z a b, ready w, by cbv⟩

def outputLink (w : World) (x y z : Name) (a b : Literal)
    (_hxy : x ≠ y) (_hxz : x ≠ z) (_hyz : y ≠ z)
    (shape : EsmSplit.mkPlan (bundle x y z) spec = expectedPlan x) :
    LinkedBehavior.CheckedLink (output x y z a b) w :=
  ⟨outputLinked x y z a b, ready w, by
    unfold output
    rw [shape]
    cbv⟩

private theorem leftAgree (x y z : Name) (a : Literal) :
    Agree (decl x a).free (sourceEnv x y z) (leftEnv x) := by
  refine ⟨rfl, rfl, ?_⟩
  simp [decl, Stmt.free, literal_closed, sourceEnv, leftEnv] <;> decide

private theorem rightAgree (x y z : Name) (b : Literal) (hxy : x ≠ y) :
    Agree (decl y b).free (sourceEnv x y z) (rightEnv y) := by
  refine ⟨rfl, rfl, ?_⟩
  simp [decl, Stmt.free, literal_closed, sourceEnv, rightEnv, List.lookup_cons,
    beq_eq_false_iff_ne.mpr (Ne.symm hxy)]

private theorem entryAgree (x y z : Name) (_hxy : x ≠ y) (hxz : x ≠ z) (hyz : y ≠ z) :
    Agree (Stmt.decl .const z (.var x)).free (sourceEnv x y z) (entryEnv x z) := by
  refine ⟨rfl, rfl, ?_⟩
  simp [Stmt.free, Expr.free, sourceEnv, entryEnv, List.lookup_cons,
    beq_eq_false_iff_ne.mpr (Ne.symm hxz), beq_eq_false_iff_ne.mpr (Ne.symm hyz),
    beq_eq_false_iff_ne.mpr hxz]

/-- The two independently allocated source cells cross before the reader. -/
theorem outcomes (x y z : Name) (a b : Literal)
    (hxy : x ≠ y) (hxz : x ≠ z) (hyz : y ≠ z)
    (shape : EsmSplit.mkPlan (bundle x y z) spec = expectedPlan x) (w : World) (fuel : Nat) :
    runProgram w fuel (source x y z a b) = runProgram w fuel (output x y z a b) := by
  have schedule : SafeReorder (unitsOf (sourceLink w x y z a b).linked)
      [(1, decl y b, sourceEnv x y z), (0, decl x a, sourceEnv x y z),
        (2, .decl .const z (.var x), sourceEnv x y z)] := by
    apply SafeReorder.swap [] [(2, .decl .const z (.var x), sourceEnv x y z)]
      0 1 (sourceEnv x y z) (sourceEnv x y z) x y a b ⟨1, 0⟩ ⟨2, 0⟩
    · simp [sourceEnv]
    · simp [sourceEnv, List.lookup_cons, beq_eq_false_iff_ne.mpr (Ne.symm hxy)]
    · decide
  have scopes : SameUnits
      [(1, decl y b, sourceEnv x y z), (0, decl x a, sourceEnv x y z),
        (2, .decl .const z (.var x), sourceEnv x y z)]
      (unitsOf (outputLink w x y z a b hxy hxz hyz shape).linked) := by
    exact .cons ⟨rfl, rfl, rightAgree x y z b hxy⟩ (.cons ⟨rfl, rfl, leftAgree x y z a⟩
      (.cons ⟨rfl, rfl, entryAgree x y z hxy hxz hyz⟩ .nil))
  exact preserve_plan fuel (sourceLink w x y z a b) (outputLink w x y z a b hxy hxz hyz shape)
    rfl _ schedule scopes

/-- Every original unit retains its actual requested owner. -/
theorem placement (x y z : Name) (a b : Literal)
    (shape : EsmSplit.mkPlan (bundle x y z) spec = expectedPlan x) :
    Places [(0, "left.mjs"), (1, "right.mjs"), (2, "entry.mjs")]
      (source x y z a b) (output x y z a b) := by
  unfold output
  rw [shape]
  unfold Places
  constructor
  · decide
  · constructor
    · conv => lhs; cbv
      exact List.Perm.refl _
    · conv => lhs; cbv
      decide

#print axioms placement

/-- The complete public entry table resolves to the original live cell. -/
theorem sameExports (x y z : Name) (a b : Literal)
    (hxy : x ≠ y) (hxz : x ≠ z) (hyz : y ≠ z)
    (shape : EsmSplit.mkPlan (bundle x y z) spec = expectedPlan x) :
    LinkedBehavior.SameExports (source x y z a b) (output x y z a b) := by
  intro w
  refine ⟨[(z, ⟨3, 0⟩)], [(z, ⟨3, 0⟩)], ?_, ?_, List.Perm.refl _⟩
  · unfold LinkedBehavior.exports
    rw [(sourceLink w x y z a b).equation]
    change (EntryExports.resolve "entry.mjs" (sourceLinked x y z a b)).toOption = _
    simp [EntryExports.resolve, sourceLinked, sourceEnv, List.lookup_cons, Except.toOption,
      beq_eq_false_iff_ne.mpr (Ne.symm hxz), beq_eq_false_iff_ne.mpr (Ne.symm hyz)]
    rfl
  · unfold LinkedBehavior.exports
    rw [(outputLink w x y z a b hxy hxz hyz shape).equation]
    change (EntryExports.resolve (output x y z a b).entry (outputLinked x y z a b)).toOption = _
    have entryName : (output x y z a b).entry = "entry.mjs" := by
      unfold output
      rw [shape]
      rfl
    rw [entryName]
    simp [EntryExports.resolve, outputLinked, entryEnv, List.lookup_cons, Except.toOption]
    rfl

/-- Source- and request-indexed evidence for the only program constructed by
this rule. The plan-shape check is on the actual planner, before any execution. -/
structure Construction (original : Program) (request : EsmSplit.Spec) where
  x : Name
  y : Name
  z : Name
  a : Literal
  b : Literal
  sourceEq : original = source x y z a b
  requestEq : request = spec
  distinctXY : x ≠ y
  distinctXZ : x ≠ z
  distinctYZ : y ≠ z
  shape : EsmSplit.mkPlan (bundle x y z) spec = expectedPlan x

def Construction.program {original request} (c : Construction original request) : Program :=
  splitProgram (stmts c.x c.y c.z c.a c.b) (bundle c.x c.y c.z)
    (EsmSplit.mkPlan (bundle c.x c.y c.z) request)

/-- Read the original source units and the unchanged authored request. Other
statement shapes, binding aliases and plan layouts are refused. -/
def construct (original : Program) (request : EsmSplit.Spec) : Except String (Construction original request) := do
  if requestEq : request = spec then
    match originalShape : original with
    | ⟨entry, [⟨moduleName, [], [(0, .decl .const x ex), (1, .decl .const y ey),
        (2, .decl .const z (.var read))], [(exportName, localName)]⟩]⟩ =>
      if sourceNames : entry = "entry.mjs" ∧ moduleName = "entry.mjs" ∧ read = x ∧ exportName = z ∧ localName = z then
        let some ⟨a, ha⟩ := LiteralSwapCheck.literal ex | throw "first initializer is not a primitive literal"
        let some ⟨b, hb⟩ := LiteralSwapCheck.literal ey | throw "second initializer is not a primitive literal"
        if distinct : x ≠ y ∧ x ≠ z ∧ y ≠ z then
          let ⟨shape⟩ ← checkPlan x y z
          have sourceEq :
              (⟨entry, [⟨moduleName, [], [(0, .decl .const x ex), (1, .decl .const y ey),
                (2, .decl .const z (.var read))], [(exportName, localName)]⟩]⟩ : Program) = source x y z a b := by
            simp [source, decl, sourceNames.1, sourceNames.2.1, sourceNames.2.2.1,
              sourceNames.2.2.2.1, sourceNames.2.2.2.2, ha, hb]
          return ⟨x, y, z, a, b, originalShape.trans sourceEq, requestEq, distinct.1, distinct.2.1, distinct.2.2, shape⟩
        else throw "source declarations must name distinct cells"
      else throw "source entry, read, or public export does not match the supported layout"
    | _ => throw "expected two literal constants and the original exported reader"
  else throw "authored placement is not the supported sequential request"

/-- The owners in the placement theorem are the unchanged request's own
selectors, not a replacement assignment supplied by the caller. -/
theorem authored_owners {original request} (c : Construction original request) :
    request.claimants (bundle c.x c.y c.z) 0 = ["left.mjs"] ∧
    request.claimants (bundle c.x c.y c.z) 1 = ["right.mjs"] ∧
    request.claimants (bundle c.x c.y c.z) 2 = ["entry.mjs"] := by
  have owners : spec.claimants (bundle c.x c.y c.z) 0 = ["left.mjs"] ∧
      spec.claimants (bundle c.x c.y c.z) 1 = ["right.mjs"] ∧
      spec.claimants (bundle c.x c.y c.z) 2 = ["entry.mjs"] := ⟨rfl, rfl, rfl⟩
  simpa only [← c.requestEq] using owners

#print axioms authored_owners

/-- Accepted constructions carry actual authored placement, all public live
cells, and complete outcomes in every world and at every fuel. -/
theorem sound {original request} (c : Construction original request) :
    Places [(0, "left.mjs"), (1, "right.mjs"), (2, "entry.mjs")] original c.program ∧
    LinkedBehavior.SameExports original c.program ∧
    ∀ w fuel, runProgram w fuel original = runProgram w fuel c.program := by
  have programEq : c.program = output c.x c.y c.z c.a c.b := by
    simp [Construction.program, output, c.requestEq]
  refine ⟨?_, ?_, ?_⟩
  · simpa only [← c.sourceEq, ← programEq] using placement c.x c.y c.z c.a c.b c.shape
  · simpa only [← c.sourceEq, ← programEq] using
      sameExports c.x c.y c.z c.a c.b c.distinctXY c.distinctXZ c.distinctYZ c.shape
  · intro w fuel
    simpa only [← c.sourceEq, ← programEq] using
      outcomes c.x c.y c.z c.a c.b c.distinctXY c.distinctXZ c.distinctYZ c.shape w fuel

#print axioms construct
#print axioms sound
#print axioms sameExports
#print axioms checkPlan
#print axioms fixture_plan
#print axioms outcomes
#print axioms sourceLink
#print axioms outputLink

end CoreJS.LiteralReorderLink
