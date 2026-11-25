import Lean.Elab.Deriving.ToExpr
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
deriving instance Lean.ToExpr for RegExpData
deriving instance Lean.ToExpr for RegExpGetter
deriving instance Lean.ToExpr for WellKnownSymbol
deriving instance Lean.ToExpr for Prim
deriving instance Lean.ToExpr for FuncKind
deriving instance Lean.ToExpr for OpaqueRun
deriving instance Lean.ToExpr for OpaqueHoistKind
deriving instance Lean.ToExpr for Expr, Target, PropDef, ClassMember, Func, BindingPattern, Stmt
deriving instance Lean.ToExpr for Import
deriving instance Lean.ToExpr for Module
deriving instance Lean.ToExpr for Program

deriving instance Lean.ToExpr for CoreJS.Addr
deriving instance Lean.ToExpr for CoreJS.ErrorKind
deriving instance Lean.ToExpr for CoreJS.Val
deriving instance Lean.ToExpr for CoreJS.PropertyKey
deriving instance Lean.ToExpr for CoreJS.Property
deriving instance Lean.ToExpr for CoreJS.Env
deriving instance Lean.ToExpr for CoreJS.Code
deriving instance Lean.ToExpr for CoreJS.Obj
deriving instance Lean.ToExpr for CoreJS.Binding

end CoreJS.LinkedBehavior
