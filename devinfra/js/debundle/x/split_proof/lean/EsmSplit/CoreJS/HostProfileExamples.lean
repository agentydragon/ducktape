import EsmSplit.CoreJS.HostProfile
import EsmSplit.CoreJS.BrowserGlobals

namespace CoreJS.HostProfileExamples

private def legacyBrowserNames : List String :=
  BrowserPageHost.aliases ++ BrowserGlobals.functions ++ BrowserGlobals.objects ++
    BrowserGlobals.opaqueBindings ++ BrowserGlobals.shims

-- The old catalog treated eval as an opaque extra. It now comes from the shared
-- ECMAScript layer as a standard intrinsic and is recorded by the Node harness.
private def legacyBrowserComparisonNames : List String := legacyBrowserNames.filter (· != "eval")

private def legacyBrowserTypeof (name : String) : Option String :=
  if BrowserPageHost.aliases.contains name || BrowserGlobals.objects.contains name then some "object"
  else if BrowserGlobals.functions.contains name then some "function"
  else if BrowserGlobals.shims.contains name then some "undefined"
  else none

private def legacyBrowserIdentity (name : String) : Option String :=
  if BrowserPageHost.aliases.contains name then some "window"
  else if name == "Array" then some "intrinsic.Array"
  else if name == "Symbol" then some "intrinsic.Symbol"
  else if BrowserGlobals.functions.contains name || BrowserGlobals.objects.contains name then
    some ("window." ++ name)
  else none

private def legacyBrowserMemberIdentity (owner key : String) : Option String :=
  if owner == "Symbol" then
    match WellKnownSymbol.ofMember? key with
    | some symbol => some symbol.description
    | none => none
  else if ((BrowserGlobals.methods.lookup owner).getD []).contains key then
    some (if owner == "Array" && key == "from" then "intrinsic.Array.from"
      else "window." ++ owner ++ "." ++ key)
  else none

private def legacyBrowserReceiver (owner : String) : String :=
  if owner == "Array" then "intrinsic.Array"
  else if owner == "Symbol" then "Symbol"
  else "window." ++ owner

private def observedRefHasPremise (profile : HostProfile) (ref : HostRef) (premise : String) : Bool :=
  match (HostPremises.observations profile).hostRef ref {} with
  | some (_, state) => state.profileFacts.contains premise
  | none => false

private def observedTypeHasPremise (profile : HostProfile) (name premise : String) : Bool :=
  match (HostPremises.observations profile).globalTypeof name {} with
  | some (_, state) => state.profileFacts.contains premise
  | none => false

#guard HostProfile.valid HostProfile.ecmascript
#guard HostProfile.valid HostProfile.browserPage
#guard HostProfile.valid HostProfile.node
#guard HostProfile.valid HostProfile.nodeRecording

-- The browser layer and the shared ECMAScript layer preserve every old browser
-- catalog answer while keeping all standard names out of the page-only layer.
#guard legacyBrowserComparisonNames.all fun name =>
  HostProfile.bindingType .browserPage name == legacyBrowserTypeof name
#guard legacyBrowserComparisonNames.all fun name =>
  HostProfile.bindingIdentity .browserPage name == legacyBrowserIdentity name
#guard legacyBrowserComparisonNames.all fun name =>
  HostProfile.opaqueBinding .browserPage name ==
    (BrowserGlobals.opaqueBindings.contains name || BrowserGlobals.shims.contains name)
#guard legacyBrowserNames.all fun name => (HostProfile.globalEntry? .browserPage name).isSome
#guard HostProfile.bindingType .browserPage "eval" == some "function"
#guard HostProfile.bindingIdentity .browserPage "eval" == some "window.eval"
#guard !HostProfile.opaqueBinding .browserPage "eval"
#guard BrowserPageHost.globals.all fun entry =>
  !(ECMAScriptHost.globalEntries.any (·.name == entry.name))
#guard NodeHost.globals.all fun entry =>
  !(ECMAScriptHost.globalEntries.any (·.name == entry.name))
#guard HostProfile.bindingIdentity .node "global" == some "node.global"
#guard HostProfile.bindingIdentity .node "globalThis" == some "node.global"
#guard HostProfile.bindingType .node "URL" == some "function"
#guard HostProfile.bindingType .node "FileReader" == some "undefined"

#guard ((ECMAScriptHost.allMembers.map fun entry => (entry.owner, entry.key)) ++
    (BrowserPageHost.members.map fun entry => (entry.owner, entry.key))).all fun (owner, key) =>
  HostProfile.identity .browserPage (.property (legacyBrowserReceiver owner) key) ==
    legacyBrowserMemberIdentity owner key
