import Lean.Elab.Deriving.ToExpr
import Lean.Elab.Tactic.Cbv
import Lean.Util.CollectAxioms
import Lean.Util.ReplaceExpr
import EsmSplit.CertificateData
import EsmSplit.CoreJS.BatchState
import EsmSplit.CoreJS.ClassParents
import EsmSplit.CoreJS.DenseArray
import EsmSplit.CoreJS.LinkedInput
import EsmSplit.CoreJS.LinkedBehavior
import EsmSplit.CoreJS.LiteralRecord
import EsmSplit.CoreJS.SyntaxMatch

namespace CoreJS.LinkedBehavior

attribute [cbv_eval] DenseArray.reduce

/-- Quantify over worlds satisfying the host observations used by the certificate. -/
def EquivalentFor (premises : HostPremises) (source candidate : Program) : Prop :=
  if premises = {} then Equivalent source candidate
  else ∀ w, premises.Satisfied w →
      ∃ t : State w, Ends w source (.ok () t) ∧ Ends w candidate (.ok () t)

theorem of_host_evaluation {premises : HostPremises} {source candidate : Program} (fuel : Nat)
    (checks : ∀ w, normal (runProgram (premises.specialize w) fuel source) = true ∧
      runProgram (premises.specialize w) fuel source =
        runProgram (premises.specialize w) fuel candidate) :
    EquivalentFor premises source candidate := by
  by_cases empty : premises = {}
  · subst premises
    exact of_evaluation fuel checks
  simp [EquivalentFor, empty]
  intro w hw
  have e := premises.specialize_eq w hw
  obtain ⟨hc, he⟩ := checks w
  rw [e] at hc he
  cases hs : runProgram w fuel source with
  | ok _ t => exact ⟨t, ⟨fuel, hs, by simp⟩, ⟨fuel, he.symm.trans hs, by simp⟩⟩
  | _ => simp [hs, normal] at hc

/-- The two programs are exactly the lowering of these three AST inputs under this placement.
Their public bindings and their complete normally terminating executions agree in every world. -/
def ASTEquivalent (profile : HostProfile) (premises : HostPremises)
    (source providers files : Lean.Json) (plan : EsmSplit.Plan) : Prop :=
  ∃ original candidate, LinkedInput.decodePair source providers files plan profile = .ok (original, candidate) ∧
    EquivalentFor premises original candidate ∧ SameExports original candidate

/-- Decode the actual ASTs and transport the execution and export proofs to those programs. -/
def certifyASTs (profile : HostProfile) (premises : HostPremises)
    (source providers files : Lean.Json) (plan : EsmSplit.Plan) (original candidate : Program)
    (semantics : EquivalentFor premises original candidate ∧ SameExports original candidate) :
    Except String (PLift (ASTEquivalent profile premises source providers files plan)) := do
  match decoded : LinkedInput.decodePair source providers files plan profile with
  | .error why => throw why
  | .ok (a, b) =>
    let some ⟨before⟩ := SyntaxMatch.programEq a original | throw "source AST does not match the certified program"
    let some ⟨after⟩ := SyntaxMatch.programEq b candidate | throw "output ASTs do not match the certified program"
    return ⟨⟨a, b, decoded, before ▸ after ▸ semantics⟩⟩

/-- Successful execution of the production checker establishes a claim about the input
and output ASTs for every host world. Its callers can discard the proof at runtime. -/
theorem checkASTs_sound {profile premises source providers files plan original candidate}
    (semantics : EquivalentFor premises original candidate ∧ SameExports original candidate)
    (accepted : (certifyASTs profile premises source providers files plan original candidate semantics).isOk = true) :
    ASTEquivalent profile premises source providers files plan := by
  cases h : certifyASTs profile premises source providers files plan original candidate semantics with
  | error why => rw [h] at accepted; contradiction
  | ok proof => exact proof.down

-- Reify every constructor and field, including the deferred code in heap closures.
-- IEEE bit patterns distinguish both zeroes and preserve NaN payloads.
local instance : Lean.ToExpr Float where
  toExpr n := Lean.mkApp (Lean.mkConst ``Float.ofBits) (Lean.toExpr n.toBits)
  toTypeExpr := Lean.mkConst ``Float

deriving instance Lean.ToExpr for ArrayElementKind
deriving instance Lean.ToExpr for DeclKind
deriving instance Lean.ToExpr for UnOp
deriving instance Lean.ToExpr for BinOp
deriving instance Lean.ToExpr for LogOp
deriving instance Lean.ToExpr for Prim
deriving instance Lean.ToExpr for FuncKind
deriving instance Lean.ToExpr for OpaqueRun
deriving instance Lean.ToExpr for OpaqueHoistKind
deriving instance Lean.ToExpr for Expr, Target, PropDef, ClassMember, Func, Stmt
deriving instance Lean.ToExpr for Import
deriving instance Lean.ToExpr for Module
deriving instance Lean.ToExpr for Program

deriving instance Lean.ToExpr for CoreJS.Addr
deriving instance Lean.ToExpr for CoreJS.ErrorKind
deriving instance Lean.ToExpr for CoreJS.Val
deriving instance Lean.ToExpr for CoreJS.Property
deriving instance Lean.ToExpr for CoreJS.Env
deriving instance Lean.ToExpr for CoreJS.Code
deriving instance Lean.ToExpr for CoreJS.Obj
deriving instance Lean.ToExpr for CoreJS.Binding

