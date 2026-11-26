import EsmSplit.CoreJS.Fragment

open CoreJS

def main : IO Unit := do
  let ctor := Func.opaque "Base" .classCtor false 0 0 0 [] none
  IO.println (Expr.cls "Base" false ctor [.field true (.str "x") .undef]).allocOnly
  IO.println (Expr.cls "Base" false ctor [.field true (.str "x") (.call (.var "effect") [])]).allocOnly
