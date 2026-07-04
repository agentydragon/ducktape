import { MantineProvider, type MantineColorScheme, type MantineColorSchemeManager } from "@mantine/core";
import { Notifications } from "@mantine/notifications";
import { createRoot } from "react-dom/client";

import "@mantine/core/styles.css";
import "@mantine/notifications/styles.css";

import { openLink } from "./bridge.ts";
import App from "./app.tsx";
import { notifyError } from "./errors.ts";
import { theme } from "./theme.ts";
import "./styles.css";

// Last-resort surfacing: anything that escapes a component's own handling (a bug, an unhandled
// rejection) still reaches the operator as a toast instead of vanishing into the console. This is
// also where backend error telemetry would hook in later (see errors.ts / the improvement note).
window.addEventListener("error", (e) => notifyError("Unexpected error", e.error ?? e.message));
window.addEventListener("unhandledrejection", (e) => notifyError("Unexpected error", e.reason));

// Intercept all anchor clicks: the iframe is sandboxed without allow-popups, so
// bare <a> navigation is blocked in the sandbox. Route every link through the
// shell's openLink bridge instead (shell scheme-gates + opens). Capture phase
// ensures this fires before the browser's own navigation handler.
document.addEventListener(
  "click",
  (e: MouseEvent) => {
    const anchor = (e.target as Element).closest("a");
    if (!anchor) return;
    const href = anchor.getAttribute("href");
    if (!href) return;
    e.preventDefault();
    void openLink(href);
  },
  true
);

// The sandboxed cross-origin iframe may not have same-origin storage, so never touch
// localStorage for the color scheme (Mantine's default manager would throw). Follow the
// system scheme (`auto` → prefers-color-scheme) with a no-op, storage-free manager.
const noopColorSchemeManager: MantineColorSchemeManager = {
  get: (fallback: MantineColorScheme) => fallback,
  set: () => {},
  subscribe: () => {},
  unsubscribe: () => {},
  clear: () => {},
};

const container = document.getElementById("root");
if (!container) throw new Error("missing #root");
createRoot(container).render(
  <MantineProvider theme={theme} defaultColorScheme="auto" colorSchemeManager={noopColorSchemeManager}>
    {/* Top-right so toasts don't collide with the bottom-fixed GitProgressBar. */}
    <Notifications position="top-right" />
    <App />
  </MantineProvider>
);
