// Mantine 9's autosizing Textarea listens for font-loading events. happy-dom does
// not provide document.fonts, but the frontend tests do not need font metrics.
if (typeof document !== "undefined" && !document.fonts) {
  Object.defineProperty(document, "fonts", {
    configurable: true,
    value: new EventTarget(),
  });
}

// MantineProvider reads window.matchMedia to detect the OS color scheme. happy-dom provides a
// stub; jsdom (used by markdown.test.tsx for its own reasons -- see the note there) does not.
if (typeof window !== "undefined" && typeof window.matchMedia !== "function") {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    addEventListener: () => {},
    removeEventListener: () => {},
  })) as unknown as typeof window.matchMedia;
}
