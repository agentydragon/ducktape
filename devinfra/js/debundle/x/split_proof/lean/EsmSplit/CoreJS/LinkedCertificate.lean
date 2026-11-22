import Lean
import EsmSplit.CoreJS.LinkedInput
import EsmSplit.CoreJS.LinkedBehavior

namespace CoreJS.LinkedBehavior
-- Lean cannot derive DecidableEq for nested mutual inductives. Compare the constructors
-- produced by LinkedInput; every successful branch returns equality, including deferred code.
private abbrev MatchEq (α : Type) := (a b : α) → Option (PLift (a = b))

private def scalarEq [DecidableEq α] : MatchEq α := fun a b =>
  if h : a = b then some ⟨h⟩ else none

private def listEq (item : MatchEq α) : MatchEq (List α)
  | [], [] => some ⟨rfl⟩
  | x :: xs, y :: ys => do
    let ⟨head⟩ ← item x y
    let ⟨tail⟩ ← listEq item xs ys
    return ⟨head ▸ tail ▸ rfl⟩
  | _, _ => none

mutual
private partial def exprEq : MatchEq Expr
  | .undef, .undef | .null, .null => some ⟨rfl⟩
  | .str a, .str b => (scalarEq a b).map fun h => ⟨congrArg Expr.str h.down⟩
  | .num a, .num b => (scalarEq a b).map fun h => ⟨congrArg Expr.num h.down⟩
  | .bool a, .bool b => (scalarEq a b).map fun h => ⟨congrArg Expr.bool h.down⟩
  | .var a, .var b => (scalarEq a b).map fun h => ⟨congrArg Expr.var h.down⟩
  | .unary op a, .unary op' b => do
    let ⟨o⟩ ← scalarEq op op'
    let ⟨v⟩ ← exprEq a b
    return ⟨o ▸ v ▸ rfl⟩
  | .binary op a b, .binary op' a' b' => do
    let ⟨o⟩ ← scalarEq op op'
    let ⟨left⟩ ← exprEq a a'
    let ⟨right⟩ ← exprEq b b'
    return ⟨o ▸ left ▸ right ▸ rfl⟩
  | .cond c t e, .cond c' t' e' => do
    let ⟨test⟩ ← exprEq c c'
    let ⟨yes⟩ ← exprEq t t'
    let ⟨no⟩ ← exprEq e e'
    return ⟨test ▸ yes ▸ no ▸ rfl⟩
  | .call f args, .call f' args' => do
    let ⟨callee⟩ ← exprEq f f'
    let ⟨arguments⟩ ← listEq exprEq args args'
    return ⟨callee ▸ arguments ▸ rfl⟩
  | .func (.mk name kind self params body), .func (.mk name' kind' self' params' body') => do
    let ⟨n⟩ ← scalarEq name name'
    let ⟨k⟩ ← scalarEq kind kind'
    let ⟨s⟩ ← scalarEq self self'
    let ⟨p⟩ ← scalarEq params params'
    let ⟨b⟩ ← listEq stmtEq body body'
    return ⟨n ▸ k ▸ s ▸ p ▸ b ▸ rfl⟩
  | _, _ => none

private partial def stmtEq : MatchEq Stmt
  | .varDecl a, .varDecl b => (scalarEq a b).map fun h => ⟨congrArg Stmt.varDecl h.down⟩
  | .expr a, .expr b => (exprEq a b).map fun h => ⟨congrArg Stmt.expr h.down⟩
  | .ret a, .ret b => (exprEq a b).map fun h => ⟨congrArg Stmt.ret h.down⟩
  | .throw a, .throw b => (exprEq a b).map fun h => ⟨congrArg Stmt.throw h.down⟩
  | .block a, .block b => (listEq stmtEq a b).map fun h => ⟨congrArg Stmt.block h.down⟩
  | .tryCatch body param handler, .tryCatch body' param' handler' => do
    let ⟨b⟩ ← stmtEq body body'
    let ⟨p⟩ ← scalarEq param param'
    let ⟨h⟩ ← stmtEq handler handler'
    return ⟨b ▸ p ▸ h ▸ rfl⟩
  | .tryFinally body finalizer, .tryFinally body' finalizer' => do
    let ⟨b⟩ ← stmtEq body body'
    let ⟨f⟩ ← stmtEq finalizer finalizer'
    return ⟨b ▸ f ▸ rfl⟩
  | .decl kind name init, .decl kind' name' init' => do
    let ⟨k⟩ ← scalarEq kind kind'
    let ⟨n⟩ ← scalarEq name name'
    let ⟨v⟩ ← exprEq init init'
    return ⟨k ▸ n ▸ v ▸ rfl⟩
  | _, _ => none
end

private def unitEq : MatchEq (Nat × Stmt)
  | (i, s), (j, t) => do
    let ⟨id⟩ ← scalarEq i j
    let ⟨stmt⟩ ← stmtEq s t
    return ⟨id ▸ stmt ▸ rfl⟩

private def moduleEq : MatchEq Module
  | ⟨n, i, b, e⟩, ⟨n', i', b', e'⟩ => do
    let ⟨name⟩ ← scalarEq n n'
    let ⟨imports⟩ ← scalarEq i i'
    let ⟨body⟩ ← listEq unitEq b b'
    let ⟨exports⟩ ← scalarEq e e'
    return ⟨name ▸ imports ▸ body ▸ exports ▸ rfl⟩

private def programEq : MatchEq Program
  | ⟨e, m⟩, ⟨e', m'⟩ => do
    let ⟨entry⟩ ← scalarEq e e'
    let ⟨modules⟩ ← listEq moduleEq m m'
    return ⟨entry ▸ modules ▸ rfl⟩

/-- The two programs are exactly the lowering of these three AST inputs under this placement.
Their public bindings and their complete normally terminating executions agree in every world. -/
def ASTEquivalent (source providers files : Lean.Json) (plan : EsmSplit.Plan) : Prop :=
  ∃ original candidate, LinkedInput.decodePair source providers files plan = .ok (original, candidate) ∧
    Equivalent original candidate ∧ SameExports original candidate

/-- Decode the actual ASTs and transport the execution and export proofs to those programs. -/
def certifyASTs (source providers files : Lean.Json) (plan : EsmSplit.Plan) (original candidate : Program)
    (semantics : Equivalent original candidate ∧ SameExports original candidate) :
    Except String (PLift (ASTEquivalent source providers files plan)) := do
  match decoded : LinkedInput.decodePair source providers files plan with
  | .error why => throw why
  | .ok (a, b) =>
    let some ⟨before⟩ := programEq a original | throw "source AST does not match the certified program"
    let some ⟨after⟩ := programEq b candidate | throw "output ASTs do not match the certified program"
    return ⟨⟨a, b, decoded, before ▸ after ▸ semantics⟩⟩

/-- Successful execution of the production checker establishes a claim about the input
and output ASTs for every host world. Its callers can discard the proof at runtime. -/
theorem checkASTs_sound {source providers files plan original candidate}
    (semantics : Equivalent original candidate ∧ SameExports original candidate)
    (accepted : (certifyASTs source providers files plan original candidate semantics).isOk = true) :
    ASTEquivalent source providers files plan := by
  cases h : certifyASTs source providers files plan original candidate semantics with
  | error why => rw [h] at accepted; contradiction
  | ok proof => exact proof.down

-- Reify every constructor and field, including the deferred code in heap closures.
-- IEEE bit patterns distinguish both zeroes and preserve NaN payloads.
local instance : Lean.ToExpr Float where
  toExpr n := Lean.mkApp (Lean.mkConst ``Float.ofBits) (Lean.toExpr n.toBits)
  toTypeExpr := Lean.mkConst ``Float

deriving instance Lean.ToExpr for DeclKind
deriving instance Lean.ToExpr for UnOp
deriving instance Lean.ToExpr for BinOp
deriving instance Lean.ToExpr for LogOp
deriving instance Lean.ToExpr for Prim
deriving instance Lean.ToExpr for FuncKind
deriving instance Lean.ToExpr for OpaqueRun
deriving instance Lean.ToExpr for Expr, Target, PropDef, ClassMember, Func, Stmt
deriving instance Lean.ToExpr for Import
deriving instance Lean.ToExpr for Module
deriving instance Lean.ToExpr for Program

/-- Render only the constructor applications, closed constants and literals produced by
`ToExpr`. Program strings are data, including line breaks, quotes and Lean comment delimiters. -/
private partial def term (e : Lean.Expr) : Except String String :=
  match e with
  | .app .. => do
    let f ← term e.getAppFn
    let args ← e.getAppArgs.toList.mapM term
    return "(" ++ String.intercalate " " (f :: args) ++ ")"
  | .const n _ => return "@" ++ n.toString
  | .lit (.strVal s) => return s.quote
  | .lit (.natVal n) => return toString n
  | _ => throw "unsupported expression in quoted CoreJS data"

/-- A closed Lean constructor term for the complete decoded program. -/
def programLiteral (p : Program) : Except String String := term (Lean.toExpr p)

deriving instance Lean.ToExpr for EsmSplit.Module
deriving instance Lean.ToExpr for EsmSplit.Plan

mutual
/-- Quote the exact Lean JSON value, including the raw object tree and each number's
mantissa and decimal exponent. No parsing or field reinsertion is needed in the certificate. -/
partial def jsonLiteral : Lean.Json → String
  | .null => "Lean.Json.null"
  | .bool b => "(Lean.Json.bool " ++ toString b ++ ")"
  | .num n => "(Lean.Json.num ⟨(" ++ toString n.mantissa ++ ")," ++ toString n.exponent ++ "⟩)"
  | .str s => "(Lean.Json.str " ++ s.quote ++ ")"
  | .arr a => "(Lean.Json.arr #[" ++ String.intercalate "," (a.toList.map jsonLiteral) ++ "])"
  | .obj ⟨⟨o⟩⟩ => "(Lean.Json.obj ⟨⟨" ++ objectLiteral o ++ "⟩⟩)"

private partial def objectLiteral : Std.DTreeMap.Internal.Impl String (fun _ => Lean.Json) → String
  | .leaf => ".leaf"
  | .inner sz key val left right =>
    "(.inner " ++ toString sz ++ " " ++ key.quote ++ " " ++ jsonLiteral val ++ " " ++
      objectLiteral left ++ " " ++ objectLiteral right ++ ")"
end

/-- Prove both obligations by evaluating the real module graphs with an arbitrary host world.
`cbv` produces kernel-checkable equations for definitions with well-founded recursion. -/
macro "verify_linked_programs " fuel:num : tactic => `(tactic|
  constructor <;> (first | apply of_evaluation $fuel | apply sameExports_of_match) <;>
    intro w <;> first | rfl | cbv <;> rfl)

-- Check the actual theorem, whose reduction lemmas depend on the input program.
open Lean Elab Command in
elab "#audit_linked_programs " thm:ident : command => do
  let name ← liftCoreM <| realizeGlobalConstNoOverload thm
  let axioms ← liftCoreM <| Lean.collectAxioms name
  let allowed := #[``propext, ``Classical.choice, ``Quot.sound]
  for ax in axioms do
    unless allowed.contains ax do throwError "linked program certificate depends on {ax}"

/-- The generated module proves an all-world, fuel-independent claim for these two programs.
The caller accepts only successful elaboration and kernel checking of the complete file. -/
def certificate (sourceAST providers files : Lean.Json) (plan : EsmSplit.Plan) (source candidate : Program) : Except String String := do
  let s ← programLiteral source
  let c ← programLiteral candidate
  let p ← term (Lean.toExpr plan)
  return "import EsmSplit.CoreJS.LinkedCertificate\n\nopen CoreJS\n" ++
    "set_option maxRecDepth 16000\nset_option maxHeartbeats 2000000\nset_option cbv.warning false\n" ++
    "private def sourceAST : Lean.Json := " ++ jsonLiteral sourceAST ++ "\n" ++
    "private def providers : Lean.Json := " ++ jsonLiteral providers ++ "\n" ++
    "private def files : Lean.Json := " ++ jsonLiteral files ++ "\n" ++
    "private def plan : EsmSplit.Plan := " ++ p ++ "\n" ++
    "private def source : Program := " ++ s ++ "\n" ++
    "private def candidate : Program := " ++ c ++ "\n" ++
    "private theorem executions : LinkedBehavior.Equivalent source candidate ∧ LinkedBehavior.SameExports source candidate := by\n" ++
    "  verify_linked_programs 16\n" ++
    "private def verified : Except String (PLift (LinkedBehavior.ASTEquivalent sourceAST providers files plan)) :=\n" ++
    "  LinkedBehavior.certifyASTs sourceAST providers files plan source candidate executions\n" ++
    "#audit_linked_programs verified\n" ++
    "#eval IO.ofExcept (verified.map fun _ => ())\n"

end CoreJS.LinkedBehavior
