import EsmSplit.ESTreeEmit
import EsmSplit.LowerSplit.CheckedArtifact
import EsmSplit.LowerSplit.Input

/-! Test-only entry point for the checked construction and real file staging. -/

open Lean (Json toJson)
open EsmSplit EsmSplit.SplitFiles EsmSplit.LowerSplit
open CoreJS (Guarantee)

private def run (manifestPath out : System.FilePath) (requested : Guarantee) (checkOnly : Bool) : IO Unit := do
  let manifest ← readDeploymentManifest manifestPath
  let [chunk] := manifest.chunks | throw (IO.userError "the fixture requires one original module")
  let .authored spec := chunk.placement | throw (IO.userError "the fixture requires an authored spec")
  unless javascriptSource chunk.source && (spec.modules.map Prod.fst).all (·.endsWith ".mjs") do
    throw (IO.userError "the fixture requires JavaScript input and .mjs output modules")
  let out ← IO.ofExcept (outputDirectory out)
  let timings := (← IO.getEnv "SPLIT_PROOF_TIMINGS") == some "1"
  IO.FS.withTempDir fun work => do
    let input ← readBundle chunk.source work true timings
    let nodes ← IO.ofExcept (ESTreeUnits.checkedNodes input.program input.source)
    let source ← timePhase timings "source_lowering" fun _ =>
      IO.ofExcept (CoreJS.LinkedInput.decodeSingletonSource input.program spec.entry)
    let built ← timePhase timings "checked_construction" fun _ =>
      IO.ofExcept (CoreJS.CheckedSplit.checkedSplit source spec requested)
    let programs ← timePhase timings "output_ast" fun _ =>
      IO.ofExcept (ESTreeEmit.programs nodes built.bundle built.plan input.source)
    let part : OutputPart := { programs, source := input.source, input := input.input, work }
    let verified ← certifyOutput (α := Unit) [part] (Json.mkObj []) out work checkOnly timings fun files =>
      timePhase timings "checked_correspondence" fun _ =>
        match CheckedArtifact.verify input.program files built with
        | .ok _ => pure (.ok ())
        | .error reason => pure (.error { reason })
    IO.ofExcept (verified.mapError (·.reason))

def main (args : List String) : IO UInt32 := do
  let (level, args) := takeFlag "--require" args
  let requested := level.getD Guarantee.normalCompletion.label
  let checkOnly := args.contains "--check-output"
  let fields := [("requested", toJson requested)]
  try
    let [manifestPath, out] := args.filter (· != "--check-output") |
      throw (IO.userError "expected <deployment.json> <outdir> [--require LEVEL] [--check-output]")
    let guarantee ← match requested with
      | "all-outcomes" => pure Guarantee.allOutcomes
      | "normal-completion" => pure Guarantee.normalCompletion
      | _ => throw (IO.userError "expected all-outcomes or normal-completion")
    run manifestPath out guarantee checkOnly
    IO.println (Json.mkObj (fields ++ [("status", toJson "certified"), ("guarantee", toJson requested)])).compress
    return 0
  catch e =>
    IO.println (Json.mkObj (fields ++ [("status", toJson "rejected"), ("guarantee", .null),
      ("reason", toJson e.toString)])).compress
    return 1
