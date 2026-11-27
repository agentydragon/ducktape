import EsmSplit.Cli
import EsmSplit.CoreJS.IndexedCheckedSplit
import EsmSplit.CoreJS.LinkedInput
import EsmSplit.CoreJS.NamedDeclarationSplit
import EsmSplit.CoreJS.OrderedConstSyntax
import EsmSplit.CoreJS.TerminalExportPlacement
import EsmSplit.CoreJS.TerminalSource
import EsmSplit.ESTreeEmit
import EsmSplit.LowerSplit.Input
import EsmSplit.LowerSplit.SparseArtifact

/-! Certify a proposed split and publish its output or verify the existing tree. -/

namespace EsmSplit.LowerSplit

open Lean EsmSplit.SplitFiles

open CoreJS (Guarantee)

private def emitVerdict (requested : String) (outcome : Except String Guarantee) (scope : Option String := none) (extra : List (String × Json) := []) : IO UInt32 := do
  let everyEndingMatches := match outcome with
    | .ok .allOutcomes => true
    | _ => false
  let fields := match outcome with
    | .error why => [("status", toJson "rejected"), ("guarantee", Json.null), ("reason", toJson why)]
    | .ok level => [("status", toJson "certified"), ("guarantee", toJson level.label)]
  IO.println (Json.mkObj ([
    ("requested", toJson requested),
    ("scope", toJson scope),
    ("every_ending_matches", toJson everyEndingMatches),
  ] ++ fields ++ extra)).compress
  return if outcome.isOk then 0 else 1

/-- Convert a typed refusal into the existing verdict fields at the CLI boundary. -/
private def emitRefusal (requested : String) (refusal : CertificationRefusal)
    (scope : Option String := none) : IO UInt32 := do
  let reasonCodeFields := refusal.reasonCode.toList.map fun code => ("reason_code", toJson code)
  let observationFields := refusal.observation.toList.map fun observation => ("observation", observation)
  let details := reasonCodeFields ++ observationFields
  emitVerdict requested (.error (IO.userError refusal.reason).toString) scope details

private structure CertifiedOptions where
  request : Guarantee
  checkOnly : Bool

private def noConstructor (requested reason : String) : IO UInt32 :=
  emitRefusal requested { reason, reasonCode := some "no_constructed_rule" }

private def tryNamedDeclarations (chunk : DeploymentChunk) (src : BundleInput)
    (out work : System.FilePath) (options : CertifiedOptions) (timings : Bool) : IO (Option UInt32) := do
  let .authored spec := chunk.placement | return none
  unless spec.sequential do return none
  let .ok (source, _localExports) :=
    CoreJS.LinkedInput.decodeSingletonSourceWithLocalExports src.program spec.entry | return none
  let [original] := source.modules | return none
  unless !original.body.isEmpty && original.body.all (fun (_, stmt) =>
      match stmt with
      | .decl .const _ _ => true
      | .decl .let_ _ .undef => false
      | .decl .let_ _ _ => true
      | _ => false) do return none
  unless spec.modules.all (fun row => row.1.endsWith ".mjs") do return none
  let .ok constructed := CoreJS.NamedDeclarationSplit.construct source spec | return none
  try
    let programs ← IO.ofExcept (ESTreeEmit.corePrograms constructed.split.program src.source)
    let part : OutputPart := { programs, source := src.source, input := src.input, work }
    let checked ← certifyOutput (α := Unit) [part] (Json.mkObj []) out work options.checkOnly timings fun _ =>
      pure (.ok ())
    match checked with
    | .ok _ => return some (← emitVerdict options.request.label (.ok .allOutcomes) "named-declarations"
        [("strategy", toJson "compiled-constructor")])
    | .error refusal => return some (← emitRefusal options.request.label refusal "named-declarations")
  catch e => return some (← emitVerdict options.request.label (.error e.toString) "named-declarations")

/-- Source and original metadata for the indexed local-export constructor. -/
private structure LocalExportSource where
  source : CoreJS.Program
  row : LocalExportUnit
  bundle : Bundle

private def decodeLocalExportSource (original : Json) (sourceText entry : String) : Except String LocalExportSource := do
  let decoded ← CoreJS.LinkedInput.decodeIndexedSource original sourceText entry
  let source := decoded.program
  let [m] := source.modules | throw "expected one source module"
  let [row] := decoded.localExports | throw "expected one local export row"
  unless m.exports == row.names do throw "local export row differs from the public export table"
  unless m.body.isEmpty || m.body.any (fun (id, _) => id < row.ordinal) do
    throw "expected an executable declaration before the export row"
  unless (CoreJS.OrderedConstSyntax.checkBody m.body).isSome &&
      (CoreJS.OrderedConstSyntax.checkEarlierReads [] m.body).isSome do
    throw "expected unique const declarations with source primitive initializers and earlier reads"
  return ⟨source, row, SparseArtifact.sourceBundle decoded.nodes⟩

