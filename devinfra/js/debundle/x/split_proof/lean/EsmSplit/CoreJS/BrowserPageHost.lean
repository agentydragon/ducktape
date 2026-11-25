import EsmSplit.CoreJS.ECMAScriptHost

namespace CoreJS.BrowserPageHost

private def pageFunction (name : String) : HostGlobalEntry :=
  ⟨name, some "function", some ("window." ++ name), false, false, false, false,
    some s!"the browser page exposes the unchanged {name} function throughout initialization"⟩

private def pageObject (name : String) : HostGlobalEntry :=
  ⟨name, some "object", some ("window." ++ name), false, false, false, false,
    some s!"the browser page exposes the unchanged {name} object throughout initialization"⟩

private def pageAlias (name : String) : HostGlobalEntry :=
  ⟨name, some "object", some "window", false, false, false, false,
    some "window, globalThis, and self return the same page global object throughout initialization"⟩

private def opaquePageGlobal (name : String) : HostGlobalEntry :=
  ⟨name, none, none, true, false, false, false,
    some s!"the browser page exposes {name} as a stable opaque binding throughout initialization"⟩

private def undefinedShim (name : String) : HostGlobalEntry :=
  ⟨name, some "undefined", none, true, false, false, false,
    some s!"typeof {name} is undefined on this page, and a direct read is treated as opaque"⟩

private def pageMember (owner key : String) : HostMemberEntry :=
  ⟨owner, key, some ("window." ++ owner ++ "." ++ key), false, none, none,
    some s!"the browser page exposes the unchanged {owner}.{key} function throughout initialization"⟩

def aliases : List String := ["window", "globalThis", "self"]

def functions : List String :=
  ["AbortController", "AbortSignal", "AudioContext", "AudioData", "Blob", "ClipboardEvent", "ClipboardItem",
   "CustomEvent", "DOMException", "DOMMatrix", "DOMParser", "DOMRect", "DocumentFragment",
   "Element", "ErrorEvent", "Event", "EventSource", "EventTarget", "File", "FileReader", "FontFace",
   "FormData", "HTMLAnchorElement", "HTMLAudioElement", "HTMLButtonElement", "HTMLCanvasElement",
   "HTMLDialogElement", "HTMLElement", "HTMLFormElement", "HTMLImageElement", "HTMLInputElement",
   "HTMLSpanElement", "HTMLTextAreaElement", "HTMLVideoElement", "Headers", "IDBCursor", "IDBDatabase",
   "IDBIndex", "IDBKeyRange", "IDBObjectStore", "IDBRequest", "IDBTransaction", "Image", "ImageBitmap",
   "ImageData", "IntersectionObserver", "KeyboardEvent", "MathMLElement", "MediaRecorder", "MediaStream",
   "MediaStreamTrack", "MediaStreamTrackGenerator", "MessageChannel", "MouseEvent", "MutationObserver",
   "Node", "NodeList", "OffscreenCanvas", "Option", "Path2D", "PointerEvent", "RTCIceCandidate",
   "RTCPeerConnection", "RTCPeerConnectionIceErrorEvent", "RTCRtpReceiver", "RTCRtpScriptTransform",
   "RTCRtpSender", "RTCSessionDescription", "Range", "ReadableStream", "Request", "ResizeObserver",
   "Response", "SVGElement", "ScrollTimeline", "ShadowRoot", "SharedWorker", "StorageEvent", "Notification", "TextDecoder",
   "TextDecoderStream", "TextEncoder", "TouchEvent", "TransformStream", "URL", "URLSearchParams",
   "VideoFrame", "WebGLRenderingContext", "WebSocket", "Worker", "WritableStream", "XMLHttpRequest",
   "XMLSerializer", "XRWebGLBinding", "XRWebGLLayer", "addEventListener", "atob", "btoa",
   "cancelAnimationFrame", "cancelIdleCallback", "clearInterval", "clearTimeout", "close", "dispatchEvent",
   "fetch", "getComputedStyle", "getSelection", "matchMedia", "open", "postMessage", "prompt",
   "queueMicrotask", "removeEventListener", "reportError", "requestAnimationFrame", "requestIdleCallback",
   "scroll", "scrollBy", "scrollTo", "setInterval", "setTimeout", "showSaveFilePicker", "structuredClone"]

def objects : List String :=
  ["CSS", "caches", "console", "crypto", "customElements", "document", "documentPictureInPicture", "history",
   "indexedDB", "localStorage", "location", "navigation", "navigator", "performance", "screen", "sessionStorage"]

def opaqueGlobals : List String :=
  ["devicePixelRatio", "event", "innerHeight", "innerWidth", "isSecureContext", "opener", "pageXOffset",
   "pageYOffset", "parent", "scrollX", "scrollY", "visualViewport", "NodeFilter"]

def undefinedShims : List String :=
  ["Buffer", "WorkerGlobalScope", "__filename", "exports", "global", "importScripts", "module", "process",
   "require", "setImmediate"]

def globals : List HostGlobalEntry :=
  aliases.map pageAlias ++ functions.map pageFunction ++ objects.map pageObject ++
    opaqueGlobals.map opaquePageGlobal ++ undefinedShims.map undefinedShim

def members : List HostMemberEntry :=
  (["canParse", "createObjectURL", "parse", "revokeObjectURL"].map (pageMember "URL"))

def layer : HostLayer :=
  { name := "browser-page", globals, members,
    opaqueConstructors := ["URL"].map fun name =>
      ⟨name, some s!"the browser page exposes the unchanged {name} constructor throughout initialization"⟩,
    capabilities := [
      ⟨.pageEnvironment, some "window, globalThis, and self denote the same page global object throughout initialization"⟩,
      ⟨.arrayFromTemplate, some "tagged template arrays use the page realm's Array iterator throughout initialization"⟩,
      ⟨.opaqueStandardConstructors, some "deferred standard constructors use the page realm's unchanged constructors throughout initialization"⟩] }

def layers : List HostLayer := [ECMAScriptHost.layer, layer]

end CoreJS.BrowserPageHost
