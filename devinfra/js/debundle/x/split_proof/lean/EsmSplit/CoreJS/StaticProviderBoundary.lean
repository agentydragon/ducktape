import EsmSplit.CoreJS.LinkedBehavior

/-! A static import boundary with one unavailable provider initializer.
The linked schedule contains the provider's declarations once before the importer;
an abrupt completion prevents the remaining steps. -/

namespace CoreJS.StaticProviderBoundary

set_option maxRecDepth 16000
set_option maxHeartbeats 4000000

def provider (initializer : Expr) : Module where
  name := "provider"
  imports := []
  body := [(0, .decl .var "external" initializer), (1, .decl .var "other" (.bool false))]
  exports := [("value", "external"), ("other", "other")]

def first : Nat × Stmt := (2, .decl .var "copy" (.var "input"))
def second : Nat × Stmt := (3, .expr (.prim .log [.var "copy", .var "input"]))

def original (initializer : Expr) : Program where
  entry := "main"
  modules := [provider initializer,
    ⟨"main", [⟨"provider", [("value", "input")]⟩], [first, second],
      [("public", "copy"), ("source", "input")]⟩]

def split (initializer : Expr) (wrong : Bool := false) : Program where
  entry := "main"
  modules := [provider initializer,
    ⟨"part", [⟨"provider", [("value", "input")]⟩], [first], [("copy", "copy")]⟩,
    ⟨"main", [⟨"part", [("copy", "copy")]⟩,
      ⟨"provider", [(if wrong then "other" else "value", "input")]⟩],
      [second], [("public", "copy"), ("source", "input")]⟩]

