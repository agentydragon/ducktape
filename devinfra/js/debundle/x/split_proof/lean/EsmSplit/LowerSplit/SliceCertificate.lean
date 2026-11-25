import EsmSplit.CoreJS.LinkedInput
import EsmSplit.CoreJS.LinkedCertificate.Probe
import EsmSplit.CoreJS.LinkedPivotCertificate
import EsmSplit.CoreJS.BindingMoves
import EsmSplit.CoreJS.HeritageCertificate
import EsmSplit.CoreJS.Split
import EsmSplit.CoreJS.SyntaxMatch
import EsmSplit.ESTreeUnits
import EsmSplit.LowerSplit.Input
import EsmSplit.SplitFiles

/-! Compiled certificates for the CoreJS slice theorems. The input decoder
omits source units with no CoreJS statement. This strategy checks a dense view
with an empty block at each such ordinal, in the source and at its planned
position in each emitted module. -/

namespace EsmSplit.LowerSplit

open Lean EsmSplit.SplitFiles

inductive SliceKind where
  | orderKept
  | moves
  | certifiedBindings
  | heritage
  deriving DecidableEq, Repr

def SliceKind.level : SliceKind → CoreJS.Guarantee
  | .orderKept => .allOutcomes
  | .moves | .certifiedBindings | .heritage => .normalCompletion

def SliceKind.scope : SliceKind → String
  | .orderKept => "linked-slice-order-kept"
  | .moves => "linked-slice-moves"
  | .certifiedBindings => "linked-slice-certified-bindings"
  | .heritage => "linked-slice-heritage"

private def ignoredSourceUnit (ast : Json) : Bool :=
  let typ := Lower.typ ast
  typ == "ImportDeclaration" || typ == "EmptyStatement" || typ == "DebuggerStatement" ||
    (typ == "ExportNamedDeclaration" && Lower.isNull (Lower.fld ast "declaration"))

private def ignoredSourceIdentity (nodes : Array Json) (id : Nat) : Bool :=
  match nodes[id]? with
  | some ast => ignoredSourceUnit ast
  | none => false

/-- Remove only the empty blocks inserted at source identities omitted by the
decoder. -/
private def eraseInserted (nodes : Array Json) (body : List (Nat × CoreJS.Stmt)) :=
  body.filter fun unit => !(ignoredSourceIdentity nodes unit.1)

