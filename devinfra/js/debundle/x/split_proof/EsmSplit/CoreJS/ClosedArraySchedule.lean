import EsmSplit.CoreJS.IndexedReorder

/-! A narrow indexed schedule rule for const initializers made from nested
closed array literals and `void` over primitive literals. It uses the shared
`Expr.allocOnly` frame proof for semantics; the shape predicate excludes
property reads, binding aliases, host calls, and abrupt operands. -/

namespace CoreJS.ClosedArraySchedule

open LiteralSwapCheck

private def closedPrimitive : Expr → Bool
  | .undef | .null | .bool _ | .num _ | .str _ => true
  | _ => false

mutual
/-- Primitive leaves, `void` over a primitive leaf, and recursively nested
array literals. No binding, host, property, or callable expression is admitted. -/
def closedValue : Expr → Bool
  | .undef | .null | .bool _ | .num _ | .str _ => true
  | .unary .void operand => closedPrimitive operand
  | .arr values => closedValues values
  | _ => false

def closedValues : List Expr → Bool
  | [] => true
  | value :: rest => closedValue value && closedValues rest
end

/-- A checked statement must be a const whose initializer is in the closed
grammar, allocation-only in the shared CoreJS fragment, and has no binding
reads. -/
def closedConst (stmt : Stmt) : Bool :=
  match stmt with
  | .decl .const _ init => closedValue init && init.allocOnly && decide (init.readsNow = [])
  | _ => false

private theorem closedConst_facts {name : Name} {init : Expr}
    (accepted : closedConst (.decl .const name init) = true) :
    closedValue init = true ∧ init.allocOnly = true ∧ init.readsNow = [] := by
  simp only [closedConst, Bool.and_eq_true, decide_eq_true_eq] at accepted
  rcases accepted with ⟨⟨closed, allocOnly⟩, noReads⟩
  exact ⟨closed, allocOnly, noReads⟩

/-- One adjacent swap of distinct, closed const initializers. Other rows stay
in place and may have any syntax; the schedule theorem crosses only this pair. -/
inductive Trace : List (Nat × Stmt) → List (Nat × Stmt) → Prop where
  | refl (body) : Trace body body
  | swap (pre tail : List (Nat × Stmt)) (i j : Nat) (x y : Name) (a b : Expr) :
      i ≠ j →
      x ≠ y →
      closedConst (.decl .const x a) = true →
      closedConst (.decl .const y b) = true →
      Trace (pre ++ (i, .decl .const x a) :: (j, .decl .const y b) :: tail)
        (pre ++ (j, .decl .const y b) :: (i, .decl .const x a) :: tail)
  | trans {before middle after} : Trace before middle → Trace middle after → Trace before after

theorem Trace.perm {before after} (trace : Trace before after) : after.Perm before := by
  induction trace with
  | refl => exact .refl _
  | swap pre tail i j x y a b _ _ _ _ =>
    apply List.Perm.append_left
    exact .swap _ _ _
  | trans _ _ first second => exact second.trans first

private theorem scopeAddressFrame {module : Module} {frames : List (Nat × List (Name × Addr))}
    {name : Name} {address : Addr} (member : (name, address) ∈ module.scope frames) :
    ∃ id frame, frames.lookup id = some frame ∧ (name, address) ∈ frame := by
  obtain ⟨⟨id, _stmt⟩, _, found⟩ := List.mem_flatMap.1 member
  cases lookup : frames.lookup id with
  | none => simp [lookup] at found
  | some frame => exact ⟨id, frame, lookup, by simpa only [lookup, Option.getD_some] using found⟩

