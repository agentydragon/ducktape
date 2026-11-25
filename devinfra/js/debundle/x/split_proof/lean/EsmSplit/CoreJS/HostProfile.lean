import EsmSplit.CoreJS.BrowserGlobals
import EsmSplit.CoreJS.Value
import Lean.Data.Json

namespace CoreJS

/-! A host contract is data assembled from layers. The built-in layers below keep the
existing observations while the semantic rules migrate to the shared ECMAScript layer. -/

inductive HostCapability where
  | ambientSymbols
  | pageEnvironment
  | arrayFromTemplate
  | recordedStringConversion
  | scriptedRecording
  | undeclaredOutsideCatalog
  deriving DecidableEq, Repr

structure HostGlobalEntry where
  name : String
  typeTag : Option String := none
  identity : Option String := none
  opaqueRef : Bool := false
  recordedValue : Bool := false
  deriving DecidableEq, Repr

structure HostMemberEntry where
  owner : String
  key : String
  identity : Option String := none
  primitive : Option Prim := none
  deriving DecidableEq, Repr

structure HostLayer where
  name : String
  globals : List HostGlobalEntry := []
  members : List HostMemberEntry := []
  opaqueConstructors : List String := []
  capabilities : List HostCapability := []
  deriving DecidableEq, Repr

structure HostProfile where
  name : String
  layers : List HostLayer := []
  deriving DecidableEq, Repr

namespace HostProfile

def label (p : HostProfile) : String := p.name

def hasCapability (p : HostProfile) (capability : HostCapability) : Bool :=
  p.layers.any fun layer => layer.capabilities.contains capability

private def mergeField {α : Type} [BEq α] (left right : Option α) : Option (Option α) :=
  match left, right with
  | some a, some b => if a == b then some (some a) else none
  | some a, none | none, some a => some (some a)
  | none, none => some none

private def mergeGlobal (left right : HostGlobalEntry) : Option HostGlobalEntry := do
  guard (left.name == right.name)
  if left.opaqueRef && right.typeTag.isSome && right.typeTag != some "undefined" then none else
  if right.opaqueRef && left.typeTag.isSome && left.typeTag != some "undefined" then none else
  let typeTag ← mergeField left.typeTag right.typeTag
  let identity ← mergeField left.identity right.identity
  pure ⟨left.name, typeTag, identity, left.opaqueRef || right.opaqueRef,
    left.recordedValue || right.recordedValue⟩

private def mergeMember (left right : HostMemberEntry) : Option HostMemberEntry := do
  guard (left.owner == right.owner && left.key == right.key)
  let identity ← mergeField left.identity right.identity
  let primitive ← mergeField left.primitive right.primitive
  pure ⟨left.owner, left.key, identity, primitive⟩

def globalEntry? (p : HostProfile) (name : String) : Option HostGlobalEntry := Id.run do
  let mut found : Option HostGlobalEntry := none
  for entry in p.layers.flatMap (·.globals) do
    if entry.name == name then
      found := match found with
        | none => some entry
        | some prior => mergeGlobal prior entry
      if found.isNone then return none
  return found

def memberEntry? (p : HostProfile) (owner key : String) : Option HostMemberEntry := Id.run do
  let mut found : Option HostMemberEntry := none
  for entry in p.layers.flatMap (·.members) do
    if entry.owner == owner && entry.key == key then
      found := match found with
        | none => some entry
        | some prior => mergeMember prior entry
      if found.isNone then return none
  return found

def valid (p : HostProfile) : Bool := Id.run do
  for (entry, i) in (p.layers.flatMap (·.globals)).zipIdx do
    if (p.layers.flatMap (·.globals)).drop (i + 1) |>.any fun next =>
        next.name == entry.name && (mergeGlobal entry next).isNone then
      return false
  for (entry, i) in (p.layers.flatMap (·.members)).zipIdx do
    if (p.layers.flatMap (·.members)).drop (i + 1) |>.any fun next =>
        next.owner == entry.owner && next.key == entry.key && (mergeMember entry next).isNone then
      return false
  return true

private def catalogGlobals : List HostGlobalEntry :=
  (BrowserGlobals.functions.map fun name => ⟨name, some "function", none, false, false⟩) ++
  (BrowserGlobals.objects.map fun name => ⟨name, some "object", none, false, false⟩) ++
  (BrowserGlobals.opaqueBindings.map fun name => ⟨name, none, none, true, false⟩) ++
  (BrowserGlobals.shims.map fun name => ⟨name, some "undefined", none, true, false⟩)

private def browserMembers : List HostMemberEntry :=
  BrowserGlobals.methods.flatMap fun (owner, keys) => keys.map fun key =>
    ⟨owner, key,
      some (if owner == "Array" && key == "from" then "intrinsic.Array.from"
        else "window." ++ owner ++ "." ++ key), none⟩

private def ecmascriptLayer : HostLayer :=
  { name := "ecmascript", globals := [⟨"Object", none, some "intrinsic.Object", false, false⟩] }

private def hostCatalogLayer : HostLayer :=
  { name := "host-catalog", globals := catalogGlobals }

private def browserPageLayer : HostLayer :=
  { name := "browser-page", globals :=
      [⟨"window", some "object", some "window", false, false⟩,
       ⟨"globalThis", some "object", some "window", false, false⟩,
       ⟨"self", some "object", some "window", false, false⟩],
    members := browserMembers,
    opaqueConstructors := ["Date", "Error", "RegExp", "Set", "URL"],
    capabilities := [.pageEnvironment, .arrayFromTemplate] }

