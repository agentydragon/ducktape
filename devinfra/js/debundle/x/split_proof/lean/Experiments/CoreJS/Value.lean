import Experiments.CoreJS.Number
import Experiments.CoreJS.Syntax

/-!
# CoreJS: values, the state, and the operations that run no program code

**Addresses.** An object or a binding lives at `(region, index)`. An allocation appends to the region the state names,
and a top-level statement runs with a region of its own. Which address an allocation gets is not observable in
JavaScript, so any policy that hands out unused ones is faithful. This one makes what a statement allocates independent
of the statements that ran before it, so two statements that only allocate end in equal states in either order, with
no renaming of addresses.

**Unknown code.** `Val.host` is a value of the host that the model says nothing about, and `Val.error` an error object
the engine made. Calling a host value, reading its properties or converting it ends the run with `Res.unknown`, and so
does converting an object to a primitive (`valueOf` and `toString` are not run in this model).
-/

namespace CoreJS

structure Addr where
  region : Nat
  index : Nat
  deriving DecidableEq, Repr

inductive ErrorKind where
  | type | reference
  deriving DecidableEq, Repr

inductive Val where
  | undef
  | null
  | bool (b : Bool)
  | num (n : Float)
  | str (s : String)
  | obj (a : Addr)
  | error (kind : ErrorKind) (msg : String)
  | host (name : String)

inductive Property where
  | data (value : Val) (writable enumerable configurable : Bool)
  | accessor (get set : Val) (enumerable configurable : Bool)

def Property.enumerable : Property → Bool
  | .data _ _ e _ | .accessor _ _ e _ => e

def Property.configurable : Property → Bool
  | .data _ _ _ c | .accessor _ _ _ c => c

/-- Names to bindings, innermost first, and the value of `this`. -/
structure Env where
  vars : List (Name × Addr)
  this : Val

/-- What a callable object runs. `fields` are the instance fields of a class constructor. -/
inductive Code where
  | closure (f : Func) (env : Env) (fields : List (String × Expr))
  | host (name : String)

structure Obj where
  proto : Option Addr
  props : List (String × Property)
  code : Option Code := none
  /-- An array: defining a property on it changes `length`, which is not modelled. -/
  array : Bool := false

/-- A binding: `value` is `none` until it is initialized (the temporal dead zone). -/
structure Binding where
  value : Option Val
  mutable : Bool

structure State where
  heap : Array (Array Obj)
  cells : Array (Array Binding)
  /-- Where allocations go. -/
  region : Nat
  /-- One entry for each `console.log`. -/
  out : Array String

/-- How a computation ends. `unknown`: it reached code the model does not define. `fuel`: the bound ran out. -/
inductive Res (α : Type) where
  | ok (a : α) (s : State)
  | throw (v : Val) (s : State)
  | unknown (why : String) (s : State)
  | fuel

def M (α : Type) : Type := State → Res α

instance : Monad M where
  pure a := fun s => .ok a s
  bind m f := fun s =>
    match m s with
    | .ok a s' => f a s'
    | .throw v s' => .throw v s'
    | .unknown why s' => .unknown why s'
    | .fuel => .fuel

def getState : M State := fun s => .ok s s
def modifyState (f : State → State) : M Unit := fun s => .ok () (f s)
def throwVal {α : Type} (v : Val) : M α := fun s => .throw v s
def throwErr {α : Type} (kind : ErrorKind) (msg : String) : M α := throwVal (.error kind msg)
def unknown {α : Type} (why : String) : M α := fun s => .unknown why s
def outOfFuel {α : Type} : M α := fun _ => .fuel

/-- Why an operation on values does not give a value. -/
inductive Abort where
  | error (kind : ErrorKind) (msg : String)
  | unknown (why : String)

/-- An operation on values that reads no state. -/
def lift {α : Type} : Except Abort α → M α
  | .ok a => pure a
  | .error (.error kind msg) => throwErr kind msg
  | .error (.unknown why) => unknown why