#guard BrowserGlobals.methods.all fun (owner, keys) => keys.all fun key =>
  HostProfile.identity .browserPage (.property (legacyBrowserReceiver owner) key) ==
    legacyBrowserMemberIdentity owner key
#guard HostProfile.identity .browserPage (.property "intrinsic.Symbol" "for") == none

-- The model's world-call table matches the pre-layer host-call fragment.
#guard ECMAScriptHost.hostFunctions == ["Boolean", "Number", "eval", "isFinite", "isNaN", "parseFloat", "parseInt"]
#guard ECMAScriptHost.hostNamespaces == [
  ("Math", ["abs", "acos", "acosh", "asin", "asinh", "atan", "atanh", "atan2", "cbrt", "ceil",
    "clz32", "cos", "cosh", "exp", "expm1", "floor", "fround", "hypot", "imul", "log", "log10",
    "log1p", "log2", "max", "min", "pow", "random", "round", "sign", "sin", "sinh", "sqrt", "tan",
    "tanh", "trunc"]),
  ("Number", ["isFinite", "isInteger", "isNaN", "isSafeInteger", "parseFloat", "parseInt"]),
  ("String", ["fromCharCode", "fromCodePoint"])]

#guard (BrowserGlobals.opaqueBindings.filter (· != "eval")).all fun name =>
  HostProfile.opaqueBinding .browserPage name &&
    (HostProfile.bindingIdentity .browserPage name).isNone
#guard BrowserGlobals.shims.all fun name =>
  HostProfile.opaqueBinding .browserPage name && (HostProfile.bindingIdentity .browserPage name).isNone

#guard HostProfile.valueForRef? .browserPage (.property "intrinsic.Math" "PI") ==
  some (.number 0x400921fb54442d18)
#guard observedRefHasPremise .browserPage (.property "intrinsic.Math" "PI")
  "the own Math.PI data property returns a Number with binary64 bits 4614256656552045848 throughout initialization"

-- The test recording uses only ECMAScript and Node names. A page-only name is
-- undeclared and its premise is carried into a typeof observation.
#guard (ECMAScriptHost.globalFunctions ++ ECMAScriptHost.globalObjects).all fun name =>
  HostProfile.recordedHostValue .nodeRecording name
#guard NodeHost.globals.all fun entry => HostProfile.recordedHostValue .nodeRecording entry.name
#guard HostProfile.bindingType .nodeRecording "Object" == some "function"
#guard (HostProfile.bindingIdentity .nodeRecording "Object").isNone
#guard (HostProfile.valueForRef? .nodeRecording (.binding "Object")).isNone
#guard NodeHost.globals.all fun entry =>
  HostProfile.recordedHostValue .nodeRecording entry.name &&
    HostProfile.bindingType .nodeRecording entry.name == entry.typeTag
#guard observedTypeHasPremise .nodeRecording "Object"
  "the ambient Object binding is the unmodified ECMAScript intrinsic throughout initialization"
#guard BrowserPageHost.globals.all fun entry =>
  if (ECMAScriptHost.globalEntries ++ NodeHost.globals).any (·.name == entry.name) then
    HostProfile.recordedHostValue .nodeRecording entry.name
  else HostProfile.undeclaredGlobal .nodeRecording entry.name
#guard !HostProfile.recordedHostValue .nodeRecording "FileReader"
#guard HostProfile.undeclaredGlobal .nodeRecording "FileReader"
#guard HostProfile.bindingType .nodeRecording "FileReader" == some "undefined"
#guard !HostProfile.undeclaredGlobal .nodeRecording "Object"
#guard observedTypeHasPremise .nodeRecording "FileReader"
  "a global name absent from the ECMAScript and Node layers is undeclared throughout initialization"

#guard HostProfile.recordedPrim .nodeRecording "console" "log" == some .log
#guard HostProfile.recordedPrim .nodeRecording "Object" "create" == some .create
#guard HostProfile.recordedPrim .nodeRecording "Object" "defineProperty" == some .defineProperty
#guard HostProfile.recordedPrim .nodeRecording "Object" "getPrototypeOf" == some .getPrototypeOf
#guard HostProfile.recordedPrim .nodeRecording "Object" "setPrototypeOf" == some .setPrototypeOf
#guard (HostProfile.recordedPrim .browserPage "Object" "create").isNone
#guard (HostProfile.premises .nodeRecording).contains
  "console.log is the unmodified Node console method and appends formatted output to the recording"

#guard ECMAScriptHost.opaqueConstructorNames.all fun name =>
  HostProfile.opaqueConstructor .browserPage name
#guard HostProfile.opaqueConstructor .browserPage "URL"
#guard !HostProfile.opaqueConstructor .nodeRecording "URL"
#guard ECMAScriptHost.opaqueConstructorNames.all fun name =>
  !HostProfile.opaqueConstructor .nodeRecording name
