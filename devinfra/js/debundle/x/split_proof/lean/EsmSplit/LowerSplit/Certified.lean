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

private inductive Guarantee where
  | allOutcomes | normalCompletion
  deriving DecidableEq

private def Guarantee.label : Guarantee → String
  | .allOutcomes => "all-outcomes"
  | .normalCompletion => "normal-completion"

/-- Premises for the closed-declaration artifact theorems. The caller checks Valid
and certifies the output ASTs against these same extracted statements. -/
private def declarationProof? (groups : List CoreJS.DeclarationGroup) (B : Bundle) (P : Plan) :
    Option (Guarantee × List CoreJS.Stmt) := do
  let ss := CoreJS.DeclarationProgram.statements groups
  guard (footprintsOk ss B)
  if decide (CoreJS.OrderKept ss P) then return (.allOutcomes, ss)
  guard (CoreJS.movesOk ss.toArray P.execOrder || CoreJS.certifiedMovesOk ss B P.execOrder)
  return (.normalCompletion, ss)

/-- Check the generated proof with the same toolchain and model as the splitter. -/
private def checkLean (work : System.FilePath) (args : Array String) : IO Unit := do
  let exe ← IO.appPath
  let args := #["-DwarningAsError=true", s!"--root={work}"] ++ args
  let proc ← if exe.fileName == some "lean" then
      IO.Process.output { cmd := exe.toString, args }
    else do
      -- The native Lake executable is <project>/.lake/build/bin/lowersplit.
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

private def checkCheckpoint (timings : Bool) (work : System.FilePath) (text : CoreJS.LinkedBehavior.CheckpointText)
    (dataArts : Lean.NameMap Lean.ImportArtifacts) : IO Unit := do
  let stages := #[(`LinkedReplayInput, text.input), (`LinkedReplaySource, text.source), (`LinkedReplayProof, text.execution)]
  let mut importArts := dataArts
  for (name, code) in stages do
    let compiled := work / (name.toString ++ ".olean")
    timePhase timings name.toString fun _ => do
      checkProofModule work { name, importArts } code #["-o", compiled.toString]
    importArts := importArts.insert name (.ofArrays #[#[compiled]])
  timePhase timings "ast_correspondence" fun _ => do
    checkProofModule work { name := `LinkedReplayGate, importArts } text.gate #[]

/-- The ordinary execution proof can also be imported into a separate AST gate. -/
private def checkOrdinaryExecution (timings : Bool) (work : System.FilePath)
    (cert : CoreJS.LinkedBehavior.Certificate) (dataArts : Lean.NameMap Lean.ImportArtifacts) : IO Unit := do
  if !cert.stagedGate then
    timePhase timings "execution_and_correspondence" fun _ => do
      checkProofModule work { name := `LinkedExecution, importArts := dataArts }
        (cert.preamble ++ cert.execution ++ cert.correspondence) #[]
  else
    let name := `LinkedExecutionProof
    let compiled := work / "LinkedExecutionProof.olean"
    timePhase timings "execution_proof" fun _ => do
      checkProofModule work { name, importArts := dataArts } (cert.preamble ++ cert.execution) #["-o", compiled.toString]
    let importArts := dataArts.insert name (.ofArrays #[#[compiled]])
    timePhase timings "ast_correspondence" fun _ => do
      checkProofModule work { name := `LinkedExecution, importArts }
        ("import LinkedExecutionProof\n" ++ cert.preamble ++ cert.correspondence) #[]

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
    | some checkpoint => try
        checkCheckpoint timings work checkpoint dataArts
        pure true
      catch _ => pure false
  unless accepted do checkOrdinaryExecution timings work cert dataArts
  return cert.premises

private def emitVerdict (requested : String) (outcome : Except String Guarantee) (scope : Option String := none) (extra : List (String × Json) := []) : IO UInt32 := do
  let fields := match outcome with
    | .error why => [("status", toJson "rejected"), ("guarantee", Json.null), ("reason", toJson why)]
    | .ok level => [("status", toJson "certified"), ("guarantee", toJson level.label)]
  IO.println (Json.mkObj ([("requested", toJson requested), ("scope", toJson scope)] ++ fields ++ extra)).compress
  return if outcome.isOk then 0 else 1

