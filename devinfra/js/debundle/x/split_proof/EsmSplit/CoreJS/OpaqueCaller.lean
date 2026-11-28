import EsmSplit.CoreJS.CallTurn
import EsmSplit.CoreJS.EntryExports
import EsmSplit.CoreJS.LinkedInput
import EsmSplit.CoreJS.StateMatch
import EsmSplit.ESTreeUnits

/-! A trial for a resolved caller invoking the same authenticated deferred
body. It compares complete results of existing dispatch, not inferred
behavior of the foreign source. -/
namespace CoreJS.OpaqueCaller

open Lean (Json)
open SyntaxMatch (MatchEq listEq scalarEq)

structure CodeIdentity where
  private mk ::
  declaration : Stmt
  private function : Func
  captureNames : List Name

/-- The frontend supplies actual parsed source bytes and positions. The trial
selects one original ASCII-prefixed unit and reads it back in its owner module. Both copies use the same recorded
ordinal and callable index; fixed module metadata supplies captured imports. -/
def authenticate (pinned source reread : Json) (ordinal : Nat) (part : Nat := 1) (ownerOrdinal : Nat := 0)
    (providers : Json := Json.mkObj []) : Except String CodeIdentity := do
  let text ← pinned.getObjValAs? String "sourceText"
  let nodes ← ESTreeUnits.checkedNodes pinned text
  let some node := nodes[ordinal]? | throw "missing original callable row"
  let start ← node.getObjValAs? Nat "start"
  let stop ← node.getObjValAs? Nat "end"
  let through := (text.take stop).toString
  unless through.length == stop && through.toList.all (fun c => c.toNat ≤ 0x7f) do
    throw "callable source prefix is not ASCII"
  let expected := (through.drop start).toString
  let lower (ast : Json) : Except String (Stmt × Func) := do
    let actual ← ast.getObjValAs? String "sourceText"
    let ownerRows ← ESTreeUnits.checkedNodes ast actual
    let some unit := ownerRows[ownerOrdinal]? | throw "missing caller owner row"
    let a ← unit.getObjValAs? Nat "start"
    let b ← unit.getObjValAs? Nat "end"
    let ownerPrefix := (actual.take b).toString
    unless ownerPrefix.length == b && ownerPrefix.toList.all (fun c => c.toNat ≤ 0x7f) &&
        (ownerPrefix.drop a).toString == expected do
      throw "reread deferred source differs from the original slice"
    let p ← LinkedInput.program ast providers (Std.HashSet.ofList [(ownerOrdinal, part)])
    let some (_, stmt@(.decl _ _ (.func outer))) := p.tops.find? (·.1 == ownerOrdinal)
      | throw "expected a callable declaration"
    let selected ← if outer.opaqueBody.isSome then pure outer else do
      let [.ret (.func inner)] := outer.body | throw "expected a factory returning one closure"
      pure inner
    unless selected.opaqueBody.any (fun (code, index, _, reason) => code == ownerOrdinal && index == part && reason.isNone) do
      throw "original callable did not lower to the recorded conversation"
    return (stmt, selected)
  let (stmt, f) ← lower source
  let (other, _) ← lower reread
  unless (SyntaxMatch.stmtEq stmt other).isSome do throw "reread callable identity differs"
  let some (_, _, free, none) := f.opaqueBody | throw "missing recorded captures"
  return ⟨stmt, f, free⟩

structure Site where
  binding : Addr
  receiver : Val
  args : List Val

private def inspect (identity : CodeIdentity) (site : Site) (s : State W) :
    Except String (Val × List (Option Addr)) := do
  let some (.obj a) := (s.cellAt site.binding).bind (·.value) | throw "caller binding does not resolve to an object"
  let some (.closure f closed _) := (s.objAt a).bind (·.code) | throw "callee is not a deferred closure"
  unless (SyntaxMatch.funcEq identity.function f).isSome do throw "callee code is not the authenticated callable"
  let some (_, _, free, none) := f.opaqueBody | throw "callee does not enter World.talk"
  let cells := free.map (closed.vars.lookup ·)
  unless cells.all (·.isSome) do throw "unresolved capture cell"
  return (.obj a, cells)

private def jobEq : MatchEq Job
  | ⟨a, r, e, body⟩, ⟨b, t, f, body'⟩ => do
    let ⟨promise⟩ ← scalarEq a b
    let ⟨region⟩ ← scalarEq r t
    let ⟨env⟩ ← scalarEq e f
    let ⟨stmts⟩ ← listEq SyntaxMatch.stmtEq body body'
    return ⟨promise ▸ region ▸ env ▸ stmts ▸ rfl⟩

/-- Equality is over complete turn results, including output, world state,
handed handles and the queue. Fuel exhaustion exposes no state in TurnResult. -/
structure Checked (left right : Turn W) (before after : Site) where
  leftCallee : Val
  rightCallee : Val
  captures : List (Option Addr)
  agrees : ∀ fuel,
    callFnTurn fuel leftCallee before.receiver before.args left =
    callFnTurn fuel rightCallee after.receiver after.args right

def check [DecidableEq W.σ] (identity : CodeIdentity) (before after : Site)
    (left right : Turn W) : Except String (Checked left right before after) := do
  let (f, captures) ← inspect identity before left.state
  let (g, captures') ← inspect identity after right.state
  unless captures == captures' do throw "ordered capture cells differ"
  let some ⟨callee⟩ := scalarEq f g | throw "resolved callee differs"
  let some ⟨receiver⟩ := scalarEq before.receiver after.receiver | throw "resolved receiver differs"
  let some ⟨args⟩ := scalarEq before.args after.args | throw "resolved arguments differ"
  let some ⟨state⟩ := StateMatch.state left.state right.state | throw "incoming state differs"
  let some ⟨jobs⟩ := listEq jobEq left.jobs right.jobs | throw "incoming job queue differs"
  have turn : left = right := by
    cases left
    cases right
    simp only at state jobs
    cases state
    cases jobs
    rfl
  return ⟨f, g, captures, fun _ => by rw [callee, receiver, args, turn]⟩

/-- Independently compare the public live cells, including any aliases. -/
def checkPublic (entry : String) (before after : List (Module × Env)) :
    Except String EntryExports.Bindings := do
  let a ← EntryExports.resolve entry before
  let b ← EntryExports.resolve entry after
  unless decide (a.Perm b) do throw "public names or live cells differ"
  return a

end CoreJS.OpaqueCaller
