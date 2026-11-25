import EsmSplit.Cli
import EsmSplit.CoreJS.Declaration.Input
import EsmSplit.CoreJS.JointStatic
import EsmSplit.CoreJS.LinkedCertificate
import EsmSplit.CoreJS.LinkedInput
import EsmSplit.CoreJS.Move
import EsmSplit.CoreJS.StructuralCertificate
import EsmSplit.ESTreeEmit
import EsmSplit.LowerSplit.Input
import EsmSplit.ReadDeployment

/-! Certify a proposed split and publish its output or verify the existing tree. -/

namespace EsmSplit.LowerSplit

open Lean EsmSplit.SplitFiles

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

private def emitVerdict (outcome : Except String Unit) (scope : Option String := none)
    (extra : List (String × Json) := []) : IO UInt32 := do
  let fields := match outcome with
    | .error why => [("status", toJson "rejected"), ("guarantee", Json.null), ("reason", toJson why)]
    | .ok _ => [("status", toJson "certified"), ("guarantee", toJson "normal-equivalence")]
  IO.println (Json.mkObj ([("scope", toJson scope)] ++ fields ++ extra)).compress
  return if outcome.isOk then 0 else 1

/-- Convert a typed refusal into the existing verdict fields at the CLI boundary. -/
private def emitRefusal (refusal : CertificationRefusal)
    (scope : Option String := none) : IO UInt32 := do
  let reasonCodeFields := refusal.reasonCode.toList.map fun code => ("reason_code", toJson code)
  let observationFields := refusal.observation.toList.map fun observation => ("observation", observation)
  let details := reasonCodeFields ++ observationFields ++ refusal.details
  emitVerdict (.error (IO.userError refusal.reason).toString) scope details

private def structuralRefusal (refusal : CoreJS.LinkedBehavior.StructuralRefusal) : CertificationRefusal :=
  { reason := refusal.reason, reasonCode := refusal.reasonCode }

private def profileFields (profile : CoreJS.HostProfile) : List (String × Json) :=
  if profile == .empty then [] else [("hostProfile", toJson profile.label)]

private def profilePremises (selected : CoreJS.HostProfile) (used : CoreJS.HostPremises) : List String :=
  (if selected == .empty then [] else [s!"Host profile: {selected.label}"]) ++ used.assumptions

private structure StrategyResult where
  scope : String
  everyEndingMatches : Bool := false
  premises : List String := []

private structure CertifiedStrategy where
  name : String
  certify : Json → IO (Except CertificationRefusal StrategyResult)

private structure StrategyFailure where
  strategy : String
  refusal : CertificationRefusal

private structure SelectedStrategy where
  strategy : String
  result : StrategyResult
  failures : List StrategyFailure := []

private def strategyFailureJson (failure : StrategyFailure) : Json :=
  let reasonCode := match failure.refusal.reasonCode with
    | none => Json.null
    | some code => toJson code
  Json.mkObj [
    ("strategy", toJson failure.strategy),
    ("reason_code", reasonCode),
    ("reason", toJson failure.refusal.reason)
  ]

/-- Try proof strategies in order. Each strategy has one typed result shape, so new
compiled checks can be inserted without changing fallback control flow. -/
private def tryStrategies (strategies : List CertifiedStrategy) (files : Json) :
    IO (Except CertificationRefusal SelectedStrategy) := do
  let mut failures := []
  for strategy in strategies do
    let result ← try strategy.certify files catch error =>
      pure (.error ({ reason := error.toString } : CertificationRefusal))
    match result with
    | .ok result => return .ok { strategy := strategy.name, result, failures }
    | .error refusal => failures := failures ++ [{ strategy := strategy.name, refusal }]
  let structuralCode := failures.find? fun failure =>
    failure.strategy == "structural" && failure.refusal.reasonCode.isSome
  match structuralCode <|> failures.find? (fun failure => failure.refusal.reasonCode.isSome) <|>
      failures.getLast? with
  | none => return .error { reason := "no certification strategy is available" }
  | some best =>
    let attempts := toJson (failures.map strategyFailureJson)
    return .error { best.refusal with
      details := best.refusal.details ++ [
        ("strategy_attempts", attempts)
      ] }

