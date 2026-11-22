import EsmSplit.CoreJS.Number
import EsmSplit.CoreJS.Syntax

/-!
# CoreJS: values, the state, and the operations that run no program code

**Addresses.** An object or a binding lives at `(region, index)`. An allocation appends to the region the state names,
and a top-level statement has a region of its own: region `i + 1` for the statement with identity `i`. Which address
an allocation gets is not observable in JavaScript, so any policy that hands out unused ones is faithful. This one
makes what a statement allocates independent of the statements that ran before it, so two statements that only
allocate end in equal states in either order, with no renaming of addresses.

**Unknown code.** `Val.host` is a value of the host that the model says nothing about, and `Val.error` an error object
the engine or the host made. Converting a host value or comparing it ends the run with `Res.unknown`, and so does
converting an object to a primitive (`valueOf` and `toString` are not run in this model).

**The world.** A host function that works on primitives is in the model without a definition: the model asks the
`World` it runs in (`hostCall`). Which functions those are is a table in this file (`worldFunction`): for each one the
claim is that, called with a primitive receiver and primitive arguments, it returns a primitive or throws, and that
it reads and changes nothing the program can reach. A call of any other host function, or one that would hand the
host an object, ends the run with `Res.unknown`.
-/

namespace CoreJS

structure Addr where
  region : Nat
  index : Nat
  deriving DecidableEq, Repr

inductive ErrorKind where
  | type | reference | syntax | range | uri
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

/-- The bindings of `names` in `env`, and its `this`: what a closure keeps of the scope it was made in. A function
keeps the bindings its code can mean (`Func.closes`) and no others, so two scopes that give those names the same
bindings give the same closure. -/
def Env.capture (env : Env) (names : List Name) : Env where
  vars := names.eraseDups.filterMap fun x => (env.vars.lookup x).map (x, ·)
  this := env.this

/-- What a callable object runs. `fields` are the instance fields of a class constructor. -/
inductive Code where
  /-- An implicit derived constructor forwards to its current internal prototype. -/
  | derived
  | closure (f : Func) (env : Env) (fields : List (String × Expr))
  | host (name : String)
  /-- Part `part` of the opaque code `code`, which the world runs; `cells` are the bindings it was handed. -/
  | opaque (code part : Nat) (cells : List (Option Addr))

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

/-- A primitive value: what the program and the host hand each other. -/
inductive Primitive where
  | undef
  | null
  | bool (b : Bool)
  | num (n : Float)
  | str (s : String)

def Primitive.val : Primitive → Val
  | .undef => .undef
  | .null => .null
  | .bool b => .bool b
  | .num n => .num n
  | .str s => .str s

def Val.primitive? : Val → Option Primitive
  | .undef => some .undef
  | .null => some .null
  | .bool b => some (.bool b)
  | .num n => some (.num n)
  | .str s => some (.str s)
  | .obj _ | .error .. | .host _ => none

/-- What the world holds of a value: an object only as its position in the list of the objects it was handed
(`Host.handed`), so the world can name no other object. -/
inductive WVal where
  | prim (p : Primitive)
  | handle (k : Nat)
  | error (kind : ErrorKind) (msg : String)
  | host (name : String)

/-- What the model tells the world: run part `part` of the opaque code `code` (part 0 is a statement, another part a
function the world made or a statement declared), as a call of `callee` with `this` and `args`; or how the last
request ended. -/
inductive Told where
  | run (code part : Nat) (callee this : WVal) (args : List WVal) (construct : Bool)
  | value (v : WVal)
  | threw (v : WVal)
  | keys (ks : List String)

/-- What the world asks of the model while it runs opaque code. `readVar`, `writeVar` and `initVar` name a binding by
its position in the names of the code (`opaqueNames`); every other request names objects by handle. -/
inductive Request where
  /-- The code ends with this value, or throws it. -/
  | done (v : WVal)
  | throw (v : WVal)
  /-- The world does not define what the code does. -/
  | unknown (why : String)
  | readVar (i : Nat)
  | writeVar (i : Nat) (v : WVal)
  | initVar (i : Nat) (v : WVal)
  | get (o k : WVal)
  | set (o k v : WVal)
  | delete (o k : WVal)
  | has (k o : WVal)
  /-- The names of the own properties of an object, in order. -/
  | keys (o : WVal)
  | call (f this : WVal) (args : List WVal)
  | construct (f : WVal) (args : List WVal)
  /-- A new object whose prototype is `proto`, or `Object.prototype`. -/
  | newObject (proto : Option WVal)
  /-- A function whose body is part `part` of the code that runs; it keeps the bindings that code was handed. -/
  | newFunction (part : Nat) (name : String) (length : Nat) (constructor : Bool)
  | prim (p : Prim) (args : List WVal)

