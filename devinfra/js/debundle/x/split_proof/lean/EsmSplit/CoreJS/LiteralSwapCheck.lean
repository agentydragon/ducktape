import EsmSplit.CoreJS.ExactLiteralMove

/-! Check a finite sequence of adjacent literal swaps against actual linked
units. This is a schedule lemma; construction of a plan and its links is a
separate obligation. -/

namespace CoreJS.LiteralSwapCheck

open ExactLiteralMove

/-- Recognize a primitive literal and retain its exact source expression. -/
def literal (e : Expr) : Option { v : Literal // v.expr = e } :=
  match e with
  | .undef => some ⟨.undef, rfl⟩
  | .null => some ⟨.null, rfl⟩
  | .bool b => some ⟨.bool b, rfl⟩
  | .num n => some ⟨.num n, rfl⟩
  | .str s => some ⟨.str s, rfl⟩
  | _ => none

/-- Schedule steps over original indexed rows. Prefixes and suffixes retain
their statements and IDs. The address obligations are discharged at allocation. -/
inductive SourceSchedule : List (Nat × Stmt) → List (Nat × Stmt) → Prop where
  | refl (body) : SourceSchedule body body
  | swap (pre tail : List (Nat × Stmt)) (i j : Nat) (x y : Name) (a b : Literal) :
      x ≠ y →
      SourceSchedule (pre ++ (i, decl x a) :: (j, decl y b) :: tail)
        (pre ++ (j, decl y b) :: (i, decl x a) :: tail)
  | trans {before middle after} :
      SourceSchedule before middle → SourceSchedule middle after → SourceSchedule before after

theorem SourceSchedule.perm {before after} (schedule : SourceSchedule before after) :
    after.Perm before := by
  induction schedule with
  | refl => exact .refl _
  | swap pre tail i j x y a b _ =>
    apply List.Perm.append_left
    exact .swap _ _ _
  | trans _ _ first second => exact second.trans first

def inScope (env : Env) (body : List (Nat × Stmt)) : List LinkedUnit :=
  body.map fun (id, stmt) => (id, stmt, env)

/-- Use one actual allocation for the entire schedule and lift each source swap
to the already-proved linked step. -/
theorem SourceSchedule.linked {before after} (schedule : SourceSchedule before after) (env : Env)
    (cells : ∀ id x e, (id, .decl .const x e) ∈ before → ∃ a, env.vars.lookup x = some a)
    (injective : ∀ x y a, env.vars.lookup x = some a → env.vars.lookup y = some a → x = y) :
    SafeReorder (inScope env before) (inScope env after) := by
  induction schedule with
  | refl => exact .refl _
  | swap pre tail i j x y a b different =>
    obtain ⟨cx, hx⟩ := cells i x a.expr (by simp [decl])
    obtain ⟨cy, hy⟩ := cells j y b.expr (by simp [decl])
    have separate : cx ≠ cy := fun same => different (injective x y cx hx (same ▸ hy))
    simpa [inScope] using
      (SafeReorder.swap (inScope env pre) (inScope env tail) i j env env x y a b cx cy hx hy separate)
  | trans first _ firstSound secondSound =>
    exact .trans (firstSound cells) (secondSound fun id x e member =>
      cells id x e (first.perm.mem_iff.1 member))

/-- Check the requested executable order using the original rows. The current
checker admits unchanged order or one adjacent pair of distinct literal consts. -/
def checkSourceSchedule (body : List (Nat × Stmt)) (order : List Nat) :
    Except String (Σ scheduled : List (Nat × Stmt),
      PLift (SourceSchedule body scheduled ∧ order = scheduled.map Prod.fst)) := do
  let ids := body.map Prod.fst
  if kept : order = ids then return ⟨body, ⟨.refl _, kept⟩⟩
  let some index := (ids.zip order).findIdx? (fun (before, after) => before != after) |
    throw "plan does not enumerate the original executable units"
  match remaining : body.drop index with
  | (i, .decl .const x ex) :: (j, .decl .const y ey) :: tail =>
    let some ⟨a, ha⟩ := literal ex | throw "first moved initializer is not a primitive literal"
    let some ⟨b, hb⟩ := literal ey | throw "second moved initializer is not a primitive literal"
    if distinct : x ≠ y then
      let pre := body.take index
      let scheduled := pre ++ (j, decl y b) :: (i, decl x a) :: tail
      if ordered : order = scheduled.map Prod.fst then
        have source : body = pre ++ (i, decl x a) :: (j, decl y b) :: tail := by
          rw [decl, ha, decl, hb, ← remaining]
          exact (List.take_append_drop index body).symm
        return ⟨scheduled, ⟨source.symm ▸ .swap pre tail i j x y a b distinct, ordered⟩⟩
      else throw "plan is not the checked adjacent swap"
    else throw "moved declarations do not name distinct bindings"
  | _ => throw "the changed order does not start with two adjacent const declarations"

/-- One checked step uses the linked addresses of the actual units. It cannot
move a read, a throw, or two declarations of the same linked cell. -/
def adjacent (units : List LinkedUnit) (index : Nat) : Except String { out // SafeReorder units out } := do
  match hrem : units.drop index with
  | (i, .decl .const x ex, envX) :: (j, .decl .const y ey, envY) :: tail =>
    let some ⟨vx, hx⟩ := literal ex | throw "first initializer is not a primitive literal"
    let some ⟨vy, hy⟩ := literal ey | throw "second initializer is not a primitive literal"
    match hX : envX.vars.lookup x, hY : envY.vars.lookup y with
    | some cx, some cy =>
      if distinct : cx ≠ cy then
        have source : units = units.take index ++ (i, decl x vx, envX) :: (j, decl y vy, envY) :: tail := by
          rw [decl, hx, decl, hy, ← hrem, List.take_append_drop]
        have safe := SafeReorder.swap (units.take index) tail i j envX envY x y vx vy cx cy hX hY distinct
        return ⟨_, source.symm ▸ safe⟩
      else throw "declarations share a linked cell"
    | _, _ => throw "declaration has no linked cell"
  | _ => throw "adjacent units must be const declarations"

/-- Apply the caller's finite swap script without executing either program. -/
def sequence (units : List LinkedUnit) : List Nat → Except String { out // SafeReorder units out }
  | [] => .ok ⟨units, .refl units⟩
  | index :: rest => do
    let ⟨next, step⟩ ← adjacent units index
    let ⟨out, more⟩ ← sequence next rest
    pure ⟨out, .trans step more⟩

/-- Every accepted script preserves every bounded modeled outcome. -/
theorem sequence_sound {units : List LinkedUnit} {script : List Nat}
    {result : { out // SafeReorder units out }}
    (_accepted : sequence units script = .ok result) {W : World} (fuel : Nat) (state : State W) :
    runUnits fuel units state = runUnits fuel result.val state :=
  reorder_exact fuel result.property state

/-- A read cannot be admitted as a primitive initializer. -/
theorem refuses_read :
    (match adjacent [(0, decl "a" (.bool true), (⟨[("a", ⟨1, 0⟩)], .undef, none⟩ : Env)),
      (1, .decl .const "b" (.var "a"), (⟨[("b", ⟨2, 0⟩)], .undef, none⟩ : Env))] 0 with
    | .error _ => True
    | .ok _ => False) := by cbv

/-- A throw cannot be crossed, so its order and effects remain observable. -/
theorem refuses_throw :
    (match adjacent [(0, decl "a" (.bool true), (⟨[("a", ⟨1, 0⟩)], .undef, none⟩ : Env)),
      (1, .throw (.str "stop"), (⟨[], .undef, none⟩ : Env))] 0 with
    | .error _ => True
    | .ok _ => False) := by cbv

private def thrown {w : World} (result : Res w Unit) : Option Val :=
  match result with
  | .throw value _ => some value
  | _ => none

/-- Reversing two throws changes the first observed abrupt outcome. -/
theorem throw_order (w : World) :
    thrown (runUnits 16 [(0, .throw (.str "first"), (⟨[], .undef, none⟩ : Env)),
      (1, .throw (.str "second"), (⟨[], .undef, none⟩ : Env))] (initialState w 2)) = some (.str "first") ∧
    thrown (runUnits 16 [(1, .throw (.str "second"), (⟨[], .undef, none⟩ : Env)),
      (0, .throw (.str "first"), (⟨[], .undef, none⟩ : Env))] (initialState w 2)) = some (.str "second") := by
  constructor <;> cbv

#print axioms SourceSchedule.perm
#print axioms SourceSchedule.linked
#print axioms checkSourceSchedule
#print axioms throw_order
#print axioms refuses_read
#print axioms refuses_throw
#print axioms literal
#print axioms adjacent
#print axioms sequence
#print axioms sequence_sound

end CoreJS.LiteralSwapCheck
