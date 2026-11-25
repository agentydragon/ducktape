import EsmSplit.CoreJS.Run
import EsmSplit.CoreJS.LinkedInput

/-- `--ast` lowers through the certification lowering; every other mode is `CoreJS.main`. -/
def main (args : List String) : IO UInt32 := do
  match args with
  | ["--ast", file] =>
    for line in (← IO.FS.lines file) do
      IO.println (CoreJS.runLine 100000 line fun j =>
        CoreJS.LinkedInput.program j ((j.getObjVal? "providers").toOption.getD (Lean.Json.mkObj [])))
    pure 0
  | _ => CoreJS.main args
