import EsmSplit.Cli
import EsmSplit.CoreJS.IndexedCheckedSplit
import EsmSplit.CoreJS.IndexedSourceSchedule
import EsmSplit.CoreJS.OrderedArrowInit
import EsmSplit.ESTreeEmit
import EsmSplit.LowerSplit.Input
import EsmSplit.SplitFiles

open Lean (Json toJson)
open CoreJS EsmSplit EsmSplit.SplitFiles CoreJS.LinkedBehavior

private def ensure (ok : Bool) (why : String) : Except String Unit :=
  unless ok do
    throw why

private def addr (a : Addr) : Json :=
  toJson [a.region, a.index]

private def valTag : Val → String
  | .undef => "undefined"
  | .obj _ => "object"
  | .host name => "host " ++ name
  | .str value => value
  | .bool value => toString value
  | _ => "other"

private def resultTag {w : World} : Res w Val → String
  | .ok value _ => valTag value
  | .unknown why _ => "unknown: " ++ why
  | .throw (.error .type why) _ => "TypeError: " ++ why
  | .throw _ _ => "throw"
  | .fuel => "fuel"

private def cell (env : Env) (name : CoreJS.Name) : Except String Addr := do
  let some a := env.vars.lookup name | throw ("missing cell for " ++ name)
  return a

private def entryEnv {p : Program} (checked : CheckedLink p World.probe) : Except String Env := do
  let some (_, env) :=
    checked.linked.find? (fun m => m.1.name == p.entry) | throw "missing entry scope"
  return env

private def closureAt (state : State World.probe) (binding : Addr) :
    Except String (Addr × Func × Env) := do
  let some (.obj a) := (state.cellAt binding).bind (·.value) | throw "expected assigned closure"
  let some (.closure f env _) := (state.objAt a).bind (·.code) | throw "expected closure code"
  return (a, f, env)

private def elem (kind : String) : M World.probe Val := do
  let points ← alloc (arrayObject [(.num 1)])
  let elem ←
    alloc
        { proto := some objectProto,
          props :=
            [("type", .data (.str kind) true true true), ("width", .data (.num 5) true true true),
              ("height", .data (.num 0) true true true),
              ("points", .data (.obj points) true true true)] }
  return .obj elem

private def audit (p : Program) : Except String Json := do
  let checked ← checkLink p
  let env ← entryEnv checked
  let consumer ← cell env "Lm"
  let size ← cell env "Vc"
  let freeDraw ← cell env "st"
  let linear ← cell env "ye"
  let linearType ← cell env "gs"
  let names := [freeDraw, linear, linearType, size, consumer]
  for binding in names do
    ensure (decide ((checked.ready.cellAt binding).bind (·.value) = some .undef))
        "var cell initialized too early"
  let .ok () state := runProgram World.probe 128 p | throw "arrow initialization did not complete"
  let closures ← names.mapM (closureAt state)
  let ids := closures.map Prod.fst
  ensure (decide ids.Nodup) "arrows did not allocate distinct closures"
  let (_, filter, filterEnv) ← closureAt state consumer
  let (_, small, smallEnv) ← closureAt state size
  ensure (filter.opaqueBody.isNone && small.opaqueBody.isNone) "unexpected deferred outer function"
  ensure
      (decide
        (filterEnv.vars.lookup "Vc" = some size && smallEnv.vars.lookup "st" = some freeDraw &&
          smallEnv.vars.lookup "ye" = some linear))
      "actual closure frames differ"
  let invoke (which : Addr) (args : List Val) : M World.probe Val := do
    callFn 128 (← readCell which) .undef args
  let predicate :=
    resultTag
      ((do
          invoke size [← elem "freedraw"])
        state)
  ensure (predicate == "true") "the original small free-draw branch did not run"
  let ordinaryArray : M World.probe Val := do
    let receiver ← alloc (arrayObject [← elem "freedraw"])
    invoke consumer [.obj receiver]
  let .unknown reason atBoundary :=
    ordinaryArray state | throw "expected a filter coverage boundary"
  ensure (reason == "host function Array.prototype.filter is called") reason
  let overridden : M World.probe Val := do
    let receiver ←
      alloc
          { proto := some objectProto,
            props := [("filter", .data (← readCell linearType) true true true)] }
    invoke consumer [.obj receiver]
  let replacement := resultTag (overridden state)
  ensure (replacement == "false") s!"the receiver's own filter returned {replacement}"
  let arrayOverride : M World.probe Val := do
    let receiver ← alloc (arrayObject [← elem "freedraw"])
    createData receiver "filter" (← readCell linearType)
    invoke consumer [.obj receiver]
  let arrayOverrideResult := resultTag (arrayOverride state)
  ensure (arrayOverrideResult == "unknown: a property is defined on an array") arrayOverrideResult
  let linkedRead := resultTag (readCell freeDraw checked.ready)
  let evaluatedRead := resultTag (readCell freeDraw state)
  let some exports := LinkedBehavior.exports World.probe p | throw "missing public cells"
  ensure (atBoundary.world.handed.isEmpty) "unexpected host conversation during filter dispatch"
  return Json.mkObj
      [("closureAddresses", toJson (ids.map addr)),
        ("exportCells", Json.mkObj (exports.map fun (name, binding) => (name, addr binding))),
        ("early", toJson linkedRead), ("late", toJson evaluatedRead),
        ("smallFreeDraw", toJson predicate), ("filter", toJson reason),
        ("ownFilter", toJson replacement), ("arrayOverride", toJson arrayOverrideResult)]

