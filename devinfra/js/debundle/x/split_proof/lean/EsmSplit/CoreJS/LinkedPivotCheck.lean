import EsmSplit.CoreJS.LinkedPivot
import EsmSplit.CoreJS.RequestAllocation
import EsmSplit.CoreJS.SyntaxMatch

/-! Check the actual linked schedule, its free-name cells, and the frame conditions
for moving one allocating unit earlier. The evaluator supplies the generic
framing theorem separately. -/

namespace CoreJS.LinkedBehavior

private def inRegion (region : Nat) (env : Env) (name : Name) : Bool :=
  (env.vars.lookup name).all fun a => a.region == region

private theorem inRegion_owns {region env name} (h : inRegion region env name = true)
    {c : Addr} (found : env.vars.lookup name = some c) : c.region = region := by
  simpa [inRegion, found] using h

/-- A declaration can be the pivot if its initializer only allocates and its
writes and currently read cells lie in its own region. A retained opaque
closure declaration has the same property when its written cells are local. -/
def localPivotOk (pivot : Nat × Stmt × Env) : Bool :=
  let owns := inRegion (pivot.1 + 1) pivot.2.2
  match pivot.2.1 with
  | .decl _ name e => e.allocOnly && owns name && e.readsNow.all owns
  | .opaque _ decls _ _ (.closures _) => decls.all (owns ·.1)
  | _ => false

theorem localPivotOk_sound {W : World} {pivot : Nat × Stmt × Env} (ok : localPivotOk pivot = true)
    (fuel : Nat) :
    Local (pivot.1 + 1) (fun _ => False) (execStmt fuel pivot.2.2 pivot.2.1 : M W Flow) := by
  obtain ⟨id, stmt, env⟩ := pivot
  cases stmt <;> simp only [localPivotOk] at ok
  case decl kind name initial =>
    simp only [Bool.and_eq_true, List.all_eq_true] at ok
    refine decl_local ok.1.1 fuel env kind name ?_ (fun _ h => inRegion_owns ok.1.2 h)
    intro name known a found
    exact Or.inr (inRegion_owns (ok.2 name known) found)
  case «opaque» code decls funs free action =>
    cases action with
    | world => contradiction
    | closures fns =>
      simp only [] at ok
      apply closures_local
      intro name hn c found
      obtain ⟨decl, member, rfl⟩ := List.mem_map.mp hn
      exact inRegion_owns (List.all_eq_true.mp ok _ member) found
  all_goals contradiction

private def outsideRegion (r : Nat) (node : HeapNode) : Bool := node.addr.region != r

private theorem outsideRegion_iff (r : Nat) (node : HeapNode) :
    outsideRegion r node = true ↔ Unprotected r (fun _ => False) node := by
  cases node <;> simp [outsideRegion, HeapNode.addr, Unprotected]

/-- Check only the scope retained by execution of this statement. The full
module environment may contain a binding that belongs to the moved unit. -/
private def unitEnvOk (r : Nat) (u : Nat × Stmt × Env) : Bool :=
  u.1 + 1 != r &&
  u.2.2.this.nodes.all (outsideRegion r) &&
  u.2.1.free.all (fun name => (u.2.2.vars.lookup name).all (fun a => outsideRegion r (.cell a)))

private theorem unitEnvOk_sound {r} {u : Nat × Stmt × Env} (ok : unitEnvOk r u = true) :
    u.1 + 1 ≠ r ∧ ∀ node ∈ (u.2.2.capture u.2.1.free).nodes, Unprotected r (fun _ => False) node := by
  simp only [unitEnvOk, Bool.and_eq_true, List.all_eq_true] at ok
  refine ⟨by simpa using ok.1.1, ?_⟩
  intro node h
  apply (outsideRegion_iff r node).mp
  rcases List.mem_append.mp h with captures | recv
  · obtain ⟨p, hp, rfl⟩ := List.mem_map.mp captures
    obtain ⟨name, named, found⟩ := List.mem_filterMap.mp hp
    have free := List.mem_eraseDups.mp named
    cases lookup : u.2.2.vars.lookup name with
    | none => simp [lookup] at found
    | some a =>
      simp only [lookup, Option.map_some, Option.some.injEq] at found
      subst p
      simpa [lookup] using ok.2 name free
  · exact ok.1.2 node recv

private theorem accessOk_outsideRegion {r : Nat} {W : World} {s : State W} {cells : List (Option Addr)}
    (ok : accessOk s cells (outsideRegion r) = true) : Access cells (Unprotected r (fun _ => False)) s := by
  have allowed : (fun node => outsideRegion r node = true) = Unprotected r (fun _ => False) :=
    funext fun n => propext (outsideRegion_iff r n)
  exact allowed ▸ accessOk_iff.mp ok


private def isModuleReceiver : Val → Bool
  | .undef => true
  | _ => false

private theorem isModuleReceiver_eq {v : Val} (h : isModuleReceiver v = true) : v = .undef := by
  cases v <;> first | rfl | contradiction

