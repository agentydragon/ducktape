import EsmSplit.CoreJS.CheckedSplit
import EsmSplit.CoreJS.StateMatch

open CoreJS CoreJS.CheckedSplit

private def entry := "entry.mjs"
private def worker := "worker.mjs"

private def source : Program := {
  entry
  modules := [{
    name := entry
    imports := []
    body := [
      (0, .decl .const "seed" (.num 42)),
      (1, .funDecl (.mk "read" .normal false [] [.ret (.var "seed")])),
      (2, .decl .const "result" (.call (.var "read") [])),
      (3, .decl .const "other" (.num 42))
    ]
    exports := [("result", "result")]
  }]
}

private def spec : EsmSplit.Spec := {
  entry
  modules := [(worker, [.name "seed", .name "read"]), (entry, [.stmt 2, .stmt 3])]
}

private def checkValue (p : Program) : IO Unit := do
  let some [("result", address)] := LinkedBehavior.exports World.probe p |
    throw (IO.userError "expected one public result cell")
  unless address = ⟨3, 0⟩ do throw (IO.userError "result resolves to the wrong live cell")
  let .ok () state := runProgram World.probe 16 p |
    throw (IO.userError "expected normal initialization")
  let .ok (.num value) _ := readCell address state |
    throw (IO.userError "expected the function's numeric result")
  unless value == 42 do throw (IO.userError s!"expected 42, got {value}")

namespace SparseConstruct

open LinkedBehavior

/-! A bounded, proof-only constructive example. The corresponding public-safe
source is `const seed = 42; export { seed }; const result = seed;`: ordinal 1
is static export metadata, while the executable units retain IDs 0 and 2. -/

def source (initial : Expr) : Program := ⟨"entry.mjs", [⟨"entry.mjs", [],
  [(0, .decl .const "seed" initial), (2, .decl .const "result" (.var "seed"))],
  [("seed", "seed")]⟩]⟩

def spec : EsmSplit.Spec := {
  entry := "entry.mjs"
  modules := [("worker.mjs", [.name "seed"]), ("entry.mjs", [.name "result"])]
}

/-- The existing checked split assigns positions in the compact executable
list, which differs from the original CoreJS unit IDs after the metadata gap. -/
theorem compactPlacement : requestedPlacement (source (.num 42)) spec =
    [(0, "worker.mjs"), (1, "entry.mjs")] := by cbv

/-! Name selectors refer to compact executable positions in #20061. Resolve
each selected position back to its original unit ID before checking placement.
This example does not interpret ordinal selectors for metadata positions. -/
def requestedSourcePlacement (input : Program) (request : EsmSplit.Spec) : List UnitPlacement :=
  match input.modules with
  | [m] => (requestedPlacement input request).filterMap fun (index, owner) =>
      (m.body[index]?).map fun unit => (unit.1, owner)
  | _ => []

theorem sourcePlacement : requestedSourcePlacement (source (.num 42)) spec =
    [(0, "worker.mjs"), (2, "entry.mjs")] := by cbv

/-- The existing strict contract has no output for this fixture: its compact
placement asks for ordinal 1, but the source has executable ordinals 0 and 2. -/
theorem noCompactSound (candidate : Program) (requested : Guarantee) :
    ¬ CoreJS.CheckedSplit.Sound (source (.num 42)) spec requested candidate := by
  intro sound
  have ids := sound.1.2.1
  rw [compactPlacement] at ids
  conv at ids => lhs; cbv
  change ([0, 2] : List Nat).Perm [0, 1] at ids
  exact (by decide : ¬ ([0, 2] : List Nat).Perm [0, 1]) ids

/-- The strict obligations with the authored placement expressed in actual
source unit IDs. This differs from `CheckedSplit.Sound` until its placement
mapping carries those IDs instead of compact list positions. -/
def Sound (source : Program) (spec : EsmSplit.Spec) (output : Program) : Prop :=
  Places (requestedSourcePlacement source spec) source output ∧ SameExports source output ∧
    ScheduleEquivalent .allOutcomes (fun _ => True) source output