/-- How a call of a host function ends: with a primitive, with a thrown primitive, or with a thrown error object. -/
inductive HostResult where
  | ret (v : Primitive)
  | throw (v : Primitive)
  | error (kind : ErrorKind) (msg : String)

/-- **The world**: the host functions that work on primitives, without their definitions. It has a state of its own
and one deterministic step: the host function `fn`, called with the receiver `this` and the arguments `args` in a
state, ends as `step` says and leaves the next state. The model says which functions it asks the world about
(`worldFunction`); a call that would hand the host an object ends the run as `unknown`. Everything proved about the
model holds for every world. -/
structure World where
  σ : Type
  init : σ
  step : (fn : String) → (this : Primitive) → (args : List Primitive) → σ → HostResult × σ
  /-- **Stage two**: opaque code, a conversation. Told what to run, or how its last request ended, the world makes its
  next request. It sees the program's objects only through what it was handed, and its own state. -/
  talk : Told → σ → Request × σ

/-- The world's side of the state: its own state, and the objects it was handed, in the order it got them. -/
structure Host (W : World) where
  state : W.σ
  handed : Array Addr

structure State (W : World) where
  heap : Array (Array Obj)
  cells : Array (Array Binding)
  /-- Where allocations go. -/
  region : Nat
  /-- One entry for each `console.log`. -/
  out : Array String
  /-- The state of the world. -/
  world : Host W

/-- How a computation ends. `unknown`: it reached code the model does not define. `fuel`: the bound ran out. -/
inductive Res (W : World) (α : Type) where
  | ok (a : α) (s : State W)
  | throw (v : Val) (s : State W)
  | unknown (why : String) (s : State W)
  | fuel

def M (W : World) (α : Type) : Type := State W → Res W α

variable {W : World}

instance : Monad (M W) where
  pure a := fun s => .ok a s
  bind m f := fun s =>
    match m s with
    | .ok a s' => f a s'
    | .throw v s' => .throw v s'
    | .unknown why s' => .unknown why s'
    | .fuel => .fuel

def getState : M W (State W) := fun s => .ok s s
def modifyState (f : State W → State W) : M W Unit := fun s => .ok () (f s)
def throwVal {α : Type} (v : Val) : M W α := fun s => .throw v s
def throwErr {α : Type} (kind : ErrorKind) (msg : String) : M W α := throwVal (.error kind msg)
def unknown {α : Type} (why : String) : M W α := fun s => .unknown why s
def outOfFuel {α : Type} : M W α := fun _ => .fuel

/-- Why an operation on values does not give a value. -/
inductive Abort where
  | error (kind : ErrorKind) (msg : String)
  | unknown (why : String)

/-- An operation on values that reads no state. -/
def lift {α : Type} : Except Abort α → M W α
  | .ok a => pure a
  | .error (.error kind msg) => throwErr kind msg
  | .error (.unknown why) => unknown why

/-! ## The heap and the bindings -/

def State.objAt (s : State W) (a : Addr) : Option Obj := (s.heap[a.region]?).bind (·[a.index]?)
def State.cellAt (s : State W) (a : Addr) : Option Binding := (s.cells[a.region]?).bind (·[a.index]?)

/-- An address that is in use names an object; `model:` marks a state the rules cannot reach. -/
def getObj (a : Addr) : M W Obj := fun s =>
  match s.objAt a with
  | some o => .ok o s
  | none => .unknown "model: no object at the address" s

def alloc (o : Obj) : M W Addr := fun s =>
  match s.heap[s.region]? with
  | some objs => .ok ⟨s.region, objs.size⟩ { s with heap := s.heap.set! s.region (objs.push o) }
  | none => .unknown "model: no such region" s

