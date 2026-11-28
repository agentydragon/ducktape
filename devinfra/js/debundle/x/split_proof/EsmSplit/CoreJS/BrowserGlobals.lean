/-! Browser-page observations available to the linked checker. Each reached lookup
needs its own world premise. A listed name can be absent in another browser or realm.
The lists describe bindings and data-valued method references, never call results. -/

namespace CoreJS.BrowserGlobals

def functions : List String :=
  ["AbortController", "AbortSignal", "AggregateError", "Array", "ArrayBuffer", "AudioContext",
    "AudioData", "BigInt", "BigInt64Array", "BigUint64Array", "Blob", "Boolean", "ClipboardEvent",
    "ClipboardItem", "CustomEvent", "DOMException", "DOMMatrix", "DOMParser", "DOMRect", "DataView",
    "Date", "DocumentFragment", "Element", "Error", "ErrorEvent", "EvalError", "Event",
    "EventSource", "EventTarget", "File", "FileReader", "FinalizationRegistry", "Float16Array",
    "Float32Array", "Float64Array", "FontFace", "FormData", "Function", "HTMLAnchorElement",
    "HTMLAudioElement", "HTMLButtonElement", "HTMLCanvasElement", "HTMLDialogElement",
    "HTMLElement", "HTMLFormElement", "HTMLImageElement", "HTMLInputElement", "HTMLSpanElement",
    "HTMLTextAreaElement", "HTMLVideoElement", "Headers", "IDBCursor", "IDBDatabase", "IDBIndex",
    "IDBKeyRange", "IDBObjectStore", "IDBRequest", "IDBTransaction", "Image", "ImageBitmap",
    "ImageData", "Int16Array", "Int32Array", "Int8Array", "IntersectionObserver", "Iterator",
    "KeyboardEvent", "Map", "MathMLElement", "MediaRecorder", "MediaStream", "MediaStreamTrack",
    "MediaStreamTrackGenerator", "MessageChannel", "MouseEvent", "MutationObserver", "Node",
    "NodeList", "Notification", "Number", "Object", "OffscreenCanvas", "Option", "Path2D",
    "PointerEvent", "Promise", "Proxy", "RTCIceCandidate", "RTCPeerConnection",
    "RTCPeerConnectionIceErrorEvent", "RTCRtpReceiver", "RTCRtpScriptTransform", "RTCRtpSender",
    "RTCSessionDescription", "Range", "RangeError", "ReadableStream", "ReferenceError", "RegExp",
    "Request", "ResizeObserver", "Response", "SVGElement", "ScrollTimeline", "Set", "ShadowRoot",
    "SharedArrayBuffer", "SharedWorker", "StorageEvent", "String", "SuppressedError", "Symbol",
    "SyntaxError", "TextDecoder", "TextDecoderStream", "TextEncoder", "TouchEvent",
    "TransformStream", "TypeError", "URIError", "URL", "URLSearchParams", "Uint16Array",
    "Uint32Array", "Uint8Array", "Uint8ClampedArray", "VideoFrame", "WeakMap", "WeakRef", "WeakSet",
    "WebGLRenderingContext", "WebSocket", "Worker", "WritableStream", "XMLHttpRequest",
    "XMLSerializer", "XRWebGLBinding", "XRWebGLLayer", "addEventListener", "atob", "btoa",
    "cancelAnimationFrame", "cancelIdleCallback", "clearInterval", "clearTimeout", "close",
    "decodeURI", "decodeURIComponent", "dispatchEvent", "encodeURI", "encodeURIComponent", "escape",
    "fetch", "getComputedStyle", "getSelection", "isFinite", "isNaN", "matchMedia", "open",
    "parseFloat", "parseInt", "postMessage", "prompt", "queueMicrotask", "removeEventListener",
    "reportError", "requestAnimationFrame", "requestIdleCallback", "scroll", "scrollBy", "scrollTo",
    "setInterval", "setTimeout", "showSaveFilePicker", "structuredClone", "unescape"]

def objects : List String :=
  ["Atomics", "CSS", "Intl", "JSON", "Math", "Reflect", "WebAssembly", "caches", "console",
    "crypto", "customElements", "document", "documentPictureInPicture", "history", "indexedDB",
    "localStorage", "location", "navigation", "navigator", "performance", "screen",
    "sessionStorage"]

-- These names can be retained, but the profile supplies no value or typeof result.
def opaqueBindings : List String :=
  ["devicePixelRatio", "event", "innerHeight", "innerWidth", "isSecureContext", "opener",
    "pageXOffset", "pageYOffset", "parent", "scrollX", "scrollY", "visualViewport", "eval",
    "NodeFilter"]

-- Browser bundles commonly guard platform shims. An absence test needs a premise;
-- directly reading such a binding remains opaque.
def shims : List String :=
  ["Buffer", "WorkerGlobalScope", "__filename", "exports", "global", "importScripts", "module",
    "process", "require", "setImmediate"]

def methods : List (String × List String) :=
  [("Array", ["from", "isArray"]), ("ArrayBuffer", ["isView"]), ("BigInt", ["asUintN"]),
    ("Date", ["UTC", "now", "parse"]),
    ("Intl", ["DateTimeFormat", "ListFormat", "NumberFormat", "RelativeTimeFormat", "Segmenter"]),
    ("JSON", ["parse", "stringify"]),
    ("Math",
      ["abs", "acos", "asin", "atan", "atan2", "cbrt", "ceil", "clz32", "cos", "cosh", "exp",
        "expm1", "floor", "fround", "hypot", "imul", "log", "log10", "log1p", "log2", "max", "min",
        "pow", "random", "round", "sign", "sin", "sinh", "sqrt", "tan", "trunc"]),
    ("Number", ["isFinite", "isInteger", "isNaN", "isSafeInteger", "parseFloat", "parseInt"]),
    ("Object",
      ["assign", "create", "defineProperties", "defineProperty", "entries", "freeze", "fromEntries",
        "getOwnPropertyDescriptor", "getOwnPropertyDescriptors", "getOwnPropertyNames",
        "getOwnPropertySymbols", "getPrototypeOf", "hasOwn", "is", "isExtensible", "isFrozen",
        "keys", "preventExtensions", "seal", "setPrototypeOf", "values"]),
    ("Promise", ["all", "allSettled", "race", "reject", "resolve"]), ("Proxy", ["revocable"]),
    ("Reflect",
      ["apply", "construct", "defineProperty", "deleteProperty", "get", "getOwnPropertyDescriptor",
        "getPrototypeOf", "has", "ownKeys", "set"]),
    ("String", ["fromCharCode", "fromCodePoint"]), ("Symbol", ["for"]),
    ("URL", ["canParse", "createObjectURL", "parse", "revokeObjectURL"])]

end CoreJS.BrowserGlobals
