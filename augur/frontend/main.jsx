import React from "react";
import { createRoot } from "react-dom/client";

import AugurApp from "./augur-app.jsx";

const root = createRoot(document.getElementById("root"));
root.render(<AugurApp />);
