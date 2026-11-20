import Lean.Data.Json
import Std.Data.HashMap
import Std.Data.HashSet
import Experiments.Lower
import Experiments.Intrinsics

/-!
# What each top-level statement does, found by running the bundle abstractly

The checker needs, for every statement, whether it touches *the world* (the environment, or code we cannot see) and
which names it may change (`Stmt.effect`, `Stmt.writes`). This file computes both from the code. It runs the whole
top level once, in order, over abstract values:

* a value is a set of codes: *some primitive*, *the world object* (`top`), or an object (an allocation site, in the
  context of the unit and the chain of calls that reached it);
* an object has its properties by constant key (`props`), a bucket for every other key (`any`), prototypes (`proto`),
  and the keys that have a getter or a setter (`acc`). A variable is an object too (a *cell*); so is a function (it
  holds the cells it captured);
* every update is weak (it only adds), so one pass over the units in order is a sound summary of the state before
  each unit, whichever valid order the units are run in (a valid order keeps every conflicting pair in place, so the
  cells a unit reads hold the same values).

A call to a function the analysis can see into is run, with the abstract arguments (the body is *inlined*, in a
frame of its own, up to a depth and a budget). A call to an intrinsic is applied from the summary table
(`Intrinsics.lean`). Everything else is a call into the world: the unit gets `effect` and `unk`, and every object passed
in *escapes*: the world may hold it, mutate it, and call any function reachable from it, at any later world call.
Escaped functions are not run again. The variables they assign are *tainted* (a read may return anything), and a unit
that runs unknown code writes every name that reaches an escaped object, which orders it against every unit that reads
or binds one of those names.

Code the language runs without a call expression is treated the same way: a getter or a setter (own or inherited), a
generator body (run when the iterator is advanced, in some later unit), a function called deeper than the analysis
follows or through more candidates than it follows, and a `then` of a thenable. A function that is run but not looked
into escapes, with its closure.

**Fail safe.** A construct this file does not understand, or a unit that exhausts its budget, is a call into the world
that also escapes everything the unit mentions. More precision is added by understanding a construct. A wrong
understanding is one way to get a wrong answer; another is a give-up that forgets what the code it skipped can reach.
The reordered programs of `experiments/effects` (checked against Node) and `experiments/translation/fuzz.mjs` hunt
for both.

**What is a footprint.** The writes of a unit are the objects its run assigned to or changed. They are reported as
*names*: every module-scope name from which the object can be reached (through contents, prototypes, the cells a
function captured, and the module-scope names a function mentions). A reader of the object holds one of those names,
so the ordinary name conflict of `Bundle.conflict` sees it. A unit that touches the world writes every name that can
reach an escaped object, and reads them too.

Premises (assumed, not checked; `premiseLines` prints them with every verdict):
P1 and P3 (`Intrinsics.lean`);
P4 the bundle's evaluation completes, which is premise A6 of `debundle/docs/design.md`: nothing throws out of the top
level, so a unit that merely might throw is not an effect for that reason;
P5 no other chunk runs while this top level runs, except through callbacks this chunk gave the world;
and the vendor table (`Premises`).
-/

open Lean Lower

namespace Effects

/-- The distinct names of `a`, in order of first occurrence. -/
def dedupeArr (a : Array String) : Array String := Id.run do
  let mut seen : Std.HashSet String := {}
  let mut out : Array String := Array.mkEmpty a.size
  for x in a do
    unless seen.contains x do
      seen := seen.insert x
      out := out.push x
  return out

/-! ## Abstract values -/

/-- A sorted array of codes: `0` some primitive that is not `undefined` or `null`, `1` the world object, `o + 1` the
object `o`. `undefined` and `null` are not values here: nothing can be read from them, called, or stored through them
(it throws), and they alias nothing, so a variable that may be `undefined` simply holds what else it may hold. -/
abbrev AV := Array Nat

namespace AV
def prim : AV := #[0]
def top : AV := #[1]
def ofObj (o : Nat) : AV := #[o + 1]

def join (a b : AV) : AV := Id.run do
  if a.isEmpty then return b
  if b.isEmpty then return a
  let mut out : AV := Array.mkEmpty (a.size + b.size)
  let mut i := 0
  let mut j := 0
  while i < a.size && j < b.size do
    let x := a[i]!
    let y := b[j]!
    if x < y then out := out.push x; i := i + 1
    else if y < x then out := out.push y; j := j + 1
    else out := out.push x; i := i + 1; j := j + 1
  while i < a.size do out := out.push a[i]!; i := i + 1
  while j < b.size do out := out.push b[j]!; j := j + 1
  return out

def joinAll (l : List AV) : AV := l.foldl join #[]

/-- The union of many sets: one sort instead of a merge per set. -/
def joinMany (xs : Array AV) : AV :=
  if xs.size ≤ 1 then xs.getD 0 #[] else
  let all := xs.foldl (· ++ ·) #[]
  let sorted := all.qsort (· < ·)
  Id.run do
    let mut out : AV := Array.mkEmpty sorted.size
    for x in sorted do
      if out.back? != some x then out := out.push x
    return out

/-- The objects (object `0` is the world). -/
def objs (a : AV) : List Nat := a.toList.filterMap fun c => if c ≥ 1 then some (c - 1) else none

def hasObj (a : AV) : Bool := a.any (· ≥ 1)
end AV

/-! ## Objects, the heap, the state -/

abbrev Scope := List (Std.HashMap String Nat)

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
  kind    : Kind := .plain
  /-- the module-scope name, for the cell of a module-scope variable -/
  name    : Option String := none
  props   : Std.HashMap String AV := {}
  any     : AV := #[]
  proto   : AV := #[]
  /-- keys with a getter or a setter anywhere in this object -/
  acc     : List String := []
  /-- the family of a vendor object: its members are the ones the premise lists -/
  fam     : Option String := none
  escaped : Bool := false
  /-- a variable that a function the world may call assigns: what it holds may be anything -/
  taint   : Bool := false
  /-- a function was stored under `valueOf`, `toString`, a well-known symbol, or a key the analysis does not know:
  converting the object may run it (arrays and collections keep their elements in `any`, which are not hooks) -/
  hook    : Bool := false
  deriving Inhabited

/-- What is assumed about code the analysis cannot see. Printed with every verdict. -/
structure Premises where
  /-- vendor entries (`from#imported`, with `.member` suffixes) that only allocate, and their family -/
  vendor : Std.HashMap String String := {}
  /-- per family, the members that only allocate a new object of the family -/
  members : Std.HashMap String (Std.HashSet String) := {}
  /-- P3: the world defines no accessors and no conversion hooks on objects the program made, and converting a value
  the world made runs no program code. Without it, reading or assigning a property of an object the world holds is a
  call into the world, and so is every conversion of a value the world made or holds. -/
  noWorldHooks : Bool := false
  deriving Inhabited

structure Fr where
  scope : List (Std.HashMap String Nat) := []
  this  : AV := AV.prim
  /-- the cell that collects what `return` returns -/
  ret   : Option Nat := none
  /-- every argument joined, for `arguments` -/
  args  : AV := #[]
  /-- the chain of calls that reached here, for allocation sites -/
  path  : UInt64 := 0
  /-- the call that started this activation: allocation sites are told apart by the last two calls only -/
  site  : UInt64 := 0
  depth : Nat := 0
  /-- the class whose constructor runs, for `super(…)` -/
  klass : Option Nat := none

/-- A change the bundle makes to an intrinsic (see `patchIntrinsic`). -/
structure Patch where
  unit : Nat
  /-- the object changed: `Symbol`, `Array.prototype`, or `global` for a global that names an intrinsic -/
  path : String
  /-- the member changed, if it is known -/
  key  : Option String
  what : String

/-- The members that the language itself reads, besides the code of the program: a conversion, a spread, an `await`,
`instanceof`, `new`. A change to one of these, or to a prototype (`for…in` reads it), or to a member that is not known,
can be read by any unit. A change to any other member of a function or a namespace is read where the program names it
(`Symbol.dispose`, `Promise.withResolvers`), which `recBuiltinRead` records. -/
def implicitKeys : List String :=
  ["valueOf", "toString", "toLocaleString", "toJSON", "then", "catch", "finally", "constructor", "__proto__", "prototype"]

def Patch.tracked (p : Patch) : Bool :=
  !p.path.endsWith ".prototype" && (match p.key with | some k => !implicitKeys.contains k | none => false)

structure St where
  objs     : Array Obj := #[{ kind := .world, escaped := true }]
  sites    : Std.HashMap (Nat × UInt64) Nat := {}
  builtins : Std.HashMap String Nat := {}
  /-- module-scope name -> its cell -/
  mods     : Std.HashMap String Nat := {}
  /-- local name of an import -> (source, imported name) -/
  imports  : Std.HashMap String (String × String) := {}
  prem     : Premises := {}
  usedVendor : Std.HashSet String := {}
  thrown   : AV := #[]
  unit     : Nat := 0
  /-- of the unit being run -/
  writes   : Std.HashSet Nat := {}
  /-- module-scope names the unit being run read, in its own text or in a function it ran -/
  reads    : Std.HashSet String := {}
  /-- the unit touches the environment: it is ordered against every other unit that does -/
  world    : Bool := false
  /-- the unit may run code the analysis cannot see: it may use every object the world holds -/
  unk      : Bool := false
  fuel     : Nat := 0
  broke    : Bool := false
  /-- node types the interpreter did not understand -/
  unknown  : Array String := #[]
  /-- counts every growth of the state, to see when a loop stops changing it -/
  changes  : Nat := 0
  /-- calls already run: (function, call site, arguments) -> (changes when it ended, result, writes, reads, world).
  A call repeated on a state that has not changed since would do nothing new, so its footprint is replayed. -/
  memo     : Std.HashMap (UInt64 × UInt64) (Nat × AV × Array Nat × Array String × Bool × Bool) := {}
  dbg      : Array Nat := #[0, 0, 0, 0]
  /-- the hash of each piece of code, by address (see `codeHash`) -/
  codeHashes : Std.HashMap USize UInt64 := {}
  /-- what each piece of code mentions and assigns, by address (see `codeInfo`) -/
  codeInfos : Std.HashMap USize (Array String × Array String) := {}
  /-- why the unit being run touches the world: the first reason -/
  why      : String := ""
  /-- why it runs unknown code: the first reason -/
  whyUnk   : String := ""
  /-- how the call being made is written, for the reason of a call into the world -/
  callLabel : String := ""
  /-- the frame of the function being run: accessors run from here -/
  cur      : Fr := {}
  /-- what the bundle changes in the intrinsics, once for each member -/
  patches  : Array Patch := #[]

abbrev M := StateM St

/-- The structural hash of a piece of code, computed once per node: hashing a function body again at every call and
every evaluation is what makes a big bundle slow. Only nodes of the units being analyzed are hashed, and those live as
long as the analysis does, so an address names one node. -/
unsafe def codeHashImpl (j : Json) : M UInt64 := do
  let a := ptrAddrUnsafe j
  match (← get).codeHashes[a]? with
  | some h => return h
  | none =>
    let h := hash j
    modify fun s => { s with codeHashes := s.codeHashes.insert a h }
    return h

