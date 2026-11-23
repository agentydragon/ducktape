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

/-- The profile is for a document Window realm. A page may install a Node shim;
certificates that specialize `typeof global` explicitly report the extra premise. -/
def pageBindings : HostProfile → List String
  | .browserPage => ["window", "globalThis", "self"]
  | .empty => []

def typeofBindings (profile : HostProfile) : List (String × String) :=
  (profile.pageBindings.map fun n => (n, "object")) ++
    (if profile == .browserPage then [("global", "undefined")] else [])

/-- An allowed opaque constructor can be retained in deferred code. A reached lookup ends
the modeled execution before argument evaluation, even inside a catch clause. -/
def opaqueConstructor : HostProfile → String → Bool
  | .browserPage, name => ["Date", "Error", "RegExp", "Set", "URL"].contains name
  | _, _ => false

end HostProfile

/-- The observations used by a certificate, independent of the selected decoding profile. -/
structure HostPremises where
  typeofBindings : List (String × String) := []
  pageBindings : List String := []
  pageError : Bool := false
  deriving DecidableEq

namespace HostPremises

def assumptions (p : HostPremises) : List String :=
  (p.typeofBindings.map fun (name, value) =>
    s!"typeof {name} is {value} without observable effects throughout initialization") ++
  (p.pageBindings.map fun name =>
    s!"{name} returns the same page object without observable effects throughout initialization") ++
  (if p.pageError then ["reading Error from the page object returns the same opaque host object without observable effects throughout initialization"] else [])

private def refIdentity (p : HostPremises) : HostRef → Option String
  | .binding name => if name ∈ p.pageBindings then some "window" else none
  | .property "window" "Error" => if p.pageError then some "window.Error" else none
  | .property .. => none

/-- Each used observation returns the specified result and preserves every host state. -/
def Satisfied (p : HostPremises) (w : World) : Prop :=
  (∀ name value, p.typeofBindings.lookup name = some value →
    ∀ s, w.globalTypeof name s = some (.ret (.str value), s)) ∧
  (∀ ref identity, p.refIdentity ref = some identity → ∀ s, w.hostRef ref s = some (identity, s))

def specialize (p : HostPremises) (w : World) : World :=
  { w with
    globalTypeof := fun name s =>
      match p.typeofBindings.lookup name with
      | some value => some (.ret (.str value), s)
      | none => w.globalTypeof name s
    hostRef := if p = {} then w.hostRef else fun ref s =>
      match p.refIdentity ref with
      | some identity => some (identity, s)
      | none => w.hostRef ref s }

theorem specialize_eq (p : HostPremises) (w : World) (h : p.Satisfied w) : p.specialize w = w := by
  have typeObs : (p.specialize w).globalTypeof = w.globalTypeof := by
    funext name s
    cases he : p.typeofBindings.lookup name with
    | none => simp [specialize, he]
    | some value => simpa [specialize, he] using (h.1 name value he s).symm
  have refObs : (p.specialize w).hostRef = w.hostRef := by
    by_cases empty : p = {}
    · simp [specialize, empty]
    funext ref s
    cases he : p.refIdentity ref with
    | none => simp [specialize, empty, he]
    | some identity => simpa [specialize, empty, he] using (h.2 ref identity he s).symm
  cases w with
  | mk σ init step observe read talk =>
    exact congr (congrArg (fun f g => World.mk σ init step f g talk) typeObs) refObs

end HostPremises
end CoreJS
