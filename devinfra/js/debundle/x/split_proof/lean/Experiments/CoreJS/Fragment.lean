import Experiments.CoreJS.Syntax

/-!
# CoreJS: the statements whose footprint is proved

Total functions on the model's syntax, which the proofs and the tools share. `Expr.allocOnly` is the shape of an
expression that only allocates ([`Frame.lean`](Frame.lean), [`Move.lean`](Move.lean)); `MayMove` says which two
statements of a bundle may run in either order (`moves_preserve`).
-/

namespace CoreJS

mutual
/-- An expression that only allocates: a literal, a name, a function, an object literal of named data properties or an array literal of such expressions. -/
def Expr.allocOnly : Expr → Bool
  | .undef | .null | .bool _ | .num _ | .str _ | .var _ | .host _ | .this | .func _ => true
  | .obj props => allocOnlyProps props
  | .arr elems => allocOnlyAll elems
  | _ => false

def allocOnlyAll : List Expr → Bool
  | [] => true
  | e :: es => e.allocOnly && allocOnlyAll es

def allocOnlyProps : List PropDef → Bool
  | [] => true
  | .init (.str _) v :: ps => v.allocOnly && allocOnlyProps ps
  | _ :: _ => false
end

mutual
/-- The names an expression that only allocates reads when it runs. A function reads none: it keeps bindings, not values. -/
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

end CoreJS
