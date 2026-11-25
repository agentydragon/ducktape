import EsmSplit.CoreJS.Move
import EsmSplit.CoreJS.HostProfile

namespace CoreJS

/-- A stable global binding and one stable data property of its receiver. -/
structure StableIntrinsic where
  id : String
  binding : String
  owner : String
  key : String
  value : HostValue
  deriving DecidableEq

namespace StableIntrinsic

def mathPI : StableIntrinsic := ⟨"Math.PI@1", "Math", "window.Math", "PI", .number 0x400921fb54442d18⟩

def objectDefineProperty : StableIntrinsic :=
  ⟨"Object.defineProperty@1", "Object", "intrinsic.Object", "defineProperty", .identity "intrinsic.Object.defineProperty"⟩

/-- Parse exactly the production contracts used by certification. The returned
premises are still enforced by the caller when selecting a proof strategy. -/
def readContract (j : Lean.Json) : Except String (HostProfile × List StableIntrinsic) := do
  let presets : List (HostProfile × StableIntrinsic) :=
    [(.browserPage, .mathPI), (.ecmascript, .objectDefineProperty)]
  match presets.find? (fun (p, d) => j == Lean.Json.mkObj [("profile", Lean.toJson p.label),
      ("stableIntrinsicValues", Lean.toJson [d.id])]) with
  | some (p, d) => return (p, [d])
  | none => return (← HostProfile.read j, [])

end StableIntrinsic
end CoreJS