def modifyObj (a : Addr) (f : Obj → Obj) : M W Unit := fun s =>
  .ok () { s with heap := s.heap.modify a.region (·.modify a.index f) }

def newCell (b : Binding) : M W Addr := fun s =>
  match s.cells[s.region]? with
  | some bs => .ok ⟨s.region, bs.size⟩ { s with cells := s.cells.set! s.region (bs.push b) }
  | none => .unknown "model: no such region" s

def setCell (a : Addr) (v : Val) : M W Unit := fun s =>
  .ok () { s with cells := s.cells.modify a.region (·.modify a.index fun b => { b with value := some v }) }

/-- New bindings, in order: each name with where its binding is. -/
def newCells : List (Name × Binding) → M W (List (Name × Addr))
  | [] => pure []
  | (x, b) :: rest => do
    let a ← newCell b
    let frame ← newCells rest
    pure ((x, a) :: frame)

/-- A new innermost frame. -/
def declare (env : Env) (bs : List (Name × Binding)) : M W Env := do
  let frame ← newCells bs
  pure { env with vars := frame ++ env.vars }

def readVar (env : Env) (x : Name) : M W Val := do
  match env.vars.lookup x with
  | none => throwErr .reference s!"{x} is not defined"
  | some a =>
    match (← getState).cellAt a with
    | some { value := some v, .. } => pure v
    | _ => throwErr .reference s!"cannot access '{x}' before initialization"

/-- An assignment to a name. In strict code one that does not resolve throws. -/
def writeVar (env : Env) (x : Name) (v : Val) : M W Unit := do
  match env.vars.lookup x with
  | none => throwErr .reference s!"{x} is not defined"
  | some a =>
    match (← getState).cellAt a with
    | some { value := some _, mutable := true } => setCell a v
    | some { value := some _, mutable := false } => throwErr .type "assignment to constant variable"
    | _ => throwErr .reference s!"cannot access '{x}' before initialization"

/-- The declaration of a name gives it its first value. -/
def initVar (env : Env) (x : Name) (v : Val) : M W Unit :=
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

/-! ## The host functions the world answers for -/

/-- The string-keyed members of `String.prototype`. A property of a string that is none of them is looked up on
`Object.prototype`, which the program can change. -/
def stringProtoNames : List String :=
  ["length", "constructor", "anchor", "at", "big", "blink", "bold", "charAt", "charCodeAt", "codePointAt", "concat",
    "endsWith", "fontcolor", "fontsize", "fixed", "includes", "indexOf", "isWellFormed", "italics", "lastIndexOf",
    "link", "localeCompare", "match", "matchAll", "normalize", "padEnd", "padStart", "repeat", "replace", "replaceAll",
    "search", "slice", "small", "split", "strike", "sub", "substr", "substring", "sup", "startsWith", "toString",
    "toWellFormed", "trim", "trimStart", "trimLeft", "trimEnd", "trimRight", "toLocaleLowerCase", "toLocaleUpperCase",
    "toLowerCase", "toUpperCase", "valueOf"]

def numberProtoNames : List String :=
  ["constructor", "toExponential", "toFixed", "toPrecision", "toString", "valueOf", "toLocaleString"]

def booleanProtoNames : List String := ["constructor", "toString", "valueOf"]

/-- The methods the world answers for, by prototype. Left out: `match`, `matchAll` and `split`, which return objects;
the methods that depend on the locale of the host beyond `localeCompare`; the ones that write HTML. -/
def worldMethodNames : List (String × List String) :=
  [("String.prototype",
      ["at", "charAt", "charCodeAt", "codePointAt", "concat", "endsWith", "includes", "indexOf", "isWellFormed",
        "lastIndexOf", "localeCompare", "normalize", "padEnd", "padStart", "repeat", "replace", "replaceAll", "search",
        "slice", "startsWith", "substr", "substring", "toLowerCase", "toString", "toUpperCase", "toWellFormed", "trim",
        "trimEnd", "trimLeft", "trimRight", "trimStart", "valueOf"]),
    ("Number.prototype", ["toExponential", "toFixed", "toPrecision", "toString", "valueOf"]),
    ("Boolean.prototype", ["toString", "valueOf"])]

