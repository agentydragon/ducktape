import EsmSplit.Cli
import EsmSplit.CoreJS.AuthenticatedSource
import EsmSplit.CoreJS.CallableDefaultProvider
import EsmSplit.CoreJS.CheckedSplit
import EsmSplit.CoreJS.ConstructedOutput
import EsmSplit.CoreJS.FiniteStaticGraph
import EsmSplit.CoreJS.IndexedCheckedSplit
import EsmSplit.CoreJS.IndexedDefaultSnapshot
import EsmSplit.CoreJS.IndexedExportedFunction
import EsmSplit.CoreJS.IndexedHelperConstructor
import EsmSplit.CoreJS.IndexedHoistedSwap
import EsmSplit.CoreJS.IndexedInlineConstructor
import EsmSplit.CoreJS.IndexedPrototypeWrite
import EsmSplit.CoreJS.IndexedSourceFacts
import EsmSplit.CoreJS.LinkedInput
import EsmSplit.CoreJS.LinkedPrimitiveAwait
import EsmSplit.CoreJS.LinkedPrimitiveAwaitProof
import EsmSplit.CoreJS.LiteralReorderConstruct
import EsmSplit.CoreJS.OrderedConstSyntax
import EsmSplit.CoreJS.ResolvedPlacement
import EsmSplit.CoreJS.RetainedClassConstruction
import EsmSplit.CoreJS.RetainedTailConstruction
import EsmSplit.CoreJS.StaticProviderConstruction
import EsmSplit.CoreJS.TerminalExportPlacement
import EsmSplit.CoreJS.TerminalSource
import EsmSplit.CoreJS.TwoJobAwait
import EsmSplit.ESTreeEmit
import EsmSplit.ESTreeUnits
import EsmSplit.LowerSplit.Input
import EsmSplit.OwnedNodePackage

/-! Certify a proposed split and publish its output or verify the existing tree. -/

namespace EsmSplit.LowerSplit

open Lean EsmSplit.SplitFiles
open CoreJS (Guarantee)

private def emitVerdict (requested : String) (outcome : Except String Guarantee)
    (scope : Option String := none) (extra : List (String × Json) := []) : IO UInt32 := do
  let everyEndingMatches :=
    match outcome with
    | .ok .allOutcomes => true
    | _ => false
  let fields :=
    match outcome with
    | .error why =>
      [("status", toJson "rejected"), ("guarantee", Json.null), ("reason", toJson why)]
    | .ok level => [("status", toJson "certified"), ("guarantee", toJson level.label)]
  IO.println
      (Json.mkObj
          ([
              ("requested", toJson requested),
              ("scope", toJson scope),
              ("every_ending_matches", toJson everyEndingMatches),
            ] ++
            fields ++
            extra)).compress
  return if outcome.isOk then 0 else 1

/-- Convert a typed refusal into the existing verdict fields at the CLI boundary. -/
private def emitRefusal (requested : String) (refusal : CertificationRefusal)
    (scope : Option String := none) : IO UInt32 := do
  let reasonCodeFields := refusal.reasonCode.toList.map fun code => ("reason_code", toJson code)
  let observationFields :=
    refusal.observation.toList.map fun observation => ("observation", observation)
  let details := reasonCodeFields ++ observationFields
  emitVerdict requested (.error (IO.userError refusal.reason).toString) scope details

private structure CertifiedOptions where
  request : Guarantee
  checkOnly : Bool

/-- Report only facts established by the graph and artifact checks on this
route. A fixed caller or manifest is not a general loader or client proof. -/
private def importReceipt (options : CertifiedOptions) (before after : CoreJS.Program)
    (fixedFiles : List String) (ownedCaller : Option String := none) (nodeEsmRoot := false) :
    String × Json :=
  let graph (p : CoreJS.Program) :=
    Json.mkObj
      [("entry", toJson p.entry), ("modules", toJson (p.modules.map (·.name))),
        ("evaluation_order", toJson p.evalOrder)]
  let exports := ((before.find before.entry).map (fun m => m.exports.map Prod.fst)).getD []
  ("receipt",
    Json.mkObj
      [("operation", toJson (if options.checkOnly then "checked-output" else "published")),
        ("graph",
          Json.mkObj
            [("source", graph before), ("output", graph after),
              ("public_entry",
                Json.mkObj
                  [("route", toJson before.entry), ("exports", toJson exports),
                    ("live_cells", toJson "same-linked-cells")]),
              ("run_result",
                toJson "equal-complete-modeled-result-at-equal-fuel-for-every-world")]),
        ("client",
          Json.mkObj
            ([("coverage",
                  toJson (if ownedCaller.isSome then "default-import-snapshot" else "unchecked")),
                ("premises",
                  toJson ["observe-through-designated-public-entry", "generated-paths-private"])] ++
              ownedCaller.toList.map (fun route => ("checked_route", toJson route)))),
        ("deployment",
          Json.mkObj
            [("claim", toJson "none"),
              ("checked",
                toJson
                  (["exact-artifact-paths", "generated-esm-syntax"] ++
                    (if fixedFiles.isEmpty then [] else ["fixed-file-bytes"]) ++
                    (if ownedCaller.isSome then ["owned-flat-root", "explicit-node-esm-manifest"]
                    else
                      if nodeEsmRoot then ["explicit-node-esm-manifest", "no-nested-package-scope"]
                      else []))),
              ("fixed_files", toJson fixedFiles),
              ("assumptions",
                toJson
                  ["runtime-loads-the-checked-graphs-with-their-formats-routes-and-module-identities",
                    "new-implementation-routes-are-fresh-in-the-deployment"])])])

