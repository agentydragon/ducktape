import EsmSplit.CoreJS.AuthenticatedSource
import EsmSplit.Cli
import EsmSplit.CoreJS.ConstructedOutput
import EsmSplit.CoreJS.IndexedCheckedSplit
import EsmSplit.CoreJS.IndexedInlineConstructor
import EsmSplit.CoreJS.IndexedSourceFacts
import EsmSplit.CoreJS.LinkedInput
import EsmSplit.CoreJS.LiteralReorderConstruct
import EsmSplit.CoreJS.CheckedSplit
import EsmSplit.CoreJS.ResolvedPlacement
import EsmSplit.CoreJS.OrderedConstSyntax
import EsmSplit.CoreJS.TerminalExportPlacement
import EsmSplit.CoreJS.TerminalSource
import EsmSplit.ESTreeEmit
import EsmSplit.LowerSplit.Input

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

/-- Stage the constructor's modules through the shared publication gate. -/
private def stageConstructed (program : CoreJS.Program) (src : BundleInput)
    (out work : System.FilePath) (options : CertifiedOptions) (timings : Bool) :
    IO (Except CertificationRefusal Unit) := do
  let programs ← IO.ofExcept (ESTreeEmit.corePrograms program src.source)
  let part : OutputPart := { programs, source := src.source, input := src.input, work }
  certifyOutput (α := Unit) [part] (Json.mkObj []) out work options.checkOnly timings fun files =>
    pure <| match CoreJS.ConstructedOutput.checkConstructedOutput files program with
      | .ok _ => .ok ()
      | .error reason => .error { reason }

private def tryInlineDeclarations (chunk : DeploymentChunk) (src : BundleInput)
    (classified : CoreJS.LinkedInput.DecodedSource) (indexed : Except String CoreJS.AuthenticatedSource.IndexedSource)
    (out work : System.FilePath) (options : CertifiedOptions) (timings : Bool) : IO (Option UInt32) := do
  let .authored spec := chunk.placement | return none
  unless spec.sequential do return none
  let original := classified.source
  unless original.imports.isEmpty && !classified.hasTemplates do return none
  unless !original.body.isEmpty && original.body.all (fun (_, stmt) =>
      match stmt with
      | .decl _ _ _ => true
      | _ => false) do return none
  unless spec.modules.all (fun row => row.1.endsWith ".mjs") do return none
  unless CoreJS.IndexedInlineConstructor.staticDeclarations [] original.body do return none
  -- Once the source is recognized, omitted original units are a final
  -- refusal. The same classification supplies the indexed construction.
  let decoded ← match indexed with
    | .ok indexed => pure indexed
    | .error reason =>
      return some (← emitRefusal options.request.label { reason := s!"source provenance: {reason}" }
        "named-declarations")
  let source := decoded.program
  -- Indexed decoding has already checked original-unit coverage. A standalone
  -- export row must be handled by the source-indexed local-export route.
  unless decoded.localExports.isEmpty do
    return some (← emitRefusal options.request.label
      { reason := "source provenance: standalone export rows require source-indexed placement" } "named-declarations")
  -- Selectors are resolved against the actual original units, before the
  -- planner can deduplicate them. Invalid selectors are terminal refusals.
  let selected ← match CoreJS.ResolvedPlacement.selectRequests original spec with
    | .ok (_, selected) => pure selected
    | .error reason =>
      return some (← emitRefusal options.request.label { reason } "named-declarations")
  unless decide ((selected.map Prod.fst).Nodup) do
    return some (← emitRefusal options.request.label { reason := "an original unit is selected more than once" }
      "named-declarations")
  unless decide ((selected.map Prod.fst).Perm (original.body.map Prod.fst)) do
    return some (← emitRefusal options.request.label { reason := "selectors do not cover every original unit" }
      "named-declarations")
  let (program, scope) ←
    match CoreJS.IndexedInlineConstructor.construct decoded (CoreJS.CheckedSplit.summarize source) spec with
    | .ok constructed => pure (constructed.program, "named-declarations")
    | .error _ =>
      -- The ordered rule cannot justify an inversion. Use the same decoded
      -- source and unmodified request for the proved literal swap rule.
      match CoreJS.LiteralReorderConstruct.construct source spec with
      | .ok constructed => pure (constructed.program, "literal-reorder")
      | .error _ => return none
  try
    let checked ← stageConstructed program src out work options timings
    match checked with
    | .ok _ => return some (← emitVerdict options.request.label (.ok .allOutcomes) scope
        [("strategy", toJson "compiled-constructor")])
    | .error refusal => return some (← emitRefusal options.request.label refusal scope)
  catch e => return some (← emitVerdict options.request.label (.error e.toString) scope)

