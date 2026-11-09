import { createElement } from "react";
import { createRoot } from "react-dom/client";
import { flushSync } from "react-dom";
import { MantineProvider, Typography } from "@mantine/core";

// The content is reviewed, committed report HTML, never an API response or user input.
// Keep report-specific scripts outside this root and import this module before binding controls.
const root = document.getElementById("report-content");
const html = root.innerHTML;
flushSync(() => {
  createRoot(root).render(
    createElement(
      MantineProvider,
      { defaultColorScheme: "auto" },
      createElement(Typography, { dangerouslySetInnerHTML: { __html: html } }),
    ),
  );
});
