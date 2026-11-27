import EsmSplit.CoreJS.LinkedBehavior
import EsmSplit.CoreJS.LinkWorld
import EsmSplit.CoreJS.Move
import EsmSplit.CoreJS.OrderedRegion
import EsmSplit.CoreJS.Split
import EsmSplit.Split

/-! Actual-link facts for hoisted functions in a plan produced from an authored
specification. Moving a function declaration between modules does not change
its original cell or the live addresses captured at link time. -/

namespace CoreJS.HoistedFunctionLink

open LinkedBehavior
variable {w : World}

/-- Primitive values that need no host observation or nested allocation to
evaluate. -/
def primitiveConstValue : Expr → Prop
  | .undef | .null | .bool _ | .num _ | .str _ => True
  | _ => False

/-- Executable decision procedure for the primitive initializer side condition. -/
def primitiveConstValue_decidable : (value : Expr) → Decidable (primitiveConstValue value)
  | .undef => isTrue trivial
  | .null => isTrue trivial
  | .bool _ => isTrue trivial
  | .num _ => isTrue trivial
  | .str _ => isTrue trivial
  | .regexp _ | .var _ | .host _ | .this | .func _ | .obj _ | .arr _ | .spreadArg _ |
    .cls .. | .derived .. | .get .. | .getSuper _ | .getOptional .. | .hasProp .. |
    .delete .. | .assign .. | .assignOp .. | .assignLogical .. | .update .. |
    .call .. | .callMethod .. | .callSuper .. | .callOptionalReceiver .. |
    .callOptionalCallee .. | .new .. | .prim .. | .unary .. | .binary .. |
    .logical .. | .cond .. | .seq .. => isFalse (by simp [primitiveConstValue])

/-- A function declaration's execution step is inert even at a zero fuel
bound: at zero both sides of any nonempty schedule return `.fuel`; with
positive fuel the step returns normally and resets the region to zero. -/
private theorem runUnit_inert_succ {u : LinkedUnit} (h : u.stmt.inert = true) (fuel : Nat) :
    (unitStep (W := w) (fuel + 1) u : M w Flow) =
      (fun s : State w => .ok .normal { s with region := 0 }) := by
  obtain ⟨id, stmt, env⟩ := u
  change runUnit (fuel + 1) env id stmt = _
  cases stmt with
  | varDecl _ => rfl
  | funDecl _ => rfl
  | «opaque» code decls funs free run =>
    simp only [Stmt.inert] at h
    cases decls with
    | nil =>
      cases run with
      | closures fns =>
        cases fns with
        | nil => rfl
        | cons _ _ => simp at h
      | world => simp at h
    | cons _ _ => simp at h
  | _ => simp [Stmt.inert] at h

private theorem runUnits_nonempty_region (fuel : Nat) (unit : LinkedUnit)
    (suffix : List LinkedUnit) (s : State w) (region : Nat) :
    runUnits fuel (unit :: suffix) {s with region} = runUnits fuel (unit :: suffix) s := by
  obtain ⟨id, stmt, env⟩ := unit
  unfold runUnits
  simp only [M.bind_apply, runUnit_region]

private theorem runUnits_inert_head (fuel : Nat) (pivot : LinkedUnit)
    (suffix : List LinkedUnit) (s : State w) (inert : pivot.stmt.inert = true) :
    runUnits (fuel + 1) (pivot :: suffix) s = runUnits (fuel + 1) suffix {s with region := 0} := by
  have hpivot := runUnit_inert_succ (w := w) (u := pivot) inert fuel
  change ((unitStep (fuel + 1) pivot >>= fun _ => runUnits (fuel + 1) suffix) s) = _
  rw [M.bind_apply, hpivot]

