import EsmSplit.CoreJS.EntryExports
import EsmSplit.CoreJS.Eval
import EsmSplit.CoreJS.LinkedPrimitiveAwait
import EsmSplit.CoreJS.PrimitiveAwait
import EsmSplit.CoreJS.PrimitiveAwaitCorrespondence
import EsmSplit.CoreJS.StateMatch
import EsmSplit.CoreJS.SyntaxMatch
import EsmSplit.ESTreeEmit
import EsmSplit.ESTreeUnits
import EsmSplit.LowerSplit.Input
import EsmSplit.Plan
import EsmSplit.SplitFiles

/-! Replay a real staged graph in the explicit await model. The gate's callback
always refuses publication, even when the finite witness matches. -/

open Lean (Json toJson)
open CoreJS CoreJS.PrimitiveAwait

private def placement (swap abrupt : Bool) : EsmSplit.Plan :=
  let calls := if swap then [6, 5] else [5, 6]
  let entry := calls ++ [7, 8, 9] ++ (if abrupt then [10] else [])
  let imported := ["trace", "left", "right", "setValue"]
  ⟨"entry.mjs", [⟨"entry.mjs", entry, [("worker.mjs", imported)], []⟩,
    ⟨"worker.mjs", [0, 1, 2, 3, 4], [], imported.map fun name => (name, name)⟩]⟩

private def importedCapturePlacement : EsmSplit.Plan :=
  ⟨"entry.mjs", [⟨"entry.mjs", List.range' 1 9, [("worker.mjs", ["trace"])], []⟩,
    ⟨"worker.mjs", [0], [], [("trace", "trace")]⟩]⟩

/-- Produce the same pointwise premise the drain theorem consumes. The
scheduler already projects each environment to the code's used addresses. -/
private def matchJob (a b : Job) : Option (PLift (Job.Corresponds a b)) := do
  let ⟨promise⟩ ← SyntaxMatch.scalarEq a.promise b.promise
  let ⟨region⟩ ← SyntaxMatch.scalarEq a.region b.region
  let ⟨body⟩ ← SyntaxMatch.listEq SyntaxMatch.stmtEq a.body b.body
  let ⟨env⟩ ← SyntaxMatch.scalarEq a.env b.env
  return ⟨⟨promise, region, body, ⟨congrArg Env.this env, congrArg Env.home env,
    fun _ _ => by rw [env]⟩⟩⟩

private def matchQueue : (a b : List Job) → Option (PLift (QueueCorresponds a b))
  | [], [] => some ⟨.nil⟩
  | a :: as, b :: bs => do
    let ⟨head⟩ ← matchJob a b
    let ⟨tail⟩ ← matchQueue as bs
    return ⟨.cons head tail⟩
  | _, _ => none

private def exports (p : Program) : Except String EntryExports.Bindings := do
  let .ok linked _ := link p (initialState World.probe p.regions) | throw "link failed"
  EntryExports.resolve p.entry linked

private def value (turn : Turn World.probe) (exported : EntryExports.Bindings) (name : String) :
    Except String Val := do
  let some addr := exported.lookup name | throw s!"missing export {name}"
  let some v := (turn.state.cellAt addr).bind (·.value) | throw s!"uninitialized export {name}"
  return v

private def settled (turn : Turn World.probe) (exported : EntryExports.Bindings) (name : String) :
    Except String PromiseSlot := do
  let .obj addr ← value turn exported name | throw s!"expected Promise export {name}"
  let some result := (turn.state.objAt addr).bind (·.promise) | throw s!"missing Promise slot for {name}"
  return result

private def immediate (input : LinkedPrimitiveAwait.Input) (abrupt : Bool) :
    Except String (Turn World.probe) :=
  match LinkedPrimitiveAwait.runImmediate World.probe 96 input with
  | .ok _ turn => if abrupt then .error "expected a synchronous throw" else .ok turn
  | .throw (.str "stop") turn => if abrupt then .ok turn else .error "unexpected module throw"
  | .throw _ _ => .error "unexpected thrown value"
  | .unknown why _ => .error why
  | .fuel => .error "out of fuel before the checkpoint"

private def drainAll (turn : Turn World.probe) : Except String (Turn World.probe) :=
  match drain 96 2 turn with
  | .ok .empty done => .ok done
  | _ => .error "the queue did not drain"

