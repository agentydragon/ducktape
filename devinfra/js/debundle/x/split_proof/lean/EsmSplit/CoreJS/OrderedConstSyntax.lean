import EsmSplit.CoreJS.OrderedRegion

/-! Source-defined primitive const initializers for ordered CoreJS plans.
The source check does not establish parser, printer, or JavaScript observation
faithfulness; the stricter consumer must check those separately. -/

namespace CoreJS.OrderedConstSyntax

/-- The unary operators represented directly by JavaScript source syntax.
The internal coercion operation `.toString` is not itself a unary token. -/
def sourceUnary : UnOp → Bool
  | .toString => false
  | _ => true

/-- Closed, primitive-expression syntax. Reads are restricted to bindings
declared in this source; a forward or self read is retained, including its
modeled temporal-dead-zone result. Binary operators are CoreJS's source
binary operators; `in` and `instanceof` use different constructors. -/
inductive Rhs (names : List Name) : Expr → Prop where
  | undef : Rhs names .undef
  | null : Rhs names .null
  | bool (b : Bool) : Rhs names (.bool b)
  | num (n : Float) : Rhs names (.num n)
  | str (s : String) : Rhs names (.str s)
  | read (name : Name) : name ∈ names → Rhs names (.var name)
  | unary {op : UnOp} {arg : Expr} : sourceUnary op = true → Rhs names arg → Rhs names (.unary op arg)
  | binary {op : BinOp} {left right : Expr} : Rhs names left → Rhs names right → Rhs names (.binary op left right)

/-- Check the source syntax without evaluating it or examining a candidate. -/
def checkRhs (names : List Name) : (expr : Expr) → Option (PLift (Rhs names expr))
  | .undef => some ⟨.undef⟩
  | .null => some ⟨.null⟩
  | .bool b => some ⟨.bool b⟩
  | .num n => some ⟨.num n⟩
  | .str s => some ⟨.str s⟩
  | .var name => if h : name ∈ names then some ⟨.read name h⟩ else none
  | .unary op arg => do
    if h : sourceUnary op = true then
      let ⟨argument⟩ ← checkRhs names arg
      return ⟨.unary h argument⟩
    else none
  | .binary _op left right => do
    let ⟨lhs⟩ ← checkRhs names left
    let ⟨rhs⟩ ← checkRhs names right
    return ⟨.binary lhs rhs⟩
  | _ => none

/-- Every free binding in an admitted initializer belongs to this source. -/
theorem rhs_free {names : List Name} {expr : Expr} (rhs : Rhs names expr) :
    ∀ name ∈ expr.free, name ∈ names := by
  induction rhs with
  | undef | null | bool _ | num _ | str _ => simp [Expr.free]
  | read name member => simpa [Expr.free] using member
  | unary _ _ ih => simpa [Expr.free] using ih
  | binary _ _ left right =>
    intro name member
    simp only [Expr.free, List.mem_append] at member
    exact member.elim (left name) (right name)

/-- Names are taken from the actual source statements, not the output or a
second inferred bundle. Unsupported statements are refused by `checkBody`. -/
def sourceNames (body : List (Nat × Stmt)) : List Name :=
  body.flatMap fun unit => unit.2.bindings.map Prod.fst

/-- Unique const declarations of arbitrary length, with a source-defined RHS. -/
inductive Declarations (names : List Name) : List Name → List (Nat × Stmt) → Prop where
  | nil (earlier) : Declarations names earlier []
  | cons {earlier id name value rest} :
      name ∉ earlier → Rhs names value → Declarations names (name :: earlier) rest →
      Declarations names earlier ((id, .decl .const name value) :: rest)

def checkDeclarations (names : List Name) : (earlier : List Name) → (body : List (Nat × Stmt)) →
    Option (PLift (Declarations names earlier body))
  | earlier, [] => some ⟨.nil earlier⟩
  | earlier, (_, .decl .const name value) :: rest => do
    if fresh : name ∉ earlier then
      let ⟨rhs⟩ ← checkRhs names value
      let ⟨tail⟩ ← checkDeclarations names (name :: earlier) rest
      return ⟨.cons fresh rhs tail⟩
    else none
  | _, _ => none