/-- An inert unit can swap with its neighbor without changing any modeled
result, including throws, unknown results and fuel exhaustion. -/
private theorem runUnits_inert_adjacent (fuel : Nat) (pivot other : LinkedUnit)
    (suffix : List LinkedUnit) (s : State w) (inert : pivot.stmt.inert = true) :
    runUnits fuel (pivot :: other :: suffix) s = runUnits fuel (other :: pivot :: suffix) s := by
  cases fuel with
  | zero =>
    simp [runUnits, runUnit, inRegion, execStmt, M.bind_apply, modifyState, outOfFuel]
  | succ fuel =>
    have hRun : runUnits (fuel + 1) (other :: pivot :: suffix) s =
        runUnits (fuel + 1) (other :: suffix) s := by
      change ((unitStep (fuel + 1) other >>= fun _ => runUnits (fuel + 1) (pivot :: suffix)) s) =
        ((unitStep (fuel + 1) other >>= fun _ => runUnits (fuel + 1) suffix) s)
      cases hother : unitStep (fuel + 1) other s with
      | ok result after =>
        have hzero : after = {after with region := 0} := by
          apply runUnit_region_zero
          exact hother
        simp only [M.bind_apply, hother]
        rw [runUnits_inert_head fuel pivot suffix after inert]
        rw [← hzero]
      | throw value after => simp only [M.bind_apply, hother]
      | unknown why after => simp only [M.bind_apply, hother]
      | fuel => simp only [M.bind_apply, hother]
    calc
      runUnits (fuel + 1) (pivot :: other :: suffix) s =
          runUnits (fuel + 1) (other :: suffix) {s with region := 0} :=
        runUnits_inert_head fuel pivot (other :: suffix) s inert
      _ = runUnits (fuel + 1) (other :: suffix) s := by
        exact runUnits_nonempty_region (fuel + 1) other suffix s 0
      _ = runUnits (fuel + 1) (other :: pivot :: suffix) s := hRun.symm

/-- Move one inert execution step across a block, keeping every intermediate
result and state. The proof deliberately makes no normal-completion assumption. -/
private theorem runUnits_move_inert (fuel : Nat) (pivot : LinkedUnit)
    (inert : pivot.stmt.inert = true) :
    ∀ (pre crossed suffix : List LinkedUnit) (s : State w),
      runUnits fuel (pre ++ [pivot] ++ crossed ++ suffix) s =
        runUnits fuel (pre ++ crossed ++ [pivot] ++ suffix) s := by
  intro pre crossed
  induction crossed generalizing pre with
  | nil => intro suffix s; simp
  | cons other crossed ih =>
    intro suffix s
    have leftList : pre ++ [pivot] ++ (other :: crossed) ++ suffix =
        pre ++ (pivot :: other :: (crossed ++ suffix)) := by simp [List.append_assoc]
    have rightList : pre ++ (other :: crossed) ++ [pivot] ++ suffix =
        pre ++ (other :: (crossed ++ [pivot] ++ suffix)) := by simp [List.append_assoc]
    rw [leftList, rightList, runUnits_append, runUnits_append]
    cases hpre : runUnits fuel pre s with
    | ok _ state =>
      simp only [M.bind_apply, hpre]
      have adjacent := runUnits_inert_adjacent fuel pivot other (crossed ++ suffix) state inert
      calc
        runUnits fuel (pivot :: other :: (crossed ++ suffix)) state =
            runUnits fuel (other :: pivot :: (crossed ++ suffix)) state := adjacent
        _ = runUnits fuel (other :: (crossed ++ [pivot] ++ suffix)) state := by
          simpa [List.append_assoc] using ih [other] suffix state
    | throw value state => simp [M.bind_apply, hpre]
    | unknown why state => simp [M.bind_apply, hpre]
    | fuel => simp [M.bind_apply, hpre]