/-- The constructor, application and literal nodes used to quote a probe observation. -/
private inductive ProbeTerm where
  | app (fn arg : ProbeTerm)
  | const (name : Lean.Name)
  | str (value : String)
  | nat (value : Nat)
  deriving DecidableEq

private def probeTerm? : Lean.Expr → Option ProbeTerm
  | .app f x => return .app (← probeTerm? f) (← probeTerm? x)
  | .const n _ => some (.const n)
  | .lit (.strVal s) => some (.str s)
  | .lit (.natVal n) => some (.nat n)
  | _ => none

/-! `State World.probe` has a closed shape. Keep a canonical projection of every
field in the run's final state; the probe's host state is `Unit`. Closures retain
their full code and environment through `Obj.code`. Floats use the `ToExpr`
instance above, which quotes their IEEE bit patterns. -/
private structure ProbeStateObservation where
  heap : List (List CoreJS.Obj)
  cells : List (List CoreJS.Binding)
  region : Nat
  out : List String
  worldState : Unit
  handed : List CoreJS.Addr
  deriving Lean.ToExpr

private def observeProbeState (profile : HostPremises) (state : CoreJS.State (profile.specialize CoreJS.World.probe)) : ProbeStateObservation :=
  { heap := state.heap.toList.map Array.toList
    cells := state.cells.toList.map Array.toList
    region := state.region
    out := state.out.toList
    worldState := state.world.state
    handed := state.world.handed.toList }

/-- Compare the complete state of two completed executions in the concrete probe.
This is a refusal check only; matching probes still go through the all-world proof. -/
def probeStatesEqual (profile : HostPremises) (left right : CoreJS.State (profile.specialize CoreJS.World.probe)) : Bool :=
  let leftTerm := Lean.toExpr (observeProbeState profile left)
  let rightTerm := Lean.toExpr (observeProbeState profile right)
  -- Equal quotations need no projection. On a difference compare only the constructor data,
  -- retaining the same treatment of universe levels and unsupported nodes.
  leftTerm.equal rightTerm || decide (probeTerm? leftTerm = probeTerm? rightTerm)

private abbrev StringFields := List (String × String)

private def wideRecordMinFields : Nat := 96

private structure RecordQuotes where
  fields : Array StringFields := #[]
  replacements : Std.HashMap Lean.Expr Lean.Expr := {}

/-- Intern ordered field lists across the two programs. Replace complete top-level
units, whose source identities distinguish them from statements in function bodies. -/
private def recordQuotes (source candidate : Program) : RecordQuotes := Id.run do
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

/-- Reduction used by the generated equation for a checked wide record. -/
def evalQuotedRecordBody {W : World} (fields : List (String × String)) (props : List PropDef)
    (width fuel : Nat) (env : Env) : M W Val :=
  match fuel with
  | 0 => outOfFuel
  | n + 1 =>
    if width < n then .obj <$> alloc (literalRecord fields) else
      (do
        let addr ← alloc { proto := some objectProto, props := [] }
        evalProps n env addr props
        pure (.obj addr))

/-- The generated equation specializes the usual record rule using the checked
property list and width. It preserves the ordinary evaluator at insufficient fuel. -/
theorem evalQuotedRecord {W : World} {fields : List (String × String)} {props : List PropDef} {width : Nat}
    (props_eq : props = fields.map literalField) (width_bound : fields.length ≤ width)
    (fuel : Nat) (env : Env) :
    (evalExpr fuel env (.obj props) : M W Val) = evalQuotedRecordBody fields props width fuel env := by
  cases fuel with
  | zero => rfl
  | succ n =>
    dsimp only [evalQuotedRecordBody]
    split
    · rename_i h
      have budget := Nat.lt_of_le_of_lt width_bound h
      rw [props_eq]
      exact evalExpr_obj_string_fields n env fields budget
    · rw [evalExpr] <;> rfl

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
private def recordDefinitions (index : Nat) (fields : StringFields) (keys : Array String) :
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

private inductive WriteWitness where
  | string | number | receiver
  | seq (left right : WriteWitness)
  | assign (key rhs : WriteWitness) (name : String) (numberKey : Option Float)

private structure WriteMatch where
  witness : WriteWitness
  minFuel : Nat
  fields : List (String × Val) := []
  /-- `none` denotes the receiver. Only literal values can be stored or used as keys. -/
  value : Option Val := none

private def quotedString : Lean.Expr → Option String
  | .lit (.strVal value) => some value
  | _ => none

private def quotedVariable (e : Lean.Expr) : Option Name :=
  if e.isAppOfArity ``Expr.var 1 then quotedString e.appArg! else none

private def quotedNumber (e : Lean.Expr) : Option Float := do
  unless e.isAppOfArity ``Float.ofBits 1 do none
  let bits := e.appArg!
  unless bits.isAppOfArity ``OfNat.ofNat 3 do none
  let .lit (.natVal n) := bits.getAppArgs[1]! | none
  return Float.ofBits (UInt64.ofNat n)