private def noConstructor (requested reason : String) : IO UInt32 :=
  emitRefusal requested { reason, reasonCode := some "no_constructed_rule" }

/-- Stage generated modules with no fixed providers. Both the initial write and
the disk recheck use the same artifact check after ESTree comparison. -/
private def stageGenerated (programs : Json) (src : BundleInput) (out work : System.FilePath)
    (options : CertifiedOptions) (timings : Bool)
    (check : Json → Except CertificationRefusal Unit) : IO (Except CertificationRefusal Unit) :=
  let part : OutputPart := { programs, source := src.source, input := src.input, work }
  certifyOutput [part] (Json.mkObj []) out work options.checkOnly timings (pure ∘ check)

/-- Stage the constructor's modules through the shared publication gate. -/
private def stageConstructed (program : CoreJS.Program) (src : BundleInput)
    (out work : System.FilePath) (options : CertifiedOptions) (timings : Bool) :
    IO (Except CertificationRefusal Unit) := do
  let programs ← IO.ofExcept (ESTreeEmit.corePrograms program src.source)
  stageGenerated programs src out work options timings fun files =>
      match CoreJS.ConstructedOutput.checkConstructedOutput files program with
      | .ok _ => .ok ()
      | .error reason => .error { reason }

/-- Resolve one import from the directory of the actual raw entry. The
authenticated classifier uses a root-level entry, so restrict this route to
root-level entry names too. The provider travels through the existing byte
comparison gate, while the generated modules are independently parsed. -/
private def runStaticProvider (chunk : DeploymentChunk) (spec : Spec) (src : BundleInput)
    (classified : CoreJS.LinkedInput.DecodedSource) (out work : System.FilePath)
    (options : CertifiedOptions) (timings : Bool) : IO UInt32 := do
  let refuse := fun reason => noConstructor options.request.label s!"static provider: {reason}"
  unless
    chunk.source.extension == some "mjs" && spec.entry.endsWith ".mjs" &&
      !spec.entry.contains '/' do
    return ← refuse "expected a raw .mjs source and a root-level entry route"
  let .ok request :=
    CoreJS.StaticProviderConstruction.providerRequest
      classified | return ← refuse "expected named imports of one export from one relative provider"
  try
    let actual ← IO.FS.realPath chunk.source
    let some root := actual.parent | return ← refuse "source does not have a directory"
    unless actual.extension == some "mjs" && root / request.source != actual do
      return ← refuse "provider must be a distinct raw .mjs file"
    let providers ← readModules root work #[request.source]
    let providerAst ← IO.ofExcept (providers.getObjVal? request.source)
    let providerText ← IO.ofExcept (providerAst.getObjValAs? String "sourceText")
    let provider ←
      IO.ofExcept
          (CoreJS.AuthenticatedSource.decodeIndexedSource providerAst providerText request.source)
    let built ←
      match CoreJS.StaticProviderConstruction.construct classified provider spec with
      | .ok built =>
        pure built
      | .error reason =>
        return ← refuse reason
    let programs ←
      IO.ofExcept
          (ESTreeEmit.programs classified.nodes (sourceBundle classified.nodes)
            (CoreJS.StaticProviderConstruction.printerPlan built) src.source spec.entry)
    let part : OutputPart := { programs, source := src.source, input := src.input, work }
    let result ←
      certifyOutput (α := Unit) [part] providers out work options.checkOnly timings fun files =>
          pure do
            let observed ←
              files.getObj? |>.mapError (fun reason => ({ reason } : CertificationRefusal))
            -- The gate has just checked these exact provider bytes; only generated
            -- modules need fresh annotations in the returned file map.
            let complete := Json.mkObj ((request.source, providerAst) :: observed.toList)
            let ⟨same⟩ ←
              (CoreJS.ConstructedOutput.checkConstructedOutput complete built.program).mapError
                  fun reason => ({ reason } : CertificationRefusal)
            let _proof :=
              fun observed
                (decoded :
                  CoreJS.ConstructedOutput.decodeConstructedOutput complete built.program =
                    .ok observed) =>
              built.readbackGuarantees (Except.ok.inj (decoded.symm.trans same))
            return ()
    match result with
    | .ok _ =>
      emitVerdict options.request.label (.ok .allOutcomes) "shared-static-provider"
          [("strategy", toJson "compiled-constructor"),
            importReceipt options (CoreJS.StaticProviderConstruction.original classified provider)
              built.program [request.source]]
    | .error why =>
      emitRefusal options.request.label why "shared-static-provider"
  catch e =>
    refuse e.toString