private def profileVerdict (selected : CoreJS.HostProfile) (used : CoreJS.HostPremises) : List (String × Json) :=
  match selected with
  | .empty => []
  | .browserPage => [("hostProfile", toJson "browser-page"), ("assumptions", toJson used.assumptions)]

private structure CertifiedOptions where
  request : Guarantee
  profile : CoreJS.HostProfile
  structural : Bool
  deploymentOrigin : Option String
  checkOnly : Bool

private def parseCertifiedOptions (label : String) (args : List String) : IO (SplitPaths × CertifiedOptions) := do
  let request ← match label with
    | "all-outcomes" => pure Guarantee.allOutcomes
    | "normal-completion" => pure Guarantee.normalCompletion
    | _ => throw (IO.userError "--require expects all-outcomes or normal-completion")
  let (contract?, args) := Effects.takeFlag "--host-contract" args
  let profile ← match contract? with
    | none => pure CoreJS.HostProfile.empty
    | some path => do
      let j ← IO.ofExcept (Json.parse (← IO.FS.readFile path))
      IO.ofExcept (CoreJS.HostProfile.read j)
  let structural := args.contains "--structural"
  let args := args.filter (· != "--structural")
  let (deploymentOrigin, args) := Effects.takeFlag "--deployment-origin" args
  if deploymentOrigin.isSome && !structural then
    throw (IO.userError "--deployment-origin requires --structural")
  let checkOnly := args.contains "--check-output"
  let args := args.filter (· != "--check-output")
  let usage := IO.userError "usage: lowersplit <bundle.mjs|bundle.json> spec.json outdir [--require LEVEL] [--check-output] [--host-contract PROFILE.json] [--structural [--deployment-origin ORIGIN]]; use --diagnostic for plan and preview options"
  let [bundle, spec, out] := args | throw usage
  if args.any (·.startsWith "--") then throw usage
  return (⟨bundle, spec, out⟩, { request, profile, structural, deploymentOrigin, checkOnly })

