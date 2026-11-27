import EsmSplit.CoreJS.Eval

/-! Initializers for single module-level `var` declarations. A declaration may
read earlier bindings or its own already allocated cell. -/

namespace CoreJS.VarInitializer

def check (earlier : List Name) (name : Name) : Expr → Bool
  | .undef | .null | .bool _ | .num _ | .str _ => true
  | .var read => read == name || earlier.contains read
  | _ => false

theorem binding_before_initializer (name : Name) (init : Expr) :
    (Stmt.decl .var name init).bindings = [(name, { value := some .undef, mutable := true })] := by
  simp [Stmt.bindings, varNamesOf, Stmt.varNames, lexDecls] <;> rfl

theorem execute_initializer {w : World} (fuel : Nat) (env : Env) (name : Name) (init : Expr) :
    execStmt (W := w) (fuel + 1) env (.decl .var name init) = (do
      writeVar env name (← evalExpr fuel env init)
      pure .normal) := rfl

end CoreJS.VarInitializer
