// Use the compiled module here and in specs so setup and tests share one observer registry.
import { TestIntersectionObserver } from "../../../../util/frontend/test_intersection_observer.js";

// Mantine 9's autosizing Textarea listens for font-loading events. happy-dom does
// not provide document.fonts, but the frontend tests do not need font metrics.
if (typeof document !== "undefined" && !document.fonts) {
  Object.defineProperty(document, "fonts", {
    configurable: true,
    value: new EventTarget(),
  });
}

// MantineProvider reads window.matchMedia to detect the OS color scheme. happy-dom provides a
// stub; install one for other DOM environments if a suite selects one.
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