/-- Construct the output, retaining the source IDs. The link and schedule
checks produce proofs; no execution result is used as an equality oracle. -/
def build (initial : Expr) (input : Program) (request : EsmSplit.Spec) :
    Except String { output : Program // Sound input request output } := do
  let some _ := SyntaxMatch.programEq input (source initial) |
    throw "source is not the two-declaration sparse fixture"
  unless (match initial with | .num _ | .var _ => true | _ => false) do
    throw "sparse fixture requires a number or binding read"
  unless request.entry == "entry.mjs" && request.order.isEmpty && !request.sequential &&
      !request.privateEntryBody && request.modules.map Prod.fst == ["worker.mjs", "entry.mjs"] &&
      request.modules.all (fun (_, selectors) => selectors.all fun selector => match selector with
        | .name name => name == "seed" || name == "result" | .stmt _ => false) do
    throw "sparse fixture requires the two named-selector modules"
  let output : Program := ⟨"entry.mjs", [
    ⟨"worker.mjs", [], [(0, .decl .const "seed" initial)], [("seed", "seed")]⟩,
    ⟨"entry.mjs", [⟨"worker.mjs", [("seed", "seed")]⟩],
      [(2, .decl .const "result" (.var "seed"))], [("seed", "seed")]⟩]⟩
  let ⟨placed⟩ ← checkPlaces (requestedSourcePlacement input request) input output
  let links ← checkProbePair input output
  let some ⟨ready⟩ := StateMatch.probe links.before.ready links.after.ready |
    throw "linked initial states differ"
  let some ⟨schedule⟩ := sameUnits? (unitsOf links.before.linked) (unitsOf links.after.linked) |
    throw "linked schedules differ"
  if exported : exportsMatchFrom links.before links.after = true then
    have runs (w : World) (fuel : Nat) : runProgram w fuel input = runProgram w fuel output := by
      rw [run_after_link fuel (links.before.forWorld w), run_after_link fuel (links.after.forWorld w)]
      have sameReady := congrArg (linkState w) ready
      change runUnits fuel (unitsOf links.before.linked) (linkState w links.before.ready) =
        runUnits fuel (unitsOf links.after.linked) (linkState w links.after.ready)
      rw [← sameReady]
      exact congrFun (runUnits_same fuel schedule) _
    return ⟨output, placed, links.sameExports exported, fun w _ fuel => runs w fuel⟩
  else throw "entry live cells differ"

theorem build_sound {initial input request} (result : { output : Program // Sound input request output })
    (_accepted : build initial input request = .ok result) : Sound input request result.val := result.property

def checkOutput {input request} (result : { output : Program // Sound input request output })
    (decoded : Program) : Except String (PLift (decoded = result.val)) :=
  match SyntaxMatch.programEq decoded result.val with
  | some proof => .ok proof
  | none => .error "decoded output differs from the constructed modules"

theorem checkOutput_sound {input request decoded} (result : { output : Program // Sound input request output })
    (same : decoded = result.val) : Sound input request decoded := same ▸ result.property

def test : IO Unit := do
  let original := source (.num 42)
  match checkedSplit original spec .allOutcomes with
  | .error "source must be an import-free entry with consecutive unit identities starting at zero" => pure ()
  | .error why => throw (IO.userError s!"unexpected sparse refusal: {why}")
  | .ok _ => throw (IO.userError "dense checked split unexpectedly accepted the sparse fixture")
  let built ← IO.ofExcept (build (.num 42) original spec)
  match checkPlaces (requestedPlacement original spec) original built.val with
  | .error "placement must cover every executable source unit once" => pure ()
  | .error why => throw (IO.userError s!"unexpected compact placement refusal: {why}")
  | .ok _ => throw (IO.userError "compact placement incorrectly matched sparse IDs")
  let some worker := built.val.find "worker.mjs" | throw (IO.userError "missing worker")
  let some entry := built.val.find "entry.mjs" | throw (IO.userError "missing entry")
  unless worker.body.map Prod.fst == [0] && entry.body.map Prod.fst == [2] &&
      built.val.regions == original.regions do throw (IO.userError "sparse IDs or regions changed")
  unless LinkedBehavior.exports World.probe built.val == some [("seed", ⟨1, 0⟩)] do
    throw (IO.userError "source export cell changed")
  let altered := { built.val with modules := [worker, { entry with exports := [("seed", "result")] }] }
  unless LinkedBehavior.exports World.probe altered == some [("seed", ⟨3, 0⟩)] do
    throw (IO.userError "altered export did not point at the other cell")
  match checkOutput built altered with
  | .error _ => pure ()
  | .ok _ => throw (IO.userError "altered export passed the exact output check")
  let extra := { spec with modules :=
    [("worker.mjs", [.name "seed", .name "missing"]), ("entry.mjs", [.name "result"])] }
  match build (.num 42) original extra with
  | .error "sparse fixture requires the two named-selector modules" => pure ()
  | _ => throw (IO.userError "an unsupported selector was not refused")
  let throwing := source (.var "missing")
  let throwingBuilt ← IO.ofExcept (build (.var "missing") throwing spec)
  let .throw (.error .reference message) state := runProgram World.probe 16 throwing |
    throw (IO.userError "expected the original unbound read to throw")
  let .throw (.error .reference message') state' := runProgram World.probe 16 throwingBuilt.val |
    throw (IO.userError "expected the split unbound read to throw")
  letI : DecidableEq World.probe.σ := by change DecidableEq Unit; infer_instance
  unless message == message' && (StateMatch.state state state').isSome do
    throw (IO.userError "the split changed the abrupt result or state")
  match runProgram World.probe 0 original, runProgram World.probe 0 built.val with
  | .fuel, .fuel => pure ()
  | _, _ => throw (IO.userError "the split changed fuel exhaustion")

#print axioms compactPlacement
#print axioms sourcePlacement
#print axioms noCompactSound
#print axioms build
#print axioms build_sound
#print axioms checkOutput_sound

end SparseConstruct

def main : IO Unit := do
  SparseConstruct.test
  let built ← IO.ofExcept (checkedSplit source spec .allOutcomes)
  let output := built.program
  unless requestedPlacement source spec == [(0, worker), (1, worker), (2, entry), (3, entry)] do
    throw (IO.userError "expected the authored owners")
  let some moved := output.find worker | throw (IO.userError "missing worker module")
  let some remaining := output.find entry | throw (IO.userError "missing entry module")
  unless moved.body.map Prod.fst == [0, 1] && remaining.body.map Prod.fst == [2, 3] do
    throw (IO.userError "the function and capture did not move as requested")
  unless decide (remaining.imports = [⟨worker, [("read", "read")]⟩]) do
    throw (IO.userError "the use must import the moved function")
  checkValue source
  checkValue output
  let _ ← IO.ofExcept (checkOutput built output)
  let normal ← IO.ofExcept (checkedSplit source spec .normalCompletion)
  let _ ← IO.ofExcept (checkOutput normal output)

  let rejected := { spec with modules := [(worker, [.name "read", .name "result"]), (entry, [.stmt 0, .stmt 3])] }
  for guarantee in [Guarantee.allOutcomes, .normalCompletion] do
    match checkedSplit source rejected guarantee with
    | .error "requested placement changes the source unit order" => pure ()
    | .error why => throw (IO.userError s!"unexpected refusal: {why}")
    | .ok _ => throw (IO.userError "the requested reorder was certified")

  let wrongCell := { output with modules := [moved, { remaining with exports := [("result", "other")] }] }
  let wrongFile := { output with modules := [{ moved with name := "other.mjs" }, remaining] }
  for changed in [wrongCell, wrongFile] do
    match checkOutput built changed with
    | .error "decoded output differs from the constructed modules" => pure ()
    | .error why => throw (IO.userError s!"unexpected output refusal: {why}")
    | .ok _ => throw (IO.userError "a changed output graph passed the check")
