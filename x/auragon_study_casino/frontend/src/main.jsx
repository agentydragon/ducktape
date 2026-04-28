import React from "react";
import { createRoot } from "react-dom/client";
import StudyCasino from "./study_casino.jsx";

// Unregister any previously installed service workers so stale cached JS
// can't shadow new deploys.  Offline mode is not needed for this app.
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.getRegistrations().then((regs) => {
    for (const reg of regs) reg.unregister();
  });
}

createRoot(document.getElementById("root")).render(<StudyCasino />);
