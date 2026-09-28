/**
 * The shared shell topbar (app.tsx): one toggle button plus two DOM slots the current route
 * portals its own title and trailing actions into, so a thread's title and menu render in the
 * same row as the toggle without app.tsx needing to know anything about threads.
 */
import { createContext, type Context, type JSX, type ReactNode, useContext } from "react";
import { createPortal } from "react-dom";

export interface TopbarSlots {
  title: HTMLElement | null;
  actions: HTMLElement | null;
}

export const TopbarContext: Context<TopbarSlots> = createContext<TopbarSlots>({ title: null, actions: null });

/** Portals `children` into the shared topbar's title slot while mounted; renders nothing before
 * the slot's DOM node exists (one frame, on first mount) or outside a `TopbarContext` provider. */
export function TopbarTitle({ children }: { children: ReactNode }): JSX.Element | null {
  const { title } = useContext(TopbarContext);
  return title ? createPortal(children, title) : null;
}

/** Portals `children` into the shared topbar's trailing-actions slot -- see `TopbarTitle`. */
export function TopbarActions({ children }: { children: ReactNode }): JSX.Element | null {
  const { actions } = useContext(TopbarContext);
  return actions ? createPortal(children, actions) : null;
}
