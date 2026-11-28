import Lean.Data.Json
import Std.Data.HashMap
import Std.Data.HashSet

open Lean

namespace Effects

/-! ## Abstract values -/

/-- A sorted array of codes: `0` some primitive that is not `undefined` or `null`, `1` the world object, `o + 1` the
object `o`. `undefined` and `null` are not values here: nothing can be read from them, called, or stored through them
(it throws), and they alias nothing, so a variable that may be `undefined` simply holds what else it may hold. -/
abbrev AV :=
  Array Nat

namespace AV

def prim : AV :=
  #[0]

def top : AV :=
  #[1]

def ofObj (o : Nat) : AV :=
  #[o + 1]

def join (a b : AV) : AV :=
  Id.run do
    if a.isEmpty then
      return b
    if b.isEmpty then
      return a
    let mut out : AV := Array.mkEmpty (a.size + b.size)
    let mut i := 0
    let mut j := 0
    while i < a.size && j < b.size do
      let x := a[i]!
      let y := b[j]!
      if x < y then
        out := out.push x;
        i := i + 1
      else if y < x then
        out := out.push y;
        j := j + 1
      else
        out := out.push x;
        i := i + 1;
        j := j + 1
    while i < a.size do
      out := out.push a[i]!;
      i := i + 1
    while j < b.size do
      out := out.push b[j]!;
      j := j + 1
    return out

def joinAll (l : List AV) : AV :=
  l.foldl join #[]

/-- The union of many sets: one sort instead of a merge per set. -/
def joinMany (xs : Array AV) : AV :=
  if xs.size ≤ 1 then xs.getD 0 #[]
  else
    let all := xs.foldl (· ++ ·) #[]
    let sorted := all.qsort (· < ·)
    Id.run do
      let mut out : AV := Array.mkEmpty sorted.size
      for x in sorted do
        if out.back? != some x then
          out := out.push x
      return out

/-- The objects (object `0` is the world). -/
def objs (a : AV) : List Nat :=
  a.toList.filterMap fun c => if c ≥ 1 then some (c - 1) else none

end AV

/-! ## Objects, the heap, the state -/

abbrev Scope :=
  List (Std.HashMap String Nat)

inductive Kind where
  | world
  /-- a variable -/
  | cell
  | plain
  | array
  /-- an instance of `Map`, `Set`, …: iterating it yields `any` -/
  | coll
  /-- `this` is what an arrow function captured -/
  | func (code : Json) (scope : Scope) (this : AV)
  | klass (code : Json) (scope : Scope)
  /-- `f.bind(this, …)`: calling it calls `target` -/
  | bound (target : AV) (this : AV) (args : List AV)
  /-- an intrinsic, or a vendor function (`vendor:…`) -/
  | builtin (path : String)
  deriving Inhabited

structure Obj where
  kind : Kind := .plain
  /-- the module-scope name, for the cell of a module-scope variable -/
  name : Option String := none
  props : Std.HashMap String AV := {}
  any : AV := #[]
  proto : AV := #[]
  /-- keys with a getter or a setter anywhere in this object -/
  acc : List String := []
  /-- the family of a vendor object: its members are the ones the premise lists -/
  fam : Option String := none
  escaped : Bool := false
  /-- a variable that a function the world may call assigns: what it holds may be anything -/
  taint : Bool := false
  /-- a function was stored under `valueOf`, `toString`, a well-known symbol, or a key the analysis does not know:
  converting the object may run it (arrays and collections keep their elements in `any`, which are not hooks) -/
  hook : Bool := false
  deriving Inhabited

/-- What is assumed about code the analysis cannot see. Printed with every verdict. -/
structure Premises where
  /-- vendor entries (`from#imported`, with `.member` suffixes) that only allocate, and their family -/
  vendor : Std.HashMap String String := {}
  /-- per family, the members that only allocate a new object of the family -/
  members : Std.HashMap String (Std.HashSet String) := {}
  /-- P3: the world defines no accessors and no conversion hooks on objects the program made. Without it, reading,
  assigning or converting an object the world holds is a call into the world. -/
  noWorldHooks : Bool := false
  deriving Inhabited