/-- Inspect the exact constructor term that will be quoted. The generated witness
checks the recognized operands, the fuel offsets, and every property key. -/
private partial def matchWrites (receiver : Name) (e : Lean.Expr) : Option WriteMatch := do
  if e.isAppOfArity ``Expr.str 1 then
    let value ← quotedString e.appArg!
    return { witness := .string, minFuel := 1, value := some (.str value) }
  if e.isAppOfArity ``Expr.num 1 then
    let value ← quotedNumber e.appArg!
    return { witness := .number, minFuel := 1, value := some (.num value) }
  if quotedVariable e == some receiver then
    return { witness := .receiver, minFuel := 1 }
  let args := e.getAppArgs
  if e.isAppOfArity ``Expr.seq 2 then
    let left ← matchWrites receiver args[0]!
    let right ← matchWrites receiver args[1]!
    return {
      witness := .seq left.witness right.witness
      minFuel := max left.minFuel right.minFuel + 1
      fields := left.fields ++ right.fields
      value := right.value }
  unless e.isAppOfArity ``Expr.assign 2 do none
  let target := args[0]!
  unless target.isAppOfArity ``Target.member 2 do none
  let targetArgs := target.getAppArgs
  unless quotedVariable targetArgs[0]! == some receiver do none
  let key ← matchWrites receiver targetArgs[1]!
  let rhs ← matchWrites receiver args[1]!
  let keyValue ← key.value
  let value ← rhs.value
  let .ok name := toStr keyValue | none
  let numberKey := match keyValue with
    | .num number => some number
    | _ => none
  return {
    witness := .assign key.witness rhs.witness name numberKey
    minFuel := max (key.minFuel + 2) (rhs.minFuel + 1)
    fields := key.fields ++ rhs.fields ++ [(name, value)]
    value := some value }

private partial def lastOperand (e : Lean.Expr) : Lean.Expr :=
  if e.isAppOfArity ``Expr.seq 2 then lastOperand e.appArg! else e

private structure WriteBatch where
  receiver : Name
  left : Lean.Expr
  right : Lean.Expr
  matched : WriteMatch

private def matchBatch (e : Lean.Expr) : Option WriteBatch := do
  unless e.isAppOfArity ``Expr.seq 2 do none
  let receiver ← quotedVariable (lastOperand e)
  let matched ← matchWrites receiver e
  -- At 64 writes, the ordinary growing-key proof already approaches 2 GiB.
  unless matched.fields.length ≥ 64 do none
  let mut seen : Std.HashSet String := {}
  for (key, _) in matched.fields do
    guard (!seen.contains key)
    seen := seen.insert key
  let args := e.getAppArgs
  return { receiver, left := args[0]!, right := args[1]!, matched }

private structure WriteBatches where
  batches : Array WriteBatch := #[]
  replacements : Std.HashMap Lean.Expr Lean.Expr := {}

/-- Keep only maximal recognized trees and reuse exact matches across both programs. -/
private partial def collectWriteBatches (collected : WriteBatches) (e : Lean.Expr) : WriteBatches :=
  if collected.replacements.contains e then collected
  else if let some batch := matchBatch e then
    let shared := EsmSplit.CertificateData.ref (.mkSimple s!"batch{collected.batches.size}Expr")
    { batches := collected.batches.push batch, replacements := collected.replacements.insert e shared }
  else if e.isApp then e.getAppArgs.foldl collectWriteBatches collected
  else collected

private abbrev WriteQuote (ε : Type) := StateT (Array String) (EsmSplit.CertificateData.Quotes ε)

/-- Each concrete number conversion gets its own checked equation. Definitional
equality gives a smaller proof when it can compute the conversion. -/
private def quoteWriteWitness (stem : String) (fuel : Nat) : WriteWitness → WriteQuote ε String
  | .string => pure s!"(WriteExpr.string (n := n + {fuel - 1}))"
  | .number => pure s!"(WriteExpr.number (n := n + {fuel - 1}))"
  | .receiver => pure s!"(WriteExpr.receiverRead (n := n + {fuel - 1}))"
  | .seq left right => do
    let l ← quoteWriteWitness stem (fuel - 1) left
    let r ← quoteWriteWitness stem (fuel - 1) right
    return s!"(WriteExpr.seq (n := n + {fuel - 1}) {l} {r})"
  | .assign key rhs name numberKey => do
    let k ← quoteWriteWitness stem (fuel - 2) key
    let r ← quoteWriteWitness stem (fuel - 1) rhs
    let conversion ← match numberKey with
      | none => pure "rfl"
      | some number => do
        let definitions ← get
        let fact := s!"{stem}Key{definitions.size}"
        let value ← liftM (EsmSplit.CertificateData.quote (ε := ε) fact (Lean.toExpr (.num number : Val)))
        set (definitions.push (s!"private theorem {fact} : toStr {value} = .ok {name.quote} := by\n" ++
          "  first | rfl | cbv <;> rfl\n"))
        pure fact
    return s!"(WriteExpr.assign (n := n + {fuel - 3}) (name := {name.quote}) {k} {r} {conversion})"

