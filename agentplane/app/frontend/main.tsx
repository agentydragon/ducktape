import "@mantine/core/styles.css";

import { createRoot } from "react-dom/client";

import { restoreRouteAfterLogin } from "./operator_login";
import { initializeBrowserTracing } from "./tracing";
import { ThemeProvider } from "./theme";

import App from "./app";

const container = document.getElementById("root");
if (!container) throw new Error("missing #root");
void initializeBrowserTracing()
  .catch((error: unknown) => {
    // Tracing setup must not keep the app from loading if the browser cannot initialize it.
    console.error("Failed to initialize browser tracing", error);
  })
  .then(() => {
    // A login redirect drops the fragment the router routes on; put it back before mounting.
    restoreRouteAfterLogin();

    createRoot(container).render(
      <ThemeProvider>
        <App />
      </ThemeProvider>
    );
  });