structure Fr where
  scope : List (Std.HashMap String Nat) := []
  this : AV := AV.prim
  /-- the cell that collects what `return` returns -/
  ret : Option Nat := none
  /-- the chain of calls that reached here, for allocation sites -/
  path : UInt64 := 0
  /-- the call that started this activation: allocation sites are told apart by the last two calls only -/
  site : UInt64 := 0
  depth : Nat := 0
  /-- the class whose constructor runs, for `super(…)` -/
  klass : Option Nat := none

/-- A change the bundle makes to an intrinsic (see `patchIntrinsic`). -/
structure Patch where
  unit : Nat
  /-- the object changed: `Symbol`, `Array.prototype`, or `global` for a global that names an intrinsic -/
  path : String
  /-- the member changed, if it is known -/
  key : Option String
  what : String

/-- The members that the language itself reads, besides the code of the program: a conversion, a spread, an `await`,
`instanceof`, `new`. A change to one of these, or to a prototype (`for…in` reads it), or to a member that is not known,
can be read by any unit. A change to any other member of a function or a namespace is read where the program names it
(`Symbol.dispose`, `Promise.withResolvers`), which `recBuiltinRead` records. -/
def implicitKeys : List String :=
  ["valueOf", "toString", "toLocaleString", "toJSON", "then", "catch", "finally", "constructor",
    "__proto__", "prototype", "stackTraceLimit", "prepareStackTrace"]

def Patch.tracked (p : Patch) : Bool :=
  !p.path.endsWith ".prototype" &&
    (match p.key with
    | some k => !implicitKeys.contains k
    | none => false)

structure St where
  objs : Array Obj := #[{ kind := .world, escaped := true }]
  sites : Std.HashMap (Nat × UInt64) Nat := {}
  builtins : Std.HashMap String Nat := {}
  /-- module-scope name -> its cell -/
  mods : Std.HashMap String Nat := {}
  /-- local name of an import -> (source, imported name) -/
  imports : Std.HashMap String (String × String) := {}
  prem : Premises := {}
  usedVendor : Std.HashSet String := {}
  thrown : AV := #[]
  /-- how many `try` blocks with a `catch` are around the code that runs, through every call: a `throw` under one is
  caught -/
  guarded : Nat := 0
  unit : Nat := 0
  /-- of the unit being run -/
  writes : Std.HashSet Nat := {}
  /-- module-scope names the unit being run read, in its own text or in a function it ran -/
  reads : Std.HashSet String := {}
  /-- the unit touches the environment: it is ordered against every other unit that does -/
  world : Bool := false
  /-- the unit may run code the analysis cannot see: it may use every object the world holds -/
  unk : Bool := false
  fuel : Nat := 0
  broke : Bool := false
  /-- node types the interpreter did not understand -/
  unknown : Array String := #[]
  /-- counts every growth of the state, to see when a loop stops changing it -/
  changes : Nat := 0
  /-- calls already run: (function, call site, arguments) -> (changes when it ended, result, writes, reads, world).
  A call repeated on a state that has not changed since would do nothing new, so its footprint is replayed. -/
  memo : Std.HashMap (UInt64 × UInt64) (Nat × AV × Array Nat × Array String × Bool × Bool × AV) :=
    {}
  /-- how many getters, setters, conversion hooks and protocol methods run one inside another (see `callHooks`) -/
  hookDepth : Nat := 0
  dbg : Array Nat := #[0, 0, 0, 0]
  /-- the hash of each piece of code, by address (see `codeHash`) -/
  codeHashes : Std.HashMap USize UInt64 := {}
  /-- what each piece of code mentions and assigns, by address (see `codeInfo`) -/
  codeInfos : Std.HashMap USize (Array String × Array String) := {}
  /-- what each piece of code patches by name, by address (see `codePatches`) -/
  codePatchInfos : Std.HashMap USize (Array (String × Option String)) := {}
  /-- why the unit being run touches the world: the first reason -/
  why : String := ""
  /-- why it runs unknown code: the first reason -/
  whyUnk : String := ""
  /-- how the call being made is written, for the reason of a call into the world -/
  callLabel : String := ""
  /-- the frame of the function being run: accessors run from here -/
  cur : Fr := {}
  /-- what the bundle changes in the intrinsics, once for each member -/
  patches : Array Patch := #[]

abbrev M :=
  StateM St

end Effects
