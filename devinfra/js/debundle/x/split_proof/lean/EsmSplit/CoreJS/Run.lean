import EsmSplit.ESTreeUnits
import EsmSplit.CoreJS.FromESTree
import EsmSplit.CoreJS.Decode
import EsmSplit.CoreJS.Eval
import EsmSplit.CoreJS.Fragment

/-!
# CoreJS: the `corejs` executable

`corejs programs.jsonl [--fuel N]` runs each line, a program as `to_core.mjs` writes it, and prints one line for it:
`{"out": […], "end": "normal" | "throw" | "unknown" | "fuel" | "malformed", "detail": …, "region": …, "order": […],
"world": …, "left": …, "state": …}`. `out` has one entry for each `console.log`. `detail` describes a thrown value as
`differential.mjs` describes Node's, or says which unknown code the run reached; `region` is 1 + the identity of the
statement it ended in; `order` is the order in which the modules run; `state` is a hash of every object and binding
of the final state, with its address.

`corejs --ast --host-contract PROFILE.json dumps.jsonl` runs the same interpreter on annotated ESTree dumps under
the supplied host profile. Each line is an entry module,
optionally with `"entry"`, its filename (default `main`), and `"providers"`: its relative dependencies by
resolved filename, which may import the entry back. The entry point in
`CoreJSMain.lean` lowers them through `LinkedInput.program`, the lowering that certification uses.
An unsupported AST produces a `malformed` result with its reason.

**The world of a run** is a recording. A program may come with `"world": [call, …]`, the calls of host functions that
a run of the program in Node made, in order, each with how it ended. The model runs in the world that answers exactly
those calls: the next call of the model has to be the next one of the recording, the same function with the same
receiver and arguments, and ends as it did there. `world` in the output says where the model's calls left the
recording, if they did, and `left` how many recorded calls the model did not make. A recording with
`"iteratorSymbol": true` supplies the stable, effect-free observation of the ambient `Symbol.iterator`.
`"symbolConstructor": true` supplies the stable, effect-free observation of the ambient `Symbol` intrinsic.

`corejs --host-names` prints the modeled prototype members and world calls used by the differential harness.
`corejs --footprints` checks footprints against a program (`checkFootprints`), and `corejs --moves` counts the
statements whose footprint is proved (`moveCensus`).
-/

open Lean (Json)

namespace CoreJS

/-- A call of a host function in a recording, and how it ended. A receiver or argument of `none`
records an ill-formed UTF-16 string, which cannot match a model value. -/
structure Recorded where
  fn : String
  this : Option Primitive
  args : List (Option Primitive)
  result : HostResult

/-- Requests for one part of opaque code, with Node expectations for its own-key responses. -/
structure Script where
  steps : List Request
  expectedKeys : List (List String) := []

/-- A running script and its registers. Register 0 is the callee, 1 is `this`, then the arguments, then each answer. -/
structure Conversation extends Script where
  regs : Array WVal

/-- The scripted part of opaque code: `(code, part)` and its requests, in which `WVal.handle k` stands for register
`k`. -/
abbrev Scripts := List ((Nat × Nat) × Script)

/-- Where a replay stands: the calls that are left, the first call of the model that was not the recorded one, and
the conversations about opaque code that are under way, innermost first. -/
structure Replay where
  left : List Recorded
  problem : Option String := none
  scripts : Scripts := []
  talks : List Conversation := []

/-- A request of a script with its registers filled in. -/
def Request.fill (regs : Array WVal) (r : Request) : Request :=
  let v : WVal → WVal := fun | .handle k => regs.getD k (.prim .undef) | w => w
  match r with
  | .done x => .done (v x)
  | .throw x => .throw (v x)
  | .unknown why => .unknown why
  | .readVar i => .readVar i
  | .writeVar i x => .writeVar i (v x)
  | .initVar i x => .initVar i (v x)
  | .get o k => .get (v o) (v k)
  | .set o k x => .set (v o) (v k) (v x)
  | .delete o k => .delete (v o) (v k)
  | .has k o => .has (v k) (v o)
  | .keys o => .keys (v o)
  | .call f this args => .call (v f) (v this) (args.map v)
  | .construct f args => .construct (v f) (args.map v)
  | .newObject proto => .newObject (proto.map v)
  | .newFunction part name length constructor => .newFunction part name length constructor
  | .prim p args => .prim p (args.map v)

