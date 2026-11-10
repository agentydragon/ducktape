// Production app entry for the public debundle corpus: mounts the published
// Excalidraw editor the way an application embedding it would.
import { Excalidraw } from "@excalidraw/excalidraw";
import "@excalidraw/excalidraw/index.css";
import { createElement } from "react";
import { createRoot } from "react-dom/client";

createRoot(globalThis.document.getElementById("root")).render(
  createElement("div", { style: { height: "100vh" } }, createElement(Excalidraw))
);
