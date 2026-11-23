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

/-- An allowed opaque constructor can be retained in deferred code. A reached lookup ends
the modeled execution before argument evaluation, even inside a catch clause. -/
def opaqueConstructor : HostProfile → String → Bool
  | .browserPage, name => ["Date", "Error", "RegExp", "Set", "URL"].contains name
  | _, _ => false

end HostProfile

/-- The observations used by a certificate, independent of the selected decoding profile. -/
structure HostPremises where
  windowTypeof : Bool := false
  windowRef : Bool := false
  deriving DecidableEq

namespace HostPremises

def assumptions (p : HostPremises) : List String :=
  (if p.windowTypeof then ["typeof window is object without observable effects throughout initialization"] else []) ++
  (if p.windowRef then ["window returns the same page object without observable effects throughout initialization"] else [])

/-- Each used observation returns the specified result and preserves every host state. -/
def Satisfied (p : HostPremises) (w : World) : Prop :=
  (p.windowTypeof = true → ∀ s, w.globalTypeof "window" s = some (.ret (.str "object"), s)) ∧
  (p.windowRef = true → ∀ s, w.globalRef "window" s = some ("window", s))

def specialize (p : HostPremises) (w : World) : World :=
  { w with
    globalTypeof := fun name s =>
      if p.windowTypeof && name == "window" then some (.ret (.str "object"), s) else w.globalTypeof name s
    globalRef := fun name s =>
      if p.windowRef && name == "window" then some ("window", s) else w.globalRef name s }

theorem specialize_eq (p : HostPremises) (w : World) (h : p.Satisfied w) : p.specialize w = w := by
  have typeObs : (p.specialize w).globalTypeof = w.globalTypeof := by
    funext name s
    simp only [specialize, Bool.and_eq_true, beq_iff_eq]
    split
    · rename_i premise
      rcases premise with ⟨enabled, rfl⟩
      exact (h.1 enabled s).symm
    · rfl
  have refObs : (p.specialize w).globalRef = w.globalRef := by
    funext name s
    simp only [specialize, Bool.and_eq_true, beq_iff_eq]
    split
    · rename_i premise
      rcases premise with ⟨enabled, rfl⟩
      exact (h.2 enabled s).symm
    · rfl
  cases w with
  | mk σ init step observe read talk =>
    exact congr (congrArg (fun f g => World.mk σ init step f g talk) typeObs) refObs

end HostPremises
end CoreJS
