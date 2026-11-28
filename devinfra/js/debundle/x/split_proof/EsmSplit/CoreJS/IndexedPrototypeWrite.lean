import EsmSplit.CoreJS.ConstructedOutput
import EsmSplit.CoreJS.DeploymentPlacement
import EsmSplit.CoreJS.HoistedFunctionLink
import EsmSplit.CoreJS.IndexedExportedFunction
import EsmSplit.CoreJS.IndexedSourceSchedule
import EsmSplit.CoreJS.LinkWorld
import EsmSplit.CoreJS.LinkedPivotCheck
import EsmSplit.CoreJS.Move
import EsmSplit.CoreJS.PlanPlacement
import EsmSplit.CoreJS.Scope
import EsmSplit.CoreJS.StateMatch

/-! Move one prototype initializer across two declarations that have already
been instantiated. Check the exact linked schedule, including captured cells
and the full ready state, before applying the same-fuel commutation theorem. -/

namespace CoreJS.IndexedPrototypeWrite

open EsmSplit LinkedBehavior

private def concreteMethod : PropDef → Bool
  | .init (.str _) (.func f) | .method (.str _) f =>
    IndexedExportedFunction.concreteRetainedStatement (.funDecl f) (switchBody := true)
  | _ => false

private def prototypeWrite (ctor : Name) : Stmt → Bool
  | .expr (.assign (.member (.var name) (.str "prototype")) (.obj _)) => name == ctor
  | _ => false

/-- Recognize only a request for the lone prototype write. Other placements
continue through the existing constructors and retain their refusals. -/
def recognises (source : Module) (B : Bundle) (spec : Spec) : Bool :=
  match spec.order, source.body with
  | [helper], (_, .funDecl _) :: (_, .funDecl ctor) :: (id, pivot) :: _ =>
    spec.sequential && !spec.privateEntryBody &&
      spec.claimants B id == [helper] && prototypeWrite ctor.name pivot
  | _, _ => false

structure Behavior (source candidate : Program) : Prop where
  sameExports : SameExports source candidate
  allOutcomes : ∀ w fuel, runProgram w fuel source = runProgram w fuel candidate

/-- Both declaration evaluation steps are inert after linking. Moving the
third unit before both declarations preserves its result (including abrupt
completion) at every fuel bound. -/
theorem runUnits_move_before_declarations {w : World} (fuel a b : Nat)
    (f g : Func) (envF envG : Env) (pivot : LinkedUnit) (tail : List LinkedUnit) (s : State w) :
    runUnits fuel ((a, .funDecl f, envF) :: (b, .funDecl g, envG) :: pivot :: tail) s =
      runUnits fuel (pivot :: (a, .funDecl f, envF) :: (b, .funDecl g, envG) :: tail) s := by
  calc
    _ = runUnits fuel ((a, .funDecl f, envF) :: pivot :: (b, .funDecl g, envG) :: tail) s := by
      change ((unitStep fuel (a, .funDecl f, envF) >>= fun _ =>
        runUnits fuel ((b, .funDecl g, envG) :: pivot :: tail)) s) =
        ((unitStep fuel (a, .funDecl f, envF) >>= fun _ =>
        runUnits fuel (pivot :: (b, .funDecl g, envG) :: tail)) s)
      simp only [M.bind_apply]
      cases unitStep (W := w) fuel (a, .funDecl f, envF) s with
      | ok _ after =>
        exact HoistedFunctionLink.runUnits_inert_adjacent fuel (b, .funDecl g, envG) pivot tail after rfl
      | _ => rfl
    _ = _ := HoistedFunctionLink.runUnits_inert_adjacent fuel (a, .funDecl f, envF) pivot
      ((b, .funDecl g, envG) :: tail) s rfl

def checkBehavior (source candidate : Program) : Except String (PLift (Behavior source candidate)) := do
  let pair ← checkProbePair source candidate
  let .isTrue sameExports := (inferInstance : Decidable (exportsMatchFrom pair.before pair.after = true)) |
    throw "entry exports do not resolve to the same live cells"
  let some ⟨ready⟩ := StateMatch.probe pair.before.ready pair.after.ready |
    throw "linked ready states differ"
  match original : unitsOf pair.before.linked with
  | (a, .funDecl f, envF) :: (b, .funDecl g, envG) :: pivot :: tail =>
    let some ⟨moved⟩ := sameUnits? (pivot :: (a, .funDecl f, envF) :: (b, .funDecl g, envG) :: tail)
        (unitsOf pair.after.linked) |
      throw "candidate changes the linked statements, captures, or order beyond the prototype write"
    return ⟨⟨pair.sameExports sameExports, fun w fuel => by
      rw [run_after_link fuel (pair.before.forWorld w), run_after_link fuel (pair.after.forWorld w)]
      change runUnits fuel (unitsOf pair.before.linked) (linkState w pair.before.ready) =
        runUnits fuel (unitsOf pair.after.linked) (linkState w pair.after.ready)
      rw [original, ← ready, ← runUnits_same fuel moved]
      exact runUnits_move_before_declarations fuel a b f g envF envG pivot tail _⟩⟩
  | _ => throw "expected a prototype write after two hoisted declarations"

