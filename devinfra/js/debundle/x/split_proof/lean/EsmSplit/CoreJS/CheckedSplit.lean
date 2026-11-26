import EsmSplit.CoreJS.DeploymentPlacement
import EsmSplit.CoreJS.LinkWorld
import EsmSplit.CoreJS.SingleModule
import EsmSplit.CoreJS.SyntaxMatch
import EsmSplit.Split

/-! Construct a CoreJS split for an order-preserving placement of read-only
declarations. The caller supplies the original program and authored spec. -/

namespace CoreJS.CheckedSplit

open LinkedBehavior

private def simpleValue : Expr → Bool
  | .undef | .null | .bool _ | .num _ | .str _ | .var _ => true
  | _ => false

private def supportedUnit : Stmt → Bool
  | .funDecl (.mk _ .normal false [] [.ret value]) => simpleValue value
  | .decl .const _ (.call (.var _) []) => true
  | .decl .const _ value => simpleValue value
  | _ => false

private def summarize (source : Program) : EsmSplit.Bundle :=
  match source.modules with
  | [m] => m.body.map fun (_, stmt) =>
    let defs := stmt.bindings.map Prod.fst
    { src := "", defs, eager := stmt.free.filter fun name => !defs.contains name,
      exports := (m.exports.filter fun (_, binding) => defs.contains binding).map fun (name, binding) => (name, some binding),
      effect := true }
  | _ => []

private def placementOf (bundle : EsmSplit.Bundle) (spec : EsmSplit.Spec) : List UnitPlacement :=
  (spec.picked bundle).flatMap fun (owner, units) => units.map (·, owner)

/-- Resolve selectors against source units to obtain the requested owners. -/
def requestedPlacement (source : Program) (spec : EsmSplit.Spec) : List UnitPlacement :=
  placementOf (summarize source) spec

def Sound (source : Program) (spec : EsmSplit.Spec) (requested : Guarantee) (output : Program) : Prop :=
  Places (requestedPlacement source spec) source output ∧
  SameExports source output ∧
  ScheduleEquivalent requested (fun _ => True) source output

/-- Executable statements from the single original module, in original order.
Other source shapes are rejected by the constructor. -/
def sourceStatements (source : Program) : List Stmt :=
  match source.modules with
  | [m] => m.body.map Prod.snd
  | _ => []

/-- The layout used to construct the program is also available to an artifact
writer. The guarantee is tied to the CoreJS program. -/
structure Construction (source : Program) (spec : EsmSplit.Spec) (requested : Guarantee) where
  bundle : EsmSplit.Bundle
  plan : EsmSplit.Plan
  program : Program
  sound : Sound source spec requested program
  footprints : FootprintsOfSyntax (sourceStatements source) bundle
  planned : plan = EsmSplit.mkPlan bundle spec
  constructed : program = splitProgram (sourceStatements source) bundle plan

private theorem forRequest {source output : Program}
    (requested : Guarantee) (runs : ∀ w n, runProgram w n source = runProgram w n output) :
    ScheduleEquivalent requested (fun _ => True) source output := by
  cases requested with
  | allOutcomes => exact fun w _ n => runs w n
  | normalCompletion =>
    apply normalEquivalent_of_bounded
    intro w _ n final
    rw [runs w n]

/-- Check the source and plan, then construct the sole candidate. Successful
results carry the requested guarantee for every CoreJS world. -/
def checkedSplit (source : Program) (spec : EsmSplit.Spec) (requested : Guarantee) :
    Except String (Construction source spec requested) := do
  unless !spec.privateEntryBody && (spec.sequential || spec.order.isEmpty) do
    throw "checked split requires no private entry body and an explicit sequential order when an order is supplied"
  EsmSplit.validateModulePaths (spec.modules.map Prod.fst)
  let [m] := source.modules | throw "checked split requires a single source module"
  let stmts := m.body.map Prod.snd
  let body := (List.range stmts.length).zip stmts
  let some ⟨sourceShape⟩ := SyntaxMatch.programEq source
      { entry := m.name, modules := [{ name := m.name, imports := [], body, exports := m.exports }] } |
    throw "source must be an import-free entry with consecutive unit identities starting at zero"
  if spec.sequential then
    unless m.body.all (fun (_, stmt) => match stmt with
        | .decl .const _ value => simpleValue value
        | _ => false) do
      throw "explicit sequential placement supports only simple const declarations"
  for (id, stmt) in m.body do
    unless supportedUnit stmt do
      throw s!"unsupported unit {id}: expected a simple const or a zero-argument function returning a value"
  let bundle := summarize source
  let plan := EsmSplit.mkPlan bundle spec
  if spec.sequential then
    unless plan.evalOrder == spec.order ++ [spec.entry] do
      throw "the constructed modules do not follow the requested order"
  let .isTrue kept := (inferInstance : Decidable (OrderKept stmts plan)) |
    throw "requested placement changes the source unit order"
  let .isTrue valid := (inferInstance : Decidable (EsmSplit.Valid bundle spec plan)) |
    throw "plan failed the placement, linkage or scope checks for the requested spec"
  let .isTrue footprints := (inferInstance : Decidable (FootprintsOfSyntax stmts bundle)) |
    throw "source footprints do not match the planned units"
  let output := splitProgram stmts bundle plan
  let ⟨placed⟩ ← checkPlaces (placementOf bundle spec) source output
  let links ← checkProbePair source output
  if exportsMatch : exportsMatchFrom links.before links.after = true then
    have runs : ∀ w n, runProgram w n source = runProgram w n output := by
      intro w n
      have sourceRun : runProgram w n source = runProgram w n (Program.single stmts) := by
        rw [sourceShape]
        exact runProgram_singleModuleMetadata body (zip_range_sorted stmts) m.name m.exports w n
      exact sourceRun.trans (run_same (split_holds_statements (orderKept_perm kept)).1
        (split_holds_statements (orderKept_perm kept)).2 (split_scopes_agree valid footprints kept) n)
    have originalStatements : sourceStatements source = stmts := by
      rw [sourceShape]
      simp only [sourceStatements, body]
      exact List.map_snd_zip (by simp)
    return ⟨bundle, plan, output,
      ⟨placed, links.sameExports exportsMatch, forRequest requested runs⟩,
      originalStatements ▸ footprints, rfl, by simp [output, originalStatements]⟩
  else
    throw "the split entry does not export the source's public live cells"

theorem checkedSplit_sound {source spec requested} {result : Construction source spec requested}
    (_accepted : checkedSplit source spec requested = .ok result) :
    Sound source spec requested result.program :=
  result.sound

/-- An artifact reader supplies the independently decoded output. Acceptance
requires equality with the constructed program, including its file and cell identities. -/
def checkOutput {source spec requested} (checked : Construction source spec requested)
    (decoded : Program) : Except String (PLift (decoded = checked.program)) :=
  match SyntaxMatch.programEq decoded checked.program with
  | some proof => .ok proof
  | none => .error "decoded output differs from the constructed modules"

theorem checkOutput_sound {source spec requested decoded}
    (checked : Construction source spec requested)
    (same : decoded = checked.program) : Sound source spec requested decoded :=
  same ▸ checked.sound

#print axioms checkedSplit_sound
#print axioms checkOutput_sound

end CoreJS.CheckedSplit