private def emitSelected (profile : CoreJS.HostProfile) (selection : SelectedStrategy) : IO UInt32 := do
  let extra := profileFields profile ++ [
    ("strategy", toJson selection.strategy),
    ("premises", toJson selection.result.premises),
    ("every_ending_matches", toJson selection.result.everyEndingMatches)
  ] ++
    (if selection.failures.isEmpty then [] else
      [("strategy_attempts", toJson (selection.failures.map strategyFailureJson))])
  emitVerdict (.ok ()) selection.result.scope extra

private structure CertifiedOptions where
  profile : CoreJS.HostProfile
  intrinsics : List CoreJS.StableIntrinsic
  deploymentOrigin : Option String
  checkOnly : Bool
  browserOptions : CoreJS.ModuleLocation.BrowserOptions

private def parseCertifiedOptions (args : List String) : IO (System.FilePath × System.FilePath × CertifiedOptions) := do
  let (contract?, args) := EsmSplit.takeFlag "--host-contract" args
  let (profile, intrinsics) ← match contract? with
    | none => pure (CoreJS.HostProfile.empty, [])
    | some path => do
      let j ← IO.ofExcept (Json.parse (← IO.FS.readFile path))
      let presets : List (CoreJS.HostProfile × CoreJS.StableIntrinsic) :=
        [(.browserPage, .mathPI), (.ecmascript, .objectDefineProperty)]
      match presets.find? (fun (p, d) => j == Json.mkObj [("profile", toJson p.label),
          ("stableIntrinsicValues", toJson [d.id])]) with
      | some (p, d) => pure (p, [d])
      | none => pure (← IO.ofExcept (CoreJS.HostProfile.read j), [])
  let (deploymentOrigin, args) := EsmSplit.takeFlag "--deployment-origin" args
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
  let usage := IO.userError "usage: lowersplit <deployment.json> <outdir> [--check-output] [--host-contract PROFILE.json] [--deployment-origin ORIGIN [--deployment-preset native-browser-record@1] [--deployment-import-map empty@1]]; use --diagnostic for plan and preview options"
  let [manifest, out] := args | throw usage
  if args.any (·.startsWith "--") then throw usage
  let out ← IO.ofExcept (outputDirectory out)
  return (manifest, out, { profile, intrinsics, deploymentOrigin, checkOnly, browserOptions := { nativeRecord, emptyMap } })

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

private def structuralResult (options : CertifiedOptions) (used : List CoreJS.StableIntrinsic)
    (proof : CoreJS.LinkedBehavior.StructuralProof) : StrategyResult :=
  let scope := match proof with
    | .preservedOrder => "linked-structural-order"
    | .singlePivot => "linked-structural-pivot"
    | .multiplePivots => "linked-structural-moves"
  let premises :=
    (if options.profile == .empty then [] else [s!"Host profile: {options.profile.label}"]) ++
    ["Retained source units use the same capability-closed opaque world in both layouts."] ++
    (if options.browserOptions.nativeRecord then [s!"{CoreJS.NativeRecord.preset}: Initializing retained module metadata is unobservable except for a fresh null-prototype object with only native writable, enumerable, configurable url and resolve data properties. Each per-record resolver uses the unchanged module URL and static resolution context."] else []) ++
    (if options.browserOptions.emptyMap then ["empty@1: The browser's import map remains empty and the native module resolver is unchanged. Retained bare imports reject their promises on resolution failure, with ordinary browser promise scheduling and rejection reporting."] else []) ++
    used.map (·.assumption) ++
    (options.deploymentOrigin.toList.flatMap fun origin => [
      s!"The static deployment at {origin} overlays only the planned module paths.",
      "The native ESM loader uses one module record per effective URL and resolves dynamic dependencies through the checked live cells."
    ])
  let everyEndingMatches := match proof with
    | .preservedOrder => true
    | .singlePivot | .multiplePivots => false
  { scope, everyEndingMatches, premises }

private def checkPartLayout (part : Part) (timings : Bool) : IO Unit := do
  let ok ← timePhase timings "plan_checks" fun _ => checkLayout part.bundle part.plan (some part.spec) false
  unless ok do
    throw (IO.userError "source placement or module linkage checks failed")

