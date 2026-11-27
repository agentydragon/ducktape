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

def readDeployment (entry : String) (originals : List (String × Json)) (root work : System.FilePath)
    (origin : String) (options : ModuleLocation.BrowserOptions := {}) : IO LinkedDeployment.Input := do
  unless absoluteOrigin origin do throw (IO.userError "deployment origin must be an HTTP(S) origin without a path")
  let asset (name text : String) : ModuleLocation.Resource := { effectiveUrl := origin ++ "/" ++ name, source := text }
  let mut resources : Std.HashMap String ModuleLocation.Resource := {}
  let mut parsed : Array (String × Json) := #[]
  let mut seen := Std.HashSet.ofList (originals.map Prod.fst)
  let mut frontier := #[]
  for (name, ast) in originals do
    let text ← IO.ofExcept (ast.getObjValAs? String "sourceText")
    let res := asset name text
    resources := resources.insert name res
    let sites ← IO.ofExcept ((ModuleLocation.providerUses ast name res options).mapError fun why => s!"{name}: {why}")
    for (_, use) in sites do
      if let .dynamicImport _ target := use then frontier := frontier.push target
  while !frontier.isEmpty do
    let mut requests := #[]
    for name in frontier do
      unless seen.contains name do
        seen := seen.insert name
        requests := requests.push name
    if requests.isEmpty then break
    let batch ← readModules root work requests
    frontier := #[]
    for name in requests do
      let ast ← IO.ofExcept (batch.getObjVal? name)
      let text ← IO.ofExcept (ast.getObjValAs? String "sourceText")
      let resource := asset name text
      parsed := parsed.push (name, ast)
      resources := resources.insert name resource
      let imports ← IO.ofExcept (DynamicImports.requests name ast resource)
      frontier := frontier ++ imports.map (·.source)
  return { entry, options, assets := .mkObj parsed.toList, before := resources }

end EsmSplit.SplitFiles
