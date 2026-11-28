import EsmSplit.CoreJS.AuthenticatedSource
import EsmSplit.CoreJS.LinkWorld
import EsmSplit.CoreJS.OpaqueCaller

open CoreJS CoreJS.LinkedBehavior
open Lean (Json toJson)

private inductive Ending where
  | returns | throws | refuses

private def world (ending : Ending) (slot : Nat) : World where
  σ := Nat
  init := 0
  step := fun _ _ _ s => (.unknown "unexpected host function", s)
  talk := fun told s =>
    let request := match s, told with
      | 0, .run 1 2 _ receiver (callback :: argv) false => .call callback receiver argv
      | 1, .value (.prim (.str "argument")) => .writeVar slot (.prim (.str "written"))
      | 2, .value _ => .prim .log [.prim (.str "world effect")]
      | 3, .value _ =>
        match ending with
        | .returns => .done (.prim (.str "result"))
        | .throws => .throw (.prim (.str "result"))
        | .refuses => .unknown "world refused after effects"
      | _, _ => .unknown "unexpected conversation"
    (request, s + 1)

private instance (ending : Ending) (slot : Nat) : DecidableEq (world ending slot).σ :=
  inferInstanceAs (DecidableEq Nat)

private def entry := "public.mjs"
private def provider := "capture.mjs"
private def worker := "private.mjs"
private def captureName := "__getOwnPropNames"
private def publicNames := [("factory", "__commonJS"), ("sameFactory", "__commonJS")]

-- Give both distinct mutable cells the same initial value. Their addresses
-- still matter because World.talk can write through a capture.
private def layout (factory : Stmt) (moved wrongCapture : Bool) : Program :=
  let value : Stmt := .decl .let_ "source" (.str "same value")
  let other : Stmt := .decl .let_ "alternate" (.str "same value")
  let fixed : Module :=
    ⟨provider, [], [(0, value), (1, other)], [("source", "source"), ("alternate", "alternate")]⟩
  let edge : Import := ⟨provider, [(if wrongCapture then "alternate" else "source", captureName)]⟩
  if moved then
    ⟨entry, [fixed, ⟨worker, [edge], [(2, factory)], [("__commonJS", "__commonJS")]⟩,
      ⟨entry, [⟨worker, [("__commonJS", "__commonJS")]⟩], [], publicNames⟩]⟩
  else
    ⟨entry, [fixed, ⟨entry, [edge], [(2, factory)], publicNames⟩]⟩

private def prospectiveSite (bindings : EntryExports.Bindings) (name : String) :
    Except String Addr := do
  let some cell := bindings.lookup name | throw s!"missing cell {name}"
  return cell