/-- The globals of the host that hold functions the world answers for, with those members. A read of such a member
gives the function: a program that had replaced it would have ended as `unknown` at the write. -/
def hostNamespaces : List (String × List String) :=
  [("Math",
      ["abs", "acos", "acosh", "asin", "asinh", "atan", "atanh", "atan2", "ceil", "cbrt", "expm1", "clz32", "cos",
        "cosh", "exp", "floor", "fround", "hypot", "imul", "log", "log1p", "log2", "log10", "max", "min", "pow",
        "random", "round", "sign", "sin", "sinh", "sqrt", "tan", "tanh", "trunc"]),
    ("Number", ["isFinite", "isInteger", "isNaN", "isSafeInteger", "parseFloat", "parseInt"]),
    ("String", ["fromCharCode", "fromCodePoint"])]

/-- The globals of the host that the world answers for when they are called. -/
def hostFunctions : List String := ["Boolean", "Number", "isFinite", "isNaN", "parseFloat", "parseInt"]

def qualified (table : List (String × List String)) : List String :=
  table.flatMap fun (owner, members) => members.map fun k => s!"{owner}.{k}"

/-- Does the world answer for the host function `name`? `some true`: a method, which also takes its receiver. -/
def worldFunction (name : String) : Option Bool :=
  if (qualified worldMethodNames).contains name then some true
  else if hostFunctions.contains name || (qualified hostNamespaces).contains name then some false
  else none

/-- The receiver and the arguments the world gets, if it answers for `name` and all of them are primitives. A function
that is not a method gets no receiver. -/
def worldArgs (name : String) (this : Val) (args : List Val) : Option (Primitive × List Primitive) := do
  let method ← worldFunction name
  let this ← if method then this.primitive? else some .undef
  pure (this, ← args.mapM Val.primitive?)

/-- A string of the model has no character outside the Basic Multilingual Plane: the model counts code points, and
JavaScript code units. -/
def Primitive.inModel : Primitive → Bool
  | .str s => s.toList.all fun c => c.toNat < 0x10000
  | _ => true

/-- What the world gave back, as the model's result. -/
def fromWorld (s : State W) : HostResult → Res W Val
  | .ret v => if v.inModel then .ok v.val s else .unknown "the host returns a string outside the Basic Multilingual Plane" s
  | .throw v => if v.inModel then .throw v.val s else .unknown "the host throws a string outside the Basic Multilingual Plane" s
  | .error kind msg => .throw (.error kind msg) s

/-- A call of the host function `name`: the world's step, if the world answers for it. -/
def hostCall (name : String) (this : Val) (args : List Val) : M W Val :=
  match worldArgs name this args with
  | none => unknown s!"host function {name} is called"
  | some (this, args) => fun s =>
    let (result, state) := W.step name this args s.world.state
    fromWorld { s with world := { s.world with state } } result

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

def isCallable (s : State W) : Val → Bool
  | .obj a => ((s.objAt a).bind (·.code)).isSome
  | _ => false

def typeOf (s : State W) : Val → Except Abort String
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

/-- String operands of loose inequality compare their contents without coercion. -/
theorem binop_ne_strings (a b : String) :
    binop .ne (.str a) (.str b) = .ok (.bool (!(a == b))) := rfl

def unop (s : State W) (op : UnOp) (v : Val) : Except Abort Val :=
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
def findProp (s : State W) : Nat → Addr → String → Option Property
  | 0, _, _ => none
  | steps + 1, a, k =>
    match s.objAt a with
    | none => none
    | some o =>
      match o.props.lookup k with
      | some p => some p
      | none => o.proto.bind fun parent => findProp s steps parent k

def State.objectCount (s : State W) : Nat := s.heap.foldl (· + ·.size) 0

def lookupProp (a : Addr) (k : String) : M W (Option Property) := do
  let s ← getState
  pure (findProp s s.objectCount a k)