/-- Add omitted identities while walking the placement and decoded body
in lockstep. This refuses a placement that would reorder decoded units. -/
private def denseBodyAux (nodes : Array Json) :
    (ids : List Nat) → (body : List (Nat × CoreJS.Stmt)) →
    Except String { dense : List (Nat × CoreJS.Stmt) // eraseInserted nodes dense = body }
  | [], [] => .ok ⟨[], by simp [eraseInserted]⟩
  | [], _ :: _ => .error "the decoded body contains identities outside its placement"
  | id :: ids, body =>
    match hIgnored : ignoredSourceIdentity nodes id with
    | true => do
      let rest ← denseBodyAux nodes ids body
      return ⟨(id, .block []) :: rest.val, by
        simpa [eraseInserted, hIgnored] using rest.property⟩
    | false =>
      match body with
      | [] => .error s!"decoded executable source identity {id} has no CoreJS statement"
      | (bodyId, stmt) :: bodyRest =>
        if hId : id = bodyId then do
          let rest ← denseBodyAux nodes ids bodyRest
          return ⟨(id, stmt) :: rest.val, by
            have hIgnoredBody : ignoredSourceIdentity nodes bodyId = false := by
              simpa [hId] using hIgnored
            calc
              eraseInserted nodes ((id, stmt) :: rest.val) =
                  (bodyId, stmt) :: eraseInserted nodes rest.val := by
                simp [eraseInserted, hIgnoredBody, hId]
              _ = (bodyId, stmt) :: bodyRest := by rw [rest.property]⟩
        else .error s!"decoded body order differs at source identity {id}"
termination_by ids body => ids.length

private def denseBody (nodes : Array Json) (ids : List Nat) (body : List (Nat × CoreJS.Stmt)) :
    Except String { dense : List (Nat × CoreJS.Stmt) // eraseInserted nodes dense = body } := do
  unless ids.Nodup do throw "the placement repeats a source identity"
  let bodyIds := body.map Prod.fst
  unless bodyIds.Nodup do throw "the decoded body repeats a source identity"
  unless bodyIds.all ids.contains do throw "the decoded body contains an unplaced source identity"
  bodyIds.forM fun id => do
    unless (ids.filter (· == id)).length == 1 do
      throw s!"the decoded identity {id} is placed more than once"
  denseBodyAux nodes ids body

/-- Per-module evidence that the dense body erases to that decoded module's
body, with its position retained in the evidence list. -/
structure DenseModuleEvidence (nodes : Array Json) where
  decoded : CoreJS.Module
  dense : CoreJS.Module
  nameSame : dense.name = decoded.name
  bodyErases : eraseInserted nodes dense.body = decoded.body

private def denseModuleEvidence (nodes : Array Json) :
    (placements : List EsmSplit.Module) → (decoded : List CoreJS.Module) →
    Except String { evidence : List (DenseModuleEvidence nodes) // evidence.map (·.decoded) = decoded }
  | [], [] => .ok ⟨[], rfl⟩
  | [], _ :: _ => .error "decoded candidate has modules absent from the plan"
  | _ :: _, [] => .error "the plan has modules absent from the decoded candidate"
  | placement :: placements, emitted :: decoded => do
    unless placement.name == emitted.name do throw "decoded candidate module order differs from the plan"
    let body ← denseBody nodes placement.stmts emitted.body
    let rest ← denseModuleEvidence nodes placements decoded
    let current : DenseModuleEvidence nodes := {
      decoded := emitted
      dense := { emitted with body := body.val }
      nameSame := rfl
      bodyErases := by simpa using body.property
    }
    return ⟨current :: rest.val, by simp [current, rest.property]⟩
termination_by placements decoded => placements.length

private def sourceHead (modules : List CoreJS.Module) :
    Except String { module : CoreJS.Module // modules.head? = some module } :=
  match modules with
  | [] => .error "missing original module"
  | module :: _ => .ok ⟨module, rfl⟩

structure DensePair (decodedOriginal decodedCandidate : CoreJS.Program) where
  sourceNodes : Array Json
  original : CoreJS.Program
  candidate : CoreJS.Program
  sourceModule : CoreJS.Module
  sourceShape : original.modules = [sourceModule] ∧ original.entry = sourceModule.name ∧ sourceModule.imports = []
  sourceBodyErases : ∃ decodedModule, decodedOriginal.modules.head? = some decodedModule ∧
    eraseInserted sourceNodes sourceModule.body = decodedModule.body
  candidateModuleEvidence : List (DenseModuleEvidence sourceNodes)
  candidateModulesDecode : candidateModuleEvidence.map (·.decoded) = decodedCandidate.modules
  candidateModulesDense : candidate.modules = candidateModuleEvidence.map (·.dense)

/-- Add the source-identity empty blocks omitted by `decodePair`. This checked
post-processing is deliberately local to the slice strategy until all linked
strategies share dense decoding. -/
def densePair (sourceAst : Json) (P : EsmSplit.Plan) (B : EsmSplit.Bundle)
    (original candidate : CoreJS.Program) : Except String (DensePair original candidate) := do
  let nodes ← (ESTreeUnits.nodes sourceAst).mapError ("source syntax: " ++ ·)
  unless nodes.size == B.length do throw "source syntax and layout bundle have different lengths"
  let providers := original.modules.drop 1
  unless providers.isEmpty do throw "slice strategy requires one original module and no providers"
  let sourceData ← sourceHead original.modules
  let sourceModule := sourceData.val
  unless sourceModule.name != ":corejs:template-objects" do
    throw "slice strategy does not cover a template-object source module"
  let checks : Except String (PLift (original.entry = sourceModule.name ∧ sourceModule.imports = [])) :=
    if hentry : original.entry = sourceModule.name then
      if himports : sourceModule.imports = [] then .ok ⟨⟨hentry, himports⟩⟩
      else .error "slice strategy does not cover source imports"
    else .error "the original entry is not its single source module"
  let hmeta ← checks
  let ⟨hentry, himports⟩ := hmeta.down
  unless candidate.entry == P.entry do throw "decoded candidate entry differs from the plan"
  unless candidate.modules.map (·.name) == P.mods.map (·.name) do
    throw "decoded candidate modules differ from the plan"
  unless candidate.modules.all (·.name != ":corejs:template-objects") do
    throw "slice strategy does not cover a template-object module"
  let denseSourceBody ← denseBody nodes (List.range nodes.size) sourceModule.body
  let denseSourceModule := { sourceModule with body := denseSourceBody.val }
  let moduleEvidence ← denseModuleEvidence nodes P.mods candidate.modules
  let denseModules := moduleEvidence.val.map (·.dense)
  let denseOriginal := { original with modules := [denseSourceModule] }
  let denseCandidate := { candidate with modules := denseModules }
  return {
    sourceNodes := nodes
    original := denseOriginal
    candidate := denseCandidate
    sourceModule := denseSourceModule
    sourceShape := by
      refine ⟨by simp [denseOriginal], ?_, ?_⟩
      simpa [denseOriginal] using hentry
      simpa [denseSourceModule] using himports
    sourceBodyErases := by
      refine ⟨sourceModule, sourceData.property, ?_⟩
      simpa [denseSourceModule] using denseSourceBody.property
    candidateModuleEvidence := moduleEvidence.val
    candidateModulesDecode := moduleEvidence.property
    candidateModulesDense := by simp [denseCandidate, denseModules]
  }

/-! `linkedLayoutBundle` records assignment targets in `Stmt.assigns` for
placement, while the binding commutation proof needs them in the bundle's
`writes` relation too. Keep this conservative enrichment local to the slice
strategy; it does not affect the candidate program or the plan. -/
private def bindingMoveBundle (B : EsmSplit.Bundle) : EsmSplit.Bundle :=
  B.map fun stmt => { stmt with writes := stmt.writes ++ stmt.assigns }

/-- The top-level module name and its export list do not affect execution when
the module has no imports. Its body identities still determine all regions. -/
private theorem sameUnits_refl : ∀ units : List CoreJS.LinkedUnit, CoreJS.SameUnits units units
  | [] => .nil
  | (_, stmt, env) :: rest => .cons ⟨rfl, rfl, CoreJS.Agree.refl stmt.free env⟩ (sameUnits_refl rest)

private theorem program_ext {a b : CoreJS.Program} (hentry : a.entry = b.entry)
    (hmodules : a.modules = b.modules) : a = b := by
  cases a
  cases b
  simp_all

private theorem module_ext {a b : CoreJS.Module} (hname : a.name = b.name)
    (himports : a.imports = b.imports) (hbody : a.body = b.body)
    (hexports : a.exports = b.exports) : a = b := by
  cases a
  cases b
  simp_all

private theorem bindingMoveBundle_exportAt (B : EsmSplit.Bundle) (i : Nat) :
    (((bindingMoveBundle B)[i]?).getD default).exports = ((B[i]?).getD default).exports := by
  cases h : B[i]? with
  | none => simp [bindingMoveBundle, h]
  | some stmt => simp [bindingMoveBundle, h]

private theorem moduleOf_bindingMoveBundle (ss : List CoreJS.Stmt) (B : EsmSplit.Bundle)
    (P : EsmSplit.Plan) (m : EsmSplit.Module) :
    CoreJS.moduleOf ss (bindingMoveBundle B) P m = CoreJS.moduleOf ss B P m := by
  simp [CoreJS.moduleOf, EsmSplit.Plan.namedExports, EsmSplit.Module.namedExports,
    EsmSplit.Module.namedExportsFrom, EsmSplit.Module.publishedFrom, bindingMoveBundle_exportAt]

private theorem splitProgram_bindingMoveBundle (ss : List CoreJS.Stmt) (B : EsmSplit.Bundle)
    (P : EsmSplit.Plan) :
    CoreJS.splitProgram ss (bindingMoveBundle B) P = CoreJS.splitProgram ss B P := by
  apply program_ext
  · rfl
  · simp [CoreJS.splitProgram, moduleOf_bindingMoveBundle]

theorem runProgram_singleModuleMetadata (body : List (Nat × CoreJS.Stmt))
    (hbody : body.Pairwise fun a b => a.1 ≤ b.1) (name : String) (exports : List (String × String))
    (W : CoreJS.World) (fuel : Nat) :
    CoreJS.runProgram W fuel
      ({ entry := name, modules := [{ name, imports := [], body, exports }] } : CoreJS.Program) =
    CoreJS.runProgram W fuel
      ({ entry := "main", modules := [{ name := "main", imports := [], body, exports := [] }] } : CoreJS.Program) := by
  let sourceModule : CoreJS.Module := { name, imports := [], body, exports }
  let targetModule : CoreJS.Module := { name := "main", imports := [], body, exports := [] }
  let source : CoreJS.Program := { entry := name, modules := [sourceModule] }
  let target : CoreJS.Program := { entry := "main", modules := [targetModule] }
  change CoreJS.runProgram W fuel source = CoreJS.runProgram W fuel target
  have sourceLoaded : source.loaded = [sourceModule] := by
    simp [CoreJS.Program.loaded, CoreJS.Program.evalOrder, CoreJS.Program.visit, CoreJS.Program.find, source, sourceModule]
  have targetLoaded : target.loaded = [targetModule] := by
    simp [CoreJS.Program.loaded, CoreJS.Program.evalOrder, CoreJS.Program.visit, CoreJS.Program.find, target, targetModule]
  have hregions : source.regions = target.regions := by
    simp [CoreJS.Program.regions, source, target, sourceModule, targetModule]
  have htops : source.tops = target.tops := by
    simp [CoreJS.Program.tops, CoreJS.Program.loaded, CoreJS.Program.evalOrder, CoreJS.Program.visit,
      CoreJS.Program.find, source, target, sourceModule, targetModule]
  have hscopes : CoreJS.ScopesAgree source target := by
    intro frames _
    let env : CoreJS.Env := { vars := sourceModule.scope frames, this := .undef }
    have envEq : env = { vars := targetModule.scope frames, this := .undef } := by
      simp [env, sourceModule, targetModule, CoreJS.Module.scope]
    refine ⟨[(sourceModule, env)], [(targetModule, env)], ?_, ?_, ?_, ?_⟩
    · rw [sourceLoaded]
      simp [CoreJS.Program.scopes, CoreJS.Program.imported, sourceModule, env] <;> rfl
    · rw [targetLoaded]
      simp [CoreJS.Program.scopes, CoreJS.Program.imported, targetModule, env, envEq] <;> rfl
    · simp only [CoreJS.unitsOf, List.flatMap_cons, List.flatMap_nil, List.append_nil]
      change CoreJS.SameUnits
        (sourceModule.body.map fun (id, stmt) => (id, stmt, env))
        (targetModule.body.map fun (id, stmt) => (id, stmt, env))
      rw [show targetModule.body = sourceModule.body by rfl]
      exact sameUnits_refl _
    · simp only [CoreJS.unitsOf, List.flatMap_cons, List.flatMap_nil, List.append_nil]
      change (sourceModule.body.map fun (id, stmt) => (id, stmt, env)).Pairwise
        fun a b : CoreJS.LinkedUnit => a.1 ≤ b.1
      exact (List.pairwise_map
        (f := fun p : Nat × CoreJS.Stmt => (p.1, p.2, env))
        (R := fun a b : CoreJS.LinkedUnit => a.1 ≤ b.1)).2 hbody
  exact CoreJS.run_same hregions htops hscopes fuel

#print axioms runProgram_singleModuleMetadata

/-! A move strategy supplies this schedule theorem to the slice packager. The
current static relation below is one way to build it. A prefix-sensitive
relation can later check its adjacent swaps at their actual intermediate
prefixes and build the same evidence from its invariant and two-way
`CommuteOn` theorem. That change belongs in the evidence producer; the dense
program bridge, theorem statement, and verdict packaging stay unchanged. -/
private structure MoveScheduleEvidence (ss : List CoreJS.Stmt) (B : EsmSplit.Bundle)
    (S : EsmSplit.Spec) (P : EsmSplit.Plan) where
  normal : CoreJS.LinkedBehavior.ScheduleEquivalent .normalCompletion (fun _ => True)
    (CoreJS.Program.single ss) (CoreJS.splitProgram ss B P)

/-! `MoveRelation` is the schedule-evidence interface consumed by the moves
slice. Its current producer below uses the static `certifiedMayMove` relation.
A prefix-sensitive relation can produce the same evidence from its checked
adjacent-swap trace and prefix invariants, without changing the linked-program
statement or the slice packager. -/
private structure MoveRelation (ss : List CoreJS.Stmt) (B : EsmSplit.Bundle)
    (S : EsmSplit.Spec) (P : EsmSplit.Plan) where
  evidence : MoveScheduleEvidence ss B S P

private structure StaticMoveRelation (ss : List CoreJS.Stmt) where
  allows : Nat → Nat → Bool
  sound : ∀ {W : CoreJS.World}, CoreJS.MovesSound W ss allows

private def certifiedMayMoveRelation (ss : List CoreJS.Stmt) (B : EsmSplit.Bundle)
    (hfp : CoreJS.FootprintsOfSyntax ss B) (hud : EsmSplit.UniqueDefs B) : StaticMoveRelation ss :=
  { allows := CoreJS.certifiedMayMove ss B
    sound := CoreJS.certifiedMayMove_sound hfp hud }

private theorem moveRelationSchedule {ss : List CoreJS.Stmt} {B : EsmSplit.Bundle}
    {S : EsmSplit.Spec} {P : EsmSplit.Plan}
    (hvalid : EsmSplit.Valid B S P) (hfp : CoreJS.FootprintsOfSyntax ss B)
    (relation : StaticMoveRelation ss)
    (hmove : P.execOrder.Pairwise fun a b => a < b ∨ relation.allows a b = true) :
    CoreJS.LinkedBehavior.ScheduleEquivalent .normalCompletion (fun _ => True)
      (CoreJS.Program.single ss) (CoreJS.splitProgram ss B P) := by
  change CoreJS.LinkedBehavior.NormalEquivalent (fun _ => True)
    (CoreJS.Program.single ss) (CoreJS.splitProgram ss B P)
  intro w _ final
  exact CoreJS.moves_preserve_of_sound hvalid hfp relation.sound hmove final

private theorem scheduleWithCertifiedMayMove (ss : List CoreJS.Stmt)
    (B : EsmSplit.Bundle) (S : EsmSplit.Spec) (P : EsmSplit.Plan)
    (hvalid : EsmSplit.Valid B S P) (hfp : CoreJS.FootprintsOfSyntax ss B)
    (hmove : P.execOrder.Pairwise fun a b => a < b ∨ CoreJS.certifiedMayMove ss B a b = true) :
    MoveRelation ss B S P := by
  let relation := certifiedMayMoveRelation ss B hfp hvalid.1
  have relationMove : P.execOrder.Pairwise fun a b => a < b ∨ relation.allows a b = true := by
    apply List.Pairwise.imp (R := fun a b => a < b ∨ CoreJS.certifiedMayMove ss B a b = true)
    · intro a b hab
      rcases hab with hlt | hallowed
      · exact Or.inl hlt
      · exact Or.inr (by simpa [relation, certifiedMayMoveRelation] using hallowed)
    · exact hmove
  exact ⟨⟨moveRelationSchedule hvalid hfp relation relationMove⟩⟩

def SliceStatementFor (profile : CoreJS.HostProfile) (source providers files : Json)
    (P : EsmSplit.Plan) (B : EsmSplit.Bundle) (S : EsmSplit.Spec) (kind : SliceKind) : Prop :=
  ∃ deployment,
    CoreJS.LinkedInput.decodeSingleton source providers files P profile = .ok deployment ∧
    CoreJS.Places deployment.assignment deployment.before deployment.after ∧
    CoreJS.LinkedBehavior.SameExports deployment.before deployment.after ∧
    ∃ dense : DensePair deployment.before deployment.after,
      densePair source P B deployment.before deployment.after = .ok dense ∧
      CoreJS.FootprintsOfSyntax (dense.sourceModule.body.map Prod.snd) B ∧
      EsmSplit.Valid B S P ∧
      CoreJS.LinkedBehavior.ScheduleEquivalent kind.level (fun _ => True) dense.original dense.candidate ∧
      CoreJS.LinkedBehavior.SameExports dense.original dense.candidate

/-- The theorem carried by a compiled slice result. Its existential records
which checked strategy supplies the guarantee; the verdict carries the same
strategy name separately for the non-dependent result API. -/
def SliceStatement (profile : CoreJS.HostProfile) (source providers files : Json)
    (P : EsmSplit.Plan) (B : EsmSplit.Bundle) (S : EsmSplit.Spec) : Prop :=
  ∃ kind : SliceKind, SliceStatementFor profile source providers files P B S kind

/-- The exact singleton deployment checked once before trying linked strategies. -/
structure SliceEligibility (profile : CoreJS.HostProfile) (source providers files : Json)
    (P : EsmSplit.Plan) where
  deployment : CoreJS.LinkedInput.DecodedDeployment
  decodedSingleton : CoreJS.LinkedInput.decodeSingleton source providers files P profile = .ok deployment
  placed : CoreJS.Places deployment.assignment deployment.before deployment.after
  probe : CoreJS.LinkedBehavior.ProbeWitness deployment.before deployment.after
  probeSucceeded : CoreJS.LinkedBehavior.probeWitness profile deployment.before deployment.after = .ok probe
  checkedLinks : Except String (CoreJS.LinkedBehavior.CheckedProbePair deployment.before deployment.after)

def SliceEligibility.replayBlockers {profile source providers files P}
    (input : SliceEligibility profile source providers files P) : List CoreJS.LinkedBehavior.ReplayBlocker :=
  match input.checkedLinks with
  | .ok pair => pair.replayBlockers
  | .error _ => [.other "probe-link-unavailable"]

def SliceEligibility.certifyReplay {profile source providers files P}
    (input : SliceEligibility profile source providers files P) :
    Option (PLift (CoreJS.LinkedBehavior.ASTEquivalent profile input.probe.premises source providers files P)) := do
  let pair ← input.checkedLinks.toOption
  let semantics ← CoreJS.LinkedBehavior.replayPremisedPrograms pair input.probe
  have decoded : CoreJS.LinkedInput.decodePair source providers files P profile =
      .ok (input.deployment.before, input.deployment.after) := by
    simp only [CoreJS.LinkedInput.decodePair, input.decodedSingleton] <;> rfl
  return ⟨⟨input.deployment.before, input.deployment.after, decoded, semantics.down⟩⟩

private def checkSliceExports {original candidate : CoreJS.Program}
    (checked : Except String (CoreJS.LinkedBehavior.CheckedProbePair original candidate)) :
    Except CertificationRefusal (PLift (CoreJS.LinkedBehavior.SameExports original candidate)) := do
  let pair ← checked.mapError fun reason => { reason, reasonCode := some "slice_link" }
  if exportsEqual : CoreJS.LinkedBehavior.exportsMatchFrom pair.before pair.after = true then
    return ⟨pair.sameExports exportsEqual⟩
  else
    let a ← (CoreJS.EntryExports.resolve original.entry pair.before.linked).mapError fun why =>
      ({ reason := "original entry: " ++ why, reasonCode := some "slice_exports" } : CertificationRefusal)
    let b ← (CoreJS.EntryExports.resolve candidate.entry pair.after.linked).mapError fun why =>
      ({ reason := "candidate entry: " ++ why, reasonCode := some "slice_exports" } : CertificationRefusal)
    throw { reason := CoreJS.EntryExports.mismatch a b, reasonCode := some "slice_exports" }

/-- Decide one of the four compiled slice proofs against the exact output ASTs
already checked by the linked path. The returned theorem names both the decoded
sparse programs and the checked dense post-processing used by this strategy. -/
def certifySlice (profile : CoreJS.HostProfile) (source providers files : Json)
    (P : EsmSplit.Plan) (B : EsmSplit.Bundle) (S : EsmSplit.Spec)
    (eligibility : SliceEligibility profile source providers files P) :
    Except CertificationRefusal (SliceKind × PLift (SliceStatement profile source providers files P B S)) := do
  let original := eligibility.deployment.before
  let candidate := eligibility.deployment.after
  match denseDecoded : densePair source P B original candidate with
  | .error reason => throw { reason, reasonCode := some "slice_dense_input" }
  | .ok dense => do
      let denseOriginal := dense.original
      let denseCandidate := dense.candidate
      let sourceModule := dense.sourceModule
      let ss := sourceModule.body.map Prod.snd
      let canonicalSource : CoreJS.Program := .single ss
      let normalizedSource : CoreJS.Program :=
        { entry := "main", modules := [{ name := "main", imports := [], body := sourceModule.body, exports := [] }] }
      let some ⟨sourceSyntax⟩ := CoreJS.SyntaxMatch.programEq normalizedSource canonicalSource |
        throw { reason := "dense source does not match Program.single", reasonCode := some "slice_source_match" }
      let split := CoreJS.splitProgram ss B P
      let candidateSyntax : PLift (denseCandidate = split) ← match CoreJS.SyntaxMatch.programEq denseCandidate split with
        | some proof => pure proof
        | none =>
          let modulesExceptExports := (split.modules.zip denseCandidate.modules).map fun (want, got) =>
            { name := want.name, imports := want.imports, body := want.body, exports := got.exports }
          let sameExceptExports : CoreJS.Program :=
            { entry := split.entry, modules := modulesExceptExports }
          let exportsPerm := (split.modules.zip denseCandidate.modules).all fun (want, got) =>
            decide (got.exports.Perm want.exports)
          if (CoreJS.SyntaxMatch.programEq denseCandidate sameExceptExports).isSome && exportsPerm then
            throw ({ reason := "decoded candidate export order differs from Module.namedExports", reasonCode := some "slice_export_order" } : CertificationRefusal)
          else throw { reason := "dense candidate does not match splitProgram", reasonCode := some "slice_candidate_match" }
      let candidateEq := candidateSyntax.down
      if hfp : CoreJS.FootprintsOfSyntax ss B then
        if hvalid : EsmSplit.Valid B S P then
          let ⟨sameExports⟩ ← checkSliceExports (CoreJS.LinkedBehavior.checkProbePair denseOriginal denseCandidate)
          let ⟨exactExports⟩ ← checkSliceExports eligibility.checkedLinks
          let sourceRun : ∀ w n,
              CoreJS.runProgram w n denseOriginal = CoreJS.runProgram w n canonicalSource := by
            intro w n
            rcases dense.sourceShape with ⟨hmodules, hentry, himports⟩
            let noImports : CoreJS.Program :=
              { entry := sourceModule.name, modules := [{ sourceModule with imports := [] }] }
            have bodyEq : sourceModule.body = (List.range ss.length).zip ss := by
              have h := congrArg (fun p : CoreJS.Program => p.modules.map CoreJS.Module.body) sourceSyntax
              simpa [normalizedSource, canonicalSource, CoreJS.Program.single] using h
            have bodySorted : sourceModule.body.Pairwise fun a b => a.1 ≤ b.1 := by
              rw [bodyEq]
              exact CoreJS.zip_range_sorted ss
            have sourceShape : denseOriginal =
                { entry := sourceModule.name, modules := [sourceModule] } := by
              exact program_ext hentry hmodules
            have noImportsEq : noImports =
                { entry := sourceModule.name, modules := [sourceModule] } := by
              apply program_ext
              · rfl
              · have moduleEq : { sourceModule with imports := [] } = sourceModule := by
                  apply module_ext
                  · rfl
                  · exact himports.symm
                  · rfl
                  · rfl
                simp [noImports, moduleEq]
            calc
              CoreJS.runProgram w n denseOriginal = CoreJS.runProgram w n noImports := by
                  rw [sourceShape, ← noImportsEq]
              _ = CoreJS.runProgram w n normalizedSource := by
                simpa [normalizedSource, himports] using
                  runProgram_singleModuleMetadata sourceModule.body bodySorted sourceModule.name sourceModule.exports w n
              _ = CoreJS.runProgram w n canonicalSource := by rw [sourceSyntax]
          let rec certifyMove (kind : SliceKind)
              (semantics : CoreJS.LinkedBehavior.ScheduleEquivalent kind.level (fun _ => True) canonicalSource split) :
              CoreJS.LinkedBehavior.ScheduleEquivalent kind.level (fun _ => True) denseOriginal denseCandidate := by
            cases kind with
            | orderKept =>
              intro w hw n
              rw [sourceRun w n, candidateEq]
              exact semantics w hw n
            | moves | certifiedBindings | heritage =>
              change CoreJS.LinkedBehavior.NormalEquivalent (fun _ => True) denseOriginal denseCandidate
              intro w hw final
              constructor
              · rintro ⟨n, hrun, hnotFuel⟩
                have hsource : CoreJS.runProgram w n canonicalSource = .ok () final := by
                  rw [← sourceRun w n]
                  exact hrun
                have htarget : CoreJS.Ends w split (.ok () final) :=
                  (semantics w hw final).mp ⟨n, hsource, hnotFuel⟩
                rcases htarget with ⟨m, hrun', hnotFuel'⟩
                exact ⟨m, by rw [candidateEq]; exact hrun', hnotFuel'⟩
              · rintro ⟨n, hrun, hnotFuel⟩
                have htarget : CoreJS.runProgram w n split = .ok () final := by
                  rw [← candidateEq]
                  exact hrun
                have hsource : CoreJS.Ends w canonicalSource (.ok () final) :=
                  (semantics w hw final).mpr ⟨n, htarget, hnotFuel⟩
                rcases hsource with ⟨m, hrun', hnotFuel'⟩
                exact ⟨m, by rw [sourceRun w m]; exact hrun', hnotFuel'⟩
          let package (kind : SliceKind)
              (semantics : CoreJS.LinkedBehavior.ScheduleEquivalent kind.level (fun _ => True) canonicalSource split) :
              SliceKind × PLift (SliceStatement profile source providers files P B S) := by
            have denseSemantics := certifyMove kind semantics
            refine (kind, ⟨kind, eligibility.deployment, eligibility.decodedSingleton, eligibility.placed,
              exactExports, dense, denseDecoded, hfp, hvalid,
              denseSemantics, sameExports⟩)
          let moveBundle := bindingMoveBundle B
          let moveSplit := CoreJS.splitProgram ss moveBundle P
          have hmoveSplit : moveSplit = split := splitProgram_bindingMoveBundle ss B P
          if horder : CoreJS.OrderKept ss P then
            let statement : CoreJS.LinkedBehavior.ScheduleEquivalent .allOutcomes (fun _ => True) canonicalSource split := by
              intro w _ n
              have tops := CoreJS.split_holds_statements (ss := ss) (B := B) (P := P)
                (CoreJS.orderKept_perm horder)
              have scopes := CoreJS.split_scopes_agree hvalid hfp horder
              exact CoreJS.run_same tops.1 tops.2 scopes n
            pure (package .orderKept statement)
          else if hfpMove : CoreJS.FootprintsOfSyntax ss moveBundle then
            if hvalidMove : EsmSplit.Valid moveBundle S P then
              if hmoves : CoreJS.movesOk ss.toArray P.execOrder = true then
                have simpleMoves := (CoreJS.movesOk_iff ss P.execOrder).1 hmoves
                have hmove : P.execOrder.Pairwise
                    fun a b => a < b ∨ CoreJS.certifiedMayMove ss moveBundle a b = true := by
                  apply List.Pairwise.imp (R := fun a b => a < b ∨ CoreJS.MayMove ss a b = true)
                  · intro a b hab
                    rcases hab with hlt | hallowed
                    · exact Or.inl hlt
                    · exact Or.inr (by simp [CoreJS.certifiedMayMove, hallowed])
                  · exact simpleMoves
                let relation := scheduleWithCertifiedMayMove ss moveBundle S P hvalidMove hfpMove hmove
                let statement : CoreJS.LinkedBehavior.ScheduleEquivalent .normalCompletion
                    (fun _ => True) canonicalSource split := by
                  have h := relation.evidence.normal
                  change CoreJS.LinkedBehavior.ScheduleEquivalent .normalCompletion (fun _ => True)
                    (CoreJS.Program.single ss) moveSplit at h
                  rw [hmoveSplit] at h
                  simpa [canonicalSource] using h
                pure (package .moves statement)
              else if hbindings : CoreJS.certifiedMovesOk ss moveBundle P.execOrder = true then
                have hmove : P.execOrder.Pairwise
                    fun a b => a < b ∨ CoreJS.certifiedMayMove ss moveBundle a b = true := by
                  simpa using
                    (CoreJS.certifiedMovesOk_iff ss moveBundle P.execOrder).1 hbindings
                let relation := scheduleWithCertifiedMayMove ss moveBundle S P hvalidMove hfpMove hmove
                let statement : CoreJS.LinkedBehavior.ScheduleEquivalent .normalCompletion
                    (fun _ => True) canonicalSource split := by
                  have h := relation.evidence.normal
                  change CoreJS.LinkedBehavior.ScheduleEquivalent .normalCompletion (fun _ => True)
                    (CoreJS.Program.single ss) moveSplit at h
                  rw [hmoveSplit] at h
                  simpa [canonicalSource] using h
                pure (package .certifiedBindings statement)
              else if hheritage : CoreJS.heritageMovesOk ss moveBundle P.execOrder = true then
                let statement : CoreJS.LinkedBehavior.ScheduleEquivalent .normalCompletion
                    (fun _ => True) canonicalSource split := by
                  change CoreJS.LinkedBehavior.NormalEquivalent (fun _ => True) canonicalSource split
                  intro w _ final
                  have h := CoreJS.moves_preserve_of_heritageMovesOk hvalidMove hfpMove hheritage final
                  change CoreJS.Ends w (CoreJS.Program.single ss) (.ok () final) ↔
                    CoreJS.Ends w moveSplit (.ok () final) at h
                  rw [hmoveSplit] at h
                  exact h
                pure (package .heritage statement)
              else throw { reason := "the plan is outside the compiled slice theorems", reasonCode := some "slice_not_applicable" }
            else throw { reason := "Valid B with binding assignment writes does not hold", reasonCode := some "slice_validity" }
          else throw { reason := "FootprintsOfSyntax ss B with binding assignment writes does not hold", reasonCode := some "slice_footprints" }
        else throw { reason := "Valid B S P does not hold", reasonCode := some "slice_validity" }
      else throw { reason := "FootprintsOfSyntax ss B does not hold", reasonCode := some "slice_footprints" }

theorem certifySlice_sound (profile : CoreJS.HostProfile) (source providers files : Json)
    (P : EsmSplit.Plan) (B : EsmSplit.Bundle) (S : EsmSplit.Spec)
    (result : SliceKind × PLift (SliceStatement profile source providers files P B S)) :
    SliceStatement profile source providers files P B S := result.2.down

#print axioms certifySlice_sound

end EsmSplit.LowerSplit
