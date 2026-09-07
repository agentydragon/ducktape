import { createRoot } from "react-dom/client";

import { App } from "./app";
import "./board.css";
import "./page.css";

createRoot(document.getElementById("root")!).render(<App />);
