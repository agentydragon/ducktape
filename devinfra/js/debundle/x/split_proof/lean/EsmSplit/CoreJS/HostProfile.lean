import EsmSplit.CoreJS.BrowserGlobals
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

def aliases := ["window", "globalThis", "self"]

def bindingType (profile : HostProfile) (name : String) : Option String := do
  guard (profile == .browserPage)
  if aliases.contains name || BrowserGlobals.objects.contains name then return "object"
  if BrowserGlobals.functions.contains name then return "function"
  if BrowserGlobals.shims.contains name then return "undefined"
  none

def opaqueBinding (profile : HostProfile) (name : String) : Bool :=
  profile == .browserPage && (BrowserGlobals.opaqueBindings.contains name || BrowserGlobals.shims.contains name)

def bindingIdentity (profile : HostProfile) (name : String) : Option String := do
  guard (profile == .browserPage)
  if aliases.contains name then return "window"
  guard (BrowserGlobals.functions.contains name || BrowserGlobals.objects.contains name)
  return "window." ++ name

def identity (profile : HostProfile) : HostRef → Option String
  | .binding name | .property "window" name => profile.bindingIdentity name
  | .property "Symbol" "iterator" => do
    guard (profile == .browserPage)
    return "Symbol.iterator"
  | .property receiver key => do
    guard (profile == .browserPage)
    let owner ← receiver.dropPrefix? "window."
    guard (((BrowserGlobals.methods.lookup owner.toString).getD []).contains key)
    return receiver ++ "." ++ key

/-- An allowed opaque constructor can be retained in deferred code. A reached lookup ends
the modeled execution before argument evaluation, even inside a catch clause. -/
def opaqueConstructor : HostProfile → String → Bool
  | .browserPage, name => ["Date", "Error", "RegExp", "Set", "URL"].contains name
  | _, _ => false

end HostProfile

/-- In any host state, this read returns exactly the indicated result and leaves
the state intact. Identity and Number-bits results are distinct constructors. -/
abbrev World.StableRead (w : World) (ref : HostRef) (value : HostValue) : Prop :=
  ∀ s, w.hostRef ref s = some (value, s)

/-- The observations used by a certificate, independent of the selected decoding profile. -/
structure HostPremises where
  typeofBindings : List (String × String) := []
  refs : List (HostRef × String) := []
  deriving DecidableEq

namespace HostPremises

def assumptions (p : HostPremises) : List String :=
  (p.typeofBindings.map fun (name, value) =>
    s!"typeof {name} is {value} without observable effects throughout initialization") ++
  (p.refs.map fun (ref, identity) =>
    let read := match ref with
      | .binding name => name
      | .property receiver key => receiver ++ "." ++ key
    let result := if ref == .property "Symbol" "iterator" && identity == "Symbol.iterator" then
      "the well-known iterator symbol"
    else if identity == "window" then "the same page object" else s!"the stable host reference {identity}"
    s!"{read} returns {result} without observable effects throughout initialization")

/-- This world selects candidate premises. The certificate independently checks execution
for every world satisfying just the observations made by the two initializers. -/
def observations (profile : HostProfile) : World where
  σ := HostPremises
  init := {}
  step := fun name this args st => ((World.probe.step name this args ()).1, st)
  talk := fun request st => ((World.probe.talk request ()).1, st)
  globalTypeof := fun name st => do
    let value ← profile.bindingType name
    return (.ret (.str value), { st with typeofBindings := st.typeofBindings.insert (name, value) })
  hostRef := fun ref st => do
    let identity ← profile.identity ref
    return (.identity identity, { st with refs := st.refs.insert (ref, identity) })

/-- Each used observation returns the specified result and preserves every host state. -/
def Satisfied (p : HostPremises) (w : World) : Prop :=
  (∀ name value, p.typeofBindings.lookup name = some value →
    ∀ s, w.globalTypeof name s = some (.ret (.str value), s)) ∧
  (∀ ref identity, p.refs.lookup ref = some identity → w.StableRead ref (.identity identity))

def specialize (p : HostPremises) (w : World) : World :=
  { w with
    globalTypeof := fun name s =>
      match p.typeofBindings.lookup name with
      | some value => some (.ret (.str value), s)
      | none => w.globalTypeof name s
    hostRef := fun ref s =>
      match p.refs.lookup ref with
      | some identity => some (.identity identity, s)
      | none => w.hostRef ref s }

theorem specialize_eq (p : HostPremises) (w : World) (h : p.Satisfied w) : p.specialize w = w := by
  have typeObs : (p.specialize w).globalTypeof = w.globalTypeof := by
    funext name s
    cases he : p.typeofBindings.lookup name with
    | none => simp [specialize, he]
    | some value => simpa [specialize, he] using (h.1 name value he s).symm
  have refObs : (p.specialize w).hostRef = w.hostRef := by
    funext ref s
    cases he : p.refs.lookup ref with
    | none => simp [specialize, he]
    | some identity => simpa [specialize, he] using (h.2 ref identity he s).symm
  cases w with
  | mk σ init step observe read talk =>
    exact congr (congrArg (fun f g => World.mk σ init step f g talk) typeObs) refObs

end HostPremises
end CoreJS