/-- A property key: a string, or a number as `ToString` writes it. -/
def toKey (k : Val) : M W String := lift (toStr k)

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
def defineOwn (a : Addr) (k : String) (d : Desc) : M W Unit := do
  let o ← getObj a
  if o.array then unknown "a property is defined on an array" else
  match ← lift (applyDesc (o.props.lookup k) d) with
  | some p => modifyObj a fun o => { o with props := setOwn o.props k p }
  | none => throwErr .type s!"cannot redefine property: {k}"

/-- `CreateDataPropertyOrThrow`. -/
def createData (a : Addr) (k : String) (v : Val) : M W Unit :=
  defineOwn a k { value := some v, writable := some true, enumerable := some true, configurable := some true }

/-- What a call does: the callee, `this`, the arguments. The operations below take it as a parameter. -/
abbrev Call (W : World) := Val → Val → List Val → M W Val

/-- What a string has itself: `length` and its characters. -/
def stringOwn (s : String) (k : String) : Option Val :=
  if k == "length" then some (.num (Float.ofNat s.length)) else
    (k.toNat?.filter (toString · == k)).bind fun i => (s.toList[i]?).map fun c => .str (String.singleton c)

/-- The property `k` of the object at `a`, read for `base`: a getter runs with `base` as `this`. -/
def getFrom (call : Call W) (a : Addr) (base : Val) (k : String) : M W Val := do
  match ← lookupProp a k with
  | none => pure .undef
  | some (.data v ..) => pure v
  | some (.accessor .undef ..) => pure .undef
  | some (.accessor getter ..) => call getter base []

/-- A property of a primitive that it does not have itself: a member of its prototype `owner`, which is a value of
the host, or else what `Object.prototype` has. -/
def protoProp (call : Call W) (owner : String) (names : List String) (base : Val) (k : String) : M W Val :=
  if names.contains k then pure (.host s!"{owner}.{k}") else getFrom call objectProto base k

/-- A member of a global of the host: a function the world answers for. -/
def hostMember (name k : String) : M W Val :=
  if ((hostNamespaces.lookup name).getD []).contains k then pure (.host s!"{name}.{k}")
  else unknown s!"a property of host value {name}"

/-- `base[key]`. A getter runs with `base` as `this`. -/
def getProp (call : Call W) (base key : Val) : M W Val := do
  match base with
  | .undef | .null => throwErr .type "cannot read properties of undefined or null"
  | .obj a => getFrom call a base (← toKey key)
  | .str s =>
    let k ← toKey key
    match stringOwn s k with
    | some v => pure v
    | none => protoProp call "String.prototype" stringProtoNames base k
  | .num _ => protoProp call "Number.prototype" numberProtoNames base (← toKey key)
  | .bool _ => protoProp call "Boolean.prototype" booleanProtoNames base (← toKey key)
  | .error .. => unknown "a property of an engine error"
  | .host name => hostMember name (← toKey key)

/-- `base[key] = v` in strict code. A setter runs with `base` as `this`. -/
def setProp (call : Call W) (base key v : Val) : M W Unit := do
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
def deleteProp (base key : Val) : M W Val := do
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
def hasProp (key base : Val) : M W Val := do
  match base with
  | .obj a => pure (.bool (← lookupProp a (← toKey key)).isSome)
  | .error .. => unknown "a property of an engine error"
  | .host name => unknown s!"a property of host value {name}"
  | _ => throwErr .type "cannot use 'in' to search in a primitive"

/-- The object a value is, for a standard function that takes one. -/
def requireObj (what : String) : Val → M W Addr
  | .obj a => pure a
  | .error .. => unknown "an engine error is inspected"
  | .host name => unknown s!"host value {name} is inspected"
  | _ => throwErr .type s!"{what} is not an object"

/-- `ToPropertyDescriptor`: the fields are read in the specified order, and reading one may run a getter. -/
def toDesc (call : Call W) (v : Val) : M W Desc := do
  let a ← requireObj "property description" v
  let field (k : String) : M W (Option Val) := do
    if (← lookupProp a k).isSome then some <$> getProp call v (.str k) else pure none
  let flag (k : String) : M W (Option Bool) := do
    match ← field k with
    | some x => some <$> lift (toBoolean x)
    | none => pure none
  let callable (k : String) : M W (Option Val) := do
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
def reaches (s : State W) (a : Addr) : Nat → Option Addr → Bool
  | _, none | 0, _ => false
  | steps + 1, some b => b == a || reaches s a steps ((s.objAt b).bind (·.proto))

