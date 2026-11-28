import EsmSplit.CoreJS.RetainedTailConstruction
import EsmSplit.LowerSplit.Input

/-! Exercise the reached retained operation in the actual parsed original
and readback graphs. No world from here is passed to the compiler. -/

open Lean EsmSplit EsmSplit.LowerSplit CoreJS

private inductive Ending where
  | normal | throws | unknown
  deriving DecidableEq

/-- Read a lexical capture, allocate an object, write the value to the object,
then return, throw, or report an unknown outcome. Host state counts the turns. -/
private def world (ending : Ending) (expected : Nat) : World where
  σ := Nat
  init := 0
  step := fun _ _ _ s => (.unknown "unexpected primitive host call", s)
  talk := fun told turn =>
    let request := match turn, told with
      | 0, .run code 0 (.prim .undef) (.prim .undef) [] false =>
          if code = expected then .readVar 0 else .unknown "wrong retained unit"
      | 1, .value (.prim (.num _)) => .newObject none
      | 2, .value (.handle _) => .readVar 0
      | 3, .value (.prim (.num value)) => .set (.handle 0) (.prim (.str "seen")) (.prim (.num value))
      | 4, .value (.prim .undef) =>
          match ending with
          | .normal => .done (.prim .undef)
          | .throws => .throw (.prim (.str "after write"))
          | .unknown => .unknown "after write"
      | _, _ => .unknown "unexpected retained call or reply"
    (request, turn + 1)

private def checkWrites {ending : Ending} {code : Nat}
    (state : State (world ending code)) (expected : Nat) : Except String Unit := do
  unless (show Nat from state.world.state) == 5 do throw "retained call did not reach its final turn"
  unless state.world.handed == #[⟨code + 1, 0⟩] do throw "lost retained allocation identity"
  let some obj := state.objAt ⟨code + 1, 0⟩ | throw "retained allocation is missing"
  let some (.data (.num value) true true true) := obj.props.lookup (.str "seen") |
    throw "retained write is missing"
  unless value.toBits == (Float.ofNat expected).toBits do throw "retained read selected the wrong capture"
  let region := if ending = .normal then 0 else code + 1
  unless state.region == region do throw "the outcome lost the current allocation region"

private def observe (program : Program) (code expected : Nat) : Except String Unit := do
  -- At these bounds the original prefix or a reached conversation exhausts.
  for fuel in [0, 2, 5] do
    unless (runProgram (world .normal code) fuel program matches .fuel) do
      throw s!"expected fuel exhaustion at {fuel}"
  let .ok () succeeded := runProgram (world .normal code) 32 program |
    throw "reached call did not complete"
  checkWrites succeeded expected
  let .throw (.str "after write") thrown := runProgram (world .throws code) 32 program |
    throw "expected the retained throw"
  checkWrites thrown expected
  let .unknown "after write" stopped := runProgram (world .unknown code) 32 program |
    throw "expected the retained unknown outcome"
  checkWrites stopped expected

/-- Invoke the hoisted function through the entry's capture. Observe a write
before either returning to the caller or throwing back through it. -/
private def publicWorld (throws : Bool) : World where
  σ := Nat
  init := 0
  step := fun _ _ _ s => (.unknown "unexpected primitive host call", s)
  talk := fun told turn =>
    let request := match turn, told with
      | 0, .run 1 0 (.prim .undef) (.prim .undef) [] false => .readVar 0
      | 1, .value (.handle 0) => .call (.handle 0) (.prim .undef) []
      | 2, .run 0 1 (.handle 0) (.prim .undef) [] false => .newObject none
      | 3, .value (.handle 1) => .set (.handle 1) (.prim (.str "called")) (.prim (.bool true))
      | 4, .value (.prim .undef) =>
        if throws then .throw (.prim (.str "after call write")) else .done (.handle 1)
      | 5, .value (.handle 1) => .done (.prim .undef)
      | 5, .threw (.prim (.str "after call write")) => .throw (.prim (.str "after call write"))
      | _, _ => .unknown "unexpected public call or reply"
    (request, turn + 1)

private def observePublic (program : Program) (name : String) (throws : Bool) : Except String Addr := do
  let w := publicWorld throws
  let some [(published, cell)] := LinkedBehavior.exports w program |
    throw "expected exactly one resolved public cell"
  unless published == name do throw "wrong public name"
  let state ← match runProgram w 32 program with
    | .ok () s => if throws then throw "expected the reached call to throw" else pure s
    | .throw (.str "after call write") s =>
      if throws then pure s else throw "expected the reached call to return"
    | _ => throw "did not reach the expected call outcome"
  let some (.obj callee) := (state.cellAt cell).bind (·.value) |
    throw "the public cell must hold the hoisted function"
  unless (show Nat from state.world.state) == 6 && state.world.handed[0]? == some callee do
    throw "the reached call did not invoke the exported function"
  let some returned := state.world.handed[1]? | throw "missing callback allocation"
  let some obj := state.objAt returned | throw "lost callback allocation"
  unless (obj.props.lookup (.str "called") matches some (.data (.bool true) true true true)) do
    throw "lost the effect before the return or throw"
  return cell

def main (args : List String) : IO Unit := do
  let [sourceFile, requestFile, outputRoot, value] := args |
    throw (IO.userError "expected SOURCE REQUEST OUTPUT_ROOT CAPTURE_VALUE_OR_PUBLIC_NAME")
  let request ← loadAuthoredSpec requestFile
  IO.FS.withTempDir fun work => do
    let source ← readBundle sourceFile work true false
    let nodes ← IO.ofExcept (ESTreeUnits.checkedNodes source.program source.source)
    let built ← IO.ofExcept (RetainedTailConstruction.construct nodes request)
    unless built.certificate.1 == .allOutcomes do
      throw (IO.userError "the constructed certificate is weaker than all outcomes")
    let files ← SplitFiles.readModules outputRoot work (built.plan.mods.map (·.name)).toArray
    IO.ofExcept (RetainedTailConstruction.checkOutput built source.program files)
    let decoded ← IO.ofExcept <|
      (LinkedInput.decodeStructuralSingleton source.program (Json.mkObj []) files built.plan built.pivots).mapError
        LinkedBehavior.StructuralRefusal.reason
    if built.pivots.isEmpty then
      for throws in [false, true] do
        let before ← IO.ofExcept (observePublic decoded.before value throws)
        let after ← IO.ofExcept (observePublic decoded.after value throws)
        unless before == after do throw (IO.userError "the exported binding moved to a different cell")
    else
      let some expected := value.toNat? | throw (IO.userError "expected numeric capture")
      for program in [decoded.before, decoded.after] do
        IO.ofExcept (observe program built.pivots.length expected)
