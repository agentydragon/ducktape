import EsmSplit.CoreJS.Value

/-! Synchronous iteration. A user iterator caches its next method on acquisition.
Native array cursors have no observable object when their protocol has
the modeled intrinsic methods. -/

namespace CoreJS

variable {W : World}

inductive IteratorCursor where
  | array (source : Addr) (index : Nat)
  | object (receiver : Addr) (next : Val)

def requireIteratorObject (message : String) : Val → M W Addr
  | .obj a => pure a
  | .host _ | .error .. => unknown "iterator protocol needs a modeled object"
  | _ => throwErr .type message

def checkArrayIteratorNext : M W Unit := do
  let proto ← getObj arrayIteratorProto
  match proto.props.lookup (.str "next") with
  | some (.data (.host "ArrayIterator.prototype.next") ..) =>
    pure ()
  | _ =>
    unknown "array iterator next differs from the modeled intrinsic"

def getIterator (call : Call W) (value : Val) : M W IteratorCursor := do
  let method ← getProp call value (.wellKnownSymbol .iterator)
  if let .host "Array.prototype.values" := method then
    let a ← requireIteratorObject "array iterator receiver is not an object" value
    -- Array.prototype.values is generic. Strict arguments objects inherit from
    -- Object.prototype and have their own data property for this iterator.
    checkArrayIteratorNext
    return .array a 0
  let receiver ←
    requireIteratorObject "iterator method result is not an object" (← call method value [])
  let next ← getProp call (.obj receiver) (.str "next")
  return .object receiver next

/-- An elision advances the cursor and reads `done`. It skips the result's `value` getter. -/
def iteratorStep (call : Call W) (cursor : IteratorCursor) (readValue : Bool := true) :
    M W (Option (Val × IteratorCursor)) := do
  match cursor with
  | .array source index =>
    let .num length ←
      getProp call (.obj source)
          (.str "length") | unknown "array iterator length is not a modeled number"
    -- For a nonnegative integer index this compares against ToLength(length),
    -- including fractional, nonfinite, and mutable array-like lengths.
    if !(index < 9007199254740991 && Float.ofNat (index + 1) ≤ length) then
      return none
    -- The intrinsic next method reads the index even if its result's value is discarded.
    let v ← getProp call (.obj source) (.str (toString index))
    return some (if readValue then v else .undef, .array source (index + 1))
  | .object receiver next =>
    let result ←
      requireIteratorObject "iterator result is not an object" (← call next (.obj receiver) [])
    if ← lift (toBoolean (← getProp call (.obj result) (.str "done"))) then
      return none
    let v ←
      if readValue then
        getProp call (.obj result) (.str "value")
      else
        pure .undef
    return some (v, cursor)

def callIteratorReturn (call : Call W) (cursor : IteratorCursor) : M W (Option Val) := do
  match cursor with
  | .object receiver _ =>
    let method ← getProp call (.obj receiver) (.str "return")
    if isNullish method then
      return none
    some <$> call method (.obj receiver) []
  | .array .. =>
    match ← lookupProp arrayIteratorProto (.str "return") with
    | none | some (.data .undef ..) | some (.data .null ..) =>
      pure none
    | _ =>
      unknown "native iterator has a custom return method"

/-- `return` runs even when the current completion throws. That original throw
takes precedence over a throw or non-object result from closing the iterator. -/
def iteratorClose {α : Type} (call : Call W) (cursor : IteratorCursor) (completion : Except Val α) :
    M W α := do
  let result ← attempt (callIteratorReturn call cursor)
  match completion with
  | .error v =>
    throwVal v
  | .ok completion =>
    match result with
    | .error v =>
      throwVal v
    | .ok none | .ok (some (.obj _)) | .ok (some (.error ..)) =>
      pure completion
    | .ok (some (.host _)) =>
      unknown "iterator return result needs a modeled object"
    | .ok (some _) =>
      throwErr .type "iterator return result is not an object"

/-- Drain the cursor for consumers that need a complete argument or element list. -/
def collectIterator (call : Call W) : Nat → IteratorCursor → M W (List Val)
  | 0, _ => outOfFuel
  | n + 1, cursor => do
    match ← iteratorStep call cursor with
    | none =>
      pure []
    | some (value, next) =>
      (value :: ·) <$> collectIterator call n next

def iteratorValues (fuel : Nat) (call : Call W) (source : Val) : M W (List Val) := do
  collectIterator call fuel (← getIterator call source)

end CoreJS
