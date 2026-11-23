import { MantineProvider } from "@mantine/core";
import { act, type ReactNode } from "react";
import { createRoot } from "react-dom/client";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

export interface Mounted {
  container: HTMLDivElement;
  rerender: (node: ReactNode) => Promise<void>;
  /** Unmounts and detaches the container now; `cleanupMounted` skips it afterwards. */
  unmount: () => Promise<void>;
}

const mounted = new Set<Mounted>();

/** Mounts `node` in a new container on `document.body`, under a test-mode `MantineProvider`. */
export async function mountWithMantine(node: ReactNode): Promise<Mounted> {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const rerender = (next: ReactNode): Promise<void> =>
    act(async () => root.render(<MantineProvider env="test">{next}</MantineProvider>));
  const handle: Mounted = {
    container,
    rerender,
    unmount: async () => {
      mounted.delete(handle);
      await act(async () => root.unmount());
      container.remove();
    },
  };
  mounted.add(handle);
  await rerender(node);
  return handle;
}

export async function renderWithMantine(node: ReactNode): Promise<HTMLDivElement> {
  return (await mountWithMantine(node)).container;
}

export async function cleanupMounted(): Promise<void> {
  for (const handle of [...mounted]) await handle.unmount();
}

/** Lets TanStack Query's observer notifications, scheduled with `setTimeout(0)`, land inside `act`. */
export async function flushQueryNotifications(): Promise<void> {
  await act(async () => new Promise<void>((resolve) => setTimeout(resolve, 0)));
}