/-! ## The heap and the bindings -/

def State.objAt (s : State) (a : Addr) : Option Obj := (s.heap[a.region]?).bind (·[a.index]?)
def State.cellAt (s : State) (a : Addr) : Option Binding := (s.cells[a.region]?).bind (·[a.index]?)

/-- An address that is in use names an object; `model:` marks a state the rules cannot reach. -/
def getObj (a : Addr) : M Obj := fun s =>
  match s.objAt a with
  | some o => .ok o s
  | none => .unknown "model: no object at the address" s

def alloc (o : Obj) : M Addr := fun s =>
  match s.heap[s.region]? with
  | some objs => .ok ⟨s.region, objs.size⟩ { s with heap := s.heap.set! s.region (objs.push o) }
  | none => .unknown "model: no such region" s

def modifyObj (a : Addr) (f : Obj → Obj) : M Unit := fun s =>
  .ok () { s with heap := s.heap.modify a.region (·.modify a.index f) }

def newCell (b : Binding) : M Addr := fun s =>
  match s.cells[s.region]? with
  | some bs => .ok ⟨s.region, bs.size⟩ { s with cells := s.cells.set! s.region (bs.push b) }
  | none => .unknown "model: no such region" s

def setCell (a : Addr) (v : Val) : M Unit := fun s =>
  .ok () { s with cells := s.cells.modify a.region (·.modify a.index fun b => { b with value := some v }) }

/-- A new innermost frame. -/
def declare (env : Env) : List (Name × Binding) → M Env
  | [] => pure env
  | (x, b) :: rest => do
    let a ← newCell b
    let env ← declare env rest
    pure { env with vars := (x, a) :: env.vars }

def readVar (env : Env) (x : Name) : M Val := do
  match env.vars.lookup x with
  | none => throwErr .reference s!"{x} is not defined"
  | some a =>
    match (← getState).cellAt a with
    | some { value := some v, .. } => pure v
    | _ => throwErr .reference s!"cannot access '{x}' before initialization"

/-- An assignment to a name. In strict code one that does not resolve throws. -/
def writeVar (env : Env) (x : Name) (v : Val) : M Unit := do
  match env.vars.lookup x with
  | none => throwErr .reference s!"{x} is not defined"
  | some a =>
    match (← getState).cellAt a with
    | some { value := some _, mutable := true } => setCell a v
    | some { value := some _, mutable := false } => throwErr .type "assignment to constant variable"
    | _ => throwErr .reference s!"cannot access '{x}' before initialization"

/-- The declaration of a name gives it its first value. -/
def initVar (env : Env) (x : Name) (v : Val) : M Unit :=
  match env.vars.lookup x with
  | some a => setCell a v
  | none => unknown "model: a declaration outside its scope"

/-! ## The objects of the host that every program reaches -/

def objectProto : Addr := ⟨0, 0⟩
def functionProto : Addr := ⟨0, 1⟩
def arrayProto : Addr := ⟨0, 2⟩

/-- The string-keyed members of `Object.prototype`. A program reads one from any object that has no property of that name. -/
def objectProtoNames : List String :=
  ["constructor", "__defineGetter__", "__defineSetter__", "hasOwnProperty", "__lookupGetter__", "__lookupSetter__",
    "isPrototypeOf", "propertyIsEnumerable", "toString", "valueOf", "toLocaleString"]

def functionProtoNames : List String := ["apply", "bind", "call", "constructor", "toString"]

def arrayProtoNames : List String :=
  ["constructor", "at", "concat", "copyWithin", "fill", "find", "findIndex", "findLast", "findLastIndex", "lastIndexOf",
    "pop", "push", "reverse", "shift", "unshift", "slice", "sort", "splice", "includes", "indexOf", "join", "keys",
    "entries", "values", "forEach", "filter", "flat", "flatMap", "map", "every", "some", "reduce", "reduceRight",
    "toReversed", "toSorted", "toSpliced", "with", "toLocaleString", "toString"]