private def tryInlineDeclarations (chunk : DeploymentChunk) (src : BundleInput)
    (classified : CoreJS.LinkedInput.DecodedSource)
    (indexed : Except String CoreJS.AuthenticatedSource.IndexedSource) (out work : System.FilePath)
    (options : CertifiedOptions) (timings : Bool) : IO (Option UInt32) := do
  let .authored spec := chunk.placement | return none
  unless spec.sequential do
    return none
  let original := classified.source
  unless original.imports.isEmpty && !classified.hasTemplates do
    return none
  unless
    !original.body.isEmpty &&
      original.body.all
        (fun (_, stmt) =>
          match stmt with
          | .decl _ _ _ => true
          | _ => false) do
    return none
  unless spec.modules.all (fun row => row.1.endsWith ".mjs") do
    return none
  unless CoreJS.IndexedInlineConstructor.staticDeclarations [] original.body do
    return none
  -- Once the source is recognized, omitted original units are a final
  -- refusal. The same classification supplies the indexed construction.
  let decoded ←
    match indexed with
    | .ok indexed =>
      pure indexed
    | .error reason =>
      return some
          (←
            emitRefusal options.request.label { reason := s!"source provenance: {reason}" }
                "named-declarations")
  let source := decoded.program
  -- Indexed decoding has already checked original-unit coverage. A standalone
  -- export row must be handled by the source-indexed local-export route.
  unless decoded.localExports.isEmpty do
    return some
        (←
          emitRefusal options.request.label
              {
                reason :=
                  "source provenance: standalone export rows require source-indexed placement" }
              "named-declarations")
  -- Selectors are resolved against the actual original units, before the
  -- planner can deduplicate them. Invalid selectors are terminal refusals.
  let selected ←
    match CoreJS.ResolvedPlacement.selectRequests original spec with
    | .ok (_, selected) =>
      pure selected
    | .error reason =>
      return some (← emitRefusal options.request.label { reason } "named-declarations")
  unless decide ((selected.map Prod.fst).Nodup) do
    return some
        (←
          emitRefusal options.request.label
              { reason := "an original unit is selected more than once" } "named-declarations")
  unless decide ((selected.map Prod.fst).Perm (original.body.map Prod.fst)) do
    return some
        (←
          emitRefusal options.request.label
              { reason := "selectors do not cover every original unit" } "named-declarations")
  let (program, scope) ←
    match
      CoreJS.IndexedInlineConstructor.construct decoded (CoreJS.CheckedSplit.summarize source)
        spec with
    | .ok constructed =>
      pure (constructed.program, "named-declarations")
    | .error _ =>
      -- The ordered rule cannot justify an inversion. Use the same decoded
      -- source and unmodified request for the proved literal swap rule.
      match CoreJS.LiteralReorderConstruct.construct source spec with
      | .ok constructed =>
        pure (constructed.program, "literal-reorder")
      | .error _ =>
        return none
  try
    let checked ← stageConstructed program src out work options timings
    match checked with
    | .ok _ =>
      return some
          (←
            emitVerdict options.request.label (.ok .allOutcomes) scope
                [("strategy", toJson "compiled-constructor")])
    | .error refusal =>
      return some (← emitRefusal options.request.label refusal scope)
  catch e =>
    return some (← emitVerdict options.request.label (.error e.toString) scope)

/-- Source and original metadata for the indexed local-export constructor. -/
private structure LocalExportSource where
  source : CoreJS.Program
  rows : List LocalExportUnit
  bundle : Bundle
  facts : CoreJS.IndexedCheckedSplit.SourceFacts source bundle rows

private def decodeLocalExportSource (decoded : CoreJS.AuthenticatedSource.IndexedSource) :
    Except String LocalExportSource := do
  let source := decoded.program
  let bundle := sourceBundle decoded.nodes
  let sequence ← CoreJS.IndexedSourceSchedule.checkSequence decoded bundle
  let m := decoded.source
  let rows := decoded.localExports
  unless !rows.isEmpty do
    throw "expected a local export row"
  unless m.exports == rows.flatMap (·.names) do
    throw "local export rows differ from the public export table"
  unless
    (CoreJS.OrderedConstSyntax.checkBody m.body).isSome &&
      (CoreJS.OrderedConstSyntax.checkEarlierReads [] m.body).isSome do
    throw "expected unique const declarations with source primitive initializers and earlier reads"
  return ⟨source, rows, bundle, CoreJS.IndexedCheckedSplit.SourceFacts.fromSequence sequence⟩

/-- Construct from the actual indexed source, bundle, export rows and authored
placement. Both terminal and interior rows use the same proof and writer. -/
private def tryLocalExports (chunk : DeploymentChunk) (src : BundleInput)
    (classified : CoreJS.LinkedInput.DecodedSource)
    (indexed : Except String CoreJS.AuthenticatedSource.IndexedSource) (out work : System.FilePath)
    (options : CertifiedOptions) (timings : Bool) : IO (Option UInt32) := do
  let .authored spec := chunk.placement | return none
  unless
    (spec.sequential || spec.privateEntryBody) &&
      spec.modules.all (fun row => row.1.endsWith ".mjs") do
    return none
  unless !classified.localExports.isEmpty do
    return none
  let .ok decoded := indexed | return none
  let .ok input := decodeLocalExportSource decoded | return none
  unless decide (CoreJS.IndexedCheckedSplit.MetadataSelectorsOnce input.bundle spec input.rows) do
    return some
        (←
          emitVerdict options.request.label
              (.error "each metadata row must be selected exactly once") "indexed-local-exports")
  let .ok built :=
    CoreJS.IndexedCheckedSplit.construct input.source input.bundle spec input.rows .allOutcomes
      input.facts | return none
  try
    match ← stageConstructed built.program src out work options timings with
    | .ok _ =>
      return some
          (←
            emitVerdict options.request.label (.ok .allOutcomes) "indexed-local-exports"
                [("strategy", toJson "compiled-constructor")])
    | .error refusal =>
      return some (← emitRefusal options.request.label refusal "indexed-local-exports")
  catch e =>
    return some (← emitVerdict options.request.label (.error e.toString) "indexed-local-exports")