private def checkWorld (identity : OpaqueCaller.CodeIdentity) (ending : Ending) :
    Except String String := do
  let some (slot, _) := identity.captureNames.zipIdx.findSome? (fun (name, index) =>
    if name == captureName then some (index, name) else none)
    | throw s!"missing linked capture {captureName}: {identity.captureNames}"
  let w := world ending slot
  let before := layout identity.declaration false false
  let after := layout identity.declaration true false
  let wrong := layout identity.declaration true true
  let beforeLink ← checkLink before
  let afterLink ← checkLink after
  let bindings ← OpaqueCaller.checkPublic entry beforeLink.linked afterLink.linked
  let originalCell ← prospectiveSite bindings "factory"
  let relocatedAlias ← prospectiveSite (← EntryExports.resolve entry afterLink.linked) "sameFactory"
  unless originalCell == relocatedAlias do throw "public alias lost its cell"
  let wrongPublic := afterLink.linked.map fun (m, e) =>
    if m.name == entry then ({m with exports := m.exports.map fun (pub, _) => (pub, "wrong")},
      { e with vars := ("wrong", ⟨1, 0⟩) :: e.vars })
    else (m, e)
  unless (OpaqueCaller.checkPublic entry beforeLink.linked wrongPublic).isOk == false do
    throw "accepted wrong public cells"
  let setup (program : Program) : Except String ((Addr × Val) × Turn w) := do
    let .ok () ready := runProgram w 128 program | throw "projection prefix did not complete"
    let load : M w (Addr × Val) := do
      let factory ← readCell originalCell
      let loader ← callFn 64 factory .undef [.undef, .undef]
      let cell ← newCell ⟨some loader, false⟩
      let callback ←
        makeFunc ⟨[], .undef, none⟩ (.mk "callback" .normal false ["input"]
        [.expr (.prim .log [.this]), .ret (.var "input")])
      return (cell, callback)
    let .ok (cell, callback) incoming := load ready | throw "could not reach returned closure and reentry callback"
    return ((cell, callback), ⟨incoming, [⟨⟨42, 0⟩, 42, ⟨[], .undef, none⟩, []⟩]⟩)
  let ((cell, cb), incoming) ← setup before
  let ((movedCell, cb'), relocated) ← setup after
  let ((wrongCell, cbWrong), wrongTurn) ← setup wrong
  let site (cell : Addr) (callback : Val) : OpaqueCaller.Site :=
    ⟨cell, .str "receiver", [callback, .str "argument"]⟩
  let left := site cell cb
  let right := site movedCell cb'
  let checked ← OpaqueCaller.check identity left right incoming relocated
  let .error why := OpaqueCaller.check identity left (site wrongCell cbWrong) incoming wrongTurn
    | throw "accepted a different equal-valued capture cell"
  unless why == "ordered capture cells differ" do throw why
  let .error receiver := OpaqueCaller.check identity left { right with receiver := .str "other" }
      incoming relocated | throw "accepted a changed receiver"
  unless receiver == "resolved receiver differs" do throw receiver
  let .error argv := OpaqueCaller.check identity left { right with args := [cb', .str "other"] }
      incoming relocated | throw "accepted changed arguments"
  unless argv == "resolved arguments differ" do throw argv
  let .error state := OpaqueCaller.check identity left right incoming
      { relocated with state := { relocated.state with out := #["previous effect"] } }
      | throw "accepted a changed incoming log"
  unless state == "incoming state differs" do throw state
  let .error queue := OpaqueCaller.check identity left right incoming { relocated with jobs := [] }
    | throw "accepted a changed incoming queue"
  unless queue == "incoming job queue differs" do throw queue
  let some capture := checked.captures[slot]?.join | throw "expected the selected captured binding"
  let beforeAlias ← prospectiveSite (← EntryExports.resolve provider beforeLink.linked) "source"
  let alternate ← prospectiveSite (← EntryExports.resolve provider beforeLink.linked) "alternate"
  unless capture == beforeAlias && capture != alternate &&
      (incoming.state.cellAt capture).bind (·.value) == (incoming.state.cellAt alternate).bind (·.value) do
    throw "capture discriminator was not initialized as intended"
  have _all := checked.agrees
  let run fuel := callFnTurn fuel checked.leftCallee left.receiver left.args incoming
  let .fuel := run 0 | throw "zero fuel was not preserved"
  let checkEffects (final : Turn w) : Except String Unit := do
    unless final.state.out == #["receiver", "world effect"] &&
        (final.state.cellAt capture).bind (·.value) == some (.str "written") &&
        (final.state.cellAt alternate).bind (·.value) == some (.str "same value") &&
        (show Nat from final.state.world.state) == 4 && final.jobs.map Job.region == [42] do
      throw "call changed the captured write, reentry log, world state or queue"
  match ending, run 128 with
  | .returns, .ok (.str "result") final => checkEffects final; pure "normal"
  | .throws, .throw (.str "result") final => checkEffects final; pure "throw"
  | .refuses, .unknown "world refused after effects" final => checkEffects final; pure "unknown-after-effects"
  | _, _ => throw "expected the scripted outcome after reaching World.talk"

private def test (input : Json) : Except String Json := do
  let pinned ← input.getObjVal? "pinned"
  let text ← pinned.getObjValAs? String "sourceText"
  let ordinal ← input.getObjValAs? Nat "ordinal"
  let source ← input.getObjVal? "source"
  let reread ← input.getObjVal? "reread"
  let .error refusal := CoreJS.AuthenticatedSource.decodeSourceClassification pinned text entry
    | throw "the whole pinned source now lowers; update the trial"
  unless refusal == "statement 0: unsupported global Object" do throw refusal
  let providers ← input.getObjVal? "providers"
  let identity ← OpaqueCaller.authenticate pinned source reread ordinal 2 1 providers
  let .error mismatch := OpaqueCaller.authenticate pinned source (← input.getObjVal? "badBody") ordinal 2 1 providers
    | throw "accepted different deferred source under the same ordinal"
  let ends ← [Ending.returns, .throws, .refuses].mapM (checkWorld identity)
  return Json.mkObj [("fullSource", toJson refusal), ("trial", toJson (ends ++ ["fuel"])),
    ("changedCode", toJson mismatch)]

def main : IO UInt32 := do
  let text ← (← IO.getStdin).getLine
  match Json.parse text >>= test with
  | .ok output => IO.println output.compress; pure 0
  | .error why => IO.eprintln why; pure 1
