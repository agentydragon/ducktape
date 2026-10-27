import React from "react";
import { createRoot } from "react-dom/client";

import AugurApp from "./app";

const rootElement = document.getElementById("root");
if (rootElement == null) throw new Error("Augur root element is missing");

const root = createRoot(rootElement);
root.render(<AugurApp />);
