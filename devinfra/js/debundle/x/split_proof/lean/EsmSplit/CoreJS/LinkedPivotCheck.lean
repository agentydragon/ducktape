import EsmSplit.CoreJS.LinkedPivot
import EsmSplit.CoreJS.Request.Allocation
import EsmSplit.CoreJS.SyntaxMatch

/-! Compare linked units and check the frame conditions
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

/-- The frame checks for moving a unit ahead of a block in the current
schedule. The prefix must also preserve the frame from the initial linked state. -/
structure CheckedPivot {w : World} (ready : State w)
    (pre crossed : List (Nat × Stmt × Env)) (pivot : Nat × Stmt × Env) : Prop where
  access : Access [] (Unprotected (pivot.1 + 1) (fun _ => False)) ready
  localFlag : localPivotOk pivot = true
  scope : ∀ u ∈ pre ++ crossed, u.1 + 1 ≠ pivot.1 + 1 ∧
    ∀ node ∈ (u.2.2.capture u.2.1.free).nodes, Unprotected (pivot.1 + 1) (fun _ => False) node

def checkPivot {w : World} (ready : State w)
    (pre crossed : List (Nat × Stmt × Env)) (pivot : Nat × Stmt × Env) :
    Except String (PLift (CheckedPivot ready pre crossed pivot)) := do
  let ordinal := pivot.1
  let r := ordinal + 1
  if localStep : localPivotOk pivot = true then
    if accessible : accessOk ready [] (outsideRegion r) = true then
      if unchanged : (pre ++ crossed).all (unitEnvOk r) = true then
        return ⟨{
          access := accessOk_outsideRegion accessible
          localFlag := localStep
          scope := fun u hu => unitEnvOk_sound (List.all_eq_true.mp unchanged u hu)
        }⟩
      else
        let offender := (pre ++ crossed).find? (fun u => !(unitEnvOk r u))
        throw s!"unit {(offender.map (·.1)).getD ordinal} has access to the moved region of unit {ordinal}"
    else throw s!"the linked state already exposes the region of unit {ordinal}"
  else throw s!"unit {ordinal} is not a local allocating declaration"

/-- Find a single object region that references the pivot. This is only a
candidate: both the isolated region and the pivot must pass the full checks. -/
def observerRegion? {w} (ready : State w) (r : Nat) : Option Nat :=
  (ready.heap.zipIdx.find? fun (row, q) =>
    q != r && row.any (fun o => o.nodes.any (fun n => n.addr.region == r))).map Prod.snd

def checkIsolated? {w} (s : State w) (q : Nat) (units : List (Nat × Stmt × Env)) :
    Option (PLift (q ≠ 0 ∧ Access [] (Unprotected q (fun _ => False)) s ∧
      ∀ u ∈ units, u.1 + 1 ≠ q ∧ ∀ node ∈ (u.2.2.capture u.2.1.free).nodes, Unprotected q (fun _ => False) node)) := do
  if hq : q ≠ 0 then
    if access : accessOk s [] (outsideRegion q) = true then
      if scopes : units.all (unitEnvOk q) = true then
        return ⟨hq, accessOk_outsideRegion access, fun u hu => unitEnvOk_sound (List.all_eq_true.mp scopes u hu)⟩
  none

end CoreJS.LinkedBehavior
