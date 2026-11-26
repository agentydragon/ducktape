import EsmSplit.CoreJS.LinkedInput
import EsmSplit.CoreJS.LinkedCertificate.Probe
import EsmSplit.CoreJS.LinkedPivotCertificate
import EsmSplit.CoreJS.BindingMoves
import EsmSplit.CoreJS.HeritageCertificate
import EsmSplit.CoreJS.SingleModule
import EsmSplit.CoreJS.SyntaxMatch
import EsmSplit.LowerSplit.Input
import EsmSplit.SplitFiles

/-! Compiled certificates for the CoreJS slice theorems on the exact decoded
programs. The slice model requires a CoreJS statement at each source ordinal. -/

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

/-! `linkedLayoutBundle` records assignment targets in `Stmt.assigns` for
placement, while the binding commutation proof needs them in the bundle's
`writes` relation too. Keep this conservative enrichment local to the slice
strategy; it does not affect the candidate program or the plan. -/
private def bindingMoveBundle (B : EsmSplit.Bundle) : EsmSplit.Bundle :=
  B.map fun stmt => { stmt with writes := stmt.writes ++ stmt.assigns }

private theorem program_ext {a b : CoreJS.Program} (hentry : a.entry = b.entry)
    (hmodules : a.modules = b.modules) : a = b := by
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

/-! A move strategy supplies this schedule theorem to the slice packager. The
current static relation below is one way to build it. A prefix-sensitive
relation can later check its adjacent swaps at their actual intermediate
prefixes and build the same evidence from its invariant and two-way
`CommuteOn` theorem. That change belongs in the evidence producer. -/
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
  profile = .empty ∧
  ∃ deployment,
    CoreJS.LinkedInput.decodeSingleton source providers files P profile = .ok deployment ∧
    CoreJS.Places deployment.assignment deployment.before deployment.after ∧
    CoreJS.LinkedBehavior.SameExports deployment.before deployment.after ∧
    CoreJS.LinkedBehavior.ScheduleEquivalent kind.level (fun _ => True) deployment.before deployment.after ∧
    EsmSplit.Valid B S P

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