def providerEnv : Env := ⟨[("external", ⟨1, 0⟩), ("other", ⟨2, 0⟩)], .undef, none⟩
def importerEnv : Env := ⟨[("copy", ⟨3, 0⟩), ("input", ⟨1, 0⟩)], .undef, none⟩
def ready (w : World) : State w :=
  let frame : Array Binding := #[⟨some .undef, true⟩]
  { initialState w 4 with cells := #[#[], frame, frame, frame, #[]] }

private def sourceLinked (initializer : Expr) : List (Module × Env) :=
  [(provider initializer, providerEnv),
    (⟨"main", [⟨"provider", [("value", "input")]⟩], [first, second],
      [("public", "copy"), ("source", "input")]⟩, importerEnv)]

private def splitLinked (initializer : Expr) : List (Module × Env) :=
  [(provider initializer, providerEnv),
    (⟨"part", [⟨"provider", [("value", "input")]⟩], [first], [("copy", "copy")]⟩, importerEnv),
    (⟨"main", [⟨"part", [("copy", "copy")]⟩, ⟨"provider", [("value", "input")]⟩],
      [second], [("public", "copy"), ("source", "input")]⟩, importerEnv)]

def sourceLink (w : World) (initializer : Expr) : LinkedBehavior.CheckedLink (original initializer) w :=
  ⟨sourceLinked initializer, ready w, by cbv⟩

def splitLink (w : World) (initializer : Expr) : LinkedBehavior.CheckedLink (split initializer) w :=
  ⟨splitLinked initializer, ready w, by cbv⟩

/-- The actual linker resolves both importer aliases and the public re-export to the same cells. -/
theorem liveCells (w : World) (initializer : Expr) :
    LinkedBehavior.exports w (original initializer) = some [("public", ⟨3, 0⟩), ("source", ⟨1, 0⟩)] ∧
    LinkedBehavior.exports w (split initializer) = some [("public", ⟨3, 0⟩), ("source", ⟨1, 0⟩)] ∧
    importerEnv.vars.lookup "input" = some ⟨1, 0⟩ := by
  constructor
  · unfold LinkedBehavior.exports
    rw [(sourceLink w initializer).equation]
    rfl
  · constructor
    · unfold LinkedBehavior.exports
      rw [(splitLink w initializer).equation]
      rfl
    · rfl

/-- Both real links put the same two provider units before the importer, exactly once. -/
theorem providerPrefix (w : World) (initializer : Expr) :
    unitsOf (sourceLink w initializer).linked =
      [(0, .decl .var "external" initializer, providerEnv), (1, .decl .var "other" (.bool false), providerEnv),
        (2, first.2, importerEnv), (3, second.2, importerEnv)] ∧
    unitsOf (splitLink w initializer).linked =
      [(0, .decl .var "external" initializer, providerEnv), (1, .decl .var "other" (.bool false), providerEnv),
        (2, first.2, importerEnv), (3, second.2, importerEnv)] :=
  ⟨rfl, rfl⟩

/-- After the shared provider prefix, the actual linked importer continuations agree for every state. -/
theorem importerContinuation (w : World) (fuel : Nat) (initializer : Expr) (state : State w) :
    runUnits fuel ((unitsOf (sourceLink w initializer).linked).drop 2) state =
      runUnits fuel ((unitsOf (splitLink w initializer).linked).drop 2) state := rfl

/-- Both actual programs enter the same provider-prefix and importer-continuation schedule. -/
theorem linkedBoundary (w : World) (fuel : Nat) (initializer : Expr) :
    runProgram w fuel (original initializer) = runUnits fuel (unitsOf (sourceLink w initializer).linked) (ready w) ∧
    runProgram w fuel (split initializer) = runUnits fuel (unitsOf (sourceLink w initializer).linked) (ready w) := by
  constructor
  · exact LinkedBehavior.run_after_link fuel (sourceLink w initializer)
  · rw [LinkedBehavior.run_after_link fuel (splitLink w initializer)]
    rfl

/-- No provider evaluation is used in this proof. Its initializer may call the world,
mutate state, throw, return unknown, or exhaust fuel. All bounded outcomes agree. -/
theorem allOutcomes (w : World) (fuel : Nat) (initializer : Expr) :
    runProgram w fuel (original initializer) = runProgram w fuel (split initializer) :=
  (linkedBoundary w fuel initializer).1.trans (linkedBoundary w fuel initializer).2.symm

/-- The public entry exports refer to the same live binding, independently of its value. -/
theorem sameExports (initializer : Expr) :
    LinkedBehavior.SameExports (original initializer) (split initializer) := by
  intro w
  exact ⟨_, _, (liveCells w initializer).1, (liveCells w initializer).2.1, List.Perm.refl _⟩

private def effectful : Expr := .seq (.prim .log [.str "provider"]) (.bool true)

private def output (result : Res World.probe Unit) : Array String :=
  match result with
  | .ok _ state => state.out
  | _ => #[]

/-- The provider's effect happens once; an import retargeted to the other live cell changes the trace. -/
theorem wrongAlias :
    output (runProgram World.probe 32 (original effectful)) = #["provider", "true true"] ∧
    output (runProgram World.probe 32 (split effectful true)) = #["provider", "true false"] := by
  constructor <;> cbv

/-- The effectful provider runs once in each of the correctly linked graphs. -/
theorem effectfulOnce :
    output (runProgram World.probe 32 (original effectful)) = #["provider", "true true"] ∧
    output (runProgram World.probe 32 (split effectful)) = #["provider", "true true"] := by
  refine ⟨wrongAlias.1, ?_⟩
  rw [← allOutcomes World.probe 32 effectful]
  exact wrongAlias.1

/-- The changed trace witnesses unequal CoreJS results, not merely different alias metadata. -/
theorem wrongAlias_changes_run :
    runProgram World.probe 32 (original effectful) ≠ runProgram World.probe 32 (split effectful true) := by
  intro eq
  have h := congrArg output eq
  rw [wrongAlias.1, wrongAlias.2] at h
  exact (by decide : (#["provider", "true true"] : Array String) ≠ #["provider", "true false"]) h

#print axioms liveCells
#print axioms providerPrefix
#print axioms importerContinuation
#print axioms linkedBoundary
#print axioms allOutcomes
#print axioms sameExports
#print axioms wrongAlias
#print axioms effectfulOnce
#print axioms wrongAlias_changes_run

end CoreJS.StaticProviderBoundary
