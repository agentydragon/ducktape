import Experiments.CoreJS.Decode
import Experiments.CoreJS.Eval

/-!
# CoreJS: the `corejs` executable

`corejs programs.jsonl [--fuel N]` runs each line, a program as `to_core.mjs` writes it, and prints one line for it:
`{"out": […], "end": "normal" | "throw" | "unknown" | "fuel" | "malformed", "detail": …}`. `out` has one entry for
each `console.log`. `detail` describes a thrown value as `differential.mjs` describes Node's, or says which unknown
code the run reached. `corejs --host-names` prints the members the model gives the host's prototypes.
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
  | .host _ => "host"

def report (out : Array String) (ending detail : String) : String :=
  (Json.mkObj [("out", Json.arr (out.map Json.str)), ("end", ending), ("detail", detail)]).compress

def runLine (fuel : Nat) (line : String) : String :=
  match Json.parse line >>= Decode.program with
  | .error msg => report #[] "malformed" msg
  | .ok program =>
    match runProgram fuel program with
    | .ok () s => report s.out "normal" ""
    | .throw v s => report s.out "throw" (describe s v)
    | .unknown why s => report s.out "unknown" why
    | .fuel => report #[] "fuel" ""

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
