import { createRoot } from "react-dom/client";

import { openLink } from "./bridge.ts";
import App from "./app.tsx";
import "./styles.css";

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
  true,
);

const container = document.getElementById("root");
if (!container) throw new Error("missing #root");
createRoot(container).render(<App />);
