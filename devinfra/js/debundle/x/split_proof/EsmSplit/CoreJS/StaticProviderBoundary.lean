import EsmSplit.CoreJS.LinkedBehavior
import EsmSplit.CoreJS.NestedModuleExtension

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
    ⟨"part", [⟨"provider", [((if wrong then "other" else "value"), "input")]⟩], [first],
      [("copy", "copy"), ("upstream", "input")]⟩,
    ⟨"main", [⟨"part", [("copy", "copy"), ("upstream", "input")]⟩],
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
    (⟨"part", [⟨"provider", [("value", "input")]⟩], [first],
      [("copy", "copy"), ("upstream", "input")]⟩, importerEnv),
    (⟨"main", [⟨"part", [("copy", "copy"), ("upstream", "input")]⟩],
      [second], [("public", "copy"), ("source", "input")]⟩, importerEnv)]

def sourceLink (w : World) (initializer : Expr) : LinkedBehavior.CheckedLink (original initializer) w :=
  ⟨sourceLinked initializer, ready w, by cbv⟩

def splitLink (w : World) (initializer : Expr) : LinkedBehavior.CheckedLink (split initializer) w :=
  ⟨splitLinked initializer, ready w, by cbv⟩

private def providerTops (initializer : Expr) : List (Nat × Stmt) := (provider initializer).body

private def extensionTops : List (Nat × Stmt) := [first, second]

private def providerFrames : List (Nat × List (Name × Addr)) :=
  [(0, [("external", ⟨1, 0⟩)]), (1, [("other", ⟨2, 0⟩)])]

private def extensionFrames : List (Nat × List (Name × Addr)) :=
  [(2, [("copy", ⟨3, 0⟩)]), (3, [])]

private def splitFrames := providerFrames ++ extensionFrames

private def providerReady (w : World) : State w :=
  let frame : Array Binding := #[⟨some .undef, true⟩]
  { initialState w 4 with cells := #[#[], frame, frame, #[], #[]] }

private theorem providerAllocation (w : World) (initializer : Expr) :
    declareTops (providerTops initializer) (initialState w 4) =
      .ok providerFrames (providerReady w) := by
  rfl

private theorem extensionAllocation (w : World) :
    declareTops extensionTops (providerReady w) = .ok extensionFrames (ready w) := by
  rfl

/-- The nested candidate extends the original provider with `part`, which
re-exports its imported `input` as `upstream`, then `main` imports and publicly
re-exports that same live cell. The exact reachable order is provider, part,
main. Cycles and URL/import.meta effects are outside this CoreJS claim. -/
theorem nestedDependencyExtension (initializer : Expr) :
    (split initializer).evalOrder = ["provider", "part", "main"] ∧
      (∀ frame ∈ providerFrames, ∀ cell ∈ frame.2,
        ∃ binding, (providerReady World.probe).cellAt cell.2 = some binding ∧
          (ready World.probe).cellAt cell.2 = some binding) ∧
      (split initializer).importCell splitFrames "part" "upstream" = .ok ⟨1, 0⟩ ∧
      (split initializer).importCell splitFrames "main" "source" = .ok ⟨1, 0⟩ ∧
      (ready World.probe).cellAt ⟨1, 0⟩ = some ⟨some .undef, true⟩ := by
  let P := split initializer
  let path : NestedModuleExtension.ReachablePath P "provider" "part" "main" :=
    ⟨[], [], by rfl, by decide, by decide, by decide⟩
  have providerAllocated : (providerReady World.probe).cellAt ⟨1, 0⟩ =
      some ⟨some .undef, true⟩ := rfl
  let providerModule := provider initializer
  let dependencyModule : Module := {
    name := "part"
    imports := [⟨"provider", [("value", "input")]⟩]
    body := [first]
    exports := [("copy", "copy"), ("upstream", "input")]
  }
  let consumerModule : Module := {
    name := "main"
    imports := [⟨"part", [("copy", "copy"), ("upstream", "input")]⟩]
    body := [second]
    exports := [("public", "copy"), ("source", "input")]
  }
  have linked := NestedModuleExtension.nestedModuleExtension
    (P := P) (frames := splitFrames)
    (oldTops := providerTops initializer) (extensionTops := extensionTops)
    (start := initialState World.probe 4) (prior := providerReady World.probe) (final := ready World.probe)
    (oldFrames := providerFrames) (extensionFrames := extensionFrames)
    (oldAllocation := providerAllocation World.probe initializer)
    (extensionAllocation := extensionAllocation World.probe)
    (topsExtension := by
      simp [Program.tops, Program.loaded, Program.evalOrder, Program.visit,
        EsmSplit.visitImports, Program.find, P, split, provider, providerTops,
        extensionTops, first, second]
      exact List.mergeSort_of_pairwise (by simp))
    (framesExtension := by rfl)
    (provider := "provider") (dependency := "part") (consumer := "main") (reachable := path)
    (depth := 6) (depth_ge_two := by decide) (exportsBound := by rfl)
    (providerModule := providerModule) (dependencyModule := dependencyModule) (consumerModule := consumerModule)
    (providerPublic := "value") (providerLocal := "external")
    (dependencyPublic := "upstream") (dependencyLocal := "input")
    (consumerPublic := "source") (consumerLocal := "input")
    (address := ⟨1, 0⟩) (binding := ⟨some .undef, true⟩)
    (providerFound := by rfl) (dependencyFound := by rfl) (consumerFound := by rfl)
    (providerExport := by rfl) (providerCell := by rfl)
    (dependencyExport := by rfl) (dependencyNotLocal := by rfl)
    (dependencyRoute := by rfl)
    (consumerExport := by rfl) (consumerNotLocal := by rfl) (consumerRoute := by rfl)
    (providerAllocated := providerAllocated)
  rcases linked with ⟨order, _, _, preserved, dependencyCell, consumerCell, providerCell⟩
  exact ⟨by simpa [P, path] using order, preserved, dependencyCell, consumerCell, providerCell⟩


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
    output (runProgram World.probe 32 (split effectful true)) = #["provider", "false false"] := by
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
  exact (by decide : (#["provider", "true true"] : Array String) ≠ #["provider", "false false"]) h


end CoreJS.StaticProviderBoundary
