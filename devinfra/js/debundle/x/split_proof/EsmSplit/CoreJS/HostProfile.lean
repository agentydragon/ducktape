import EsmSplit.CoreJS.BrowserGlobals
import EsmSplit.CoreJS.Value
import Lean.Data.Json

namespace CoreJS

inductive HostProfile where
  | empty
  | browserPage
  | ecmascript
  /-- Test-only: the host the Node comparison harness records. It is not a deployment profile. -/
  | nodeRecording
  deriving DecidableEq, Repr

namespace HostProfile

def label : HostProfile → String
  | .empty => "empty"
  | .browserPage => "browser-page"
  | .ecmascript => "ecmascript"
  | .nodeRecording => "node-recording"

/-- Fresh `Symbol(…)` calls and the ambient well-known symbols are host observations. -/
def ambientSymbols (p : HostProfile) : Bool :=
  p == .browserPage || p == .nodeRecording

/-- Under the recorded host, the host catalog's globals and `eval` are opaque host values. The world
answers calls of `hostFunctions` and of the members of `hostNamespaces`. -/
def recordedHostValue (p : HostProfile) (name : String) : Bool :=
  p == .nodeRecording &&
    (name == "eval" || hostFunctions.contains name || (hostNamespaces.lookup name).isSome ||
      BrowserGlobals.functions.contains name ||
      BrowserGlobals.objects.contains name)

/-- Under the recorded host, a name outside the host catalog is undeclared: reading it throws. -/
def undeclaredGlobal (p : HostProfile) (name : String) : Bool :=
  p == .nodeRecording && !p.recordedHostValue name

/-- The recorded host's primitive calls: `console.log` and the `Object` functions the model defines. -/
def recordedPrim (p : HostProfile) (owner key : String) : Option Prim := do
  guard (p == .nodeRecording)
  match owner, key with
  | "console", "log" =>
    some .log
  | "Object", "defineProperty" =>
    some .defineProperty
  | "Object", "create" =>
    some .create
  | "Object", "getPrototypeOf" =>
    some .getPrototypeOf
  | "Object", "setPrototypeOf" =>
    some .setPrototypeOf
  | _, _ =>
    none

/-- Read a supported deployment profile. -/
def read (j : Lean.Json) : Except String HostProfile := do
  if j == Lean.Json.mkObj [("profile", .str "browser-page")] then
    return .browserPage
  throw "unsupported host contract (expected browser-page)"

def aliases :=
  ["window", "globalThis", "self"]

def bindingType (profile : HostProfile) (name : String) : Option String := do
  guard (profile == .browserPage)
  if aliases.contains name || BrowserGlobals.objects.contains name then
    return "object"
  if BrowserGlobals.functions.contains name then
    return "function"
  if BrowserGlobals.shims.contains name then
    return "undefined"
  none

def opaqueBinding (profile : HostProfile) (name : String) : Bool :=
  profile == .browserPage &&
    (BrowserGlobals.opaqueBindings.contains name || BrowserGlobals.shims.contains name)

def bindingIdentity (profile : HostProfile) (name : String) : Option String := do
  if profile == .ecmascript then
    guard (name == "Object")
    return "intrinsic.Object"
  guard (profile == .browserPage)
  if aliases.contains name then
    return "window"
  if name == "Array" then
    return "intrinsic.Array"
  if name == "Symbol" then
    return "intrinsic.Symbol"
  guard (BrowserGlobals.functions.contains name || BrowserGlobals.objects.contains name)
  return "window." ++ name

def identity (profile : HostProfile) : HostRef → Option String
  | .binding name => profile.bindingIdentity name
  | .property "window" name => do
    guard (profile == .browserPage)
    profile.bindingIdentity name
  | .property "intrinsic.Array" key => do
    guard (profile == .browserPage)
    if key == "from" then
      return "intrinsic.Array.from"
    guard (((BrowserGlobals.methods.lookup "Array").getD []).contains key)
    return "window.Array." ++ key
  | .property "Symbol" member => do
    guard (profile == .browserPage)
    return (← WellKnownSymbol.ofMember? member).description
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
      let read :=
        match ref with
        | .binding name => name
        | .property receiver key => receiver ++ "." ++ key
      let result :=
        if ref == .property "Symbol" "iterator" && identity == "Symbol.iterator" then
          "the well-known iterator symbol"
        else
          if ref == .property "Symbol" "asyncIterator" && identity == "Symbol.asyncIterator" then
            "the well-known asyncIterator symbol"
          else
            if ref == .property "Symbol" "hasInstance" && identity == "Symbol.hasInstance" then
              "the well-known hasInstance symbol"
            else
              if ref == .property "Symbol" "toStringTag" && identity == "Symbol.toStringTag" then
                "the well-known toStringTag symbol"
              else
                if identity == "intrinsic.Array" then "the intrinsic Array constructor"
                else
                  if identity == "intrinsic.Array.from" then "the intrinsic Array.from function"
                  else
                    if identity == "intrinsic.Symbol" then "the intrinsic Symbol function"
                    else
                      if identity == "window" then "the same page object"
                      else s!"the stable host reference {identity}"
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
  (∀ name value,
      p.typeofBindings.lookup name = some value →
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
