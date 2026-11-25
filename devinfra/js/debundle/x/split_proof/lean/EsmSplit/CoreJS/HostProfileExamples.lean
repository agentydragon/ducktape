import EsmSplit.CoreJS.HostProfile

namespace CoreJS.HostProfileExamples

private def browserNames : List String :=
  HostProfile.aliases ++ BrowserGlobals.functions ++ BrowserGlobals.objects ++
    BrowserGlobals.opaqueBindings ++ BrowserGlobals.shims

private def oldBrowserTypeof (name : String) : Option String :=
  if HostProfile.aliases.contains name || BrowserGlobals.objects.contains name then some "object"
  else if BrowserGlobals.functions.contains name then some "function"
  else if BrowserGlobals.shims.contains name then some "undefined"
  else none

private def oldBrowserIdentity (name : String) : Option String :=
  if HostProfile.aliases.contains name then some "window"
  else if name == "Array" then some "intrinsic.Array"
  else if name == "Symbol" then some "intrinsic.Symbol"
  else if BrowserGlobals.functions.contains name || BrowserGlobals.objects.contains name then
    some ("window." ++ name)
  else none

private def oldUndeclared (name : String) : Bool :=
  !(BrowserGlobals.functions.contains name || BrowserGlobals.objects.contains name)

private def oldRecordedValue (name : String) : Bool :=
  hostFunctions.contains name || (hostNamespaces.lookup name).isSome ||
    BrowserGlobals.functions.contains name || BrowserGlobals.objects.contains name

private def oldRecordedPrimitive (owner key : String) : Option Prim :=
  match owner, key with
  | "console", "log" => some .log
  | "Object", "defineProperty" => some .defineProperty
  | "Object", "create" => some .create
  | "Object", "getPrototypeOf" => some .getPrototypeOf
  | "Object", "setPrototypeOf" => some .setPrototypeOf
  | _, _ => none

#guard browserNames.all fun name =>
  HostProfile.bindingType .browserPage name == oldBrowserTypeof name

#guard browserNames.all fun name =>
  HostProfile.bindingIdentity .browserPage name == oldBrowserIdentity name

#guard browserNames.all fun name =>
  HostProfile.opaqueBinding .browserPage name ==
    (BrowserGlobals.opaqueBindings.contains name || BrowserGlobals.shims.contains name)

#guard (hostFunctions ++ hostNamespaces.map Prod.fst).all fun name =>
  HostProfile.recordedHostValue .nodeRecording name == oldRecordedValue name

#guard HostProfile.recordedHostValue .nodeRecording "Boolean"
#guard HostProfile.recordedHostValue .nodeRecording "Number"
#guard HostProfile.recordedHostValue .nodeRecording "isFinite"
#guard HostProfile.recordedHostValue .nodeRecording "isNaN"
#guard HostProfile.recordedHostValue .nodeRecording "parseFloat"
#guard HostProfile.recordedHostValue .nodeRecording "parseInt"
#guard HostProfile.recordedHostValue .nodeRecording "Math"
#guard HostProfile.recordedHostValue .nodeRecording "String"

#guard browserNames.all fun name =>
  HostProfile.undeclaredGlobal .nodeRecording name == oldUndeclared name

#guard ((BrowserGlobals.methods.flatMap fun (owner, keys) => keys.map fun key => (owner, key))).all
    fun (owner, key) =>
      let receiver := if owner == "Array" then "intrinsic.Array" else "window." ++ owner
      let identity := if owner == "Array" && key == "from" then "intrinsic.Array.from"
        else "window." ++ owner ++ "." ++ key
      HostProfile.identity .browserPage (.property receiver key) == some identity

#guard ["Date", "Error", "RegExp", "Set", "URL"].all fun name =>
  HostProfile.opaqueConstructor .browserPage name

#guard [("console", "log"), ("Object", "create"), ("Object", "defineProperty"),
    ("Object", "getPrototypeOf"), ("Object", "setPrototypeOf")].all fun (owner, key) =>
  HostProfile.recordedPrim .nodeRecording owner key == oldRecordedPrimitive owner key

end CoreJS.HostProfileExamples