private def writeBatchDefinitions (index : Nat) (batch : WriteBatch) : EsmSplit.CertificateData.Quotes ε String := do
  let stem := s!"batch{index}"
  let left ← EsmSplit.CertificateData.quote (stem ++ "Left") batch.left
  let right ← EsmSplit.CertificateData.quote (stem ++ "Right") batch.right
  let fields ← EsmSplit.CertificateData.quote (stem ++ "Fields") (Lean.toExpr batch.matched.fields)
  let minFuel := batch.matched.minFuel
  let receiver := batch.receiver.quote
  let (witness, conversions) ← (quoteWriteWitness stem minFuel batch.matched.witness).run #[]
  return String.join conversions.toList ++
    s!"private def {stem}Left : Expr := {left}\n" ++
    s!"private def {stem}Right : Expr := {right}\n" ++
    s!"private def {stem}Expr : Expr := .seq {stem}Left {stem}Right\n" ++
    s!"attribute [local cbv_opaque] {stem}Expr\n" ++
    s!"private def {stem}Fields : List (String × Val) := {fields}\n" ++
    s!"private theorem {stem}Writes (a : Addr) (n : Nat) :\n" ++
    s!"    WriteExpr {receiver} a (n + {minFuel}) {stem}Expr {stem}Fields (.obj a) :=\n  {witness}\n" ++
    s!"private theorem {stem}Eval " ++ "{W : World} (fuel : Nat) (env : Env) :\n" ++
    s!"    (evalExpr fuel env {stem}Expr : M W Val) =\n" ++
    s!"      evalBatch {receiver} {stem}Left {stem}Right {stem}Fields {minFuel} fuel env :=\n" ++
    s!"  evalExpr_seq_batch {stem}Writes ((EsmSplit.nodupB_iff _).mp (by decide +kernel))\n" ++
    s!"attribute [local cbv_eval] {stem}Eval\n"

deriving instance Lean.ToExpr for EsmSplit.Module
deriving instance Lean.ToExpr for EsmSplit.Plan

section JsonQuoting

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

private def jsonFromTree (tree : JsonTree) : Lean.Json := .obj ⟨⟨tree⟩⟩

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
    let wrap := mkApp (mkConst ``jsonFromTree)
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

end JsonQuoting

/-- The equation compiler puts a negative premise on the general unary case because
`typeof` of a variable has its own lookup rule. The proof-producing `cbv` evaluator
only uses unconditional rewrites. Dispatch on the operator and operand on the RHS
so it can reduce any unary expression without unfolding the mutually recursive interpreter. -/
@[cbv_eval] private theorem evalExpr_unary (n : Nat) (env : Env) (op : UnOp) (a : Expr) :
    (evalExpr (n + 1) env (.unary op a) : M W Val) = (match op, a with
      | .typeof, .var x =>
        if (env.vars.lookup x).isNone then pure (.str "undefined") else do
          let v ← readVar env x
          lift (unop (← getState) .typeof v)
      | _, _ => do
        let v ← evalExpr n env a
        lift (unop (← getState) op v)) := by
  cases op <;> cases a <;> rfl

/-- Resolve an own data prototype on a concrete class constructor directly. Using the
checked lookup avoids expanding the nested interpreter calls in the non-null heritage path. -/
@[cbv_eval] private theorem classParents_obj (call : Call W) (a : Addr) (s : State W) :
    classParents call (.obj a) s =
      match s.objAt a with
      | some o =>
        match o.code with
        | some (.closure f ..) =>
          match f.kind, o.props.lookup "prototype" with
          | .classCtor, some (.data (.obj p) ..) => .ok (a, some p) s
          | _, _ => classParentsNonNull call (.obj a) s
        | _ => classParentsNonNull call (.obj a) s
      | none => classParentsNonNull call (.obj a) s := by
  cases ho : s.objAt a with
  | none => rfl
  | some o =>
    cases hc : o.code with
    | none => simp only [hc]; rfl
    | some c =>
      cases c <;> try (simp only [hc]; rfl)
      case closure f closed fields =>
        cases hk : f.kind <;> try (simp only [hc, hk]; rfl)
        case classCtor =>
          cases hp : o.props.lookup "prototype" with
          | none => simp only [hc, hk, hp]; rfl
          | some property =>
            cases property <;> try (simp only [hc, hk, hp]; rfl)
            case data value w e c =>
              cases value <;> try (simp only [hc, hk, hp]; rfl)
              case obj p =>
                simpa only [hc, hk, hp] using classParents_ownPrototype call ho hc hk hp

/-- An own data property does not need the prototype-walk fuel or accessor dispatch. -/
@[cbv_eval] private theorem getProp_obj_str (call : Call W) (a : Addr) (key : String) (s : State W) :
    getProp call (.obj a) (.str key) s =
      match (s.objAt a).bind (fun o => o.props.lookup key) with
      | some (.data v ..) => .ok v s
      | _ => getFrom call a (.obj a) key s := by
  cases ho : s.objAt a with
  | none => rfl
  | some o =>
    simp only [Option.bind_some]
    cases hp : o.props.lookup key with
    | none => rfl
    | some p =>
      cases p with
      | accessor => rfl
      | data => exact getProp_ownData call ho hp

/-- Prove both obligations by evaluating the real module graphs with an arbitrary host world.
`cbv` produces kernel-checkable equations for definitions with well-founded recursion. -/
macro "verify_linked_programs " fuel:num useCbv:(" cbv_first")? : tactic => do
  let reduce ← if useCbv.isSome then `(tactic| first | cbv <;> rfl | rfl)
    else `(tactic| first | rfl | cbv <;> rfl)
  -- One goal lets the symbolic reducer reuse the source run. The fallback
  -- gives the two reductions separate step budgets.
  let reduceExecution ← if useCbv.isSome then
    `(tactic| first | cbv <;> constructor <;> rfl | exact ⟨rfl, rfl⟩ | constructor <;> $reduce)
    else `(tactic| first | exact ⟨rfl, rfl⟩ | cbv <;> constructor <;> rfl | constructor <;> $reduce)
  `(tactic|
    (constructor
     · first | apply of_evaluation $fuel | apply of_host_evaluation $fuel
       intro w <;> $reduceExecution
     · apply sameExports_of_match
       intro w <;> $reduce))

/-- Reduce only the real link operation. Reflexivity fills the two output fields
with the kernel-checked module scopes and state. -/
macro "check_linked_layout" : tactic =>
  `(tactic|
    first
    | refine some ⟨?linked, ?ready, ?equation⟩
      case equation =>
        conv =>
          lhs
          cbv
        all_goals rfl
    | exact none)