/-- Reject malformed terminal-row selectors before any constructor can decline
the proof and let the ordinary checker reinterpret the authored request. This
uses the terminal selector resolver without imposing its coverage or owner
requirements on otherwise unsupported placements. -/
private def rejectDuplicateTerminalSelectors (chunk : DeploymentChunk)
    (classified : CoreJS.LinkedInput.DecodedSource) (options : CertifiedOptions) :
    IO (Option UInt32) := do
  let .authored spec := chunk.placement | return none
  unless spec.sequential && !spec.privateEntryBody do
    return none
  unless classified.source.imports.isEmpty && !classified.hasTemplates do
    return none
  let .ok terminal :=
    CoreJS.TerminalSource.check (classified.program, classified.localExports) | return none
  let terminalSource : CoreJS.TerminalExportPlacement.TerminalSource :=
    { module := terminal.source
      exportOrdinal := terminal.row.ordinal }
  match CoreJS.TerminalExportPlacement.selectRequests terminalSource spec with
  | .error reason =>
    return some
        (←
          emitRefusal options.request.label { reason, reasonCode := some "linked_placement" }
              "terminal-declarations")
  | .ok selected =>
    unless (selected.map Prod.fst).Nodup do
      return some
          (←
            emitRefusal options.request.label
                { reason := "an original unit is selected more than once",
                  reasonCode := some "linked_placement" }
                "terminal-declarations")
    return none

/-- Only the separate exact-swap rule may handle this inversion. Use the
same authored plan for emission and the independently reparsed modules. -/
private def runHoistedSwap (spec : Spec) (src : BundleInput)
    (decoded : CoreJS.AuthenticatedSource.IndexedSource) (bundle : Bundle)
    (out work : System.FilePath) (options : CertifiedOptions) (timings : Bool) : IO UInt32 := do
  let scope := "indexed-hoisted-swap"
  let built ←
    match CoreJS.IndexedHoistedSwap.construct decoded bundle spec with
    | .ok built =>
      pure built
    | .error reason =>
      return ←
          emitRefusal options.request.label { reason, reasonCode := some "no_constructed_rule" }
              scope
  try
    let programs ←
      IO.ofExcept (ESTreeEmit.programs decoded.nodes bundle built.plan src.source spec.entry)
    let checked ←
      stageGenerated programs src out work options timings fun files => do
          let _ ←
            (built.checkReadback files).mapError fun reason => ({ reason } : CertificationRefusal)
          return ()
    if let .error refusal := checked then
      return ← emitRefusal options.request.label refusal scope
    return ←
        emitVerdict options.request.label (.ok .allOutcomes) scope
            [("strategy", toJson "compiled-constructor")]
  catch e =>
    return ← emitVerdict options.request.label (.error e.toString) scope

/-- Preserve the original declaration and export row through the staged AST
check, then decode the reread program before issuing the guarantee. -/
private def tryExportedFunction (spec : Spec) (src : BundleInput)
    (decoded : CoreJS.AuthenticatedSource.IndexedSource) (out work : System.FilePath)
    (options : CertifiedOptions) (timings : Bool) : IO (Option UInt32) := do
  unless
    spec.sequential && !spec.privateEntryBody && !decoded.localExports.isEmpty &&
      decoded.source.body.any
        (fun (_, stmt) =>
          match stmt with
          | .funDecl _ => true
          | _ => false) do
    return none
  let bundle := sourceBundle decoded.nodes
  let (built, scope, level) ←
    match CoreJS.IndexedExportedFunction.construct decoded bundle spec with
    | .ok built =>
      pure (built, "indexed-exported-function", Guarantee.allOutcomes)
    | .error reason =>
      if CoreJS.IndexedHoistedSwap.recognises decoded bundle spec then
        return some (← runHoistedSwap spec src decoded bundle out work options timings)
      else if options.request == .normalCompletion then
        match CoreJS.IndexedExportedFunction.constructClassPrefix decoded bundle spec with
        | .ok built =>
          pure (built, "indexed-class-prefix", Guarantee.normalCompletion)
        | .error _ =>
          return some
            (← emitRefusal options.request.label { reason, reasonCode := some "no_constructed_rule" }
                "indexed-exported-function")
      else
        return some
          (← emitRefusal options.request.label { reason, reasonCode := some "no_constructed_rule" }
              "indexed-exported-function")
  try
    let programs ←
      IO.ofExcept
          (ESTreeEmit.programs decoded.nodes bundle built.schedule.plan src.source spec.entry)
    let checked ←
      stageGenerated programs src out work options timings fun files => do
          let ⟨same⟩ ←
            (CoreJS.ConstructedOutput.checkConstructedOutput files built.program).mapError
                fun reason => ({ reason } : CertificationRefusal)
          let _proof :=
            fun observed
              (reread :
                CoreJS.ConstructedOutput.decodeConstructedOutput files built.program =
                  .ok observed) =>
            built.readbackGuarantees (Except.ok.inj (reread.symm.trans same))
          return ()
    match checked with
    | .ok _ =>
      return some
          (←
            emitVerdict options.request.label (.ok level) scope
                [("strategy", toJson "compiled-constructor")])
    | .error refusal =>
      return some (← emitRefusal options.request.label refusal scope)
  catch e =>
    return some (← emitVerdict options.request.label (.error e.toString) scope)