@[implemented_by codeHashImpl] opaque codeHash (j : Json) : M UInt64

/-- What a piece of code mentions (reads or assigns, in its own text or in nested functions), and what it assigns. -/
unsafe def codeInfoImpl (j : Json) : M (Array String × Array String) := do
  let a := ptrAddrUnsafe j
  match (← get).codeInfos[a]? with
  | some r => return r
  | none =>
    let acc := (visit [] false j).run {} |>.2
    let r := (acc.eager ++ acc.lazy, acc.assignedEager ++ acc.assignedLazy)
    modify fun s => { s with codeInfos := s.codeInfos.insert a r }
    return r

@[implemented_by codeInfoImpl] opaque codeInfo (j : Json) : M (Array String × Array String)

/-- How many nested calls are run. -/
def maxDepth : Nat := 8
/-- Abstract steps per unit. -/
def budget : Nat := 100000

def tick : M Unit :=
  modify fun s => if s.fuel == 0 then { s with broke := true } else { s with fuel := s.fuel - 1 }

def isBroke : M Bool := return (← get).broke
def bump : M Unit := modify fun s => { s with changes := s.changes + 1 }
/-- Unknown code: it may do anything, to anything the world holds. `why` is kept for the first reason a unit gets. -/
def setWorld (why : String := "") : M Unit :=
  modify fun s => { s with world := true, unk := true, why := if s.why.isEmpty then why else s.why,
                           whyUnk := if s.whyUnk.isEmpty then why else s.whyUnk }

/-- An effect on the environment only. -/
def setTrace (why : String := "") : M Unit :=
  modify fun s => { s with world := true, why := if s.why.isEmpty then why else s.why }
def recWrite (o : Nat) : M Unit := modify fun s => { s with writes := s.writes.insert o }
def getObj (o : Nat) : M Obj := return (← get).objs[o]!
def modObj (o : Nat) (f : Obj → Obj) : M Unit := modify fun s => { s with objs := s.objs.modify o f }

/-- The bundle changes an intrinsic, or replaces a global that names one: premise P1 does not hold for this program.
A unit that reads what was changed may read either value, so `footprints` orders it like a call into the world: the
units that read the member (`recBuiltinRead`), or, for a change that is not read by name (see `implicitKeys`), every
unit that is not a function declaration. -/
def patchIntrinsic (path : String) (key : Option String) (what : String) : M Unit :=
  modify fun s =>
    if s.patches.any (fun p => p.path == path && p.key == key) then s
    else { s with patches := s.patches.push { unit := s.unit, path, key, what } }

/-- Whether every object among `v` is an intrinsic, and there is one. A change to it is then a change the bundle makes
by name (`Object.prototype.x = …`). A change through a value that may also be another object is not counted: the sets
of values the analysis computes are wide (`while (up !== proto) up = Object.getPrototypeOf(up)` may also end at
`Object.prototype`), and counting those would make every library a patch. Objects of the environment and of a vendor
library are the world's, and an effect already. -/
def onlyIntrinsics (v : AV) : M Bool := do
  let mut found := false
  for c in v do
    if c == 0 then continue
    if c == 1 then return false
    match (← get).objs[c - 1]!.kind with
    | .builtin p =>
      if p.startsWith "env:" || p.startsWith "vendor" then return false
      found := true
    | _ => return false
  return found

/-- A change to the objects of `v`, whose keys are not known: a patch when they are all intrinsics. -/
def noteChange (v : AV) (what : String) : M Unit := do
  if ← onlyIntrinsics v then
    for c in v do
      if c ≥ 2 then
        if let .builtin p := (← get).objs[c - 1]!.kind then patchIntrinsic p none s!"{what} {p}"

/-- A read of a member of an intrinsic (`Symbol.dispose`), or of all of them: `footprints` compares what a unit read with
what the bundle patches. The names start with `%`, which no variable does. -/
def recBuiltinRead (path : String) (key : Option String) : M Unit :=
  modify fun s => { s with reads := s.reads.insert ("%" ++ path ++ "." ++ key.getD "*") }

/-- Everything about the intrinsic objects among `v` is read: `Object.keys(Symbol)`, `for (k in Promise)`. -/
def recBuiltinsRead (v : AV) : M Unit := do
  for c in v do
    if c ≥ 2 then
      match (← get).objs[c - 1]!.kind with
      | .builtin p => unless p.startsWith "env:" || p.startsWith "vendor" do recBuiltinRead p none
      | _ => pure ()

def builtinObj (path : String) : M Nat := do
  match (← get).builtins[path]? with
  | some o => return o
  | none =>
    modifyGet fun s =>
      let o := s.objs.size
      (o, { s with objs := s.objs.push { kind := .builtin path }, builtins := s.builtins.insert path o,
                   changes := s.changes + 1 })

/-- An object for an allocation site in the current unit. The same site yields the same object. Arrays, plain
objects and functions start with their intrinsic prototype. (The state is updated in place: `get` followed by `set`
would keep a second reference to the arrays and copy them on every allocation.) -/
def alloc (k : Kind) (key : Option UInt64) : M Nat := do
  let st ← get
  let hit := key.bind fun h => st.sites[(st.unit, h)]?
  match hit with
  | some o =>
    match k with
    | .func .. | .klass .. | .bound .. => modObj o ({ · with kind := k })
    | _ => pure ()
    return o
  | none =>
    let protoPath := match k with
      | .array => some "Array.prototype"
      | .plain => some "Object.prototype"
      | .func .. | .klass .. | .bound .. => some "Function.prototype"
      | _ => none
    let proto ← match protoPath with
      | some pp => do pure (AV.ofObj (← builtinObj pp))
      | none => pure #[]
    modifyGet fun s =>
      let o := s.objs.size
      (o, { s with objs := s.objs.push { kind := k, proto }, changes := s.changes + 1,
                   sites := match key with | some h => s.sites.insert (s.unit, h) o | none => s.sites })

/-- Module-scope names a function or class mentions. -/
def mentions (code : Json) : Array String :=
  let acc := (visit [] false code).run {} |>.2
  acc.eager ++ acc.lazy

mutual

/-- Everything reachable from `o` may now be held, changed and called by the world. -/
partial def escapeObj (o : Nat) : M Unit := do
  let ob ← getObj o
  if ob.escaped || ob.kind matches .builtin _ then return
  modObj o ({ · with escaped := true })
  bump
  for v in ob.props.values do escapeAV v
  escapeAV ob.any
  escapeAV ob.proto
  match ob.kind with
  | .func code scope this =>
    escapeAV this
    escapeCode code scope
  | .klass code scope => escapeCode code scope
  | .bound target this args =>
    escapeAV target
    escapeAV this
    for a in args do escapeAV a
  | _ => pure ()

partial def escapeAV (v : AV) : M Unit := do
  for c in v do if c ≥ 1 then escapeObj (c - 1)

/-- The world may run the code at any time: what it can reach escapes, and what it assigns may be anything. (Code
the world runs is not run here: every variable it may assign reads as anything from now on.) Also for code that is
run but not looked into (a call deeper than the analysis follows, a generator that is advanced later). -/
partial def escapeCode (code : Json) (scope : Scope) : M Unit := do
  for fr in scope do for c in fr.values do escapeObj c
  let (mentioned, assigned) ← codeInfo code
  for n in mentioned do
    if let some c := (← get).mods[n]? then escapeObj c
  for n in assigned do
    let mods := (← get).mods
    let c? := match scope.findSome? (·[n]?) with
      | some c => some c
      | none => mods[n]?
    if let some c := c? then modObj c ({ · with taint := true })

end

/-- How many values a set may hold before it is given up: it becomes the world's, and everything in it escapes. A
bucket of thousands of objects costs more to look at than it can tell. -/
def avCap : Nat := 256

/-- Calls through a set with more function values than this are calls into the world. -/
def fanOut : Nat := 20

def widen (v : AV) : M AV := do
  if v.size ≤ avCap then return v
  escapeAV v
  modify fun s => { s with unknown := s.unknown.push "wide set" }
  return AV.top

/-- Whether a value may be a function the program made. -/
def hasFunction (v : AV) : M Bool := do
  for c in v do
    if c ≥ 2 then
      match (← getObj (c - 1)).kind with
      | .func .. | .klass .. | .bound .. => return true
      | _ => pure ()
  return false

/-- Add `v` to a field of an object; count a change when it grew. What is stored into an object the world holds is the
world's too: `joinAny`, `joinProp` and `joinProto` are the only ways to store. -/
def joinAny (o : Nat) (v : AV) : M Unit := do
  let ob ← getObj o
  if ob.escaped then escapeAV v
  let nw ← widen (ob.any.join v)
  if nw != ob.any then
    modObj o ({ · with any := nw })
    bump
  unless ob.hook || ob.kind matches .array | .coll do
    if ← hasFunction v then modObj o ({ · with hook := true })

def joinProp (o : Nat) (k : String) (v : AV) : M Unit := do
  let ob ← getObj o
  if ob.escaped then escapeAV v
  let old := ob.props.getD k #[]
  let nw ← widen (old.join v)
  if nw != old then
    modObj o fun x => { x with props := x.props.insert k nw }
    bump
  if !ob.hook && !v.isEmpty && (k.endsWith "valueOf" || k.endsWith "toString" || k.startsWith "@@") then
    modObj o ({ · with hook := true })

def joinProto (o : Nat) (v : AV) : M Unit := do
  let ob ← getObj o
  if ob.escaped then escapeAV v
  let nw := ob.proto.join v
  if nw.size != ob.proto.size then
    modObj o ({ · with proto := nw })
    bump

/-- Mark everything the code mentions as escaped, and note the unit as touching the world. -/
def giveUp (why : String) (code : Json) : M Unit := do
  setWorld ("give up: " ++ why)
  modify fun s => { s with unknown := s.unknown.push why }
  for n in mentions code do
    if let some c := (← get).mods[n]? then escapeObj c

/-! ## Reading and writing properties -/

def litStr (j : Json) : Option String :=
  if typ j != "Literal" then none else
  match (fld j "lit").getStr? with
  | .ok s => if s.startsWith "s:" then some (s.drop 2).toString else none
  | .error _ => match (fld j "value").getStr? with | .ok s => some s | .error _ => none

/-- The constant key of a member access (`computed` false: the name; otherwise a string literal). -/
def memberKey (j : Json) : Option String :=
  let p := fld j "property"
  if flag j "computed" then litStr p
  else if typ p == "PrivateIdentifier" then some ("#" ++ str (fld p "name"))
  else some (str (fld p "name"))

/-- The constant key of a property of an object literal, a class or a pattern. -/
def propKey (j : Json) : Option String :=
  let k := fld j "key"
  if flag j "computed" then litStr k
  else if typ k == "PrivateIdentifier" then some ("#" ++ str (fld k "name"))
  else if typ k == "Literal" then litStr k
  else some (str (fld k "name"))