/-- Reduce a run against the link result whose equation the kernel has checked. -/
macro "reduce_linked_run_from " fuel:num layout:ident world:ident : tactic =>
  `(tactic|
    (rw [run_after_link $fuel (($layout $world).get (by rfl))] <;>
      (conv =>
        lhs
        cbv) <;> rfl))

macro "verify_linked_exports_from " source:ident candidate:ident : tactic =>
  `(tactic|
    (apply sameExports_of_match
     intro w
     unfold exportsMatch exports
     rw [(($source w).get (by rfl)).equation, (($candidate w).get (by rfl)).equation]
     first | rfl | cbv <;> rfl))

/-- Compare the two post-hoist states and captured schedules without evaluating their statements. -/
macro "check_ordered_linked_runs " fuel:num source:ident candidate:ident world:term : tactic =>
  `(tactic|
    (apply run_eq_of_ordered_links $fuel (($source $world).get (by rfl)) (($candidate $world).get (by rfl))
     all_goals first | rfl | cbv <;> rfl))

macro "verify_ordered_linked_programs " fuel:num source:ident candidate:ident premises:ident : tactic =>
  `(tactic|
    (constructor
     · apply of_host_evaluation (premises := $premises) $fuel
       intro w
       constructor
       · rw [run_after_link $fuel (($source (HostPremises.specialize $premises w)).get (by rfl))]
         first | cbv <;> rfl | rfl
       · check_ordered_linked_runs $fuel $source $candidate (HostPremises.specialize $premises w)
     · verify_linked_exports_from $source $candidate))

/-- Reuse each checked link between execution and export matching. -/
macro "verify_linked_programs_from " fuel:num source:ident candidate:ident premises:ident : tactic => do
  let reduce ← `(tactic| first | cbv <;> constructor <;> rfl | exact ⟨rfl, rfl⟩)
  `(tactic|
    (constructor
     · first
       | apply of_evaluation $fuel
         intro w
         rw [run_after_link $fuel (($source w).get (by rfl)),
             run_after_link $fuel (($candidate w).get (by rfl))] <;> $reduce
       | apply of_host_evaluation (premises := $premises) $fuel
         intro w
         rw [run_after_link $fuel (($source (HostPremises.specialize $premises w)).get (by rfl)),
             run_after_link $fuel (($candidate (HostPremises.specialize $premises w)).get (by rfl))]
         -- Lean 4.35 can panic when cbv simplifies the conjunction in the specialized world.
         constructor <;> (first | cbv <;> rfl | rfl)
     · verify_linked_exports_from $source $candidate))

-- Check the actual theorem, whose reduction lemmas depend on the input program.
open Lean Elab Command in
elab "#audit_linked_programs " thm:ident : command => do
  let name ← liftCoreM <| realizeGlobalConstNoOverload thm
  let axioms ← liftCoreM <| Lean.collectAxioms name
  let allowed := #[``propext, ``Classical.choice, ``Quot.sound]
  for ax in axioms do
    unless allowed.contains ax do throwError "linked program certificate depends on {ax}"

/-- If both concrete runs finish and their complete state projections differ,
the runs' final results cannot be the shared result required by `Equivalent`.
The projection need not be injective: equality of states implies equality of
their quoted projections, which is enough to make a mismatch decisive. -/
theorem probeObservationMismatchRefutesEquivalent (profile : HostPremises) {source candidate : Program} {fuel : Nat}
    {sourceState candidateState : CoreJS.State (profile.specialize CoreJS.World.probe)}
    (sourceRun : CoreJS.runProgram (profile.specialize CoreJS.World.probe) fuel source = .ok () sourceState)
    (candidateRun : CoreJS.runProgram (profile.specialize CoreJS.World.probe) fuel candidate = .ok () candidateState)
    (different : probeStatesEqual profile sourceState candidateState = false) :
    ¬ Equivalent source candidate := by
  apply different_probe_states_refute_equivalence sourceRun candidateRun
  rintro rfl
  simp [probeStatesEqual] at different

#audit_linked_programs probeObservationMismatchRefutesEquivalent

inductive RefusalCode where
  | probeMismatch | probeThrow | probeUnknown | probeFuel | proofPreparation

def RefusalCode.label : RefusalCode → String
  | .probeMismatch => "probe_mismatch"
  | .probeThrow => "probe_throw"
  | .probeUnknown => "probe_unknown"
  | .probeFuel => "probe_fuel"
  | .proofPreparation => "proof_preparation"

structure Refusal where
  code : RefusalCode
  reason : String
  observation : Option Lean.Json := none

private structure ValuePair where
  path : List String
  original : Val
  candidate : Val

