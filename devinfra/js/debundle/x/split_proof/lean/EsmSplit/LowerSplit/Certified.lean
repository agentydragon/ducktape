import EsmSplit.Cli
import EsmSplit.CoreJS.Declaration.Artifacts
import EsmSplit.CoreJS.JointStatic
import EsmSplit.CoreJS.IndexedCheckedSplit
import EsmSplit.CoreJS.IndexedSourceSchedule
import EsmSplit.CoreJS.LinkedCertificate
import EsmSplit.CoreJS.LinkedInput
import EsmSplit.CoreJS.Move
import EsmSplit.CoreJS.NamedDeclarationSplit
import EsmSplit.CoreJS.StructuralCertificate
import EsmSplit.CoreJS.OrderedConstSyntax
import EsmSplit.ESTreeEmit
import EsmSplit.LowerSplit.Input
import EsmSplit.LowerSplit.SliceCertificate
import EsmSplit.ReadDeployment

/-! Certify a proposed split and publish its output or verify the existing tree. -/

namespace EsmSplit.LowerSplit

open Lean EsmSplit.SplitFiles

open CoreJS (Guarantee)

/-- Compiler and project paths selected by the splitter's build. -/
structure GeneratedProofToolchain where
  lean : System.FilePath
  sysroot : System.FilePath
  importArts : Lean.NameMap Lean.ImportArtifacts := {}
  projectLibrary : Option System.FilePath := none
  projectSource : Option System.FilePath := none

/-- Use the SDK's default runtime settings and only the build-selected search paths. -/
private def checkLean (toolchain : GeneratedProofToolchain)
    (work : System.FilePath) (args : Array String) : IO Unit := do
  let paths := #[("LEAN_SYSROOT", some toolchain.sysroot.toString),
    ("LEAN_PATH", toolchain.projectLibrary.map toString),
    ("LEAN_SRC_PATH", toolchain.projectSource.map toString)]
  let defaults := #["LEAN_IMPORT_WORKERS", "LEAN_NUM_THREADS", "LEAN_STACK_SIZE_KB",
    "LEAN_MAIN_USE_THREAD", "LEAN_ABORT_ON_PANIC", "LEAN_ABORT_ON_NONLINEAR",
    "LEAN_BACKTRACE", "LEAN_BACKTRACE_RAW", "LEAN_TRACE_TASK_GET_BLOCKED"].map (·, none)
  let proc ← IO.Process.output {
    cmd := toolchain.lean.toString
    args := #["-DwarningAsError=true", s!"--root={work}"] ++ args
    env := paths ++ defaults
  }
  unless proc.exitCode == 0 do
    let detail := (proc.stdout ++ proc.stderr).trimAscii.take 1200
    let reason := s!"could not prove equivalent linked executions (Lean exited with status {proc.exitCode})"
    throw (IO.userError (if detail.isEmpty then reason else s!"{reason}: {detail}"))

