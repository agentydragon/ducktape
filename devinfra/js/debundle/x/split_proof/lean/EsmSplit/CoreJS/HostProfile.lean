import EsmSplit.CoreJS.Value
import Lean.Data.Json

namespace CoreJS

inductive HostProfile where
  | empty | browserPage
  deriving DecidableEq, Repr

namespace HostProfile

def read (j : Lean.Json) : Except String HostProfile := do
  unless j == Lean.Json.mkObj [("profile", .str "browser-page")] do
    throw "unsupported host contract (expected the browser-page profile)"
  return .browserPage

def stableTypeof : HostProfile → String → Option String
  | .browserPage, "window" => some "object"
  | _, _ => none

/-- The observation has the specified value at every host state, with no effect on that state. -/
def Satisfied (p : HostProfile) (w : World) : Prop :=
  ∀ name value, p.stableTypeof name = some value → ∀ s,
    w.globalTypeof name s = some (.ret (.str value), s)

def specialize (p : HostProfile) (w : World) : World :=
  { w with globalTypeof := fun name s =>
      match p.stableTypeof name with
      | some value => some (.ret (.str value), s)
      | none => w.globalTypeof name s }

theorem specialize_eq (p : HostProfile) (w : World) (h : p.Satisfied w) : p.specialize w = w := by
  have obs : (p.specialize w).globalTypeof = w.globalTypeof := by
    funext name s
    cases e : p.stableTypeof name with
    | none => simp [specialize, e]
    | some value => simpa [specialize, e] using (h name value e s).symm
  cases w with
  | mk σ init step observe talk => exact congrArg (fun f => World.mk σ init step f talk) obs

end HostProfile
end CoreJS
