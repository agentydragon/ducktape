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
// Treat code-block placeholders as visible so tests exercise the mounted CodeMirror view, while
// leaving any other observer users to happy-dom's implementation.
const NativeIntersectionObserver = globalThis.IntersectionObserver;
if (typeof window !== "undefined" && typeof NativeIntersectionObserver !== "undefined") {
  class VisibleIntersectionObserver implements IntersectionObserver {
    private readonly observer: IntersectionObserver;
    readonly root: Element | Document | null;
    readonly rootMargin: string;
    readonly thresholds: ReadonlyArray<number>;

    constructor(
      private readonly callback: IntersectionObserverCallback,
      options?: IntersectionObserverInit
    ) {
      this.observer = new NativeIntersectionObserver((entries) => this.callback(entries, this), options);
      this.root = options?.root ?? null;
      this.rootMargin = options?.rootMargin ?? "0px";
      this.thresholds = Array.isArray(options?.threshold) ? options.threshold : [options?.threshold ?? 0];
    }

    observe(target: Element): void {
      if (!target.matches(".agentplane-code-block-placeholder")) {
        this.observer.observe(target);
        return;
      }
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

    disconnect(): void {
      this.observer.disconnect();
    }
    unobserve(target: Element): void {
      this.observer.unobserve(target);
    }
    takeRecords(): IntersectionObserverEntry[] {
      return this.observer.takeRecords();
    }
  }

  globalThis.IntersectionObserver = VisibleIntersectionObserver;
}