/-- Source and original metadata for the indexed local-export constructor. -/
private structure LocalExportSource where
  source : CoreJS.Program
  rows : List LocalExportUnit
  bundle : Bundle
  facts : CoreJS.IndexedCheckedSplit.SourceFacts source bundle rows

private def decodeLocalExportSource (decoded : CoreJS.AuthenticatedSource.IndexedSource) : Except String LocalExportSource := do
  let source := decoded.program
  let bundle := sourceBundle decoded.nodes
  let sequence ← CoreJS.IndexedSourceSchedule.checkSequence decoded bundle
  let m := decoded.source
  let rows := decoded.localExports
  unless !rows.isEmpty do throw "expected a local export row"
  unless m.exports == rows.flatMap (·.names) do throw "local export rows differ from the public export table"
  unless (CoreJS.OrderedConstSyntax.checkBody m.body).isSome &&
      (CoreJS.OrderedConstSyntax.checkEarlierReads [] m.body).isSome do
    throw "expected unique const declarations with source primitive initializers and earlier reads"
  return ⟨source, rows, bundle, CoreJS.IndexedCheckedSplit.SourceFacts.fromSequence sequence⟩

/-- Construct from the actual indexed source, bundle, export rows and authored
placement. Both terminal and interior rows use the same proof and writer. -/
private def tryLocalExports (chunk : DeploymentChunk) (src : BundleInput)
    (classified : CoreJS.LinkedInput.DecodedSource) (indexed : Except String CoreJS.AuthenticatedSource.IndexedSource)
    (out work : System.FilePath) (options : CertifiedOptions) (timings : Bool) : IO (Option UInt32) := do
  let .authored spec := chunk.placement | return none
  unless spec.sequential && spec.modules.all (fun row => row.1.endsWith ".mjs") do return none
  unless !classified.localExports.isEmpty do return none
  let .ok decoded := indexed | return none
  let .ok input := decodeLocalExportSource decoded | return none
  unless decide (CoreJS.IndexedCheckedSplit.MetadataSelectorsOnce input.bundle spec input.rows) do
    return some (← emitVerdict options.request.label (.error "each metadata row must be selected exactly once")
      "indexed-local-exports")
  let .ok built := CoreJS.IndexedCheckedSplit.construct input.source input.bundle spec input.rows .allOutcomes input.facts |
    return none
  try
    match ← stageConstructed built.program src out work options timings with
    | .ok _ => return some (← emitVerdict options.request.label (.ok .allOutcomes) "indexed-local-exports"
        [("strategy", toJson "compiled-constructor")])
    | .error refusal => return some (← emitRefusal options.request.label refusal "indexed-local-exports")
  catch e => return some (← emitVerdict options.request.label (.error e.toString) "indexed-local-exports")

/-- Reject malformed terminal-row selectors before any constructor can decline
the proof and let the ordinary checker reinterpret the authored request. This
uses the terminal selector resolver without imposing its coverage or owner
requirements on otherwise unsupported placements. -/
private def rejectDuplicateTerminalSelectors (chunk : DeploymentChunk)
    (classified : CoreJS.LinkedInput.DecodedSource)
    (options : CertifiedOptions) : IO (Option UInt32) := do
  let .authored spec := chunk.placement | return none
  unless spec.sequential && !spec.privateEntryBody do return none
  unless classified.source.imports.isEmpty && !classified.hasTemplates do return none
  let .ok terminal := CoreJS.TerminalSource.check (classified.program, classified.localExports) | return none
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
  if let .authored spec := chunk.placement then
    if spec.sequential then
      if let .ok classified := CoreJS.AuthenticatedSource.decodeSourceClassification src.program src.source spec.entry then
        if let some verdict ← rejectDuplicateTerminalSelectors chunk classified options then return verdict
        let indexed := CoreJS.AuthenticatedSource.checkIndexedSource classified
        if let some verdict ← tryLocalExports chunk src classified indexed out work options timings then return verdict
        if let some verdict ← tryInlineDeclarations chunk src classified indexed out work options timings then return verdict
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