/-- Preserve singleton declarations and executable linked certificates,
with the original bundle at the virtual root for authored inputs. -/
private def runSingleton (chunk : DeploymentChunk) (out work : System.FilePath)
    (options : CertifiedOptions) (timings : Bool) : IO UInt32 := do
  let profile := options.profile
  let intrinsics := options.intrinsics
  let checkOnly := options.checkOnly
  let src ← readBundle chunk.source work true timings (← IO.ofExcept chunk.sourceHash)
  let declarations := (CoreJS.DeclarationInput.decode src.program).toOption
  let part ← prepare chunk src work profile "bundle.mjs" timings declarations
  let { original := program, plan := P, output } := part
  let verify {α : Type} (providers : Json) (check : Json → IO (Except CertificationRefusal α)) :=
    certifyOutput [output] providers out work checkOnly timings check
  let providers := fun _ => timePhase timings "provider_loading" fun _ => do
    let (entry, root) ← if javascriptSource chunk.source then
        let path ← IO.FS.realPath chunk.source
        pure (path.fileName.getD "bundle.mjs", path.parent)
      else pure ("bundle.mjs", none)
    Json.mkObj <$> loadProviders entry (Std.HashMap.ofList [(entry, program)]) root work (allowEntryImport := false)
  try
    checkPartLayout part timings
    let shared ← providers ()
    let structural : CertifiedStrategy := {
      name := "structural"
      certify := fun files => do
        timePhase timings "structural_certificate" fun _ => do
          let result ← IO.lazyPure fun _ =>
            CoreJS.LinkedBehavior.certifyStructuralASTs profile intrinsics program shared files P none
          pure <| result.mapError structuralRefusal |>.map fun ⟨used, _, proof, _⟩ =>
            structuralResult options used proof
    }
    let linked : CertifiedStrategy := {
      name := "linked"
      certify := fun files => do
        let decoded ← timePhase timings "linked_lowering" fun _ =>
          IO.lazyPure fun _ => CoreJS.LinkedInput.decodePair program shared files P profile
        match decoded with
        | .error reason => pure (.error ({ reason, reasonCode := some "linked_input" } : CertificationRefusal))
        | .ok (original, candidate) => do
          let prepared : Except CertificationRefusal
              (CoreJS.LinkedBehavior.Certificate × Lean.NameMap Lean.ImportArtifacts) ←
            timePhase timings "certificate_preparation" fun _ => do
              let result ← IO.lazyPure fun _ =>
                CoreJS.LinkedBehavior.certificate profile program shared files P original candidate
              match result with
              | .error refusal =>
                pure (.error ({
                  reason := refusal.reason
                  reasonCode := some refusal.code.label
                  observation := refusal.observation
                } : CertificationRefusal))
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
            let active ← verifyExecution timings work cert dataArts
            pure (.ok {
              scope := "linked-programs"
              premises := profilePremises profile active
            })
    }
    let strategies := [structural, linked]
    let checked ← verify shared fun files => tryStrategies strategies files
    match checked with
    | .ok selected => emitSelected profile selected
    | .error refusal => emitRefusal refusal
  catch e => emitVerdict (.error e.toString)

/-- Load and check the actual static graph, then certify the union of its
changed parts and the byte-identical modules shared by both schedules. -/
private def runDeployment (manifest : DeploymentManifest) (out work : System.FilePath)
    (options : CertifiedOptions) (timings : Bool) : IO UInt32 := do
  let { profile, intrinsics, deploymentOrigin, checkOnly, browserOptions } := options
  try
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
        | .error reason =>
          let refusal := structuralRefusal reason
          let failure : StrategyFailure := { strategy := "structural", refusal }
          pure (.error { refusal with details := refusal.details ++ [
            ("strategy", toJson "structural"),
            ("strategy_attempts", toJson [strategyFailureJson failure])
          ] })
        | .ok ⟨used, _, proof, _⟩ =>
          pure (.ok { strategy := "structural", result := structuralResult options used proof })
    match checked with
    | .ok selected => emitSelected profile selected
    | .error refusal => emitRefusal refusal
  catch e => emitVerdict (.error e.toString)

def certified (args : List String) : IO UInt32 := do
  try
    let (manifestPath, out, options) ← parseCertifiedOptions args
    let manifest ← readDeploymentManifest manifestPath
    let timings := (← IO.getEnv "SPLIT_PROOF_TIMINGS") == some "1"
    IO.FS.withTempDir fun work =>
      match manifest.chunks, options.deploymentOrigin with
      | [chunk], none => runSingleton chunk out work options timings
      | _, _ => runDeployment manifest out work options timings
  catch e => emitVerdict (.error e.toString)

end EsmSplit.LowerSplit
