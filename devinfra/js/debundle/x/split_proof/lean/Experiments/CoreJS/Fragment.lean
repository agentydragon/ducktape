import Experiments.CoreJS.Syntax

/-!
# CoreJS: the statements whose footprint is proved

Total functions on the model's syntax, which the proofs and the tools share. `Expr.allocOnly` is the shape of an
expression that only allocates ([`Frame.lean`](Frame.lean), [`Move.lean`](Move.lean)); `MayMove` says which two
statements of a bundle may run in either order (`moves_preserve`).
-/

namespace CoreJS

/-- Plain class creation stores method bodies without running them. Fields and computed keys are excluded. -/
def plainClassMembers : List ClassMember → Bool
  | [] => true
  | .method _ (.str _) _ :: ms | .getter _ (.str _) _ :: ms | .setter _ (.str _) _ :: ms => plainClassMembers ms
  | _ :: _ => false

theorem plainClassMembers_fieldInits {ms : List ClassMember} (h : plainClassMembers ms = true) :
    fieldInits ms = [] := by
  induction ms with
  | nil => rfl
  | cons m ms ih =>
    cases m <;> rename_i static k f <;> cases k <;>
      simp only [plainClassMembers, Bool.false_eq_true] at h
    all_goals simpa only [fieldInits] using ih h

mutual
/-- An expression that only allocates: a literal, a name, a function, an object literal of named data properties, an array literal of such expressions, or a plain class. -/
def Expr.allocOnly : Expr → Bool
  | .undef | .null | .bool _ | .num _ | .str _ | .var _ | .host _ | .this | .func _ => true
  | .obj props => allocOnlyProps props
  | .arr elems => allocOnlyAll elems
  | .cls _ _ _ members => plainClassMembers members
  | _ => false

def allocOnlyAll : List Expr → Bool
  | [] => true
  | e :: es => e.allocOnly && allocOnlyAll es

def allocOnlyProps : List PropDef → Bool
  | [] => true
  | .init (.str _) v :: ps => v.allocOnly && allocOnlyProps ps
  | _ :: _ => false
end

/-- Opaque method bodies stay deferred during plain class creation. -/
theorem allocOnly_plainClass_opaqueMethod :
    (Expr.cls "Plain" true (.opaque "Plain" .classCtor false 0 0 0 [])
      [.method false (.str "method") (.opaque "method" .method false 0 0 1 ["later"])]).allocOnly = true := rfl

theorem allocOnly_class_staticField_rejected :
    (Expr.cls "Plain" false (.opaque "Plain" .classCtor false 0 0 0 [])
      [.field true (.str "field") .undef]).allocOnly = false := rfl

theorem allocOnly_class_instanceField_rejected :
    (Expr.cls "Plain" false (.opaque "Plain" .classCtor false 0 0 0 [])
      [.field false (.str "field") .undef]).allocOnly = false := rfl

theorem allocOnly_class_nonliteralKey_rejected :
    (Expr.cls "Plain" false (.opaque "Plain" .classCtor false 0 0 0 [])
      [.method false (.var "key") (.opaque "method" .method false 0 0 1 [])]).allocOnly = false := rfl

mutual
/-- The names an expression that only allocates reads when it runs. Functions and admitted plain classes read none: they keep bindings, not values. -/
def Expr.readsNow : Expr → List Name
  | .var x => [x]
  | .obj props => readsNowProps props
  | .arr elems => readsNowAll elems
  | _ => []

def readsNowAll : List Expr → List Name
  | [] => []
  | e :: es => e.readsNow ++ readsNowAll es

def readsNowProps : List PropDef → List Name
  | [] => []
  | .init _ v :: ps => v.readsNow ++ readsNowProps ps
  | _ :: ps => readsNowProps ps
end

/-- A statement that does nothing when it runs: its work is done when the program is linked. An opaque statement that
declares only functions is one. -/
def Stmt.inert : Stmt → Bool
  | .funDecl _ | .varDecl _ | .opaque _ [] _ _ (.closures []) => true
  | _ => false

/-- A statement that only allocates and gives its own bindings their values: the names it declares and the names it
reads when it runs. A declaration whose initializer only allocates, and an opaque statement that only makes
functions, which reads none. -/
def Stmt.allocs : Stmt → Option (List Name × List Name)
  | .decl _ x e => if e.allocOnly then some ([x], e.readsNow) else none
  | .opaque _ decls funs _ (.closures _) => some (decls.map (·.1) ++ funs.map (·.1), [])
  | _ => none

/-- Two statements whose order does not matter: one does nothing when it runs, or both only allocate and neither
reads a name the other declares. -/
def mayMove (a b : Stmt) : Bool :=
  a.inert || b.inert ||
    match a.allocs, b.allocs with
    | some (xs, rs), some (ys, rs') => !rs.any ys.contains && !rs'.any xs.contains
    | _, _ => false

/-- Statements `i` and `j` of the bundle `ss` may run in either order. -/
def MayMove (ss : List Stmt) (i j : Nat) : Bool :=
  match ss[i]?, ss[j]? with
  | some a, some b => mayMove a b
  | _, _ => false

/-- `MayMove` on an array of the statements, which a tool can index in constant time. -/
def MayMoveAt (ss : Array Stmt) (i j : Nat) : Bool :=
  match ss[i]?, ss[j]? with
  | some a, some b => mayMove a b
  | _, _ => false

theorem mayMoveAt_toArray (ss : List Stmt) (i j : Nat) : MayMoveAt ss.toArray i j = MayMove ss i j := by
  simp [MayMoveAt, MayMove]

/-- Every pair that `order` runs out of the bundle's order passes `MayMove`: the last hypothesis of `moves_preserve`,
decided in time quadratic in the length of `order` (`movesOk_iff`). -/
def movesOk (ss : Array Stmt) : List Nat → Bool
  | [] => true
  | a :: l => l.all (fun b => decide (a < b) || MayMoveAt ss a b) && movesOk ss l

theorem movesOk_iff (ss : List Stmt) (order : List Nat) :
    movesOk ss.toArray order = true ↔ order.Pairwise fun a b => a < b ∨ MayMove ss a b = true := by
  induction order with
  | nil => simp [movesOk]
  | cons a l ih => simp [movesOk, List.pairwise_cons, ih, mayMoveAt_toArray]

end CoreJS