private def hostMembers (owner : String) (names : List String) : List (String × Property) :=
  names.map fun k => (k, .data (.host s!"{owner}.{k}") true false true)

private def hostAccessor (owner k : String) : String × Property :=
  (k, .accessor (.host s!"get {owner}.{k}") (.host s!"set {owner}.{k}") false true)

/-- The three prototypes, with every member a value of the host. `Function.prototype` is itself callable. -/
def builtins : Array Obj := #[
  { proto := none, props := hostMembers "Object.prototype" objectProtoNames ++ [hostAccessor "Object.prototype" "__proto__"] },
  { proto := some objectProto, code := some (.host "Function.prototype"),
    props := [("length", .data (.num 0) false false true), ("name", .data (.str "") false false true),
      hostAccessor "Function.prototype" "arguments", hostAccessor "Function.prototype" "caller"] ++
      hostMembers "Function.prototype" functionProtoNames },
  { proto := some objectProto, array := true,
    props := ("length", .data (.num 0) true false false) :: hostMembers "Array.prototype" arrayProtoNames }]

/-! ## Conversions and operators -/

def needsPrimitive {α : Type} : Except Abort α := .error (.unknown "an object is converted to a primitive")
def inspectsHost {α : Type} (name : String) : Except Abort α := .error (.unknown s!"host value {name} is inspected")

def toBoolean : Val → Except Abort Bool
  | .undef | .null => pure false
  | .bool b => pure b
  | .num n => pure (!(n == 0.0 || n.isNaN))
  | .str s => pure (!s.isEmpty)
  | .obj _ | .error .. => pure true
  | .host name => inspectsHost name

def toNumber : Val → Except Abort Float
  | .undef => pure Num.nan
  | .null => pure 0.0
  | .bool b => pure (if b then 1.0 else 0.0)
  | .num n => pure n
  | .str s => pure (Num.ofString s)
  | .obj _ | .error .. => needsPrimitive
  | .host name => inspectsHost name

def toStr : Val → Except Abort String
  | .undef => pure "undefined"
  | .null => pure "null"
  | .bool b => pure (if b then "true" else "false")
  | .num n => pure (Num.toString n)
  | .str s => pure s
  | .obj _ | .error .. => needsPrimitive
  | .host name => inspectsHost name

def isCallable (s : State) : Val → Bool
  | .obj a => ((s.objAt a).bind (·.code)).isSome
  | _ => false

def typeOf (s : State) : Val → Except Abort String
  | .undef => pure "undefined"
  | .null => pure "object"
  | .bool _ => pure "boolean"
  | .num _ => pure "number"
  | .str _ => pure "string"
  | .obj a => pure (if isCallable s (.obj a) then "function" else "object")
  | .error .. => pure "object"
  | .host name => inspectsHost name

/-- `===`. Whether two error objects of the engine are one object is not recorded. -/
def strictEq : Val → Val → Except Abort Bool
  | .host name, _ | _, .host name => inspectsHost name
  | .error .., .error .. => .error (.unknown "two engine errors are compared")
  | .undef, .undef | .null, .null => pure true
  | .bool a, .bool b => pure (a == b)
  | .num a, .num b => pure (a == b)
  | .str a, .str b => pure (a == b)
  | .obj a, .obj b => pure (a == b)
  | _, _ => pure false

/-- `SameValue`: as `===`, but a NaN is itself and the two zeros differ. -/
def sameValue : Val → Val → Except Abort Bool
  | .num a, .num b => pure (if a.isNaN then b.isNaN else a == b && Num.isNegZero a == Num.isNegZero b)
  | a, b => strictEq a b

def isNullish : Val → Bool
  | .undef | .null => true
  | _ => false

