import EsmSplit.CertificateData
import EsmSplit.CoreJS.LinkedCertificate.QuoteCoreJS

namespace CoreJS.LinkedBehavior

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

structure WriteBatches where
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

def WriteBatches.collect (source candidate : Lean.Expr) : WriteBatches :=
  collectWriteBatches (collectWriteBatches {} source) candidate

def WriteBatches.isEmpty (writes : WriteBatches) : Bool :=
  writes.batches.isEmpty

def WriteBatches.definitions (writes : WriteBatches) : EsmSplit.CertificateData.Quotes ε String := do
  let definitions ← writes.batches.zipIdx.toList.mapM fun (batch, index) => writeBatchDefinitions index batch
  return String.join definitions

end CoreJS.LinkedBehavior
