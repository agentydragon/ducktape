import Lean
import EsmSplit.CoreJS.LinkedBehavior

namespace CoreJS.LinkedBehavior

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

private def stringLiteral (s : String) : String :=
  if s.toList.all (fun c => ' ' ≤ c && c ≤ '~' && c != '"' && c != '\\') then
    "\"" ++ s ++ "\""
  else
    "(String.ofList [" ++ String.intercalate "," (s.toList.map fun c =>
      "Char.ofNat " ++ toString c.toNat) ++ "])"

/-- Render only the constructor applications, closed constants and literals produced by
`ToExpr`. Program strings are data, including line breaks, quotes and Lean comment delimiters. -/
private partial def term (e : Lean.Expr) : Except String String :=
  match e with
  | .app .. => do
    let f ← term e.getAppFn
    let args ← e.getAppArgs.toList.mapM term
    return "(" ++ String.intercalate " " (f :: args) ++ ")"
  | .const n _ => return "@" ++ n.toString
  | .lit (.strVal s) => return stringLiteral s
  | .lit (.natVal n) => return toString n
  | _ => throw "unsupported expression in quoted CoreJS data"

/-- A closed Lean constructor term for the complete decoded program. -/
def programLiteral (p : Program) : Except String String := term (Lean.toExpr p)

/-- Prove both obligations by evaluating the real module graphs with an arbitrary host world.
`cbv` produces kernel-checkable equations for definitions with well-founded recursion. -/
macro "verify_linked_programs " fuel:num : tactic => `(tactic|
  constructor <;> (first | apply of_evaluation $fuel | apply sameExports_of_match) <;>
    intro w <;> first | rfl | cbv <;> rfl)

/-- The generated module proves an all-world, fuel-independent claim for these two programs.
The caller accepts only successful elaboration and kernel checking of the complete file. -/
def certificate (source candidate : Program) : Except String String := do
  let s ← programLiteral source
  let c ← programLiteral candidate
  return "import EsmSplit.CoreJS.LinkedCertificate\n\nopen CoreJS\n" ++
    "set_option maxRecDepth 16000\nset_option maxHeartbeats 2000000\nset_option cbv.warning false\n" ++
    "private def source : Program := " ++ s ++ "\n" ++
    "private def candidate : Program := " ++ c ++ "\n" ++
    "theorem verified : LinkedBehavior.Equivalent source candidate ∧ LinkedBehavior.SameExports source candidate := by\n" ++
    "  verify_linked_programs 16\n"

end CoreJS.LinkedBehavior