private theorem sourceScopeAddressRegion {W : World} {body : List (Nat × Stmt)} {B : EsmSplit.Bundle}
    {frames : List (Nat × List (Name × Addr))} (footprints : IndexedExportCells.Footprints body B)
    (frameNames : FramesOf body frames) (unique : EsmSplit.UniqueDefs B)
    {source : Module} (sourceBody : source.body = body)
    {id : Nat} {stmt : Stmt} {name : Name} {address : Addr}
    (row : body.lookup id = some stmt) (declared : name ∈ (B.stmt id).defs)
    {state ready : State W} (allocation : declareTops body state = .ok frames ready)
    (lookup : (source.scope frames).lookup name = some address) : address.region = id + 1 := by
  obtain ⟨frameId, frame, frameLookup, nameInFrame⟩ :=
    scopeAddressFrame (lookup_mem lookup)
  have nameInKeys : name ∈ (frameAt frames frameId).map Prod.fst := by
    change name ∈ ((frames.lookup frameId).getD []).map Prod.fst
    rw [frameLookup]
    exact List.mem_map.2 ⟨(name, address), nameInFrame, rfl⟩
  obtain ⟨other, otherRow, otherDeclared⟩ :=
    (IndexedExportCells.frame_keys footprints frameNames).1 nameInKeys
  have rowWithin := (footprints.statement id stmt row).1
  have otherWithin := (footprints.statement frameId other otherRow).1
  have sameId := CoreJS.unique_defs unique rowWithin otherWithin declared otherDeclared
  have region := CoreJS.declareTops_regions body allocation (frameId, frame)
    (lookup_mem frameLookup) (name, address) nameInFrame
  rw [sameId]
  simpa using region

private theorem traceCellFacts {W : World} {body : List (Nat × Stmt)} {B : EsmSplit.Bundle}
    {frames : List (Nat × List (Name × Addr))} (footprints : IndexedExportCells.Footprints body B)
    (frameNames : FramesOf body frames) (unique : EsmSplit.UniqueDefs B)
    {source : Module} (sourceBody : source.body = body)
    {id : Nat} {name : Name} {init : Expr} (member : (id, .decl .const name init) ∈ body)
    {state ready : State W} (allocation : declareTops body state = .ok frames ready) :
    ∃ address, (source.scope frames).lookup name = some address ∧ address.region = id + 1 := by
  have originalRow := IndexedExportCells.body_lookup footprints member
  have declared := ((footprints.statement id _ originalRow).2 name).2
    (by simp [Stmt.bindings, Stmt.varNames, varNamesOf, lexDecls])
  have memberSource : (id, .decl .const name init) ∈ source.body := by
    rw [sourceBody]
    exact lookup_mem originalRow
  obtain ⟨address, lookup, _⟩ := IndexedExportCells.source_and_module_cell
    footprints frameNames unique sourceBody originalRow memberSource declared
  exact ⟨address, lookup, sourceScopeAddressRegion footprints frameNames unique sourceBody
    originalRow declared allocation lookup⟩

/-- An accepted adjacent pair commutes in both directions for successful
executions. The proof retains the actual declaration cells in their original
regions and uses the common allocation-only frame theorem. Fuel exhaustion
and partial states are outside this relation. -/
theorem swap_successful (pre tail : List (Nat × Stmt)) (i j : Nat)
    (x y : Name) (a b : Expr) (differentIds : i ≠ j) (_differentNames : x ≠ y)
    (acceptedA : closedConst (.decl .const x a) = true)
    (acceptedB : closedConst (.decl .const y b) = true)
    (env : Env)
    (leftCell : ∀ c, env.vars.lookup x = some c → c.region = i + 1)
    (rightCell : ∀ c, env.vars.lookup y = some c → c.region = j + 1)
    (fuel : Nat) (start final : State W) :
    runUnits fuel (inScope env (pre ++ (i, .decl .const x a) :: (j, .decl .const y b) :: tail)) start =
        .ok () final ↔
      runUnits fuel (inScope env (pre ++ (j, .decl .const y b) :: (i, .decl .const x a) :: tail)) start =
        .ok () final := by
  have factsA := closedConst_facts acceptedA
  have factsB := closedConst_facts acceptedB
  have localA : Local (i + 1) (fun _ => False)
      (execStmt fuel env (.decl .const x a) : M W Flow) := by
    exact decl_local (e := a) factsA.2.1 fuel env .const x
      (by simp [Within, factsA.2.2]) leftCell
  have localB : Local (j + 1) (fun _ => False)
      (execStmt fuel env (.decl .const y b) : M W Flow) := by
    exact decl_local (e := b) factsB.2.1 fuel env .const y
      (by simp [Within, factsB.2.2]) rightCell
  have commute := local_units_commute differentIds localA localB
    (by intro c hc; contradiction) (by intro c hc; contradiction)
  have commuteReverse := local_units_commute differentIds.symm localB localA
    (by intro c hc; contradiction) (by intro c hc; contradiction)
  have pivot := runUnits_pivot fuel (inScope env pre)
      [(j, .decl .const y b, env)] (inScope env tail)
      (i, .decl .const x a, env) (fun _ => True) start final
      (by intro state _; trivial)
      (by intro _ _ _ _ _ _ _; trivial)
      (by
        intro unit member
        simp only [List.mem_singleton] at member
        subst unit
        exact ⟨(fun state _ => commute state), (fun state _ => commuteReverse state)⟩)
  simpa [inScope, List.map_append, Function.comp_def] using pivot

