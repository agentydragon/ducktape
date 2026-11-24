import EsmSplit.Cli
import EsmSplit.CoreJS.Declaration.Artifacts
import EsmSplit.CoreJS.LinkedCertificate
import EsmSplit.CoreJS.LinkedInput
import EsmSplit.CoreJS.Move
import EsmSplit.CoreJS.StructuralCertificate
import EsmSplit.LowerSplit.Input
import EsmSplit.ReadDeployment

/-! Certify a proposed split and publish its output or verify the existing tree. -/

namespace EsmSplit.LowerSplit

open Lean EsmSplit.SplitFiles

/-- The linked execution theorem checks the schedules themselves. Its planner needs only the
bindings each unit declares, mentions, assigns and exports. Promoting lazy reads to eager in
the effect analysis leaves their first-occurrence order in `eager ++ lazy` unchanged. -/
private def linkedLayoutBundle (asts units : Array Json) : Bundle :=
  let tops := units.map Lower.analyzeTop
  let declared := Effects.declaredNames tops
  (asts.zip tops).toList.map fun (ast, t) =>
    { src := "", defs := t.defs, eager := (Effects.dedupeArr t.rawEager).toList,
      lazy := (Effects.dedupeArr t.rawLazy).toList, assigns := Effects.assignedNames t declared,
      exports := published ast, hoisted := t.hoisted }

open CoreJS.DeclarationArtifacts (Guarantee)

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
  let fields := match outcome with
    | .error why => [("status", toJson "rejected"), ("guarantee", Json.null), ("reason", toJson why)]
    | .ok level => [("status", toJson "certified"), ("guarantee", toJson level.label)]
  IO.println (Json.mkObj ([("requested", toJson requested), ("scope", toJson scope)] ++ fields ++ extra)).compress
  return if outcome.isOk then 0 else 1

/-- Convert a typed refusal into the existing verdict fields at the CLI boundary. -/
private def emitRefusal (requested : String) (refusal : CertificationRefusal)
    (scope : Option String := none) : IO UInt32 := do
  let reasonCodeFields := refusal.reasonCode.toList.map fun code => ("reason_code", toJson code)
  let observationFields := refusal.observation.toList.map fun observation => ("observation", observation)
  let details := reasonCodeFields ++ observationFields
  emitVerdict requested (.error (IO.userError refusal.reason).toString) scope details

private def profileFields (profile : CoreJS.HostProfile) : List (String × Json) :=
  if profile == .empty then [] else [("hostProfile", toJson profile.label)]

private def profileVerdict (selected : CoreJS.HostProfile) (used : CoreJS.HostPremises)
    (extra : List String := []) : List (String × Json) :=
  if selected == .empty && extra.isEmpty then []
  else profileFields selected ++ [("assumptions", toJson (used.assumptions ++ extra))]

private def deploymentAssumptions (origin : Option String) : List String :=
  origin.toList.flatMap fun url => [
    s!"The static deployment at {url} overlays only the planned module paths.",
    "The native ESM loader uses one module record per effective URL and resolves dynamic dependencies through the checked live cells."
  ]

private structure CertifiedOptions where
  request : Guarantee
  profile : CoreJS.HostProfile
  intrinsics : List CoreJS.StableIntrinsic
  structural : Bool
  deploymentOrigin : Option String
  checkOnly : Bool
  nativeRecord : Bool
  resolvedChunk : Option String

