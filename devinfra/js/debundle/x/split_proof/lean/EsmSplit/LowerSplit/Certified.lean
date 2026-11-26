import EsmSplit.Cli
import EsmSplit.ESTreeEmit
import EsmSplit.LowerSplit.Input

/-! Certify a proposed split and publish its output only when a constructed
source-to-output rule proves the strict public contract. -/

namespace EsmSplit.LowerSplit

open Lean EsmSplit.SplitFiles

private def requestedGuarantee : String := "all-outcomes"

private def emitRefusal (requested : String) (refusal : CertificationRefusal) : IO UInt32 := do
  let reasonCodeFields := refusal.reasonCode.toList.map fun code => ("reason_code", toJson code)
  let observationFields := refusal.observation.toList.map fun observation => ("observation", observation)
  IO.println (Json.mkObj ([
    ("requested", toJson requested),
    ("scope", Json.null),
    ("every_ending_matches", toJson false),
    ("status", toJson "rejected"),
    ("guarantee", Json.null),
    ("reason", toJson refusal.reason),
  ] ++ reasonCodeFields ++ observationFields)).compress
  return 1

private def noConstructedRule : CertificationRefusal := {
  reason := "no constructed rule currently covers all-outcomes strict JavaScript source-to-output observations for this split; no output was published"
  reasonCode := some "no_constructed_rule"
}

private def parseCertifiedArgs (args : List String) : IO (System.FilePath × System.FilePath) := do
  let usage := IO.userError "usage: lowersplit <deployment.json> <outdir>; strategy, guarantee, host, deployment-origin, diagnostic, generator, and check-output options are not accepted"
  let [manifest, out] := args | throw usage
  if args.any (·.startsWith "--") then throw usage
  let out ← IO.ofExcept (outputDirectory out)
  return (manifest, out)

private structure Part where
  original : Json
  bundle : Bundle
  spec : Spec
  plan : Plan
  output : OutputPart

private def prepare (chunk : DeploymentChunk) (input : BundleInput) (work : System.FilePath)
    (sourcePath : String) (timings : Bool) (retainEntryRoute := false) : IO Part := do
  let { program, source, input } := input
  let asts ← IO.ofExcept (ESTreeUnits.checkedNodes program source)
  if let some reason := asts.findSome? unsupportedStatement then throw (IO.userError reason)
  let units := Effects.footprintUnits asts
  let B := linkedLayoutBundle asts units
  let S ← IO.ofExcept (chunk.spec asts.size)
  for name in S.entry :: S.modules.map Prod.fst do
    let moduleExtension := name.endsWith ".mjs" || (retainEntryRoute && name == sourcePath && name.endsWith ".js")
    unless CoreJS.ModuleInput.supportedFilename name && moduleExtension do
      throw (IO.userError s!"certified output requires a safe relative .mjs path (.js is supported only for a retained deployment entry), got {name}")
  for (ast, i) in asts.zipIdx do
    if let some reason := entryPlacementProblem B S ast i then throw (IO.userError reason)
  let P ← planSplit B S timings
  let programs ← timePhase timings "output_ast" fun _ =>
    IO.ofExcept (ESTreeEmit.programs asts B P source sourcePath)
  return { original := program, bundle := B, spec := S, plan := P, output := { programs := programs, source := source, input := input, work := work } }

private def checkPartLayout (part : Part) (timings : Bool) : IO Unit := do
  let ok ← timePhase timings "plan_checks" fun _ => checkLayout part.bundle part.plan (some part.spec) false
  unless ok do throw (IO.userError "source placement or module linkage checks failed")

private def runSingleton (chunk : DeploymentChunk) (work : System.FilePath) (timings : Bool) : IO UInt32 := do
  let src ← readBundle chunk.source work true timings (← IO.ofExcept chunk.sourceHash)
  let part ← prepare chunk src work "bundle.mjs" timings
  checkPartLayout part timings
  let (entry, root) ← if javascriptSource chunk.source then
      let path ← IO.FS.realPath chunk.source
      pure (path.fileName.getD "bundle.mjs", path.parent)
    else pure ("bundle.mjs", none)
  let _providers ← loadProviders entry (Std.HashMap.ofList [(entry, part.original)]) root work (allowEntryImport := false)
  emitRefusal requestedGuarantee noConstructedRule

private def runDeployment (manifest : DeploymentManifest) (work : System.FilePath) (timings : Bool) : IO UInt32 := do
  let root ← deploymentRoot manifest
  let mut originals := #[]
  let mut parts := #[]
  for (chunk, index) in manifest.chunks.zipIdx do
    let dir := work / toString index
    IO.FS.createDir dir
    match chunk.placement with
    | .fixed =>
      originals := originals.push (chunk.path, ← loadProgram chunk.source dir true)
    | _ =>
      let source ← readBundle chunk.source dir true timings (← IO.ofExcept chunk.sourceHash)
      let part ← prepare chunk source dir chunk.path timings (retainEntryRoute := true)
      checkPartLayout part timings
      parts := parts.push part
      originals := originals.push (chunk.path, source.program)
  if parts.isEmpty then throw (IO.userError "expected at least one chunk placement")
  let paths := parts.toList.flatMap fun part => (part.plan.mods.map (·.name)).filter (· != part.plan.entry)
  checkNewRoutes root work paths
  let supplied := Std.HashMap.ofList originals.toList
  let providers ← timePhase timings "provider_loading" fun _ =>
    loadProviders manifest.entry supplied (some root) work
  let reachable := Std.HashSet.ofList (manifest.entry :: providers.map Prod.fst)
  for (name, _) in originals do
    unless reachable.contains name do throw (IO.userError s!"original chunk is unreachable from the entry: {name}")
  let _someMain ← match supplied[manifest.entry]? with
    | some main => pure main
    | none => throw (IO.userError "missing deployment entry")
  emitRefusal requestedGuarantee noConstructedRule

def certified (args : List String) : IO UInt32 := do
  let requested := requestedGuarantee
  try
    let (manifestPath, _out) ← parseCertifiedArgs args
    let manifest ← readDeploymentManifest manifestPath
    let timings := (← IO.getEnv "SPLIT_PROOF_TIMINGS") == some "1"
    IO.FS.withTempDir fun work =>
      match manifest.chunks with
      | [chunk] => runSingleton chunk work timings
      | _ => runDeployment manifest work timings
  catch e => emitRefusal requested { reason := e.toString }

end EsmSplit.LowerSplit