private def replay (source files : Json) (plan : EsmSplit.Plan) (swap abrupt : Bool) :
    Except String Json := do
  let (a, b) ← LinkedPrimitiveAwait.decodePlaced source files plan
  let ea ← exports a.program
  let eb ← exports b.program
  unless ea.Perm eb do throw "exported names resolve to different addresses"
  let ta ← immediate a abrupt
  let tb ← immediate b abrupt
  let some ⟨stateEq⟩ := StateMatch.probe ta.state tb.state |
    throw "immediate states differ"
  let .str "" ← value ta ea "inTurn" | throw "the caller saw a continuation"
  for name in ["first", "second"] do
    let .pending ← settled ta ea name | throw "the immediate result has a settled Promise"
  if swap then
    unless (matchQueue ta.jobs tb.jobs).isNone do throw "swapped queue was treated as corresponding"
  else
    let some ⟨jobs⟩ := matchQueue ta.jobs tb.jobs | throw "preserving queues do not correspond"
    let correspondence : ∀ quota, (drain 96 quota ta).Corresponds (drain 96 quota tb) := fun quota =>
      drain_corresponds 96 quota ⟨stateEq, jobs⟩
    -- Check the readback premise with executable equality and instantiate the
    -- generic theorem for both draining just the head and draining the queue.
    let _ := correspondence 1
    let _ := correspondence 2
  let finalA ← drainAll ta
  let finalB ← drainAll tb
  unless swap || (StateMatch.probe finalA.state finalB.state).isSome do
    throw "preserving placement finished with a different state"
  let .str "A7B7" ← value finalA ea "trace" | throw "source drain saw the wrong trace"
  let .str observed ← value finalB eb "trace" | throw "expected a live trace export"
  unless observed == (if swap then "B7A7" else "A7B7") do throw "candidate drain saw the wrong trace"
  let .rejected (.str "left") ← settled finalB eb "first" | throw "lost rejection"
  let .fulfilled (.num n) ← settled finalB eb "second" | throw "lost fulfillment"
  unless n == 7 do throw "continuation read a stale captured value"
  return Json.mkObj [
    ("trace", toJson observed), ("swapped", toJson swap), ("abrupt", toJson abrupt),
    ("exports", toJson (eb.map Prod.fst)), ("pending", toJson tb.jobs.length)]

def main (args : List String) : IO Unit := do
  let [path, output, mode] := args | throw (IO.userError "expected source, output and mode")
  let input : System.FilePath := path
  let out : System.FilePath := output
  let swap := mode == "swap"
  let abrupt := mode == "abrupt"
  let importedCapture := mode == "imported-capture"
  let plan := if importedCapture then importedCapturePlacement else placement swap abrupt
  IO.FS.withTempDir fun work => do
    let ast ← EsmSplit.SplitFiles.loadProgram input work true
    let source ← IO.FS.readFile input
    let nodes ← IO.ofExcept (ESTreeUnits.checkedNodes ast source)
    let programs ← IO.ofExcept (EsmSplit.ESTreeEmit.programs nodes
      (EsmSplit.LowerSplit.sourceBundle nodes) plan source)
    let part : EsmSplit.SplitFiles.OutputPart := ⟨programs, source, input, work⟩
    let observed ← IO.mkRef (none : Option Json)
    let result ← EsmSplit.SplitFiles.certifyOutput (α := Unit) [part] (Json.mkObj []) out work false false fun files => do
      if importedCapture then
        match LinkedPrimitiveAwait.decodePlaced ast files plan with
        | .error "placed async declaration 2 captures imported binding trace" =>
          observed.set (some (Json.mkObj [("importedCaptureRefused", toJson true)]))
          return .error { reason := "test-only checkpoint refuses publication" }
        | .error reason => return .error { reason }
        | .ok _ => return .error { reason := "reused original cut even though writes would target an import" }
      match replay ast files plan swap abrupt with
      | .ok report =>
        observed.set (some report)
        return .error { reason := "test-only checkpoint refuses publication" }
      | .error why => return .error { reason := why }
    let .error refusal := result | throw (IO.userError "test-only observation published output")
    let some report ← observed.get | throw (IO.userError refusal.reason)
    if ← out.pathExists then throw (IO.userError "test-only observation created the output path")
    IO.println report.compress
