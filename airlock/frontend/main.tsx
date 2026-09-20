import "@mantine/core/styles.css";
import { MantineProvider } from "@mantine/core";
import { createRoot } from "react-dom/client";

import App from "./App";

const root = document.getElementById("root");
if (!root) throw new Error("No #root element");

createRoot(root).render(
  <MantineProvider defaultColorScheme="auto">
    <App />
  </MantineProvider>
);
