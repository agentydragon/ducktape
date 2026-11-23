import EsmSplit.CoreJS.LinkedBehavior
import EsmSplit.CoreJS.Move
import EsmSplit.CoreJS.LiteralRecord

namespace CoreJS.LinkedBehavior

theorem literalReads (xs : List (String × String)) : readsNowProps (xs.map literalField) = [] := by
  induction xs with
  | nil => rfl
  | cons x xs ih => simpa only [List.map_cons, readsNowProps, literalField, Expr.readsNow, List.nil_append] using ih

theorem literalAllocs (xs : List (String × String)) : allocOnlyProps (xs.map literalField) = true := by
  induction xs with
  | nil => rfl
  | cons x xs ih => simpa only [List.map_cons, allocOnlyProps, literalField, Expr.allocOnly, Bool.true_and] using ih
private abbrev LinkedUnit := Nat × Stmt × Env

/-- The declaration initializes a cell in its own region and cannot read a cell
in the other unit's region, even through another local name for the same cell. -/
private def localAway (u : LinkedUnit) (other : Nat) : Bool :=
  match u.2.1 with
  | .decl _ x e => e.allocOnly &&
      (u.2.2.vars.lookup x).all (fun a => a.region == u.1 + 1) &&
      e.readsNow.all (fun y => (u.2.2.vars.lookup y).all (fun a => a.region != other + 1))
  | _ => false

private theorem localAway_sound {u : LinkedUnit} {other : Nat} (ok : localAway u other = true)
    (fuel : Nat) : Local (u.1 + 1) (fun a => a.region ≠ other + 1)
      (execStmt fuel u.2.2 u.2.1 : M w Flow) := by
  obtain ⟨id, stmt, env⟩ := u
  cases stmt <;> simp only [localAway] at ok
  case decl kind name initial =>
    simp only [Bool.and_eq_true, List.all_eq_true] at ok
    apply decl_local ok.1.1
    · intro x hx a found
      exact Or.inl (by simpa [found] using ok.2 x hx)
    · intro c found
      simpa [found] using ok.1.2
  all_goals contradiction

private def commutes (a b : LinkedUnit) : Bool :=
  a.2.1.inert || b.2.1.inert || (a.1 != b.1 && localAway a b.1 && localAway b a.1)

private theorem commutes_sound {a b : LinkedUnit} (ok : commutes a b = true) (fuel : Nat) :
    Commute (unitStep fuel a : M w Flow) (unitStep fuel b) ∧
    Commute (unitStep fuel b : M w Flow) (unitStep fuel a) := by
  simp only [commutes, Bool.or_eq_true, Bool.and_eq_true, bne_iff_ne] at ok
  rcases ok with (h | h) | ⟨⟨different, ha⟩, hb⟩
  · exact ⟨inert_commute h, commute_inert h⟩
  · exact ⟨commute_inert h, inert_commute h⟩
  · exact ⟨local_units_commute different (localAway_sound ha fuel) (localAway_sound hb fuel)
        (fun _ h => h) (fun _ h => h),
      local_units_commute different.symm (localAway_sound hb fuel) (localAway_sound ha fuel)
        (fun _ h => h) (fun _ h => h)⟩

/-- Try moving one linked unit earlier, across exactly `width` units after
the common prefix. Both the check and the permutation use the actual scopes. -/
def moveUnitEarlier? (start width : Nat) (us : List LinkedUnit) : Option (List LinkedUnit) := do
  let tail := us.drop start
  let crossed := tail.take width
  let pivot :: suffix := tail.drop width | none
  if crossed.all (commutes pivot) then
    return us.take start ++ [pivot] ++ crossed ++ suffix
  else none

theorem moveUnitEarlier_ok {start width : Nat} {us vs : List LinkedUnit}
    (move : moveUnitEarlier? start width us = some vs) (fuel : Nat) (s final : State w) :
    runUnits fuel us s = .ok () final ↔ runUnits fuel vs s = .ok () final := by
  cases eq : (us.drop start).drop width with
  | nil => simp [moveUnitEarlier?, eq] at move
  | cons pivot suffix =>
    generalize hc : (us.drop start).take width = crossed at *
    by_cases ok : crossed.all (commutes pivot) = true
    · have changed : us.take start ++ [pivot] ++ crossed ++ suffix = vs := by
        simpa [moveUnitEarlier?, eq, hc, ok] using move
      subst vs
      have orig : us.take start ++ crossed ++ [pivot] ++ suffix = us := by
        rw [List.append_assoc, List.append_assoc]
        simp only [List.singleton_append, ← eq, ← hc, List.take_append_drop]
      conv => lhs; rw [← orig]
      apply Iff.symm
      apply runUnits_pivot (P := fun _ => True)
      · intros; trivial
      · intros; trivial
      · intro u hu
        obtain ⟨ab, ba⟩ := commutes_sound (w := w) (List.all_eq_true.mp ok u hu) fuel
        exact ⟨fun t _ => ab t, fun t _ => ba t⟩
    · simp [moveUnitEarlier?, eq, hc, ok] at move

/-- The candidate reaches the source's final state without evaluating the common
units again. The caller supplies the source's normal completion separately. -/
theorem run_ok_of_moved_link {source candidate : Program} {w : World} {start width fuel : Nat}
    (sourceLink : CheckedLink source w) (candidateLink : CheckedLink candidate w)
    (ready : sourceLink.ready = candidateLink.ready)
    (schedule : moveUnitEarlier? start width (captureUnits (unitsOf sourceLink.linked)) =
      some (captureUnits (unitsOf candidateLink.linked))) {final : State w}
    (sourceRun : runProgram w fuel source = .ok () final) : runProgram w fuel candidate = .ok () final := by
  rw [run_after_link fuel sourceLink, ← runUnits_capture fuel (unitsOf sourceLink.linked), ready] at sourceRun
  rw [run_after_link fuel candidateLink, ← runUnits_capture fuel (unitsOf candidateLink.linked)]
  exact (moveUnitEarlier_ok schedule fuel candidateLink.ready final).mp sourceRun

/-- Select an earlier move on the concrete probe link. This is a producer hint.
The generated theorem repeats the schedule and commutation checks for every world. -/
def earlierMove? (source candidate : Program) : Option (Nat × Nat) := do
  let before := source.loaded.flatMap fun m => m.body.map Prod.fst
  let after := candidate.loaded.flatMap fun m => m.body.map Prod.fst
  let start := ((before.zip after).takeWhile fun p => p.1 == p.2).length
  let moved ← after[start]?
  let tail := before.drop start
  let width := tail.idxOf moved
  guard (before.take start ++ [moved] ++ tail.take width ++ tail.drop (width + 1) == after)
  let .ok linked _ := link source (initialState World.probe source.regions) | none
  let reordered ← moveUnitEarlier? start width (unitsOf linked)
  guard (reordered.map Prod.fst == after)
  return (start, width)

end CoreJS.LinkedBehavior
