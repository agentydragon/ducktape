import EsmSplit.CertificateData
import EsmSplit.CoreJS.LinkedCertificate.QuoteCoreJS
import EsmSplit.CoreJS.LiteralRecord

namespace CoreJS.LinkedBehavior

private abbrev StringFields := List (String × String)

private def wideRecordMinFields : Nat := 96

structure RecordQuotes where
  fields : Array StringFields := #[]
  replacements : Std.HashMap Lean.Expr Lean.Expr := {}

/-- Intern ordered field lists across the two programs. Replace complete top-level
units, whose source identities distinguish them from statements in function bodies. -/
def RecordQuotes.collect (source candidate : Program) : RecordQuotes := Id.run do
  let mut quotes : RecordQuotes := {}
  for program in [source, candidate] do
    for md in program.modules do
      for unit in md.body do
        let (ordinal, stmt) := unit
        let .decl kind name (.obj props) := stmt | continue
        if props.isEmpty then continue
        let some fields := props.mapM (fun
          | .init (.str key) (.str value) => some (key, value)
          | _ => none) | continue
        let index := (quotes.fields.findIdx? (· == fields)).getD quotes.fields.size
        if index == quotes.fields.size then quotes := { quotes with fields := quotes.fields.push fields }
        let props := EsmSplit.CertificateData.ref (.mkSimple s!"record{index}Props")
        let init := Lean.mkApp (Lean.mkConst ``Expr.obj) props
        let decl := Lean.mkApp3 (Lean.mkConst ``Stmt.decl) (Lean.toExpr kind) (Lean.toExpr name) init
        let replacement := Lean.mkApp4 (Lean.mkConst ``Prod.mk [0, 0])
          (Lean.toTypeExpr Nat) (Lean.toTypeExpr Stmt) (Lean.toExpr ordinal) decl
        quotes := { quotes with replacements := quotes.replacements.insert (Lean.toExpr unit) replacement }
  return quotes

private partial def literalReadKeys (keys : Std.HashSet String) (e : Lean.Expr) : Std.HashSet String :=
  match e with
  | .app .. => Id.run do
    let args := e.getAppArgs
    let mut keys := keys
    if e.isAppOfArity ``Expr.get 2 || e.isAppOfArity ``Expr.getOptional 2 || e.isAppOfArity ``Expr.callMethod 3 then
      let key := args[1]!
      if key.isAppOfArity ``Expr.str 1 then
        if let .lit (.strVal s) := key.appArg! then keys := keys.insert s
    return args.foldl literalReadKeys keys
  | _ => keys

