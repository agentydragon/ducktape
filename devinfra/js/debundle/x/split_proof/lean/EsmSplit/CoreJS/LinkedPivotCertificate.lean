import EsmSplit.CoreJS.HiddenRegion
import EsmSplit.CoreJS.LinkedPivotCheck
import EsmSplit.CoreJS.LinkWorld
import EsmSplit.CoreJS.StateMatch

namespace CoreJS.LinkedBehavior

private theorem runUnits_move {W : World} {ready : State W} {pre crossed suffix pivot}
    (n : Nat) (final : State W) (checks : CheckedPivot ready pre crossed pivot) :
    runUnits n (pre ++ crossed ++ [pivot] ++ suffix) ready = .ok () final ↔
      runUnits n (pre ++ [pivot] ++ crossed ++ suffix) ready = .ok () final := by
  refine runUnits_pivot_normal n final (fun _ => False) _ checks.access
    (localPivotOk_sound checks.localFlag n) ?_
  intro u hu
  obtain ⟨away, scope⟩ := checks.scope u hu
  refine ⟨fun eq => away (congrArg (· + 1) eq), [], by simp, ?_⟩
  rw [← capture_faithful n u.2.2 u.2.1 u.2.1.free (fun _ h => h)]
  have frames := evaluatorFrames W (r := pivot.1 + 1) (by omega)
    (R := fun _ => False) (fun _ _ h => h) n
  have step := frames.stmt [] (u.2.2.capture u.2.1.free) u.2.1 scope
  refine ⟨step.replace, step.preserves, ?_⟩
  intro s access away
  have result := step.accessible s access away
  cases h : execStmt n (u.2.2.capture u.2.1.free) u.2.1 s <;> rw [h] at result
  case ok v t => exact ⟨trivial, result.2⟩
  all_goals exact result

private def CheckedLink.forWorld {p : Program} (checked : CheckedLink p World.probe) (w : World) :
    CheckedLink p w :=
  ⟨checked.linked, linkState w checked.ready, link_from_probe checked w⟩

private def checkLink (p : Program) : Except String (CheckedLink p World.probe) :=
  match h : link p (initialState World.probe p.regions) with
  | .ok linked ready => .ok ⟨linked, ready, h⟩
  | .unknown reason _ => .error reason
  | .throw _ _ => .error "the module graph threw during linking"
  | .fuel => .error "linking exhausted fuel"

private theorem Access.linkState {w cells A} {s : State World.probe} (access : Access cells A s) :
    Access cells A (linkState w s) := by
  refine ⟨access.handed, access.supplied, ?_⟩
  intro src dst hsrc edge
  cases edge with
  | obj ho hm => exact access.closed _ _ hsrc (.obj ho hm)
  | cell hc hm => exact access.closed _ _ hsrc (.cell hc hm)

private abbrev LinkedUnit := Nat × Stmt × Env

private theorem CheckedPivot.forWorld {ready pre crossed pivot}
    (checks : CheckedPivot ready pre crossed pivot) (w : World) :
    CheckedPivot (linkState w ready) pre crossed pivot :=
  ⟨Access.linkState checks.access, checks.localFlag, checks.scope⟩

private theorem swapLocal (n : Nat) (s final : State w) (pre suffix : List LinkedUnit) {a b : LinkedUnit}
    (diff : a.1 ≠ b.1) (ha : localPivotOk a = true) (hb : localPivotOk b = true) :
    runUnits n (pre ++ [a, b] ++ suffix) s = .ok () final ↔
      runUnits n (pre ++ [b, a] ++ suffix) s = .ok () final := by
  have swap := runUnits_pivot n pre [b] suffix a (fun _ => True) s final
    (fun _ _ => trivial) (fun _ _ _ _ _ _ _ => trivial)
  simp only [List.append_assoc, List.cons_append, List.nil_append] at swap ⊢
  apply swap
  intro u hu
  obtain rfl := List.mem_singleton.mp hu
  have aLocal := localPivotOk_sound (W := w) ha n
  have bLocal := localPivotOk_sound (W := w) hb n
  exact ⟨fun t _ => local_units_commute diff aLocal bLocal nofun nofun t,
    fun t _ => local_units_commute diff.symm bLocal aLocal nofun nofun t⟩

/-- Move adjacent declarations together when the first captures the second. In the
proof schedule the second moves first, before the first can publish its capture.
Both moves across arbitrary code still require the full linked heap frame. -/
private theorem movePair (n : Nat) (final : State w) {ready : State w} {pre crossed suffix : List LinkedUnit}
    {a b : LinkedUnit} (diff : a.1 ≠ b.1)
    (second : CheckedPivot ready pre crossed b) (first : CheckedPivot ready (pre ++ [b]) crossed a) :
    runUnits n (pre ++ crossed ++ [a, b] ++ suffix) ready = .ok () final ↔
      runUnits n (pre ++ [a, b] ++ crossed ++ suffix) ready = .ok () final := by
  have swapBefore := swapLocal n ready final (pre ++ crossed) suffix diff first.localFlag second.localFlag
  have moveSecond := runUnits_move (suffix := a :: suffix) n final second
  have moveFirst := runUnits_move (suffix := suffix) n final first
  have swapAfter := swapLocal n ready final pre (crossed ++ suffix) diff first.localFlag second.localFlag
  simp only [List.append_assoc, List.cons_append, List.nil_append] at *
  exact swapBefore.trans (moveSecond.trans (moveFirst.trans swapAfter.symm))