/-- Stage the constructor's modules through the shared publication gate. -/
private def stageLocalExports (program : CoreJS.Program) (src : BundleInput)
    (out work : System.FilePath) (options : CertifiedOptions) (timings : Bool) :
    IO (Except CertificationRefusal Unit) := do
  let programs ← IO.ofExcept (ESTreeEmit.corePrograms program src.source)
  let part : OutputPart := { programs, source := src.source, input := src.input, work }
  certifyOutput (α := Unit) [part] (Json.mkObj []) out work options.checkOnly timings fun files =>
    pure <| match CoreJS.LinkedInput.checkConstructedOutput files program with
      | .ok _ => .ok ()
      | .error reason => .error { reason }

/-- Construct from the actual indexed source, bundle, export row and authored
placement. Both terminal and interior rows use the same proof and writer. -/
private def tryLocalExports (chunk : DeploymentChunk) (src : BundleInput)
    (out work : System.FilePath) (options : CertifiedOptions) (timings : Bool) : IO (Option UInt32) := do
  let .authored spec := chunk.placement | return none
  unless spec.sequential && spec.modules.all (fun row => row.1.endsWith ".mjs") do return none
  let .ok input := decodeLocalExportSource src.program src.source spec.entry | return none
  let .ok built := CoreJS.IndexedCheckedSplit.construct input.source input.bundle spec input.row .allOutcomes |
    return none
  try
    match ← stageLocalExports built.program src out work options timings with
    | .ok _ => return some (← emitVerdict options.request.label (.ok .allOutcomes) "indexed-local-exports"
        [("strategy", toJson "compiled-constructor")])
    | .error refusal => return some (← emitRefusal options.request.label refusal "indexed-local-exports")
  catch e => return some (← emitVerdict options.request.label (.error e.toString) "indexed-local-exports")

/-- Reject malformed terminal-row selectors before any constructor can decline
the proof and let the ordinary checker reinterpret the authored request. This
uses the terminal selector resolver without imposing its coverage or owner
requirements on otherwise unsupported placements. -/
private def rejectDuplicateTerminalSelectors (chunk : DeploymentChunk) (src : BundleInput)
    (options : CertifiedOptions) : IO (Option UInt32) := do
  let .authored spec := chunk.placement | return none
  unless spec.sequential && !spec.privateEntryBody do return none
  let .ok input := CoreJS.LinkedInput.decodeSingletonSourceWithLocalExports src.program spec.entry | return none
  let .ok terminal := CoreJS.TerminalSource.check input | return none
  let terminalSource : CoreJS.TerminalExportPlacement.TerminalSource := {
    module := terminal.source
    exportOrdinal := terminal.row.ordinal
  }
  match CoreJS.TerminalExportPlacement.selectRequests terminalSource spec with
  | .error reason => return some (← emitRefusal options.request.label
      { reason, reasonCode := some "linked_placement" } "terminal-declarations")
  | .ok selected =>
    unless (selected.map Prod.fst).Nodup do
      return some (← emitRefusal options.request.label
        { reason := "an original unit is selected more than once", reasonCode := some "linked_placement" }
        "terminal-declarations")
    return none

/-- Both emission and readback must start from a successful construction. -/
private def runSingleton (chunk : DeploymentChunk) (out work : System.FilePath)
    (options : CertifiedOptions) (timings : Bool) : IO UInt32 := do
  let src ← readBundle chunk.source work true timings (← IO.ofExcept chunk.sourceHash)
  if let some verdict ← rejectDuplicateTerminalSelectors chunk src options then return verdict
  if let some verdict ← tryLocalExports chunk src out work options timings then return verdict
  if let some verdict ← tryNamedDeclarations chunk src out work options timings then return verdict
  noConstructor options.request.label s!"no constructor accepts the requested placement of {chunk.path}"

def certified (args : List String) : IO UInt32 := do
  let (level?, args) := EsmSplit.takeFlag "--require" args
  let label := level?.getD Guarantee.normalCompletion.label
  try
    let request ← match label with
      | "all-outcomes" => pure Guarantee.allOutcomes
      | "normal-completion" => pure Guarantee.normalCompletion
      | _ => throw (IO.userError "--require expects all-outcomes or normal-completion")
    if args.any (fun arg => ["--host-contract", "--structural", "--deployment-origin",
        "--deployment-import-map", "--deployment-preset"].contains arg) then
      return ← noConstructor label "no constructor supports host or structural deployment options"
    let checkOnly := args.contains "--check-output"
    let [manifestPath, dest] := args.filter (· != "--check-output")
      | throw (IO.userError "usage: lowersplit <deployment.json> <outdir> [--require LEVEL] [--check-output]")
    let out ← IO.ofExcept (outputDirectory dest)
    let manifest ← readDeploymentManifest manifestPath
    let [chunk] := manifest.chunks | return ← noConstructor label "no constructor supports multi-chunk deployments"
    let timings := (← IO.getEnv "SPLIT_PROOF_TIMINGS") == some "1"
    IO.FS.withTempDir fun work => runSingleton chunk out work { request, checkOnly } timings
  catch e => emitVerdict label (.error e.toString)

end EsmSplit.LowerSplit
