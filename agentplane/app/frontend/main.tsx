import "@mantine/core/styles.css";

import { createRoot } from "react-dom/client";

import { restoreRouteAfterLogin } from "./operator_login";
import { ThemeProvider } from "./theme";

import App from "./app";

const container = document.getElementById("root");
if (!container) throw new Error("missing #root");
// A login redirect drops the fragment the router routes on; put it back before mounting.
restoreRouteAfterLogin();

createRoot(container).render(
  <ThemeProvider>
    <App />
  </ThemeProvider>
);