#guard (HostProfile.premises .browserPage).contains
  "deferred standard constructors use the page realm's unchanged constructors throughout initialization"

private def invalidGlobalProfile : HostProfile :=
  { name := "invalid", layers := [{ name := "bad", globals := [{ name := "x", identity := some "x" }] }] }

private def conflictingGlobalFieldsProfile : HostProfile :=
  { name := "invalid", layers := [{ name := "bad", globals := [
      { name := "x", typeTag := some "object", identity := some "x", opaqueRef := true,
        premise := some "conflicting global facts" }] }] }

#guard !HostProfile.valid invalidGlobalProfile
#guard !HostProfile.valid conflictingGlobalFieldsProfile

private def recordedValueWithoutPremise : HostProfile :=
  { name := "recorded", layers := [{ name := "recorded", globals := [{ name := "x", recordedValue := true }] }] }

#guard HostProfile.valid recordedValueWithoutPremise
#guard HostProfile.premises recordedValueWithoutPremise == []
#guard observedRefHasPremise .browserPage (.binding "Object")
  "the ambient Object binding is the unmodified ECMAScript intrinsic throughout initialization"
#guard observedTypeHasPremise .browserPage "FileReader"
  "the browser page exposes the unchanged FileReader function throughout initialization"

private def invalidMemberProfile : HostProfile :=
  { name := "invalid", layers := [{ name := "bad", members := [
      { owner := "Object", key := "create", primitive := some .create }] }] }

private def invalidCapabilityProfile : HostProfile :=
  { name := "invalid", layers := [{ name := "bad", capabilities := [⟨.ambientSymbols, none⟩] }] }

#guard !HostProfile.valid invalidMemberProfile
#guard !HostProfile.valid invalidCapabilityProfile

private def conflictingGlobalProfile : HostProfile :=
  { name := "conflict", layers := [
      { name := "first", globals := [⟨"x", none, some "first", false, false, false, false, some "first premise"⟩] },
      { name := "second", globals := [⟨"x", none, some "second", false, false, false, false, some "second premise"⟩] }] }

private def conflictingMemberProfile : HostProfile :=
  { name := "conflict", layers := [
      { name := "first", members := [⟨"X", "y", some "first", false, none, none, some "first premise"⟩] },
      { name := "second", members := [⟨"X", "y", some "second", false, none, none, some "second premise"⟩] }] }

#guard !HostProfile.valid conflictingGlobalProfile
#guard !HostProfile.valid conflictingMemberProfile

private def explicitUndeclaredProfile : HostProfile :=
  { name := "author-extension", layers := [{ name := "author-extension", globals := [
      { name := "optionalFeature", undeclared := true, premise := some "optionalFeature is absent" }] }] }

#guard HostProfile.valid explicitUndeclaredProfile
#guard HostProfile.undeclaredGlobal explicitUndeclaredProfile "optionalFeature"
#guard HostProfile.premises explicitUndeclaredProfile == ["optionalFeature is absent"]
#guard HostProfile.bindingType explicitUndeclaredProfile "optionalFeature" == some "undefined"

private def parseProfile (source : String) : Except String HostProfile := do
  HostProfile.read (← Lean.Json.parse source)

#guard (parseProfile "{\"profile\":\"browser-page\"}").isOk
#guard (parseProfile "{\"profile\":\"empty\",\"extensions\":[{\"name\":\"app\",\"globals\":[{\"name\":\"appConfig\",\"typeof\":\"object\",\"premise\":\"appConfig is stable\"}]}]}").isOk
#guard (match parseProfile "{\"profile\":\"empty\",\"extensions\":[{\"name\":\"bad\",\"globals\":[{\"name\":\"appConfig\",\"typeof\":\"object\"}]}]}" with
    | .error _ => true
    | .ok _ => false)
#guard (parseProfile "{\"profile\":\"empty\",\"extensions\":[{\"name\":\"recording\",\"globals\":[{\"name\":\"recorded\",\"recordedValue\":true}]}]}").isOk
#guard (parseProfile "{\"profile\":\"empty\",\"extensions\":[{\"name\":\"app\",\"members\":[{\"owner\":\"appConfig\",\"key\":\"version\",\"dataValue\":{\"kind\":\"number\",\"bits\":\"4614256656552045848\"},\"premise\":\"appConfig.version is stable\"}]}]}").isOk
#guard (match parseProfile "{\"profile\":\"empty\",\"extensions\":[{\"name\":\"bad\",\"globals\":[{\"name\":\"appConfig\",\"typeof\":\"other\",\"premise\":\"bad type\"}]}]}" with
    | .error _ => true
    | .ok _ => false)

end CoreJS.HostProfileExamples
