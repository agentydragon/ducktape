import Lean.Util.ReplaceExpr
import EsmSplit.CoreJS.LinkedCertificate.Probe
import EsmSplit.CoreJS.LinkedCertificate.QuoteJson
import EsmSplit.CoreJS.LinkedCertificate.QuoteRecords
import EsmSplit.CoreJS.LinkedCertificate.QuoteWrites

namespace CoreJS.LinkedBehavior

deriving instance Lean.ToExpr for EsmSplit.Module
deriving instance Lean.ToExpr for EsmSplit.Plan

structure ProofModule where
  name : Lean.Name
  code : String
  timing : String

/-- Modules are elaborated in order; every module except the last is imported by a later step. -/
abbrev ProofRecipe := Array ProofModule

structure Certificate where
  ordinary : ProofRecipe
  checkpoint : Option ProofRecipe
  premises : HostPremises

private def importModule (name : Lean.Name) : String := s!"import {name}\n"

private def layoutDeclaration (visibility program : String) : String :=
  s!"{visibility}noncomputable def {program}Link (w : World) : Option (LinkedBehavior.CheckedLink {program} w) := by\n" ++
  "  check_linked_layout\n"

/-- Three separately compiled modules check the shared result state before the
ordinary AST gate imports their execution theorem. -/
private def checkpointRecipe (preamble options declarations correspondence : String) (fuel : Nat)
    (stageLink ordered : Bool) (move : Option (Nat × Nat)) : ProofRecipe :=
  let inputName := `LinkedReplayInput
  let sourceName := `LinkedReplaySource
  let proofName := `LinkedReplayProof
  let gateName := `LinkedReplayGate
  let sourceLayout := if stageLink then layoutDeclaration "" "source" else ""
  let candidateLayout := if stageLink then layoutDeclaration "private " "candidate" else ""
  let reduceSource := if stageLink then s!"    reduce_linked_run_from {fuel} sourceLink w\n"
    else "    (conv =>\n      lhs\n      cbv) <;> rfl\n"
  let replay := if stageLink then s!"reduce_linked_run_from {fuel} candidateLink w\n"
    else "(conv =>\n    lhs\n    cbv) <;> rfl\n"
  let candidateProof := if ordered then
      "  first\n" ++
      s!"  | have same : runProgram w {fuel} source = runProgram w {fuel} candidate := by\n" ++
      s!"      check_ordered_linked_runs {fuel} sourceLink candidateLink w\n" ++
      "    exact same.symm.trans (sourceWitness w).property\n" ++
      "  | " ++ replay
    else if let some (start, width) := move then
      "  first\n" ++
      s!"  | apply LinkedBehavior.run_ok_of_moved_link (start := {start}) (width := {width}) " ++
      "((sourceLink w).get (by rfl)) ((candidateLink w).get (by rfl)) _ _ (sourceWitness w).property\n" ++
      "    · rfl\n    · cbv <;> rfl\n" ++
      "  | " ++ replay
    else "  " ++ replay
  let checkExports := if stageLink then "  · verify_linked_exports_from sourceLink candidateLink\n"
    else "  · apply LinkedBehavior.sameExports_of_match\n    intro w\n    first | rfl | cbv <;> rfl\n"
  let input := preamble ++ "namespace LinkedReplay\n" ++ declarations ++ "end LinkedReplay\n"
  let source := importModule inputName ++ "import Lean.Elab.Tactic.Conv\n" ++ options ++
    "namespace LinkedReplay\n" ++
    sourceLayout ++
    "noncomputable def sourceWitness (w : World) :\n" ++
    "    { t : State w // " ++ s!"runProgram w {fuel} source = .ok () t" ++ " } := by\n" ++
    "  refine ⟨?state, ?run⟩\n" ++
    "  case run =>\n" ++
    reduceSource ++
    "end LinkedReplay\n"
  let execution := importModule sourceName ++ options ++
    "namespace LinkedReplay\n" ++
    candidateLayout ++
    "private theorem candidateRun (w : World) :\n" ++
    s!"    runProgram w {fuel} candidate = .ok () (sourceWitness w).val := by\n" ++
    candidateProof ++
    "theorem executions : LinkedBehavior.EquivalentFor {} source candidate ∧\n" ++
    "    LinkedBehavior.SameExports source candidate := by\n" ++
    "  constructor\n" ++
    s!"  · apply LinkedBehavior.of_runs {fuel}\n" ++
    "    intro w\n" ++
    "    exact ⟨_, (sourceWitness w).property, candidateRun w⟩\n" ++
    checkExports ++
    "#audit_linked_programs executions\n" ++
    "end LinkedReplay\n"
  let gate := importModule proofName ++ options ++ "open LinkedReplay\n" ++ correspondence
  #[
    { name := inputName, code := input, timing := inputName.toString },
    { name := sourceName, code := source, timing := sourceName.toString },
    { name := proofName, code := execution, timing := proofName.toString },
    { name := gateName, code := gate, timing := "ast_correspondence" }
  ]

