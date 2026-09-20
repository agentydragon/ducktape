import { createContext, useContext } from "react";

import type { RunModalPrefill } from "./types";

interface RunModalContextValue {
  open(prefill?: RunModalPrefill): void;
}

const RunModalContext = createContext<RunModalContextValue | null>(null);

export function useRunModal(): RunModalContextValue {
  const context = useContext(RunModalContext);
  if (!context) throw new Error("useRunModal must be used within RunModalContext.Provider");
  return context;
}

export { RunModalContext };