private abbrev original (units : List PreparedUnit) := units.map PreparedUnit.unit

private def checkPair? (ready : State World.probe) (pre crossed : List PreparedUnit) (a b : PreparedUnit) :
    Option (PLift (a.unit.1 ≠ b.unit.1 ∧ CheckedPivot ready (original pre) (original crossed) b.unit ∧
      CheckedPivot ready (original pre ++ [b.unit]) (original crossed) a.unit)) := do
  -- Only try this proof order when a has a reference into b's region.
  guard (a.unit.2.1.free.any fun x => (a.unit.2.2.vars.lookup x).any (fun cell => cell.region == b.unit.1 + 1))
  if diff : a.unit.1 ≠ b.unit.1 then
    let ⟨second⟩ ← (checkPivot ready pre crossed b.unit).toOption
    let ⟨first⟩ ← (checkPivot ready (pre ++ [b]) crossed a.unit).toOption
    return ⟨diff, second, by simpa [original] using first⟩
  else none

private structure CheckedSchedule (ready : State World.probe) (source : List PreparedUnit) where
  units : List LinkedUnit
  moves : Nat
  equivalent : ∀ w n (final : State w),
    runUnits n (source.map (·.unit)) (linkState w ready) = .ok () final ↔
      runUnits n units (linkState w ready) = .ok () final

