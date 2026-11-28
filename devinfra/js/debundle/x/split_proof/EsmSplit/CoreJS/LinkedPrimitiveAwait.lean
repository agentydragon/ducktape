import EsmSplit.CoreJS.CallTurn
import EsmSplit.CoreJS.Eval
import EsmSplit.CoreJS.FromESTree
import EsmSplit.CoreJS.LinkedInput
import EsmSplit.CoreJS.PrimitiveAwait
import EsmSplit.CoreJS.ProgramTurn
import EsmSplit.CoreJS.Scope
import EsmSplit.CoreJS.Split
import EsmSplit.CoreJS.SyntaxMatch
import EsmSplit.ESTreeUnits
import EsmSplit.Plan

/-!
An explicit model entry for linked modules with discarded primitive awaits
in ordinary async declarations. The bridge recognizes calls syntactically in
module expression statements and const initializers, then checks the callee
against the actual linked function object. Aliases, nested call sites and any
call outside this driver still see the ordinary deferred evaluator.

The result at the end of the module includes its pending queue. A consumer
can inspect that state or explicitly drain; this gives the driver no facts
about host checkpoints or placement safety.
-/

open Lean (Json)

namespace CoreJS.LinkedPrimitiveAwait

open PrimitiveAwait

variable {W : World}

abbrev Input := TurnProgram

private def registerCuts (source : LinkedInput.DecodedSource) : List (Nat × DeferredAwait) :=
  source.nodes.toList.zipIdx.filterMap fun (node, id) => do
    let parts ← (FromESTree.primitiveAwaitParts node).toOption
    let some (.funDecl f) := source.source.body.lookup id | none
    let some (unit, _, free, some _) := f.opaqueBody | none
    guard (unit == id && f.kind == .asyncFn && f.name == parts.scope.name &&
      parts.scope.closes.all free.contains)
    return (id, ⟨f, ⟨parts.scope, parts.before, parts.operand, parts.after⟩⟩)

private def decodeSource (source : Json) (entry : String) : Except String LinkedInput.DecodedSource := do
  let decoded ← LinkedInput.decodeSingletonSourceNodes (← ESTreeUnits.nodes source) entry
  unless decoded.source.imports.isEmpty && !decoded.hasTemplates do
    throw "await model requires an import-free original without templates"
  return decoded

def decode (source : Json) (entry : String) : Except String Input := do
  let decoded ← decodeSource source entry
  return ⟨decoded.program, registerCuts decoded⟩

/-- Reread a placed graph through the existing syntax, ordinal and live-cell
checks. Source cuts transfer only by their checked original declaration IDs. -/
structure Placed where
  before : Input
  after : Input
  sameCuts : before.cuts = after.cuts

def decodePlacedChecked (source files : Json) (plan : EsmSplit.Plan) : Except String Placed := do
  let original ← decodeSource source plan.entry
  let (before, after) ← LinkedInput.decodePair source (Json.mkObj []) files plan
  let cuts := registerCuts original
  for (id, cut) in cuts do
    let some owner := after.modules.find? (fun m => (m.body.lookup id).isSome) |
      throw s!"missing placed async declaration {id}"
    let some (.funDecl actual) := owner.body.lookup id | throw s!"missing placed async declaration {id}"
    unless (SyntaxMatch.funcEq actual cut.deferred).isSome do
      throw s!"placed deferred closure differs at original ordinal {id}"
    let imports := owner.imports.flatMap fun i => i.names.map Prod.snd
    -- Cut code came from the original module. It cannot be reused if a
    -- captured name became an import: an assignment would then need a
    -- frozen target, even though both lookups still reach the same cell.
    if let some name := cut.parts.scope.free.find? imports.contains then
      throw s!"placed async declaration {id} captures imported binding {name}"
  return ⟨⟨before, cuts⟩, ⟨after, cuts⟩, rfl⟩

def decodePlaced (source files : Json) (plan : EsmSplit.Plan) : Except String (Input × Input) :=
  (decodePlacedChecked source files plan).map fun p => (p.before, p.after)

abbrev Immediate := TurnResult

def Immediate.shared (outcome : Immediate W α) : Res W Unit :=
  outcome.observed.erase

def Immediate.pending : Immediate W α → Option (List Job)
  | .ok _ turn | .throw _ turn | .unknown _ turn => some turn.jobs
  | .fuel => none

abbrev entryEnv := TurnUnits.entryEnv

open TurnUnits