/-- The next request of the innermost conversation. One that ends the code ends the conversation. -/
def Replay.next (st : Replay) : Request × Replay :=
  match st.talks with
  | [] => (.unknown "world: no conversation is under way", st)
  | c :: rest =>
    match c.steps with
    | [] =>
      if c.expectedKeys.isEmpty then (.done (.prim .undef), { st with talks := rest })
      else (.unknown "model: script ended before its expected own keys", st)
    | r :: more =>
      match r.fill c.regs with
      | .done x =>
        if c.expectedKeys.isEmpty then (.done x, { st with talks := rest })
        else (.unknown "model: script ended before its expected own keys", st)
      | .throw x => (.throw x, { st with talks := rest })
      | .unknown why => (.unknown why, { st with talks := rest })
      | r => (r, { st with talks := { c with steps := more } :: rest })

/-- What the innermost conversation is told. A throw ends it with the same throw: a script catches nothing. -/
def Replay.told (told : Told) (st : Replay) : Request × Replay :=
  match told, st.talks with
  | .run code part callee this args _, talks =>
    match st.scripts.lookup (code, part) with
    | none => (.unknown s!"world: no script for part {part} of opaque code {code}", st)
    | some script => Replay.next { st with talks := { toScript := script, regs := #[callee, this] ++ args.toArray } :: talks }
  | .value v, c :: rest => Replay.next { st with talks := { c with regs := c.regs.push v } :: rest }
  | .keys keys, c :: rest =>
    match c.expectedKeys with
    | expected :: more =>
      if keys == expected then
        Replay.next { st with talks := { c with expectedKeys := more, regs := c.regs.push (.prim .undef) } :: rest }
      else (.unknown s!"model: own keys {repr keys}, expected {repr expected}", st)
    | [] => (.unknown "model: own keys have no Node expectation", st)
  | .threw v, _ :: rest => (.throw v, { st with talks := rest })
  | _, [] => (.unknown "world: no conversation is under way", st)

def samePrimitive : Primitive → Primitive → Bool
  | .undef, .undef | .null, .null => true
  | .bool a, .bool b => a == b
  | .num a, .num b => (a.isNaN && b.isNaN) || a.toBits == b.toBits
  | .str a, .str b => a == b
  | _, _ => false

def samePrimitives : List (Option Primitive) → List Primitive → Bool
  | [], [] => true
  | some a :: as, b :: bs => samePrimitive a b && samePrimitives as bs
  | _, _ => false

def showPrimitive : Primitive → String
  | .undef => "undefined"
  | .null => "null"
  | .bool b => toString b
  | .num n => Num.toString n
  | .str s => (Json.str s).compress

def showCall (fn : String) (this : Option Primitive) (args : List (Option Primitive)) : String :=
  let showArg := fun v : Option Primitive => v.elim "<ill-formed UTF-16 string>" showPrimitive
  s!"{fn} on {showArg this} with ({", ".intercalate (args.map showArg)})"

/-- The world a recording describes. A call that is not the next recorded one stops the replay and throws. -/
def replay (calls : List Recorded) (scripts : Scripts := []) (refs : List (HostRef × String) := [])
    (profile : HostProfile := .empty) : World where
  σ := Replay
  init := { left := calls, scripts }
  talk := Replay.told
  globalTypeof name st := do
    let value ← profile.bindingType name
    return (.ret (.str value), st)
  hostRef ref st := (refs.lookup ref).map fun identity => (.identity identity, st)
  step fn this args st :=
    let stop (why : String) : HostResult × Replay :=
      (.throw (.str "world: not the recorded call"), { st with left := [], problem := st.problem.or (some why) })
    match st.left with
    | r :: rest =>
      if r.fn == fn && r.this.any (samePrimitive · this) && samePrimitives r.args args then
        (r.result, { st with left := rest })
      else stop s!"the model calls {showCall fn (some this) (args.map some)}, Node called {showCall r.fn r.this r.args}"
    | [] => stop s!"the model calls {showCall fn (some this) (args.map some)} after Node's last call"

namespace Decode

/-- A representable primitive: `["u"]`, `["n"]`, `["b", true]`, `["d", bits]` or `["s", text]`
with well-formed Unicode text. -/
def primitive (j : Json) : Except String Primitive := do
  match (← j.getArr?).toList with
  | [.str "u"] => pure .undef
  | [.str "n"] => pure .null
  | [.str "b", b] => .bool <$> b.getBool?
  | [.str "d", .str hex] =>
    match Num.natOf 16 hex.toList with
    | some bits => pure (.num (Float.ofBits bits.toUInt64))
    | none => throw s!"not the bits of a number: {hex}"
  | [.str "s", .str s] => pure (.str s)
  | _ => throw "a primitive was expected"

/-- The recorder writes an isolated surrogate as `["x"]` before JSON encoding. -/
def recordedPrimitive (j : Json) : Except String (Option Primitive) :=
  match j with
  | .arr #[.str "x"] => pure none
  | _ => some <$> primitive j

def errorKind : String → Except String ErrorKind
  | "TypeError" => pure .type
  | "ReferenceError" => pure .reference
  | "SyntaxError" => pure .syntax
  | "RangeError" => pure .range
  | "URIError" => pure .uri
  | name => throw s!"an error the model has no kind for: {name}"

/-- A call records a return, throw, engine error, or an unrepresentable host result. -/
def recorded (j : Json) : Except String Recorded := do
  let fn ← j.getObjValAs? String "fn"
  let this ← j.getObjVal? "this" >>= recordedPrimitive
  let args ← (← j.getObjVal? "args" >>= Json.getArr?).toList.mapM recordedPrimitive
  let result ← match j.getObjVal? "ret", j.getObjVal? "throw", j.getObjVal? "error", j.getObjVal? "unknown" with
    | .ok v, _, _, _ => do
      match ← recordedPrimitive v with
      | some p => pure (.ret p)
      | none => pure (.unknown "the host returns a string containing an isolated UTF-16 surrogate")
    | _, .ok v, _, _ => do
      match ← recordedPrimitive v with
      | some p => pure (.throw p)
      | none => pure (.unknown "the host throws a string containing an isolated UTF-16 surrogate")
    | _, _, .ok (.str name), _ => (.error · "") <$> errorKind name
    | _, _, _, .ok (.str why) => pure (.unknown why)
    | _, _, _, _ => throw s!"the recorded call of {fn} has no ending"
  pure { fn, this, args, result }

/-- A value in a script: a primitive as in a recording, or `["r", k]` for register `k`. -/
def wval (j : Json) : Except String WVal :=
  match j.getArr? with
  | .ok #[.str "r", k] => do pure (.handle (← k.getNat?))
  | _ => do
    match ← recordedPrimitive j with
    | some p => pure (.prim p)
    | none => pure .illFormedString

def primName : String → Except String Prim
  | "log" => pure .log
  | "defineProperty" => pure .defineProperty
  | "create" => pure .create
  | "getPrototypeOf" => pure .getPrototypeOf
  | "setPrototypeOf" => pure .setPrototypeOf
  | p => throw s!"not a standard function of the model: {p}"

/-- A request of a script: `[op, operand…]`. -/
def request (j : Json) : Except String Request := do
  match (← j.getArr?).toList with
  | [.str "done", x] => .done <$> wval x
  | [.str "throw", x] => .throw <$> wval x
  | [.str "unknown", .str why] => pure (.unknown why)
  | [.str "readVar", i] => .readVar <$> i.getNat?
  | [.str "writeVar", i, x] => do pure (.writeVar (← i.getNat?) (← wval x))
  | [.str "initVar", i, x] => do pure (.initVar (← i.getNat?) (← wval x))
  | [.str "get", o, k] => do pure (.get (← wval o) (← wval k))
  | [.str "set", o, k, x] => do pure (.set (← wval o) (← wval k) (← wval x))
  | [.str "delete", o, k] => do pure (.delete (← wval o) (← wval k))
  | [.str "has", k, o] => do pure (.has (← wval k) (← wval o))
  | [.str "keys", o] => .keys <$> wval o
  | .str "call" :: f :: this :: args => do pure (.call (← wval f) (← wval this) (← args.mapM wval))
  | .str "construct" :: f :: args => do pure (.construct (← wval f) (← args.mapM wval))
  | [.str "newObject"] => pure (.newObject none)
  | [.str "newObject", p] => do pure (.newObject (some (← wval p)))
  | [.str "newFunction", part, .str name, length, .bool constructor] => do
    pure (.newFunction (← part.getNat?) name (← length.getNat?) constructor)
  | .str "prim" :: .str p :: args => do pure (.prim (← primName p) (← args.mapM wval))
  | _ => throw s!"not a request of a script: {j.compress}"

/-- `"scripts": [{"code", "part", "steps": [request, …]}, …]`: what the world does for opaque code. -/
def scripts (j : Json) : Except String Scripts :=
  match j.getObjVal? "scripts" with
  | .ok all => do
    (← all.getArr?).toList.mapM fun s => do
      let expectedKeys ← match s.getObjVal? "expectedKeys" with
        | .ok j => (← j.getArr?).toList.mapM fun keys => do (← keys.getArr?).toList.mapM Json.getStr?
        | .error _ => pure []
      pure ((← s.getObjValAs? Nat "code", ← s.getObjValAs? Nat "part"),
        { steps := ← (← s.getObjVal? "steps" >>= Json.getArr?).toList.mapM request, expectedKeys })
  | .error _ => pure []

/-- The recording that comes with a program, if one does. -/
def world (j : Json) : Except String (List Recorded) :=
  match j.getObjVal? "world" with
  | .ok calls => do (← calls.getArr?).toList.mapM recorded
  | .error _ => pure []

def hostRefs (j : Json) : Except String (List (HostRef × String)) := do
  let observed key := match j.getObjVal? key with
    | .ok value => value.getBool?
    | .error _ => pure false
  let iteratorSymbol ← observed "iteratorSymbol"
  let hasInstance ← observed "hasInstanceSymbol"
  let tag ← observed "toStringTagSymbol"
  let symbolConstructor ← observed "symbolConstructor"
  return (if iteratorSymbol then [(.property "Symbol" "iterator", "Symbol.iterator")] else []) ++
    (if hasInstance then [(.property "Symbol" "hasInstance", "Symbol.hasInstance")] else []) ++
    (if tag then [(.property "Symbol" "toStringTag", "Symbol.toStringTag")] else []) ++
    (if symbolConstructor then [(.binding "Symbol", "intrinsic.Symbol")] else [])

end Decode

/-- Describe a thrown value for the Node comparison. Fresh symbols are identified by type here;
their identity is observable inside the program. -/
def describe {W : World} (s : State W) : Val → String
  | .undef => "undefined"
  | .null => "null"
  | .bool b => if b then "boolean:true" else "boolean:false"
  | .num n => "number:" ++ Num.toString n
  | .str x => "string:" ++ x
  | .wellKnownSymbol sym => s!"symbol:Symbol({sym.description})"
  | .symbol _ => "symbol"
  | .obj a => if isCallable s (.obj a) then "function" else "object"
  | .error .type _ => "TypeError"
  | .error .reference _ => "ReferenceError"
  | .error .syntax _ => "SyntaxError"
  | .error .range _ => "RangeError"
  | .error .uri _ => "URIError"
  | .host _ => "host"

private def showSymbol (a : Addr) : String := s!"symbol@{a.region}.{a.index}"

/-- A value as text, an object or a fresh symbol by its address. -/
def showVal : Val → String
  | .undef => "undefined"
  | .null => "null"
  | .bool b => toString b
  | .num n => s!"#{n.toBits}"
  | .str x => (Json.str x).compress
  | .wellKnownSymbol sym => sym.description
  | .symbol a => showSymbol a
  | .obj a => s!"@{a.region}.{a.index}"
  | .error kind msg => s!"error {reprStr kind} {msg}"
  | .host name => s!"host {name}"

private def showKey : PropertyKey → String
  | .str k => (Json.str k).compress
  | .wellKnownSymbol sym => sym.description
  | .symbol a => showSymbol a

def showAddr (a : Addr) : String := s!"@{a.region}.{a.index}"

def showProperty : Property → String
  | .data v w e c => s!"data {showVal v} {w} {e} {c}"
  | .accessor g st e c => s!"accessor {showVal g} {showVal st} {e} {c}"

def showCode : Code → String
  | .derived => "derived"
  | .derivedFields env fields =>
    s!"derived fields {env.vars.map fun (x, a) => (x, showAddr a)} {fields.map fun (key, _) => showKey key}"
  | .derivedBody before args f env fields =>
    let argumentShape := match args with
      | .forward => "forward"
      | .explicit es => toString es.length
      | .conditional _ yes no => s!"if({yes.length},{no.length})"
    let base := s!"derived {f.name} {f.params} {before.length} {argumentShape} {env.vars.map fun (x, a) => (x, showAddr a)}"
    if fields.isEmpty then base else s!"{base} {fields.map fun (key, _) => showKey key}"
  | .closure f env fields =>
    s!"closure {f.name} {f.params} {env.vars.map fun (x, a) => (x, showAddr a)} {showVal env.this} {fields.map fun (k, _) => showKey k}"
  | .host name => s!"host {name}"
  | .opaque code part cells ctor => s!"opaque {code} {part} {cells.map (·.map showAddr)} {ctor}"

def showObj (o : Obj) : String :=
  s!"{o.proto.map showAddr} {o.props.map fun (k, p) => (showKey k, showProperty p)} {o.code.map showCode} {o.array} {repr o.regexp}"

def showBinding (b : Binding) : String := s!"{b.value.map showVal} {b.mutable}"

/-- Every object and every binding of a state as text, each at its address. Two states with the same text have the
same heap and the same bindings, up to the code of their functions, of which only the name, the parameters and the
kept bindings are written. -/
def showState {W : World} (s : State W) : String :=
  s!"{s.heap.map (·.map showObj)} {s.cells.map (·.map showBinding)}"

/-- `s`: the state the run ended in. Its region says where: 0 outside every statement, `i + 1` in the statement
with identity `i`. `state` is a hash of `showState`. -/
def report {calls : List Recorded} {scripts : Scripts} {refs : List (HostRef × String)}
    (profile : HostProfile) (order : List String) (s : State (replay calls scripts refs profile))
    (ending detail : String) : String :=
  (Json.mkObj [("out", Json.arr (s.out.map Json.str)), ("end", ending), ("detail", detail), ("region", s.region),
    ("order", Json.arr (order.toArray.map Json.str)), ("world", s.world.state.problem.getD ""),
    ("left", s.world.state.left.length), ("handed", s.world.handed.size),
    ("state", toString (hash (showState s)))]).compress

def runLineWithProfile (fuel : Nat) (line : String) (profile : HostProfile)
    (decode : Json → Scripts → Except String Program := fun j _ => Decode.program j) : String :=
  match Json.parse line >>= fun j => do
    let scripts ← Decode.scripts j
    pure (← decode j scripts, ← Decode.world j, scripts, ← Decode.hostRefs j) with
  | .error msg => report profile [] (initialState (replay [] [] [] profile) 0) "malformed" msg
  | .ok (program, calls, scripts, refs) =>
    let order := program.evalOrder
    let recorded := replay calls scripts refs profile
    match runProgram recorded fuel program with
    | .ok () s => report profile order s "normal" ""
    | .throw v s => report profile order s "throw" (describe s v)
    | .unknown why s => report profile order s "unknown" why
    | .fuel => report profile order (initialState recorded 0) "fuel" ""

def runLine (fuel : Nat) (line : String)
    (decode : Json → Scripts → Except String Program := fun j _ => Decode.program j) : String :=
  runLineWithProfile fuel line .empty decode

def strings (names : List String) : Json := Json.arr (names.toArray.map Json.str)

/-- **`FootprintsOfSyntax`, checked** (`Split.lean`): for the statement with identity `i`, the footprint on line `i`
names as `defs` what the statement declares (`Stmt.bindings`), and every name the statement mentions
(`Stmt.free`) is among its `defs`, `eager` or `lazy`. A line without a statement (an `export { … }`, which the model
keeps as the module's exports) has to declare nothing. One line of JSON: the counts, and the first few failures. -/
def checkFootprints (program : Program) (lines : List String) : Except String String := do
  let body := program.modules.flatMap (·.body)
  let names (j : Json) (k : String) : Except String (List String) := do
    (← j.getObjVal? k >>= Json.getArr?).toList.mapM Json.getStr?
  let mut checked := 0
  let mut opaques : Nat := 0
  let mut problems : Array String := #[]
  let mut failed := 0
  for (line, i) in lines.zipIdx do
    let fp ← Json.parse line
    let defs ← names fp "defs"
    let known := defs ++ (← names fp "eager") ++ (← names fp "lazy")
    let found : Option String := match body.lookup i with
      | none => if defs.isEmpty then none else some s!"declares {defs} and has no statement in the model"
      | some s =>
        let declared := s.bindings.map (·.1)
        if !(defs.all declared.contains && declared.all defs.contains) then
          some s!"the footprint declares {defs}, the statement {declared}"
        else match s.free.filter (!known.contains ·) with
          | [] => none
          | missing => some s!"mentions {missing.eraseDups}, which the footprint does not list"
    checked := checked + 1
    if let some (.opaque ..) := body.lookup i then opaques := opaques + 1
    if let some why := found then
      failed := failed + 1
      if problems.size < 20 then problems := problems.push s!"{i}: {why}"
  if body.any (·.1 ≥ lines.length) then throw "a statement has no footprint"
  pure (Json.mkObj [("checked", checked), ("opaque", opaques), ("failed", failed),
    ("problems", Json.arr (problems.map Json.str))]).compress

/-- **Which statements have a proved footprint** (`Fragment.lean`): the statements that do nothing when they run, and
the others that only allocate: declarations whose initializers only allocate, opaque statements that only make
functions. With the footprints of `lower` (one line per identity), it also counts how the analysis
describes those statements: as an effect or as writing a name (more than is proved: imprecise, not wrong), and
whether it lists every name the statement reads among the names it reads while it runs or declares (if not, a
finding). One line of JSON. -/
def moveCensus (program : Program) (lines : Option (List String)) : Except String String := do
  let body := program.modules.flatMap (·.body)
  let names (j : Json) (k : String) : Except String (List String) := do
    (← j.getObjVal? k >>= Json.getArr?).toList.mapM Json.getStr?
  let fps ← (lines.getD []).mapM Json.parse
  let mut opaques := 0
  let mut inert : Nat := 0
  let mut inertEffect : Nat := 0
  let mut allocs : Nat := 0
  let mut effect : Nat := 0
  let mut writes : Nat := 0
  let mut findings : Array String := #[]
  for (i, s) in body do
    if let .opaque .. := s then opaques := opaques + 1
    if s.inert then
      inert := inert + 1
      if let some fp := fps[i]? then
        if (fp.getObjValAs? Bool "effect").toOption == some true then inertEffect := inertEffect + 1
    else if let some (_, reads) := s.allocs then
      allocs := allocs + 1
      if let some fp := fps[i]? then
        if (fp.getObjValAs? Bool "effect").toOption == some true then effect := effect + 1
        if !(← names fp "writes").isEmpty then writes := writes + 1
        let known := (← names fp "defs") ++ (← names fp "eager")
        match reads.filter (!known.contains ·) with
        | [] => pure ()
        | missing => findings := findings.push s!"{i}: reads {missing.eraseDups}, which the footprint does not list"
  let counts : List (String × Json) := [("units", (body.length : Nat)), ("opaque", (opaques : Nat)),
    ("inert", (inert : Nat)), ("allocate", (allocs : Nat))]
  let analysis : List (String × Json) := [("inert_effect", (inertEffect : Nat)), ("allocate_effect", (effect : Nat)),
    ("allocate_writes", (writes : Nat)),
    ("findings", Json.arr (findings.map Json.str))]
  pure (Json.mkObj (counts ++ if lines.isSome then analysis else [])).compress

def main (args : List String) : IO UInt32 := do
  match args with
  | ["--host-names"] =>
    let ownStringNames (o : Obj) := strings (o.props.filterMap fun (k, _) => k.string?)
    IO.println (Json.mkObj [
      ("prototypes", Json.mkObj [
        ("Object.prototype", strings ("__proto__" :: objectProtoNames)),
        ("Function.prototype", strings (["length", "name", "arguments", "caller"] ++ functionProtoNames)),
        ("Array.prototype", strings ("length" :: arrayProtoNames)),
        ("ArrayIterator.prototype", ownStringNames arrayIteratorPrototype),
        ("Iterator.prototype", ownStringNames iteratorPrototype),
        ("Generator.prototype", ownStringNames generatorPrototype),
        ("AsyncIterator.prototype", ownStringNames asyncIteratorPrototype),
        ("AsyncGenerator.prototype", ownStringNames asyncGeneratorPrototype),
        ("AsyncGeneratorFunction.prototype", ownStringNames asyncGeneratorFunctionPrototype),
        ("GeneratorFunction.prototype", ownStringNames generatorFunctionPrototype),
        ("RegExp.prototype", ownStringNames regexpPrototype),
        ("AsyncFunction.prototype", ownStringNames asyncFunctionPrototype),
        ("String.prototype", strings stringProtoNames),
        ("Number.prototype", strings numberProtoNames),
        ("Boolean.prototype", strings booleanProtoNames)]),
      ("methods", strings (qualified worldMethodNames)),
      ("functions", strings (hostFunctions ++ qualified hostNamespaces))]).compress
    pure 0
  | ["--footprints", programFile, footprintsFile] =>
    let lines := ((← IO.FS.readFile footprintsFile).splitOn "\n").filter (· != "")
    match Json.parse (← IO.FS.readFile programFile) >>= Decode.program >>= (checkFootprints · lines) with
    | .ok report => IO.println report; pure 0
    | .error msg => IO.eprintln msg; pure 1
  | "--moves" :: programFile :: rest =>
    let lines ← match rest with
      | [] => pure none
      | [footprintsFile] => pure (some (((← IO.FS.readFile footprintsFile).splitOn "\n").filter (· != "")))
      | _ => IO.eprintln "usage: corejs --moves program.json [footprints.jsonl]"; return 2
    match Json.parse (← IO.FS.readFile programFile) >>= Decode.program >>= (moveCensus · lines) with
    | .ok report => IO.println report; pure 0
    | .error msg => IO.eprintln msg; pure 1
  | [file] | [file, "--fuel", _] =>
    let fuel := (args[2]?.bind String.toNat?).getD 100000
    for line in (← IO.FS.lines file) do
      IO.println (runLine fuel line)
    pure 0
  | _ =>
    IO.eprintln "usage: corejs programs.jsonl [--fuel N] | corejs --ast --host-contract PROFILE.json dumps.jsonl | corejs --host-names | corejs --footprints program.json footprints.jsonl | corejs --moves program.json [footprints.jsonl]"
    pure 2

end CoreJS
