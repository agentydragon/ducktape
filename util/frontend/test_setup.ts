// Use the compiled module here and in specs so setup and tests share one observer registry.
import { TestIntersectionObserver } from "./test_intersection_observer.js";

if (typeof document !== "undefined") {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
}

// happy-dom has no FontFaceSet; transcript layout listens for its loading events.
if (typeof document !== "undefined" && !document.fonts) {
  Object.defineProperty(document, "fonts", { configurable: true, value: new EventTarget() });
}

// MantineProvider reads window.matchMedia to detect the OS color scheme. happy-dom provides a
// stub; jsdom does not.
if (typeof window !== "undefined" && typeof window.matchMedia !== "function") {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    addEventListener: () => {},
    removeEventListener: () => {},
  })) as unknown as typeof window.matchMedia;
}

// Both happy-dom and jsdom lack layout-driven intersection notifications. The shared fake
// lets behavior tests deliver those explicitly; code-view placeholders start visible.
if (typeof window !== "undefined") window.IntersectionObserver = TestIntersectionObserver;
