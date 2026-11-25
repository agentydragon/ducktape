import EsmSplit.CoreJS.ECMAScriptHost

namespace CoreJS.NodeHost

private def nodeFunction (name : String) : HostGlobalEntry :=
  ⟨name, some "function", some ("node." ++ name), false, false, false, false,
    some s!"the Node host exposes the unmodified {name} global throughout initialization"⟩

private def nodeObject (name : String) : HostGlobalEntry :=
  ⟨name, some "object", some ("node." ++ name), false, false, false, false,
    some s!"the Node host exposes the unmodified {name} global throughout initialization"⟩

private def nodeGlobalObject (name : String) : HostGlobalEntry :=
  ⟨name, some "object", some "node.global", false, false, false, false,
    some s!"the Node {name} binding aliases the Node global object throughout initialization"⟩

def globals : List HostGlobalEntry :=
  ( ["AbortController", "AbortSignal", "Blob", "Buffer", "ByteLengthQueuingStrategy",
     "BroadcastChannel", "CompressionStream", "CountQueuingStrategy", "Crypto", "CryptoKey",
     "CustomEvent", "DecompressionStream", "DOMException", "Event", "EventTarget", "File",
     "FormData", "Headers", "MessageChannel", "MessageEvent", "MessagePort", "Navigator",
     "PerformanceEntry", "PerformanceMark", "PerformanceMeasure", "PerformanceObserver",
     "PerformanceObserverEntryList", "PerformanceResourceTiming", "ReadableByteStreamController",
     "ReadableStream", "ReadableStreamBYOBReader", "ReadableStreamBYOBRequest",
     "ReadableStreamDefaultController", "ReadableStreamDefaultReader", "Request", "Response",
     "SubtleCrypto", "TextDecoder", "TextDecoderStream", "TextEncoder", "TextEncoderStream",
     "TransformStream", "TransformStreamDefaultController", "URL", "URLSearchParams", "WebSocket",
     "WritableStream", "WritableStreamDefaultController", "WritableStreamDefaultWriter"].map nodeFunction) ++
  [nodeFunction "atob", nodeFunction "btoa", nodeFunction "clearImmediate", nodeFunction "clearInterval",
   nodeFunction "clearTimeout", nodeFunction "fetch", nodeFunction "queueMicrotask", nodeFunction "setImmediate",
   nodeFunction "setInterval", nodeFunction "setTimeout", nodeFunction "structuredClone",
   nodeObject "console", nodeObject "crypto", nodeObject "navigator", nodeObject "performance",
   nodeObject "process", nodeGlobalObject "global", nodeGlobalObject "globalThis"]

def members : List HostMemberEntry :=
  [⟨"console", "log", some "node.console.log", false, none, none,
    some "Node's console.log binding is the unmodified console method throughout initialization"⟩]

def layer : HostLayer :=
  { name := "node",
    globals,
    members,
    capabilities := [
      ⟨.undeclaredNames, some "a global name absent from the ECMAScript and Node layers is undeclared throughout initialization"⟩] }

def layers : List HostLayer := [ECMAScriptHost.layer, layer]

end CoreJS.NodeHost