/-- Publish the original executable default syntax; a local export list
cannot stand in for the snapshotted expression. -/
private def tryDefaultSnapshot (spec : Spec) (src : BundleInput)
    (decoded : CoreJS.AuthenticatedSource.IndexedSource) (out work : System.FilePath)
    (options : CertifiedOptions) (timings : Bool) : IO (Option UInt32) := do
  unless
    spec.sequential && !spec.privateEntryBody && decoded.nodes.any isIdentifierDefault &&
      decoded.source.body.any
        (fun (_, stmt) =>
          match stmt with
          | .funDecl _ => true
          | _ => false) do
    return none
  let scope := "indexed-default-snapshot"
  let bundle := snapshotSourceBundle decoded.nodes
  let built ←
    match CoreJS.IndexedDefaultSnapshot.construct decoded bundle spec with
    | .ok built =>
      pure built
    | .error reason =>
      return some
          (←
            emitRefusal options.request.label { reason, reasonCode := some "no_constructed_rule" }
                scope)
  try
    let programs ←
      IO.ofExcept
          (ESTreeEmit.programs decoded.nodes bundle built.schedule.plan src.source spec.entry)
    let checked ←
      stageGenerated programs src out work options timings fun files => do
          let ⟨same⟩ ←
            (CoreJS.ConstructedOutput.checkConstructedOutput files built.program).mapError
                fun reason => ({ reason } : CertificationRefusal)
          let _proof :=
            fun observed
              (reread :
                CoreJS.ConstructedOutput.decodeConstructedOutput files built.program =
                  .ok observed) =>
            built.readbackGuarantees (Except.ok.inj (reread.symm.trans same))
          return ()
    match checked with
    | .ok _ =>
      return some
          (←
            emitVerdict options.request.label (.ok .allOutcomes) scope
                [("strategy", toJson "compiled-constructor"),
                  importReceipt options decoded.program built.program []])
    | .error refusal =>
      return some (← emitRefusal options.request.label refusal scope)
  catch e =>
    return some (← emitVerdict options.request.label (.error e.toString) scope)

/-- Keep the original callable ranges when printing a helper. Compare both
the emitted syntax and the independently lowered program before publication. -/
private def tryOrderedHelper (spec : Spec) (src : BundleInput)
    (decoded : CoreJS.AuthenticatedSource.IndexedSource) (out work : System.FilePath)
    (options : CertifiedOptions) (timings : Bool) : IO (Option UInt32) := do
  unless
    spec.sequential && !spec.privateEntryBody &&
      decoded.source.body.any
        (fun (_, stmt) =>
          match stmt with
          | .funDecl _ => true
          | _ => false) do
    return none
  let bundle := sourceBundle decoded.nodes
  let built ←
    match CoreJS.IndexedHelperConstructor.construct decoded bundle spec with
    | .ok built =>
      pure built
    | .error reason =>
      return some
          (←
            emitRefusal options.request.label { reason, reasonCode := some "no_constructed_rule" }
                "ordered-helper")
  try
    let programs ←
      IO.ofExcept
          (ESTreeEmit.programs decoded.nodes bundle built.schedule.plan src.source spec.entry)
    let checked ←
      stageGenerated programs src out work options timings fun files => do
          let ⟨same⟩ ←
            (CoreJS.ConstructedOutput.checkConstructedOutput files built.program).mapError
                fun reason => ({ reason } : CertificationRefusal)
          let _proof :=
            fun observed
              (decoded :
                CoreJS.ConstructedOutput.decodeConstructedOutput files built.program =
                  .ok observed) =>
            built.readbackGuarantees (Except.ok.inj (decoded.symm.trans same))
          return ()
    match checked with
    | .ok _ =>
      return some
          (←
            emitVerdict options.request.label (.ok .allOutcomes) "ordered-helper"
                [("strategy", toJson "compiled-constructor")])
    | .error refusal =>
      return some (← emitRefusal options.request.label refusal "ordered-helper")
  catch e =>
    return some (← emitVerdict options.request.label (.error e.toString) "ordered-helper")

/-- The private file runs the original prototype write before the entry's
already-instantiated declarations. Verify the authored links and the files
read back from the publication gate. -/
private def tryPrototypeWrite (spec : Spec) (src : BundleInput)
    (decoded : CoreJS.AuthenticatedSource.IndexedSource) (out work : System.FilePath)
    (options : CertifiedOptions) (timings : Bool) : IO (Option UInt32) := do
  let bundle := sourceBundle decoded.nodes
  unless CoreJS.IndexedPrototypeWrite.recognises decoded.source bundle spec do
    return none
  let scope := "indexed-prototype-write"
  let built ←
    match CoreJS.IndexedPrototypeWrite.construct decoded bundle spec with
    | .ok built =>
      pure built
    | .error reason =>
      return some
          (←
            emitRefusal options.request.label { reason, reasonCode := some "no_constructed_rule" }
                scope)
  try
    let programs ←
      IO.ofExcept (ESTreeEmit.programs decoded.nodes bundle built.plan src.source spec.entry)
    let checked ←
      stageGenerated programs src out work options timings fun files => do
          let _ ←
            (built.checkReadback files).mapError fun reason => ({ reason } : CertificationRefusal)
          return ()
    match checked with
    | .ok _ =>
      return some
          (←
            emitVerdict options.request.label (.ok .allOutcomes) scope
                [("strategy", toJson "compiled-constructor")])
    | .error refusal =>
      return some (← emitRefusal options.request.label refusal scope)
  catch e =>
    return some (← emitVerdict options.request.label (.error e.toString) scope)

