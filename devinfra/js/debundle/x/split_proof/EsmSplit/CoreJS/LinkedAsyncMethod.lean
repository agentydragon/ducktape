import EsmSplit.CoreJS.FromESTree
import EsmSplit.CoreJS.LinkedInput
import EsmSplit.CoreJS.ProgramTurn
import EsmSplit.CoreJS.TurnUnits
import EsmSplit.ESTreeUnits

/-! Direct module-level calls of public instance methods with one primitive
await. Class declaration evaluation and property lookup use the ordinary
evaluator. The resolved deferred closure selects the source-derived body,
and the actual property-call receiver is passed to that body. -/

namespace CoreJS.LinkedAsyncMethod

open Lean TurnUnits
variable {W : World}

private structure Method where
  declaration : Func
  body : AwaitBody
  resultBinding : Option Name

structure Input where
  program : Program
  private methods : List Method
  bodyRefusals : List String

private def publicInstanceAsync (node : Json) : Bool :=
  (node.getObjValAs? String "type").toOption == some "MethodDefinition" &&
    (node.getObjValAs? String "kind").toOption == some "method" &&
    (node.getObjValAs? Bool "static").toOption == some false &&
    (node.getObjValAs? Bool "computed").toOption == some false &&
    ((node.getObjVal? "key" >>= fun key => key.getObjValAs? String "type").toOption != some "PrivateIdentifier") &&
    ((node.getObjVal? "value" >>= fun value => value.getObjValAs? Bool "async").toOption == some true)

private def lowerMethod (id : Nat) (members : List ClassMember) (node : Json) :
    Except String Method := do
  let key ← node.getObjVal? "key" >>= FromESTree.moduleExportName
  let candidates := members.filterMap fun m =>
    match m with
    | .method false (.str k) f => if k == key then some f else none
    | _ => none
  let [f] := candidates | throw s!"ambiguous public method {key}"
  let some (unit, _, captures, some _) := f.opaqueBody |
    throw s!"expected a deferred public method {key}"
  unless unit == id && f.kind == .asyncFn && f.name == key do
    throw s!"deferred public method differs at ordinal {id}"
  let (parts, resultBinding) ← FromESTree.primitiveMethodAwaitParts key (← node.getObjVal? "value")
  unless parts.scope.closes.all captures.contains do
    throw s!"method {key} has a capture absent from the linked closure"
  return ⟨f, ⟨parts.scope, parts.before, parts.operand, parts.after⟩, resultBinding⟩

/-- Record additional body diagnostics without replacing ordinary class syntax
or granting a host premise. Each payload is tied to its numbered closure. -/
def decode (source : Json) (entry : String) : Except String Input := do
  let nodes ← ESTreeUnits.nodes source
  let decoded ← LinkedInput.decodeSingletonSourceNodes nodes entry
  unless decoded.source.imports.isEmpty && !decoded.hasTemplates do
    throw "async method turn requires an import-free original without templates"
  let mut methods := []
  let mut refusals := []
  for (node, id) in nodes.zipIdx do
    if (node.getObjValAs? String "type").toOption != some "ClassDeclaration" then continue
    let some (.decl _ _ (.cls _ _ _ members)) := decoded.source.body.lookup id | continue
    let body ← node.getObjVal? "body" >>= (·.getObjValAs? (Array Json) "body")
    for member in body.filter publicInstanceAsync do
      match lowerMethod id members member with
      | .ok method => methods := method :: methods
      | .error why => refusals := why :: refusals
  return ⟨decoded.program, methods.reverse, refusals.reverse⟩

private def invoke (methods : List Method) (fuel : Nat) (callee receiver : Val)
    (args : List Val) : TurnM W Val :=
  match fuel with
  | 0 => TurnM.lift outOfFuel
  | n + 1 =>
    dispatchCallWithDeferred TurnM.lift
      (fun f closed this argv => TurnM.lift (runLocalCall (execStmts n) f closed this argv))
      (fun f closed this argv reason =>
        match methods.find? (fun m => sameDeferred m.declaration f) with
        | some method => invokeAwait method.body closed this argv method.resultBinding fuel
        | none => TurnM.lift (unknown s!"deferred function {f.name}: {reason}"))
      (fun code cells told => TurnM.lift (converse n code cells told)) callee receiver args

private def methodCall? : Stmt → Option (Option Name × Expr × Expr × List Expr)
  | .decl .const target (.callMethod receiver key args) => some (some target, receiver, key, args)
  | .expr (.callMethod receiver key args) => some (none, receiver, key, args)
  | _ => none

private def runUnit (methods : List Method) (fuel : Nat) (unit : LinkedUnit) : TurnM W Unit :=
  match fuel, methodCall? unit.stmt with
  | n + 2, some (target, receiver, key, args) => do
    TurnM.lift (modifyState fun s => { s with region := unit.id + 1 })
    let base ← TurnM.lift (evalExpr n unit.env receiver)
    let property ← TurnM.lift (evalExpr n unit.env key)
    let callee ← TurnM.lift (getProp (callFn n) base property)
    let result ← invoke methods n callee base (← TurnM.lift (evalArgs n unit.env args))
    if let some name := target then
      TurnM.lift (initVar unit.env name result)
    TurnM.lift (modifyState fun s => { s with region := 0 })
  | _, _ => do
    let _ ← TurnM.lift (CoreJS.runUnit fuel unit.env unit.id unit.stmt)
    pure ()

def runImmediate (w : World) (fuel : Nat) (input : Input) : TurnResult w Env :=
  withLinkedTurn w input.program fun linked turn =>
    match entryEnv input.program.entry linked with
    | none => .unknown "expected the linked entry module" turn
    | some env =>
      if input.methods.isEmpty then
        TurnResult.andThen (CoreJS.runUnits fuel (unitsOf linked)) turn fun () done => .ok env done
      else
        ((unitsOf linked).forM (runUnit input.methods fuel) *> pure env : TurnM w Env) turn

end CoreJS.LinkedAsyncMethod