-- Estimate the size of expanded constructor syntax independently of .olean sharing.
private partial def expandedBytes (expr : Lean.Expr) : Nat :=
  match expr with
  | .app .. => expr.getAppArgs.foldl (fun size arg => size + 1 + expandedBytes arg) (2 + expandedBytes expr.getAppFn)
  | .const name _ => name.toString.utf8ByteSize + 1
  | .lit (.strVal s) => s.quote.utf8ByteSize
  | .lit (.natVal n) => (toString n).utf8ByteSize
  | .fvar id => id.name.toString.utf8ByteSize
  | _ => 0

private def certificateText (profile : HostProfile) (sourceAST providers files : Lean.Json) (plan : EsmSplit.Plan)
    (witness? : Option (HostPremises × Nat)) (source candidate : Program) :
    EsmSplit.CertificateData.Quotes Refusal Certificate := do
  let records := RecordQuotes.collect source candidate
  let sourceTerm := Lean.toExpr source
  let candidateTerm := Lean.toExpr candidate
  let (premises, fuel) ← match witness? with
    | some witness => pure witness
    | none => do
      let probe ← liftM (probeWitness profile source candidate)
      pure (probe.premises, probe.fuel)

  -- An initial reflexivity attempt can expand the parameter prelude before the
  -- reflection tactic uses the evaluator's equations.
  let hasDefaults := fun (e : Lean.Expr) => (e.find? fun t => t.isConstOf ``Func.withDefaults).isSome
  let strategy := if hasDefaults sourceTerm || hasDefaults candidateTerm then " cbv_first" else ""
  let writes := WriteBatches.collect sourceTerm candidateTerm
  let replace := fun e =>
    if e.isAppOfArity ``Prod.mk 4 then records.replacements[e]?
    else if e.isAppOfArity ``Expr.seq 2 then writes.replacements[e]?
    else none
  let sourceData := sourceTerm.replace replace
  let candidateData := candidateTerm.replace replace
  let s ← EsmSplit.CertificateData.quote "source" sourceData
  let c ← EsmSplit.CertificateData.quote "candidate" candidateData
  let checkpointed := premises = {} && writes.isEmpty && expandedBytes sourceData + expandedBytes candidateData ≥ 40 * 1024
  let move := if checkpointed then earlierMove? source candidate else none
  let recordTerms ← records.definitions sourceTerm candidateTerm move.isSome
  let writeTerms ← writes.definitions
  let p ← EsmSplit.CertificateData.quote "plan" (Lean.toExpr plan)
  let (asts, astData) := jsonDefinitions [("sourceAST", sourceAST), ("providers", providers), ("files", files)]
  modify (· ++ astData)
  -- The generated declarations are short. Estimate the compiler's workload from
  -- the quoted terms when deciding whether to run correspondence separately.
  let stagedGate := astData.foldl (fun bytes (_, term) => bytes + expandedBytes term) 0 ≥ 512 * 1024
  let writeImports := if writes.isEmpty then "" else "import EsmSplit.Fast\n"
  let profileTerm := fun (p : HostProfile) => match p with
    | .empty => "CoreJS.HostProfile.empty"
    | .browserPage => "CoreJS.HostProfile.browserPage"
    | .ecmascript => "CoreJS.HostProfile.ecmascript"
    | .nodeRecording => "CoreJS.HostProfile.nodeRecording"
  let ordinaryTactic := s!"verify_linked_programs {fuel}{strategy}"
  -- This only selects the proof attempt. The theorem checks the complete units,
  -- free-name cells, hoisted state and entry exports from the actual links.
  let order := fun (p : Program) => p.loaded.flatMap fun m => m.body.map Prod.fst
  let ordered := order source == order candidate
  -- Larger module graphs can spend most of one reduction budget on linking.
  let stageLink := ordered || (source.modules.map (·.body.length)).sum ≥ 32
  let (layouts, tactic) := if stageLink then
    (layoutDeclaration "private " "source" ++ layoutDeclaration "private " "candidate",
     "  first\n" ++
     (if ordered then s!"  | verify_ordered_linked_programs {fuel} sourceLink candidateLink premises\n" else "") ++
     s!"  | verify_linked_programs_from {fuel} sourceLink candidateLink premises\n  | {ordinaryTactic}\n")
    else ("", s!"  {ordinaryTactic}\n")
  let options := "\nopen CoreJS\n" ++
    "set_option maxRecDepth 16000\nset_option maxHeartbeats 2000000\nset_option cbv.warning false\n" ++
    "\n"
  let preamble := "import EsmSplit.CertificateData\nimport EsmSplit.CoreJS.LinkedCertificate.Proof\n" ++
    s!"import {EsmSplit.CertificateData.moduleName}\n" ++ writeImports ++ options
  let data := recordTerms ++ writeTerms
  let raw := String.join asts.toList ++ "private def plan : EsmSplit.Plan := " ++ p ++ "\n"
  let programs := s!"def source : Program := {s}\ndef candidate : Program := {c}\n"
  let profiles := fun (visibility : String) =>
    s!"{visibility}def profile := {profileTerm profile}\n" ++
    s!"{visibility}def premises : HostPremises := ⟨{reprStr premises.typeofBindings}, {reprStr premises.refs}⟩\n"
  let execution := "namespace LinkedExecutionProof\n" ++ data ++ programs ++ profiles "" ++ layouts ++
    "theorem executions : LinkedBehavior.EquivalentFor premises source candidate ∧ LinkedBehavior.SameExports source candidate := by\n" ++
    tactic ++ "end LinkedExecutionProof\n"
  let gate :=
    "private def verified : Except String (PLift (LinkedBehavior.ASTEquivalent profile premises sourceAST providers files plan)) :=\n" ++
    "  LinkedBehavior.certifyASTs profile premises sourceAST providers files plan source candidate executions\n" ++
    "#audit_linked_programs verified\n" ++
    "#eval IO.ofExcept (verified.map fun _ => ())\n"

  let correspondence := "open LinkedExecutionProof\n" ++ raw ++ gate
  -- The measured 384-field and 705-field cases benefit from separate processes.
  -- Smaller proofs avoid their startup cost. Write batches use file-local CBV rules.
  let checkpoint := if checkpointed then
      some (checkpointRecipe preamble options (data ++ programs) (raw ++ profiles "private " ++ gate) fuel
        (stageLink || records.hasWideRecord || move.isSome) ordered move)
    else none
  let executionName := `LinkedExecutionProof
  let correspondenceName := `LinkedExecution
  let ordinary := if stagedGate then
      #[
        { name := executionName, code := preamble ++ execution, timing := "execution_proof" },
        { name := correspondenceName,
          code := importModule executionName ++ preamble ++ correspondence,
          timing := "ast_correspondence" }
      ]
    else
      #[{ name := correspondenceName,
          code := preamble ++ execution ++ correspondence,
          timing := "execution_and_correspondence" }]
  return { ordinary, checkpoint, premises }

/-- Both proof layouts check the same all-world execution claim and exact ASTs. -/
def certificate (profile : HostProfile) (sourceAST providers files : Lean.Json) (plan : EsmSplit.Plan)
    (source candidate : Program) : Except Refusal (Certificate × EsmSplit.CertificateData.Entries) :=
  (certificateText profile sourceAST providers files plan none source candidate).run #[]

/-- Prepare the ordinary proof using the successful concrete witness already
checked by the caller on this exact linked pair. -/
def certificateWithWitness (profile : HostProfile) (sourceAST providers files : Lean.Json) (plan : EsmSplit.Plan)
    {source candidate : Program} (probe : ProbeWitness source candidate) :
    Except Refusal (Certificate × EsmSplit.CertificateData.Entries) :=
  (certificateText profile sourceAST providers files plan (some (probe.premises, probe.fuel)) source candidate).run #[]

end CoreJS.LinkedBehavior
