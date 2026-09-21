import { createContext, type JSX, type ReactNode, useContext, useMemo, useState } from "react";

const MAX_RETAINED_DISCLOSURES = 128;

interface DisclosureState {
  open: ReadonlySet<string>;
  setOpen: (id: string, open: boolean) => void;
}

const DisclosureContext = createContext<DisclosureState | null>(null);

/** Retains bounded local disclosure state while virtualized rows leave the DOM. */
export function RetainedDisclosureProvider({ children }: { children: ReactNode }): JSX.Element {
  const [open, setOpenState] = useState<ReadonlySet<string>>(() => new Set());
  const value = useMemo<DisclosureState>(
    () => ({
      open,
      setOpen: (id, nextOpen) => {
        setOpenState((previous) => {
          const next = new Set(previous);
          next.delete(id);
          if (nextOpen) next.add(id);
          while (next.size > MAX_RETAINED_DISCLOSURES) next.delete(next.values().next().value!);
          return next;
        });
      },
    }),
    [open]
  );
  return <DisclosureContext.Provider value={value}>{children}</DisclosureContext.Provider>;
}

export function RetainedDisclosure({
  id,
  summary,
  children,
}: {
  id: string;
  summary: ReactNode;
  children: ReactNode;
}): JSX.Element {
  const state = useContext(DisclosureContext);
  if (!state) throw new Error("RetainedDisclosure requires RetainedDisclosureProvider");
  const open = state.open.has(id);
  return (
    <details open={open} onToggle={(event) => state.setOpen(id, event.currentTarget.open)}>
      <summary>{summary}</summary>
      {open && children}
    </details>
  );
}