/-- `==`. -/
def looseEq (a b : Val) : Except Abort Bool :=
  match a, b with
  | .host name, _ | _, .host name => inspectsHost name
  | .obj x, .obj y => pure (x == y)
  | .error .., .error .. => .error (.unknown "two engine errors are compared")
  | .str x, .str y => pure (x == y)
  | _, _ =>
    if isNullish a || isNullish b then pure (isNullish a && isNullish b) else
    match a, b with
    | .obj _, .error .. | .error .., .obj _ => pure false
    | .obj _, _ | _, .obj _ | .error .., _ | _, .error .. => needsPrimitive
    | _, _ => do pure ((← toNumber a) == (← toNumber b))

/-- `a < b` and its three relatives: strings compare by code unit, anything else as numbers. -/
def relational (num : Float → Float → Bool) (str : String → String → Bool) : Val → Val → Except Abort Bool
  | .str a, .str b => pure (str a b)
  | a, b => do pure (num (← toNumber a) (← toNumber b))

/-- An int32 as its 32 bits, and back. -/
def bits32 (i : Int) : Nat := (Int.emod i (2 ^ 32)).toNat
def int32Of (u : Nat) : Int := if u % 2 ^ 32 ≥ 2 ^ 31 then ((u % 2 ^ 32 : Nat) : Int) - 2 ^ 32 else ((u % 2 ^ 32 : Nat) : Int)

def ofInt32 (i : Int) : Val := .num (Float.ofInt i)

def binop (op : BinOp) (a b : Val) : Except Abort Val :=
  let arith (f : Float → Float → Float) : Except Abort Val := do
    let x ← toNumber a
    pure (.num (f x (← toNumber b)))
  let bits (f : Nat → Nat → Nat) : Except Abort Val := do
    let x ← toNumber a
    pure (ofInt32 (int32Of (f (bits32 (Num.toInt32 x)) (bits32 (Num.toInt32 (← toNumber b))))))
  match op with
  | .add =>
    match a, b with
    | .obj _, _ | _, .obj _ | .error .., _ | _, .error .. => needsPrimitive
    | .str x, _ => do pure (.str (x ++ (← toStr b)))
    | _, .str y => do pure (.str ((← toStr a) ++ y))
    | _, _ => arith (· + ·)
  | .sub => arith (· - ·)
  | .mul => arith (· * ·)
  | .div => arith (· / ·)
  | .mod => arith Num.fmod
  | .exp => arith Num.pow
  | .bitAnd => bits (· &&& ·)
  | .bitOr => bits (· ||| ·)
  | .bitXor => bits (· ^^^ ·)
  | .shl => do
    let x ← toNumber a
    pure (ofInt32 (int32Of (bits32 (Num.toInt32 x) <<< (Num.toUint32 (← toNumber b) % 32))))
  | .shr => do
    let x ← toNumber a
    pure (ofInt32 (Int.fdiv (Num.toInt32 x) (2 ^ (Num.toUint32 (← toNumber b) % 32))))
  | .ushr => do
    let x ← toNumber a
    pure (.num (Float.ofNat (Num.toUint32 x >>> (Num.toUint32 (← toNumber b) % 32))))
  | .eq => .bool <$> looseEq a b
  | .ne => (fun r => .bool !r) <$> looseEq a b
  | .strictEq => .bool <$> strictEq a b
  | .strictNe => (fun r => .bool !r) <$> strictEq a b
  | .lt => .bool <$> relational (fun x y => decide (x < y)) (fun x y => decide (x < y)) a b
  | .le => .bool <$> relational (fun x y => decide (x ≤ y)) (fun x y => !decide (y < x)) a b
  | .gt => .bool <$> relational (fun x y => decide (x > y)) (fun x y => decide (y < x)) a b
  | .ge => .bool <$> relational (fun x y => decide (x ≥ y)) (fun x y => !decide (x < y)) a b

