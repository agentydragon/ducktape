import EsmSplit.Plan

namespace EsmSplit

/-- Output module names are URL paths inside the output tree. Restrict their spelling so the same names are
interpreted as the same relative paths by the filesystem and by the module loader. -/
def safeModulePath (path : String) : Bool := Id.run do
  if path.isEmpty || path.startsWith "/" || path.toList.any (· == '\\') then return false
  for segment in path.splitOn "/" do
    if segment.isEmpty || segment == "." || segment == ".." then return false
    for c in segment.toList do
      let alpha := ('a' ≤ c && c ≤ 'z') || ('A' ≤ c && c ≤ 'Z')
      let digit := '0' ≤ c && c ≤ '9'
      unless alpha || digit || c == '.' || c == '_' || c == '-' || c == '~' do return false
  return true

/-- Fail before creating output directories if a module path could escape the selected output root. -/
def validateModulePaths (paths : List String) : Except String Unit := do
  for path in paths do
    unless safeModulePath path do
      throw s!"unsafe output module path `{path}` (expected slash-separated URL-safe relative path segments)"

private def normalizePathFrom (path spec : String) : String :=
  let segments := (path.splitOn "/").dropLast ++ spec.splitOn "/"
  let stack := segments.foldl (fun (acc : List String) seg =>
    if seg == "." || seg == "" then acc
    else if seg == ".." then
      match acc with
      | [] => [".."]
      | ".." :: _ => ".." :: acc
      | _ :: rest => rest
    else seg :: acc) []
  "/".intercalate stack.reverse

private def commonPrefixLength (left right : List String) : Nat := Id.run do
  let mut n := 0
  for (a, b) in left.zip right do
    if a != b then break
    n := n + 1
  return n

/-- Split the validated URL path using lists so concrete renderings reduce in the kernel. -/
private def pathSegments (path : String) : List String :=
  let parts := path.toList.foldr (fun c (acc : List (List Char)) =>
    if c == '/' then [] :: acc else (c :: acc.headD []) :: acc.drop 1) [[]]
  parts.map String.ofList

/-- A URL-relative specifier from one generated module file to another. -/
def relativeUrlSpecifier (fromFile targetFile : String) : String := Id.run do
  let fromDirs := (pathSegments fromFile).dropLast
  let target := pathSegments targetFile
  let outside := target.takeWhile (· == "..")
  let targetTail := target.drop outside.length
  let parts := if outside.isEmpty then
      let common := commonPrefixLength fromDirs targetTail
      List.replicate (fromDirs.length - common) ".." ++ targetTail.drop common
    else List.replicate (fromDirs.length + outside.length) ".." ++ targetTail
  let relative := "/".intercalate parts
  return if parts.head? == some ".." then relative else "./" ++ relative

private def safeRelativeRequest (source : String) : Bool := Id.run do
  if !(source.startsWith "./" || source.startsWith "../") then return false
  if source.endsWith "/" then return false
  let parts := source.splitOn "/"
  if parts.length < 2 then return false
  if parts.getLast! == "." || parts.getLast! == ".." then return false
  for part in parts.drop 1 do
    if part.isEmpty then return false
    for c in part.toList do
      let alpha := ('a' ≤ c && c ≤ 'z') || ('A' ≤ c && c ≤ 'Z')
      let digit := '0' ≤ c && c ≤ '9'
      unless alpha || digit || c == '.' || c == '_' || c == '-' || c == '~' do return false
  return true

/-- Rebase a source-relative request when its generated importer is nested below the output root. The original
bundle is treated as living at that root: `./dep.js` from `nested/entry.js` becomes `../dep.js`. -/
def rebaseRelativeRequest (modulePath source : String) : Except String String := do
  unless modulePath.contains '/' && source.startsWith "." do return source
  unless safeRelativeRequest source do
    throw s!"relative request `{source}` cannot be represented safely from nested output module `{modulePath}`"
  let target := normalizePathFrom "bundle.mjs" source
  if target.isEmpty || target == "." then
    throw s!"relative request `{source}` does not name a file from nested output module `{modulePath}`"
  return relativeUrlSpecifier modulePath target

/-- Blocks of lines with an empty line between them; an empty block is left out. -/
def renderBlocks (blocks : List (List String)) : String :=
  "\n\n".intercalate ((blocks.filter (!·.isEmpty)).map ("\n".intercalate ·)) ++ "\n"

/-- The glue `import`s of a module. A binding its source exports under another name is renamed back. -/
def Module.importLines (P : Plan) (m : Module) : List String :=
  m.imports.map fun (src, ns) =>
    -- Module destinations contain only URL-safe path segments, so no JS escapes are needed.
    let specifier := "\"" ++ relativeUrlSpecifier m.name src ++ "\""
    if ns.isEmpty then s!"import {specifier};"
    else
      let source := P.find src
      let spec (u : Name) :=
        let p := (source.map (·.publicName u)).getD u
        if p == u then u else s!"{p} as {u}"
      s!"import \{ {", ".intercalate (ns.map spec)} } from {specifier};"

/-- The glue `export` of a module. -/
def Module.exportLines (m : Module) : List String :=
  if m.exports.isEmpty then [] else
    [s!"export \{ {", ".intercalate (m.exports.map fun (p, u) => if p == u then u else s!"{u} as {p}")} };"]

/-- Source text of a module: its `import`s, its statements verbatim, its `export`. -/
def Module.render (B : Bundle) (P : Plan) (m : Module) : String :=
  renderBlocks [m.importLines P, m.stmts.map fun i => (B.stmt i).src, m.exportLines]

end EsmSplit
