import EsmSplit.CoreJS.LinkedPivotCertificate
import EsmSplit.CoreJS.DeploymentPlacement

/-! Proof-only examples of one omitted metadata ordinal. The modeled programs
are explicit CoreJS fixtures; this file does not add a decoder or a move rule. -/

open CoreJS CoreJS.LinkedBehavior

namespace SparseMetadataExamples

set_option maxRecDepth 16000
set_option maxHeartbeats 4000000

private def declaration : Nat × Stmt := (0, .decl .var "x" (.bool true))
private def printX : Nat × Stmt := (2, .expr (.prim .log [.var "x"]))
private def sourceModule : Module := ⟨"main.mjs", [], [declaration, printX], [("visible", "x")]⟩
private def chunk : Module := ⟨"chunk.mjs", [], [declaration], [("x", "x")]⟩
private def entry : Module :=
  ⟨"main.mjs", [⟨"chunk.mjs", [("x", "x")]⟩], [printX], [("visible", "x")]⟩

def source : Program := ⟨"main.mjs", [sourceModule]⟩
def ordered : Program := ⟨"main.mjs", [chunk, entry]⟩
def reversed : Program := ⟨"main.mjs", [{ sourceModule with body := [printX, declaration] }]⟩

private def scope : Env := ⟨[("x", ⟨1, 0⟩)], .undef, none⟩
private def ready (w : World) : State w :=
  { initialState w 3 with cells := #[#[], #[⟨some .undef, true⟩], #[], #[]] }

private def sourceLink (w : World) : CheckedLink source w :=
  ⟨[(sourceModule, scope)], ready w, by cbv⟩
private def orderedLink (w : World) : CheckedLink ordered w :=
  ⟨[(chunk, scope), (entry, scope)], ready w, by cbv⟩

/-- The real sparse graphs have the same complete bounded result for every
world and fuel. This includes throws, unknowns, fuel and all state fields. -/
theorem sparse_order_kept (w : World) (fuel : Nat) :
    runProgram w fuel source = runProgram w fuel ordered := by
  apply run_eq_of_ordered_links fuel (sourceLink w) (orderedLink w)
  · rfl
  · rfl

/-- The sparse example also satisfies the fuel-independent outcome relation. -/
theorem sparse_all_outcomes (w : World) (result : Res w Unit) :
    Ends w source result ↔ Ends w ordered result := by
  constructor
  · rintro ⟨fuel, run, notFuel⟩
    exact ⟨fuel, (sparse_order_kept w fuel).symm.trans run, notFuel⟩
  · rintro ⟨fuel, run, notFuel⟩
    exact ⟨fuel, (sparse_order_kept w fuel).trans run, notFuel⟩

/-- The public export resolves to the same actual live cell in both links. -/
theorem sparse_exports : SameExports source ordered := by
  apply sameExports_of_match
  intro w
  rw [exportsMatch_from_checked (sourceLink w) (orderedLink w)]
  rfl

/-- The omitted ordinal 1 is not a placement. Both actual executable IDs are
assigned exactly once to their emitted module. -/
theorem sparse_placement : Places [(0, "chunk.mjs"), (2, "main.mjs")] source ordered := by
  unfold Places
  constructor
  · decide
  · constructor
    · conv => lhs; cbv
      exact List.Perm.refl _
    · conv => lhs; cbv

private def output (r : Res World.probe Unit) : Array String :=
  match r with
  | .ok _ state => state.out
  | _ => #[]

/-- Reordering the read before the write changes the host-visible trace. -/
theorem wrong_order_observed :
    output (runProgram World.probe 32 source) = #["true"] ∧
    output (runProgram World.probe 32 reversed) = #["undefined"] := by
  constructor <;> cbv

/-- A terminal metadata ordinal is omitted from the sparse body. The padded
variant instead executes an empty block at that ordinal. -/
def terminalSparse : Program := ⟨"main.mjs", [⟨"main.mjs", [], [(0, .varDecl "x")], [("x", "x")]⟩]⟩
def terminalPadded : Program := ⟨"main.mjs", [⟨"main.mjs", [], [(0, .varDecl "x"), (1, .block [])], [("x", "x")]⟩]⟩

/-- The same public export aliases the same cell despite the full-state mismatch. -/
theorem terminal_exports (w : World) :
    exports w terminalSparse = some [("x", ⟨1, 0⟩)] ∧
    exports w terminalPadded = some [("x", ⟨1, 0⟩)] := by
  constructor <;> cbv

private def terminalState (regions : Nat) : State World.probe :=
  { initialState World.probe regions with
    cells := if regions == 1 then #[#[], #[⟨some .undef, true⟩]] else #[#[], #[⟨some .undef, true⟩], #[]] }

/-- Padding the final ordinal changes heap and cell region counts even though
it declares nothing and the surviving x cell keeps its original address. -/
theorem terminal_region_counterexample :
    runProgram World.probe 8 terminalSparse = .ok () (terminalState 1) ∧
    runProgram World.probe 8 terminalPadded = .ok () (terminalState 2) ∧
    (terminalState 1).heap.size = 2 ∧ (terminalState 2).heap.size = 3 ∧
    (terminalState 1).cellAt ⟨1, 0⟩ = (terminalState 2).cellAt ⟨1, 0⟩ := by
  constructor
  · cbv
  · constructor
    · cbv
    · constructor
      · cbv
      · constructor <;> cbv

/-- The actual normal-completion relation used by the move theorem cannot
transfer unchanged between these programs: its final state is the full state. -/
theorem terminal_normal_transfer_fails :
    ¬ (∀ final : State World.probe,
      Ends World.probe terminalSparse (.ok () final) ↔ Ends World.probe terminalPadded (.ok () final)) := by
  intro transfer
  have left : Ends World.probe terminalSparse (.ok () (terminalState 1)) :=
    ⟨8, terminal_region_counterexample.1, by simp⟩
  have right : Ends World.probe terminalPadded (.ok () (terminalState 2)) :=
    ⟨8, terminal_region_counterexample.2.1, by simp⟩
  have both := Ends.unique ((transfer (terminalState 1)).mp left) right
  have sizes := congrArg (fun r : Res World.probe Unit => match r with
    | .ok _ state => state.heap.size
    | _ => 0) both
  simp [terminalState, initialState] at sizes

/-- Exact fuel is also not invariant: an empty block needs more than one step. -/
theorem terminal_fuel_counterexample :
    runProgram World.probe 1 terminalSparse = .ok () (terminalState 1) ∧
    runProgram World.probe 1 terminalPadded = .fuel := by
  constructor <;> cbv

private def hoisted (padded : Bool) : Program :=
  let f := Stmt.funDecl (.mk "f" .arrow false [] [.ret (.var "f")])
  ⟨"main.mjs", [⟨"main.mjs", [], if padded then [(0, f), (1, .block [])] else [(0, f)], [("f", "f")]⟩]⟩

private def hoistedShape (r : Res World.probe Unit) : Option (Nat × Nat × Nat × Option Addr) := do
  let .ok () s := r | none
  let some (.obj address) := (s.cellAt ⟨1, 0⟩).bind (·.value) | none
  let some (.closure _ closed _) := (s.objAt address).bind (·.code) | none
  return (s.heap.size, address.region, address.index, closed.vars.lookup "f")

/-- Link-time function allocation and its recursive capture keep their cell
and object addresses, while the trailing padding still adds a region. -/
theorem hoisted_region_counterexample :
    hoistedShape (runProgram World.probe 8 (hoisted false)) = some (2, 1, 0, some ⟨1, 0⟩) ∧
    hoistedShape (runProgram World.probe 8 (hoisted true)) = some (3, 1, 0, some ⟨1, 0⟩) := by
  constructor <;> cbv

private def opaqueProgram (padded : Bool) : Program :=
  ⟨"main.mjs", [⟨"main.mjs", [],
    if padded then [(0, .opaque 0 [] [] [] .world), (1, .block [])]
    else [(0, .opaque 0 [] [] [] .world)], []⟩]⟩

private def throwingWorld : World where
  σ := Nat
  init := 0
  step := fun _ _ _ state => (.ret .undef, state)
  talk := fun _ state => (.throw (.prim (.str "stop")), state + 1)

private def unknownShape (r : Res World.probe Unit) : Option (Nat × Nat) :=
  match r with
  | .unknown _ s => some (s.heap.size, s.region)
  | _ => none

private def throwShape (r : Res throwingWorld Unit) : Option (Nat × Nat × Nat) :=
  match r with
  | .throw (.str "stop") s => some (s.heap.size, s.region, s.world.state)
  | _ => none

/-- An opaque failure occurs before the pad but retains the differing initial
region layout, including the host's state after a throwing request. -/
theorem abrupt_region_counterexamples :
    unknownShape (runProgram World.probe 16 (opaqueProgram false)) = some (2, 1) ∧
    unknownShape (runProgram World.probe 16 (opaqueProgram true)) = some (3, 1) ∧
    throwShape (runProgram throwingWorld 16 (opaqueProgram false)) = some (2, 1, 1) ∧
    throwShape (runProgram throwingWorld 16 (opaqueProgram true)) = some (3, 1, 1) := by
  constructor
  · cbv
  · constructor
    · cbv
    · constructor <;> cbv

#print axioms sparse_order_kept
#print axioms sparse_all_outcomes
#print axioms sparse_exports
#print axioms sparse_placement
#print axioms wrong_order_observed
#print axioms terminal_region_counterexample
#print axioms terminal_exports
#print axioms terminal_normal_transfer_fails
#print axioms terminal_fuel_counterexample
#print axioms hoisted_region_counterexample
#print axioms abrupt_region_counterexamples

end SparseMetadataExamples
