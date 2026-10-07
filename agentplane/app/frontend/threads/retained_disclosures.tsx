import { createContext, type JSX, type ReactNode, useContext, useMemo, useState } from "react";

import { Disclosure } from "../disclosure";

const MAX_RETAINED_DISCLOSURES = 128;

interface DisclosureState {
  open: ReadonlyMap<string, boolean>;
  setOpen: (id: string, open: boolean) => void;
}

const DisclosureContext = createContext<DisclosureState | null>(null);

/** Retains bounded local disclosure state while virtualized rows leave the DOM. */
export function RetainedDisclosureProvider({ children }: { children: ReactNode }): JSX.Element {
  const [open, setOpenState] = useState<ReadonlyMap<string, boolean>>(() => new Map());
  const value = useMemo<DisclosureState>(
    () => ({
      open,
      setOpen: (id, nextOpen) => {
        setOpenState((previous) => {
          const next = new Map(previous);
          next.delete(id);
          next.set(id, nextOpen);
          while (next.size > MAX_RETAINED_DISCLOSURES) next.delete(next.keys().next().value!);
          return next;
        });
      },
    }),
    [open]
  );
  return <DisclosureContext.Provider value={value}>{children}</DisclosureContext.Provider>;
}

/** The retained open state behind `id`, for a disclosure toggled by something other than a summary.
 * `id: null` means there is no disclosure to track (e.g. a reasoning step with nothing to expand
 * into) -- always closed, and toggling it is a no-op. */
export function useRetainedDisclosure(id: string | null, defaultOpen = false): [boolean, (open: boolean) => void] {
  const state = useContext(DisclosureContext);
  if (!state) throw new Error("Retained disclosures require RetainedDisclosureProvider");
  return [id !== null ? (state.open.get(id) ?? defaultOpen) : false, (open) => id !== null && state.setOpen(id, open)];
}

export function RetainedDisclosure({
  id,
  summary,
  summaryAside,
  defaultOpen,
  dividerBoundary,
  children,
}: {
  id: string;
  summary: ReactNode;
  summaryAside?: ReactNode;
  defaultOpen?: boolean;
  /** Keep this disclosure's divider within its enclosing card edge. */
  dividerBoundary?: boolean;
  children: ReactNode;
}): JSX.Element {
  const [open, setOpen] = useRetainedDisclosure(id, defaultOpen);
  return (
    <Disclosure
      open={open}
      onOpenChange={setOpen}
      summary={summary}
      summaryAside={summaryAside}
      dividerBoundary={dividerBoundary}
    >
      {children}
    </Disclosure>
  );
}
