import EsmSplit.CoreJS.Run
import EsmSplit.CoreJS.LinkedInput

private def loweringProfile (j : Lean.Json) : Except String CoreJS.HostProfile := do
  if j == Lean.Json.mkObj [("profile", .str "empty")] then return .empty
  if j == Lean.Json.mkObj [("profile", .str "ecmascript")] then return .ecmascript
  CoreJS.HostProfile.read j

private def runLoweringCensus (file : String) (profile : CoreJS.HostProfile) : IO UInt32 := do
  match Lean.Json.parse (← IO.FS.readFile file) >>= (CoreJS.LinkedInput.concreteCensus · profile) with
  | .ok report => IO.println report.compress; pure 0
  | .error why => IO.eprintln why; pure 1

/-- `--ast` lowers through the certification lowering under the recorded Node host; every other
mode is `CoreJS.main`. -/
def main (args : List String) : IO UInt32 := do
  match args with
  | ["--lowering-census", file] => runLoweringCensus file .empty
  | ["--lowering-census", file, "--host-contract", contract] =>
    match Lean.Json.parse (← IO.FS.readFile contract) >>= loweringProfile with
    | .ok profile => runLoweringCensus file profile
    | .error why => IO.eprintln why; pure 1
  | ["--ast", file] =>
    for line in (← IO.FS.lines file) do
      IO.println (CoreJS.runLine 100000 line fun j scripts =>
        CoreJS.LinkedInput.program j ((j.getObjVal? "providers").toOption.getD (Lean.Json.mkObj []))
          (Std.HashSet.ofList (scripts.map Prod.fst)) ((j.getObjValAs? String "entry").toOption.getD "main"))
    pure 0
  | _ => CoreJS.main args