private def runOwnedNodePackage (pkg : OwnedNodePackage.Spec) (chunk : DeploymentChunk)
    (out work : System.FilePath) (options : CertifiedOptions) (timings : Bool) : IO UInt32 := do
  let scope := "owned-node-callable-provider"
  let refuse := fun reason =>
    emitRefusal options.request.label { reason, reasonCode := some "no_constructed_rule" } scope
  unless options.request.label == Guarantee.allOutcomes.label do
    return ← refuse "owned_node_package requires --require all-outcomes"
  let .authored spec :=
    chunk.placement | return ← refuse "owned_node_package requires an authored placement"
  unless chunk.path == pkg.entry && chunk.source == pkg.root / pkg.entry do
    return ← refuse "chunk source must be the declared package entry route"
  try
    let captured ← OwnedNodePackage.check pkg work
    unless captured.callerIsDefaultSnapshot do
      return ←
          refuse
              "expected the fixed caller to contain only a default import and a snapshot of that binding"
    let classified ←
      IO.ofExcept
          (CoreJS.AuthenticatedSource.decodeSourceClassification captured.entryAst
            captured.entryText pkg.entry)
    let providerText ← IO.ofExcept (captured.providerAst.getObjValAs? String "sourceText")
    let provider ←
      IO.ofExcept
          (CoreJS.AuthenticatedSource.decodeIndexedSource captured.providerAst providerText
            pkg.provider)
    let built ← IO.ofExcept (CoreJS.CallableDefaultProvider.construct classified provider spec)
    let programs ← IO.ofExcept (built.programs captured.entryText)
    let result ←
      OwnedNodePackage.certify captured programs out work options.checkOnly timings fun files =>
          pure do
            let _ ←
              built.checkReadback files |>.mapError fun reason =>
                  ({ reason } : CertificationRefusal)
            return ()
    if let .error failure := result then
      return ← emitRefusal options.request.label failure scope
    emitVerdict options.request.label (.ok .allOutcomes) scope
        [("strategy", toJson "compiled-constructor"),
          ("proof_scope", toJson "linked-entry-provider"),
          ("caller_context", toJson "default-import-snapshot"),
          importReceipt options (CoreJS.CallableDefaultProvider.original classified provider)
            built.program ["package.json", pkg.provider, pkg.caller] (some pkg.caller)]
  catch e =>
    refuse e.toString

/-- The proved layout keeps the three declarations and their live cells in
the worker. Preserve authored order so the checked schedule can reject a
swap even when all the same units appear in the output. -/
private def twoJobPlan (spec : Spec) : Except String Plan := do
  unless
    spec.entry == "entry.mjs" && spec.sequential && !spec.privateEntryBody &&
      spec.order == ["worker.mjs"] &&
      spec.modules.length == 2 do
    throw "expected the two-job entry and worker layout"
  let ids := fun name => do
    let some rows := spec.modules.lookup name | throw s!"missing {name}"
    rows.mapM fun
        | .stmt i => .ok i
        | .name _ => .error "expected original statement selectors"
  let worker ← ids "worker.mjs"
  let entry ← ids "entry.mjs"
  unless worker == [0, 1, 2, 3, 4] && entry.mergeSort (· ≤ ·) == [5, 6, 7, 8, 9] do
    throw "the two-job plan moved or omitted a source unit"
  let imported := ["trace", "left", "right", "setValue"]
  return ⟨spec.entry,
      [⟨spec.entry, entry, [("worker.mjs", imported)], []⟩,
        ⟨"worker.mjs", worker, [], imported.map fun name => (name, name)⟩]⟩

private def tryTwoJobAwait (spec : Spec) (src : BundleInput) (out work : System.FilePath)
    (options : CertifiedOptions) (timings : Bool) : IO (Option UInt32) := do
  let scope := "two-job-primitive-await"
  let .ok before := CoreJS.LinkedPrimitiveAwait.decode src.program spec.entry | return none
  let .ok _ := CoreJS.TwoJobAwait.checkSupport before | return none
  let .ok plan := twoJobPlan spec |
    return some
        (←
          emitRefusal options.request.label
              { reason := "expected the supported two-job placement",
                reasonCode := some "linked_placement" }
              scope)
  try
    let nodes ← IO.ofExcept (ESTreeUnits.checkedNodes src.program src.source)
    let programs ← IO.ofExcept (ESTreeEmit.programs nodes (sourceBundle nodes) plan src.source)
    let checked ←
      stageGenerated programs src out work options timings fun files => do
          let _ ←
            (CoreJS.LinkedPrimitiveAwait.checkStagedPublication src.program files plan
                    CoreJS.TwoJobAwait.checkSupport).mapError
                fun reason => ({ reason } : CertificationRefusal)
          return ()
    match checked with
    | .ok _ =>
      return some
          (←
            emitVerdict options.request.label (.ok .allOutcomes) scope
                [("strategy", toJson "compiled-constructor")])
    | .error refusal =>
      return some (← emitRefusal options.request.label refusal scope)
  catch e =>
    return some (← emitVerdict options.request.label (.error e.toString) scope)