private def displayValue : Val → String
  | .undef => "undefined"
  | .null => "null"
  | .bool b => toString b
  | .num n => if n.toBits == 0x8000000000000000 then "-0" else toString n
  | .str s => (s.take 160).toString.quote
  | .obj _ => "object"
  | .host n => "host value " ++ n
  | .error k msg => s!"{repr k}: {(msg.take 160).toString.quote}"

/-- Inspect exported cells and their immediate own data properties without calling JS. -/
private def observeExports (profile : HostPremises) (source candidate : Program)
    (original split : State (profile.specialize World.probe)) : List ValuePair := Id.run do
  let world := profile.specialize World.probe
  let a := (exports world source).getD []
  let b := (exports world candidate).getD []
  let mut values := []
  for (name, addr) in a do
    let some left := (original.cellAt addr).bind (·.value) | continue
    let some right := ((b.lookup name).bind split.cellAt).bind (·.value) | continue
    values := ⟨[name], left, right⟩ :: values
    if let (.obj l, .obj r) := (left, right) then
      if let (some l, some r) := (original.objAt l, split.objAt r) then
        let properties := Std.HashMap.ofList r.props
        for (key, value) in l.props do
          if let (.data a .., some (.data b ..)) := (value, properties[key]?) then
            values := ⟨[name, key], a, b⟩ :: values
  return values.reverse

private def firstExportDifference (values : List ValuePair) : Option Lean.Json := Id.run do
  let mut seenOriginal : Std.HashMap (Nat × Nat) ((Nat × Nat) × List String) := {}
  let mut seenSplit : Std.HashMap (Nat × Nat) ((Nat × Nat) × List String) := {}
  for ⟨path, original, split⟩ in values do
    if let (.obj a, .obj b) := (original, split) then
      let l := (a.region, a.index)
      let r := (b.region, b.index)
      let detail (earlier : List String) (aliased : Bool) := Lean.Json.mkObj [
        ("kind", Lean.toJson "alias"), ("paths", Lean.toJson [earlier, path]),
        ("original_alias", Lean.toJson aliased), ("split_alias", Lean.toJson (!aliased))]
      if let some (previous, earlier) := seenOriginal[l]? then
        if previous != r then return some (detail earlier true)
      if let some (previous, earlier) := seenSplit[r]? then
        if previous != l then return some (detail earlier false)
      seenOriginal := seenOriginal.insert l (r, path)
      seenSplit := seenSplit.insert r (l, path)
    else if Lean.toExpr original != Lean.toExpr split then
      return some (Lean.Json.mkObj [
        ("kind", Lean.toJson "value"), ("path", Lean.toJson path),
        ("original", Lean.toJson (displayValue original)), ("split", Lean.toJson (displayValue split))])
  return none

/-- Only fuel exhaustion can be resolved by a larger witness. A normal probe
selects a witness for the kernel proof. If both runs finish but their complete
states differ, the theorem above proves that no all-world certificate can hold. -/
private def probeWitnessAt (profile : HostPremises) (source candidate : Program) (fuel : Nat) : Except Refusal (Option Nat) := do
  match sourceRun : runProgram (profile.specialize World.probe) fuel source with
  | .ok () sourceState =>
    match candidateRun : runProgram (profile.specialize World.probe) fuel candidate with
    | .ok () candidateState =>
      match statesAgree : probeStatesEqual profile sourceState candidateState with
      | true => return some fuel
      | false =>
        let _ : ¬ Equivalent source candidate :=
          probeObservationMismatchRefutesEquivalent profile sourceRun candidateRun statesAgree
        throw ⟨.probeMismatch, "probe executions completed normally with different final states",
          firstExportDifference (observeExports profile source candidate sourceState candidateState)⟩
    | .fuel => return none
    | .throw .. => throw ⟨.probeThrow, "split threw during linked initialization", none⟩
    | .unknown why _ => throw ⟨.probeUnknown, s!"split reached an unknown result during linked initialization: {why}", none⟩
  | .fuel => return none
  | .throw .. => throw ⟨.probeThrow, "original threw during linked initialization", none⟩
  | .unknown why _ => throw ⟨.probeUnknown, s!"original reached an unknown result during linked initialization: {why}", none⟩

/-- Evaluating a flat record uses one level of interpreter fuel per property.
Reserve the usual depth for its expressions and surrounding declaration. -/
private def recordFuelLimit (source : Program) : Nat := Id.run do
  let mut width := 0
  for m in source.modules do
    for (_, s) in m.body do
      if let .decl _ _ (.obj props) := s then
        width := max width props.length
  return min 2048 (64 + width)

private def witnessFuel (profile : HostPremises) (source candidate : Program) : Except Refusal Nat := do
  let test := probeWitnessAt profile source candidate
  for fuel in [16, 32, 64] do
    if let some witness ← test fuel then return witness
  let limit := recordFuelLimit source
  if limit > 64 then
    if let some witness ← test limit then return witness
  throw ⟨.probeFuel, s!"linked initialization exhausted the fuel search budget ({limit})", none⟩

structure CheckpointText where
  input : String
  source : String
  execution : String
  gate : String

structure Certificate where
  preamble : String
  execution : String
  correspondence : String
  stagedGate : Bool
  checkpoint : Option CheckpointText
  premises : HostPremises

private def layoutDeclaration (visibility program : String) : String :=
  s!"{visibility}noncomputable def {program}Link (w : World) : Option (LinkedBehavior.CheckedLink {program} w) := by\n" ++
  "  check_linked_layout\n"

