import { createRoot } from "react-dom/client";

import "./app.css";
import App from "./App";

const container = document.getElementById("app");
if (!container) throw new Error("missing #app");
createRoot(container).render(<App />);
