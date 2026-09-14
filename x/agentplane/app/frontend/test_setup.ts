// Mantine 9's autosizing Textarea listens for font-loading events. happy-dom does
// not provide document.fonts, but the frontend tests do not need font metrics.
if (typeof document !== "undefined" && !document.fonts) {
  Object.defineProperty(document, "fonts", {
    configurable: true,
    value: new EventTarget(),
  });
}
