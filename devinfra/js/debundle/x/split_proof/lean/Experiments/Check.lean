import Experiments.CheckOutput

open EsmSplit Lean

structure Facts where
  bundle : List Stmt
  entry  : ModName
  outs   : List OutModule
  deriving FromJson

/-- Why `takeStmts` failed: the first statement text that is not an unused bundle statement. -/
def firstUnmatched (B : Bundle) (outs : List OutModule) : Option (ModName × String) :=
  let rec go : List OutModule → List Nat → Option (ModName × String)
    | [], _ => none
    | o :: os, pool =>
      match o.body.foldl (fun (st : Except String (List Nat)) t => st.bind fun pool =>
          match pool.find? fun i => (B.stmt i).src == t with
          | some i => .ok (pool.erase i)
          | none => .error t) (.ok pool) with
      | .error t => some (o.name, t)
      | .ok pool' => go os pool'
  go outs (List.range B.length)

def showViolations (B : Bundle) (P : Plan) : IO Unit := do
  let ord := P.execOrder
  let pairs := (ord.zipIdx.flatMap fun (a, ia) => (ord.zipIdx.filter (fun (_, ib) => ia < ib)).filterMap
    fun (b, _) => if b < a && B.conflict a b then some (a, b) else none)
  for (a, b) in pairs.take 3 do
    IO.println s!"    statement {b} must run before statement {a} but the split runs {a} first: {(B.stmt b).src.take 50} / {(B.stmt a).src.take 50}"
  for m in P.mods do
    for i in m.stmts do
      for u in (B.stmt i).eager ++ (B.stmt i).lazy do
        if P.resolve B m u != P.declaredIn B u then
          IO.println s!"    {m.name}: `{u}` resolves to {repr (P.resolve B m u)}, the bundle means {repr (P.declaredIn B u)}"

def main (args : List String) : IO UInt32 := do
  let [path] := args | IO.eprintln "usage: check <facts.json>"; return 2
  let facts ← IO.ofExcept (fromJson? (← IO.ofExcept (Json.parse (← IO.FS.readFile path))) : Except String Facts)
  let B := facts.bundle
  let some P := matchOutput B facts.entry facts.outs | do
    match firstUnmatched B facts.outs with
    | some (m, t) => IO.println s!"REJECT  {m} contains a statement that is not an unused statement of the bundle:\n    {t}"
    | none => IO.println "REJECT  some bundle statement appears in no output module"
    return 1
  let checks : List (String × Bool) :=
    [ ("Matches   files say what the plan says; each statement exactly once", decide (Matches B facts.entry facts.outs P))
    , ("UniqueDefs", decide (UniqueDefs B)), ("ReadsClosed", decide (ReadsClosed B))
    , ("Linkable  imports/exports link", decide (Linkable B P))
    , ("Hygienic  every name keeps its declaration", decide (Hygienic B P))
    , ("Permutes  every statement runs once", decide (Permutes B P))
    , ("OrderOk   conflicting statements keep their order", decide (OrderOk B P)) ]
  IO.println s!"  module order     {" → ".intercalate P.evalOrder}"
  IO.println s!"  statement order  {P.execOrder}"
  for (n, ok) in checks do IO.println s!"  {if ok then "pass" else "FAIL"}  {n}"
  if checks.all (·.2) then IO.println "ACCEPT"; return 0
  showViolations B P
  IO.println "REJECT"; return 1