private def runCertified (paths : SplitPaths) (options : CertifiedOptions) (work : System.FilePath) : IO UInt32 := do
  let { request, profile, structural, deploymentOrigin, checkOnly } := options
  let { bundle := bundlePath, spec := specPath, out } := paths
  let timings := (← IO.getEnv "SPLIT_PROOF_TIMINGS") == some "1"
  let ⟨program, source, input⟩ ← readBundle bundlePath work true timings
  let asts ← IO.ofExcept (ESTreeUnits.checkedNodes program source)
  if let some reason := asts.findSome? unsupportedStatement then throw (IO.userError reason)
  let units := Effects.footprintUnits asts
  let declarations := (CoreJS.DeclarationInput.decode program).toOption
  let B ← if declarations.isSome then do
      let (fps, _) ← analyzeFootprints asts units {} timings
      pure (bundleOfFootprints asts fps #[])
    else timePhase timings "layout_scan" fun _ => IO.lazyPure fun _ => linkedLayoutBundle asts units
  let S ← loadSpec specPath
  for name in S.entry :: S.modules.map Prod.fst do
    let moduleExtension := name.endsWith ".mjs" || (profile == .browserPage && name.endsWith ".js")
    unless CoreJS.ModuleInput.supportedFilename name && moduleExtension do
      throw (IO.userError s!"certified output requires a safe relative .mjs path (.js is supported for browser-page modules), got {name}")
  for (ast, i) in asts.zipIdx do
    if let some reason := entryPlacementProblem B S ast i then throw (IO.userError reason)
  let P ← planSplit B S timings
  let layoutOk ← timePhase timings "plan_checks" fun _ => checkLayout B P (some S) false
  -- Only the closed-declaration theorem needs the footprint-based order check.
  let declaration := if !structural && layoutOk then declarations.bind (declarationProof? · B P) else none
  if let some (level, ss) := declaration.filter (fun _ => decide (OrderOk B P)) then
    try
      if request == .allOutcomes && level != .allOutcomes then
        throw (IO.userError "the checked reorder proves only normal-completion")
      certifyOutput (Json.mkObj []) B P out work input checkOnly timings asts source fun files =>
        timePhase timings "closed_declarations" fun _ => do
          IO.ofExcept (CoreJS.DeclarationArtifacts.check program files ss B P)
      return ← emitVerdict request.label (.ok level) "closed-declarations" (profileVerdict profile {})
    catch e => return ← emitVerdict request.label (.error e.toString) "closed-declarations"
  if structural then
    try
      unless request == .normalCompletion do
        throw (IO.userError "the structural schedule certificate proves only normal-completion")
      unless layoutOk do
        throw (IO.userError "source placement or module linkage checks failed")
      let providers ← timePhase timings "provider_loading" fun _ => loadProviders program bundlePath work
      let deployment ← deploymentOrigin.mapM fun origin =>
        timePhase timings "deployment_loading" fun _ => readDeployment program providers bundlePath work origin P
      let proof ← certifyOutput providers B P out work input checkOnly timings asts source fun files =>
        timePhase timings "structural_certificate" fun _ => do
          let result ← IO.lazyPure fun _ =>
            CoreJS.LinkedBehavior.certifyStructuralASTs profile program providers files P deployment
          IO.ofExcept (result.map Prod.fst)
      let scope := match proof with
        | .preservedOrder => "linked-structural-order"
        | .singlePivot => "linked-structural-pivot"
        | .multiplePivots => "linked-structural-moves"
      let assumptions :=
        ["Retained source units use the same capability-closed opaque world in both layouts."] ++
        (deploymentOrigin.toList.flatMap fun origin => [
          s!"The static deployment at {origin} overlays only the planned module paths.",
          "The native ESM loader uses one module record per effective URL and resolves dynamic dependencies through the checked live cells."
        ])
      let host := if profile == .browserPage then [("hostProfile", toJson "browser-page")] else []
      return ← emitVerdict request.label (.ok .normalCompletion) scope (host ++ [("assumptions", toJson assumptions)])
    catch e => return ← emitVerdict request.label (.error e.toString) "linked-structural"
  let errorFields ← IO.mkRef ([] : List (String × Json))
  try
    unless layoutOk do throw (IO.userError "source placement or module linkage checks failed")
    let providers ← timePhase timings "provider_loading" fun _ => do
      loadProviders program bundlePath work
    let used ← IO.mkRef ({} : CoreJS.HostPremises)
    certifyOutput providers B P out work input checkOnly timings asts source fun files => do
      let decoded ← timePhase timings "linked_lowering" fun _ =>
        IO.lazyPure fun _ => CoreJS.LinkedInput.decodePair program providers files P profile
      let (original, candidate) ← match decoded with
        | .ok pair => pure pair
        | .error reason =>
          errorFields.set [("reason_code", toJson "linked_input")]
          throw (IO.userError reason)
      unless request == .normalCompletion do
        throw (IO.userError "linked execution proves only normal-completion")
      let prepared ← timePhase timings "certificate_preparation" fun _ => do
        let result ← IO.lazyPure fun _ =>
          CoreJS.LinkedBehavior.certificate profile program providers files P original candidate
        match result with
        | .error refusal => pure (Except.error refusal)
        | .ok (cert, terms) =>
          try
            return .ok (cert, ← writeCertificateData work terms)
          catch err =>
            return .error { code := .proofPreparation, reason := err.toString }
      let (cert, dataArts) ← match prepared with
        | .ok result => pure result
        | .error refusal =>
          let detail := (refusal.observation.map fun value => ("observation", value)).toList
          errorFields.set (("reason_code", toJson refusal.code.label) :: detail)
          throw (IO.userError refusal.reason)
      used.set (← verifyExecution timings work cert dataArts)
    let active ← used.get
    return ← emitVerdict request.label (.ok .normalCompletion) "linked-programs" (profileVerdict profile active)
  catch e => return ← emitVerdict request.label (.error e.toString) "linked-programs" (← errorFields.get)

def certified (args : List String) : IO UInt32 := do
  let (level?, args) := Effects.takeFlag "--require" args
  let label := level?.getD Guarantee.normalCompletion.label
  try
    let (paths, options) ← parseCertifiedOptions label args
    IO.FS.withTempDir fun work => runCertified paths options work
  catch e => emitVerdict label (.error e.toString)

end EsmSplit.LowerSplit