/-- A closed array declaration has no binding reads, and it initializes its
own cell in the source identity's region. An accepted trace therefore
preserves every successful final state for every world and fuel. -/
theorem Trace.successful {W : World} {before after : List (Nat × Stmt)} (trace : Trace before after)
    (env : Env)
    (cell : ∀ id name init, (id, .decl .const name init) ∈ before →
      ∃ address, env.vars.lookup name = some address ∧ address.region = id + 1)
    (fuel : Nat) (start final : State W) :
    runUnits fuel (inScope env before) start = .ok () final ↔
      runUnits fuel (inScope env after) start = .ok () final := by
  induction trace generalizing env fuel start final with
  | refl => exact Iff.rfl
  | swap pre tail i j x y a b differentIds differentNames acceptedA acceptedB =>
    have rowA : (i, .decl .const x a) ∈ pre ++ (i, .decl .const x a) :: (j, .decl .const y b) :: tail := by
      simp
    have rowB : (j, .decl .const y b) ∈ pre ++ (i, .decl .const x a) :: (j, .decl .const y b) :: tail := by
      simp
    obtain ⟨cellA, lookupA, regionA⟩ := cell i x a rowA
    obtain ⟨cellB, lookupB, regionB⟩ := cell j y b rowB
    have leftCell : ∀ address, env.vars.lookup x = some address → address.region = i + 1 := by
      intro address found
      have same : address = cellA := Option.some.inj (found.symm.trans lookupA)
      rw [same]
      exact regionA
    have rightCell : ∀ address, env.vars.lookup y = some address → address.region = j + 1 := by
      intro address found
      have same : address = cellB := Option.some.inj (found.symm.trans lookupB)
      rw [same]
      exact regionB
    simpa [inScope, List.map_append, Function.comp_def] using
      swap_successful pre tail i j x y a b differentIds differentNames
        acceptedA acceptedB env leftCell rightCell fuel start final
  | trans first second ihFirst ihSecond =>
    exact (ihFirst env cell fuel start final).trans
      (ihSecond env (fun id name init member => cell id name init (first.perm.mem_iff.mp member))
        fuel start final)