private theorem observed_map {α β : Type} (f : α → β) (outcome : Immediate W α) :
    (outcome.map f).observed = outcome.observed := by cases outcome <;> rfl

/-- The module phase never runs a continuation. Its callers can separately use
`PrimitiveAwait.drain` on the returned turn. -/
def runImmediate (W : World) (fuel : Nat) (input : Input) : Immediate W Env :=
  withLinkedTurn W input.program fun linked entered =>
    match entryEnv input.program.entry linked with
    | some env => (runLinked fuel input.cuts (unitsOf linked) entered).map fun () => env
    | none => .unknown "expected the linked entry module" entered

/-- Successful checked linking ties the ordinary staging observation to
the fixed turn evaluator. The same schedule consumes the same executable
body data at identical fuel. -/
theorem runImmediate_eq_programTurn {input : Input} {linked : List (Module × Env)} {ready : State W}
    (linkedOk : link input.program (initialState W input.program.regions) = .ok linked ready)
    (entry : (entryEnv input.program.entry linked).isSome = true) (fuel : Nat) :
    (runImmediate W fuel input).observed = runProgramTurn W fuel input := by
  simp only [runImmediate, runProgramTurn, withLinkedTurn, TurnResult.andThen, linkedOk]
  cases h : entryEnv input.program.entry linked with
  | none => simp [h] at entry
  | some _ =>
    rw [observed_map]
    cases runLinked fuel input.cuts (unitsOf linked) (⟨ready, []⟩ : Turn W) with
    | ok value turn => cases value; rfl
    | _ => rfl

/-- Holding the shared link state, cut table and ordered free-name bindings
fixed makes all module outcomes and the full pending queue equal. -/
theorem runImmediate_same {W : World} {a b : Input} {left right : List (Module × Env)}
    {ready : State W}
    (aLink : link a.program (initialState W a.program.regions) = .ok left ready)
    (bLink : link b.program (initialState W b.program.regions) = .ok right ready)
    (aEntry : (entryEnv a.program.entry left).isSome = true)
    (bEntry : (entryEnv b.program.entry right).isSome = true)
    (cuts : a.cuts = b.cuts) (units : SameUnits (unitsOf left) (unitsOf right)) (fuel : Nat) :
    (runImmediate W fuel a).observed = (runImmediate W fuel b).observed := by
  simp only [runImmediate, withLinkedTurn, TurnResult.andThen, aLink, bLink]
  cases ha : entryEnv a.program.entry left with
  | none => simp [ha] at aEntry
  | some ea =>
    cases hb : entryEnv b.program.entry right with
    | none => simp [hb] at bEntry
    | some eb => rw [observed_map, observed_map, cuts, runLinked_same fuel b.cuts units]

/-- On a successfully linked graph with an entry and no authenticated cuts,
the staged rule returns exactly the ordinary result at the same fuel and
never introduces jobs. -/
theorem noCuts_turn {input : Input} {linked : List (Module × Env)} {ready : State W} {env : Env}
    (cuts : input.cuts = [])
    (linkedOk : link input.program (initialState W input.program.regions) = .ok linked ready)
    (entry : entryEnv input.program.entry linked = some env) (fuel : Nat) :
    (runImmediate W fuel input).observed = TurnResult.withJobs [] (runProgram W fuel input.program) := by
  simp only [runImmediate, withLinkedTurn, runProgram, M.bind_apply, TurnResult.andThen, runLinked,
    linkedOk, entry, cuts, observed_map]
  cases runUnits fuel (unitsOf linked) ready <;> rfl

/-- Once a module has linked, the no-cut path agrees with the shared
evaluator at every bound. Equality retains object identity and all stateful
outcomes; the queue is empty whenever the shared result has state. -/
theorem noCuts_agree {W : World} {input : Input} {mod : Module} {env : Env} {ready : State W}
    (cuts : input.cuts = [])
    (linked : link input.program (initialState W input.program.regions) = .ok [(mod, env)] ready)
    (fuel : Nat) :
    let immediate := runImmediate W fuel input
    let shared := runProgram W fuel input.program
    immediate.shared = shared ∧
      immediate.pending = (match shared with | .fuel => none | _ => some []) := by
  simp only [runImmediate, withLinkedTurn, runProgram, M.bind_apply, TurnResult.andThen, entryEnv, runLinked, linked, cuts]
  cases runUnits fuel (unitsOf [(mod, env)]) ready <;> exact ⟨rfl, rfl⟩

end CoreJS.LinkedPrimitiveAwait