private def checkProofModule (toolchain : GeneratedProofToolchain)
    (work : System.FilePath) (setup : Lean.ModuleSetup)
    (code : String) (args : Array String) : IO Unit := do
  let stem := setup.name.toString
  let file := work / (stem ++ ".lean")
  let config := work / (stem ++ ".setup.json")
  IO.FS.writeFile file code
  IO.FS.writeFile config (toJson setup).compress
  checkLean toolchain work (#[s!"--setup={config}"] ++ args ++ #[file.toString])

private def runProofRecipe (toolchain : GeneratedProofToolchain)
    (timings : Bool) (work : System.FilePath)
    (initialImports : Lean.NameMap Lean.ImportArtifacts) (recipe : CoreJS.LinkedBehavior.ProofRecipe) : IO Unit := do
  let mut importArts := initialImports
  for (module, index) in recipe.zipIdx do
    let intermediate := index + 1 < recipe.size
    let args := if intermediate then
        #["-o", (work / (module.name.toString ++ ".olean")).toString]
      else #[]
    timePhase timings module.timing fun _ =>
      checkProofModule toolchain work { name := module.name, importArts } module.code args
    if intermediate then
      let compiled := work / (module.name.toString ++ ".olean")
      importArts := importArts.insert module.name (.ofArrays #[#[compiled]])

private def writeCertificateData (work : System.FilePath)
    (terms : CertificateData.Entries) : IO System.FilePath := do
  let dataFile := work / (CertificateData.moduleName.toString ++ ".olean")
  CertificateData.write dataFile terms
  return dataFile

/-- Publication waits for the kernel-checked executions and the exact AST gate. -/
private def verifyExecution (loadToolchain : IO GeneratedProofToolchain)
    (timings : Bool) (work : System.FilePath) (cert : CoreJS.LinkedBehavior.Certificate)
    (dataFile : System.FilePath) : IO CoreJS.HostPremises := do
  let toolchain ← loadToolchain
  let importArts := toolchain.importArts.insert CertificateData.moduleName (.ofArrays #[#[dataFile]])
  let accepted ← match cert.checkpoint with
    | none => pure false
    | some recipe => try
        runProofRecipe toolchain timings work importArts recipe
        pure true
      catch _ => pure false
  unless accepted do runProofRecipe toolchain timings work importArts cert.ordinary
  return cert.premises

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

private def structuralRefusal (refusal : CoreJS.LinkedBehavior.StructuralRefusal) : CertificationRefusal :=
  { reason := refusal.reason, reasonCode := refusal.reasonCode }

private def probeRefusal (refusal : CoreJS.LinkedBehavior.Refusal) : CertificationRefusal :=
  { reason := refusal.reason
    reasonCode := some refusal.code.label
    observation := refusal.observation }

private def profileFields (profile : CoreJS.HostProfile) : List (String × Json) :=
  if profile == .empty then [] else [("hostProfile", toJson profile.label)]

private def profileVerdict (selected : CoreJS.HostProfile) (used : CoreJS.HostPremises) : List (String × Json) :=
  if selected == .empty then [] else profileFields selected ++ [("assumptions", toJson used.assumptions)]

private structure CertifiedOptions where
  request : Guarantee
  profile : CoreJS.HostProfile
  intrinsics : List CoreJS.StableIntrinsic
  structural : Bool
  deploymentOrigin : Option String
  checkOnly : Bool
  browserOptions : CoreJS.ModuleLocation.BrowserOptions

private def parseCertifiedOptions (label : String) (args : List String) : IO (System.FilePath × System.FilePath × CertifiedOptions) := do
  let request ← match label with
    | "all-outcomes" => pure Guarantee.allOutcomes
    | "normal-completion" => pure Guarantee.normalCompletion
    | _ => throw (IO.userError "--require expects all-outcomes or normal-completion")
  let (contract?, args) := EsmSplit.takeFlag "--host-contract" args
  let (profile, intrinsics) ← match contract? with
    | none => pure (CoreJS.HostProfile.empty, [])
    | some path => do
      let j ← IO.ofExcept (Json.parse (← IO.FS.readFile path))
      IO.ofExcept (CoreJS.StableIntrinsic.readContract j)
  let structural := args.contains "--structural"
  unless structural || intrinsics.isEmpty do
    throw (IO.userError "stableIntrinsicValues requires --structural")
  let args := args.filter (· != "--structural")
  let (deploymentOrigin, args) := EsmSplit.takeFlag "--deployment-origin" args
  if deploymentOrigin.isSome && !structural then
    throw (IO.userError "--deployment-origin requires --structural")
  let (preset, args) := EsmSplit.takeFlag "--deployment-preset" args
  let nativeRecord := preset == some CoreJS.NativeRecord.preset
  if preset.isSome && !(nativeRecord && deploymentOrigin.isSome && profile == .browserPage) then
    throw (IO.userError "native-browser-record@1 requires a browser-page profile and deployment origin")
  let (mapProfile, args) := EsmSplit.takeFlag "--deployment-import-map" args
  let emptyMap := mapProfile == some "empty@1"
  if mapProfile.isSome && !(emptyMap && deploymentOrigin.isSome && profile == .browserPage) then
    throw (IO.userError "empty@1 requires a browser-page profile and deployment origin")
  let checkOnly := args.contains "--check-output"
  let args := args.filter (· != "--check-output")
  let usage := IO.userError "usage: lowersplit <deployment.json> <outdir> [--require LEVEL] [--check-output] [--host-contract PROFILE.json] [--structural [--deployment-origin ORIGIN [--deployment-preset native-browser-record@1] [--deployment-import-map empty@1]]]; use --diagnostic for plan and preview options"
  let [manifest, out] := args | throw usage
  if args.any (·.startsWith "--") then throw usage
  let out ← IO.ofExcept (outputDirectory out)
  return (manifest, out, { request, profile, intrinsics, structural, deploymentOrigin, checkOnly, browserOptions := { nativeRecord, emptyMap } })

private structure Part where
  original : Json
  bundle : Bundle
  spec : Spec
  plan : Plan
  output : OutputPart

/-- An entry that must publish glue cannot also expose exactly the original
namespace. With the author's permission, keep the interface statements at the
public route and place the other entry units together in a private module. -/
private def entryFacade (asts : Array Json) (B : Bundle) (S : Spec) (P : Plan) : Option Spec := do
  guard S.privateEntryBody
  let entry ← P.find S.entry
  guard (!entry.exports.isEmpty)
  let bodyName := S.entry ++ ".body.mjs"
  guard ((P.find bodyName).isNone)
  let defs := Std.HashSet.ofList (entry.exports.map Prod.snd)
  let statements := B.toArray
  let last ← (entry.stmts.filter fun i => (statements.getD i default).defs.any defs.contains).getLast?
  let (interface, body) := entry.stmts.partition fun i =>
    i > last || ["ImportDeclaration", "ExportNamedDeclaration", "ExportDefaultDeclaration", "ExportAllDeclaration"].contains
      (Lower.typ (asts.getD i Json.null))
  guard (!body.isEmpty)
  let modules := S.modules.map fun (name, selectors) =>
    (name, if name == S.entry then interface.map Selector.stmt else selectors)
  return { S with modules := modules ++ [(bodyName, body.map Selector.stmt)],
                  order := if S.order.isEmpty then [] else bodyName :: S.order }

private def prepare (chunk : DeploymentChunk) (input : BundleInput) (work : System.FilePath)
    (profile : CoreJS.HostProfile) (sourcePath : String) (timings : Bool)
    (declarations : Option (List CoreJS.DeclarationGroup)) (retainEntryRoute := false) : IO Part := do
  let { program, source, input } := input
  let asts ← IO.ofExcept (ESTreeUnits.checkedNodes program source)
  if let some reason := asts.findSome? unsupportedStatement then throw (IO.userError reason)
  let units := Effects.footprintUnits asts
  let B ← if declarations.isSome then do
      let (fps, _) ← analyzeFootprints asts units {} timings
      pure (bundleOfFootprints asts fps)
    else timePhase timings "layout_scan" fun _ => IO.lazyPure fun _ => linkedLayoutBundle asts units
  let S ← IO.ofExcept (chunk.spec asts.size)
  for name in S.entry :: S.modules.map Prod.fst do
    let moduleExtension := name.endsWith ".mjs" ||
      ((profile == .browserPage || (retainEntryRoute && name == sourcePath)) && name.endsWith ".js")
    unless CoreJS.ModuleInput.supportedFilename name && moduleExtension do
      throw (IO.userError s!"certified output requires a safe relative .mjs path (.js requires a retained deployment entry or browser-page modules), got {name}")
  for (ast, i) in asts.zipIdx do
    if let some reason := entryPlacementProblem B S ast i then throw (IO.userError reason)
  let P ← planSplit B S timings
  let (S, P) ← match entryFacade asts B S P with
    | none => pure (S, P)
    | some physical => do
      unless ← checkLayout B P (some S) false do throw (IO.userError "invalid logical placement before entry facade")
      pure (physical, ← planSplit B physical timings)
  let programs ← timePhase timings "output_ast" fun _ =>
    IO.ofExcept (ESTreeEmit.programs asts B P source sourcePath)
  return { original := program, bundle := B, spec := S, plan := P, output := { programs, source, input, work } }

private def requireGuarantee (request proved : Guarantee) : IO Unit := do
  if request == .allOutcomes && proved != .allOutcomes then
    throw (IO.userError "the certificate proves only normal-completion")

private def emitStructural (options : CertifiedOptions) (used : List CoreJS.StableIntrinsic)
    (level : Guarantee) (proof : CoreJS.LinkedBehavior.StructuralProof) : IO UInt32 := do
  let scope := match proof with
    | .preservedOrder => "linked-structural-order"
    | .singlePivot => "linked-structural-pivot"
    | .multiplePivots => "linked-structural-moves"
  let assumptions :=
    ["Retained source units use the same capability-closed opaque world in both layouts."] ++
    (if options.browserOptions.nativeRecord then [s!"{CoreJS.NativeRecord.preset}: Initializing retained module metadata is unobservable except for a fresh null-prototype object with only native writable, enumerable, configurable url and resolve data properties. Each per-record resolver uses the unchanged module URL and static resolution context."] else []) ++
    (if options.browserOptions.emptyMap then ["empty@1: The browser's import map remains empty and the native module resolver is unchanged. Retained bare imports reject their promises on resolution failure, with ordinary browser promise scheduling and rejection reporting."] else []) ++
    used.map (·.assumption) ++
    (options.deploymentOrigin.toList.flatMap fun origin => [
      s!"The static deployment at {origin} overlays only the planned module paths.",
      "The native ESM loader uses one module record per effective URL and resolves dynamic dependencies through the checked live cells."
    ])
  emitVerdict options.request.label (.ok level) scope
    (profileFields options.profile ++ [("assumptions", toJson assumptions)])

private def checkPartLayout (part : Part) (timings : Bool) : IO Unit := do
  let ok ← timePhase timings "plan_checks" fun _ => checkLayout part.bundle part.plan (some part.spec) false
  unless ok do
    throw (IO.userError "source placement or module linkage checks failed")

/-- Which check rejected the concrete singleton input before strategy selection. -/
inductive SingletonPreparationRefusal where
  | input (reason : String)
  | placement (reason : String)
  | probe (refusal : CoreJS.LinkedBehavior.Refusal)

def SingletonPreparationRefusal.toCertificationRefusal : SingletonPreparationRefusal → CertificationRefusal
  | .input reason => { reason, reasonCode := some "linked_input" }
  | .placement reason => { reason, reasonCode := some "linked_placement" }
  | .probe refusal => probeRefusal refusal

private def retainDecoded (result : Except String CoreJS.LinkedInput.DecodedDeployment) :
    Except SingletonPreparationRefusal
      { deployment : CoreJS.LinkedInput.DecodedDeployment // result = Except.ok deployment } :=
  match result with
  | .ok deployment => .ok ⟨deployment, rfl⟩
  | .error reason => .error (.input reason)

def prepareSliceEligibility (profile : CoreJS.HostProfile) (source providers files : Json)
    (P : Plan) (timings : Bool) : IO (Except SingletonPreparationRefusal (SliceEligibility profile source providers files P)) := do
  let decoded ← timePhase timings "linked_lowering" fun _ => IO.lazyPure fun _ =>
    retainDecoded (CoreJS.LinkedInput.decodeSingleton source providers files P profile)
  match decoded with
  | .error refusal => pure (.error refusal)
  | .ok ⟨deployment, decodedSingleton⟩ =>
    timePhase timings "compiled_slice_probe" fun _ => IO.lazyPure fun _ => do
      let ⟨placed⟩ ← (CoreJS.checkPlaces deployment.assignment deployment.before deployment.after).mapError
        SingletonPreparationRefusal.placement
      match probed : CoreJS.LinkedBehavior.probeWitness profile deployment.before deployment.after with
      | .error refusal => throw (.probe refusal)
      | .ok probe =>
        let checkedLinks := CoreJS.LinkedBehavior.checkProbePair deployment.before deployment.after
        pure ⟨deployment, decodedSingleton, placed, probe, probed, checkedLinks⟩

/-- Stage the constructor's modules through the shared publication gate. -/
private def stageConstructed (program : CoreJS.Program) (src : BundleInput)
    (out work : System.FilePath) (options : CertifiedOptions) (timings : Bool) :
    IO (Except CertificationRefusal Unit) := do
  let programs ← IO.ofExcept (ESTreeEmit.corePrograms program src.source)
  let part : OutputPart := { programs, source := src.source, input := src.input, work }
  certifyOutput (α := Unit) [part] (Json.mkObj []) out work options.checkOnly timings fun files =>
    pure <| match CoreJS.LinkedInput.checkConstructedOutput files program with
      | .ok _ => .ok ()
      | .error reason => .error { reason }

/-- Use the proved ordered-declaration constructor when it accepts. Other inputs
continue through the existing certified routes; printer refusals remain final. -/
private def tryNamedDeclarations (chunk : DeploymentChunk) (src : BundleInput)
    (out work : System.FilePath) (options : CertifiedOptions) (timings : Bool) : IO (Option UInt32) := do
  unless options.profile == .empty && options.intrinsics.isEmpty && !options.structural do return none
  let .authored spec := chunk.placement | return none
  unless spec.sequential do return none
  let .ok (source, localExports) :=
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
  -- This dense constructor accounts for executable source units only. Do not
  -- publish or fall through to another route when original nodes were omitted
  -- or a standalone export row lacks an authenticated owner.
  let provenance : Except String Unit := do
    let nodes ← ESTreeUnits.checkedNodes src.program src.source
    let _ ← CoreJS.LinkedInput.checkSourceUnitCoverage nodes.size source localExports
    unless localExports.isEmpty do
      throw "standalone export rows require source-indexed placement"
    return ()
  if let .error reason := provenance then
    return some (← emitRefusal options.request.label { reason := s!"source provenance: {reason}" } "named-declarations")
  try
    let checked ← stageConstructed constructed.split.program src out work options timings
    match checked with
    | .ok _ => return some (← emitVerdict options.request.label (.ok .allOutcomes) "named-declarations"
        [("strategy", toJson "compiled-constructor")])
    | .error refusal => return some (← emitRefusal options.request.label refusal "named-declarations")
  catch e => return some (← emitVerdict options.request.label (.error e.toString) "named-declarations")

/-- Source and original metadata for the indexed local-export constructor. -/
private structure LocalExportSource where
  source : CoreJS.Program
  rows : List LocalExportUnit
  bundle : Bundle

private def decodeLocalExportSource (original : Json) (sourceText entry : String) : Except String LocalExportSource := do
  let decoded ← CoreJS.LinkedInput.decodeIndexedSource original sourceText entry
  let source := decoded.program
  let bundle := sourceBundle decoded.nodes
  let sequence ← CoreJS.IndexedSourceSchedule.checkSequence decoded bundle
  let m := sequence.source
  let rows := decoded.localExports
  unless !rows.isEmpty do throw "expected a local export row"
  unless m.exports == rows.flatMap (·.names) do throw "local export rows differ from the public export table"
  unless (CoreJS.OrderedConstSyntax.checkBody m.body).isSome &&
      (CoreJS.OrderedConstSyntax.checkEarlierReads [] m.body).isSome do
    throw "expected unique const declarations with source primitive initializers and earlier reads"
  return ⟨source, rows, bundle⟩

/-- Construct from the actual indexed source, bundle, export rows and authored
placement. Both terminal and interior rows use the same proof and writer. -/
private def tryLocalExports (chunk : DeploymentChunk) (src : BundleInput)
    (out work : System.FilePath) (options : CertifiedOptions) (timings : Bool) : IO (Option UInt32) := do
  unless options.profile == .empty && options.intrinsics.isEmpty && !options.structural &&
      options.deploymentOrigin.isNone do return none
  let .authored spec := chunk.placement | return none
  unless spec.sequential && spec.modules.all (fun row => row.1.endsWith ".mjs") do return none
  let .ok input := decodeLocalExportSource src.program src.source spec.entry | return none
  let .ok built := CoreJS.IndexedCheckedSplit.construct input.source input.bundle spec input.rows .allOutcomes |
    return none
  try
    match ← stageConstructed built.program src out work options timings with
    | .ok _ => return some (← emitVerdict options.request.label (.ok .allOutcomes) "indexed-local-exports"
        [("strategy", toJson "compiled-constructor")])
    | .error refusal => return some (← emitRefusal options.request.label refusal "indexed-local-exports")
  catch e => return some (← emitVerdict options.request.label (.error e.toString) "indexed-local-exports")

/-- Preserve singleton declarations and executable linked certificates,
with the original bundle at the virtual root for authored inputs. -/
private def runSingleton (loadToolchain : IO GeneratedProofToolchain)
    (chunk : DeploymentChunk) (out work : System.FilePath)
    (options : CertifiedOptions) (timings : Bool) : IO UInt32 := do
  let request := options.request
  let profile := options.profile
  let intrinsics := options.intrinsics
  let structural := options.structural
  let checkOnly := options.checkOnly
  let src ← readBundle chunk.source work true timings (← IO.ofExcept chunk.sourceHash)
  if let some verdict ← tryLocalExports chunk src out work options timings then return verdict
  if let some verdict ← tryNamedDeclarations chunk src out work options timings then return verdict
  let declarations := (CoreJS.DeclarationInput.decode src.program).toOption
  let part ← prepare chunk src work profile "bundle.mjs" timings declarations
  let { original := program, bundle := B, spec := S, plan := P, output } := part
  let verify {α : Type} (providers : Json) (check : Json → IO (Except CertificationRefusal α)) :=
    certifyOutput [output] providers out work checkOnly timings check
  let providers := fun _ => timePhase timings "provider_loading" fun _ => do
    let (entry, root) ← if javascriptSource chunk.source then
        let path ← IO.FS.realPath chunk.source
        pure (path.fileName.getD "bundle.mjs", path.parent)
      else pure ("bundle.mjs", none)
    Json.mkObj <$> loadProviders entry (Std.HashMap.ofList [(entry, program)]) root work (allowEntryImport := false)
  let declaration := if structural then none else declarations.bind fun groups =>
    CoreJS.DeclarationArtifacts.prepare? program (CoreJS.DeclarationProgram.statements groups) B S P
  if let some prepared := declaration then
    let level := prepared.level
    try
      requireGuarantee request level
      let checked ← verify (Json.mkObj []) fun files => do
        timePhase timings "closed_declarations" fun _ => do
          let _ ← IO.ofExcept (prepared.certify files)
          pure (.ok ())
      match checked with
      | .ok _ => return ← emitVerdict request.label (.ok level) "closed-declarations" (profileVerdict profile {})
      | .error refusal => return ← emitRefusal request.label refusal "closed-declarations"
    catch e => return ← emitVerdict request.label (.error e.toString) "closed-declarations"
  if structural then
    try
      checkPartLayout part timings
      let shared ← providers ()
      let checked ← verify shared fun files => do
        timePhase timings "structural_certificate" fun _ => do
          let result ← IO.lazyPure fun _ =>
            CoreJS.LinkedBehavior.certifyStructuralASTs profile intrinsics program shared files P none
          match result with
          | .error reason => pure (.error (structuralRefusal reason))
          | .ok ⟨used, level, proof, _⟩ => do
            requireGuarantee request level
            pure (.ok (used, level, proof))
      match checked with
      | .ok (used, level, proof) => return ← emitStructural options used level proof
      | .error refusal => return ← emitRefusal request.label refusal "linked-structural"
    catch e => return ← emitVerdict request.label (.error e.toString) "linked-structural"
  try
    checkPartLayout part timings
    let providers ← providers ()
    let checked ← verify providers fun files => do
      match ← prepareSliceEligibility profile program providers files P timings with
      | .error refusal => pure (.error refusal.toCertificationRefusal)
      | .ok eligibility => do
        let blockers := eligibility.replayBlockers
        let checkReplay := fun phase => timePhase timings phase fun _ => IO.lazyPure fun _ => eligibility.certifyReplay
        -- Keep the generated proof path for a property read that may invoke
        -- an accessor when the compiled replay check cannot handle that read.
        let needsAccessorCheck := blockers.contains .objectAccessor && blockers.contains .propertyRead
        let accessorReplay ← if needsAccessorCheck then checkReplay "compiled_replay_slice_guard" else pure none
        let sliceResult ← if needsAccessorCheck && accessorReplay.isNone then
            pure (.error ({
              reason := "compiled replay declined a property read; retaining the generated proof path"
              reasonCode := some "slice_replay_guard"
            } : CertificationRefusal))
          else timePhase timings "compiled_slice" fun _ =>
            IO.lazyPure fun _ => certifySlice profile program providers files P B S eligibility
        match sliceResult with
        | .ok ⟨kind, _statement⟩ =>
          if request == .allOutcomes && kind.level != .allOutcomes then
            pure (.error ({
              reason := s!"the compiled {kind.scope} slice proves only normal-completion"
              reasonCode := some "slice_guarantee"
            } : CertificationRefusal))
          else pure (.ok (some kind, {}, "compiled-slice"))
        | .error _refusal =>
          if request != .normalCompletion then
            pure (.error ({ reason := "linked execution proves only normal-completion" } : CertificationRefusal))
          else do
            let replayed ← if needsAccessorCheck then pure accessorReplay else checkReplay "compiled_replay"
            if replayed.isSome then
              pure (.ok (none, eligibility.probe.premises, "compiled-replay-premised"))
            else
              let prepared : Except CertificationRefusal
                  (CoreJS.LinkedBehavior.Certificate × System.FilePath) ←
                timePhase timings "certificate_preparation" fun _ => do
                  let result ← IO.lazyPure fun _ =>
                    CoreJS.LinkedBehavior.certificateWithWitness profile program providers files P eligibility.probe
                  match result with
                  | .error refusal => pure (.error (probeRefusal refusal))
                  | .ok (cert, terms) =>
                    try
                      pure (.ok (cert, ← writeCertificateData work terms))
                    catch err =>
                      pure (.error ({
                        reason := err.toString
                        reasonCode := some "proof_preparation"
                      } : CertificationRefusal))
              match prepared with
              | .error refusal => pure (.error refusal)
              | .ok (cert, dataFile) => do
                let used ← verifyExecution loadToolchain timings work cert dataFile
                pure (.ok (none, used, "generated-proof"))
    match checked with
    | .ok (some kind, active, strategy) => do
      return ← emitVerdict request.label (.ok kind.level) kind.scope
        (profileVerdict profile active ++ [("strategy", toJson strategy)])
    | .ok (none, active, strategy) => do
      return ← emitVerdict request.label (.ok .normalCompletion) "linked-programs"
        (profileVerdict profile active ++ [("strategy", toJson strategy)])
    | .error refusal => return ← emitRefusal request.label refusal "linked-programs"
  catch e => return ← emitVerdict request.label (.error e.toString) "linked-programs"

/-- Load and check the actual static graph, then certify the union of its
changed parts and the byte-identical modules shared by both schedules. -/
private def runDeployment (manifest : DeploymentManifest) (out work : System.FilePath)
    (options : CertifiedOptions) (timings : Bool) : IO UInt32 := do
  let { request, profile, intrinsics, structural, deploymentOrigin, checkOnly, browserOptions } := options
  try
    unless structural do throw (IO.userError "a deployment with multiple chunks requires --structural")
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
        let part ← prepare chunk source dir profile chunk.path timings none (retainEntryRoute := true)
        checkPartLayout part timings
        parts := parts.push part
        originals := originals.push (chunk.path, source.program)
    if parts.isEmpty then throw (IO.userError "expected at least one chunk placement")
    let paths := parts.toList.flatMap fun part =>
      (part.plan.mods.map (·.name)).filter (· != part.plan.entry)
    checkNewRoutes root work paths
    let supplied := Std.HashMap.ofList originals.toList
    let providers ← timePhase timings "provider_loading" fun _ =>
      loadProviders manifest.entry supplied (some root) work
    let reachable := Std.HashSet.ofList (manifest.entry :: providers.map Prod.fst)
    for (name, _) in originals do
      unless reachable.contains name do throw (IO.userError s!"original chunk is unreachable from the entry: {name}")
    let some main := supplied[manifest.entry]? | throw (IO.userError "missing deployment entry")
    let linked := (manifest.entry, main) :: providers
    let changed := Std.HashSet.ofList (parts.toList.map (·.plan.entry))
    let shared := Json.mkObj (linked.filter fun (name, _) => !changed.contains name)
    let deployment ← deploymentOrigin.mapM fun origin =>
      timePhase timings "deployment_loading" fun _ =>
        readDeployment manifest.entry linked root work origin browserOptions
    let checked ← certifyOutput (parts.toList.map (·.output)) shared out work checkOnly timings fun files => do
      let actual ← IO.ofExcept <| parts.toList.mapM fun part => do
        let outputs ← part.plan.mods.mapM fun m => do
          return (m.name, ← files.getObjVal? m.name)
        pure ({ source := part.original, files := Json.mkObj outputs, plan := part.plan } : CoreJS.LinkedInput.StaticSplit)
      timePhase timings "structural_certificate" fun _ => do
        let result ← IO.lazyPure fun _ =>
          CoreJS.LinkedBehavior.certifyDeployment manifest.entry actual shared deployment profile intrinsics
        match result with
        | .error reason => pure (.error (structuralRefusal reason))
        | .ok ⟨used, level, proof, _⟩ => do
          requireGuarantee request level
          pure (.ok (used, level, proof))
    match checked with
    | .ok (used, level, proof) => emitStructural options used level proof
    | .error refusal => emitRefusal request.label refusal "linked-structural"
  catch e => emitVerdict request.label (.error e.toString) "linked-structural"

def certified (args : List String) (loadToolchain : IO GeneratedProofToolchain) :
    IO UInt32 := do
  let (level?, args) := EsmSplit.takeFlag "--require" args
  let label := level?.getD Guarantee.normalCompletion.label
  try
    let (manifestPath, out, options) ← parseCertifiedOptions label args
    let manifest ← readDeploymentManifest manifestPath
    let timings := (← IO.getEnv "SPLIT_PROOF_TIMINGS") == some "1"
    IO.FS.withTempDir fun work =>
      match manifest.chunks, options.deploymentOrigin with
      | [chunk], none => runSingleton loadToolchain chunk out work options timings
      | _, _ => runDeployment manifest out work options timings
  catch e => emitVerdict label (.error e.toString)

end EsmSplit.LowerSplit
