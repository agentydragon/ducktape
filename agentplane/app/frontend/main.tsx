import "@mantine/core/styles.css";

import { MantineProvider } from "@mantine/core";
import { createRoot } from "react-dom/client";

import { restoreRouteAfterLogin } from "./operator_login";

import App from "./app";

const container = document.getElementById("root");
if (!container) throw new Error("missing #root");
// A login redirect drops the fragment the router routes on; put it back before mounting.
restoreRouteAfterLogin();

createRoot(container).render(
  <MantineProvider defaultColorScheme="auto">
    <App />
  </MantineProvider>
);