/-- Retain one final operation without assigning meaning to an ambient
callee. The source and authored priority determine the complete output. -/
private def tryRetainedTail (spec : Spec) (src : BundleInput) (out work : System.FilePath)
    (options : CertifiedOptions) (timings : Bool) : IO (Option UInt32) := do
  unless spec.privateEntryBody do
    return none
  let .ok nodes := ESTreeUnits.checkedNodes src.program src.source | return none
  let .ok built := CoreJS.RetainedTailConstruction.construct nodes spec | return none
  let level := built.certificate.1
  if options.request == .allOutcomes && level != .allOutcomes then
    return none
  let scope := if built.pivots.isEmpty then "retained-public-function" else "retained-tail"
  try
    let programs ←
      IO.ofExcept (ESTreeEmit.programs nodes (sourceBundle nodes) built.plan src.source)
    let checked ←
      stageGenerated programs src out work options timings fun files =>
          (CoreJS.RetainedTailConstruction.checkOutput built src.program files).mapError
            fun reason => ({ reason } : CertificationRefusal)
    match checked with
    | .ok _ =>
      return some
          (←
            emitVerdict options.request.label (.ok level) scope
                [("strategy", toJson "compiled-constructor")])
    | .error refusal =>
      return some (← emitRefusal options.request.label refusal scope)
  catch e =>
    return some (← emitVerdict options.request.label (.error e.toString) scope)

/-- Keep exact class definitions and their original initialization order. -/
private def tryRetainedClasses (spec : Spec) (src : BundleInput) (out work : System.FilePath)
    (options : CertifiedOptions) (timings : Bool) : IO (Option UInt32) := do
  let .ok nodes := ESTreeUnits.checkedNodes src.program src.source | return none
  let .ok built := CoreJS.RetainedClassConstruction.construct nodes spec | return none
  let scope := "retained-classes"
  try
    let programs ←
      IO.ofExcept (ESTreeEmit.programs nodes (sourceBundle nodes) built.plan src.source spec.entry)
    let checked ←
      stageGenerated programs src out work options timings fun files => do
        let _ ← (built.checkOutput src.program files).mapError
          fun reason => ({ reason } : CertificationRefusal)
        return ()
    match checked with
    | .ok _ =>
      return some (← emitVerdict options.request.label (.ok .allOutcomes) scope
        [("strategy", toJson "compiled-constructor"),
          importReceipt options built.before built.after []])
    | .error refusal => return some (← emitRefusal options.request.label refusal scope)
  catch e =>
    return some (← emitVerdict options.request.label (.error e.toString) scope)

/-- Both emission and readback must start from a successful construction. -/
private def runSingleton (chunk : DeploymentChunk) (out work : System.FilePath)
    (options : CertifiedOptions) (timings : Bool) : IO UInt32 := do
  let src ← readBundle chunk.source work true timings (← IO.ofExcept chunk.sourceHash)
  if let .authored spec := chunk.placement then
    if chunk.source.extension == some "mjs" then
      if let some verdict← tryTwoJobAwait spec src out work options timings then
        return verdict
    if spec.sequential || spec.privateEntryBody then
      if let .ok classified :=
          CoreJS.AuthenticatedSource.decodeSourceClassification src.program src.source
            spec.entry then
        if !classified.source.imports.isEmpty then
          return ← runStaticProvider chunk spec src classified out work options timings
        if let some verdict← rejectDuplicateTerminalSelectors chunk classified options then
          return verdict
        let indexed := CoreJS.AuthenticatedSource.checkIndexedSource classified
        if let some verdict←
            tryLocalExports chunk src classified indexed out work options timings then
          return verdict
        if let some verdict←
            tryInlineDeclarations chunk src classified indexed out work options timings then
          return verdict
        if javascriptSource chunk.source then
          if let .ok decoded := indexed then
            if let some verdict← tryDefaultSnapshot spec src decoded out work options timings then
              return verdict
            -- Standalone exports take precedence over the helper's rejection of metadata.
            if let some verdict← tryExportedFunction spec src decoded out work options timings then
              return verdict
            if let some verdict← tryPrototypeWrite spec src decoded out work options timings then
              return verdict
            if let some verdict← tryOrderedHelper spec src decoded out work options timings then
              return verdict
    if javascriptSource chunk.source then
      if let some verdict ← tryRetainedClasses spec src out work options timings then
        return verdict
      if let some verdict← tryRetainedTail spec src out work options timings then
        return verdict
  noConstructor options.request.label
      s!"no constructor accepts the requested placement of {chunk.path}"

/-- Select only a declared synchronous static graph with fixed dependencies.
The .js family requires the explicit Node ESM root opt-in. -/
private def finiteStaticInput (manifest : DeploymentManifest) :
    Option (Spec × List DeploymentChunk) := do
  guard (manifest.ownedNodePackage.isNone && manifest.chunks.length >= 3)
  let entry ← manifest.chunks.find? (·.path == manifest.entry)
  let .authored spec := entry.placement | none
  let fixed := manifest.chunks.filter (·.path != manifest.entry)
  let ext := if manifest.nodeEsmRoot.isSome then "js" else "mjs"
  guard (manifest.nodeEsmRoot.isNone || fixed.length == 2)
  guard
      (manifest.chunks.all
          (fun chunk => chunk.path.endsWith ("." ++ ext) && chunk.source.extension == some ext) &&
        fixed.all
          (fun chunk =>
            match chunk.placement with
            | .fixed => true
            | _ => false))
  return (spec, fixed)

