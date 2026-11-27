import EsmSplit.CoreJS.Eval
import EsmSplit.CoreJS.FromESTree
import EsmSplit.CoreJS.LinkedInput
import EsmSplit.CoreJS.PrimitiveAwait
import EsmSplit.ESTreeUnits

/-!
An explicit model entry for a single module with discarded primitive awaits
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
  cuts : List FromESTree.PrimitiveAwaitParts

def decode (source : Json) (entry : String) : Except String Input := do
  let nodes ← ESTreeUnits.nodes source
  let decoded ← LinkedInput.decodeSingletonSourceNodes nodes entry
  unless decoded.source.imports.isEmpty && !decoded.hasTemplates do
    throw "await model requires an import-free module without templates"
  let cuts := nodes.toList.zipIdx.filterMap fun (node, id) => do
    let parts ← (FromESTree.primitiveAwaitParts node).toOption
    let some (.funDecl f) := decoded.source.body.lookup id | none
    let some (_, _, _, some _) := f.opaqueBody | none
    if f.kind == .asyncFn && f.name == parts.scope.name then some parts else none
  return ⟨decoded.program, cuts⟩

private structure Cut where
  callee : Addr
  closed : Env
  parts : FromESTree.PrimitiveAwaitParts

private def bindCuts (env : Env) (parts : List FromESTree.PrimitiveAwaitParts) :
    M W (List (Name × Cut)) :=
  parts.mapM fun part => do
    let name := part.scope.name
    let .obj addr ← readVar env name | unknown "model: expected the linked async declaration"
    let some (.closure _ closed _) := (← getObj addr).code | unknown "model: expected an async closure"
    return (name, ⟨addr, closed, part⟩)

private def prefixOf (cut : Cut) (args : List Val) (fuel : Nat) : M W Prefix := do
  let env ← enterFunction cut.parts.scope cut.closed .undef args
  match ← execStmts fuel env cut.parts.before with
  | .normal =>
    let operand ← evalExpr fuel env cut.parts.operand
    pure (.suspended operand env cut.parts.after)
  | .ret value => pure (.completed value)
  | _ => unknown "model: invalid control flow before await"

inductive Immediate (W : World) (α : Type) where
  | ok (value : α) (turn : Turn W)
  | throw (reason : Val) (turn : Turn W)
  | unknown (reason : String) (turn : Turn W)
  | fuel

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
    match linked with
    | [(_, env)] =>
      thenSync (bindCuts env input.cuts) entered fun cuts ready =>
        match runUnitsWithCuts cuts fuel (unitsOf linked) ready with
        | .ok () done => .ok env done
        | .throw reason stopped => .throw reason stopped
        | .unknown why stopped => .unknown why stopped
        | .fuel => .fuel
    | _ => .unknown "expected exactly one linked module" entered

end CoreJS.LinkedPrimitiveAwait
