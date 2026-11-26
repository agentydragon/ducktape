import EsmSplit.CoreJS.LinkedInput
import EsmSplit.CoreJS.NamedDeclarationSplit
import EsmSplit.ESTreeEmit
import EsmSplit.LowerSplit.Input

/-! The public splitter publishes only the output of a proved CoreJS constructor. -/

namespace EsmSplit.LowerSplit

open Lean EsmSplit.SplitFiles
open CoreJS (Guarantee)

private def emitVerdict (requested : String) (outcome : Except String Guarantee)
    (scope : Option String := none) (extra : List (String × Json) := []) : IO UInt32 := do
  let everyEndingMatches := match outcome with
    | .ok .allOutcomes => true
    | _ => false
  let fields := match outcome with
    | .error why => [("status", toJson "rejected"), ("guarantee", Json.null), ("reason", toJson why)]
    | .ok level => [("status", toJson "certified"), ("guarantee", toJson level.label)]
  IO.println (Json.mkObj ([("requested", toJson requested), ("scope", toJson scope),
    ("every_ending_matches", toJson everyEndingMatches)] ++ fields ++ extra)).compress
  return if outcome.isOk then 0 else 1

private def emitRefusal (requested : String) (refusal : CertificationRefusal)
    (scope : Option String := none) : IO UInt32 := do
  let reasonCodeFields := refusal.reasonCode.toList.map fun code => ("reason_code", toJson code)
  let observationFields := refusal.observation.toList.map fun observation => ("observation", observation)
  emitVerdict requested (.error (IO.userError refusal.reason).toString) scope (reasonCodeFields ++ observationFields)

private def noConstructedRule (requested : String) : IO UInt32 :=
  emitRefusal requested { reason := "no compiled constructor supports this split; no JavaScript was written",
                         reasonCode := some "no_constructed_rule" }

/-- Apply the named-declaration constructor to its actual decoded source and
print only the modules it returns. A decline never writes to the destination. -/
private def runNamedDeclarations (requested : String) (chunk : DeploymentChunk)
    (out work : System.FilePath) (checkOnly timings : Bool) : IO UInt32 := do
  let .authored spec := chunk.placement | return ← noConstructedRule requested
  unless spec.sequential do return ← noConstructedRule requested
  let src ← readBundle chunk.source work true timings (← IO.ofExcept chunk.sourceHash)
  let .ok source := CoreJS.LinkedInput.decodeSingletonSource src.program spec.entry | return ← noConstructedRule requested
  let [original] := source.modules | return ← noConstructedRule requested
  unless !original.body.isEmpty && original.body.all (fun (_, stmt) =>
      match stmt with | .decl .const _ _ => true | _ => false) do return ← noConstructedRule requested
  unless spec.modules.all (fun row => row.1.endsWith ".mjs") do return ← noConstructedRule requested
  let .ok constructed := CoreJS.NamedDeclarationSplit.construct source spec | return ← noConstructedRule requested
  let programs ← IO.ofExcept (ESTreeEmit.corePrograms constructed.split.program src.source)
  let part : OutputPart := { programs, source := src.source, input := src.input, work }
  let checked ← certifyOutput (α := Unit) [part] (Json.mkObj []) out work checkOnly timings fun _ =>
    pure (.ok ())
  match checked with
  | .ok _ => emitVerdict requested (.ok .allOutcomes) "named-declarations" [("strategy", toJson "compiled-constructor")]
  | .error refusal => emitRefusal requested refusal "named-declarations"

/-- Unsupported requests refuse; supported requests use the compiled constructor. -/
def certified (args : List String) : IO UInt32 := do
  let (level?, args) := EsmSplit.takeFlag "--require" args
  let requested := level?.getD "all-outcomes"
  unless ["all-outcomes", "normal-completion"].contains requested do
    return ← noConstructedRule requested
  let checkOnly := args.contains "--check-output"
  let args := args.filter (· != "--check-output")
  let [manifestPath, out] := args | return ← noConstructedRule requested
  if args.any (·.startsWith "--") then return ← noConstructedRule requested
  try
    let out ← IO.ofExcept (outputDirectory out)
    let manifest ← readDeploymentManifest manifestPath
    let [chunk] := manifest.chunks | return ← noConstructedRule requested
    let timings := (← IO.getEnv "SPLIT_PROOF_TIMINGS") == some "1"
    IO.FS.withTempDir fun work => runNamedDeclarations requested chunk out work checkOnly timings
  catch e => emitVerdict requested (.error e.toString)

end EsmSplit.LowerSplit