/-- Check a slice against the exact decoded singleton deployment. The empty
profile ensures the decode did not use a deployment host description. The
source body and candidate must already match the slice model. -/
def certifySlice (profile : CoreJS.HostProfile) (source providers files : Json)
    (P : EsmSplit.Plan) (B : EsmSplit.Bundle) (S : EsmSplit.Spec)
    (eligibility : SliceEligibility profile source providers files P) :
    Except CertificationRefusal (Σ kind, PLift (SliceStatementFor profile source providers files P B S kind)) := do
  let original := eligibility.deployment.before
  let candidate := eligibility.deployment.after
  let .isTrue emptyProfile := decEq profile .empty |
    throw { reason := "compiled slice requires the empty lowering profile", reasonCode := some "slice_profile" }
  if candidate.modules.any (·.name == ":corejs:template-objects") then
    throw { reason := "slice strategy does not cover a template-object module", reasonCode := some "slice_candidate_match" }
  match sourceModules : original.modules with
  | [sourceModule] => do
      let .isTrue sourceEntry := decEq original.entry sourceModule.name |
        throw { reason := "the original entry is not its only module", reasonCode := some "slice_source_match" }
      if sourceModule.name == ":corejs:template-objects" then
        throw { reason := "slice strategy does not cover a template-object source module", reasonCode := some "slice_source_match" }
      let .isTrue sourceImports := decEq sourceModule.imports [] |
        throw { reason := "slice strategy requires an original module without imports", reasonCode := some "slice_source_match" }
      let sourceSyntax : original = { entry := sourceModule.name, modules := [sourceModule] } :=
        program_ext sourceEntry sourceModules
      let ss := sourceModule.body.map Prod.snd
      let canonicalSource : CoreJS.Program := .single ss
      let normalizedSource : CoreJS.Program :=
        { entry := "main", modules := [{ name := "main", imports := [], body := sourceModule.body, exports := [] }] }
      let some ⟨bodySyntax⟩ := CoreJS.SyntaxMatch.programEq normalizedSource canonicalSource |
        throw { reason := "decoded source does not match Program.single", reasonCode := some "slice_source_match" }
      let split := CoreJS.splitProgram ss B P
      let candidateSyntax : PLift (candidate = split) ← match CoreJS.SyntaxMatch.programEq candidate split with
        | some proof => pure proof
        | none =>
          let modulesExceptExports := (split.modules.zip candidate.modules).map fun (want, got) =>
            { name := want.name, imports := want.imports, body := want.body, exports := got.exports }
          let sameExceptExports : CoreJS.Program :=
            { entry := split.entry, modules := modulesExceptExports }
          let exportsPerm := (split.modules.zip candidate.modules).all fun (want, got) =>
            decide (got.exports.Perm want.exports)
          if (CoreJS.SyntaxMatch.programEq candidate sameExceptExports).isSome && exportsPerm then
            throw ({ reason := "decoded candidate export order differs from Module.namedExports", reasonCode := some "slice_export_order" } : CertificationRefusal)
          else throw { reason := "decoded candidate does not match splitProgram", reasonCode := some "slice_candidate_match" }
      let candidateEq := candidateSyntax.down
      if hfp : CoreJS.FootprintsOfSyntax ss B then
        if hvalid : EsmSplit.Valid B S P then
          let ⟨exactExports⟩ ← checkSliceExports eligibility.checkedLinks
          let sourceRun : ∀ w n,
              CoreJS.runProgram w n original = CoreJS.runProgram w n canonicalSource := by
            intro w n
            have bodyEq : sourceModule.body = (List.range ss.length).zip ss := by
              have h := congrArg (fun p : CoreJS.Program => p.modules.map CoreJS.Module.body) bodySyntax
              simpa [normalizedSource, canonicalSource, CoreJS.Program.single] using h
            have bodySorted : sourceModule.body.Pairwise fun a b => a.1 ≤ b.1 := by
              rw [bodyEq]
              exact CoreJS.zip_range_sorted ss
            rw [sourceSyntax, ← bodySyntax]
            cases sourceModule
            simp only at sourceImports
            simpa only [sourceImports, normalizedSource] using
              CoreJS.runProgram_singleModuleMetadata _ bodySorted _ _ w n
          let rec certifyMove (kind : SliceKind)
              (semantics : CoreJS.LinkedBehavior.ScheduleEquivalent kind.level (fun _ => True) canonicalSource split) :
              CoreJS.LinkedBehavior.ScheduleEquivalent kind.level (fun _ => True) original candidate := by
            cases kind with
            | orderKept =>
              intro w hw n
              rw [sourceRun w n, candidateEq]
              exact semantics w hw n
            | moves | certifiedBindings | heritage =>
              change CoreJS.LinkedBehavior.NormalEquivalent (fun _ => True) original candidate
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
              Σ kind, PLift (SliceStatementFor profile source providers files P B S kind) := by
            exact ⟨kind, ⟨emptyProfile, eligibility.deployment, eligibility.decodedSingleton,
              eligibility.placed, exactExports, certifyMove kind semantics, hvalid⟩⟩
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

  | _ => throw { reason := "slice strategy requires one original module without providers", reasonCode := some "slice_source_match" }

theorem certifySlice_sound (profile : CoreJS.HostProfile) (source providers files : Json)
    (P : EsmSplit.Plan) (B : EsmSplit.Bundle) (S : EsmSplit.Spec)
    (result : Σ kind, PLift (SliceStatementFor profile source providers files P B S kind)) :
    SliceStatementFor profile source providers files P B S result.1 := result.2.down

#print axioms certifySlice_sound

end EsmSplit.LowerSplit
