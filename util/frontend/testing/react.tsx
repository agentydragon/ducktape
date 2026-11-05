import { MantineProvider } from "@mantine/core";
import { act, type ReactNode } from "react";
import { createRoot } from "react-dom/client";

const mounted: Array<{ root: ReturnType<typeof createRoot>; container: HTMLDivElement }> = [];

export async function renderWithMantine(node: ReactNode): Promise<HTMLDivElement> {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  mounted.push({ root, container });
  await act(async () => root.render(<MantineProvider env="test">{node}</MantineProvider>));
  return container;
}

export async function cleanupMounted(): Promise<void> {
  for (const { root, container } of mounted.splice(0)) {
    await act(async () => root.unmount());
    container.remove();
  }
}
