import EsmSplit.LowerSplit
import Lean.Elab.Term
import Lean.Util.Path

namespace EsmSplit.LowerSplit

open Lean Elab Term in
/-- Record the actual compiler SDK used by Lake to build this entry. -/
elab "lowerSplitBuildSysroot%" : term =>
  return mkStrLit (← IO.FS.realPath (← Lean.getBuildDir)).toString

private def loadLakeProofToolchain : IO GeneratedProofToolchain := do
  let exe := (← IO.appPath).toString
  let suffix := "/.lake/build/bin/lowersplit"
  let missing := IO.userError "generated proofs require the executable from lake build lowersplit in its project"
  unless exe.endsWith suffix do throw missing
  let project := System.FilePath.mk (exe.dropEnd suffix.length).toString
  if project.toString.contains System.SearchPath.separator then
    throw (IO.userError "Lake project path contains Lean's search path separator")
  unless ← (project / "lakefile.toml").pathExists do throw missing
  let sysroot := System.FilePath.mk lowerSplitBuildSysroot%
  let lean := sysroot / "bin" / "lean"
  unless ← lean.pathExists do
    throw (IO.userError s!"missing generated-proof Lean compiler from the Lake build: {lean}")
  -- This Lake package has no external dependencies.
  let library := project / ".lake" / "build" / "lib" / "lean"
  unless ← (library / "EsmSplit" / "CoreJS" / "LinkedCertificate" / "Proof.olean").pathExists do
    throw (IO.userError s!"missing generated-proof imports from the Lake build: {library}")
  return { lean, sysroot, projectLibrary := some library, projectSource := some project }

end EsmSplit.LowerSplit

def main : List String → IO UInt32 :=
  EsmSplit.LowerSplit.main EsmSplit.LowerSplit.loadLakeProofToolchain