def unop (s : State) (op : UnOp) (v : Val) : Except Abort Val :=
  match op with
  | .neg => (fun x => .num (-x)) <$> toNumber v
  | .pos => .num <$> toNumber v
  | .not => (fun b => .bool !b) <$> toBoolean v
  | .bitNot => (fun x => ofInt32 (-(Num.toInt32 x) - 1)) <$> toNumber v
  | .typeof => .str <$> typeOf s v
  | .void => pure .undef
  | .toString => .str <$> toStr v

/-- Does `&&`, `||` or `??` stop at its left operand? -/
def shortCircuits (op : LogOp) (v : Val) : Except Abort Bool :=
  match op, v with
  | .nullish, .undef | .nullish, .null => pure false
  | .nullish, .host name => inspectsHost name
  | .nullish, _ => pure true
  | .and, _ => (!·) <$> toBoolean v
  | .or, _ => toBoolean v

/-! ## Properties -/

def setOwn (props : List (String × Property)) (k : String) (p : Property) : List (String × Property) :=
  if props.any (·.1 == k) then props.map fun (k', p') => if k' == k then (k', p) else (k', p') else props ++ [(k, p)]

/-- The first property named `k` on `a` and its prototypes. `steps` bounds the walk: a chain has no cycle, so the count of objects is enough. -/
def findProp (s : State) : Nat → Addr → String → Option Property
  | 0, _, _ => none
  | steps + 1, a, k =>
    match s.objAt a with
    | none => none
    | some o =>
      match o.props.lookup k with
      | some p => some p
      | none => o.proto.bind fun parent => findProp s steps parent k

def State.objectCount (s : State) : Nat := s.heap.foldl (· + ·.size) 0

def lookupProp (a : Addr) (k : String) : M (Option Property) := do
  let s ← getState
  pure (findProp s s.objectCount a k)

/-- A property key: a string, or a number as `ToString` writes it. -/
def toKey (k : Val) : M String := lift (toStr k)

/-- The fields a property descriptor may have. -/
structure Desc where
  value : Option Val := none
  writable : Option Bool := none
  get : Option Val := none
  set : Option Val := none
  enumerable : Option Bool := none
  configurable : Option Bool := none

/-- `ValidateAndApplyPropertyDescriptor` on an extensible object: the property after the definition, or `none` if it is refused. -/
def applyDesc (current : Option Property) (d : Desc) : Except Abort (Option Property) := do
  let isAccessor := d.get.isSome || d.set.isSome
  let isData := d.value.isSome || d.writable.isSome
  let differs (wanted : Option Val) (have_ : Val) : Except Abort Bool :=
    match wanted with
    | some v => (!·) <$> sameValue v have_
    | none => pure false
  match current with
  | none =>
    pure <| some <|
      if isAccessor then .accessor (d.get.getD .undef) (d.set.getD .undef) (d.enumerable.getD false) (d.configurable.getD false)
      else .data (d.value.getD .undef) (d.writable.getD false) (d.enumerable.getD false) (d.configurable.getD false)
  | some cur =>
    let e := d.enumerable.getD cur.enumerable
    let c := d.configurable.getD cur.configurable
    if !cur.configurable && (d.configurable == some true || e != cur.enumerable) then pure none else
    match cur with
    | .data v w _ _ =>
      if isAccessor then
        pure (if cur.configurable then some (.accessor (d.get.getD .undef) (d.set.getD .undef) e c) else none)
      else if !cur.configurable && !w && (d.writable == some true || (← differs d.value v)) then pure none
      else pure (some (.data (d.value.getD v) (d.writable.getD w) e c))
    | .accessor g st _ _ =>
      if isData then
        pure (if cur.configurable then some (.data (d.value.getD .undef) (d.writable.getD false) e c) else none)
      else if !cur.configurable && ((← differs d.get g) || (← differs d.set st)) then pure none
      else pure (some (.accessor (d.get.getD g) (d.set.getD st) e c))