/-- Three separately compiled modules check the shared result state before the
ordinary AST gate imports their execution theorem. -/
private def checkpointText (preamble options declarations correspondence : String) (fuel : Nat) (stageLink ordered : Bool) : CheckpointText :=
  let sourceLayout := if stageLink then layoutDeclaration "" "source" else ""
  let candidateLayout := if stageLink then layoutDeclaration "private " "candidate" else ""
  let reduceSource := if stageLink then s!"    reduce_linked_run_from {fuel} sourceLink w\n"
    else "    (conv =>\n      lhs\n      cbv) <;> rfl\n"
  let candidateProof := if ordered then
      s!"  have same : runProgram w {fuel} source = runProgram w {fuel} candidate := by\n" ++
      s!"    check_ordered_linked_runs {fuel} sourceLink candidateLink w\n" ++
      "  exact same.symm.trans (sourceWitness w).property\n"
    else if stageLink then s!"  reduce_linked_run_from {fuel} candidateLink w\n"
    else "  (conv =>\n    lhs\n    cbv) <;> rfl\n"
  let checkExports := if stageLink then "  · verify_linked_exports_from sourceLink candidateLink\n"
    else "  · apply LinkedBehavior.sameExports_of_match\n    intro w\n    first | rfl | cbv <;> rfl\n"
  let input := preamble ++ "namespace LinkedReplay\n" ++ declarations ++ "end LinkedReplay\n"
  let source := "import LinkedReplayInput\nimport Lean.Elab.Tactic.Conv\n" ++ options ++
    "namespace LinkedReplay\n" ++
    sourceLayout ++
    "noncomputable def sourceWitness (w : World) :\n" ++
    "    { t : State w // " ++ s!"runProgram w {fuel} source = .ok () t" ++ " } := by\n" ++
    "  refine ⟨?state, ?run⟩\n" ++
    "  case run =>\n" ++
    reduceSource ++
    "end LinkedReplay\n"
  let execution := "import LinkedReplaySource\n" ++ options ++
    "namespace LinkedReplay\n" ++
    candidateLayout ++
    "private theorem candidateRun (w : World) :\n" ++
    s!"    runProgram w {fuel} candidate = .ok () (sourceWitness w).val := by\n" ++
    candidateProof ++
    "theorem executions : LinkedBehavior.EquivalentFor {} source candidate ∧\n" ++
    "    LinkedBehavior.SameExports source candidate := by\n" ++
    "  constructor\n" ++
    s!"  · apply LinkedBehavior.of_runs {fuel}\n" ++
    "    intro w\n" ++
    "    exact ⟨_, (sourceWitness w).property, candidateRun w⟩\n" ++
    checkExports ++
    "#audit_linked_programs executions\n" ++
    "end LinkedReplay\n"
  let gate := "import LinkedReplayProof\n" ++ options ++ "open LinkedReplay\n" ++ correspondence
  { input, source, execution, gate }

-- Estimate the size of expanded constructor syntax independently of .olean sharing.
private partial def expandedBytes (expr : Lean.Expr) : Nat :=
  match expr with
  | .app .. => expr.getAppArgs.foldl (fun size arg => size + 1 + expandedBytes arg) (2 + expandedBytes expr.getAppFn)
  | .const name _ => name.toString.utf8ByteSize + 1
  | .lit (.strVal s) => s.quote.utf8ByteSize
  | .lit (.natVal n) => (toString n).utf8ByteSize
  | .fvar id => id.name.toString.utf8ByteSize
  | _ => 0