private def sameUnit? (u v : Nat × Stmt × Env) :
    Option (PLift (SameUnit u v)) := do
  let tag ← if h : u.1 = v.1 then some (PLift.up h) else none
  let st ← SyntaxMatch.stmtEq u.2.1 v.2.1
  let receiver ← if h : isModuleReceiver u.2.2.this && isModuleReceiver v.2.2.this then
    have both : isModuleReceiver u.2.2.this = true ∧ isModuleReceiver v.2.2.this = true := by
      simpa only [Bool.and_eq_true] using h
    some (PLift.up ((isModuleReceiver_eq both.1).trans (isModuleReceiver_eq both.2).symm))
    else none
  if names : u.2.1.free.all (fun x => u.2.2.vars.lookup x == v.2.2.vars.lookup x) then
    return ⟨tag.down, st.down, ⟨receiver.down, fun x hx => beq_iff_eq.1 (List.all_eq_true.mp names x hx)⟩⟩
  else none

def sameUnits? : (u v : List (Nat × Stmt × Env)) → Option (PLift (SameUnits u v))
  | [], [] => some ⟨.nil⟩
  | x :: xs, y :: ys => do
    let head ← sameUnit? x y
    let tail ← sameUnits? xs ys
    return ⟨.cons head.down tail.down⟩
  | _, _ => none

/-- Recover the first differing unit, then verify that the candidate inserts
that unit before one whole block. The checker compares both actual sequences,
including every free-name cell in each unit's linked environment. -/
private def pivotLayout? {source candidate : Program} {w : World}
    (sourceLink : CheckedLink source w) (candidateLink : CheckedLink candidate w)
    (initial : sourceLink.ready = candidateLink.ready) : Option (PivotLayout source candidate w) := do
  let sourceUnits := unitsOf sourceLink.linked
  let candidateUnits := unitsOf candidateLink.linked
  let shared := ((sourceUnits.zip candidateUnits).takeWhile fun (a, b) => a.1 == b.1).length
  let pre := sourceUnits.take shared
  let originalTail := sourceUnits.drop shared
  let moved ← (candidateUnits.drop shared).head?
  let crossed := originalTail.takeWhile (fun u => u.1 != moved.1)
  guard !crossed.isEmpty
  let pivot :: suffix := originalTail.drop crossed.length | none
  let sourceOrder ← sameUnits? sourceUnits (pre ++ crossed ++ [pivot] ++ suffix)
  let candidateOrder ← sameUnits? candidateUnits (pre ++ [pivot] ++ crossed ++ suffix)
  return {
    sourceLink
    candidateLink
    initial
    pre
    crossed
    pivot
    suffix
    sourceOrder := sourceOrder.down
    candidateOrder := candidateOrder.down
  }


/-- The concrete checks needed by the normal-outcome theorem, independently of
the proof that execution itself preserves the reachable heap. -/
structure CheckedPivot (source candidate : Program) (w : World) where
  layout : PivotLayout source candidate w
  access : Access [] (Unprotected (layout.pivot.1 + 1) (fun _ => False)) layout.sourceLink.ready
  localFlag : localPivotOk layout.pivot = true
  scope : ∀ u ∈ layout.pre ++ layout.crossed, u.1 + 1 ≠ layout.pivot.1 + 1 ∧
    ∀ node ∈ (u.2.2.capture u.2.1.free).nodes, Unprotected (layout.pivot.1 + 1) (fun _ => False) node

/-- Find and validate the intended single earlier move. The link equations and
initial-state equality are already kernel-checked by the caller. -/
def checkPivot {source candidate : Program} {w : World} (ordinal : Nat)
    (sourceLink : CheckedLink source w) (candidateLink : CheckedLink candidate w)
    (initial : sourceLink.ready = candidateLink.ready) : Except String (CheckedPivot source candidate w) := do
  let some layout := pivotLayout? sourceLink candidateLink initial |
    throw "the linked schedules do not differ by a single earlier move"
  unless layout.pivot.1 == ordinal do
    throw s!"expected to move unit {ordinal}; the linked pivot is {layout.pivot.1}"
  let r := layout.pivot.1 + 1
  if localStep : localPivotOk layout.pivot = true then
    if accessible : accessOk layout.sourceLink.ready [] (outsideRegion r) = true then
      if unchanged : (layout.pre ++ layout.crossed).all (unitEnvOk r) = true then
        return {
          layout
          access := accessOk_outsideRegion accessible
          localFlag := localStep
          scope := fun u hu => unitEnvOk_sound (List.all_eq_true.mp unchanged u hu)
        }
      else
        let offender := (layout.pre ++ layout.crossed).find? (fun u => !(unitEnvOk r u))
        throw s!"unit {(offender.map (·.1)).getD ordinal} has access to the moved region"
    else throw s!"the linked state already exposes the region of unit {ordinal}"
  else throw s!"unit {ordinal} is not a local allocating declaration"

end CoreJS.LinkedBehavior