def prim (call : Call W) (p : Prim) (args : List Val) : M W Val := do
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

/-! ## The world, stage two: opaque code

The world runs opaque code as a conversation (`converse` in `Eval.lean`): it makes a request, the model carries it out
and tells it how it ended. The model keeps the list of objects the world was handed (`Host.handed`); the world names
an object only by its position there, so it can reach only what it was handed and what it reads from that. A request
that throws is told to the world as a throw, which it may catch.
-/

/-- The world's name for a value. An object the world has not been handed before goes on the end of its list. -/
def toW (v : Val) : M W WVal :=
  match v with
  | .obj a => fun s =>
    match s.world.handed.findIdx? (· == a) with
    | some k => .ok (.handle k) s
    | none => .ok (.handle s.world.handed.size) { s with world := { s.world with handed := s.world.handed.push a } }
  | .error kind msg => pure (.error kind msg)
  | .host name => pure (.host name)
  | .undef => pure (.prim .undef)
  | .null => pure (.prim .null)
  | .bool b => pure (.prim (.bool b))
  | .num n => pure (.prim (.num n))
  | .str x => pure (.prim (.str x))

/-- The value the world names. -/
def fromW : WVal → M W Val
  | .prim p => if p.inModel then pure p.val else unknown "the world gives a string outside the Basic Multilingual Plane"
  | .handle k => fun s =>
    match s.world.handed[k]? with
    | some a => .ok (.obj a) s
    | none => .unknown "the world names an object it was not handed" s
  | .error kind msg => pure (.error kind msg)
  | .host name => pure (.host name)

/-- One step of the world. -/
def talk (told : Told) : M W Request := fun s =>
  let (req, state) := W.talk told s.world.state
  .ok req { s with world := { s.world with state } }

/-- Run `m` and give back whether it threw. -/
def attempt {α : Type} (m : M W α) : M W (Except Val α) := fun s =>
  match m s with
  | .ok a s' => .ok (.ok a) s'
  | .throw v s' => .ok (.error v) s'
  | .unknown why s' => .unknown why s'
  | .fuel => .fuel

/-- The binding at position `i` of what the code was handed. A name that the program does not declare is the host's,
and the world holds it itself. -/
def opaqueCell (cells : List (Option Addr)) (i : Nat) : M W Addr :=
  match cells[i]? with
  | some (some a) => pure a
  | some none => unknown "the world asks for the binding of a name the program does not declare"
  | none => unknown "the world names a binding the code was not handed"

def readCell (a : Addr) : M W Val := do
  match (← getState).cellAt a with
  | some { value := some v, .. } => pure v
  | _ => throwErr .reference "cannot access a binding before initialization"

def writeCell (a : Addr) (v : Val) : M W Unit := do
  match (← getState).cellAt a with
  | some { value := some _, mutable := true } => setCell a v
  | some { value := some _, mutable := false } => throwErr .type "assignment to constant variable"
  | _ => throwErr .reference "cannot access a binding before initialization"

/-- An ECMAScript array-index property name: canonical decimal spelling, excluding `2^32 - 1`. -/
def arrayIndex? (key : String) : Option Nat := do
  let index ← key.toNat?
  if index < 4294967295 ∧ toString index = key then some index else none

/-- Own string keys: array indices in numeric order, then the other keys in insertion order. The model has no symbols. -/
def ownPropertyKeys (props : List (String × Property)) : List String :=
  let indexed : List (Nat × String) := props.filterMap fun (key, _) => (arrayIndex? key).map (·, key)
  (indexed.mergeSort fun a b => decide (a.1 ≤ b.1)).map (·.2) ++
    props.filterMap fun (key, _) => if (arrayIndex? key).isNone then some key else none

/-- The names of the own properties of an object, in ECMAScript enumeration order. -/
def ownKeys (v : Val) : M W (List String) :=
  match v with
  | .obj a => do pure (ownPropertyKeys (← getObj a).props)
  | _ => unknown "the own keys of a value that is not an object of the program"

end CoreJS
