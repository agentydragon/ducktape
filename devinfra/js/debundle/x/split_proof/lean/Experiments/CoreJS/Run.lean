import Experiments.CoreJS.Decode
import Experiments.CoreJS.Eval

/-!
# CoreJS: the `corejs` executable

`corejs programs.jsonl [--fuel N]` runs each line, a program as `to_core.mjs` writes it, and prints one line for it:
`{"out": […], "end": "normal" | "throw" | "unknown" | "fuel" | "malformed", "detail": …, "region": …, "order": […],
"world": …, "left": …, "state": …}`. `out` has one entry for each `console.log`. `detail` describes a thrown value as
`differential.mjs` describes Node's, or says which unknown code the run reached; `region` is 1 + the identity of the
statement it ended in; `order` is the order in which the modules run; `state` is a hash of every object and binding
of the final state, with its address.

**The world of a run** is a recording. A program may come with `"world": [call, …]`, the calls of host functions that
a run of the program in Node made, in order, each with how it ended. The model runs in the world that answers exactly
those calls: the next call of the model has to be the next one of the recording, the same function with the same
receiver and arguments, and ends as it did there. `world` in the output says where the model's calls left the
recording, if they did, and `left` how many recorded calls the model did not make.

`corejs --host-names` prints the members the model gives the host's prototypes, and the host functions it asks the
world about.
-/

open Lean (Json)

namespace CoreJS

/-- A call of a host function in a recording, and how it ended. -/
structure Recorded where
  fn : String
  this : Primitive
  args : List Primitive
  result : HostResult

/-- Where a replay stands: the calls that are left, and the first call of the model that was not the recorded one. -/
structure Replay where
  left : List Recorded
  problem : Option String := none

def samePrimitive : Primitive → Primitive → Bool
  | .undef, .undef | .null, .null => true
  | .bool a, .bool b => a == b
  | .num a, .num b => (a.isNaN && b.isNaN) || a.toBits == b.toBits
  | .str a, .str b => a == b
  | _, _ => false

def samePrimitives : List Primitive → List Primitive → Bool
  | [], [] => true
  | a :: as, b :: bs => samePrimitive a b && samePrimitives as bs
  | _, _ => false

def showPrimitive : Primitive → String
  | .undef => "undefined"
  | .null => "null"
  | .bool b => toString b
  | .num n => Num.toString n
  | .str s => (Json.str s).compress

def showCall (fn : String) (this : Primitive) (args : List Primitive) : String :=
  s!"{fn} on {showPrimitive this} with ({", ".intercalate (args.map showPrimitive)})"

/-- The world a recording describes. A call that is not the next recorded one stops the replay and throws. -/
def replay (calls : List Recorded) : World where
  σ := Replay
  init := { left := calls }
  step fn this args st :=
    match st.left with
    | r :: rest =>
      if r.fn == fn && samePrimitive r.this this && samePrimitives r.args args then (r.result, { st with left := rest })
      else (.throw (.str "world: not the recorded call"),
        { left := [], problem := st.problem.or
            (some s!"the model calls {showCall fn this args}, Node called {showCall r.fn r.this r.args}") })
    | [] => (.throw (.str "world: not the recorded call"),
        { left := [], problem := st.problem.or (some s!"the model calls {showCall fn this args} after Node's last call") })

namespace Decode

/-- A primitive of a recording: `["u"]`, `["n"]`, `["b", true]`, `["d", bits]`, `["s", text]`, or `["x"]` for a string
that is not in the model (one with a character outside the Basic Multilingual Plane, or half of one). -/
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
  | [.str "x"] => pure (.str (String.singleton (Char.ofNat 0x10000)))
  | _ => throw "a primitive was expected"

def errorKind : String → Except String ErrorKind
  | "TypeError" => pure .type
  | "ReferenceError" => pure .reference
  | "SyntaxError" => pure .syntax
  | "RangeError" => pure .range
  | "URIError" => pure .uri
  | name => throw s!"an error the model has no kind for: {name}"

/-- `{"fn", "this", "args", and one of "ret": primitive, "throw": primitive, "error": the name of the error's class}`. -/
def recorded (j : Json) : Except String Recorded := do
  let fn ← j.getObjValAs? String "fn"
  let this ← j.getObjVal? "this" >>= primitive
  let args ← (← j.getObjVal? "args" >>= Json.getArr?).toList.mapM primitive
  let result ← match j.getObjVal? "ret", j.getObjVal? "throw", j.getObjVal? "error" with
    | .ok v, _, _ => .ret <$> primitive v
    | _, .ok v, _ => .throw <$> primitive v
    | _, _, .ok (.str name) => (.error · "") <$> errorKind name
    | _, _, _ => throw s!"the recorded call of {fn} has no ending"
  pure { fn, this, args, result }

