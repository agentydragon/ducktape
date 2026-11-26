import EsmSplit.LowerSplit
import EsmSplit.LowerSplit.BazelProofConfig

namespace EsmSplit.LowerSplit

private def installedRoot (exe suffix : String) : Option System.FilePath :=
  if exe.endsWith ("/" ++ suffix) then some (exe.dropEnd suffix.length).toString else none

/-- The running binary anchors the proof toolchain to its Bazel installation.
The build records both the execroot and materialized runfiles paths of every input. -/
private def loadGeneratedProofToolchain : IO GeneratedProofToolchain := do
  let exe := (← IO.appPath).toString
  let installedPath : (String × String) → System.FilePath ←
    if let some root := installedRoot exe BazelProofConfig.executablePaths.1 then
      pure fun paths => root / paths.1
    else if let some root := installedRoot exe BazelProofConfig.executablePaths.2 then
      pure fun paths => root / paths.2
    else
      throw (IO.userError s!"cannot locate the build-pinned generated-proof toolchain beside {exe}")
  let resolve (paths : String × String) : IO System.FilePath := do
    let path := installedPath paths
    unless ← path.pathExists do
      throw (IO.userError s!"missing generated-proof input {path}")
    return path
  let lean ← IO.FS.realPath (← resolve BazelProofConfig.leanPaths)
  let some sysroot := lean.parent.bind System.FilePath.parent
    | throw (IO.userError "missing generated-proof Lean sysroot")
  let mut importArts : Lean.NameMap Lean.ImportArtifacts := {}
  for (module, paths) in BazelProofConfig.importPaths do
    let name := (module.splitOn ".").foldl Lean.Name.str .anonymous
    importArts := importArts.insert name (.ofArrays #[#[← resolve paths]])
  return { lean, sysroot, importArts }

end EsmSplit.LowerSplit

def main : List String → IO UInt32 :=
  EsmSplit.LowerSplit.main EsmSplit.LowerSplit.loadGeneratedProofToolchain
