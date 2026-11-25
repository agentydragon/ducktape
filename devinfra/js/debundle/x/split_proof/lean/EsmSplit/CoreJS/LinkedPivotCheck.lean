import EsmSplit.CoreJS.LinkedPivot
import EsmSplit.CoreJS.StringInitializer
import EsmSplit.CoreJS.StableIntrinsic
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
def localPivotOk (pivot : LinkedUnit) : Bool :=
  let owns := inRegion (pivot.id + 1) pivot.env
  match pivot.stmt with
  | .decl _ name e => e.allocOnly && owns name && e.readsNow.all owns
  | .opaque _ decls _ _ (.closures _) => decls.all (owns ·.1)
  | _ => false

theorem localPivotOk_sound {W : World} {pivot : LinkedUnit} (ok : localPivotOk pivot = true)
    (fuel : Nat) :
    Local (pivot.id + 1) (fun _ => False) (execStmt fuel pivot.env pivot.stmt : M W Flow) := by
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

abbrev Outside (q : Nat) := Unprotected q (fun _ => False)

def IsolatedUnit (q : Nat) (u : LinkedUnit) : Prop :=
  u.id + 1 ≠ q ∧ ∀ node ∈ (u.env.capture u.stmt.free).nodes, Outside q node

abbrev PivotLocal (w : World) (n : Nat) (u : LinkedUnit) :=
  Local (u.id + 1) (fun _ => False) (execStmt n u.env u.stmt : M w Flow)

abbrev PivotLocalFor (intrinsics : List StableIntrinsic) (u : LinkedUnit) :=
  ∀ w, StableIntrinsic.Worlds intrinsics w → ∀ n, PivotLocal w n u

/-- A closed string expression reads no lexical or host state. Host reads need
the selected descriptor's own proof. Both feed the same declaration step. -/
private def pureInitializer? (intrinsics : List StableIntrinsic) (e : Expr) :
    Option (PLift (∀ w, StableIntrinsic.Worlds intrinsics w → ∀ fuel env,
      (evalExpr fuel env e : M w Val) = outOfFuel ∨ ∃ v, evalExpr (W := w) fuel env e = pure v)) := do
  -- A checked own numeric read uses this two-level expression to preserve fuel.
  match h : e with
  | .seq .undef (.num x) =>
    return ⟨by
      intro w _ fuel env
      rw [h]
      cases fuel with
      | zero => exact Or.inl rfl
      | succ n =>
        cases n with
        | zero => exact Or.inl rfl
        | succ => exact Or.inr ⟨.num x, rfl⟩⟩
  | _ => pure ()
  if let some a := stringInitializer? (fun _ => none) e then return ⟨fun w _ => a.closed w⟩
  intrinsics.attach.findSome? fun ⟨d, hd⟩ => do
    let ⟨evaluate⟩ ← d.evalValue? e
    return ⟨fun w hw => evaluate w (hw d hd)⟩

/-- Existing local evaluations need no host observations. An opted-in
descriptor can also prove an actual Number read independent of host state. -/
def checkedLocal? (intrinsics : List StableIntrinsic) (pivot : LinkedUnit) :
    Option (PLift (PivotLocalFor intrinsics pivot)) := do
  if h : localPivotOk pivot = true then return ⟨fun _ _ => localPivotOk_sound h⟩
  match hp : pivot with
  | (id, .decl kind name e, env) =>
    let ⟨owned⟩ ← if h : inRegion (id + 1) env name = true then some (PLift.up h) else none
    let ⟨evaluate⟩ ← pureInitializer? intrinsics e
    return ⟨by
      rw [hp]
      intro w hw fuel
      have hx (c : Addr) (h : env.vars.lookup name = some c) : c.region = id + 1 := inRegion_owns owned h
      cases fuel with
      | zero => exact local_outOfFuel
      | succ n =>
        have expr : Local (id + 1) (fun _ => False) (evalExpr n env e : M w Val) := by
          rcases evaluate w hw n env with exhausted | ⟨x, value⟩
          · rw [exhausted]; exact local_outOfFuel
          · rw [value]; exact local_pure _
        change Local (id + 1) (fun _ => False) (execStmt (n + 1) env (.decl kind name e) : M w Flow)
        cases kind <;> unfold execStmt
        case var => exact expr.bind' fun v => (local_writeVar hx v).bind' fun _ => local_pure _
        all_goals exact expr.bind' fun v => (local_initVar hx v).bind' fun _ => local_pure _⟩
  | _ => none