private def certificateText (profile : HostProfile) (sourceAST providers files : Lean.Json) (plan : EsmSplit.Plan)
    (source candidate : Program) : EsmSplit.CertificateData.Quotes Refusal Certificate := do
  let records := recordQuotes source candidate
  let sourceTerm := Lean.toExpr source
  let candidateTerm := Lean.toExpr candidate
  -- Collect host observations from both concrete executions. These runs only select
  -- hypotheses. The resulting theorem still checks the executions in the kernel.
  let premises := if profile == .empty then {} else Id.run do
    let world := HostPremises.observations profile
    let limit := max (recordFuelLimit source) (recordFuelLimit candidate)
    let observed := fun (program : Program) =>
      match runProgram world limit program with
      | .ok _ s | .throw _ s | .unknown _ s => s.world.state
      | .fuel => ({} : HostPremises)
    let left := observed source
    let right := observed candidate
    return { typeofBindings := (left.typeofBindings ++ right.typeofBindings).eraseDups.mergeSort (fun a b => a.1 ≤ b.1)
             refs := (left.refs ++ right.refs).eraseDups.reverse }
  let fuel ← liftM (witnessFuel premises source candidate)
  -- An initial reflexivity attempt can expand the parameter prelude before the
  -- reflection tactic uses the evaluator's equations.
  let hasDefaults := fun (e : Lean.Expr) => (e.find? fun t => t.isConstOf ``Func.withDefaults).isSome
  let strategy := if hasDefaults sourceTerm || hasDefaults candidateTerm then " cbv_first" else ""
  let writes := collectWriteBatches (collectWriteBatches {} sourceTerm) candidateTerm
  let replace := fun e =>
    if e.isAppOfArity ``Prod.mk 4 then records.replacements[e]?
    else if e.isAppOfArity ``Expr.seq 2 then writes.replacements[e]?
    else none
  let sourceData := sourceTerm.replace replace
  let candidateData := candidateTerm.replace replace
  let s ← EsmSplit.CertificateData.quote "source" sourceData
  let c ← EsmSplit.CertificateData.quote "candidate" candidateData
  let hasWideRecord := records.fields.any (·.length ≥ wideRecordMinFields)
  let recordTerms ← if records.fields.isEmpty then pure "" else do
    let keys := if hasWideRecord then
      (literalReadKeys (literalReadKeys {} sourceTerm) candidateTerm).toArray.qsort (· < ·)
    else #[]
    let definitions ← records.fields.zipIdx.toList.mapM fun (fields, index) => recordDefinitions index fields keys
    pure ("attribute [cbv_eval] CoreJS.lookup_literalProperties\n" ++ String.join definitions)
  let writeTerms ← writes.batches.zipIdx.toList.mapM fun (batch, index) => writeBatchDefinitions index batch
  let p ← EsmSplit.CertificateData.quote "plan" (Lean.toExpr plan)
  let (asts, astData) := jsonDefinitions [("sourceAST", sourceAST), ("providers", providers), ("files", files)]
  modify (· ++ astData)
  -- The generated declarations are short. Estimate the compiler's workload from
  -- the quoted terms when deciding whether to run correspondence separately.
  let stagedGate := astData.foldl (fun bytes (_, term) => bytes + expandedBytes term) 0 ≥ 512 * 1024
  let writeImports := if writeTerms.isEmpty then "" else "import EsmSplit.Fast\n"
  let profileTerm := fun (p : HostProfile) => match p with
    | .empty => "CoreJS.HostProfile.empty"
    | .browserPage => "CoreJS.HostProfile.browserPage"
  let ordinaryTactic := s!"verify_linked_programs {fuel}{strategy}"
  -- Larger module graphs can spend most of one reduction budget on linking.
  -- This only selects the proof attempt. The theorem checks the complete units,
  -- free-name cells, hoisted state and entry exports from the actual links.
  let order := fun (p : Program) => p.loaded.flatMap fun m => m.body.map Prod.fst
  let ordered := order source == order candidate
  let stageLink := ordered || (source.modules.map (·.body.length)).sum ≥ 32
  let (layouts, tactic) := if stageLink then
    (layoutDeclaration "private " "source" ++ layoutDeclaration "private " "candidate",
     "  first\n" ++
     (if ordered then s!"  | verify_ordered_linked_programs {fuel} sourceLink candidateLink premises\n" else "") ++
     s!"  | verify_linked_programs_from {fuel} sourceLink candidateLink premises\n  | {ordinaryTactic}\n")
    else ("", s!"  {ordinaryTactic}\n")
  let options := "\nopen CoreJS\n" ++
    "set_option maxRecDepth 16000\nset_option maxHeartbeats 2000000\nset_option cbv.warning false\n" ++
    "\n"
  let preamble := "import EsmSplit.CoreJS.LinkedCertificate\n" ++
    s!"import {EsmSplit.CertificateData.moduleName}\n" ++ writeImports ++ options
  let data := recordTerms ++ String.join writeTerms
  let raw := String.join asts.toList ++ "private def plan : EsmSplit.Plan := " ++ p ++ "\n"
  let programs := s!"def source : Program := {s}\ndef candidate : Program := {c}\n"
  let profiles := fun (visibility : String) =>
    s!"{visibility}def profile := {profileTerm profile}\n" ++
    s!"{visibility}def premises : HostPremises := ⟨{reprStr premises.typeofBindings}, {reprStr premises.refs}⟩\n"
  let execution := "namespace LinkedExecutionProof\n" ++ data ++ programs ++ profiles "" ++ layouts ++
    "theorem executions : LinkedBehavior.EquivalentFor premises source candidate ∧ LinkedBehavior.SameExports source candidate := by\n" ++
    tactic ++ "end LinkedExecutionProof\n"
  let gate :=
    "private def verified : Except String (PLift (LinkedBehavior.ASTEquivalent profile premises sourceAST providers files plan)) :=\n" ++
    "  LinkedBehavior.certifyASTs profile premises sourceAST providers files plan source candidate executions\n" ++
    "#audit_linked_programs verified\n" ++
    "#eval IO.ofExcept (verified.map fun _ => ())\n"

  let correspondence := "open LinkedExecutionProof\n" ++ raw ++ gate
  -- The measured 384-field and 705-field cases benefit from separate processes.
  -- Smaller proofs avoid their startup cost. Write batches use file-local CBV rules.
  let checkpoint := if premises = {} ∧ writes.batches.isEmpty ∧ expandedBytes sourceData + expandedBytes candidateData ≥ 40 * 1024 then
      -- Wide records also benefit from staging the link below the statement cutoff.
      some (checkpointText preamble options (data ++ programs) (raw ++ profiles "private " ++ gate) fuel
        (stageLink || hasWideRecord) ordered)
    else none
  return { preamble, execution, correspondence, stagedGate, checkpoint, premises }

/-- Both proof layouts check the same all-world execution claim and exact ASTs. -/
def certificate (profile : HostProfile) (sourceAST providers files : Lean.Json) (plan : EsmSplit.Plan)
    (source candidate : Program) : Except Refusal (Certificate × EsmSplit.CertificateData.Entries) :=
  (certificateText profile sourceAST providers files plan source candidate).run #[]

end CoreJS.LinkedBehavior
