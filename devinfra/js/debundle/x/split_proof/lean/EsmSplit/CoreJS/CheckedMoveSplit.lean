import EsmSplit.CoreJS.CheckedSplit
import EsmSplit.CoreJS.BindingMoves

/-! Construct a dense singleton split with a checked, commuting schedule. -/

namespace CoreJS.CheckedMoveSplit

open LinkedBehavior

private def simpleValue : Expr → Bool
  | .undef | .null | .bool _ | .num _ | .str _ | .var _ => true
  | _ => false

private def supportedUnit : Stmt → Bool
  | .decl .const _ value => simpleValue value
  | _ => false

/-- Footprints for the supported constant declarations. The move theorem
checks their actual syntax and commutation independently of this summary. -/
private def summarize (source : Program) : EsmSplit.Bundle :=
  match source.modules with
  | [m] => m.body.map fun (_, stmt) =>
    let defs := stmt.bindings.map Prod.fst
    { src := "", defs, eager := stmt.free.filter fun name => !defs.contains name,
      exports := (m.exports.filter fun (_, binding) => defs.contains binding).map fun (name, binding) => (name, some binding),
      effect := false }
  | _ => []

/-- In addition to the common split contract, a sequential request has its
authored module evaluation order. -/
def Sound (source : Program) (spec : EsmSplit.Spec) (requested : Guarantee) (output : Program) : Prop :=
  CheckedSplit.Sound source spec requested output ∧ output.evalOrder = spec.order ++ [spec.entry]

/-- The ordinary construction and the checked sequential order. -/
structure Construction (source : Program) (spec : EsmSplit.Spec) (requested : Guarantee) where
  split : CheckedSplit.Construction source spec requested
  ordered : split.program.evalOrder = spec.order ++ [spec.entry]

/-- Construct the requested dense placement when the checked moves commute.
The move theorem establishes exactly normal-completion equivalence. -/
def checkedMoveSplit (source : Program) (spec : EsmSplit.Spec) (requested : Guarantee) :
    Except String (Construction source spec requested) := do
  let .isTrue requestedNormal := (inferInstance : Decidable (requested = .normalCompletion)) |
    throw "commuting moves prove only normal-completion"
  unless !spec.privateEntryBody do throw "private entry bodies are not supported"
  unless spec.sequential && !spec.order.isEmpty do throw "expected an explicit sequential module order"
  EsmSplit.validateModulePaths (spec.modules.map Prod.fst)
  let [m] := source.modules | throw "checked split requires a single source module"
  let stmts := m.body.map Prod.snd
  let body := (List.range stmts.length).zip stmts
  let some ⟨sourceShape⟩ := SyntaxMatch.programEq source
      { entry := m.name, modules := [{ name := m.name, imports := [], body, exports := m.exports }] } |
    throw "source must be an import-free entry with consecutive unit identities starting at zero"
  for (id, stmt) in m.body do
    unless supportedUnit stmt do throw s!"unsupported unit {id}: expected a simple const"
  let bundle := summarize source
  unless spec.modules.all (fun (_, selectors) => selectors.all fun selector =>
      !(selector.picks bundle.declIdx bundle.length).isEmpty) do
    throw "a requested selector does not match a source unit"
  let plan := EsmSplit.mkPlan bundle spec
  let .isTrue schedule := (inferInstance : Decidable (plan.evalOrder = spec.order ++ [spec.entry])) |
    throw "the constructed modules do not follow the requested order"
  let .isTrue valid := (inferInstance : Decidable (EsmSplit.Valid bundle spec plan)) |
    throw "plan failed the placement, linkage or scope checks for the requested spec"
  let .isTrue footprints := (inferInstance : Decidable (FootprintsOfSyntax stmts bundle)) |
    throw "source footprints do not match the planned units"
  if moves : certifiedMovesOk stmts bundle plan.execOrder = true then
    let output := splitProgram stmts bundle plan
    let ⟨placed⟩ ← checkPlaces (CheckedSplit.requestedPlacement source spec) source output
    let links ← checkProbePair source output
    if exportsMatched : exportsMatchFrom links.before links.after = true then
      have normal : ScheduleEquivalent .normalCompletion (fun _ => True) source output := by
        apply normalEquivalent_of_bounded
        intro w _ n final
        have sourceRun : runProgram w n source = runProgram w n (Program.single stmts) := by
          rw [sourceShape]
          exact runProgram_singleModuleMetadata body (zip_range_sorted stmts) m.name m.exports w n
        rw [sourceRun]
        exact moves_preserve_run_of_sound valid footprints (certifiedMayMove_sound footprints valid.1)
          ((certifiedMovesOk_iff stmts bundle plan.execOrder).1 moves) n final
      have ordered : output.evalOrder = spec.order ++ [spec.entry] := by
        simpa only [output, split_evalOrder] using schedule
      return ⟨⟨bundle, plan, output, placed, links.sameExports exportsMatched, requestedNormal ▸ normal⟩, ordered⟩
    else throw "the split entry does not export the source's public live cells"
  else throw "a reordered pair is not certified to commute"

theorem checkedMoveSplit_sound {source spec requested}
    {result : Construction source spec requested}
    (_accepted : checkedMoveSplit source spec requested = .ok result) :
    Sound source spec requested result.split.program := ⟨result.split.sound, result.ordered⟩

#print axioms checkedMoveSplit
#print axioms checkedMoveSplit_sound

end CoreJS.CheckedMoveSplit