set_option linter.unusedVariables false in
/-- An indexed const schedule with closed array initializers preserves normal
completion. Source allocation, live scopes, exports, and statement identities
are the same ones used by the linked CoreJS programs. -/
theorem indexed_normal_of_schedule {B : EsmSplit.Bundle} {S : EsmSplit.Spec} {P : EsmSplit.Plan}
    (valid : EsmSplit.Valid B S P) {original : Program} {source : Module}
    (single : original.modules = [source]) (imports : source.imports = [])
    (found : original.find original.entry = some source)
    (footprints : IndexedExportCells.Footprints source.body B)
    (free : IndexedUnitsAgree.FreeFootprints source.body B)
    (complete : IndexedPlanCells.DefinitionsPresent source.body B)
    (ordered : source.body.Pairwise (fun a b => a.1 ≤ b.1))
    (consts : ∀ p ∈ source.body, ∃ name init, p.2 = .decl .const name init)
    {scheduled : List (Nat × Stmt)} (trace : Trace source.body scheduled)
    (ids : SparseProjectedOrder.executableIds source.body P = scheduled.map Prod.fst) :
    LinkedBehavior.ScheduleEquivalent .normalCompletion (fun _ => True)
      original (splitProgramIndexed source.body B P) := by
  have unitSchedule : ∀ {w} (links : IndexedReorder.ScheduleLinks (B := B) (P := P) original source scheduled w)
      (fuel : Nat) (final : State w),
      runUnits fuel (inScope links.env source.body) links.ready = .ok () final ↔
        runUnits fuel (inScope links.env scheduled) links.ready = .ok () final := by
    intro w links fuel final
    have cells : ∀ id name init, (id, .decl .const name init) ∈ source.body →
        ∃ address, links.env.vars.lookup name = some address ∧ address.region = id + 1 := by
      intro id name init member
      obtain ⟨address, lookup, region⟩ := traceCellFacts footprints
        (declareTops_names _ links.allocated) valid.1 rfl member links.allocated
      exact ⟨address, by simpa [links.envMatches] using lookup, region⟩
    exact trace.successful links.env cells fuel links.ready final
  exact IndexedReorder.normal_of_unit_schedule valid single imports found footprints free complete
    ordered consts trace.perm ids unitSchedule

/-- Check the requested executable order. This extension deliberately accepts
exactly one adjacent swap and retains source IDs and statements. -/
def check (body : List (Nat × Stmt)) (order : List Nat) :
    Except String (Σ scheduled : List (Nat × Stmt),
      PLift (Trace body scheduled ∧ order = scheduled.map Prod.fst)) := do
  let ids := body.map Prod.fst
  let some index := (ids.zip order).findIdx? (fun (before, after) => before != after) |
    throw "plan does not enumerate the original executable units"
  match remaining : body.drop index with
  | (i, .decl .const x a) :: (j, .decl .const y b) :: tail =>
    if differentIds : i ≠ j then
      if acceptedA : closedConst (.decl .const x a) = true then
        if acceptedB : closedConst (.decl .const y b) = true then
          if differentNames : x ≠ y then
            let pre := body.take index
            let scheduled := pre ++ (j, .decl .const y b) :: (i, .decl .const x a) :: tail
            if ordered : order = scheduled.map Prod.fst then
              have source : body = pre ++ (i, .decl .const x a) :: (j, .decl .const y b) :: tail := by
                calc
                  body = body.take index ++ body.drop index := (List.take_append_drop index body).symm
                  _ = pre ++ (i, .decl .const x a) :: (j, .decl .const y b) :: tail := by
                    simp only [pre, remaining]
              return ⟨scheduled, ⟨source ▸ Trace.swap pre tail i j x y a b
                differentIds differentNames acceptedA acceptedB, ordered⟩⟩
            else throw "plan is not the checked adjacent swap"
          else throw "moved declarations do not name distinct bindings"
        else throw "second moved initializer is outside the closed array fragment"
      else throw "first moved initializer is outside the closed array fragment"
    else throw "moved declarations have a duplicate source identity"
  | _ => throw "the changed order does not start with two adjacent closed const declarations"

/-- A property read cannot enter the closed initializer grammar. -/
theorem refuses_property_read :
    closedConst (.decl .const "rows" (.arr [.get (.var "table") (.str "value")])) = false := by
  decide

/-- Binding aliases are not closed array leaves. -/
theorem refuses_alias :
    closedConst (.decl .const "rows" (.arr [.var "shared"])) = false := by
  decide

end CoreJS.ClosedArraySchedule
