import EsmSplit.CoreJS.Syntax

/-! A single source-defined primitive RHS grammar. The caller supplies the
binding policy: for example, names already initialized, all source-declared
names, or unrestricted names for a syntax-only check. -/

namespace CoreJS.PrimitiveRhs

/-- Internal string coercion is not a source unary operator. -/
def sourceUnary : UnOp → Bool
  | .toString => false
  | _ => true

inductive Rhs (allows : Name → Bool) : Expr → Prop where
  | undef : Rhs allows .undef
  | null : Rhs allows .null
  | bool (b : Bool) : Rhs allows (.bool b)
  | num (n : Float) : Rhs allows (.num n)
  | str (s : String) : Rhs allows (.str s)
  | read (name : Name) : allows name = true → Rhs allows (.var name)
  | unary {op : UnOp} {arg : Expr} : sourceUnary op = true → Rhs allows arg → Rhs allows (.unary op arg)
  | binary {op : BinOp} {left right : Expr} : Rhs allows left → Rhs allows right → Rhs allows (.binary op left right)

/-- Check syntax and the supplied binding policy in one traversal. -/
def check (allows : Name → Bool) : (expr : Expr) → Option (PLift (Rhs allows expr))
  | .undef => some ⟨.undef⟩
  | .null => some ⟨.null⟩
  | .bool b => some ⟨.bool b⟩
  | .num n => some ⟨.num n⟩
  | .str s => some ⟨.str s⟩
  | .var name => if h : allows name = true then some ⟨.read name h⟩ else none
  | .unary op arg => do
    if h : sourceUnary op = true then
      let ⟨argument⟩ ← check allows arg
      return ⟨.unary h argument⟩
    else none
  | .binary _op left right => do
    let ⟨lhs⟩ ← check allows left
    let ⟨rhs⟩ ← check allows right
    return ⟨.binary lhs rhs⟩
  | _ => none

/-- Every free identifier satisfies the actual caller-supplied policy. -/
theorem free_allowed {allows : Name → Bool} {expr : Expr} (rhs : Rhs allows expr) :
    ∀ name ∈ expr.free, allows name = true := by
  induction rhs with
  | undef | null | bool _ | num _ | str _ => simp [Expr.free]
  | read name member => simpa [Expr.free] using member
  | unary _ _ ih => simpa [Expr.free] using ih
  | binary _ _ left right =>
    intro name member
    simp only [Expr.free, List.mem_append] at member
    exact member.elim (left name) (right name)

/-- A less restrictive binding policy preserves the same supported syntax. -/
theorem weaken {allows more : Name → Bool} {expr : Expr} (rhs : Rhs allows expr)
    (includes : ∀ name, allows name = true → more name = true) : Rhs more expr := by
  induction rhs with
  | undef => exact .undef
  | null => exact .null
  | bool b => exact .bool b
  | num n => exact .num n
  | str s => exact .str s
  | read name allowed => exact .read name (includes name allowed)
  | unary op arg ih => exact .unary op ih
  | binary left right ihl ihr => exact .binary ihl ihr

/-- Syntax accepted without name restrictions can be restricted precisely by
checking its free identifiers; no operator-specific evaluation is needed. -/
theorem restrict {allows : Name → Bool} {expr : Expr} (rhs : Rhs (fun _ => true) expr)
    (closed : ∀ name ∈ expr.free, allows name = true) : Rhs allows expr := by
  induction rhs with
  | undef => exact .undef
  | null => exact .null
  | bool b => exact .bool b
  | num n => exact .num n
  | str s => exact .str s
  | read name _ => exact .read name (closed name (by simp [Expr.free]))
  | unary op arg ih => exact .unary op (ih (by simpa [Expr.free] using closed))
  | binary left right ihl ihr =>
    refine .binary (ihl ?_) (ihr ?_)
    · intro name member
      apply closed name
      simp only [Expr.free, List.mem_append]
      exact Or.inl member
    · intro name member
      apply closed name
      simp only [Expr.free, List.mem_append]
      exact Or.inr member

theorem policy_iff {allows : Name → Bool} {expr : Expr} :
    Rhs allows expr ↔ Rhs (fun _ => true) expr ∧ ∀ name ∈ expr.free, allows name = true := by
  constructor
  · intro rhs
    exact ⟨weaken rhs (fun _ _ => rfl), free_allowed rhs⟩
  · rintro ⟨shape, closed⟩
    exact restrict shape closed

#print axioms check
#print axioms free_allowed
#print axioms weaken
#print axioms restrict
#print axioms policy_iff

end CoreJS.PrimitiveRhs