/-- `DefinePropertyOrThrow` on the object at `a`. -/
def defineOwn (a : Addr) (k : String) (d : Desc) : M Unit := do
  let o ← getObj a
  if o.array then unknown "a property is defined on an array" else
  match ← lift (applyDesc (o.props.lookup k) d) with
  | some p => modifyObj a fun o => { o with props := setOwn o.props k p }
  | none => throwErr .type s!"cannot redefine property: {k}"

/-- `CreateDataPropertyOrThrow`. -/
def createData (a : Addr) (k : String) (v : Val) : M Unit :=
  defineOwn a k { value := some v, writable := some true, enumerable := some true, configurable := some true }

/-- What a call does: the callee, `this`, the arguments. The operations below take it as a parameter. -/
abbrev Call := Val → Val → List Val → M Val

/-- `base[key]`. A getter runs with `base` as `this`. -/
def getProp (call : Call) (base key : Val) : M Val := do
  match base with
  | .undef | .null => throwErr .type "cannot read properties of undefined or null"
  | .obj a =>
    match ← lookupProp a (← toKey key) with
    | none => pure .undef
    | some (.data v ..) => pure v
    | some (.accessor .undef ..) => pure .undef
    | some (.accessor getter ..) => call getter base []
  | .str s =>
    let k ← toKey key
    let chars := s.toList
    if k == "length" then pure (.num (Float.ofNat chars.length)) else
    match k.toNat? with
    | some i =>
      match chars[i]? with
      | some c => if toString i == k then pure (.str (String.singleton c)) else unknown "a property of String.prototype"
      | none => unknown "a property of String.prototype"
    | none => unknown "a property of String.prototype"
  | .num _ | .bool _ => unknown "a property of a number or a boolean"
  | .error .. => unknown "a property of an engine error"
  | .host name => unknown s!"a property of host value {name}"

/-- `base[key] = v` in strict code. A setter runs with `base` as `this`. -/
def setProp (call : Call) (base key v : Val) : M Unit := do
  match base with
  | .undef | .null => throwErr .type "cannot set properties of undefined or null"
  | .obj a =>
    let k ← toKey key
    match ← lookupProp a k with
    | some (.accessor _ .undef ..) => throwErr .type s!"cannot set property {k}, which has only a getter"
    | some (.accessor _ setter ..) => discard (call setter base [v])
    | some (.data _ false ..) => throwErr .type s!"cannot assign to read only property {k}"
    | _ =>
      -- an own writable data property keeps its attributes; an inherited or a missing one makes a new own property
      defineOwn a k (if ((← getObj a).props.lookup k).isSome then { value := some v } else
        { value := some v, writable := some true, enumerable := some true, configurable := some true })
  | .str _ | .num _ | .bool _ => unknown "a property is set on a primitive"
  | .error .. => unknown "a property of an engine error"
  | .host name => unknown s!"a property of host value {name}"

/-- `delete base[key]` in strict code. -/
def deleteProp (base key : Val) : M Val := do
  match base with
  | .undef | .null => throwErr .type "cannot convert undefined or null to object"
  | .obj a =>
    let k ← toKey key
    match (← getObj a).props.lookup k with
    | none => pure (.bool true)
    | some p =>
      if p.configurable then
        modifyObj a fun o => { o with props := o.props.filter (·.1 != k) }
        pure (.bool true)
      else throwErr .type s!"cannot delete property {k}"
  | .str _ | .num _ | .bool _ => unknown "a property of a primitive is deleted"
  | .error .. => unknown "a property of an engine error"
  | .host name => unknown s!"a property of host value {name}"

/-- `key in base`. -/
def hasProp (key base : Val) : M Val := do
  match base with
  | .obj a => pure (.bool (← lookupProp a (← toKey key)).isSome)
  | .error .. => unknown "a property of an engine error"
  | .host name => unknown s!"a property of host value {name}"
  | _ => throwErr .type "cannot use 'in' to search in a primitive"

