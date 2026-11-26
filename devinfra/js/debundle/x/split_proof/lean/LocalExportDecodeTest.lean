import EsmSplit.CoreJS.LinkedInput
import EsmSplit.CoreJS.TerminalConstructorInput

open Lean (Json)
open CoreJS

private def onlyModule (p : Program) : IO Module := do
  let [m] := p.modules | throw (IO.userError "expected exactly one source module")
  unless m.name == p.entry do throw (IO.userError "source entry changed")
  return m

private def decode (j : Json) : IO (Program × List EsmSplit.LocalExportUnit) :=
  IO.ofExcept (LinkedInput.decodeSingletonSourceWithLocalExports j "entry.mjs")

private def require (ok : Bool) (why : String) : IO Unit :=
  unless ok do throw (IO.userError why)

private def checkGraph (j : Json) : IO Unit := do
  let (p, rows) ← decode j
  let m ← onlyModule p
  let [row] := rows | throw (IO.userError "expected one standalone export list")
  let pairs := (List.range 16).map fun i => (s!"reader{i}", s!"reader{i}")
  require (row.ordinal == 32 && row.names == pairs) "lost #32 or its ordered reader exports"
  require (m.body.map Prod.fst == List.range 32) "lost original executable identities"
  require (m.exports == pairs) "module exports differ from the original export list"
  let checked ← IO.ofExcept (TerminalConstructorInput.check (p, rows))
  let _ := TerminalConstructorInput.all_const checked
  let _ := TerminalConstructorInput.source_shape checked
  let badImport : Program := { p with modules := [{ m with imports := [⟨"provider.mjs", []⟩] }] }
  match TerminalConstructorInput.check (badImport, rows) with
  | .error "terminal source must not import modules" => pure ()
  | _ => throw (IO.userError "a directly supplied source with an import was not refused")
  let badBody := m.body.map fun (id, stmt) =>
    if id == 0 then (id, .decl .let_ "data0" (.str "value0")) else (id, stmt)
  let badDeclaration : Program := { p with modules := [{ m with body := badBody }] }
  match TerminalConstructorInput.check (badDeclaration, rows) with
  | .error "expected unique const declarations with only primitive initializers or earlier reads" => pure ()
  | _ => throw (IO.userError "a non-const original statement was not refused")

private def checkSparse (j : Json) : IO Unit := do
  let (p, rows) ← decode j
  let m ← onlyModule p
  let [row] := rows | throw (IO.userError "expected one interior export list")
  require (row.ordinal == 1 && row.names == [("exposed", "seed")]) "interior export or alias changed"
  require (m.body.map Prod.fst == [0, 2]) "interior export shifted executable identities"
  require (m.exports == [("exposed", "seed")]) "interior public binding changed"
  match TerminalConstructorInput.check (p, rows) with
  | .error "expected dense executable original IDs followed by one terminal metadata unit" => pure ()
  | _ => throw (IO.userError "interior export was not refused by the terminal source guard")

private def checkAliases (j : Json) : IO Unit := do
  let (p, rows) ← decode j
  let m ← onlyModule p
  let [aliases, empty] := rows | throw (IO.userError "expected alias and empty export lists")
  let pairs := [("publicName", "seed"), ("with-dash", "seed")]
  require (aliases.ordinal == 1 && aliases.names == pairs) "ordered public/local aliases changed"
  require (empty.ordinal == 2 && empty.names.isEmpty) "empty export list was omitted"
  require (m.body.map Prod.fst == [0, 3]) "export metadata became executable"
  require (m.exports == pairs) "alias export table changed"

private def checkInline (j : Json) : IO Unit := do
  let (p, rows) ← decode j
  let m ← onlyModule p
  require rows.isEmpty "inline export became standalone metadata"
  require (m.body.map Prod.fst == [0] && m.exports == [("declared", "declared")])
    "inline export changed"

private def checkDefault (j : Json) : IO Unit := do
  let (p, rows) ← decode j
  let m ← onlyModule p
  require rows.isEmpty "default export became standalone metadata"
  require (m.body.map Prod.fst == [0] && m.exports == [("default", "default")])
    "default export changed"

private def checkMixed (j : Json) : IO Unit := do
  let (p, rows) ← decode j
  let m ← onlyModule p
  let [row] := rows | throw (IO.userError "expected only the standalone export row")
  require (row.ordinal == 1 && row.names == [("alias", "declared")])
    "inline export was incorrectly added to the standalone row"
  require (m.exports == [("declared", "declared"), ("alias", "declared")])
    "mixed inline and local export table changed"

private def checkRefused (j : Json) (expected : String) : IO Unit := do
  match LinkedInput.decodeSingletonSourceWithLocalExports j "entry.mjs" with
  | .ok _ => throw (IO.userError s!"expected refusal: {expected}")
  | .error why => require (why == expected) s!"expected {expected}; got {why}"

def main : IO UInt32 := do
  let line ← (← IO.getStdin).getLine
  let cases ← IO.ofExcept (Json.parse line >>= Json.getArr?)
  unless cases.size == 8 do throw (IO.userError "expected eight real parsed sources")
  checkGraph cases[0]!
  checkSparse cases[1]!
  checkAliases cases[2]!
  checkInline cases[3]!
  checkDefault cases[4]!
  checkRefused cases[5]! "unsupported re-export from another module"
  checkRefused cases[6]! "checked construction requires an import-free original"
  checkMixed cases[7]!
  return 0
