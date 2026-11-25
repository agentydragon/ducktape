import EsmSplit.CoreJS.Run
import EsmSplit.CoreJS.LinkedInput

/-- `--ast` lowers through the certification lowering under the supplied host profile; every
other mode is `CoreJS.main`. -/
def main (args : List String) : IO UInt32 := do
  match args with
  | ["--ast", "--host-contract", profileFile, file] =>
    let profileJson ← IO.ofExcept (Lean.Json.parse (← IO.FS.readFile profileFile))
    let profile ← IO.ofExcept (CoreJS.HostProfile.read profileJson)
    for line in (← IO.FS.lines file) do
      IO.println (CoreJS.runLineWithProfile 100000 line profile fun j scripts =>
        CoreJS.LinkedInput.program profile j ((j.getObjVal? "providers").toOption.getD (Lean.Json.mkObj []))
          (Std.HashSet.ofList (scripts.map Prod.fst)) ((j.getObjValAs? String "entry").toOption.getD "main"))
    pure 0
  | "--ast" :: _ =>
    IO.eprintln "usage: corejs --ast --host-contract PROFILE.json dumps.jsonl"
    pure 2
  | _ => CoreJS.main args
