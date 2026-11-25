import EsmSplit.CertificateData

namespace CoreJS.LinkedBehavior

open Lean (mkConst mkApp mkApp2 mkApp3 mkApp5 mkAppN mkLambda toExpr)

private structure JsonDefs where
  definitions : Array String := #[]
  entries : EsmSplit.CertificateData.Entries := #[]
  shared : Std.HashMap Lean.Expr Lean.Name := {}

private abbrev JsonQuote := StateM JsonDefs

private def addJsonDefinition (name type : String) (value : Lean.Expr) : JsonQuote Lean.Expr := do
  let id := Lean.Name.mkSimple name
  modify fun s => { s with
    definitions := s.definitions.push s!"private def {name} : {type} := certificate_data% {name}\n"
    entries := s.entries.push (id, value) }
  return EsmSplit.CertificateData.ref id

private def shareJsonDefinition (stem type : String) (value : Lean.Expr) : JsonQuote Lean.Expr := do
  let s ← get
  if let some id := s.shared[value]? then
    return EsmSplit.CertificateData.ref id
  let name := s!"{stem}_{s.definitions.size}"
  modify fun s => { s with shared := s.shared.insert value (Lean.Name.mkSimple name) }
  addJsonDefinition name type value

private abbrev JsonTree := Std.DTreeMap.Internal.Impl String (fun _ => Lean.Json)

private def jsonType := mkConst ``Lean.Json

private def treeConstructor (name : Lean.Name) : Lean.Expr :=
  mkApp2 (mkConst name [0, 0]) (mkConst ``String) (mkLambda `_ .default (mkConst ``String) jsonType)

private def objectTree [Monad m] (value : Lean.Json → m Lean.Expr) : JsonTree → m Lean.Expr
  | .leaf => pure (treeConstructor ``Std.DTreeMap.Internal.Impl.leaf)
  | .inner size key item left right => do
    return mkApp5 (treeConstructor ``Std.DTreeMap.Internal.Impl.inner)
      (toExpr size) (toExpr key) (← value item) (← objectTree value left) (← objectTree value right)

private def objectParameter (i : Nat) : Lean.Name := Lean.Name.mkSimple s!"v{i}"

mutual
private partial def jsonLiteral (stem : String) : Lean.Json → JsonQuote Lean.Expr
  | .null => pure (mkConst ``Lean.Json.null)
  | .bool b => pure (mkApp (mkConst ``Lean.Json.bool) (toExpr b))
  | .num n => pure (mkApp (mkConst ``Lean.Json.num)
      (mkApp2 (mkConst ``Lean.JsonNumber.mk) (toExpr n.mantissa) (toExpr n.exponent)))
  | .str s => pure (mkApp (mkConst ``Lean.Json.str) (toExpr s))
  | .arr a => do return mkApp (mkConst ``Lean.Json.arr) (← arrayLiteral stem a 0 a.size)
  | .obj ⟨⟨o⟩⟩ => do
    let wrap := mkApp (mkConst ``EsmSplit.CertificateData.jsonFromTree)
    -- Common AST nodes share a function that fills the same raw object layout.
    if o.size == 0 || o.size > 16 then
      return wrap (← objectTree (jsonLiteral stem) o)
    let parameter (v : Lean.Json) : StateM (Array Lean.Json) Lean.Expr := do
      let args ← get
      set (args.push v)
      return EsmSplit.CertificateData.ref (objectParameter args.size)
    let (tree, args) := (objectTree parameter o).run #[]
    let params := (Array.range args.size).map objectParameter
    let body := (wrap tree).abstract (params.map EsmSplit.CertificateData.ref)
    let builder := params.foldr (fun param body => mkLambda param .default jsonType body) body
    let type := String.join (List.replicate params.size "Lean.Json → ") ++ "Lean.Json"
    let fn ← shareJsonDefinition (stem ++ "_object") type builder
    return mkAppN fn (← args.mapM (jsonLiteral stem))

/-- Keep each wide array fragment small enough to compile on its own. -/
private partial def arrayLiteral (stem : String) (a : Array Lean.Json) (start stop : Nat) : JsonQuote Lean.Expr := do
  let chunkSize := 8
  let count := stop - start
  let value ← if count ≤ chunkSize then do
    let nil := mkApp (mkConst ``List.nil [0]) jsonType
    let cons := mkApp (mkConst ``List.cons [0]) jsonType
    let items ← a.foldrM (fun value tail => do return mkApp2 cons (← jsonLiteral stem value) tail)
      nil (start := stop) (stop := start)
    pure (mkApp2 (mkConst ``List.toArray [0]) jsonType items)
  else do
    let middle := start + count / 2
    let left ← arrayLiteral stem a start middle
    let right ← arrayLiteral stem a middle stop
    pure (mkApp3 (mkConst ``Array.append [0]) jsonType left right)
  if a.size ≤ chunkSize then return value
  shareJsonDefinition (stem ++ "_array") "Array Lean.Json" value
end

/-- Store exact JSON terms in the same certificate data format as the CoreJS programs.
The generated file only declares their types and names. -/
def jsonDefinitions (roots : List (String × Lean.Json)) :
    Array String × EsmSplit.CertificateData.Entries :=
  let quote : JsonQuote Unit := do
    for (name, json) in roots do
      let _ ← addJsonDefinition name "Lean.Json" (← jsonLiteral name json)
  let result := (quote.run {}).2
  (result.definitions, result.entries)

end CoreJS.LinkedBehavior
