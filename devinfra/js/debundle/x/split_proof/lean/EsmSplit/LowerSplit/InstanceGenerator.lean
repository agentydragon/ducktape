import EsmSplit.Cli
import EsmSplit.InstanceSource
import EsmSplit.LowerSplit.Input

namespace EsmSplit.LowerSplit

open Lean EsmSplit.SplitFiles

private def parseInstanceGeneratorArgs (args : List String) : IO (System.FilePath × System.FilePath × InstanceSource.Options) := do
  match args with
  | manifest :: output :: options =>
    return (manifest, output, ← InstanceSource.parseOptions {} options)
  | _ => do
    IO.eprintln "usage: split_instance_generator <deployment.json> <Generated.lean> [--namespace NAME] [--variant SPEC]... [--control SPEC]..."
    throw (IO.userError "usage")

/-- Generate the proof-source example without rendering JavaScript modules. -/
def generateInstance (args : List String) : IO UInt32 := do
  let (manifestPath, outputPath, options) ← parseInstanceGeneratorArgs args
  IO.FS.withTempDir fun work => do
    let manifest ← readDeploymentManifest manifestPath
    let [chunk] := manifest.chunks | throw (IO.userError "instance generation requires one original chunk")
    let ⟨program, source, input⟩ ← readBundle chunk.source work false false (← IO.ofExcept chunk.sourceHash)
    let asts ← IO.ofExcept (ESTreeUnits.checkedNodes program source)
    if let some reason := asts.findSome? unsupportedStatement then
      throw (IO.userError reason)
    let units := Effects.footprintUnits asts
    let prem ← Effects.loadPremises none
    let (fps, _) ← analyzeFootprints asts units prem false
    let unitsFile := work / "units.json"
    let reply := work / "texts.json"
    IO.FS.writeFile unitsFile (Json.arr asts).compress
    javascript #["--print-units"] #[unitsFile, input, reply]
    let texts ← IO.ofExcept (fromJson? (α := Array String) (← readJson reply))
    let bundle := bundleOfFootprints asts fps texts
    let spec ← IO.ofExcept (chunk.spec asts.size)
    IO.FS.writeFile outputPath (InstanceSource.render bundle spec options)
    IO.println s!"wrote Lean instance source to {outputPath}"
    return 0

end EsmSplit.LowerSplit