private def outsideRegion (r : Nat) (node : HeapNode) : Bool := node.addr.region != r

private theorem outsideRegion_iff (r : Nat) (node : HeapNode) :
    outsideRegion r node = true ↔ Unprotected r (fun _ => False) node := by
  cases node <;> simp [outsideRegion, HeapNode.addr, Unprotected]

/-- The schedule reuses this exact capture for every pivot crossing the unit.
A unit never crossed by an allocating pivot need not resolve its free names.
The original unit (including its full scope) still runs in the certificate. -/
structure PreparedUnit where
  unit : LinkedUnit
  nodes : Thunk (List HeapNode)
  exactNodes : nodes.get = (unit.env.capture unit.stmt.free).nodes

def PreparedUnit.ofUnit (u : LinkedUnit) : PreparedUnit :=
  ⟨u, Thunk.mk (fun _ => (u.env.capture u.stmt.free).nodes), rfl⟩

private def unitEnvOk (r : Nat) (u : PreparedUnit) : Bool :=
  u.unit.id + 1 != r && u.nodes.get.all (outsideRegion r)

private theorem isolatedUnits_of_check {r} {units : List PreparedUnit}
    (ok : units.all (unitEnvOk r) = true) : ∀ u ∈ units.map PreparedUnit.unit, IsolatedUnit r u := by
  intro u hu
  obtain ⟨v, hv, rfl⟩ := List.mem_map.mp hu
  simpa only [IsolatedUnit, Outside, unitEnvOk, Bool.and_eq_true, bne_iff_ne, v.exactNodes,
    List.all_eq_true, outsideRegion_iff] using List.all_eq_true.mp ok v hv

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

private def sameUnit? (u v : LinkedUnit) :
    Option (PLift (SameUnit u v)) := do
  let tag ← if h : u.id = v.id then some (PLift.up h) else none
  let st ← SyntaxMatch.stmtEq u.stmt v.stmt
  let receiver ← if h : isModuleReceiver u.env.this && isModuleReceiver v.env.this then
    have both : isModuleReceiver u.env.this = true ∧ isModuleReceiver v.env.this = true := by
      simpa only [Bool.and_eq_true] using h
    some (PLift.up ((isModuleReceiver_eq both.1).trans (isModuleReceiver_eq both.2).symm))
    else none
  let home ← if h : u.env.home == v.env.home then some (PLift.up (beq_iff_eq.1 h)) else none
  if names : u.stmt.free.all (fun x => u.env.vars.lookup x == v.env.vars.lookup x) then
    return ⟨tag.down, st.down, ⟨receiver.down, home.down, fun x hx => beq_iff_eq.1 (List.all_eq_true.mp names x hx)⟩⟩
  else none

def sameUnits? : (u v : List LinkedUnit) → Option (PLift (SameUnits u v))
  | [], [] => some ⟨.nil⟩
  | x :: xs, y :: ys => do
    let head ← sameUnit? x y
    let tail ← sameUnits? xs ys
    return ⟨.cons head.down tail.down⟩
  | _, _ => none

/-- The frame checks for moving a unit ahead of a block in the current
schedule. The prefix must also preserve the frame from the initial linked state. -/
structure CheckedPivot {w : World} (ready : State w)
    (pre crossed : List LinkedUnit) (pivot : LinkedUnit) : Prop where
  access : Access [] (Unprotected (pivot.id + 1) (fun _ => False)) ready
  scope : ∀ u ∈ pre ++ crossed, IsolatedUnit (pivot.id + 1) u

/-- Check the initial heap and every environment that runs before the pivot has crossed the block. -/
def pivotFrameOk {w : World} (ready : State w)
    (pre crossed : List PreparedUnit) (pivot : LinkedUnit) : Bool :=
  accessOk ready [] (outsideRegion (pivot.id + 1)) &&
    (pre ++ crossed).all (unitEnvOk (pivot.id + 1))

