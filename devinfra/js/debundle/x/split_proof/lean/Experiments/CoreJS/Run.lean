import Experiments.CoreJS.Decode
import Experiments.CoreJS.Eval

/-!
# CoreJS: the `corejs` executable

`corejs programs.jsonl [--fuel N]` runs each line, a program as `to_core.mjs` writes it, and prints one line for it:
`{"out": […], "end": "normal" | "throw" | "unknown" | "fuel" | "malformed", "detail": …, "region": …, "order": […]}`.
`out` has one entry for each `console.log`. `detail` describes a thrown value as `differential.mjs` describes Node's, or
says which unknown code the run reached; `region` is 1 + the identity of the statement it ended in; `order` is the
order in which the modules run. `corejs --host-names` prints the members the model gives the host's prototypes.
-/

open Lean (Json)

namespace CoreJS

/-- A thrown value, as far as a run in Node is compared with: a primitive with its type, an error by its kind. -/
def describe (s : State) : Val → String
  | .undef => "undefined"
  | .null => "null"
  | .bool b => if b then "boolean:true" else "boolean:false"
  | .num n => "number:" ++ Num.toString n
  | .str x => "string:" ++ x
  | .obj a => if isCallable s (.obj a) then "function" else "object"
  | .error .type _ => "TypeError"
  | .error .reference _ => "ReferenceError"
  | .error .syntax _ => "SyntaxError"
  | .host _ => "host"

/-- `s`: the state the run ended in. Its region says where: 0 outside every statement, `i + 1` in the statement
with identity `i`. -/
def report (order : List String) (s : State) (ending detail : String) : String :=
  (Json.mkObj [("out", Json.arr (s.out.map Json.str)), ("end", ending), ("detail", detail), ("region", s.region),
    ("order", Json.arr (order.toArray.map Json.str))]).compress

def runLine (fuel : Nat) (line : String) : String :=
  match Json.parse line >>= Decode.program with
  | .error msg => report [] (initialState 0) "malformed" msg
  | .ok program =>
    let order := program.evalOrder
    match runProgram fuel program with
    | .ok () s => report order s "normal" ""
    | .throw v s => report order s "throw" (describe s v)
    | .unknown why s => report order s "unknown" why
    | .fuel => report order (initialState 0) "fuel" ""

def main (args : List String) : IO UInt32 := do
  match args with
  | ["--host-names"] =>
    IO.println (Json.mkObj [
      ("Object.prototype", Json.arr (("__proto__" :: objectProtoNames).toArray.map Json.str)),
      ("Function.prototype", Json.arr ((["length", "name", "arguments", "caller"] ++ functionProtoNames).toArray.map Json.str)),
      ("Array.prototype", Json.arr (("length" :: arrayProtoNames).toArray.map Json.str))]).compress
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
