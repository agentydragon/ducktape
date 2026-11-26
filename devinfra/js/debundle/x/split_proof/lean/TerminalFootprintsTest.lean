import EsmSplit.CoreJS.TerminalFootprints

open CoreJS

private def statements : List Stmt :=
  [.decl .const "first" (.str "value"), .decl .const "second" (.var "first")]

private def row : EsmSplit.LocalExportUnit :=
  ⟨2, [("exposed", "second"), ("alias", "second")]⟩

private def bundle : EsmSplit.Bundle := [
  { src := "", defs := ["first"] },
  { src := "", defs := ["second"], eager := ["first"] },
  { src := "", eager := ["second"], exports := [("exposed", some "second"), ("alias", some "second")] }]

def main : IO Unit := do
  unless decide (MetadataCells.Footprints statements bundle) do
    throw (IO.userError "valid full source footprints refused")
  unless decide (row.FootprintMatches (bundle.stmt 2)) do
    throw (IO.userError "deduplicated aliased export row refused")
  let badMetadata := bundle.take 2 ++ [{ src := "", defs := ["unexpected"] }]
  if decide (MetadataCells.Footprints statements badMetadata) then
    throw (IO.userError "metadata with an extra declaration accepted")
  let badUse := [bundle[0]!, { src := "", defs := ["second"] }, bundle[2]!]
  if decide (MetadataCells.Footprints statements badUse) then
    throw (IO.userError "missing executable free read accepted")
  let badRow : EsmSplit.Stmt := { src := "", eager := ["second"], exports := [("alias", some "second")] }
  if decide (row.FootprintMatches badRow) then
    throw (IO.userError "incomplete public export row accepted")
  let badLocal : EsmSplit.Stmt := { src := "", exports := (bundle.stmt 2).exports }
  if decide (row.FootprintMatches badLocal) then
    throw (IO.userError "export without its local use accepted")