theorem pivotFrameOk_sound {w : World} {ready : State w} {pre crossed : List PreparedUnit} {pivot : LinkedUnit}
    (ok : pivotFrameOk ready pre crossed pivot = true) :
    CheckedPivot ready (pre.map (·.unit)) (crossed.map (·.unit)) pivot := by
  simp only [pivotFrameOk, Bool.and_eq_true] at ok
  exact ⟨accessOk_outsideRegion ok.1, by
    simpa only [List.map_append] using isolatedUnits_of_check ok.2⟩

#print axioms pivotFrameOk_sound

def checkPivot {w : World} (ready : State w)
    (pre crossed : List PreparedUnit) (pivot : LinkedUnit) :
    Except String (PLift (CheckedPivot ready (pre.map (·.unit)) (crossed.map (·.unit)) pivot)) := do
  let ordinal := pivot.id
  let r := ordinal + 1
  if ok : pivotFrameOk ready pre crossed pivot = true then
    return ⟨pivotFrameOk_sound ok⟩
  else if accessOk ready [] (outsideRegion r) then
    let offender := (pre ++ crossed).find? (fun u => !(unitEnvOk r u))
    throw s!"unit {(offender.map (·.unit.id)).getD ordinal} has access to the moved region of unit {ordinal}"
  else throw s!"the linked state already exposes the region of unit {ordinal}"

/-- One candidate per object row with a direct edge into the pivot. The
move checker must still prove that each candidate is isolated. -/
def observerRegions {w} (ready : State w) (r : Nat) : List Nat :=
  ready.heap.zipIdx.toList.filterMap fun (row, q) =>
    if q != r && row.any (fun o => o.nodes.any (fun n => n.addr.region == r)) then some q else none

/-- Index foreign incoming regions once, including both closure captures in objects
and object values in binding cells. Multiple references from one row give one predecessor. -/
private def addIncoming {α} (rows : Array (Array α)) (edges : α → List HeapNode)
    (incoming : Std.HashMap Nat (List Nat)) : Std.HashMap Nat (List Nat) := Id.run do
  let mut incoming := incoming
  for (row, src) in rows.zipIdx do
    let mut seen : Std.HashSet Nat := {}
    for item in row do
      for node in edges item do
        let dst := node.addr.region
        unless src == dst || seen.contains dst do
          seen := seen.insert dst
          incoming := incoming.insert dst (src :: incoming.getD dst [])
  return incoming

/-- An observer may itself be captured by another hoist. Propose masking the
incoming closure in dependency order: peel outer observers before their targets.
This is only a bounded hint; each removal still needs an isolation proof on
the current linked state. Cycles have no first isolated row. -/
def observerClosure? {w} (ready : State w) (r : Nat) : Option (List Nat) := do
  let incoming := addIncoming ready.cells Binding.nodes (addIncoming ready.heap Obj.nodes {})
  let rec visit (fuel : Nat) (q : Nat) (active done_ : Std.HashSet Nat) (rev : List Nat) :
      Option (Std.HashSet Nat × List Nat) := do
    if done_.contains q then return (done_, rev)
    guard (!active.contains q && active.size + done_.size ≤ 32)
    match fuel with
    | 0 => none
    | fuel + 1 =>
      let active := active.insert q
      let mut st := (done_, rev)
      for p in incoming.getD q [] do
        st ← visit fuel p active st.1 st.2
      return (st.1.insert q, q :: st.2)
  let mut st : Std.HashSet Nat × List Nat := ({}, [])
  for q in incoming.getD r [] do
    st ← visit 32 q ({r} : Std.HashSet Nat) st.1 st.2
  return st.2.reverse

def checkIsolated? {w} (s : State w) (q : Nat) (units : List PreparedUnit) :
    Option (PLift (q ≠ 0 ∧ Access [] (Unprotected q (fun _ => False)) s ∧
      ∀ u ∈ units.map PreparedUnit.unit, IsolatedUnit q u)) := do
  if hq : q ≠ 0 then
    if access : accessOk s [] (outsideRegion q) = true then
      if scopes : units.all (unitEnvOk q) = true then
        return ⟨hq, accessOk_outsideRegion access, isolatedUnits_of_check scopes⟩
  none

end CoreJS.LinkedBehavior
