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

/-- The schedule reuses this exact capture for every pivot crossing the unit.
A unit never crossed by an allocating pivot need not resolve its free names.
The original unit (including its full scope) still runs in the certificate. -/
structure PreparedUnit where
  unit : Nat × Stmt × Env
  nodes : Thunk (List HeapNode)
  exactNodes : nodes.get = (unit.2.2.capture unit.2.1.free).nodes

def PreparedUnit.ofUnit (u : Nat × Stmt × Env) : PreparedUnit :=
  ⟨u, Thunk.mk (fun _ => (u.2.2.capture u.2.1.free).nodes), rfl⟩

private def unitEnvOk (r : Nat) (u : PreparedUnit) : Bool :=
  u.unit.1 + 1 != r && u.nodes.get.all (outsideRegion r)

private theorem unitEnvOk_sound {r} {u : PreparedUnit} (ok : unitEnvOk r u = true) :
    u.unit.1 + 1 ≠ r ∧ ∀ node ∈ (u.unit.2.2.capture u.unit.2.1.free).nodes, Unprotected r (fun _ => False) node := by
  simpa only [unitEnvOk, Bool.and_eq_true, bne_iff_ne, u.exactNodes, List.all_eq_true, outsideRegion_iff] using ok

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
    (pre crossed : List PreparedUnit) (pivot : Nat × Stmt × Env) :
    Except String (PLift (CheckedPivot ready (pre.map (·.unit)) (crossed.map (·.unit)) pivot)) := do
  let ordinal := pivot.1
  let r := ordinal + 1
  if localStep : localPivotOk pivot = true then
    if accessible : accessOk ready [] (outsideRegion r) = true then
      if unchanged : (pre ++ crossed).all (unitEnvOk r) = true then
        return ⟨{
          access := accessOk_outsideRegion accessible
          localFlag := localStep
          scope := by
            intro u hu
            rw [← List.map_append] at hu
            obtain ⟨v, hv, rfl⟩ := List.mem_map.mp hu
            exact unitEnvOk_sound (List.all_eq_true.mp unchanged v hv)
        }⟩
      else
        let offender := (pre ++ crossed).find? (fun u => !(unitEnvOk r u))
        throw s!"unit {(offender.map (·.unit.1)).getD ordinal} has access to the moved region of unit {ordinal}"
    else throw s!"the linked state already exposes the region of unit {ordinal}"
  else throw s!"unit {ordinal} is not a local allocating declaration"

/-- Find a single object region that references the pivot. This is only a
candidate: both the isolated region and the pivot must pass the full checks. -/
def observerRegion? {w} (ready : State w) (r : Nat) : Option Nat :=
  (ready.heap.zipIdx.find? fun (row, q) =>
    q != r && row.any (fun o => o.nodes.any (fun n => n.addr.region == r))).map Prod.snd

def checkIsolated? {w} (s : State w) (q : Nat) (units : List PreparedUnit) :
    Option (PLift (q ≠ 0 ∧ Access [] (Unprotected q (fun _ => False)) s ∧
      ∀ u ∈ units.map (·.unit), u.1 + 1 ≠ q ∧ ∀ node ∈ (u.2.2.capture u.2.1.free).nodes, Unprotected q (fun _ => False) node)) := do
  if hq : q ≠ 0 then
    if access : accessOk s [] (outsideRegion q) = true then
      if scopes : units.all (unitEnvOk q) = true then
        return ⟨hq, accessOk_outsideRegion access, by
          intro u hu
          obtain ⟨v, hv, rfl⟩ := List.mem_map.mp hu
          exact unitEnvOk_sound (List.all_eq_true.mp scopes v hv)⟩
  none

end CoreJS.LinkedBehavior
