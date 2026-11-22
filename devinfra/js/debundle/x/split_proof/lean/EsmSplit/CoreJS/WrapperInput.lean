import EsmSplit.CoreJS.WrapperPair
import EsmSplit.ESTreeUnits
import EsmSplit.Lower
import EsmSplit.ModulePath
import Std.Data.HashMap

/-! Recognize the creation phase of a linked lazy wrapper. Both inputs are the
shared frontend's annotated ESTree. Provider evaluation must already have
completed, and the two requests must resolve to this same provider instance
before and after the split. The result describes the source shapes for a
pairwise check. -/

namespace EsmSplit
open Lean (Json)

namespace WrapperInput
open Lower (typ fld items str flag children isNull)

private def ident (j : Json) : Except String String := do
  unless typ j == "Identifier" do throw "expected an identifier"
  j.getObjValAs? String "name"

private def ordinaryArrow (j : Json) : Bool :=
  typ j == "ArrowFunctionExpression" && !flag j "async" && !flag j "generator"

private partial def existsNode (p : Json → Bool) (j : Json) : Bool :=
  p j || (children j).any (existsNode p)

private def mentions (name : String) (j : Json) : Bool :=
  existsNode (fun n => typ n == "Identifier" && str (fld n "name") == name) j

private def hasOuterReferences (j : Json) : Bool :=
  existsNode (fun n =>
    (typ n == "Identifier" && ["T", "V", "G"].contains (str (fld n "k"))) ||
    ["ThisExpression", "Super", "MetaProperty", "ImportExpression"].contains (typ n)) j

private def valueDecl (node : Json) : Except String (String × String × Json) := do
  unless typ node == "VariableDeclaration" do throw "expected a variable declaration"
  let #[d] := items (fld node "declarations") | throw "expected a single declarator"
  let id := fld d "id"
  unless str (fld id "k") == "T" do throw "expected a module binding"
  return (← ident id, ← node.getObjValAs? String "kind", fld d "init")

private def localExport (s : Json) : Bool :=
  typ s == "ExportNamedDeclaration" && isNull (fld s "source") && (items (fld s "attributes")).isEmpty

private def exportedBindings (units : Array Json) : Except String (List (String × String)) := do
  let mut exports := []
  for s in units do
    if localExport s then
      let decl := fld s "declaration"
      if isNull decl then
        for sp in items (fld s "specifiers") do
          let publicName ← ident (fld sp "exported")
          let localName ← ident (fld sp "local")
          exports := (publicName, localName) :: exports
      else
        let (name, _, _) ← valueDecl decl
        exports := (name, name) :: exports
    else unless typ s == "VariableDeclaration" do
      throw "provider must consist of variable declarations and local named exports"
  return exports

private def factoryParams (p : Array Json) (bind : String) : Except String (List String) := do
  let mut factory := none
  for s in p do
    let decl := if localExport s then fld s "declaration" else s
    unless isNull decl do
      if let .ok (name, kind, init) := valueDecl decl then
        if name == bind then
          unless factory.isNone && kind == "var" do
            throw s!"provider binding {bind} is not a unique var declaration"
          factory := some init
        else if mentions bind decl then throw s!"provider binding {bind} occurs outside its declaration or export"
      else if mentions bind decl then throw s!"provider binding {bind} occurs outside its declaration or export"
  let some init := factory | throw s!"provider binding {bind} has no declaration"
  unless ordinaryArrow init do throw s!"provider binding {bind} is not an ordinary arrow"
  let params ← (items (fld init "params")).toList.mapM ident
  unless [1, 2].contains params.length do throw "factory must take one or two ordinary parameters"
  let outerBody := fld init "body"
  let returned ← if typ outerBody == "BlockStatement" then do
    let #[ret] := items (fld outerBody "body") | throw "factory block must only return the deferred arrow"
    unless typ ret == "ReturnStatement" do throw "factory block must only return the deferred arrow"
    pure (fld ret "argument")
  else pure outerBody
  unless ordinaryArrow returned && (items (fld returned "params")).isEmpty do
    throw "factory must return a zero-argument arrow without executing its body"
  -- A deferred body may use the factory's parameter cells and its own locals.
  -- Module, host and location-sensitive occurrences would need additional premises.
  if hasOuterReferences returned then
    throw "deferred arrow captures more than the factory's local cells"
  return params