private def parseCertifiedOptions (label : String) (args : List String) : IO (SplitPaths × CertifiedOptions) := do
  let request ← match label with
    | "all-outcomes" => pure Guarantee.allOutcomes
    | "normal-completion" => pure Guarantee.normalCompletion
    | _ => throw (IO.userError "--require expects all-outcomes or normal-completion")
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
  let structural := args.contains "--structural"
  unless structural || intrinsics.isEmpty do
    throw (IO.userError "stableIntrinsicValues requires --structural")
  let args := args.filter (· != "--structural")
  let (deploymentOrigin, args) := EsmSplit.takeFlag "--deployment-origin" args
  let (preset, args) := EsmSplit.takeFlag "--deployment-preset" args
  let nativeRecord := preset == some CoreJS.NativeRecord.preset
  if preset.isSome && !(structural && nativeRecord && deploymentOrigin.isSome && profile == .browserPage) then
    throw (IO.userError "native-browser-record@1 requires --structural, a browser-page profile and deployment origin")
  let (resolvedChunk, args) := EsmSplit.takeFlag "--resolved-placement" args
  let checkOnly := args.contains "--check-output"
  let args := args.filter (· != "--check-output")
  let usage := IO.userError "usage: lowersplit <bundle.mjs|bundle.json> spec.json outdir [--require LEVEL] [--resolved-placement CHUNK] [--check-output] [--host-contract PROFILE.json] [--structural] [--deployment-origin ORIGIN [--deployment-preset native-browser-record@1]]; use --diagnostic for plan and preview options"
  let [bundle, spec, out] := args | throw usage
  if args.any (·.startsWith "--") then throw usage
  return (⟨bundle, spec, out⟩, { request, profile, intrinsics, structural, deploymentOrigin, checkOnly, nativeRecord, resolvedChunk })

