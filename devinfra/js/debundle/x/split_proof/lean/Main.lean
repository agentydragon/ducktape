import EsmSplit.Cli
import EsmSplit.Controls

open EsmSplit Example Lean

/-- Check `P`; write it if it passes (or with `force`). Exit code 0 iff it passes. -/
def checkAndWrite (S : Spec) (P : Plan) (dir : System.FilePath) (force : Bool) : IO UInt32 := do
  let ok ← report bundle S P
  if ok || force then writeModules bundle P dir
  else IO.eprintln "checks failed; nothing written"
  return if ok then 0 else 1

/-- ```text
render <dir>                          the spec in spec.json (must pass)
render <dir> --variant <name>         another accepted spec
render <dir> --force <control>        a control the checks reject, written anyway
render <dir> --spec <file> [--force]  any spec file, read at run time
``` -/
def main (args : List String) : IO UInt32 := do
  match args with
  | [dir] => IO.println "spec.json"; checkAndWrite spec plan dir false
  | [dir, "--variant", name] =>
    let some s := specVariants.lookup name | IO.eprintln s!"no variant named {name}"; return 2
    IO.println s!"variant {name}"; checkAndWrite s (mkPlan bundle s) dir false
  | [dir, "--force", name] =>
    let some (S, P) := (specControls.lookup name).map (fun s => (s, mkPlan bundle s))
        <|> (planControls.lookup name).map (spec, ·)
      | IO.eprintln s!"no control named {name}"; return 2
    IO.println s!"control {name} (checks are expected to fail)"; checkAndWrite S P dir true
  | dir :: "--spec" :: file :: rest =>
    let s ← loadSpec file
    checkAndWrite s (mkPlan bundle s) dir (rest == ["--force"])
  | _ => IO.eprintln "usage: render <dir> [--variant <name> | --force <control> | --spec <file> [--force]]"; return 2