structure Construction (decoded : AuthenticatedSource.IndexedSource) (B : Bundle) (spec : Spec) where
  plan : Plan
  valid : Valid B spec plan
  program : Program
  ordered : program.evalOrder = spec.order ++ [spec.entry]
  places : Places (PlanPlacement.assignments plan) decoded.program program
  behavior : Behavior decoded.program program

def construct (decoded : AuthenticatedSource.IndexedSource) (B : Bundle) (spec : Spec) :
    Except String (Construction decoded B spec) := do
  let [helper] := spec.order | throw "expected one authored helper"
  let [(0, .funDecl first), (1, .funDecl ctor), (2, pivot), (3, .funDecl factory)] := decoded.source.body |
    throw "expected two hoisted declarations, a prototype write, and a hoisted factory"
  let .expr (.assign _ (.obj fields)) := pivot | throw "expected a prototype object"
  unless recognises decoded.source B spec && !fields.isEmpty && fields.all concreteMethod do
    throw "expected a prototype object with concrete methods on the second declaration"
  unless factory.name == "default" &&
      [first, ctor, factory].all (fun f => IndexedExportedFunction.concreteRetainedStatement (.funDecl f)) do
    throw "expected three concrete hoisted functions, including the default factory"
  let _ ← IndexedSourceSchedule.checkSequence decoded B
  unless decoded.localExports.isEmpty && helper != spec.entry &&
      (spec.modules.map Prod.fst).Perm [spec.entry, helper] &&
      spec.modules.all (fun row => row.1.endsWith ".js" || row.1.endsWith ".mjs") do
    throw "expected two JavaScript files with inline exports only"
  unless ((spec.modules.flatMap Prod.snd).map (fun
      | .stmt id => some id | .name _ => none)).Perm ((List.range B.length).map some) do
    throw "select each original ordinal exactly once"
  unless [0, 1, 3].all (fun id => spec.claimants B id == [spec.entry]) do
    throw "all hoisted declarations must remain at the public entry"
  let plan := mkPlan B spec
  let .isTrue valid := (inferInstance : Decidable (Valid B spec plan)) |
    throw "authored plan failed validity checks"
  unless plan.execOrder == [2, 0, 1, 3] do
    throw "authored plan changes order beyond the prototype write"
  let some entry := plan.find spec.entry | throw "missing authored entry"
  let some privateModule := plan.find helper | throw "missing authored helper"
  unless entry.exports.isEmpty && privateModule.exports.isEmpty &&
      entry.imports.map Prod.fst == [helper] && privateModule.imports.map Prod.fst == [spec.entry] do
    throw "expected an entry-helper cycle without additional exports"
  let program := splitProgramIndexed decoded.source.body B plan
  let .isTrue ordered := (inferInstance : Decidable (program.evalOrder = spec.order ++ [spec.entry])) |
    throw "constructed module order differs from authored order"
  let ⟨places⟩ ← checkPlaces (PlanPlacement.assignments plan) decoded.program program
  let ⟨behavior⟩ ← checkBehavior decoded.program program
  return ⟨plan, valid, program, ordered, places, behavior⟩

/-- The reparsed artifact inherits the constructor's placement, authored
order, live public cells and all outcomes. -/
def Construction.checkReadback {decoded B spec} (built : Construction decoded B spec)
    (files : Lean.Json) : Except String (PLift (∃ observed,
      ConstructedOutput.decodeConstructedOutput files built.program = .ok observed ∧
      Places (PlanPlacement.assignments built.plan) decoded.program observed ∧
      observed.evalOrder = spec.order ++ [spec.entry] ∧ Behavior decoded.program observed)) := do
  let ⟨same⟩ ← ConstructedOutput.checkConstructedOutput files built.program
  return ⟨built.program, same, built.places, built.ordered, built.behavior⟩

end CoreJS.IndexedPrototypeWrite