private def runFiniteStaticGraph (manifest : DeploymentManifest) (spec : Spec)
    (fixed : List DeploymentChunk) (out work : System.FilePath) (options : CertifiedOptions)
    (timings : Bool) : IO UInt32 := do
  let scope := "finite-static-graph"
  let refuse := fun reason => noConstructor options.request.label s!"finite static graph: {reason}"
  try
    let root ← deploymentRoot manifest
    let fixedNames := fixed.map (·.path)
    let routes := manifest.entry :: fixedNames
    let (files, format) ←
      match manifest.nodeEsmRoot with
      | none =>
        pure (← readModules root work routes.toArray, [])
      | some declared =>
        do
          unless
            declared == root &&
              manifest.chunks.all (fun chunk => chunk.source == root / chunk.path) do
            throw (IO.userError "declared .js inputs must match the exact node_esm_root routes")
          let requests := work / "node-esm-routes.json"
          let captured := work / "node-esm-capture.json"
          IO.FS.writeFile requests (toJson routes).compress
          javascript #["--read-node-esm-root"] #[root, requests, captured]
          let input ← readJson captured
          pure
              (← IO.ofExcept (input.getObjVal? "files"),
                [("package.json", ← IO.ofExcept (input.getObjValAs? String "manifest"))])
    let decode ast name := do
      let text ← ast.getObjValAs? String "sourceText"
      CoreJS.AuthenticatedSource.decodeSourceClassification ast text name (importer := name)
    let ast ← IO.ofExcept (files.getObjVal? manifest.entry)
    let source ← IO.ofExcept (ast.getObjValAs? String "sourceText")
    let entry ← IO.ofExcept (decode ast manifest.entry)
    let providers ←
      IO.ofExcept
          (fixedNames.mapM fun name => do
            return (name, ← files.getObjVal? name))
    let decoded ← IO.ofExcept (providers.mapM fun (name, ast) => decode ast name)
    let family :=
      if manifest.nodeEsmRoot.isSome then CoreJS.FiniteStaticGraph.Family.nodePrototype
      else .primitive
    let built ←
      match CoreJS.FiniteStaticGraph.construct entry decoded spec family with
      | .ok built =>
        pure built
      | .error reason =>
        return ← refuse reason
    checkNewRoutes root work spec.order
    let programs ←
      IO.ofExcept
          (ESTreeEmit.programs entry.nodes (sourceBundle entry.nodes) built.printerPlan source
            spec.entry)
    let input := work / "source.mjs"
    IO.FS.writeFile input source
    let part : OutputPart := { programs, source, input, work }
    let fixedBytes ←
      IO.ofExcept
          (providers.mapM fun (name, ast) => do
            return (name, ← ast.getObjValAs? String "sourceText"))
    let confirm :=
      if manifest.nodeEsmRoot.isSome then
        javascript
          #["--check-node-package-destination", if options.checkOnly then "reread" else "fresh"]
          #[root, out, work]
      else pure ()
    let verify : Json → IO (Except CertificationRefusal Unit) := fun files =>
      pure do
        let ⟨same⟩ ←
          (CoreJS.ConstructedOutput.checkConstructedOutput files built.program).mapError
              fun reason => ({ reason } : CertificationRefusal)
        let _proof :=
          fun observed
            (decoded :
              CoreJS.ConstructedOutput.decodeConstructedOutput files built.program =
                .ok observed) =>
          built.readbackGuarantees (Except.ok.inj (decoded.symm.trans same))
        return ()
    let result ←
      certifyOutputWithFixed [part] (fixedBytes ++ format) out work options.checkOnly timings verify
          confirm (recheckPublished := manifest.nodeEsmRoot.isSome) (parseProviders := true)
    match result with
    | .ok _ =>
      emitVerdict options.request.label (.ok .allOutcomes) scope
          [("strategy", toJson "compiled-constructor"),
            importReceipt options built.before built.program (fixedNames ++ format.map Prod.fst)
              (nodeEsmRoot := manifest.nodeEsmRoot.isSome)]
    | .error why =>
      emitRefusal options.request.label why scope
  catch e =>
    refuse e.toString

def certified (args : List String) : IO UInt32 := do
  let (level?, args) := EsmSplit.takeFlag "--require" args
  let label := level?.getD Guarantee.normalCompletion.label
  try
    let request ←
      match label with
      | "all-outcomes" =>
        pure Guarantee.allOutcomes
      | "normal-completion" =>
        pure Guarantee.normalCompletion
      | _ =>
        throw (IO.userError "--require expects all-outcomes or normal-completion")
    if
        args.any
          (fun arg =>
            ["--host-contract", "--structural", "--deployment-origin", "--deployment-import-map",
                  "--deployment-preset"].contains
              arg) then
      return ← noConstructor label "no constructor supports host or structural deployment options"
    let checkOnly := args.contains "--check-output"
    let [manifestPath, dest] := args.filter (· != "--check-output")
      | throw (IO.userError "usage: lowersplit <deployment.json> <outdir> [--require LEVEL] [--check-output]")
    let out ← IO.ofExcept (outputDirectory dest)
    let manifest ←
      readDeploymentManifest manifestPath (allowOwnedPackage := true) (allowNodeEsmRoot := true)
    let timings := (← IO.getEnv "SPLIT_PROOF_TIMINGS") == some "1"
    IO.FS.withTempDir fun work =>
        if let [chunk] := manifest.chunks then do
          if manifest.nodeEsmRoot.isSome then
            return ← noConstructor label "node_esm_root requires the declared three-file graph"
          match manifest.ownedNodePackage with
          | some pkg =>
            runOwnedNodePackage pkg chunk out work { request, checkOnly } timings
          | none =>
            runSingleton chunk out work { request, checkOnly } timings
        else
          if let some (spec, fixed) := finiteStaticInput manifest then
            runFiniteStaticGraph manifest spec fixed out work { request, checkOnly } timings
          else noConstructor label "no constructor supports multi-chunk deployments"
  catch e =>
    emitVerdict label (.error e.toString)

end EsmSplit.LowerSplit