private def nodeRecordingLayer : HostLayer :=
  { name := "recorded-node",
    globals := (catalogGlobals.filter fun entry =>
      entry.typeTag == some "function" || entry.typeTag == some "object").map fun entry =>
        { entry with recordedValue := true },
    members := [
      ⟨"console", "log", none, some .log⟩,
      ⟨"Object", "defineProperty", none, some .defineProperty⟩,
      ⟨"Object", "create", none, some .create⟩,
      ⟨"Object", "getPrototypeOf", none, some .getPrototypeOf⟩,
      ⟨"Object", "setPrototypeOf", none, some .setPrototypeOf⟩],
    capabilities := [.ambientSymbols, .recordedStringConversion,
      .scriptedRecording, .undeclaredOutsideCatalog] }

def empty : HostProfile := ⟨"empty", []⟩
def browserPage : HostProfile := ⟨"browser-page", [hostCatalogLayer, browserPageLayer]⟩
def ecmascript : HostProfile := ⟨"ecmascript", [ecmascriptLayer]⟩
def nodeRecording : HostProfile := ⟨"node-recording", [hostCatalogLayer, nodeRecordingLayer]⟩

def isEmpty (p : HostProfile) : Bool := p.layers.isEmpty

/-- Fresh `Symbol(…)` calls and the ambient well-known symbols are host observations. -/
def ambientSymbols (p : HostProfile) : Bool := p.hasCapability .ambientSymbols || p.hasCapability .pageEnvironment

/-- The recorded Node layer marks globals whose references the harness records. The model-defined
calls are an additional part of that layer. -/
def recordedHostValue (p : HostProfile) (name : String) : Bool :=
  (p.globalEntry? name).any (·.recordedValue)

/-- Under the recorded host, a name outside the host catalog is undeclared: reading it throws. -/
def undeclaredGlobal (p : HostProfile) (name : String) : Bool :=
  p.hasCapability .undeclaredOutsideCatalog &&
    !((p.globalEntry? name).any fun entry => entry.typeTag == some "function" || entry.typeTag == some "object")

/-- The recorded host's primitive calls: `console.log` and the `Object` functions the model defines. -/
def recordedPrim (p : HostProfile) (owner key : String) : Option Prim := do
  p.memberEntry? owner key >>= (·.primitive)

def read (j : Lean.Json) : Except String HostProfile := do
  unless j == Lean.Json.mkObj [("profile", .str "browser-page")] do
    throw "unsupported host contract (expected the browser-page profile)"
  return browserPage

def aliases := ["window", "globalThis", "self"]

def bindingType (profile : HostProfile) (name : String) : Option String := do
  guard (profile.hasCapability .pageEnvironment)
  (profile.globalEntry? name).bind (·.typeTag)

def opaqueBinding (profile : HostProfile) (name : String) : Bool :=
  profile.hasCapability .pageEnvironment && (profile.globalEntry? name).any (·.opaqueRef)

def bindingIdentity (profile : HostProfile) (name : String) : Option String := do
  if let some identity := (profile.globalEntry? name).bind (·.identity) then return identity
  guard (profile.hasCapability .pageEnvironment)
  if aliases.contains name then return "window"
  if name == "Array" then return "intrinsic.Array"
  if name == "Symbol" then return "intrinsic.Symbol"
  let entry ← profile.globalEntry? name
  guard (entry.typeTag == some "function" || entry.typeTag == some "object")
  return "window." ++ name

def identity (profile : HostProfile) : HostRef → Option String
  | .binding name => profile.bindingIdentity name
  | .property "window" name => do
    guard (profile.hasCapability .pageEnvironment)
    profile.bindingIdentity name
  | .property "intrinsic.Array" key => do
    guard (profile.hasCapability .pageEnvironment)
    (profile.memberEntry? "Array" key).bind (·.identity)
  | .property "Symbol" member => do
    guard (profile.ambientSymbols)
    return (← WellKnownSymbol.ofMember? member).description
  | .property receiver key => do
    guard (profile.hasCapability .pageEnvironment)
    let owner ← receiver.dropPrefix? "window."
    (profile.memberEntry? owner.toString key).bind (·.identity)

/-- An allowed opaque constructor can be retained in deferred code. A reached lookup ends
the modeled execution before argument evaluation, even inside a catch clause. -/
def opaqueConstructor : HostProfile → String → Bool
  | profile, name => profile.layers.any fun layer => layer.opaqueConstructors.contains name

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
    else if ref == .property "Symbol" "asyncIterator" && identity == "Symbol.asyncIterator" then
      "the well-known asyncIterator symbol"
    else if ref == .property "Symbol" "hasInstance" && identity == "Symbol.hasInstance" then
      "the well-known hasInstance symbol"
    else if ref == .property "Symbol" "toStringTag" && identity == "Symbol.toStringTag" then
      "the well-known toStringTag symbol"
    else if identity == "intrinsic.Array" then "the intrinsic Array constructor"
    else if identity == "intrinsic.Array.from" then "the intrinsic Array.from function"
    else if identity == "intrinsic.Symbol" then "the intrinsic Symbol function"
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
