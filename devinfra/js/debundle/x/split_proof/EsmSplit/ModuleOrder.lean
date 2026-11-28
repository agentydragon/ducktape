namespace EsmSplit

/-- Synchronous ES module evaluation visits imports depth-first in source order.
  A module runs after its dependencies. Recording entry before descent skips cycles.
  Fuel bounds the recursion depth. -/
def visitImports (imports : String → List String) :
    Nat → String → List String × List String → List String × List String
  | 0, _, st => st
  | fuel + 1, m, (entered, done) =>
    if entered.contains m then (entered, done)
    else
      let st := (imports m).foldl (fun st s => visitImports imports fuel s st) (m :: entered, done)
      (st.1, st.2 ++ [m])

end EsmSplit