/-- The recording that comes with a program, if one does. -/
def world (j : Json) : Except String (List Recorded) :=
  match j.getObjVal? "world" with
  | .ok calls => do (← calls.getArr?).toList.mapM recorded
  | .error _ => pure []

end Decode

/-- A thrown value, as far as a run in Node is compared with: a primitive with its type, an error by its kind. -/
def describe {W : World} (s : State W) : Val → String
  | .undef => "undefined"
  | .null => "null"
  | .bool b => if b then "boolean:true" else "boolean:false"
  | .num n => "number:" ++ Num.toString n
  | .str x => "string:" ++ x
  | .obj a => if isCallable s (.obj a) then "function" else "object"
  | .error .type _ => "TypeError"
  | .error .reference _ => "ReferenceError"
  | .error .syntax _ => "SyntaxError"
  | .error .range _ => "RangeError"
  | .error .uri _ => "URIError"
  | .host _ => "host"

/-- A value as text, an object by its address. -/
def showVal : Val → String
  | .undef => "undefined"
  | .null => "null"
  | .bool b => toString b
  | .num n => s!"#{n.toBits}"
  | .str x => (Json.str x).compress
  | .obj a => s!"@{a.region}.{a.index}"
  | .error kind msg => s!"error {reprStr kind} {msg}"
  | .host name => s!"host {name}"

def showAddr (a : Addr) : String := s!"@{a.region}.{a.index}"

def showProperty : Property → String
  | .data v w e c => s!"data {showVal v} {w} {e} {c}"
  | .accessor g st e c => s!"accessor {showVal g} {showVal st} {e} {c}"

def showCode : Code → String
  | .closure f env fields =>
    s!"closure {f.name} {f.params} {env.vars.map fun (x, a) => (x, showAddr a)} {showVal env.this} {fields.map (·.1)}"
  | .host name => s!"host {name}"

def showObj (o : Obj) : String :=
  s!"{o.proto.map showAddr} {o.props.map fun (k, p) => (k, showProperty p)} {o.code.map showCode} {o.array}"

def showBinding (b : Binding) : String := s!"{b.value.map showVal} {b.mutable}"

/-- Every object and every binding of a state as text, each at its address. Two states with the same text have the
same heap and the same bindings, up to the code of their functions, of which only the name, the parameters and the
kept bindings are written. -/
def showState {W : World} (s : State W) : String :=
  s!"{s.heap.map (·.map showObj)} {s.cells.map (·.map showBinding)}"

/-- `s`: the state the run ended in. Its region says where: 0 outside every statement, `i + 1` in the statement
with identity `i`. `state` is a hash of `showState`. -/
def report {calls : List Recorded} (order : List String) (s : State (replay calls)) (ending detail : String) : String :=
  (Json.mkObj [("out", Json.arr (s.out.map Json.str)), ("end", ending), ("detail", detail), ("region", s.region),
    ("order", Json.arr (order.toArray.map Json.str)), ("world", s.world.problem.getD ""),
    ("left", s.world.left.length), ("state", toString (hash (showState s)))]).compress

def runLine (fuel : Nat) (line : String) : String :=
  match Json.parse line >>= fun j => do pure (← Decode.program j, ← Decode.world j) with
  | .error msg => report [] (initialState (replay []) 0) "malformed" msg
  | .ok (program, calls) =>
    let order := program.evalOrder
    match runProgram (replay calls) fuel program with
    | .ok () s => report order s "normal" ""
    | .throw v s => report order s "throw" (describe s v)
    | .unknown why s => report order s "unknown" why
    | .fuel => report order (initialState (replay calls) 0) "fuel" ""

def strings (names : List String) : Json := Json.arr (names.toArray.map Json.str)

def main (args : List String) : IO UInt32 := do
  match args with
  | ["--host-names"] =>
    IO.println (Json.mkObj [
      ("prototypes", Json.mkObj [
        ("Object.prototype", strings ("__proto__" :: objectProtoNames)),
        ("Function.prototype", strings (["length", "name", "arguments", "caller"] ++ functionProtoNames)),
        ("Array.prototype", strings ("length" :: arrayProtoNames)),
        ("String.prototype", strings stringProtoNames),
        ("Number.prototype", strings numberProtoNames),
        ("Boolean.prototype", strings booleanProtoNames)]),
      ("methods", strings (qualified worldMethodNames)),
      ("functions", strings (hostFunctions ++ qualified hostNamespaces)),
      ("globals", strings (hostFunctions ++ hostNamespaces.map (·.1)).eraseDups)]).compress
    pure 0
  | [file] | [file, "--fuel", _] =>
    let fuel := (args[2]?.bind String.toNat?).getD 100000
    for line in (← IO.FS.lines file) do
      IO.println (runLine fuel line)
    pure 0
  | _ =>
    IO.eprintln "usage: corejs programs.jsonl [--fuel N] | corejs --host-names"
    pure 2

end CoreJS