/-- The accessor functions of kind `kind` (`"get"` or `"set"`) an object holds for `key` (any key if `none`): under the
key's own entry, and among the values of computed keys. -/
def accessorFns (ob : Obj) (key : Option String) (kind : String) : AV :=
  match key with
  | some k => (ob.props.getD (kind ++ ":" ++ k) #[]).join ob.any
  | none => ob.props.fold (fun acc k v => if k.startsWith (kind ++ ":") then acc.join v else acc) ob.any

/-! ## Variables -/

def readVar (c : Nat) : M AV := do
  let ob ← getObj c
  if let some n := ob.name then modify fun s => { s with reads := s.reads.insert n }
  return if ob.taint then ob.any.join AV.top else ob.any

/-- Assign to a variable: weak, and a write of the cell. -/
def writeVar (c : Nat) (v : AV) : M Unit := do
  recWrite c
  joinAny c v

def lookup (fr : Fr) (n : String) : M (Option Nat) := do
  for f in fr.scope do
    if let some c := f[n]? then return some c
  return (← get).mods[n]?

/-- A cheap identity for a node: its type and the few scalars that tell two nodes of one kind apart. Two nodes may
share one; that only merges their objects, which is sound. -/
partial def shallow (j : Json) : UInt64 :=
  let t := hash (typ j)
  match typ j with
  | "Identifier" => mixHash t (hash (str (fld j "name")))
  | "Literal" => mixHash t (hash (fld j "lit"))
  | "MemberExpression" => mixHash (mixHash t (shallow (fld j "object"))) (hash (memberKey j))
  | "CallExpression" | "NewExpression" =>
    mixHash (mixHash t (shallow (fld j "callee"))) (UInt64.ofNat (items (fld j "arguments")).size)
  | "ObjectExpression" => mixHash t (UInt64.ofNat (items (fld j "properties")).size)
  | "ArrayExpression" => mixHash t (UInt64.ofNat (items (fld j "elements")).size)
  | _ => t

def siteOf (fr : Fr) (j : Json) : UInt64 := mixHash fr.path (shallow j)

/-- A function or a class must not share its object with another: the object holds the code. -/
def codeSite (fr : Fr) (j : Json) : M UInt64 := return mixHash fr.path (← codeHash j)

/-- A fresh frame holding cells for `names`, each starting as `undefined`. -/
def newFrame (fr : Fr) (names : List String) : M Fr := do
  let mut m : Std.HashMap String Nat := {}
  for n in names do
    let c ← alloc .cell (some (mixHash fr.path (hash ("cell:" ++ n))))
    m := m.insert n c
  return { fr with scope := m :: fr.scope }

/-! ## Calls into the world, and what the world may run -/

/-- Run `run` with the unit's footprint accumulators empty, merge them back, and remember the call. -/
def withMemo (key : UInt64 × UInt64) (run : M AV) : M AV := do
  let s ← get
  match s.memo[key]? with
  | some (c, r, ws, rs, w, u) =>
    if c == s.changes then
      modify fun s => { s with writes := ws.foldl (·.insert ·) s.writes, reads := rs.foldl (·.insert ·) s.reads,
                               world := s.world || w, unk := s.unk || u }
      return r
  | none => pure ()
  let (w0, r0, wd0, u0) := (s.writes, s.reads, s.world, s.unk)
  modify fun s => { s with writes := {}, reads := {}, world := false, unk := false }
  let res ← run
  let s2 ← get
  let (ws, rs, w, u) := (s2.writes, s2.reads, s2.world, s2.unk)
  modify fun s => { s with writes := ws.fold (·.insert ·) w0, reads := rs.fold (·.insert ·) r0, world := wd0 || w,
                           unk := u0 || u }
  unless s2.broke do
    modify fun s => { s with memo := s.memo.insert key (s.changes, res, ws.toArray, rs.toArray, w, u) }
  return res

mutual

/-- A getter or a setter runs on the object it was found for. The candidates that are program code are run, with the
value for a setter. A world function, or a receiver the world holds when the world may have added accessors to it
(`Premises.noWorldHooks` off), is unknown code. -/
partial def runAccessors (recv : Nat) (fns : AV) (args : List AV) (tag : String) : M AV := do
  let mut programs : AV := #[]
  let mut unknown := false
  for d in fns do
    if d == 1 then unknown := true
    else if d ≥ 2 then
      match (← getObj (d - 1)).kind with
      | .func .. | .klass .. | .bound .. => programs := programs.join (AV.ofObj (d - 1))
      | _ => pure ()
  if unknown then
    escapeAV fns
    escapeObj recv
    setWorld "getter or setter the world made"
  if programs.isEmpty then return (if unknown then AV.top else #[])
  let cur := (← get).cur
  let r ← callAll programs (AV.ofObj recv) args false cur (mixHash cur.site (hash tag))
  return if unknown then r.join AV.top else r

/-- Reading or assigning a property of an object the world holds: the world may have added accessors to it, which run
its code (unless the premise says it adds none). -/
partial def worldMayHaveAccessors (recv : Nat) : M Unit := do
  unless (← get).prem.noWorldHooks do
    escapeObj recv
    setWorld "a property of an object the world holds"

/-- Values stored in an object under `key` (any key if `none`), and in what it inherits. A getter on the way is run as
unknown code. -/
partial def getVal (c : Nat) (key : Option String) (seen : List Nat := []) : M AV := do
  if c == 0 then
    match key with
    | some k =>
      let child := "prim." ++ k
      if table.contains child then return AV.prim.join (AV.ofObj (← builtinObj child))
      else if members.contains child then return AV.prim.join AV.top
      else return AV.prim
    | none => return AV.prim.join AV.top
  let o := c - 1
  -- what the world holds may have been changed by the world's code, which only a unit that runs unknown code runs: such
  -- a unit writes every name that reaches the world's objects, including the names of the imports (see `footprints`)
  if o == 0 then return AV.top
  if seen.contains o || seen.length > 10 then return #[]
  let ob ← getObj o
  match ob.kind with
  | .world => return AV.top
  | .builtin path =>
    unless path.startsWith "env:" || path.startsWith "vendor" do recBuiltinRead path key
    match key with
    | none => return AV.top
    | some k =>
      let child := path ++ "." ++ k
      if path.startsWith "vendor:" then
        if (← get).prem.vendor.contains (child.drop 7).toString then
          modify fun s => { s with usedVendor := s.usedVendor.insert (child.drop 7).toString }
          return AV.ofObj (← builtinObj child)
        else return AV.top
      else if table.contains child then return AV.prim.join (AV.ofObj (← builtinObj child))
      -- the state of the environment: reading it is an effect, and the value is the world's
      else if path.startsWith "env:" then
        setTrace ("read of " ++ path)
        return AV.top
      -- a standard function is a function: it has `call`, `apply` and `bind`
      else if (k == "call" || k == "apply" || k == "bind") && !path.endsWith ".prototype"
          && !["Math", "JSON", "Reflect", "Atomics", "Intl"].contains path then
        return AV.ofObj (← builtinObj ("Function.prototype." ++ k))
      -- a constructor's prototype and a prototype's constructor are intrinsic objects, which a store can patch
      else if k == "prototype" && members.contains child then return AV.ofObj (← builtinObj child)
      else if k == "constructor" && path.endsWith ".prototype" then
        return AV.ofObj (← builtinObj (path.toRawSubstring.dropRight 10).toString)
      -- the property exists but is not summarized: a call of it is a call into the world
      else if members.contains child then return AV.top
      -- a prototype has only the members it has
      else if path.endsWith ".prototype" then return #[]
      -- an unlisted member of a namespace or a constructor
      else return AV.top
  | _ =>
    -- `any` of an array or a collection holds its elements, which a method or `length` is not
    let isElements := ob.kind matches .array | .coll
    let named := match key with | some k => isElements && !(k.all Char.isDigit) | none => false
    let mut r : AV := if named then #[] else ob.any
    match key with
    | some k =>
      r := r.join (ob.props.getD k #[])
      if k == "length" && isElements then r := r.join AV.prim
      if k == "__proto__" then r := r.join ob.proto
      if ob.acc.contains k || ob.acc.contains "*" then
        r := r.join (← runAccessors (seen.getLast?.getD o) (accessorFns ob key "get") [] ("get" ++ k))
      -- a function's own prototype object exists once someone asks for it
      if k == "prototype" && (ob.props.getD k #[]).isEmpty then
        match ob.kind with
        | .func code .. =>
          if typ code == "FunctionExpression" || typ code == "FunctionDeclaration" then
            let p ← alloc .plain (some (mixHash 7 (UInt64.ofNat o)))
            joinProp o "prototype" (AV.ofObj p)
            r := r.join (AV.ofObj p)
        | _ => pure ()
    | none =>
      for v in ob.props.values do r := r.join v
      unless ob.acc.isEmpty do
        r := r.join (← runAccessors (seen.getLast?.getD o) (accessorFns ob key "get") [] "get*")
    if ob.escaped then
      r := r.join AV.top
      worldMayHaveAccessors (seen.getLast?.getD o)
    -- a member of a vendor object: a method the premise lists allocates, anything else is unknown
    match ob.fam, key with
    | some f, some k =>
      if ((← get).prem.members.getD f {}).contains k then r := r.join (AV.ofObj (← builtinObj s!"vendor-member:{f}"))
      else r := r.join AV.top
    | some _, none => r := r.join AV.top
    | none, _ => pure ()
    for p in ob.proto do r := r.join (← getVal p key (o :: seen))
    return r

partial def getAV (a : AV) (key : Option String) : M AV := do
  let mut parts : Array AV := #[]
  for c in a do parts := parts.push (← getVal c key)
  let r := AV.joinMany parts
  modify fun s => { s with dbg := #[s.dbg[0]! + 1, max s.dbg[1]! a.size, s.dbg[2]! + (parts.foldl (· + ·.size) 0), max s.dbg[3]! (parts.foldl (· + ·.size) 0)] }
  return r

/-- Whether an assignment to `key` of the object `o` may run a setter (or a read a getter), found on the object and on
what it inherits, and the accessor functions of kind `kind` found. -/
partial def accessorsOn (o : Nat) (key : Option String) (kind : String) (seen : List Nat := []) : M (Bool × AV) := do
  if seen.contains o || seen.length > 10 || o == 0 then return (false, #[])
  let ob ← getObj o
  let mut found := match key with | some k => ob.acc.contains k || ob.acc.contains "*" | none => !ob.acc.isEmpty
  let mut fns : AV := if found then accessorFns ob key kind else #[]
  for p in ob.proto do
    if p ≥ 2 then
      let (f, a) ← accessorsOn (p - 1) key kind (o :: seen)
      found := found || f
      fns := fns.join a
  return (found, fns)

/-- Store `v` under `key` into every object of `target`. -/
partial def setAV (target : AV) (key : Option String) (v : AV) : M Unit := do
  let definite ← onlyIntrinsics target
  for c in target do
    if c == 0 then continue
    if c == 1 then
      setWorld "store into a value the world made"; escapeAV v; continue
    let o := c - 1
    let ob ← getObj o
    match ob.kind with
    | .builtin p =>
      -- the environment: a global, a title, a handler the world keeps
      if p.startsWith "env:" then
        setTrace ("store into " ++ p)
        -- replacing a global that names an intrinsic changes what the name means to every unit
        if let some k := key then
          if intrinsicGlobals.contains k then patchIntrinsic "global" (some k) s!"store into {p}.{k}"
      else
        setWorld ("store into " ++ p)
        if definite then patchIntrinsic p key s!"store into {p}{key.elim "" ("." ++ ·)}"
      escapeAV v
    | .world => setWorld "store into the world"; escapeAV v
    | _ =>
      recWrite o
      -- an assignment runs a setter the object or what it inherits has for the key
      let (hit, fns) ← accessorsOn o key "set"
      if hit then let _ ← runAccessors o fns [v] ("set" ++ key.getD "*")
      if ob.escaped then worldMayHaveAccessors o
      match key with
      | some "__proto__" => joinProto o v
      | some k => joinProp o k v
      | none => joinAny o v
      if ob.escaped then escapeAV v

/-- Everything the objects of `a` hold: the values iterating them or copying their properties yields. -/
partial def contentsOf (a : AV) : M AV := do
  let mut parts : Array AV := #[]
  for c in a do
    if c == 0 then parts := parts.push AV.prim
    else if c == 1 then
      parts := parts.push AV.top
    else
      let ob ← getObj (c - 1)
      parts := parts.push ob.any
      for v in ob.props.values do parts := parts.push v
      unless ob.acc.isEmpty do
        parts := parts.push (← runAccessors (c - 1) (accessorFns ob none "get") [] "get*")
      if ob.escaped then
        parts := parts.push AV.top
        worldMayHaveAccessors (c - 1)
      match ob.kind with
      | .builtin p =>
        unless p.startsWith "env:" || p.startsWith "vendor" do recBuiltinRead p none
        parts := parts.push AV.top
      | _ => pure ()
  return AV.joinMany parts

/-- A call into the world: it may do anything with what it is given, and keep it. -/
partial def worldCall (vals : List AV) : M AV := do
  setWorld ("call of " ++ (← get).callLabel)
  for v in vals do escapeAV v
  return AV.top

/-- Apply a call of any abstract value. -/
partial def callVal (c : Nat) (thisV : AV) (args : List AV) (isNew : Bool) (fr : Fr) (site : UInt64) : M AV := do
  if c == 0 then return #[]
  if c == 1 then return ← worldCall (thisV :: args)
  let ob ← getObj (c - 1)
  match ob.kind with
  | .func code scope cthis =>
    if isNew then
      let inst ← alloc .plain (some (mixHash (mixHash fr.path site) 17))
      joinProto inst (← getVal c (some "prototype"))
      let r ← callFunc code scope cthis (AV.ofObj inst) args fr site
      return (AV.ofObj inst).join (if r.any (· ≥ 1) then r else #[])
    else callFunc code scope cthis thisV args fr site
  | .klass .. =>
    if isNew then
      let inst ← alloc .plain (some (mixHash (mixHash fr.path site) 19))
      joinProto inst (← getVal c (some "prototype"))
      construct (c - 1) inst args fr site
    else return #[]
  | .builtin path => callBuiltin path thisV args fr site
  | .bound target bthis bargs =>
    tick
    if (← isBroke) then return AV.top
    if fr.depth ≥ maxDepth then
      -- the target may run later, at any time
      escapeAV target
      escapeAV bthis
      for a in bargs do escapeAV a
      return ← worldCall (thisV :: args)
    callAll target bthis (bargs ++ args) isNew { fr with depth := fr.depth + 1 } site
  | _ => return #[]

partial def callAll (callee : AV) (thisV : AV) (args : List AV) (isNew : Bool) (fr : Fr) (site : UInt64) : M AV := do
  let mut candidates := 0
  for c in callee do
    if c ≥ 2 then
      match (← getObj (c - 1)).kind with
      | .func .. | .klass .. | .bound .. => candidates := candidates + 1
      | _ => pure ()
  if candidates > fanOut then
    modify fun s => { s with unknown := s.unknown.push "many callees" }
    escapeAV callee
    return ← worldCall (thisV :: args)
  let mut r : AV := #[]
  for c in callee do r := r.join (← callVal c thisV args isNew fr site)
  return r

/-- Run a function body in a frame of its own. -/
partial def callFunc (code : Json) (scope : Scope) (cthis : AV) (thisV : AV) (args : List AV) (fr : Fr)
    (site : UInt64) : M AV := do
  let key := (mixHash (← codeHash code) (hash (scope.flatMap (·.values))), mixHash site (hash (cthis, thisV, args)))
  withMemo key (callFuncRun code scope cthis thisV args fr site)

partial def callFuncRun (code : Json) (scope : Scope) (cthis : AV) (thisV : AV) (args : List AV) (fr : Fr)
    (site : UInt64) : M AV := do
  tick
  if (← isBroke) then return AV.top
  if fr.depth ≥ maxDepth || flag code "generator" then
    -- The body is not run here: the world may run it, at any time. (A generator's body runs when its iterator is
    -- advanced, in whichever unit does that.)
    escapeCode code scope
    escapeAV cthis
    return ← worldCall (thisV :: args)
  let isArrow := typ code == "ArrowFunctionExpression"
  let params := items (fld code "params")
  let body := fld code "body"
  let stmts := if typ body == "BlockStatement" then items (fld body "body") else #[]
  let selfName := if typ code == "FunctionExpression" then binders (fld code "id") else []
  let path := site
  let retCell ← alloc .cell (some (mixHash path 1))
  let argJoin := AV.joinAll args
  let fr0 : Fr := { scope := scope, this := if isArrow then cthis else thisV, ret := some retCell,
                    args := argJoin, path, site, depth := fr.depth + 1, klass := fr.klass }
  let names := (params.toList.flatMap binders) ++ selfName ++ (stmts.toList.flatMap varNames) ++ lexicalNames stmts
  let fr1 ← newFrame fr0 names
  let outer := (← get).cur
  modify fun s => { s with cur := fr1 }
  if let some n := selfName.head? then
    -- a function expression's own name
    if let some c := fr1.scope.head!.get? n then
      let f ← alloc (.func code scope cthis) (some (← codeSite fr code))
      joinAny c (AV.ofObj f)
  for (p, i) in params.toList.zipIdx do
    if typ p == "RestElement" then
      let arr ← alloc .array (some (mixHash path (hash p)))
      joinAny arr (AV.joinAll (args.drop i))
      bindPattern fr1 (fld p "argument") (AV.ofObj arr)
    else bindPattern fr1 p (args[i]?.getD #[])
  if typ body == "BlockStatement" then
    hoistFns fr1 stmts
    for s in stmts do exec fr1 s
  else
    let v ← eval fr1 body
    writeVar retCell v
  modify fun s => { s with cur := outer }
  let r ← readVar retCell
  if flag code "async" then
    let p ← alloc .plain (some (mixHash path 23))
    joinProto p (AV.ofObj (← builtinObj "Promise.prototype"))
    joinAny p (r.join AV.top)
    return AV.ofObj p
  return r

/-- `new C(args)`: instance fields, then the constructor chain. -/
partial def construct (cls : Nat) (inst : Nat) (args : List AV) (fr : Fr) (site : UInt64) : M AV := do
  tick
  let ob ← getObj cls
  match ob.kind with
  | .klass code scope =>
    if fr.depth ≥ maxDepth then
      escapeObj cls
      return ← worldCall (AV.ofObj inst :: args)
    let path := mixHash site (hash (shallow code))
    let fr0 : Fr := { scope := scope, this := AV.ofObj inst, path, site, depth := fr.depth + 1, klass := some cls, args := AV.joinAll args }
    let members := items (fld (fld code "body") "body")
    -- instance fields
    for m in members do
      if typ m == "PropertyDefinition" && !flag m "static" then
        let v ← if isNull (fld m "value") then pure AV.prim else eval fr0 (fld m "value")
        recWrite inst
        match propKey m with
        | some k => joinProp inst k v
        | none => joinAny inst v
    let ctor := members.find? fun m => typ m == "MethodDefinition" && str (fld m "kind") == "constructor"
    match ctor with
    | some m =>
      let _ ← callFunc (fld m "value") scope AV.prim (AV.ofObj inst) args { fr0 with depth := fr.depth }
        (mixHash site 29)
      pure ()
    | none =>
      if !isNull (fld code "superClass") then
        let _ ← superCall fr0 args
    return AV.ofObj inst
  | _ => return AV.ofObj inst

/-- `super(args)`: construct the parent on the same `this`. -/
partial def superCall (fr : Fr) (args : List AV) : M AV := do
  let some cls := fr.klass | return ← worldCall (fr.this :: args)
  let ob ← getObj cls
  let mut r : AV := #[]
  let inst := (fr.this.objs.head?).getD 0
  for c in ob.proto do
    if c == 0 then continue
    if c == 1 then r := r.join (← worldCall (fr.this :: args))
    else
      let pob ← getObj (c - 1)
      match pob.kind with
      | .klass .. => r := r.join (← construct (c - 1) inst args fr 31)
      | .func code scope cthis => r := r.join (← callFunc code scope cthis fr.this args fr 37)
      | .builtin path => r := r.join (← callBuiltin path fr.this args fr 41)
      | _ => pure ()
  if ob.proto.isEmpty then r := r.join (← worldCall (fr.this :: args))
  return r

/-- Apply a summary from the table, or a vendor entry. -/
partial def callBuiltin (path : String) (thisV : AV) (args : List AV) (fr : Fr) (site : UInt64) :
    M AV := do
  if path.startsWith "vendor-member:" then
    -- a member the premise lists: allocates a new object of the family, holding the receiver and the arguments
    let fam := (path.drop 14).toString
    let o ← alloc .plain (some (mixHash (mixHash fr.path site) 43))
    modObj o ({ · with fam := some fam })
    joinAny o (AV.joinAll (thisV :: args))
    return AV.ofObj o
  if path.startsWith "vendor:" then
    let key := (path.drop 7).toString
    match (← get).prem.vendor[key]? with
    | some fam =>
      modify fun s => { s with usedVendor := s.usedVendor.insert key }
      let o ← alloc .plain (some (mixHash (mixHash fr.path site) 47))
      modObj o ({ · with fam := some fam })
      joinAny o (AV.joinAll args)
      return AV.ofObj o
    | none => return ← worldCall (thisV :: args)
  -- `f.call(t, …)`, `f.apply(t, args)`, `f.bind(t, …)`: the receiver is the function
  if path == "Function.prototype.call" || path == "Function.prototype.apply" then
    -- a level of the call chain, or a value that holds this function would recurse for ever
    if fr.depth ≥ maxDepth then return ← worldCall (thisV :: args)
    let deeper : Fr := { fr with depth := fr.depth + 1 }
    if path == "Function.prototype.call" then
      return ← callAll thisV (args[0]?.getD #[]) (args.drop 1) false deeper site
    let a ← contentsOf (args[1]?.getD #[])
    return ← callAll thisV (args[0]?.getD #[]) (List.replicate 4 a) false deeper site
  if path == "Function.prototype.bind" then
    let o ← alloc (.bound thisV (args[0]?.getD #[]) (args.drop 1)) (some (mixHash (mixHash fr.path site) 71))
    return AV.ofObj o
  match table[path]? with
  | none => worldCall (thisV :: args)
  | some s =>
    -- the standard functions convert their operands to numbers, strings and keys
    convert args fr (mixHash site 89)
    applySummary s thisV args fr site

partial def applySummary (s : Summary) (thisV : AV) (args : List AV) (fr : Fr) (site : UInt64) : M AV := do
  let pos (i : Nat) : AV := if i == 0 then thisV else args[i - 1]?.getD #[]
  let all : AV := AV.joinAll (thisV :: args)
  recBuiltinsRead all
  if s.world then setTrace ("call of " ++ (← get).callLabel)
  for p in s.convertsContents do convert [← contentsOf (pos p)] fr (mixHash site (UInt64.ofNat p))
  for p in s.iterates do let _ ← iterate (pos p) false
  for p in s.optionalIterates do let _ ← iterate (pos p) true
  for p in s.retains do escapeAV (pos p)
  for p in s.muts do
    noteChange (pos p) "mutation of"
    for c in pos p do
      if c == 1 then setWorld "mutation of a value the world made"
      else if c ≥ 2 then
        let o := c - 1
        recWrite o
        if (← getObj o).escaped then escapeAV all
  if s.storeArgs then
    setAV thisV none (AV.joinAll args)
  for (src, dst) in s.store do setAV (pos dst) none (pos src)
  if s.setProto then
    noteChange (pos 1) "prototype of"
    for c in pos 1 do
      if c == 1 then setWorld "prototype of a value the world made"
      else if c ≥ 2 then joinProto (c - 1) (pos 2)
  if s.define then
    -- the descriptor's contents become the target's contents. A descriptor that may carry a getter or a setter makes
    -- the property an accessor: reading it runs code the analysis does not see.
    let d := pos 3
    setAV (pos 1) none (← contentsOf d)
    let mut accessor := false
    for c in d do
      if c == 1 then accessor := true
      else if c ≥ 2 then
        let dob ← getObj (c - 1)
        match dob.kind with
        | .plain => if dob.props.contains "get" || dob.props.contains "set" || !dob.any.isEmpty || dob.escaped then accessor := true
        | _ => accessor := true
    if accessor then
      for c in pos 1 do
        if c ≥ 2 then modObj (c - 1) fun x => { x with acc := "*" :: x.acc }
  let deeper : Fr := { fr with depth := fr.depth + 1 }
  -- callbacks that cannot be run from here may run later, at any time
  let tooDeep := fr.depth ≥ maxDepth && !(s.callsWithWorld.isEmpty && s.calls.isEmpty)
  if tooDeep then
    for p in s.callsWithWorld ++ s.calls do escapeAV (pos p)
    let _ ← worldCall (thisV :: args)
  for p in s.callsWithWorld do
    unless tooDeep do
      let _ ← callAll (pos p) AV.prim [AV.top, AV.top] false deeper (mixHash site (UInt64.ofNat (p + 100)))
  let mut calledRet : AV := #[]
  if !s.calls.isEmpty && !tooDeep then
    let elems ← contentsOf (AV.joinAll (thisV :: args))
    for p in s.calls do
      calledRet := calledRet.join (← callAll (pos p) AV.prim [elems, AV.prim, thisV] false deeper (mixHash site (UInt64.ofNat p)))
  let mkArr (contents : AV) : M AV := do
    let o ← alloc .array (some (mixHash (mixHash fr.path site) 53))
    joinAny o contents
    return AV.ofObj o
  match s.ret with
  | .prim => return AV.prim
  | .fresh =>
    let o ← alloc .plain (some (mixHash (mixHash fr.path site) 59))
    if s.storeArgs then joinAny o (AV.joinAll args)
    return AV.ofObj o
  | .thisV => return thisV
  | .arg i => return pos (i + 1)
  | .contents p => contentsOf (pos p)
  | .freshOf p => mkArr (← contentsOf (pos p))
  | .freshOfCalls => mkArr calledRet
  | .freshPrims => mkArr AV.prim
  | .member => return AV.top
  | .freshProto p =>
    let o ← alloc .plain (some (mixHash (mixHash fr.path site) 73))
    joinProto o (pos p)
    return AV.ofObj o
  | .protoOf p =>
    let mut r : AV := #[]
    for c in pos p do
      if c ≤ 1 then r := r.join AV.top
      else
        let ob ← getObj (c - 1)
        r := r.join ob.proto
        if ob.kind matches .builtin _ then r := r.join AV.top
    return r
  | .freshOfEntries p =>
    let o ← alloc .plain (some (mixHash (mixHash fr.path site) 83))
    joinAny o (← contentsOf (← contentsOf (pos p)))
    return AV.ofObj o
  | .descriptorOf p =>
    let o ← alloc .plain (some (mixHash (mixHash fr.path site) 79))
    joinProp o "value" (← contentsOf (pos p))
    -- the source may be an accessor: then the descriptor has a getter
    for c in pos p do
      if c == 1 then joinProp o "get" AV.top
      else if c ≥ 2 then
        let sob ← getObj (c - 1)
        if !sob.acc.isEmpty || sob.escaped then joinProp o "get" AV.top
    return AV.ofObj o
  | .inst proto =>
    let o ← alloc (if proto == "Map.prototype" || proto == "Set.prototype" then .coll else .plain) (some (mixHash (mixHash fr.path site) 61))
    joinProto o (AV.ofObj (← builtinObj proto))
    -- `new Map(entries)` holds the keys and values inside the entries, `new Set(items)` the items
    if proto == "Map.prototype" then joinAny o (← contentsOf (← contentsOf (pos 1)))
    else if proto == "Set.prototype" then joinAny o (← contentsOf (pos 1))
    else joinAny o (AV.joinAll args)
    if proto == "Error.prototype" then
      joinProp o "message" AV.prim
      joinProp o "stack" AV.prim
    return AV.ofObj o

-- ## Expressions

/-- What an operator, a template string or a property key does to an operand: it converts it to a primitive, which runs
the operand's `valueOf` and `toString` (and `Symbol.toPrimitive`, which is a computed key and so is among the members
looked up). The lookups find what the program defines, and the calls are run; an object the world may hold has a
callee that is the world. Only a value the *world* made (`top`) is not looked into, by premise P3 (see
`Premises.noWorldHooks`). -/
partial def convert (vs : List AV) (fr : Fr) (site : UInt64) : M Unit := do
  -- each array is looked into once, however the arrays nest
  let mut seen : Std.HashSet Nat := {}
  let mut work : List AV := vs
  while true do
    let v :: rest := work | break
    work := rest
    for c in v do
      if c == 1 then
        unless (← get).prem.noWorldHooks do let _ ← worldCall [v]
      else if c ≥ 2 then
        let ob ← getObj (c - 1)
        match ob.kind with
        | .builtin _ | .coll => pure ()
        | .array =>
          -- `Array.prototype.toString` joins: the elements are converted. (`any` holds the elements, not methods.)
          unless seen.contains c do
            seen := seen.insert c
            for k in ["valueOf", "toString"] do
              let f := ob.props.getD k #[]
              unless f.isEmpty do let _ ← callAll f (AV.ofObj (c - 1)) [] false fr (mixHash site (hash k))
            work := ob.any :: work
        | _ =>
          if ← chainHasHook (c - 1) then
            for k in ["valueOf", "toString"] do
              let f ← hookFns (← getVal c (some k))
              unless f.isEmpty do let _ ← callAll f (AV.ofObj (c - 1)) [] false fr (mixHash site (hash k))

/-- Whether converting the object may run a function: it or what it inherits has a hook. -/
partial def chainHasHook (o : Nat) (seen : List Nat := []) : M Bool := do
  if seen.contains o || seen.length > 10 then return false
  let ob ← getObj o
  if ob.hook || !ob.acc.isEmpty then return true
  for p in ob.proto do
    if p ≥ 2 && (← chainHasHook (p - 1) (o :: seen)) then return true
  return false

/-- The candidates for a hook that are program code: the intrinsic ones run no program code, and a value the world
made runs none by P3 (`Premises.noWorldHooks`; without it, the world is a callee). -/
partial def hookFns (f : AV) : M AV := do
  let mut r : AV := #[]
  for d in f do
    if d == 1 then
      unless (← get).prem.noWorldHooks do r := r.join AV.top
    else if d ≥ 2 then
      match (← getObj (d - 1)).kind with
      | .func .. | .klass .. | .bound .. => r := r.join (AV.ofObj (d - 1))
      | _ => pure ()
  return r

/-- `await v`, or one step of `for await`: the rest of the async function runs after the program, in the order the
awaits were reached, so the unit that awaits is an effect and two such units keep their order. A thenable the program
defined has its `then` called when the value is resolved. -/
partial def awaitValue (v : AV) (fr : Fr) (site : UInt64) : M Unit := do
  setTrace "await"
  for c in v do
    if c ≥ 2 then
      let mut fns : AV := #[]
      for d in ← getVal c (some "then") do
        if d ≥ 2 then
          match (← getObj (d - 1)).kind with
          | .func .. | .klass .. | .bound .. => fns := fns.join (AV.ofObj (d - 1))
          | _ => pure ()
      unless fns.isEmpty do
        let _ ← callAll fns (AV.ofObj (c - 1)) [AV.top, AV.top] false fr site

partial def evalIdent (fr : Fr) (n : String) : M AV := do
  match ← lookup fr n with
  | some c => readVar c
  | none =>
    if let some (src, imp) := (← get).imports[n]? then
      modify fun s => { s with reads := s.reads.insert n }
      let key := src ++ "#" ++ imp
      if (← get).prem.vendor.contains key then
        modify fun s => { s with usedVendor := s.usedVendor.insert key }
        return AV.ofObj (← builtinObj ("vendor:" ++ key))
      else return AV.top
    else if n == "arguments" && fr.depth > 0 then
      let o ← alloc .array (some (mixHash fr.path 67))
      joinAny o fr.args
      return AV.ofObj o
    else if n == "undefined" then return #[]
    else if primGlobals.contains n then return AV.prim
    else if intrinsicGlobals.contains n then
      recBuiltinRead "global" (some n)
      return AV.ofObj (← builtinObj n)
    else if envGlobals.contains n then return AV.ofObj (← builtinObj ("env:" ++ n))
    else
      -- an undeclared global the analysis does not know: the environment
      setTrace ("global " ++ n)
      return AV.top

partial def evalArgs (fr : Fr) (args : Array Json) : M (List AV) := do
  let mut out : List AV := []
  for a in args do
    if typ a == "SpreadElement" then
      let v ← eval fr (fld a "argument")
      out := out ++ [← iterate v]
    else out := out ++ [← eval fr a]
  return out

/-- Whether the object or what it inherits holds a value under a key the analysis does not know. -/
partial def mayHaveComputedKey (o : Nat) (seen : List Nat := []) : M Bool := do
  if seen.contains o || seen.length > 10 then return false
  let ob ← getObj o
  if ob.kind matches .builtin _ then return false
  if !ob.any.isEmpty || !ob.acc.isEmpty || ob.escaped || ob.fam.isSome then return true
  for p in ob.proto do
    if p ≥ 2 && (← mayHaveComputedKey (p - 1) (o :: seen)) then return true
  return false

/-- The values iterating `v` yields (spread, `for … of`, array destructuring). -/
partial def iterate (v : AV) (optional : Bool := false) : M AV := do
  let mut r : AV := #[]
  for c in v do
    if c == 0 then r := r.join AV.prim
    else if c == 1 then r := r.join (← worldCall [AV.top])
    else
      let ob ← getObj (c - 1)
      match ob.kind with
      | .array | .coll => r := r.join (← contentsOf (AV.ofObj (c - 1)))
      | _ =>
        -- another object is iterable only through a `Symbol.iterator` the program defined, which is a computed key
        if optional && !(← mayHaveComputedKey (c - 1)) then r := r.join (← contentsOf (AV.ofObj (c - 1)))
        else r := r.join (← worldCall [AV.ofObj (c - 1)])
  return r

partial def eval (fr : Fr) (j : Json) : M AV := do
  if (← isBroke) then return AV.top
  tick
  match typ j with
  | "Literal" =>
    let lit := (fld j "lit").getStr?.toOption.getD ""
    if lit == "null" || (isNull (fld j "lit") && isNull (fld j "regex") && isNull (fld j "value") && !isNull (fld j "raw") && str (fld j "raw") == "null") then
      return #[]
    if lit.startsWith "r:" || !isNull (fld j "regex") then
      let o ← alloc .plain (some (siteOf fr j))
      joinProto o (AV.ofObj (← builtinObj "RegExp.prototype"))
      return AV.ofObj o
    return AV.prim
  | "Identifier" => evalIdent fr (str (fld j "name"))
  | "ThisExpression" => return fr.this
  -- `import.meta` is the module's record of its environment; `new.target` is the function being constructed, or nothing
  | "MetaProperty" =>
    if str (fld (fld j "meta") "name") == "import" then return AV.ofObj (← builtinObj "env:import.meta")
    else return AV.top
  | "ParenthesizedExpression" | "ChainExpression" => eval fr (fld j "expression")
  | "TemplateLiteral" =>
    let vs ← (items (fld j "expressions")).toList.mapM (eval fr)
    convert vs fr (shallow j)
    return AV.prim
  | "TaggedTemplateExpression" =>
    let tag := fld j "tag"
    let (callee, thisV) ← if typ tag == "MemberExpression" then do
        let o ← eval fr (fld tag "object")
        pure (← getAV o (memberKey tag), o)
      else pure (← eval fr tag, AV.prim)
    let q := fld j "quasi"
    let vs ← (items (fld q "expressions")).toList.mapM (eval fr)
    let strs ← alloc .array (some (siteOf fr j))
    callAll callee thisV (AV.ofObj strs :: vs) false fr (shallow j)
  | "ArrayExpression" =>
    let o ← alloc .array (some (siteOf fr j))
    for e in items (fld j "elements") do
      if isNull e then continue
      if typ e == "SpreadElement" then joinAny o (← iterate (← eval fr (fld e "argument")))
      else joinAny o (← eval fr e)
    return AV.ofObj o
  | "ObjectExpression" => evalObject fr j
  | "FunctionExpression" | "ArrowFunctionExpression" =>
    return AV.ofObj (← alloc (.func j fr.scope fr.this) (some (← codeSite fr j)))
  | "ClassExpression" => evalClass fr j
  | "UnaryExpression" =>
    let op := str (fld j "operator")
    let arg := fld j "argument"
    if op == "delete" && typ arg == "MemberExpression" then
      let o ← eval fr (fld arg "object")
      let k ← if flag arg "computed" then (do let _ ← eval fr (fld arg "property"); pure (memberKey arg)) else pure (memberKey arg)
      setAV o k AV.prim
      return AV.prim
    let v ← eval fr arg
    if op == "typeof" || op == "void" || op == "!" || op == "delete" then pure () else convert [v] fr (shallow j)
    return if op == "void" then #[] else AV.prim
  | "BinaryExpression" =>
    let a ← eval fr (fld j "left")
    let b ← eval fr (fld j "right")
    let op := str (fld j "operator")
    if op == "in" then recBuiltinsRead b
    if op == "===" || op == "!==" then pure () else convert [a, b] fr (shallow j)
    return AV.prim
  | "LogicalExpression" =>
    let a ← eval fr (fld j "left")
    let b ← eval fr (fld j "right")
    return a.join b
  | "ConditionalExpression" =>
    let _ ← eval fr (fld j "test")
    let a ← eval fr (fld j "consequent")
    let b ← eval fr (fld j "alternate")
    return a.join b
  | "SequenceExpression" =>
    let mut r : AV := AV.prim
    for e in items (fld j "expressions") do r ← eval fr e
    return r
  | "MemberExpression" =>
    if typ (fld j "object") == "Super" then
      giveUp "super member" j
      return AV.top
    let o ← eval fr (fld j "object")
    if flag j "computed" then
      convert [← eval fr (fld j "property")] fr (shallow j)
    getAV o (memberKey j)
  | "AssignmentExpression" => evalAssign fr j
  | "UpdateExpression" =>
    -- reads the target, converts what it read to a number, and assigns
    let t := fld j "argument"
    if typ t == "MemberExpression" then
      let o ← eval fr (fld t "object")
      if flag t "computed" then
        convert [← eval fr (fld t "property")] fr (shallow j)
      convert [← getAV o (memberKey t)] fr (shallow j)
      setAV o (memberKey t) AV.prim
    else
      convert [← eval fr t] fr (shallow j)
      assignTo fr t AV.prim
    return AV.prim
  | "CallExpression" => evalCall fr j false
  | "NewExpression" => evalCall fr j true
  | "AwaitExpression" =>
    awaitValue (← eval fr (fld j "argument")) fr (shallow j)
    return AV.top.join AV.prim
  | "ImportExpression" =>
    let v ← eval fr (fld j "source")
    worldCall [v]
  | t =>
    giveUp t j
    return AV.top

partial def evalObject (fr : Fr) (j : Json) : M AV := do
  let o ← alloc .plain (some (siteOf fr j))
  for p in items (fld j "properties") do
    if typ p == "SpreadElement" then
      let src ← eval fr (fld p "argument")
      for c in src do
        if c ≥ 2 then
          let sob ← getObj (c - 1)
          unless sob.acc.isEmpty do setWorld "getter or setter"
      joinAny o (← contentsOf src)
    else
      let key ← if flag p "computed" then (do let _ ← eval fr (fld p "key"); pure (propKey p)) else pure (propKey p)
      let v ← eval fr (fld p "value")
      let kind := str (fld p "kind")
      if kind == "get" || kind == "set" then
        -- the function is kept under its own key so that escaping finds it; reads of the key are a world call
        modObj o fun x => { x with acc := (key.getD "") :: x.acc }
        match key with
        | some k => joinProp o (kind ++ ":" ++ k) v
        | none => joinAny o v
      else
        match key with
        -- `__proto__: v` in a literal sets the prototype
        | some "__proto__" => if flag p "computed" || flag p "shorthand" || flag p "method" then joinProp o "__proto__" v else joinProto o v
        | some k => joinProp o k v
        | none => joinAny o v
  return AV.ofObj o

partial def evalClass (fr : Fr) (c : Json) : M AV := do
  let hasSuper := !isNull (fld c "superClass")
  let superAV ← if hasSuper then eval fr (fld c "superClass") else pure #[]
  let site ← codeSite fr c
  let cname := binders (fld c "id")
  let innerCell ← alloc .cell (some (mixHash site 2))
  let scope' := match cname.head? with
    | some n => (({} : Std.HashMap String Nat).insert n innerCell) :: fr.scope
    | none => fr.scope
  let C ← alloc (.klass c scope') (some site)
  let P ← alloc .plain (some (mixHash site 3))
  joinAny innerCell (AV.ofObj C)
  joinProp C "prototype" (AV.ofObj P)
  joinProto C superAV
  let objProto := AV.ofObj (← builtinObj "Object.prototype")
  let superProto ← if hasSuper then getAV superAV (some "prototype") else pure objProto
  joinProto P superProto
  let fr' : Fr := { fr with scope := scope', this := AV.ofObj C }
  for m in items (fld (fld c "body") "body") do
    match typ m with
    | "MethodDefinition" =>
      let kind := str (fld m "kind")
      let static := flag m "static"
      let target := if static then C else P
      let key ← if flag m "computed" then (do let _ ← eval fr' (fld m "key"); pure (propKey m)) else pure (propKey m)
      let f ← alloc (.func (fld m "value") scope' AV.prim) (some (mixHash site (← codeHash m)))
      if kind == "constructor" then pure ()
      else if kind == "get" || kind == "set" then
        modObj target fun x => { x with acc := (key.getD "") :: x.acc }
        match key with
        | some k => joinProp target (kind ++ ":" ++ k) (AV.ofObj f)
        | none => joinAny target (AV.ofObj f)
      else
        match key with
        | some k => joinProp target k (AV.ofObj f)
        | none => joinAny target (AV.ofObj f)
    | "PropertyDefinition" =>
      if flag m "computed" then
        let _ ← eval fr' (fld m "key")
      if flag m "static" && !isNull (fld m "value") then
        let v ← eval fr' (fld m "value")
        recWrite C
        match propKey m with
        | some k => joinProp C k v
        | none => joinAny C v
    | "StaticBlock" =>
      let stmts := items (fld m "body")
      let fr'' ← newFrame fr' (lexicalNames stmts ++ stmts.toList.flatMap varNames)
      hoistFns fr'' stmts
      for s in stmts do exec fr'' s
    | t => giveUp t m
  return AV.ofObj C

/-- How a callee is written, for the reason a call is a call into the world. -/
partial def calleeLabel (fr : Fr) (j : Json) : M String := do
  match typ j with
  | "Identifier" =>
    let n := str (fld j "name")
    match ← lookup fr n with
    | some c => return (match (← getObj c).name with | some m => "variable " ++ m | none => "local " ++ n)
    | none =>
      match (← get).imports[n]? with
      | some (src, imp) => return s!"import {src}#{imp}"
      | none => return "global " ++ n
  | "MemberExpression" => return (← calleeLabel fr (fld j "object")) ++ "." ++ ((memberKey j).getD "[]")
  | "CallExpression" => return (← calleeLabel fr (fld j "callee")) ++ "()"
  | "ChainExpression" => calleeLabel fr (fld j "expression")
  | "ThisExpression" => return "this"
  | t => return t

partial def evalCall (fr : Fr) (j : Json) (isNew : Bool) : M AV := do
  let calleeJ := fld j "callee"
  let site := shallow j
  if typ calleeJ == "Super" then
    let args ← evalArgs fr (items (fld j "arguments"))
    return ← superCall fr args
  if typ calleeJ == "Identifier" && str (fld calleeJ "name") == "eval" && !isNew then
    -- a direct eval reads any name in scope by its spelling: everything this unit can see escapes
    giveUp "direct eval" j
    return AV.top
  let (callee, thisV) ←
    if typ calleeJ == "MemberExpression" && typ (fld calleeJ "object") != "Super" then
      let o ← eval fr (fld calleeJ "object")
      if flag calleeJ "computed" then
        let _ ← eval fr (fld calleeJ "property")
      pure (← getAV o (memberKey calleeJ), o)
    else if typ calleeJ == "ChainExpression" && typ (fld calleeJ "expression") == "MemberExpression" then
      let m := fld calleeJ "expression"
      let o ← eval fr (fld m "object")
      if flag m "computed" then
        let _ ← eval fr (fld m "property")
      pure (← getAV o (memberKey m), o)
    else pure (← eval fr calleeJ, AV.prim)
  let args ← evalArgs fr (items (fld j "arguments"))
  let label ← calleeLabel fr calleeJ
  modify fun s => { s with callLabel := label }
  callAll callee thisV args isNew fr site

/-- Assign `v` to an identifier or a pattern. -/
partial def assignTo (fr : Fr) (t : Json) (v : AV) : M Unit := do
  match typ t with
  | "Identifier" =>
    match ← lookup fr (str (fld t "name")) with
    | some c => writeVar c v
    | none => setWorld ("assignment to the global " ++ str (fld t "name")); escapeAV v
  | "MemberExpression" =>
    let o ← eval fr (fld t "object")
    if flag t "computed" then
      let _ ← eval fr (fld t "property")
    setAV o (memberKey t) v
  | _ => bindPattern fr t v

/-- Bind a pattern (declaration, parameter, assignment target) to a value. -/
partial def bindPattern (fr : Fr) (p : Json) (v : AV) : M Unit := do
  match typ p with
  | "Identifier" | "MemberExpression" => assignTo fr p v
  | "ObjectPattern" =>
    for q in items (fld p "properties") do
      if typ q == "RestElement" then
        let rest ← alloc .plain (some (siteOf fr q))
        joinAny rest (← contentsOf v)
        bindPattern fr (fld q "argument") (AV.ofObj rest)
      else
        let key ← if flag q "computed" then (do let _ ← eval fr (fld q "key"); pure (propKey q)) else pure (propKey q)
        bindPattern fr (fld q "value") (← getAV v key)
  | "ArrayPattern" =>
    let elems ← iterate v
    for e in items (fld p "elements") do
      unless isNull e do
        if typ e == "RestElement" then
          let rest ← alloc .array (some (siteOf fr e))
          joinAny rest elems
          bindPattern fr (fld e "argument") (AV.ofObj rest)
        else bindPattern fr e elems
  | "AssignmentPattern" =>
    let d ← eval fr (fld p "right")
    bindPattern fr (fld p "left") (v.join d)
  | "RestElement" => bindPattern fr (fld p "argument") v
  | _ => giveUp (typ p) p

partial def evalAssign (fr : Fr) (j : Json) : M AV := do
  let op := str (fld j "operator")
  let l := fld j "left"
  let r ← eval fr (fld j "right")
  if op == "=" then
    assignTo fr l r
    return r
  -- compound: read the target, combine, assign
  let cur ← if typ l == "Identifier" || typ l == "MemberExpression" then eval fr l else pure AV.top
  let isLogical := op == "&&=" || op == "||=" || op == "??="
  if !isLogical then convert [cur, r] fr (shallow j)
  let nw := if isLogical then cur.join r else AV.prim
  assignTo fr l nw
  return nw

-- ## Statements

/-- Function declarations are bound when their scope is entered. -/
partial def hoistFns (fr : Fr) (stmts : Array Json) : M Unit := do
  for s in stmts do
    let d := if typ s == "ExportNamedDeclaration" || typ s == "ExportDefaultDeclaration" then fld s "declaration" else s
    if typ d == "FunctionDeclaration" then
      let f ← alloc (.func d fr.scope AV.prim) (some (← codeSite fr d))
      for n in binders (fld d "id") do
        if let some c ← lookup fr n then joinAny c (AV.ofObj f)

/-- Repeat `body` until a pass changes nothing. -/
partial def fixLoop (body : M Unit) : M Unit := do
  let mut n := 0
  repeat
    let c0 := (← get).changes
    body
    n := n + 1
    if (← get).changes == c0 then break
    if n ≥ 6 then
      modify fun s => { s with broke := true }
      break

partial def exec (fr : Fr) (s : Json) : M Unit := do
  if (← isBroke) then return
  tick
  match typ s with
  | "ExpressionStatement" => let _ ← eval fr (fld s "expression")
  | "VariableDeclaration" =>
    -- `using` calls `[Symbol.dispose]` when the scope ends
    if (str (fld s "kind")).endsWith "using" then giveUp "using" s
    for d in items (fld s "declarations") do
      let v ← if isNull (fld d "init") then pure AV.prim else eval fr (fld d "init")
      bindPattern fr (fld d "id") v
  | "FunctionDeclaration" | "EmptyStatement" | "DebuggerStatement" | "BreakStatement" | "ContinueStatement" => pure ()
  | "ClassDeclaration" =>
    let c ← evalClass fr s
    for n in binders (fld s "id") do
      if let some cell ← lookup fr n then writeVar cell c
  | "ReturnStatement" =>
    let v ← if isNull (fld s "argument") then pure AV.prim else eval fr (fld s "argument")
    if let some r := fr.ret then writeVar r v
  | "ThrowStatement" =>
    let v ← eval fr (fld s "argument")
    modify fun st => { st with thrown := st.thrown.join v }
  | "IfStatement" =>
    let _ ← eval fr (fld s "test")
    exec fr (fld s "consequent")
    unless isNull (fld s "alternate") do exec fr (fld s "alternate")
  | "BlockStatement" | "StaticBlock" =>
    let stmts := items (fld s "body")
    let names := lexicalNames stmts
    let fr' ← if names.isEmpty then pure fr else newFrame fr names
    hoistFns fr' stmts
    for t in stmts do exec fr' t
  | "LabeledStatement" => exec fr (fld s "body")
  | "ForStatement" =>
    let init := fld s "init"
    let fr' ← newFrame fr (lexicalNames #[init])
    unless isNull init do
      if typ init == "VariableDeclaration" then exec fr' init else let _ ← eval fr' init
    fixLoop do
      unless isNull (fld s "test") do let _ ← eval fr' (fld s "test")
      exec fr' (fld s "body")
      unless isNull (fld s "update") do let _ ← eval fr' (fld s "update")
  | "ForInStatement" | "ForOfStatement" =>
    let left := fld s "left"
    if typ left == "VariableDeclaration" && (str (fld left "kind")).endsWith "using" then giveUp "using" s
    let fr' ← newFrame fr (lexicalNames #[left])
    let right ← eval fr (fld s "right")
    if typ s == "ForInStatement" then recBuiltinsRead right
    let elems ← if typ s == "ForOfStatement" then iterate right else pure AV.prim
    if flag s "await" then awaitValue elems fr (shallow s)
    fixLoop do
      if typ left == "VariableDeclaration" then
        for d in items (fld left "declarations") do bindPattern fr' (fld d "id") elems
      else bindPattern fr' left elems
      exec fr' (fld s "body")
  | "WhileStatement" | "DoWhileStatement" =>
    fixLoop do
      let _ ← eval fr (fld s "test")
      exec fr (fld s "body")
  | "TryStatement" =>
    exec fr (fld s "block")
    let h := fld s "handler"
    unless isNull h do
      let param := fld h "param"
      let fr' ← newFrame fr (binders param)
      let err ← alloc .plain (some 83)
      joinProto err (AV.ofObj (← builtinObj "Error.prototype"))
      joinProp err "message" AV.prim
      joinProp err "stack" AV.prim
      unless isNull param do bindPattern fr' param ((← get).thrown.join (AV.ofObj err))
      exec fr' (fld h "body")
    unless isNull (fld s "finalizer") do exec fr (fld s "finalizer")
  | "SwitchStatement" =>
    let _ ← eval fr (fld s "discriminant")
    let cases := items (fld s "cases")
    let fr' ← newFrame fr (lexicalNames (cases.flatMap fun c => items (fld c "consequent")))
    for c in cases do
      unless isNull (fld c "test") do let _ ← eval fr' (fld c "test")
      for t in items (fld c "consequent") do exec fr' t
  | "ExportNamedDeclaration" =>
    unless isNull (fld s "declaration") do exec fr (fld s "declaration")
  | "ExportDefaultDeclaration" =>
    let d := fld s "declaration"
    if typ d == "FunctionDeclaration" || typ d == "ClassDeclaration" then exec fr d
    else let _ ← eval fr d
  | t => giveUp t s

end

/-! ## The whole bundle -/

/-- `codeHash` and `codeInfo` for code the run has already seen, without the state to put new answers in. -/
unsafe def codeHashPureImpl (s : St) (j : Json) : UInt64 :=
  match s.codeHashes[ptrAddrUnsafe j]? with
  | some h => h
  | none => hash j

@[implemented_by codeHashPureImpl] opaque codeHashPure (s : St) (j : Json) : UInt64

unsafe def mentionsPureImpl (s : St) (j : Json) : Array String :=
  match s.codeInfos[ptrAddrUnsafe j]? with
  | some (m, _) => m
  | none => mentions j

@[implemented_by mentionsPureImpl] opaque mentionsPure (s : St) (j : Json) : Array String

/-- The objects that hold each object: through contents, prototypes, the cells a function captured, and the
module-scope cells it mentions. -/
def parentsOf (s : St) : Array (Array Nat) := Id.run do
  let n := s.objs.size
  let mut par : Array (Array Nat) := Array.replicate n #[]
  let mut memo : Std.HashMap UInt64 (Array Nat) := {}
  for o in [0:n] do
    let ob := s.objs[o]!
    let mut kids : List Nat := []
    for v in ob.props.values do kids := kids ++ AV.objs v
    kids := kids ++ AV.objs ob.any ++ AV.objs ob.proto
    match ob.kind with
    | .func code scope this =>
      kids := kids ++ AV.objs this
      for fr in scope do kids := kids ++ fr.values
      let h := codeHashPure s code
      let ms ← match memo[h]? with
        | some ms => pure ms
        | none =>
          let ms := (mentionsPure s code).filterMap fun nm => s.mods[nm]?
          memo := memo.insert h ms
          pure ms
      kids := kids ++ ms.toList
    | .klass code scope =>
      for fr in scope do kids := kids ++ fr.values
      let h := codeHashPure s code
      let ms ← match memo[h]? with
        | some ms => pure ms
        | none =>
          let ms := (mentionsPure s code).filterMap fun nm => s.mods[nm]?
          memo := memo.insert h ms
          pure ms
      kids := kids ++ ms.toList
    | .bound target this args =>
      kids := kids ++ AV.objs target ++ AV.objs this ++ (args.flatMap AV.objs)
    | _ => pure ()
    for k in kids do
      unless s.objs[k]!.kind matches .builtin _ do par := par.modify k (·.push o)
  return par

/-- The module-scope names from which something in `starts` can be reached: through what an object holds, what a
prototype holds, and the variables a closure captured. -/
partial def namesReaching (s : St) (par : Array (Array Nat)) (starts : List Nat) : Array String := Id.run do
  let mut seen : Std.HashSet Nat := {}
  let mut work := starts
  let mut out : Array String := #[]
  while !work.isEmpty do
    match work with
    | [] => break
    | o :: rest =>
      work := rest
      if seen.contains o then continue
      seen := seen.insert o
      -- a module-scope variable is read by mentioning it: the search stops there, it does not go on to the functions
      -- that mention the variable
      if let some n := s.objs[o]!.name then out := out.push n
      else work := par[o]!.toList ++ work
  return dedupeArr out

structure UnitEff where
  /-- touches the environment or runs code that is not seen -/
  world  : Bool
  /-- runs code that is not seen -/
  unk    : Bool
  /-- the first reason it touches the world, and the first reason it runs unknown code -/
  why    : String
  whyUnk : String
  /-- module-scope names the run read -/
  reads  : Array String
  /-- the module-scope names whose objects it may have changed -/
  writes : Array String

structure Result where
  units   : Array UnitEff
  /-- names from which an object the world may hold can be reached -/
  escaped : Array String
  /-- vendor entries the verdict relied on -/
  used    : Array String
  /-- node types the interpreter did not understand -/
  unknown : Array String
  /-- units that exhausted the budget -/
  gaveUp  : Nat
  objects : Nat
  /-- a line per escaped object, for debugging -/
  escapedObjs : Array String
  /-- abstract steps each unit took -/
  steps : Array Nat
  /-- the biggest abstract values: (size, object) -/
  biggest : Array (Nat × String)
  dbg : Array Nat
  /-- what the bundle changes in the intrinsics -/
  patches : Array Patch

def analyze (units : Array Json) (imports : Std.HashMap String (String × String)) (prem : Premises)
    (exported : Array String) : Result :=
  let act : M (Array (Bool × Bool × String × String × Array Nat × Array String) × Nat × Array Nat) := do
    modify fun s => { s with imports, prem }
    for u in units do
      for n in topDefs u do
        let c ← alloc .cell none
        modObj c ({ · with name := some n })
        modify fun s => { s with mods := s.mods.insert n c }
    -- what other modules can observe while this one runs is held by the world from the start
    for n in exported do
      if let some c := (← get).mods[n]? then escapeObj c
    -- a module's function declarations exist before any code runs
    for (u, i) in units.toList.zipIdx do
      modify fun s => { s with unit := i }
      hoistFns { path := UInt64.ofNat (i + 1) } #[u]
    let mut out : Array (Bool × Bool × String × String × Array Nat × Array String) := #[]
    let mut steps : Array Nat := #[]
    let mut gaveUp := 0
    for (u, i) in units.toList.zipIdx do
      modify fun s => { s with unit := i, writes := {}, reads := {}, world := false, unk := false, why := "", whyUnk := "", callLabel := "", fuel := budget, broke := false, thrown := #[], cur := { path := UInt64.ofNat (i + 1) } }
      exec { path := UInt64.ofNat (i + 1) } u
      if (← get).broke then
        gaveUp := gaveUp + 1
        modify fun s => { s with broke := false }
        giveUp "budget" u
      let s ← get
      out := out.push (s.world, s.unk, s.why, s.whyUnk, s.writes.toArray, s.reads.toArray)
      steps := steps.push (budget - s.fuel)
    return (out, gaveUp, steps)
  let ((raw, gaveUp, steps), s) := act.run {}
  let par := parentsOf s
  Id.run do
    let mut memo : Std.HashMap Nat (Array String) := {}
    let mut units : Array UnitEff := #[]
    for (w, u, why, whyUnk, ws, rs) in raw do
      let mut names : Array String := #[]
      for o in ws do
        match memo[o]? with
        | some ns => names := names ++ ns
        | none =>
          let ns := namesReaching s par [o]
          memo := memo.insert o ns
          names := names ++ ns
      units := units.push { world := w, unk := u, why, whyUnk, writes := dedupeArr names, reads := rs }
    let escapedObjs := (List.range s.objs.size).filter fun o => s.objs[o]!.escaped
    let describe (o : Nat) : String :=
      let ob := s.objs[o]!
      let k := match ob.kind with
        | .world => "world" | .cell => "cell" | .plain => "plain" | .array => "array" | .coll => "coll"
        | .func .. => "func" | .klass .. => "class" | .bound .. => "bound" | .builtin p => "builtin " ++ p
      s!"#{o} {k}{match ob.name with | some n => " " ++ n | none => ""} any={ob.any.size} props={ob.props.size} keys={(ob.props.toList.take 6).map (·.1)} sizes={(ob.props.toList.take 6).map (·.2.size)}"
    return { units, escaped := namesReaching s par escapedObjs, used := s.usedVendor.toArray, unknown := s.unknown,
             gaveUp, objects := s.objs.size, escapedObjs := (escapedObjs.map describe).toArray, steps,
             dbg := s.dbg, patches := s.patches,
             biggest := ((List.range s.objs.size).toArray.map fun o =>
               let ob := s.objs[o]!
               (ob.any.size + ob.props.fold (fun n _ v => n + v.size) 0, describe o)).qsort (fun a b => a.1 > b.1) |>.extract 0 6 }

/-- What the checker needs from each statement. Reads are the names it mentions (`Lower`) and the names the run read
through functions it called (`deps`); a unit that touches the world also counts what its own closures mention. Writes
come from the run, and a unit that runs code the analysis cannot see writes every name that can reach an object the
world holds, which also orders it against every reader of one.

`exported` are the names other modules can observe while this one evaluates (a cycle): they start out held by the
world. -/
def footprints (units : Array Json) (imports : Std.HashMap String (String × String)) (prem : Premises)
    (exported : Array String := #[]) : Array Footprint × Result :=
  let tops := units.map analyzeTop
  let r := analyze units imports prem exported
  let wide := r.patches.any (!·.tracked)
  let tracked := r.patches.filter (·.tracked)
  let named : Std.HashSet String := tracked.foldl (fun s p => s.insert ("%" ++ p.path ++ "." ++ p.key.getD "*")) {}
  let any : Std.HashSet String := tracked.foldl (fun s p => s.insert ("%" ++ p.path ++ ".*")) {}
  let declared : Std.HashSet String := tops.foldl (fun s t => t.defs.foldl (·.insert ·) s) {}
  -- what an import stands for is held by the world: unknown code may have changed it
  let importNames := imports.toArray.map (·.1)
  let fps := (tops.zip r.units).map fun (t, e) =>
    -- P1 does not hold when the bundle changes an intrinsic: a unit that reads what it changed may read either value
    let patched := !t.hoisted && (wide || e.reads.any fun rd => rd.startsWith "%" && (named.contains rd || any.contains rd))
    let world := e.world || patched
    let eager := if world then (dedupeArr (t.rawEager ++ t.rawLazy)).toList else (dedupeArr t.rawEager).toList
    let seeds : Std.HashSet String := Std.HashSet.ofList eager
    { defs := t.defs, eager, lazy := (dedupeArr t.rawLazy).toList,
      deps := (dedupeArr e.reads).toList.filter (fun n => !n.startsWith "%" && !seeds.contains n),
      hoisted := t.hoisted, effect := world,
      writes := (dedupeArr (if e.unk then e.writes ++ r.escaped ++ importNames else e.writes)).toList,
      assigns := (dedupeArr ((t.assignedEager ++ t.assignedLazy).filter declared.contains)).toList,
      fallback := (dedupeArr t.fallback).toList, rawEager := (dedupeArr t.rawEager).toList,
      rawLazy := (dedupeArr t.rawLazy).toList }
  (fps, r)

/-- `{"vendor": {"from#imported": "family"}, "members": {"family": ["m", …]}, "noWorldHooks": true}`. Every key is
optional (omitting one assumes nothing); an unknown key or a value of the wrong shape is an error. -/
def Premises.ofJson (j : Json) : Except String Premises := do
  let .obj kvs := j | throw "premises: expected a JSON object"
  let mut p : Premises := {}
  for (k, v) in (kvs.foldl (fun acc k v => (k, v) :: acc) []).reverse do
    match k with
    | "vendor" =>
      let .obj entries := v | throw "premises.vendor: expected an object of `from#imported` -> family"
      for (e, f) in (entries.foldl (fun acc k v => (k, v) :: acc) []).reverse do
        let .str fam := f | throw s!"premises.vendor.{e}: expected a family name"
        p := { p with vendor := p.vendor.insert e fam }
    | "members" =>
      let .obj fams := v | throw "premises.members: expected an object of family -> [member, …]"
      for (fam, ms) in (fams.foldl (fun acc k v => (k, v) :: acc) []).reverse do
        let .arr xs := ms | throw s!"premises.members.{fam}: expected an array of member names"
        let mut names : Std.HashSet String := {}
        for x in xs do
          let .str m := x | throw s!"premises.members.{fam}: expected member names"
          names := names.insert m
        p := { p with members := p.members.insert fam names }
    | "noWorldHooks" =>
      let .bool b := v | throw "premises.noWorldHooks: expected a boolean"
      p := { p with noWorldHooks := b }
    | _ => throw s!"premises: unknown key `{k}`"
  return p

/-- The premises file named by `--premises`, or no premises at all. -/
def loadPremises (path? : Option String) : IO Premises := do
  let some path := path? | return {}
  IO.ofExcept (Premises.ofJson (← IO.ofExcept (Json.parse (← IO.FS.readFile path))))

/-- Remove `--name value` from the arguments. -/
def takeFlag (name : String) : List String → Option String × List String
  | a :: v :: rest =>
    if a == name then (some v, rest) else let (r, l) := takeFlag name (v :: rest); (r, a :: l)
  | l => (none, l)

/-- The local names of a bundle's imports, with the module and the name each one imports. -/
def importsOf (bundleJ : Json) : Std.HashMap String (String × String) :=
  (items (fld bundleJ "imports")).foldl
    (fun acc i => acc.insert (str (fld i "local")) (str (fld i "from"), str (fld i "imported"))) {}

/-- What a verdict rests on, one line each: the premises of this analysis and what it did not understand. -/
def premiseLines (prem : Premises) (r : Result) : List String :=
  ["  analysis premises (assumed, not checked):",
   "    P1 nothing the analysis cannot see patches the intrinsics (`Object`, `Array`, `Map`, `Promise`, …)",
   s!"    P3 the world defines no accessors or conversion hooks on program objects, and converting a value it made runs no program code: {prem.noWorldHooks}",
   "    P4 evaluation completes (no uncaught exception, no hang)",
   "    P5 no other chunk runs during evaluation except through callbacks this chunk gave the world",
   s!"    vendor: {r.used.size} imports assumed to only allocate{if r.used.isEmpty then "" else ": " ++ ", ".intercalate r.used.toList}"]
  ++ (if r.patches.isEmpty then [] else
        [s!"  P1 does not hold: the bundle changes an intrinsic ({", ".intercalate (r.patches.toList.take 4 |>.map fun p => s!"unit {p.unit}: {p.what}")}); " ++ (if r.patches.any (!·.tracked) then "every unit that is not a function declaration counts as touching the world" else "the units that read what it changes count as touching the world")])
  ++ (if r.unknown.isEmpty then [] else
        [s!"  node types the analysis did not understand (each made its unit touch the world): {", ".intercalate (dedupeArr r.unknown).toList}"])
  ++ [s!"  {r.objects} abstract objects; {r.gaveUp} units out of budget"]

end Effects