private def checkHiddenMove? (ready : State World.probe) (pre crossed suffix : List PreparedUnit) (pivot : PreparedUnit) :
    Option (PLift (∀ w n (final : State w),
      runUnits n (original (pre ++ crossed ++ [pivot] ++ suffix)) (linkState w ready) = .ok () final ↔
        runUnits n (original (pre ++ [pivot] ++ crossed ++ suffix)) (linkState w ready) = .ok () final)) := do
  let q ← observerRegion? ready (pivot.unit.1 + 1)
  let masked := ready.put q #[] #[]
  let ⟨hq, access, scope⟩ ← checkIsolated? masked q (pre ++ crossed ++ [pivot])
  let ⟨checks⟩ ← (checkPivot masked pre crossed pivot.unit).toOption
  return ⟨by
    intro w n final
    simp only [original, List.map_append, List.map_cons, List.map_nil] at scope ⊢
    let H := ready.heap[q]?.getD #[]
    let C := ready.cells[q]?.getD #[]
    have restore : (linkState w masked).put q H C = linkState w ready := by
      change linkState w ((ready.put q #[] #[]).put q H C) = _
      rw [State.put_put]
      simp only [State.put, H, C, set!_same rfl]
    have reordered : ∀ u ∈ original pre ++ [pivot.unit] ++ original crossed, IsolatedUnit q u := by
      intro u hu
      apply scope
      simpa only [List.mem_append, List.mem_singleton, or_assoc, or_left_comm, or_comm] using hu
    rw [← restore, runUnits_restore hq n _ (original suffix) scope _ _ (Access.linkState access) H C,
      runUnits_restore hq n _ (original suffix) reordered _ _ (Access.linkState access) H C]
    apply exists_congr
    intro t
    apply and_congr_left
    intro _
    simpa only [List.append_nil] using runUnits_move (suffix := []) n t
      (⟨Access.linkState checks.access, checks.localFlag, checks.scope⟩ :
        CheckedPivot (linkState w masked) (original pre) (original crossed) pivot.unit)⟩

/-- Instantiation has already initialized hoisted functions in both links. For an
inert body step, even a crossed unit that calls the function commutes with it.
An allocating initializer still needs the region and capture checks. -/
private def checkMove (ready : State World.probe) (pre crossed suffix : List PreparedUnit) (pivot : PreparedUnit) :
    Except String (PLift (∀ w n (final : State w),
      runUnits n ((pre ++ crossed ++ [pivot] ++ suffix).map (·.unit)) (linkState w ready) = .ok () final ↔
        runUnits n ((pre ++ [pivot] ++ crossed ++ suffix).map (·.unit)) (linkState w ready) = .ok () final)) := do
  if inert : pivot.unit.2.1.inert = true then
    return ⟨by
      intro w n final
      simp only [List.map_append, List.map_cons, List.map_nil]
      apply Iff.symm
      apply runUnits_pivot n _ _ _ pivot.unit (fun _ => True) _ _
        (fun _ _ => trivial) (fun _ _ _ _ _ _ _ => trivial)
      intro u _
      exact ⟨fun s _ => inert_commute inert s, fun s _ => commute_inert inert s⟩⟩
  match checkPivot ready pre crossed pivot.unit with
  | .ok ⟨checks⟩ => return ⟨by
      intro w n final
      simpa only [List.map_append, List.map_cons, List.map_nil] using
        runUnits_move (suffix := original suffix) n final (checks.forWorld w)⟩
  | .error why =>
    let some proof := checkHiddenMove? ready pre crossed suffix pivot | throw why
    return proof

/-- Match the requested order from left to right. Each move is checked against
the prefix already placed, including units moved by previous steps. -/
private def checkSchedule (ready : State World.probe) (prefixRev remaining : List PreparedUnit) :
    List Nat → Except String (CheckedSchedule ready (prefixRev.reverse ++ remaining))
  | [] => do
    unless remaining.isEmpty do throw "the candidate schedule omits linked units"
    return ⟨original (prefixRev.reverse ++ remaining), 0, fun _ _ _ => Iff.rfl⟩
  | id :: rest => do
    match h : remaining with
    | u :: tail =>
      if u.unit.1 == id then
        let next ← checkSchedule ready (u :: prefixRev) tail rest
        return ⟨next.units, next.moves, by
          simpa only [h, List.reverse_cons, List.append_assoc, List.singleton_append] using next.equivalent⟩
    | [] => pure ()
    let crossed := remaining.takeWhile (fun u => u.unit.1 != id)
    match h : remaining.dropWhile (fun u => u.unit.1 != id) with
    | [] => throw s!"unit {id} is missing from the remaining linked schedule"
    | pivot :: suffix =>
      have split : remaining = crossed ++ pivot :: suffix := by
        have joined := List.takeWhile_append_dropWhile (p := fun u : PreparedUnit => u.unit.1 != id) (l := remaining)
        rw [h] at joined
        exact joined.symm
      -- Try the reversed proof order only for an adjacent forward reference.
      match rest, hs : suffix with
      | nextId :: rest', b :: tail =>
        if b.unit.1 == nextId then
          if let some ⟨diff, second, first⟩ := checkPair? ready prefixRev.reverse crossed pivot b then
            let next ← checkSchedule ready (b :: pivot :: prefixRev) (crossed ++ tail) rest'
            return ⟨next.units, next.moves + 2, by
              intro w n final
              have pair := movePair (suffix := original tail) n final diff (second.forWorld w) (first.forWorld w)
              have after := next.equivalent w n final
              simp only [original, List.map_append, List.map_cons, split, hs, List.reverse_cons,
                List.append_assoc, List.cons_append, List.nil_append]
                at pair after ⊢
              exact pair.trans after⟩
      | _, _ => pure ()
      let ⟨move⟩ ← checkMove ready prefixRev.reverse crossed suffix pivot
      let next ← checkSchedule ready (pivot :: prefixRev) (crossed ++ suffix) rest
      return ⟨next.units, next.moves + 1, by
        intro w n final
        rw [split]
        have step := move w n final
        have tail := next.equivalent w n final
        simp only [List.reverse_cons, List.append_assoc, List.singleton_append] at step tail ⊢
        exact step.trans tail⟩

inductive StructuralProof where
  | preservedOrder
  | singlePivot
  | multiplePivots

/-- Select the rule from the linked schedules, then validate the witness in all worlds. -/
def checkStructuralSchedule (source candidate : Program) :
    Except String (StructuralProof × PLift (NormalEquivalent source candidate ∧ SameExports source candidate)) := do
  let before ← (checkLink source).mapError ("original linker: " ++ ·)
  let after ← (checkLink candidate).mapError ("candidate linker: " ++ ·)
  let some ⟨initial⟩ := StateMatch.probe before.ready after.ready |
    throw "the linked initial states differ"
  let ⟨exportsEqual⟩ ←
    if checked : exportsMatchFrom before after = true then pure (PLift.up checked)
    else do
      let a ← (EntryExports.resolve source.entry before.linked).mapError ("original entry: " ++ ·)
      let b ← (EntryExports.resolve candidate.entry after.linked).mapError ("candidate entry: " ++ ·)
      throw (EntryExports.mismatch a b)
  have sameExports : SameExports source candidate := sameExports_of_match fun w => by
    rw [exportsMatch_from_checked (before.forWorld w) (after.forWorld w)]
    exact exportsEqual
  let sourceUnits := unitsOf before.linked
  let candidateUnits := unitsOf after.linked
  let schedule ← checkSchedule before.ready [] (sourceUnits.map PreparedUnit.ofUnit) (candidateUnits.map (·.1))
  let some ⟨same⟩ := sameUnits? schedule.units candidateUnits |
    throw "the linked schedules differ in unit order, syntax, or scope"
  have equivalent : NormalEquivalent source candidate := normalEquivalent_of_bounded fun w n final => by
    rw [run_after_link n (before.forWorld w), run_after_link n (after.forWorld w)]
    change runUnits n sourceUnits (linkState w before.ready) = .ok () final ↔
      runUnits n candidateUnits (linkState w after.ready) = .ok () final
    rw [← runUnits_same n same, ← congrArg (linkState w) initial]
    simpa [List.map_map, Function.comp_def, PreparedUnit.ofUnit] using schedule.equivalent w n final
  let method := match schedule.moves with
    | 0 => .preservedOrder
    | 1 => .singlePivot
    | _ => .multiplePivots
  return (method, ⟨⟨equivalent, sameExports⟩⟩)

end CoreJS.LinkedBehavior
