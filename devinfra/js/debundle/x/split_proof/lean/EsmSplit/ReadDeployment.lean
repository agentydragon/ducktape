import EsmSplit.SplitFiles
import EsmSplit.CoreJS.LinkedDeployment

/-! Read the route snapshot for a static deployment that overlays only the
emitted files on the original asset directory. -/

namespace EsmSplit.SplitFiles

open Lean
open CoreJS

private def absoluteOrigin (text : String) : Bool := Id.run do
  let some authority := text.dropPrefix? "http://" <|> text.dropPrefix? "https://" | return false
  let validHost (host : String) := !host.isEmpty &&
    host.toList.all (fun c => c.toNat < 128 && (c.isAlphanum || c == '.' || c == '-'))
  match authority.toString.splitOn ":" with
  | [host] => return validHost host
  | [host, port] => return validHost host && port.toNat?.any fun n => n > 0 && n ≤ 65535
  | _ => return false

def readDeployment (program providers : Json) (bundlePath work : System.FilePath)
    (origin : String) (P : EsmSplit.Plan) : IO LinkedDeployment.Input := do
  unless absoluteOrigin origin do throw (IO.userError "deployment origin must be an HTTP(S) origin without a path")
  unless bundlePath.extension == some "js" || bundlePath.extension == some "mjs" do
    throw (IO.userError "deployment checks require the original JavaScript file")
  let some entry := bundlePath.fileName | throw (IO.userError "source module is missing a filename")
  let source ← IO.ofExcept (program.getObjValAs? String "sourceText")
  let uses ← IO.ofExcept (ModuleLocation.entryUses program entry source)
  let staticProviders ← IO.ofExcept providers.getObj?
  let linked := Std.HashSet.ofList (entry :: staticProviders.toList.map Prod.fst)
  let root := bundlePath.parent.getD "."
  for module in P.mods do
    if module.name != entry && (← (root / module.name).pathExists) then
      throw (IO.userError s!"output path already exists: {module.name}")
  let asset (name text : String) : ModuleLocation.Resource := { effectiveUrl := origin ++ "/" ++ name, source := text }
  let mut resources := Std.HashMap.ofList [(entry, asset entry source)]
  let mut parsed : Array (String × Json) := #[]
  let mut seen := linked
  for (name, ast) in staticProviders.toList do
    let text ← IO.ofExcept (ast.getObjValAs? String "sourceText")
    resources := resources.insert name (asset name text)
  let mut frontier := uses.filterMap fun (_, use) =>
    match use with | .dynamicImport _ target => some target | .metaUrl => none
  while !frontier.isEmpty do
    let mut requests := #[]
    for name in frontier do
      unless seen.contains name do
        seen := seen.insert name
        requests := requests.push name
    if requests.isEmpty then break
    let batch ← readModules bundlePath work requests
    frontier := #[]
    for name in requests do
      let ast ← IO.ofExcept (batch.getObjVal? name)
      let text ← IO.ofExcept (ast.getObjValAs? String "sourceText")
      let resource := asset name text
      parsed := parsed.push (name, ast)
      resources := resources.insert name resource
      let imports ← IO.ofExcept (DynamicImports.requests name ast resource)
      frontier := frontier ++ imports.map (·.source)
  return { entry, assets := .mkObj parsed.toList, before := resources }

end EsmSplit.SplitFiles