private def inspect (input : Json) : IO Json := do
  let sourceFile : System.FilePath := ⟨← IO.ofExcept (input.getObjValAs? String "source")⟩
  let output : System.FilePath := ⟨← IO.ofExcept (input.getObjValAs? String "out")⟩
  let spec ← IO.ofExcept (parseSpec (← IO.ofExcept (input.getObjVal? "spec")))
  let checkOnly ← IO.ofExcept (input.getObjValAs? Bool "checkOnly")
  IO.FS.withTempDir fun work => do
      let src ← LowerSplit.readBundle sourceFile work true false none
      let decoded ←
        IO.ofExcept (AuthenticatedSource.decodeIndexedSource src.program src.source spec.entry)
      let bundle := LowerSplit.sourceBundle decoded.nodes
      let schedule ← IO.ofExcept (IndexedSourceSchedule.check decoded bundle spec)
      let candidate := splitProgramIndexed decoded.source.body bundle schedule.plan
      unless candidate.evalOrder == spec.order ++ [spec.entry] do
        throw (IO.userError "changed authored module order")
      let _ ←
        IO.ofExcept
            (checkPlaces (IndexedCheckedSplit.assignments decoded.source.body schedule.plan)
              decoded.program candidate)
      let ⟨proof⟩ ← IO.ofExcept (OrderedArrowInit.check decoded.program candidate)
      let programs ←
        IO.ofExcept (ESTreeEmit.programs decoded.nodes bundle schedule.plan src.source spec.entry)
      let part : OutputPart := { programs, source := src.source, input := src.input, work }
      let gate ←
        certifyOutput (α := Json) [part] (Json.mkObj []) output work checkOnly false fun files =>
            pure <|
              (do
                    let _ ← OrderedArrowInit.checkReadback proof files
                    let observed ← ConstructedOutput.decodeConstructedOutput files candidate
                    let flat ← audit decoded.program
                    let actual ← audit observed
                    ensure (flat == actual) "the concrete observations differ"
                    return actual).mapError
                (fun reason => ({ reason } : CertificationRefusal))
      match gate with
      | .ok report =>
        return report
      | .error e =>
        throw (IO.userError e.reason)

def main : IO UInt32 := do
  let input ← (← IO.getStdin).getLine
  try
    let report ← inspect (← IO.ofExcept (Json.parse input))
    IO.println (Json.mkObj [("report", report)]).compress
  catch e =>
    IO.println (Json.mkObj [("refusal", toJson e.toString)]).compress
  return 0
