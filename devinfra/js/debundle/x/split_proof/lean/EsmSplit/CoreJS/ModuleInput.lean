import EsmSplit.CoreJS.FromESTree

/-! Static module requests in parsed ESTree and emitted glue. -/
namespace CoreJS.ModuleInput

open Lean (Json)

private def typ (j : Json) := j.getObjValAs? String "type"

private def fileSegment (s : String) : Bool :=
  !s.isEmpty && s != "." && s != ".." &&
    s.toList.all fun c => c.isAlphanum || ['.', '_', '-', '~'].contains c

/-- A relative module filename suitable for both the filesystem and the URL resolver. -/
def supportedFilename (name : String) : Bool :=
  (name.splitOn "/").all fileSegment

/-- Resolve a local URL request from the importing file's directory. Refuse paths that need
URL decoding, query or fragment semantics, or access outside the supplied module tree. -/
def resolve (importer request : String) : Except String String := do
  unless request.startsWith "./" || request.startsWith "../" do throw "expected a relative module request"
  let parts := request.splitOn "/"
  unless parts.getLast?.any fileSegment do throw "expected a module filename"
  let mut dir := (importer.splitOn "/").reverse.drop 1
  for part in parts do
    if part == ".." then
      let _ :: parent := dir | throw "import leaves the module tree"
      dir := parent
    else if part != "." then
      unless fileSegment part do throw "unsupported module URL segment"
      dir := part :: dir
  return "/".intercalate dir.reverse

def noAttributes (j : Json) : Except String Unit := do
  if let .ok attrs := j.getObjVal? "attributes" then
    unless (← attrs.getArr?).isEmpty do throw "unsupported module attributes"

/-- Read a named or side-effect-only import and resolve its relative module request. -/
def importDecl (importer : String) (j : Json) : Except String Import := do
  unless (← typ j) == "ImportDeclaration" do throw "expected an import declaration"
  let request ← j.getObjVal? "source" >>= FromESTree.stringLiteral
  let source ← resolve importer request
  noAttributes j
  let specs ← j.getObjValAs? (List Json) "specifiers"
  let names ← specs.mapM fun sp => do
    unless (← typ sp) == "ImportSpecifier" do throw "expected a named import"
    return (← FromESTree.moduleExportName (← sp.getObjVal? "imported"),
      ← FromESTree.identifier (← sp.getObjVal? "local"))
  return { source, names }

end CoreJS.ModuleInput
