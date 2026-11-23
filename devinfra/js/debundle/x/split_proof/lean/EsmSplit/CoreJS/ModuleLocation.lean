import EsmSplit.CoreJS.ModuleInput
import EsmSplit.ESTreeUnits
import EsmSplit.Lower
import EsmSplit.ModulePath
import EsmSplit.Plan

/-! Check the location-dependent inputs of a proposed split in a static-file deployment.
The caller supplies the effective URL and JavaScript source served at each requested path in each
deployment. Absence means that path does not exist. The snapshots cover both the relative targets and
the new output names. The paired decoder separately checks the syntax in the emitted files.

Module loading has other observable effects. In particular a dynamic target can statically import
the entry and acquire its live exports. Matching its URL and source does not establish matching
module records, initialization, namespace values or promise reactions. -/

namespace CoreJS.ModuleLocation

open Lean (Json)

structure Resource where
  effectiveUrl : String
  source : String
  deriving BEq

inductive Use where
  | metaUrl
  | dynamicImport (request target : String)
  deriving Repr, BEq

private def importMeta (j : Json) : Bool :=
  Lower.typ j == "MetaProperty" &&
    (j.getObjVal? "meta" >>= FromESTree.identifier).toOption == some "import" &&
    (j.getObjVal? "property" >>= FromESTree.identifier).toOption == some "meta"

/-- URL dependencies present in a parsed node, including nested function bodies. -/
partial def uses (entry : String) (j : Json) : Except String (Array Use) := do
  if Lower.typ j == "MemberExpression" && importMeta (Lower.fld j "object") then
    unless (← j.getObjValAs? Bool "computed") == false &&
        (j.getObjVal? "property" >>= FromESTree.identifier).toOption == some "url" do
      throw "expected import.meta.url"
    return #[.metaUrl]
  if importMeta j then throw "expected import.meta.url"
  if Lower.typ j == "ImportExpression" then
    unless Lower.fld j "options" == .null do throw "unsupported dynamic import options"
    let request ← j.getObjVal? "source" >>= FromESTree.stringLiteral
    let target ← ModuleInput.resolve entry request
    return #[.dynamicImport request target]
  let mut out := #[]
  for child in Lower.children j do out := out ++ (← uses entry child)
  return out

/-- Check every URL-sensitive source ordinal against the placement, equal effective entry URLs,
and the same effective target URL and source for every literal relative dynamic import. The plan
must use fresh output paths. Import maps, custom loaders and context-dependent responses require
a different deployment model. This is a location check, separate from the execution semantics. -/
def check (source : Json) (sourceEntry : String) (P : EsmSplit.Plan)
    (before after : String → Option Resource) : Except String (Array (Nat × Use)) := do
  let names := P.mods.map (·.name)
  EsmSplit.validateModulePaths (sourceEntry :: P.entry :: names)
  unless sourceEntry == P.entry do throw "source entry path differs from emitted entry path"
  unless names.Nodup do throw "duplicate output path"
  let some entry := P.find P.entry | throw "missing entry placement"
  let some old := before sourceEntry | throw "missing original entry resource"
  let some new := after P.entry | throw "missing emitted entry resource"
  unless !old.effectiveUrl.isEmpty && old.effectiveUrl == new.effectiveUrl do throw "effective entry URL changed"
  for name in names do
    if name != P.entry && (before name).isSome then throw s!"output path already exists: {name}"
  unless (← source.getObjValAs? String "sourceText") == old.source do throw "parsed source differs from original resource"
  let nodes ← ESTreeUnits.checkedNodes source old.source
  unless (P.mods.flatMap (·.stmts)).Perm (List.range nodes.size) do throw "placement must cover every original unit once"
  let retained := Std.HashSet.ofList entry.stmts
  let mut sites := #[]
  for (node, id) in nodes.zipIdx do
    if let some why := Lower.outsideModel false node then throw s!"unit {id}: {why}"
    for use in (← (uses sourceEntry node).mapError fun why => s!"unit {id}: {why}") do
      unless retained.contains id do throw s!"URL-sensitive unit {id} moved from the entry"
      if let .dynamicImport _ target := use then
        if names.contains target then throw s!"dynamic target is an output module: {target}"
        let some resource := before target | throw s!"missing original dynamic target {target}"
        unless !resource.effectiveUrl.isEmpty && after target == some resource do
          throw s!"dynamic target URL or source changed: {target}"
      sites := sites.push (id, use)
  return sites

end CoreJS.ModuleLocation