/-- The object a value is, for a standard function that takes one. -/
def requireObj (what : String) : Val → M Addr
  | .obj a => pure a
  | .error .. => unknown "an engine error is inspected"
  | .host name => unknown s!"host value {name} is inspected"
  | _ => throwErr .type s!"{what} is not an object"

/-- `ToPropertyDescriptor`: the fields are read in the specified order, and reading one may run a getter. -/
def toDesc (call : Call) (v : Val) : M Desc := do
  let a ← requireObj "property description" v
  let field (k : String) : M (Option Val) := do
    if (← lookupProp a k).isSome then some <$> getProp call v (.str k) else pure none
  let flag (k : String) : M (Option Bool) := do
    match ← field k with
    | some x => some <$> lift (toBoolean x)
    | none => pure none
  let callable (k : String) : M (Option Val) := do
    match ← field k with
    | some (.host name) => unknown s!"host value {name} is inspected"
    | some .undef => pure (some .undef)
    | some f => if isCallable (← getState) f then pure (some f) else throwErr .type s!"{k}ter must be a function"
    | none => pure none
  let enumerable ← flag "enumerable"
  let configurable ← flag "configurable"
  let value ← field "value"
  let writable ← flag "writable"
  let get ← callable "get"
  let set ← callable "set"
  if (get.isSome || set.isSome) && (value.isSome || writable.isSome) then
    throwErr .type "a property descriptor has both accessors and a value"
  else pure { value, writable, get, set, enumerable, configurable }

/-- How `console.log` writes a primitive. An object is written by the host. -/
def logArg : Val → Except Abort String
  | .num n => pure (if Num.isNegZero n then "-0" else Num.toString n)
  | .obj _ | .error .. => .error (.unknown "console.log writes an object")
  | v => toStr v

/-- Does a prototype chain come to `a`? -/
def reaches (s : State) (a : Addr) : Nat → Option Addr → Bool
  | _, none | 0, _ => false
  | steps + 1, some b => b == a || reaches s a steps ((s.objAt b).bind (·.proto))

def prim (call : Call) (p : Prim) (args : List Val) : M Val := do
  let arg (i : Nat) : Val := args.getD i .undef
  match p with
  | .log =>
    match args with
    | .str format :: _ :: _ => if format.contains '%' then unknown "console.log with a format string" else pure ()
    | _ => pure ()
    let line ← lift (args.mapM logArg)
    modifyState fun s => { s with out := s.out.push (" ".intercalate line) }
    pure .undef
  | .create =>
    match arg 0 with
    | .null => .obj <$> alloc { proto := none, props := [] }
    | v => do
      let parent ← requireObj "object prototype" v
      if (arg 1) matches .undef then .obj <$> alloc { proto := some parent, props := [] }
      else unknown "Object.create with properties"
  | .getPrototypeOf =>
    match arg 0 with
    | .undef | .null => throwErr .type "cannot convert undefined or null to object"
    | .obj a => do pure (match (← getObj a).proto with | some p => .obj p | none => .null)
    | _ => unknown "the prototype of a primitive or a host value"
  | .setPrototypeOf => do
    match arg 0 with
    | .undef | .null => throwErr .type "Object.setPrototypeOf called on null or undefined"
    | _ => pure ()
    let parent ← match arg 1 with
      | .null => pure none
      | v => some <$> requireObj "object prototype" v
    match arg 0 with
    | .obj a =>
      let s ← getState
      if (← getObj a).proto == parent then pure (arg 0)
      else if reaches s a s.objectCount parent then throwErr .type "cyclic __proto__ value"
      else
        modifyObj a fun o => { o with proto := parent }
        pure (arg 0)
    | .host name => unknown s!"host value {name} is inspected"
    | .error .. => unknown "an engine error is inspected"
    | v => pure v
  | .defineProperty => do
    let a ← requireObj "Object.defineProperty target" (arg 0)
    let k ← toKey (arg 1)
    defineOwn a k (← toDesc call (arg 2))
    pure (arg 0)

end CoreJS