/-- The two actual links for an authored plan start from the same ready state.
The candidate units retain their plan execution order and their free names
resolve to the source bundle's live cells. -/
theorem planned_linked_pair {ss : List Stmt} {bundle : EsmSplit.Bundle} {spec : EsmSplit.Spec}
    (valid : EsmSplit.Valid bundle spec (EsmSplit.mkPlan bundle spec))
    (footprints : FootprintsOfSyntax ss bundle) (w : World)
    (before : CheckedLink (Program.single ss) w) :
    ∃ after : CheckedLink (splitProgram ss bundle (EsmSplit.mkPlan bundle spec)) w,
      before.ready = after.ready ∧
      ∃ frames sourceLinked candidateLinked,
        FramesOf ((List.range ss.length).zip ss) frames ∧
        (Program.single ss).scopes frames (Program.single ss).loaded = .ok sourceLinked ∧
        (splitProgram ss bundle (EsmSplit.mkPlan bundle spec)).scopes frames
          (splitProgram ss bundle (EsmSplit.mkPlan bundle spec)).loaded = .ok candidateLinked ∧
        before.linked = sourceLinked ∧ after.linked = candidateLinked ∧
        (unitsOf candidateLinked).map (fun u => (u.1, u.2.1)) =
          (EsmSplit.mkPlan bundle spec).execOrder.filterMap (numbered ss) ∧
        ∀ u ∈ unitsOf candidateLinked,
          Agree u.2.1.free (bundleEnv ss frames) u.2.2 := by
  let plan := EsmSplit.mkPlan bundle spec
  let source := Program.single ss
  let candidate := splitProgram ss bundle plan
  have hperm : plan.execOrder.Perm (List.range ss.length) := valid_perm valid footprints.1
  obtain ⟨hregions, htops⟩ := split_holds_statements (B := bundle) hperm
  have scopeData : OrderedRegion.HoistScopesAgree source candidate := by
    intro frames hframes
    have hF : FramesOf ((List.range ss.length).zip ss) frames := by
      simpa only [source, single_tops] using hframes
    obtain ⟨candidateLinked, candidateScope, candidateKeys, candidateAgreement⟩ :=
      split_units_agree valid footprints hF
    have sourceScope : source.scopes frames source.loaded =
        .ok [(bundleModule ss, bundleEnv ss frames)] := by
      rw [single_loaded]
      rfl
    have sourceKeys :
        (unitsOf [(bundleModule ss, bundleEnv ss frames)]).map (fun u => (u.1, u.2.1)) =
          (List.range ss.length).zip ss := by
      rw [unitsOf_keys]
      simp [bundleModule]
    have sourceEnv : ∀ u ∈ unitsOf [(bundleModule ss, bundleEnv ss frames)],
        u.2.2 = bundleEnv ss frames := by
      intro u hu
      obtain ⟨p, hp, _, he⟩ := mem_unitsOf hu
      rw [List.mem_singleton] at hp
      subst hp
      exact he
    have sameSorted : SameUnits
        ((unitsOf [(bundleModule ss, bundleEnv ss frames)]).mergeSort fun a b => decide (a.1 ≤ b.1))
        ((unitsOf candidateLinked).mergeSort fun a b => decide (a.1 ≤ b.1)) := by
      refine sameUnits_of ?_ fun u hu u' hu' _ hstmt => ?_
      · rw [List.map_mergeSort (f := fun u : LinkedUnit => (u.1, u.2.1))
            (s := fun a b : Nat × Stmt => decide (a.1 ≤ b.1)) (fun _ _ _ _ => rfl),
          sourceKeys,
          List.map_mergeSort (f := fun u : LinkedUnit => (u.1, u.2.1))
            (s := fun a b : Nat × Stmt => decide (a.1 ≤ b.1)) (fun _ _ _ _ => rfl),
          candidateKeys, mergeSort_zip_range ss, mergeSort_numbered ss hperm]
      · rw [sourceEnv u ((List.mergeSort_perm _ _).mem_iff.1 hu), hstmt]
        exact candidateAgreement u' ((List.mergeSort_perm _ _).mem_iff.1 hu')
    exact ⟨[(bundleModule ss, bundleEnv ss frames)], candidateLinked,
      sourceScope, candidateScope, sameSorted⟩
  obtain ⟨_, frames, _, after, _, declared, sourceScope, candidateScope, readyEq⟩ :=
    OrderedRegion.matching_hoist_link_with_frames hregions htops scopeData before
  have hF : FramesOf ((List.range ss.length).zip ss) frames := by
    simpa only [source, single_tops] using declareTops_names _ declared
  obtain ⟨candidateLinked, candidateScope', candidateKeys, candidateAgreement⟩ :=
    split_units_agree valid footprints hF
  have candidateListEq := Except.ok.inj (candidateScope.symm.trans candidateScope')
  refine ⟨after, readyEq, frames, before.linked, after.linked, hF,
    sourceScope, candidateScope, rfl, rfl, ?_, ?_⟩
  · rw [candidateListEq, candidateKeys]
  · intro u hu
    rw [candidateListEq] at hu
    exact candidateAgreement u hu

/-- For an actual source bundle and its authored `mkPlan`, the original
function statement keeps its identity and exact code, its declaration cell is
the original live cell, and its free-name capture resolves to the same
addresses in both links. No execution-order premise is needed. -/
theorem planned_function {ss : List Stmt} {bundle : EsmSplit.Bundle} {spec : EsmSplit.Spec}
    (valid : EsmSplit.Valid bundle spec (EsmSplit.mkPlan bundle spec))
    (footprints : FootprintsOfSyntax ss bundle) (w : World)
    (before : CheckedLink (Program.single ss) w) {unitId : Nat} {f : Func}
    (sourceUnit : (unitId, .funDecl f) ∈ (List.range ss.length).zip ss) :
    ∃ after : CheckedLink (splitProgram ss bundle (EsmSplit.mkPlan bundle spec)) w,
      before.ready = after.ready ∧
      ∃ sourceEnv candidateEnv cell,
        (unitId, .funDecl f, sourceEnv) ∈ unitsOf before.linked ∧
        (unitId, .funDecl f, candidateEnv) ∈ unitsOf after.linked ∧
        sourceEnv.vars.lookup f.name = some cell ∧
        candidateEnv.vars.lookup f.name = some cell ∧
        sourceEnv.capture f.free = candidateEnv.capture f.free := by
  let plan := EsmSplit.mkPlan bundle spec
  let source := Program.single ss
  obtain ⟨after, readyEq, frames, sourceLinked, candidateLinked, hFrames,
      sourceScope, candidateScope, sourceLinkedEq, candidateLinkedEq, candidateKeys,
      candidateAgreement⟩ := planned_linked_pair valid footprints w before
  have sourceListEq : sourceLinked = [(bundleModule ss, bundleEnv ss frames)] := by
    have expected : source.scopes frames source.loaded =
        .ok [(bundleModule ss, bundleEnv ss frames)] := by
      rw [single_loaded]
      rfl
    rw [expected] at sourceScope
    injection sourceScope with eq
    exact eq.symm
  have sourceUnitNumbered : (unitId, .funDecl f) ∈
      (List.range ss.length).filterMap (numbered ss) := by
    rw [filterMap_numbered_range]
    exact sourceUnit
  have sourceInfo := (mem_numbered ss).1 sourceUnitNumbered
  have sourceMem : (unitId, .funDecl f, bundleEnv ss frames) ∈
      unitsOf [(bundleModule ss, bundleEnv ss frames)] := by
    simpa [unitsOf, bundleModule] using sourceUnit
  have beforeMem : (unitId, .funDecl f, bundleEnv ss frames) ∈ unitsOf before.linked := by
    rw [sourceLinkedEq, sourceListEq]
    exact sourceMem
  have idInOrder : unitId ∈ plan.execOrder :=
    (valid_perm valid footprints.1).mem_iff.mpr sourceInfo.1
  have candidateKey : (unitId, .funDecl f) ∈ plan.execOrder.filterMap (numbered ss) :=
    (mem_numbered ss).2 ⟨idInOrder, sourceInfo.2⟩
  have candidateMemKey : (unitId, .funDecl f) ∈
      (unitsOf candidateLinked).map (fun u => (u.1, u.2.1)) := by
    rw [candidateKeys]
    exact candidateKey
  obtain ⟨candidateUnit, candidateMem, candidateUnitKey⟩ := List.mem_map.1 candidateMemKey
  obtain ⟨candidateId, candidateStmt, candidateEnv⟩ := candidateUnit
  dsimp only at candidateUnitKey
  simp only [Prod.mk.injEq] at candidateUnitKey
  rcases candidateUnitKey with ⟨candidateIdEq, candidateStmtEq⟩
  subst candidateId
  subst candidateStmt
  have candidateMemActual : (unitId, .funDecl f, candidateEnv) ∈ unitsOf after.linked := by
    rw [candidateLinkedEq]
    exact candidateMem
  have fNameDefs : f.name ∈ (bundle.stmt unitId).defs := by
    have hin : ss[unitId]? = some (.funDecl f) := sourceInfo.2
    have hdefs := ((footprints.2 unitId (.funDecl f) hin).1 f.name)
    exact hdefs.2 (by simp [Stmt.bindings, Stmt.varNames, varNamesOf, lexDecls])
  have hmain : ∃ p ∈ (bundleModule ss).body, p.1 = unitId := by
    exact ⟨(unitId, .funDecl f), sourceUnit, rfl⟩
  have frameKey := (frame_keys footprints hFrames).2
    ⟨List.mem_range.1 sourceInfo.1, fNameDefs⟩
  have frameCell : ∃ cell, (frameAt frames unitId).lookup f.name = some cell :=
    Option.ne_none_iff_exists'.1 fun none =>
      (lookup_none_iff_not_mem_keys.mp none) frameKey
  obtain ⟨cell, frameCell⟩ := frameCell
  have sourceCell : (bundleEnv ss frames).vars.lookup f.name = some cell := by
    show ((bundleModule ss).scope frames ++ []).lookup f.name = some cell
    rw [List.append_nil,
      scope_lookup_decl footprints hFrames valid.1 hmain (List.mem_range.1 sourceInfo.1) fNameDefs]
    exact frameCell
  have agree := candidateAgreement (unitId, .funDecl f, candidateEnv) candidateMem
  have sameCell : candidateEnv.vars.lookup f.name = some cell := by
    have names := agree.vars f.name (by simp [Stmt.free])
    rw [sourceCell] at names
    exact names.symm
  have sameCapture : (bundleEnv ss frames).capture f.free = candidateEnv.capture f.free := by
    apply capture_agree
    exact agree.mono (by
      intro name member
      exact List.mem_cons_of_mem f.name member)
  exact ⟨after, readyEq, bundleEnv ss frames, candidateEnv, cell,
    beforeMem, candidateMemActual, sourceCell, sameCell, sameCapture⟩

/-- Bundle units in the order produced by a planner, carrying the source
bundle's single-module environment. -/
private def bundleOrderUnits (ss : List Stmt)
    (frames : List (Nat × List (Name × Addr))) (order : List Nat) : List LinkedUnit :=
  (order.filterMap (numbered ss)).map fun p => (p.1, p.2, bundleEnv ss frames)

private theorem bundleOrderUnits_env {ss : List Stmt}
    {frames : List (Nat × List (Name × Addr))} {order : List Nat} {u : LinkedUnit}
    (member : u ∈ bundleOrderUnits ss frames order) : u.2.2 = bundleEnv ss frames := by
  unfold bundleOrderUnits at member
  obtain ⟨p, _, rfl⟩ := List.mem_map.1 member
  rfl

private theorem capture_lookup_of_mem {env : Env} {names : List Name} {name : Name}
    (member : name ∈ names) : (env.capture names).vars.lookup name = env.vars.lookup name := by
  simp [Env.capture, lookup_filterMap, List.mem_eraseDups, member]

/-- If `mkPlan` moves one ordinary function declaration across an independent
primitive const, actual source and planned links preserve every bounded result.
The function closes over a different earlier const. One checked link of the
actual source is transported to every world; the candidate link, original
function cell and captured live address are derived from the plan's frames. -/
theorem planned_independent_primitive_closure_crossing
    {ss : List Stmt} {bundle : EsmSplit.Bundle} {spec : EsmSplit.Spec}
    (valid : EsmSplit.Valid bundle spec (EsmSplit.mkPlan bundle spec))
    (footprints : FootprintsOfSyntax ss bundle)
    (sourceLink : CheckedLink (Program.single ss) World.probe)
    (preOrder suffix : List Nat) (captureId literalId functionId : Nat)
    (captureName literalName : Name) (captureValue literalValue : Expr)
    (f : Func)
    (_captureEarlier : captureId ∈ preOrder)
    (capture : ss[captureId]? = some (.decl .const captureName captureValue))
    (crossed : ss[literalId]? = some (.decl .const literalName literalValue))
    (_primitive : primitiveConstValue literalValue)
    (function : ss[functionId]? = some (.funDecl f))
    (_ordinary : f.kind = .normal) (_nonSelf : f.self = false)
    (_notOpaque : f.opaqueBody = none)
    (capturesEarlier : captureName ∈ f.free)
    (_independent : literalName ∉ f.free)
    (sourceOrder : List.range ss.length = preOrder ++ [literalId, functionId] ++ suffix)
    (plannedOrder : (EsmSplit.mkPlan bundle spec).execOrder = preOrder ++ [functionId, literalId] ++ suffix) :
    ∀ w fuel,
      runProgram w fuel (Program.single ss) =
        runProgram w fuel (splitProgram ss bundle (EsmSplit.mkPlan bundle spec)) ∧
      ∃ after : CheckedLink (splitProgram ss bundle (EsmSplit.mkPlan bundle spec)) w,
        (sourceLink.forWorld w).ready = after.ready ∧
        ∃ sourceEnv candidateEnv functionCell capturedCell,
          (functionId, .funDecl f, sourceEnv) ∈ unitsOf (sourceLink.forWorld w).linked ∧
          (functionId, .funDecl f, candidateEnv) ∈ unitsOf after.linked ∧
          sourceEnv.vars.lookup f.name = some functionCell ∧
          candidateEnv.vars.lookup f.name = some functionCell ∧
          (sourceLink.forWorld w).ready.cellAt functionCell = after.ready.cellAt functionCell ∧
          sourceEnv.capture f.free = candidateEnv.capture f.free ∧
          (sourceEnv.capture f.free).vars.lookup captureName = some capturedCell ∧
          (candidateEnv.capture f.free).vars.lookup captureName = some capturedCell := by
  intro w fuel
  let before := sourceLink.forWorld w
  obtain ⟨after, readyEq, frames, sourceLinked, candidateLinked, hFrames,
      sourceScope, candidateScope, sourceLinkedEq, candidateLinkedEq, candidateKeys,
      candidateAgreement⟩ := planned_linked_pair valid footprints w before
  have sourceListEq : sourceLinked = [(bundleModule ss, bundleEnv ss frames)] := by
    have expected : (Program.single ss).scopes frames (Program.single ss).loaded =
        .ok [(bundleModule ss, bundleEnv ss frames)] := by
      rw [single_loaded]
      rfl
    rw [expected] at sourceScope
    injection sourceScope with eq
    exact eq.symm
  have sourceKeys : (unitsOf before.linked).map (fun u => (u.1, u.2.1)) =
      (bundleOrderUnits ss frames (List.range ss.length)).map (fun u => (u.1, u.2.1)) := by
    rw [sourceLinkedEq, sourceListEq, unitsOf_keys, bundleModule, bundleOrderUnits]
    simp only [List.flatMap_cons, List.flatMap_nil, List.append_nil, List.map_map, Function.comp_def]
    rw [filterMap_numbered_range]
    simp
  have sourceEnv : ∀ u ∈ unitsOf before.linked,
      u.2.2 = bundleEnv ss frames := by
    intro u hu
    rw [sourceLinkedEq, sourceListEq] at hu
    obtain ⟨p, hp, _, he⟩ := mem_unitsOf hu
    rw [List.mem_singleton] at hp
    subst hp
    exact he
  have sourceSame : SameUnits (unitsOf before.linked)
      (bundleOrderUnits ss frames (List.range ss.length)) := by
    refine sameUnits_of sourceKeys fun u hu u' hu' _ _ => ?_
    rw [sourceEnv u hu, bundleOrderUnits_env hu']
    exact ⟨rfl, rfl, fun _ _ => rfl⟩
  have candidateScheduleKeys :
      (unitsOf after.linked).map (fun u => (u.1, u.2.1)) =
        (bundleOrderUnits ss frames (EsmSplit.mkPlan bundle spec).execOrder).map
          (fun u => (u.1, u.2.1)) := by
    rw [candidateLinkedEq]
    simpa [bundleOrderUnits, List.map_map, Function.comp_def] using candidateKeys
  have candidateSame : SameUnits (unitsOf after.linked)
      (bundleOrderUnits ss frames (EsmSplit.mkPlan bundle spec).execOrder) := by
    refine sameUnits_of candidateScheduleKeys fun u hu u' hu' _ _ => ?_
    have agree := candidateAgreement u (by
      rw [candidateLinkedEq] at hu
      exact hu)
    rw [bundleOrderUnits_env hu']
    exact ⟨agree.this.symm, agree.home.symm, fun x hx => (agree.vars x hx).symm⟩
  let literalUnit : LinkedUnit := (literalId, .decl .const literalName literalValue, bundleEnv ss frames)
  let functionUnit : LinkedUnit := (functionId, .funDecl f, bundleEnv ss frames)
  have sourceSchedule : bundleOrderUnits ss frames (List.range ss.length) =
      bundleOrderUnits ss frames preOrder ++ [literalUnit, functionUnit] ++
        bundleOrderUnits ss frames suffix := by
    rw [sourceOrder]
    simp [bundleOrderUnits, crossed, function, numbered, literalUnit, functionUnit,
      List.filterMap_append, List.map_append, List.append_assoc]
  have plannedSchedule : bundleOrderUnits ss frames (EsmSplit.mkPlan bundle spec).execOrder =
      bundleOrderUnits ss frames preOrder ++ [functionUnit, literalUnit] ++
        bundleOrderUnits ss frames suffix := by
    rw [plannedOrder]
    simp [bundleOrderUnits, crossed, function, numbered, literalUnit, functionUnit,
      List.filterMap_append, List.map_append, List.append_assoc]
  have moved := runUnits_move_inert fuel functionUnit (by simp [functionUnit, Stmt.inert])
    (bundleOrderUnits ss frames preOrder) [literalUnit] (bundleOrderUnits ss frames suffix) before.ready
  have executions : runProgram w fuel (Program.single ss) =
      runProgram w fuel (splitProgram ss bundle (EsmSplit.mkPlan bundle spec)) := by
    rw [run_after_link fuel before, run_after_link fuel after, ← readyEq]
    rw [runUnits_same fuel sourceSame, runUnits_same fuel candidateSame]
    rw [sourceSchedule, plannedSchedule]
    simpa [List.append_assoc] using moved.symm
  obtain ⟨functionLink, _, sourceEnv', candidateEnv', functionCell, sourceMem, candidateMem,
      sourceCell, candidateCell, captures⟩ :=
    planned_function valid footprints w before (by
      rw [← filterMap_numbered_range]
      exact (mem_numbered ss).2 ⟨List.mem_range.2 ((List.getElem?_eq_some_iff.1 function).1), function⟩)
  have linkResultEq : functionLink.linked = after.linked ∧ functionLink.ready = after.ready := by
    have h : (.ok functionLink.linked functionLink.ready : Res w (List (Module × Env))) =
        .ok after.linked after.ready := functionLink.equation.symm.trans after.equation
    exact ⟨by injection h, by injection h⟩
  have sourceFunctionMem := sourceMem
  have sourceEnvEq : sourceEnv' = bundleEnv ss frames := by
    rw [sourceLinkedEq, sourceListEq] at sourceMem
    obtain ⟨p, hp, _, he⟩ := mem_unitsOf sourceMem
    rw [List.mem_singleton] at hp
    subst p
    exact he
  have candidateMemAfter : (functionId, .funDecl f, candidateEnv') ∈ unitsOf after.linked := by
    rw [← linkResultEq.1]
    exact candidateMem
  have agreeCandidate : Agree f.free (bundleEnv ss frames) candidateEnv' := by
    have agreeStatement := candidateAgreement (functionId, .funDecl f, candidateEnv') (by
      rw [← candidateLinkedEq]
      exact candidateMemAfter)
    exact agreeStatement.mono (by
      intro x hx
      simp [Stmt.free, hx])
  have captureIndex : captureId < ss.length := by
    have member : captureId ∈ List.range ss.length := by
      rw [sourceOrder]
      exact List.mem_append_left _ (List.mem_append_left _ _captureEarlier)
    exact List.mem_range.1 member
  have captureTop : (captureId, .decl .const captureName captureValue) ∈
      (List.range ss.length).zip ss := by
    rw [← filterMap_numbered_range]
    exact (mem_numbered ss).2 ⟨List.mem_range.2 captureIndex, capture⟩
  have captureMain : ∃ p ∈ (bundleModule ss).body, p.1 = captureId :=
    ⟨(captureId, .decl .const captureName captureValue), by simpa [bundleModule] using captureTop, rfl⟩
  have captureNameBinding : captureName ∈
      (Stmt.decl DeclKind.const captureName captureValue).bindings.map Prod.fst := by
    simp [Stmt.bindings, Stmt.varNames, varNamesOf, lexDecls]
  have captureNameDef : captureName ∈ (bundle.stmt captureId).defs :=
    (footprints.2 captureId _ capture).1 captureName |>.2 captureNameBinding
  have frameKey := (frame_keys footprints hFrames).2 ⟨captureIndex, captureNameDef⟩
  have frameCell : ∃ cell, (frameAt frames captureId).lookup captureName = some cell :=
    Option.ne_none_iff_exists'.1 fun none =>
      (lookup_none_iff_not_mem_keys.mp none) frameKey
  obtain ⟨capturedCell, frameCell⟩ := frameCell
  have sourceCapturedNameCell : (bundleEnv ss frames).vars.lookup captureName = some capturedCell := by
    show ((bundleModule ss).scope frames ++ []).lookup captureName = some capturedCell
    rw [List.append_nil,
      scope_lookup_decl footprints hFrames valid.1 captureMain captureIndex captureNameDef]
    exact frameCell
  have candidateCapturedNameCell : candidateEnv'.vars.lookup captureName = some capturedCell := by
    exact (agreeCandidate.vars captureName capturesEarlier).symm.trans sourceCapturedNameCell
  have sourceCapturedCell : (sourceEnv'.capture f.free).vars.lookup captureName = some capturedCell := by
    rw [capture_lookup_of_mem capturesEarlier, sourceEnvEq]
    exact sourceCapturedNameCell
  have candidateCapturedCell : (candidateEnv'.capture f.free).vars.lookup captureName = some capturedCell := by
    rw [capture_lookup_of_mem capturesEarlier]
    exact candidateCapturedNameCell
  have functionCellState : before.ready.cellAt functionCell = after.ready.cellAt functionCell := by
    rw [readyEq]
  refine ⟨executions, after, readyEq, sourceEnv', candidateEnv', functionCell, capturedCell,
    sourceFunctionMem, ?_, sourceCell, candidateCell, functionCellState, captures,
    sourceCapturedCell, candidateCapturedCell⟩
  rw [← linkResultEq.1]
  exact candidateMem



end CoreJS.HoistedFunctionLink