/-- Proof-carrying source check. The full declaration list fixes the set of
names, so no host/global or candidate-derived binding can be introduced. -/
def checkBody (body : List (Nat × Stmt)) : Option (PLift (Declarations (sourceNames body) [] body)) :=
  checkDeclarations (sourceNames body) [] body

theorem all_const {names earlier : List Name} {body : List (Nat × Stmt)}
    (decls : Declarations names earlier body) :
    ∀ stmt ∈ body.map Prod.snd, ∃ name value, stmt = .decl .const name value := by
  induction decls with
  | nil => simp
  | @cons earlier id name value rest fresh rhs tail ih =>
    intro stmt member
    simp only [List.map_cons, List.mem_cons] at member
    rcases member with rfl | member
    · exact ⟨name, value, rfl⟩
    · exact ih stmt member

/-- Separate source-order read condition for callers whose JavaScript
observation contract cannot admit a reached TDZ exception. This condition
does not assert that every possible operation or host environment is safe. -/
inductive EarlierReads : List Name → List (Nat × Stmt) → Prop where
  | nil (earlier) : EarlierReads earlier []
  | cons {earlier id name value rest} :
      (∀ x ∈ value.free, x ∈ earlier) → EarlierReads (name :: earlier) rest →
      EarlierReads earlier ((id, .decl .const name value) :: rest)

def checkEarlierReads : (earlier : List Name) → (body : List (Nat × Stmt)) →
    Option (PLift (EarlierReads earlier body))
  | earlier, [] => some ⟨.nil earlier⟩
  | earlier, (_, .decl .const name value) :: rest => do
    if reads : ∀ x ∈ value.free, x ∈ earlier then
      let ⟨tail⟩ ← checkEarlierReads (name :: earlier) rest
      return ⟨.cons reads tail⟩
    else none
  | _, _ => none

/-- On a real import-free singleton and the actual authored dense plan,
checked syntax implies the const-source shape and the existing ordered-plan
proof supplies equality for all modeled worlds and fuel. Source/plan
footprints, validity, and preserved order are independent checked premises. -/
theorem dense_runs (ss : List Stmt) (B : EsmSplit.Bundle) (S : EsmSplit.Spec)
    (grammar : Declarations (sourceNames ((List.range ss.length).zip ss)) [] ((List.range ss.length).zip ss))
    (valid : EsmSplit.Valid B S (EsmSplit.mkPlan B S)) (footprints : FootprintsOfSyntax ss B)
    (ordered : OrderKept ss (EsmSplit.mkPlan B S)) (name : Name) (exports : List (Name × Name)) :
    (∀ stmt ∈ ss, ∃ binding value, stmt = .decl .const binding value) ∧
    ∀ w fuel, runProgram w fuel
      ⟨name, [⟨name, [], (List.range ss.length).zip ss, exports⟩]⟩ =
      runProgram w fuel (splitProgram ss B (EsmSplit.mkPlan B S)) := by
  have consts := all_const grammar
  rw [List.map_snd_zip (by simp)] at consts
  exact ⟨consts, fun w fuel => OrderedRegion.planned_runs_with_exports ss B S valid footprints ordered name exports w fuel⟩

-- Supported nested syntax, and the strict caller's separate forward-read guard.
example : (checkBody [(0, .decl .const "a" (.num 3)),
    (1, .decl .const "b" (.binary .sub (.var "a") (.unary .neg (.num 1))))]).isSome = true := by decide

example : (checkBody [(0, .decl .const "a" (.var "b")), (1, .decl .const "b" (.bool true))]).isSome = true := by decide

example : (checkEarlierReads [] [(0, .decl .const "a" (.var "b")),
    (1, .decl .const "b" (.bool true))]).isSome = false := by decide

example : (checkBody [(0, .decl .const "a" (.host "console"))]).isSome = false := by decide

#print axioms checkRhs
#print axioms rhs_free
#print axioms checkBody
#print axioms all_const
#print axioms checkEarlierReads
#print axioms dense_runs

end CoreJS.OrderedConstSyntax