/-- Quote the specialized raw tree with the same constructors the lookup lemmas reduce. -/
private def indexTreeExpr (tree : LiteralFieldIndex.Tree) : Lean.Expr :=
  let str := Lean.toTypeExpr String
  let ctor := fun name => Lean.mkApp2 (Lean.mkConst name [0, 0]) str (Lean.mkLambda `_ .default str str)
  let leaf := ctor ``Std.DTreeMap.Internal.Impl.leaf
  let inner := ctor ``Std.DTreeMap.Internal.Impl.inner
  let rec visit : LiteralFieldIndex.Tree → Lean.Expr
    | .leaf => leaf
    | .inner size key value left right =>
      Lean.mkApp5 inner (Lean.toExpr size) (Lean.toExpr key) (Lean.toExpr value) (visit left) (visit right)
  visit tree

/-- The syntax retains the checked original field list. Wide records expose
individual lookups, while small records expose their full property list so
later operations can inspect or replace its descriptors. -/
private def recordDefinitions (index : Nat) (fields : StringFields) (keys : Array String) (checkMove : Bool) :
    EsmSplit.CertificateData.Quotes ε String := do
  let stem := s!"record{index}"
  -- Many static reads amortize quoting a tree. Its projection must retain the
  -- exact source syntax, so keep the ordered pairs when a key occurs twice.
  let positions := Std.HashMap.ofList (fields.zipIdx.map fun ((k, _), i) => (k, i))
  let indexed := fields.length ≥ wideRecordMinFields && keys.size ≥ 8 && positions.size == fields.length
  let data ← if indexed then do
    let names ← EsmSplit.CertificateData.quote (stem ++ "Keys") (Lean.toExpr (fields.map Prod.fst))
    let tree := (Std.TreeMap.ofList fields : Std.TreeMap String String).inner.inner
    let treeTerm ← EsmSplit.CertificateData.quote (stem ++ "Tree") (indexTreeExpr tree)
    pure (s!"private def {stem}Keys : List String := {names}\n" ++
      s!"private def {stem}Tree : CoreJS.LiteralFieldIndex.Tree := {treeTerm}\n" ++
      s!"private def {stem}Data := CoreJS.LiteralFieldIndex.fields {stem}Tree {stem}Keys\n")
  else do
    let pairs ← EsmSplit.CertificateData.quote (stem ++ "Pairs") (Lean.toExpr fields)
    pure s!"private def {stem}Data : List (String × String) := {pairs}\n"
  let widthProof := if indexed then
    s!"exact Nat.le_trans (CoreJS.LiteralFieldIndex.length_le {stem}Tree {stem}Keys) (Nat.le_refl {fields.length})"
    else "exact Nat.le_refl _"
  let mut definitions := data ++
    s!"private opaque {stem}Witness : " ++ "{ v : List (String × String) // v = " ++
    s!"{stem}Data" ++ " } := " ++ s!"⟨{stem}Data, rfl⟩\n" ++
    s!"@[cbv_opaque] private def {stem}Fields := {stem}Witness.val\n" ++
    s!"private theorem {stem}Eq : {stem}Fields = {stem}Data := {stem}Witness.property\n" ++
    s!"private theorem {stem}Width : {stem}Fields.length ≤ {fields.length} := by rw [{stem}Eq]; {widthProof}\n" ++
    s!"@[cbv_opaque] private def {stem}Props := {stem}Fields.map CoreJS.literalField\n" ++
    s!"@[cbv_eval] private theorem {stem}Free : freeProps {stem}Props = [] := freeProps_literalFields {stem}Fields\n" ++
    s!"@[cbv_eval] private theorem {stem}Eval " ++ "{W : World} (fuel : Nat) (env : Env) :\n" ++
    s!"    (evalExpr fuel env (.obj {stem}Props) : M W Val) =\n" ++
    s!"      CoreJS.LinkedBehavior.evalQuotedRecordBody {stem}Fields {stem}Props {fields.length} fuel env :=\n" ++
    s!"  CoreJS.LinkedBehavior.evalQuotedRecord (W := W) (props := {stem}Props) (fields := {stem}Fields) " ++
    s!"rfl {stem}Width fuel env\n" ++
    s!"@[cbv_eval] private theorem {stem}Reverse : {stem}Fields.reverse = {stem}Data.reverse :=\n" ++
    s!"  congrArg List.reverse {stem}Eq\n"
  if checkMove then
    definitions := definitions ++
      s!"@[cbv_eval] private theorem {stem}Reads : CoreJS.readsNowProps {stem}Props = [] := LinkedBehavior.literalReads {stem}Fields\n" ++
      s!"@[cbv_eval] private theorem {stem}Allocs : CoreJS.allocOnlyProps {stem}Props = true := LinkedBehavior.literalAllocs {stem}Fields\n"
  if fields.length < wideRecordMinFields then
    let props ← EsmSplit.CertificateData.quote (stem ++ "OwnProperties") (Lean.toExpr (literalProperties fields))
    return definitions ++ s!"@[cbv_eval] private theorem {stem}OwnProperties :\n" ++
      s!"    CoreJS.literalProperties {stem}Fields = {props} := by\n" ++
      s!"  rw [{stem}Eq] <;> rfl\n"
  if indexed then
    definitions := definitions ++
      s!"@[cbv_eval] private theorem {stem}DynamicRead (key : String) :\n" ++
      s!"    CoreJS.lookupLiteralValue {stem}Fields key = (CoreJS.LiteralFieldIndex.read {stem}Tree key).bind " ++
      s!"(fun v => if {stem}Keys.contains key then some v else none) := by\n" ++
      s!"  rw [{stem}Eq]; exact CoreJS.LiteralFieldIndex.readValue _ _ _\n"
  let reversed := fields.reverse
  for (key, i) in keys.zipIdx do
    let result := match reversed.lookup key with
      | some value => s!"some (CoreJS.literalDataProperty {value.quote})"
      | none => "none"
    let proof := if !indexed then s!"rw [CoreJS.lookupLiteralProperty, {stem}Eq] <;> rfl" else
      match positions[key]? with
      | some pos => s!"rw [{stem}Eq]; exact CoreJS.LiteralFieldIndex.hit (tree := {stem}Tree) (keys := {stem}Keys) (i := {pos}) rfl rfl"
      | none => s!"rw [{stem}Eq]; exact CoreJS.LiteralFieldIndex.miss (tree := {stem}Tree) rfl"
    definitions := definitions ++
      s!"@[cbv_eval] private theorem {stem}Read{i} :\n" ++
      s!"    CoreJS.lookupLiteralProperty {stem}Fields {key.quote} = {result} := by\n" ++
      s!"  {proof}\n"
  return definitions

def RecordQuotes.hasWideRecord (records : RecordQuotes) : Bool :=
  records.fields.any (·.length ≥ wideRecordMinFields)

def RecordQuotes.definitions (records : RecordQuotes) (source candidate : Lean.Expr) (checkMove : Bool) :
    EsmSplit.CertificateData.Quotes ε String := do
  if records.fields.isEmpty then return ""
  let keys := if records.hasWideRecord then
    (literalReadKeys (literalReadKeys {} source) candidate).toArray.qsort (· < ·)
  else #[]
  let definitions ← records.fields.zipIdx.toList.mapM fun (fields, index) => recordDefinitions index fields keys checkMove
  return "attribute [cbv_eval] CoreJS.lookup_literalProperties\n" ++ String.join definitions

end CoreJS.LinkedBehavior
