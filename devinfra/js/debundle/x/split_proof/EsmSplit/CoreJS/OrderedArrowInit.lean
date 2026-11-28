import EsmSplit.CoreJS.ConstructedOutput
import EsmSplit.CoreJS.LinkWorld
import EsmSplit.CoreJS.LinkedPivotCheck
import EsmSplit.CoreJS.StateMatch

/-! The initialization proof for an ordered block of var arrows. It uses the
actual linked programs and is intentionally independent of any call-coverage
claim about the newly allocated functions. -/

namespace CoreJS.OrderedArrowInit

open LinkedBehavior

/-- The distinction from hoisted function declarations: the declaration phase
only creates a mutable undefined cell. -/
theorem bindings_var_arrow (name : Name) (f : Func) :
    (Stmt.decl .var name (.func f)).bindings = [(name, ⟨some .undef, true⟩)] := by rfl

/-- Each arrow is made and written into its var cell at its original evaluation
step, using the step's actual environment and allocation region. -/
theorem runUnit_var_arrow {w : World} (fuel id : Nat) (env : Env) (name : Name) (f : Func) :
    (runUnit (fuel + 2) env id (.decl .var name (.func f)) : M w Flow) =
      inRegion id
        (do
          writeVar env name (← makeFunc env f)
          pure .normal) := by
  rfl

structure Behavior (source candidate : Program) : Prop where
  linked :
    ∀ w,
      ∃ (before : CheckedLink source w) (after : CheckedLink candidate w),
        before.ready = after.ready ∧ SameUnits (unitsOf before.linked) (unitsOf after.linked)
  publicCells : SameExports source candidate
  initialization : ∀ w fuel, runProgram w fuel source = runProgram w fuel candidate

/-- Apply a caller observation to a specific public live cell after initialization.
The caller can supply arguments or allocate its own receiver in the common state. -/
def observeExport {w : World} (p : Program) (fuel : Nat) (name : Name) (cell : Addr)
    (observe : Val → M w α) : Res w α :=
  match runProgram w fuel p with
  | .ok () state =>
    if (exports w p).any (·.contains (name, cell)) then
      (do
          observe (← readCell cell))
        state
    else .unknown "missing public cell" state
  | .throw v state => .throw v state
  | .unknown why state => .unknown why state
  | .fuel => .fuel

/-- An observer using the same public cell gets the same complete result,
including the effects of supplied getters and callbacks. Equality can
still be an unsupported result on both sides. -/
theorem Behavior.observeExport_eq {source candidate : Program} (proof : Behavior source candidate)
    (w : World) (fuel : Nat) (name : Name) (cell : Addr) (observe : Val → M w α) :
    observeExport source fuel name cell observe =
      observeExport candidate fuel name cell observe := by
  obtain ⟨a, b, ha, hb, perm⟩ := proof.publicCells w
  have same : a.contains (name, cell) = b.contains (name, cell) := by
    apply Bool.eq_iff_iff.mpr
    simp only [List.contains_iff_mem]
    exact perm.mem_iff
  unfold observeExport
  rw [proof.initialization w fuel, ha, hb]
  simp only [Option.any, same]

private def concreteVarArrow (u : LinkedUnit) : Bool :=
  match u.stmt with
  | .decl .var _ (.func f) => f.kind == .arrow && f.opaqueBody.isNone
  | _ => false

/-- SameUnits checks every original unit, including the free cell addresses
of deferred calls. The comparison of ready states retains allocation regions.
Transport uses the fact that linking does not run host code. -/
def check (source candidate : Program) : Except String (PLift (Behavior source candidate)) := do
  let pair ← checkProbePair source candidate
  let units := unitsOf pair.before.linked
  unless !units.isEmpty && units.all concreteVarArrow do
    throw "expected concrete var-initialized arrows"
  let some ⟨same⟩ :=
    sameUnits? units
      (unitsOf pair.after.linked) | throw "ordered units or their captured cells differ"
  let some ⟨ready⟩ :=
    StateMatch.probe pair.before.ready pair.after.ready | throw "linked ready states differ"
  if exports : exportsMatchFrom pair.before pair.after = true then
    have readyFor (w : World) : (pair.before.forWorld w).ready = (pair.after.forWorld w).ready :=
      congrArg (linkState w) ready
    return ⟨⟨fun w => ⟨pair.before.forWorld w, pair.after.forWorld w, readyFor w, same⟩,
          pair.sameExports exports, fun w fuel => by
          rw [run_after_link fuel (pair.before.forWorld w),
            run_after_link fuel (pair.after.forWorld w), readyFor w]
          exact congrFun (runUnits_same (W := w) fuel same) (pair.after.forWorld w).ready⟩⟩
  else
    throw "public names or live export cells differ"

/-- The result applies to the independently reparsed files when they lower to
the program constructed from the plan. -/
def checkReadback {source candidate : Program} (proof : Behavior source candidate)
    (files : Lean.Json) :
    Except String
      (PLift
        (∃ observed,
          ConstructedOutput.decodeConstructedOutput files candidate = .ok observed ∧
            Behavior source observed)) :=
  do
  let ⟨same⟩ ← ConstructedOutput.checkConstructedOutput files candidate
  return ⟨candidate, same, proof⟩

end CoreJS.OrderedArrowInit
