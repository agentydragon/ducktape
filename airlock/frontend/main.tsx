import "@mantine/core/styles.css";
import { MantineProvider } from "@mantine/core";
import { createRoot } from "react-dom/client";

import App from "./App";
import { handleAuthCallback, isAuthCallback } from "./auth";

// Handle the OIDC callback before mounting the app. A failed or re-loaded
// callback (single-use authorization code already spent, PKCE state gone from
// sessionStorage) must not leave the root blank. On error, remove stale callback
// parameters and mount the app so it can start a clean login.
if (isAuthCallback()) {
  try {
    await handleAuthCallback();
  } catch (error) {
    console.error("OIDC callback failed; restarting login", error);
    window.history.replaceState({}, "", "/");
  }
}

const root = document.getElementById("root");
if (!root) throw new Error("No #root element");

createRoot(root).render(
  <MantineProvider defaultColorScheme="auto">
    <App />
  </MantineProvider>
);
