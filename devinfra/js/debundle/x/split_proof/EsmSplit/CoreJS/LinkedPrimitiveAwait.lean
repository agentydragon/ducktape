import EsmSplit.CoreJS.Eval
import EsmSplit.CoreJS.FromESTree
import EsmSplit.CoreJS.LinkedInput
import EsmSplit.CoreJS.PrimitiveAwait
import EsmSplit.CoreJS.Scope
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

structure Input where
  private mk ::
  program : Program
  cuts : List (Nat × FromESTree.PrimitiveAwaitParts)

private def registerCuts (source : LinkedInput.DecodedSource) :
    List (Nat × FromESTree.PrimitiveAwaitParts) :=
  source.nodes.toList.zipIdx.filterMap fun (node, id) => do
    let parts ← (FromESTree.primitiveAwaitParts node).toOption
    let some (.funDecl f) := source.source.body.lookup id | none
    let some (_, _, _, some _) := f.opaqueBody | none
    if f.kind == .asyncFn && f.name == parts.scope.name then some (id, parts) else none

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
def decodePlaced (source files : Json) (plan : EsmSplit.Plan) : Except String (Input × Input) := do
  let original ← decodeSource source plan.entry
  let (before, after) ← LinkedInput.decodePair source (Json.mkObj []) files plan
  let cuts := registerCuts original
  for (id, part) in cuts do
    let some owner := after.modules.find? (fun m => (m.body.lookup id).isSome) |
      throw s!"missing placed async declaration {id}"
    let imports := owner.imports.flatMap fun i => i.names.map Prod.snd
    -- Cut code came from the original module. It cannot be reused if a
    -- captured name became an import: an assignment would then need a
    -- frozen target, even though both lookups still reach the same cell.
    if let some name := part.scope.free.find? imports.contains then
      throw s!"placed async declaration {id} captures imported binding {name}"
  return (⟨before, cuts⟩, ⟨after, cuts⟩)

private structure Cut where
  callee : Addr
  closed : Env
  parts : FromESTree.PrimitiveAwaitParts

private def bindCuts (linked : List (Module × Env))
    (parts : List (Nat × FromESTree.PrimitiveAwaitParts)) : M W (List (Name × Cut)) :=
  parts.mapM fun (id, part) => do
    let name := part.scope.name
    let some unit := (unitsOf linked).find? (·.id == id) |
      unknown "model: missing async declaration ordinal"
    let .funDecl f := unit.stmt | unknown "model: expected an async declaration at the checked ordinal"
    unless f.kind == .asyncFn && f.name == name do unknown "model: async declaration changed"
    let .obj addr ← readVar unit.env name | unknown "model: expected the linked async declaration"
    let some (.closure _ closed _) := (← getObj addr).code | unknown "model: expected an async closure"
    return (name, ⟨addr, closed, part⟩)

private def prefixOf (cut : Cut) (args : List Val) (fuel : Nat) : M W Prefix := do
  let env ← enterFunction cut.parts.scope cut.closed .undef args
  match ← execStmts fuel env cut.parts.before with
  | .normal =>
    -- Each preceding item uses one layer of execStmts. At the cut
    -- the list and the expression statement each consume one more.
    let operand ← evalExpr (fuel - cut.parts.before.length - 2) env cut.parts.operand
    pure (.suspended operand env cut.parts.after)
  | .ret value => pure (.completed value)
  | _ => unknown "model: invalid control flow before await"

inductive Immediate (W : World) (α : Type) where
  | ok (value : α) (turn : Turn W)
  | throw (reason : Val) (turn : Turn W)
  | unknown (reason : String) (turn : Turn W)
  | fuel

/-- Erase the driver-only result: keep the exact shared result, including
abrupt reasons and their heap, cell, world and output state. A pending queue
is a separate observation and cannot in general be erased for a split proof. -/
def Immediate.shared : Immediate W α → Res W Unit
  | .ok _ turn => .ok () turn.state
  | .throw reason turn => .throw reason turn.state
  | .unknown reason turn => .unknown reason turn.state
  | .fuel => .fuel

