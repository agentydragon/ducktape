import React from "react";
import { createRoot } from "react-dom/client";
import StudyCasino from "./study_casino.jsx";

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch((err) => {
      console.warn("service worker registration failed", err);
    });
  });
}

createRoot(document.getElementById("root")).render(<StudyCasino />);