private def runCertified (paths : SplitPaths) (options : CertifiedOptions) (work : System.FilePath) : IO UInt32 := do
  let { request, profile, intrinsics, structural, deploymentOrigin, checkOnly, nativeRecord, resolvedChunk } := options
  let { bundle := bundlePath, spec := specPath, out } := paths
  let timings := (← IO.getEnv "SPLIT_PROOF_TIMINGS") == some "1"
  let resolved ← resolvedChunk.mapM fun chunk => do
    let j ← readJson specPath
    let digest ← IO.ofExcept (j.getObjValAs? String "source_sha256")
    return (chunk, j, digest)
  let ⟨program, source, input⟩ ← readBundle bundlePath work true timings (resolved.map (·.2.2))
  let asts ← IO.ofExcept (ESTreeUnits.checkedNodes program source)
  if let some reason := asts.findSome? unsupportedStatement then throw (IO.userError reason)
  let units := Effects.footprintUnits asts
  let declarations := (CoreJS.DeclarationInput.decode program).toOption
  let B ← if declarations.isSome then do
      let (fps, _) ← analyzeFootprints asts units {} timings
      pure (bundleOfFootprints asts fps #[])
    else timePhase timings "layout_scan" fun _ => IO.lazyPure fun _ => linkedLayoutBundle asts units
  let S ← match resolved with
    | none => loadSpec specPath
    | some (chunk, j, _) => do
      let some entry := bundlePath.fileName | throw (IO.userError "source is missing its entry filename")
      IO.ofExcept (parseResolvedSpec j chunk entry asts.size)
  for name in S.entry :: S.modules.map Prod.fst do
    let moduleExtension := name.endsWith ".mjs" || (profile == .browserPage && name.endsWith ".js")
    unless CoreJS.ModuleInput.supportedFilename name && moduleExtension do
      throw (IO.userError s!"certified output requires a safe relative .mjs path (.js is supported for browser-page modules), got {name}")
  for (ast, i) in asts.zipIdx do
    if let some reason := entryPlacementProblem B S ast i then throw (IO.userError reason)
  let P ← planSplit B S timings
  let declaration := if structural || deploymentOrigin.isSome then none else declarations.bind fun groups =>
    CoreJS.DeclarationArtifacts.prepare? program (CoreJS.DeclarationProgram.statements groups) B S P
  if let some prepared := declaration then
    let level := prepared.level
    try
      if request == .allOutcomes && level != .allOutcomes then
        throw (IO.userError "the checked reorder proves only normal-completion")
      let checked ← certifyOutput (Json.mkObj []) B P out work input checkOnly timings asts source fun files => do
        timePhase timings "closed_declarations" fun _ => do
          let _ ← IO.ofExcept (prepared.certify files)
          pure (.ok ())
      match checked with
      | .ok _ => return ← emitVerdict request.label (.ok level) "closed-declarations" (profileVerdict profile {})
      | .error refusal => return ← emitRefusal request.label refusal "closed-declarations"
    catch e => return ← emitVerdict request.label (.error e.toString) "closed-declarations"
  let layoutOk ← timePhase timings "plan_checks" fun _ => checkLayout B P (some S) false
  if structural then
    try
      unless request == .normalCompletion do
        throw (IO.userError "the structural schedule certificate proves only normal-completion")
      unless layoutOk do
        throw (IO.userError "source placement or module linkage checks failed")
      let providers ← timePhase timings "provider_loading" fun _ => loadProviders program bundlePath work
      let deployment ← deploymentOrigin.mapM fun origin =>
        timePhase timings "deployment_loading" fun _ => readDeployment program providers bundlePath work origin P nativeRecord
      let checked ← certifyOutput providers B P out work input checkOnly timings asts source fun files => do
        timePhase timings "structural_certificate" fun _ => do
          let result ← IO.lazyPure fun _ =>
            CoreJS.LinkedBehavior.certifyStructuralASTs profile intrinsics program providers files P deployment
          let value ← IO.ofExcept (result.map fun r => (r.1, r.2.1))
          pure (.ok value)
      let (used, proof) ← match checked with
        | .ok value => pure value
        | .error refusal => return ← emitRefusal request.label refusal "linked-structural"
      let scope := match proof with
        | .preservedOrder => "linked-structural-order"
        | .singlePivot => "linked-structural-pivot"
        | .multiplePivots => "linked-structural-moves"
      let assumptions :=
        ["Retained source units use the same capability-closed opaque world in both layouts."] ++
        (if nativeRecord then [s!"{CoreJS.NativeRecord.preset}: Initializing the retained entry's metadata is unobservable except for a fresh null-prototype object with only native writable, enumerable, configurable url and resolve data properties. Its per-record resolver uses the unchanged entry URL and static resolution context."] else []) ++
        used.map (·.assumption) ++ deploymentAssumptions deploymentOrigin
      return ← emitVerdict request.label (.ok .normalCompletion) scope
        (profileFields profile ++ [("assumptions", toJson assumptions)])
    catch e => return ← emitVerdict request.label (.error e.toString) "linked-structural"
  try
    unless layoutOk do throw (IO.userError "source placement or module linkage checks failed")
    let providers ← timePhase timings "provider_loading" fun _ => do
      loadProviders program bundlePath work
    let deployment ← deploymentOrigin.mapM fun origin =>
      timePhase timings "deployment_loading" fun _ => readDeployment program providers bundlePath work origin P
    let checked ← certifyOutput providers B P out work input checkOnly timings asts source fun files => do
      let decoded ← timePhase timings "linked_lowering" fun _ =>
        IO.lazyPure fun _ => CoreJS.LinkedInput.decodePair program providers files P profile deployment
      match decoded with
      | .error reason => pure (.error ({ reason, reasonCode := some "linked_input" } : CertificationRefusal))
      | .ok (original, candidate) => do
        if request != .normalCompletion then
          pure (.error ({ reason := "linked execution proves only normal-completion" } : CertificationRefusal))
        else do
          let prepared : Except CertificationRefusal
              (CoreJS.LinkedBehavior.Certificate × Lean.NameMap Lean.ImportArtifacts) ←
            timePhase timings "certificate_preparation" fun _ => do
            let result ← IO.lazyPure fun _ =>
              CoreJS.LinkedBehavior.certificate profile program providers files P original candidate deployment
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
            let used ← verifyExecution timings work cert dataArts
            pure (.ok used)
    match checked with
    | .ok active => return ← emitVerdict request.label (.ok .normalCompletion) "linked-programs" (profileVerdict profile active (deploymentAssumptions deploymentOrigin))
    | .error refusal => return ← emitRefusal request.label refusal "linked-programs"
  catch e => return ← emitVerdict request.label (.error e.toString) "linked-programs"

def certified (args : List String) : IO UInt32 := do
  let (level?, args) := EsmSplit.takeFlag "--require" args
  let label := level?.getD Guarantee.normalCompletion.label
  try
    let (paths, options) ← parseCertifiedOptions label args
    IO.FS.withTempDir fun work => runCertified paths options work
  catch e => emitVerdict label (.error e.toString)

end EsmSplit.LowerSplit
