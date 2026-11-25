import EsmSplit.CoreJS.Eval

open CoreJS

private def global : GlobalEnv := ⟨[("lexical", ⟨1, 0⟩), ("tdz", ⟨1, 1⟩),
  ("constant", ⟨1, 2⟩)], globalObject, .obj globalObject⟩
private def env : Env := ⟨[], .undef, none⟩
private def call : Call World.probe := callFn 30

private def showVal : Val → String
  | .str s => s
  | .bool true => "true"
  | .bool false => "false"
  | .undef => "undefined"
  | .obj a => if a == globalObject then "global" else "object"
  | .error .reference _ => "ReferenceError"
  | .error .type _ => "TypeError"
  | _ => "other"

private def outcome (m : M World.probe Val) : M World.probe String := do
  match ← attempt m with
  | .ok v => pure (showVal v)
  | .error e => pure (showVal e)

private def read (name : Name) : M World.probe Val := do
  readGlobal call name (← resolveGlobal env global name)

private def write (name : Name) (v : Val) : M World.probe Val := do
  let ref ← resolveGlobal env global name
  writeGlobal call name ref v
  pure v

private def scenario : M World.probe (List String) := do
  let _ ← newCell ⟨some (.str "declarative"), true⟩
  let _ ← newCell ⟨none, true⟩
  let _ ← newCell ⟨some (.str "fixed"), false⟩
  createData globalObject (.str "lexical") (.str "property")
  createData globalObject (.str "mutable") (.str "before")
  createData globalObject (.str "presentUndefined") .undef
  let getterObj : Obj := { proto := some functionProto, props := [], code := some (.closure (.mk "get" .method false [] [.ret (.str "getter")]) ⟨[], .undef, none⟩ []) }
  let getter ← alloc getterObj
  defineOwn globalObject (.str "accessor") { get := some (.obj getter), configurable := some true }
  let a ← outcome (read "lexical")
  let b ← outcome (getProp call (.obj globalObject) (.str "lexical"))
  let c ← outcome (write "mutable" (.str "after"))
  let d ← outcome (getProp call (.obj globalObject) (.str "mutable"))
  let e ← outcome (read "accessor")
  let f ← outcome (typeofGlobal call "missing" (← resolveGlobal env global "missing"))
  let g ← outcome (typeofGlobal call "tdz" (← resolveGlobal env global "tdz"))
  let h ← outcome (write "constant" (.str "new"))
  let i ← outcome (read "missing")
  let j ← outcome (typeofGlobal call "presentUndefined" (← resolveGlobal env global "presentUndefined"))
  let del ← deleteGlobal "mutable" (← resolveGlobal env global "mutable")
  let k ← outcome (read "mutable")
  let stale ← resolveGlobal env global "accessor"
  let _ ← deleteGlobal "accessor" stale
  let l ← outcome (writeGlobal call "accessor" stale (.str "late") *> pure .undef)
  pure [a,b,c,d,e,f,g,h,i,j, toString del,k,l]

def main : IO Unit := do
  let s := { initialState World.probe 1 with region := 1 }
  match scenario s with
  | .ok values _ => IO.println (String.intercalate "\n" values)
  | .throw _ _ => throw (IO.userError "scenario unexpectedly threw")
  | .unknown why _ => throw (IO.userError why)
  | .fuel => throw (IO.userError "scenario exhausted fuel")