def Immediate.pending : Immediate W α → Option (List Job)
  | .ok _ turn | .throw _ turn | .unknown _ turn => some turn.jobs
  | .fuel => none

private def thenSync {α β : Type} (step : M W α) (turn : Turn W)
    (next : α → Turn W → Immediate W β) : Immediate W β :=
  match step turn.state with
  | .ok value s => next value { turn with state := s }
  | .throw reason s => .throw reason { turn with state := s }
  | .unknown why s => .unknown why { turn with state := s }
  | .fuel => .fuel

private def ordinary (fuel : Nat) (unit : LinkedUnit) (turn : Turn W) : Immediate W Unit :=
  thenSync (runUnit fuel unit.env unit.id unit.stmt) turn (fun _ next => .ok () next)

private def directCall? : Stmt → Option (Option Name × Name × List Expr)
  | .decl .const name (.call (.var callee) args) => some (some name, callee, args)
  | .expr (.call (.var callee) args) => some (none, callee, args)
  | _ => none

private def runUnitWithCuts (cuts : List (Name × Cut)) (fuel : Nat) (unit : LinkedUnit)
    (turn : Turn W) : Immediate W Unit :=
  match fuel, directCall? unit.stmt with
  | n + 3, some (target, name, args) =>
    match cuts.lookup name with
    | none => ordinary fuel unit turn
    | some cut =>
      let entered := { turn with state := { turn.state with region := unit.id + 1 } }
      thenSync (readVar unit.env name) entered fun callee prepared =>
        if callee != .obj cut.callee then ordinary fuel unit turn else
        thenSync (evalArgs (n + 1) unit.env args) prepared fun argv evaluated =>
          match launch promiseProto (prefixOf cut argv) (n + 1) evaluated with
          | .ok promise started =>
            let finish : M W Unit := do
              if let some dest := target then initVar unit.env dest promise
              modifyState fun s => { s with region := 0 }
            thenSync finish started fun _ done => .ok () done
          | .unknown why stopped => .unknown why stopped
          | .fuel => .fuel
  | _, _ => ordinary fuel unit turn

private def runUnitsWithCuts (cuts : List (Name × Cut)) (fuel : Nat) :
    List LinkedUnit → Turn W → Immediate W Unit
  | [], turn => .ok () turn
  | unit :: rest, turn =>
    match runUnitWithCuts cuts fuel unit turn with
    | .ok () next => runUnitsWithCuts cuts fuel rest next
    | .throw reason stopped => .throw reason stopped
    | .unknown why stopped => .unknown why stopped
    | .fuel => .fuel

/-- The module phase never runs a continuation. Its callers can separately use
`PrimitiveAwait.drain` on the returned turn. -/
def runImmediate (W : World) (fuel : Nat) (input : Input) : Immediate W Env :=
  thenSync (link input.program) ⟨initialState W input.program.regions, []⟩ fun linked entered =>
    let entry := match linked with
      | [one] => some one
      | _ => linked.find? fun (m, _) => m.name == input.program.entry
    match entry with
    | some (_, env) =>
      match input.cuts with
      | [] => thenSync (runUnits fuel (unitsOf linked)) entered fun () done => .ok env done
      | parts =>
        thenSync (bindCuts linked parts) entered fun cuts ready =>
          match runUnitsWithCuts cuts fuel (unitsOf linked) ready with
          | .ok () done => .ok env done
          | .throw reason stopped => .throw reason stopped
          | .unknown why stopped => .unknown why stopped
          | .fuel => .fuel
    | none => .unknown "expected the linked entry module" entered

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
  simp only [runImmediate, runProgram, M.bind_apply, thenSync, linked, cuts]
  cases runUnits fuel (unitsOf [(mod, env)]) ready <;> exact ⟨rfl, rfl⟩

end CoreJS.LinkedPrimitiveAwait