private structure Target where
  publicName : String
  binding : String
  params : List String

private def importTargets (consumer provider : Array Json) (specifier : String) :
    Except String (List (String × Except String Target)) := do
  let exports ← exportedBindings provider
  let mut targets := []
  for s in consumer do
    let source := fld s "source"
    if typ s == "ImportDeclaration" && typ source == "Literal" && str (fld source "value") == specifier then
      unless (items (fld s "attributes")).isEmpty do throw "wrapper import has attributes"
      for sp in items (fld s "specifiers") do
        if typ sp == "ImportSpecifier" then
          let alias ← ident (fld sp "local")
          let target : Except String Target := do
            let publicName ← ident (fld sp "imported")
            let some binding := exports.lookup publicName | throw s!"provider does not export {publicName}"
            let params ← factoryParams provider binding
            return { publicName, binding, params }
          targets := (alias, target) :: targets
  if targets.isEmpty then throw s!"consumer has no named import from {specifier}"
  return targets

private def pair (units : Array Json) (targets : List (String × Except String Target))
    (declarers : Std.HashMap String Nat)
    (earlier later : Nat) : Except String WrapperSwapShape := do
  let .isTrue increasing := (inferInstance : Decidable (earlier < later)) | throw "expected increasing source ordinals"
  let some first := units[earlier]? | throw s!"missing unit {earlier}"
  let some second := units[later]? | throw s!"missing unit {later}"
  let (resultBinding, kind, call) ← valueDecl first
  unless kind == "var" && typ call == "CallExpression" && !flag call "optional" do
    throw "earlier unit must initialize a var with an ordinary wrapper call"
  let callee := fld call "callee"
  let importAlias ← ident callee
  unless str (fld callee "k") == "V" do throw s!"callee {importAlias} is not an imported binding"
  let some target := targets.lookup importAlias | throw s!"callee {importAlias} does not import from the supplied provider"
  let target ← target
  let #[callback] := items (fld call "arguments") | throw "expected exactly one callback argument"
  let callbackConstructible := typ callback == "FunctionExpression" && !flag callback "async" && !flag callback "generator"
  unless ordinaryArrow callback || callbackConstructible do throw "argument must be an ordinary callable literal"
  let (movedBinding, kind, moved) ← valueDecl second
  let movedKind : CoreJS.DeclKind ← match kind with
    | "var" => pure .var
    | "let" => pure .let_
    | "const" => pure .const
    | _ => throw "expected an ordinary declaration kind for the later unit"
  unless resultBinding != movedBinding && ordinaryArrow moved do
    throw "later unit must initialize a different binding with an ordinary arrow"
  unless declarers.getD resultBinding 0 == 1 && declarers.getD movedBinding 0 == 1 do
    throw "result and moved bindings must each have a unique declaration"
  if hasOuterReferences moved then throw "moved arrow is not closed"
  return {
    importAlias, providerExport := target.publicName, providerBinding := target.binding,
    factoryParams := target.params, resultBinding, movedBinding, movedKind, earlier, later, increasing
  }

end WrapperInput

/-- Build the source-side pair checker once so that testing many candidate pairs does
not repeatedly walk the module or re-resolve its named imports. -/
def prepareWrapperSwaps (consumer provider : Json) (specifier : String) :
    Except String (Nat → Nat → Except String WrapperSwapShape) := do
  unless specifier.startsWith "./" && safeModulePath (specifier.drop 2).toString do
    throw "expected a canonical relative provider path"
  let units ← ESTreeUnits.nodes consumer
  let providerUnits ← ESTreeUnits.nodes provider
  if let some reason := (units.findSome? (Lower.outsideModel false)).orElse (fun _ =>
      providerUnits.findSome? (Lower.outsideModel false)) then throw reason
  let targets ← WrapperInput.importTargets units providerUnits specifier
  let mut declarers : Std.HashMap String Nat := {}
  for unit in units do
    for binding in Lower.topDefs unit do
      declarers := declarers.insert binding (declarers.getD binding 0 + 1)
  return WrapperInput.pair units targets declarers

end EsmSplit
