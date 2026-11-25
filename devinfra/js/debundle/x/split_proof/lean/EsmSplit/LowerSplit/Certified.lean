import EsmSplit.Cli
import EsmSplit.CoreJS.Declaration.Artifacts
import EsmSplit.CoreJS.JointStatic
import EsmSplit.CoreJS.LinkedCertificate
import EsmSplit.CoreJS.LinkedInput
import EsmSplit.CoreJS.Move
import EsmSplit.CoreJS.StructuralCertificate
import EsmSplit.ESTreeEmit
import EsmSplit.LowerSplit.Input
import EsmSplit.LowerSplit.SliceCertificate
import EsmSplit.ReadDeployment

/-! Certify a proposed split and publish its output or verify the existing tree. -/

namespace EsmSplit.LowerSplit

open Lean EsmSplit.SplitFiles

open CoreJS (Guarantee)

/-- Check the generated proof with the same toolchain and model as the splitter. -/
private def checkLean (work : System.FilePath) (args : Array String) : IO Unit := do
  let exe ← IO.appPath
  let args := #["-DwarningAsError=true", s!"--root={work}"] ++ args
  let proc ← if exe.fileName == some "lean" then
      IO.Process.output { cmd := exe.toString, args }
    else do
      -- The native Lake executable is <project>/.lake/build/bin/lowersplit.
      -- TODO: Consider resolving the project environment once per run (see split_proof/TODO.md).
      let mut project := exe
      for _ in [:4] do project := project.parent.getD "."
      IO.Process.output { cmd := "lake", args := #["env", "lean"] ++ args, cwd := some project }
  unless proc.exitCode == 0 do
    let detail := (proc.stdout ++ proc.stderr).trimAscii.take 1200
    let reason := s!"could not prove equivalent linked executions (Lean exited with status {proc.exitCode})"
    throw (IO.userError (if detail.isEmpty then reason else s!"{reason}: {detail}"))

private def checkProofModule (work : System.FilePath) (setup : Lean.ModuleSetup)
    (code : String) (args : Array String) : IO Unit := do
  let stem := setup.name.toString
  let file := work / (stem ++ ".lean")
  let config := work / (stem ++ ".setup.json")
  IO.FS.writeFile file code
  IO.FS.writeFile config (toJson setup).compress
  checkLean work (#[s!"--setup={config}"] ++ args ++ #[file.toString])

private def runProofRecipe (timings : Bool) (work : System.FilePath)
    (dataArts : Lean.NameMap Lean.ImportArtifacts) (recipe : CoreJS.LinkedBehavior.ProofRecipe) : IO Unit := do
  let mut importArts := dataArts
  for (module, index) in recipe.zipIdx do
    let intermediate := index + 1 < recipe.size
    let args := if intermediate then
        #["-o", (work / (module.name.toString ++ ".olean")).toString]
      else #[]
    timePhase timings module.timing fun _ =>
      checkProofModule work { name := module.name, importArts } module.code args
    if intermediate then
      let compiled := work / (module.name.toString ++ ".olean")
      importArts := importArts.insert module.name (.ofArrays #[#[compiled]])

private def writeCertificateData (work : System.FilePath) (terms : CertificateData.Entries) :
    IO (Lean.NameMap Lean.ImportArtifacts) := do
  let dataName := CertificateData.moduleName
  let dataFile := work / (dataName.toString ++ ".olean")
  CertificateData.write dataFile terms
  return ({} : Lean.NameMap Lean.ImportArtifacts).insert dataName (.ofArrays #[#[dataFile]])

/-- Publication waits for the kernel-checked executions and the exact AST gate. -/
private def verifyExecution (timings : Bool) (work : System.FilePath) (cert : CoreJS.LinkedBehavior.Certificate)
    (dataArts : Lean.NameMap Lean.ImportArtifacts) : IO CoreJS.HostPremises := do
  let accepted ← match cert.checkpoint with
    | none => pure false
    | some recipe => try
        runProofRecipe timings work dataArts recipe
        pure true
      catch _ => pure false
  unless accepted do runProofRecipe timings work dataArts cert.ordinary
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
      pure (bundleOfFootprints asts fps #[])
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

private def replayForPair (timings : Bool) (phase : String) (profile : CoreJS.HostProfile)
    (program providers files : Json) (plan : Plan) (original candidate : CoreJS.Program)
    (witness : CoreJS.HostPremises × Nat) : IO (Option CoreJS.HostPremises) := do
  let (premises, fuel) := witness
  timePhase timings phase fun _ => IO.lazyPure fun _ =>
    match CoreJS.LinkedBehavior.replayPremisedPrograms premises fuel original candidate with
    | none => none
    | some semantics =>
      match CoreJS.LinkedBehavior.certifyASTs profile premises program providers files plan original candidate semantics.down with
      | .error _ => none
      | .ok _ => some premises

/-- Preserve singleton declarations and executable linked certificates,
with the original bundle at the virtual root for authored inputs. -/
private def runSingleton (chunk : DeploymentChunk) (out work : System.FilePath)
    (options : CertifiedOptions) (timings : Bool) : IO UInt32 := do
  let request := options.request
  let profile := options.profile
  let intrinsics := options.intrinsics
  let structural := options.structural
  let checkOnly := options.checkOnly
  let src ← readBundle chunk.source work true timings (← IO.ofExcept chunk.sourceHash)
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
  -- The opt-in census wrapper must be able to measure linked execution even
  -- when the ordinary selector would take the closed-declaration fast path.
  let censusLinked ← do
    let enabled ← IO.getEnv "SPLIT_PROOF_STRATEGY_CENSUS"
    let forceLinked ← IO.getEnv "SPLIT_PROOF_CENSUS_FORCE_LINKED"
    pure (enabled == some "1" && forceLinked == some "1")
  let declaration := if structural || censusLinked then none else declarations.bind fun groups =>
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
      let decoded ← timePhase timings "linked_lowering" fun _ =>
        IO.lazyPure fun _ => CoreJS.LinkedInput.decodePair program providers files P profile
      match decoded with
      | .error reason => pure (.error ({ reason, reasonCode := some "linked_input" } : CertificationRefusal))
      | .ok (original, candidate) => do
        let probe ← timePhase timings "compiled_slice_probe" fun _ =>
          IO.lazyPure fun _ => CoreJS.LinkedBehavior.probeWitness profile original candidate
        -- An accessor and a property-read blocker trigger a real replay
        -- preflight. Its result controls strategy order; only successful
        -- replay permits the slice to replace this existing fallback path.
        let replayKinds := CoreJS.LinkedBehavior.replayPremisedBlockerKinds original candidate
        let hasAccessorRead := replayKinds.contains "object-accessor" && replayKinds.contains "property-read"
        let replayForSlice ← match probe with
          | .ok witness =>
            if hasAccessorRead then
              replayForPair timings "compiled_replay_slice_guard" profile program providers files P original candidate witness
            else pure none
          | .error _ => pure none
        let skipSliceForReplay := match probe with
          | .ok _ => hasAccessorRead && replayForSlice.isNone
          | .error _ => false
        let sliceResult ← match probe with
          | .error refusal => pure (.error (probeRefusal refusal))
          | .ok _ =>
            if skipSliceForReplay then
              pure (.error ({
                reason := "compiled replay declined a property read; retaining the generated proof path"
                reasonCode := some "slice_replay_guard"
              } : CertificationRefusal))
            else timePhase timings "compiled_slice" fun _ =>
              IO.lazyPure fun _ => certifySlice profile program providers files P B S
        match sliceResult with
        | .ok (kind, _statement) =>
          if request == .allOutcomes && kind.level != .allOutcomes then
            pure (.error ({
              reason := s!"the compiled {kind.scope} slice proves only normal-completion"
              reasonCode := some "slice_guarantee"
            } : CertificationRefusal))
          else pure (.ok (some kind, {}, "compiled-slice", []))
        | .error _refusal =>
          if request != .normalCompletion then
            pure (.error ({ reason := "linked execution proves only normal-completion" } : CertificationRefusal))
          else do
            let census ← IO.getEnv "SPLIT_PROOF_STRATEGY_CENSUS"
            let replayBlockers := if census == some "1" then replayKinds else []
            let replayed : Option CoreJS.HostPremises ← match probe with
              | .error _ => pure none
              | .ok witness =>
                if let some premises := replayForSlice then pure (some premises)
                else if skipSliceForReplay then pure none
                else replayForPair timings "compiled_replay" profile program providers files P original candidate witness
            if let some premises := replayed then
              pure (.ok (none, premises, "compiled-replay-premised", []))
            else
              let prepared : Except CertificationRefusal
                  (CoreJS.LinkedBehavior.Certificate × Lean.NameMap Lean.ImportArtifacts) ←
                timePhase timings "certificate_preparation" fun _ => do
                  match probe with
                  | .error refusal => pure (.error (probeRefusal refusal))
                  | .ok witness => do
                    let result ← IO.lazyPure fun _ =>
                      CoreJS.LinkedBehavior.certificateWithWitness profile program providers files P original candidate witness
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
              | .ok (cert, dataArts) => do
                let used ← verifyExecution timings work cert dataArts
                pure (.ok (none, used, "generated-proof", replayBlockers))
    match checked with
    | .ok (some kind, active, strategy, blockers) => do
      let blockerFields := if blockers.isEmpty then [] else [("replay_blockers", toJson blockers)]
      return ← emitVerdict request.label (.ok kind.level) kind.scope
        (profileVerdict profile active ++ [("strategy", toJson strategy)] ++ blockerFields)
    | .ok (none, active, strategy, blockers) => do
      let blockerFields := if blockers.isEmpty then [] else [("replay_blockers", toJson blockers)]
      return ← emitVerdict request.label (.ok .normalCompletion) "linked-programs"
        (profileVerdict profile active ++ [("strategy", toJson strategy)] ++ blockerFields)
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

def certified (args : List String) : IO UInt32 := do
  let (level?, args) := EsmSplit.takeFlag "--require" args
  let label := level?.getD Guarantee.normalCompletion.label
  try
    let (manifestPath, out, options) ← parseCertifiedOptions label args
    let manifest ← readDeploymentManifest manifestPath
    let timings := (← IO.getEnv "SPLIT_PROOF_TIMINGS") == some "1"
    IO.FS.withTempDir fun work =>
      match manifest.chunks, options.deploymentOrigin with
      | [chunk], none => runSingleton chunk out work options timings
      | _, _ => runDeployment manifest out work options timings
  catch e => emitVerdict label (.error e.toString)

end EsmSplit.LowerSplit
