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

// happy-dom exposes IntersectionObserver but does not calculate visibility or call observers.
// Treat observed nodes as visible so tests exercise the mounted CodeMirror view instead of its
// near-viewport placeholder.
if (typeof window !== "undefined") {
  class VisibleIntersectionObserver implements IntersectionObserver {
    constructor(private readonly callback: IntersectionObserverCallback) {}

    observe(target: Element): void {
      const bounds = target.getBoundingClientRect();
      this.callback(
        [
          {
            boundingClientRect: bounds,
            intersectionRatio: 1,
            intersectionRect: bounds,
            isIntersecting: true,
            rootBounds: null,
            target,
            time: performance.now(),
          },
        ],
        this
      );
    }

    disconnect(): void {}
    unobserve(_target: Element): void {}
    takeRecords(): IntersectionObserverEntry[] {
      return [];
    }
  }

  globalThis.IntersectionObserver = VisibleIntersectionObserver;
}
