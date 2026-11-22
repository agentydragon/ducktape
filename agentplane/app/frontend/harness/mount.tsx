import type { ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { ThemeProvider } from "../shell/theme";

export function mount(element: ReactNode): void {
  const container = document.getElementById("app");
  if (!container) throw new Error("missing #app");
  createRoot(container).render(<ThemeProvider>{element}</ThemeProvider>);
}
